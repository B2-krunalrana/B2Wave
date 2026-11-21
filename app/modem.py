"""
AT-command engine for the SIMCom A7672S.

Works over two transports:
  * WiFi  - TCP to the ESP32-S3 GSM Bridge firmware (port 7000)
  * Serial - direct USB cable to the board's 4G_USB port (A7672S AT COM port)

Everything is asyncio; one command runs at a time, unsolicited result codes
(URCs: RING, +CLIP, +CMTI, +CLCC ...) are parsed continuously and pushed out
as events.
"""
from __future__ import annotations

import asyncio
import csv
import json
import re
import socket
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

try:
    import serial  # pyserial
    import serial.tools.list_ports
except ImportError:  # serial mode is optional
    serial = None

BRIDGE_PORT = 7000
DISCOVERY_PORT = 7002
SIMCOM_VID = 0x1E0E

FINAL_OK = ("OK",)
FINAL_ERR = ("ERROR", "+CME ERROR", "+CMS ERROR", "NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE")
# lines that are always unsolicited, never part of a command response
URC_PREFIXES = ("RING", "+CLIP:", "+CMTI:", "+CRING:", "VOICE CALL:", "MISSED_CALL:", "*ATREADY",
                "SMS DONE", "PB DONE", "+CPIN: NOT READY", "+CLCC:", "+CDS", "+CMT:",
                "+RXDTMF", "+DTMF", "RXDTMF", "+CCMXPLAY", "+CCMXSTOP", "+AUDIOSTATE",
                "+CTTS:", "+CGEV:")
# received key-press (DTMF) reports - different firmwares use different spellings
DTMF_RE = re.compile(r"^\+?(?:RX)?DTMF:\s*\"?([0-9A-D*#])\"?", re.I)

GSM_SAFE = re.compile(r"^[A-Za-z0-9 \r\n.,!?'\"@#%&*()\-+=/:;<>_$]*$")


# ======================================================================= transports
class TcpTransport:
    def __init__(self, host: str, port: int = BRIDGE_PORT):
        self.host, self.port = host, port
        self.reader = self.writer = None

    @property
    def label(self) -> str:
        return f"Wi-Fi bridge {self.host}:{self.port}"

    async def open(self):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=6)
        sock = self.writer.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    async def read(self) -> bytes:
        data = await self.reader.read(2048)
        if not data:
            raise ConnectionError("bridge closed the connection")
        return data

    async def write(self, data: bytes):
        self.writer.write(data)
        await self.writer.drain()

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass


class SerialTransport:
    def __init__(self, port: str, baud: int = 115200):
        self.port, self.baud = port, baud
        self.ser = None

    @property
    def label(self) -> str:
        return f"USB serial {self.port}"

    async def open(self):
        if serial is None:
            raise RuntimeError("pyserial not installed")
        self.ser = await asyncio.to_thread(serial.Serial, self.port, self.baud, timeout=0.1)

    async def read(self) -> bytes:
        ser = self.ser
        return await asyncio.to_thread(lambda: ser.read(max(1, ser.in_waiting)))

    async def write(self, data: bytes):
        await asyncio.to_thread(self.ser.write, data)

    async def close(self):
        if self.ser:
            await asyncio.to_thread(self.ser.close)


# ======================================================================= discovery
async def discover_bridges(timeout: float = 1.5) -> list[dict]:
    """Broadcast on UDP 7002; every GSM Bridge on the LAN answers with JSON."""
    loop = asyncio.get_running_loop()
    found: dict[str, dict] = {}

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            try:
                info = json.loads(data.decode())
                info.setdefault("ip", addr[0])
                found[info["ip"]] = info
            except Exception:
                pass

    transport, _ = await loop.create_datagram_endpoint(
        Proto, local_addr=("0.0.0.0", 0), allow_broadcast=True)
    try:
        for _ in range(3):
            transport.sendto(b"GSM_BRIDGE_DISCOVER", ("255.255.255.255", DISCOVERY_PORT))
            await asyncio.sleep(timeout / 3)
    finally:
        transport.close()

    if not found:  # fallback: mDNS name
        try:
            infos = await asyncio.wait_for(loop.getaddrinfo("gsm-bridge.local", BRIDGE_PORT,
                                                            family=socket.AF_INET), 2)
            ip = infos[0][4][0]
            found[ip] = {"ip": ip, "name": "gsm-bridge", "port": BRIDGE_PORT}
        except Exception:
            pass
    return list(found.values())


def list_serial_ports() -> list[dict]:
    if serial is None:
        return []
    out = []
    for p in serial.tools.list_ports.comports():
        out.append({"device": p.device, "description": p.description,
                    "simcom": p.vid == SIMCOM_VID, "vid": p.vid, "pid": p.pid})
    out.sort(key=lambda p: (not p["simcom"], "AT" not in p["description"].upper(), p["device"]))
    return out


# ======================================================================= helpers
def ucs2_encode(s: str) -> str:
    return s.encode("utf-16-be").hex().upper()


def ucs2_decode(h: str) -> str:
    return bytes.fromhex(h).decode("utf-16-be", errors="replace")


def _plausible_text(h: str) -> bool:
    try:
        t = bytes.fromhex(h).decode("utf-16-be")
    except Exception:
        return False
    return all(c.isprintable() or c in "\r\n\t" for c in t)


def looks_ucs2(body: str, length: Optional[int] = None, dcs: Optional[int] = None) -> bool:
    """Text-mode SMS bodies in UCS2 arrive as hex. The A7672S reports odd <dcs>/<length>
    values, so decide from the body itself; length only rules out short plain texts."""
    if not body or len(body) % 4 or not re.fullmatch(r"[0-9A-Fa-f]+", body):
        return False
    if length is not None and length == len(body):
        return False          # length matches char count -> plain text like "1234" / "CAFE"
    if not _plausible_text(body):
        return False
    if dcs is not None and (dcs & 0x0C) == 0x08:
        return True
    return length is not None or len(body) >= 8


def decode_text(body: str, length=None, dcs=None) -> str:
    if looks_ucs2(body, length, dcs):
        try:
            return ucs2_decode(body)
        except Exception:
            return body
    return body


def decode_addr(a: str) -> str:
    """Sender numbers/names come as plain text in IRA mode. Only decode when it is
    clearly UCS2 of ASCII ("002B0039..."), never a numeric short code like "57575701"."""
    if a and re.fullmatch(r"(00[0-7][0-9A-Fa-f]){2,}", a):
        try:
            d = ucs2_decode(a)
            if d.isprintable():
                return d
        except Exception:
            pass
    return a


def parse_scts(s: str) -> str:
    """'24/11/21,10:05:33+22' -> ISO string (local time of the network)."""
    try:
        return datetime.strptime(s[:17], "%y/%m/%d,%H:%M:%S").isoformat()
    except Exception:
        return datetime.now().isoformat(timespec="seconds")


def csv_fields(s: str) -> list[str]:
    return next(csv.reader([s], skipinitialspace=True))


def split_sms(text: str, limit: int) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


def operator_name(op: str) -> str:
    """Numeric PLMN -> friendly name for the common Indian operators."""
    if not op.isdigit():
        return op
    mcc, mnc = op[:3], op[3:]
    if mcc == "405" and mnc.isdigit() and 840 <= int(mnc) <= 874:
        return f"Jio ({op})"
    if mcc in ("404", "405"):
        airtel = {"02", "03", "10", "16", "31", "40", "45", "49", "51", "52", "53", "54", "55", "56",
                  "70", "90", "92", "93", "94", "95", "96", "97", "98"}
        vi = {"01", "05", "11", "13", "15", "20", "27", "43", "46", "60", "84", "86", "88"}
        if mnc in airtel:
            return f"Airtel ({op})"
        if mnc in vi:
            return f"Vi ({op})"
    return op


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ======================================================================= state
@dataclass
class CallState:
    state: str = "idle"          # idle | dialing | ringing | incoming | active | held
    number: str = ""
    direction: str = ""          # out | in
    started: float = 0.0         # epoch when active


@dataclass
class ModemStatus:
    connected: bool = False
    transport: str = ""
    error: str = ""
    notice: str = ""
    model: str = ""
    imei: str = ""
    sim: str = ""
    operator: str = ""
    network: str = ""            # registered / searching / denied / roaming
    signal: int = -1             # 0..31, 99 unknown
    signal_pct: int = 0
    phone: str = ""


# ======================================================================= modem
class Modem:
    def __init__(self, emit: Callable[[dict], Awaitable[None]]):
        self.emit = emit
        self.transport = None
        self.status = ModemStatus()
        self.call = CallState()
        self._lock = asyncio.Lock()
        self._pending: Optional[dict] = None
        self._buf = b""
        self._connected_evt = asyncio.Event()

    # ------------------------------------------------------------ low level
    async def _log(self, direction: str, line: str):
        await self.emit({"type": "log", "dir": direction, "line": line, "t": time.time()})

    async def _reader(self):
        while True:
            data = await self.transport.read()
            if not data:
                continue
            self._buf += data
            # SMS prompt "> " arrives without a newline
            if self._pending and self._pending.get("want_prompt") and b">" in self._buf:
                idx = self._buf.rfind(b">")
                if b"\n" not in self._buf[idx:]:
                    self._buf = b""
                    fut = self._pending["prompt"]
                    if not fut.done():
                        fut.set_result(True)
                    continue
            while b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    await self._handle_line(line)

    async def _handle_line(self, line: str):
        await self._log("rx", line)
        p = self._pending
        if p and line == p["cmd"]:  # echo
            return
        if p and p.get("want_prompt") and line.startswith("CONNECT"):   # FSWRITE's "ready for data"
            if not p["prompt"].done():
                p["prompt"].set_result(True)
            return
        is_urc = line.startswith(URC_PREFIXES)
        if line.startswith("+CLCC:") and p and p["cmd"].upper() == "AT+CLCC":
            is_urc = False
        if line == "NO CARRIER" and not (p and p["cmd"].upper().startswith("ATD")):
            is_urc = True
        if is_urc:
            asyncio.create_task(self._urc(line))
            return
        if p is None:
            # unsolicited lines we do not know as URC prefixes
            if line.startswith(("+CREG:", "+CEREG:", "+CGREG:")):
                asyncio.create_task(self._urc(line))
            return
        if line in FINAL_OK or line.startswith(FINAL_ERR):
            if not p["future"].done():
                p["future"].set_result((line in FINAL_OK, p["lines"], line))
        else:
            p["lines"].append(line)

    async def cmd(self, command: str, timeout: float = 6.0) -> tuple[bool, list[str], str]:
        """Send one AT command, return (ok, response_lines, final_line)."""
        if not self.status.connected or not self.transport:
            raise ConnectionError("modem not connected")
        async with self._lock:
            loop = asyncio.get_running_loop()
            self._pending = {"cmd": command, "lines": [], "future": loop.create_future()}
            try:
                await self._log("tx", command)
                await self.transport.write((command + "\r").encode())
                return await asyncio.wait_for(self._pending["future"], timeout)
            except asyncio.TimeoutError:
                return False, self._pending["lines"], "TIMEOUT"
            finally:
                self._pending = None

    async def cmd_ok(self, command: str, timeout: float = 6.0) -> list[str]:
        ok, lines, final = await self.cmd(command, timeout)
        if not ok:
            raise RuntimeError(f"{command} -> {final}")
        return lines

    # ------------------------------------------------------------ lifecycle
    async def connect(self, transport):
        await self.disconnect()
        self.transport = transport
        await transport.open()
        self.status.connected = True
        self.status.transport = transport.label
        self.status.error = ""
        self._buf = b""
        return asyncio.create_task(self._reader())

    async def disconnect(self):
        if self.transport:
            try:
                await self.transport.close()
            except Exception:
                pass
        self.transport = None
        self.status.connected = False

    async def initialize(self):
        # wake up / sync auto-baud
        for _ in range(5):
            ok, _, _ = await self.cmd("AT", 1.5)
            if ok:
                break
        else:
            raise RuntimeError("modem does not answer AT")
        for c in ("ATE0", "AT+CMEE=2", "AT+CMGF=1", 'AT+CSCS="IRA"', "AT+CSDH=1",
                  "AT+CNMI=2,1,0,0,0", "AT+CLIP=1", "AT+CREG=2"):
            await self.cmd(c)
        await self.cmd("AT+CLCC=1")       # live call-state reports (if supported)
        await self.cmd("AT+COPS=3,0")     # operator as a name, not "405857"
        await self.cmd("AT+CSMP=17,167,0,0")
        self.status.model = " ".join(await self._q("AT+CGMM")) or "A7672S"
        self.status.imei = " ".join(await self._q("AT+CGSN"))
        num = await self._q("AT+CNUM")
        if num and num[0].startswith("+CNUM:"):
            f = csv_fields(num[0][6:])
            self.status.phone = f[1] if len(f) > 1 else ""
        await self.poll_status()
        await self.recover_call()

    async def recover_call(self):
        """After an app restart, pick up a call that is still running on the module."""
        for l in await self._q("AT+CLCC"):
            if l.startswith("+CLCC:"):
                await self._urc(l)

    async def _q(self, c: str) -> list[str]:
        ok, lines, _ = await self.cmd(c)
        return lines if ok else []

    async def poll_status(self):
        st = self.status
        lines = await self._q("AT+CPIN?")
        st.sim = lines[0].split(":", 1)[1].strip() if lines else "NO SIM / ERROR"
        for l in await self._q("AT+CSQ"):
            if l.startswith("+CSQ:"):
                rssi = int(l[5:].split(",")[0])
                st.signal = rssi
                st.signal_pct = 0 if rssi == 99 else min(100, round(rssi * 100 / 31))
        reg = ""
        for c in ("AT+CEREG?", "AT+CREG?"):
            for l in await self._q(c):
                if ":" in l:
                    parts = l.split(":", 1)[1].split(",")
                    if len(parts) > 1:
                        reg = parts[1].strip()
            if reg in ("1", "5"):
                break
        st.network = {"0": "not searching", "1": "registered", "2": "searching", "3": "denied",
                      "4": "unknown", "5": "roaming"}.get(reg, "unknown")
        for l in await self._q("AT+COPS?"):
            if l.startswith("+COPS:"):
                f = csv_fields(l[6:])
                st.operator = operator_name(f[2]) if len(f) > 2 else ""
        await self.emit_status()

    async def emit_status(self):
        await self.emit({"type": "status", "status": asdict(self.status), "call": asdict(self.call)})

    # ------------------------------------------------------------ URCs
    async def _urc(self, line: str):
        c = self.call
        if line.startswith("+CLCC:"):
            f = csv_fields(line[6:])
            try:
                direction = "in" if f[1] == "1" else "out"
                stat = int(f[2])
                number = f[5] if len(f) > 5 else c.number
            except (IndexError, ValueError):
                return
            new = {0: "active", 1: "held", 2: "dialing", 3: "ringing", 4: "incoming",
                   5: "incoming", 6: "idle"}.get(stat, c.state)
            await self._set_call(new, number, direction)
        elif line.startswith("+CLIP:"):
            number = csv_fields(line[6:])[0]
            if c.state in ("idle", "incoming"):
                await self._set_call("incoming", number, "in")
        elif line in ("RING",) or line.startswith("+CRING"):
            if c.state == "idle":
                await self._set_call("incoming", c.number, "in")
        elif line.startswith("VOICE CALL: BEGIN"):
            await self._set_call("active", c.number, c.direction)
        elif line.startswith(("VOICE CALL: END", "NO CARRIER", "BUSY", "NO ANSWER")):
            await self._set_call("idle", c.number, c.direction)
        elif line.startswith("MISSED_CALL"):
            await self._set_call("idle", c.number, "in")
        elif DTMF_RE.match(line):
            key = DTMF_RE.match(line).group(1)
            await self.emit({"type": "dtmf", "key": key, "number": c.number, "time": now_iso()})
        elif line.startswith(("+CCMXPLAY", "+CCMXSTOP", "+AUDIOSTATE", "+CTTS:")):
            await self.emit({"type": "audio_event", "line": line, "time": now_iso()})
        elif line.startswith(("+CMTI:", "+CDS")):
            try:
                index = int(line.split(",")[-1])
                await self.read_sms(index)
            except Exception as e:
                await self._log("err", f"read new sms failed: {e}")
        elif line.startswith(("*ATREADY", "SMS DONE")):
            # module rebooted -> re-apply settings
            asyncio.create_task(self._reinit_later())
        elif line.startswith(("+CREG:", "+CEREG:")):
            asyncio.create_task(self.poll_status())

    async def _reinit_later(self):
        await asyncio.sleep(2)
        try:
            await self.initialize()
        except Exception as e:
            await self._log("err", f"re-init failed: {e}")

    async def _set_call(self, state: str, number: str, direction: str):
        c = self.call
        prev = c.state
        if state == prev and number == c.number:
            return
        if state == "active" and prev != "active":
            c.started = time.time()
        if state == "idle" and prev != "idle":
            await self.emit({"type": "call_ended", "number": c.number or number,
                             "direction": c.direction or direction,
                             "answered": prev in ("active", "held"),
                             "duration": int(time.time() - c.started) if c.started else 0,
                             "time": now_iso()})
            self.call = CallState()
        else:
            c.state, c.number, c.direction = state, number or c.number, direction or c.direction
        await self.emit_status()

    # ------------------------------------------------------------ SMS
    async def send_sms(self, number: str, text: str) -> int:
        number = re.sub(r"[^\d+*#]", "", number)
        if not number:
            raise ValueError("invalid phone number")
        unicode = not GSM_SAFE.match(text)
        parts = split_sms(text, 70 if unicode else 160)
        if unicode:
            await self.cmd_ok("AT+CSMP=17,167,0,8")
            await self.cmd_ok('AT+CSCS="UCS2"')
        try:
            for part in parts:
                da = ucs2_encode(number) if unicode else number
                body = ucs2_encode(part) if unicode else part
                await self._cmgs(da, body)
        finally:
            if unicode:
                await self.cmd('AT+CSCS="IRA"')
                await self.cmd("AT+CSMP=17,167,0,0")
        return len(parts)

    async def _cmgs(self, da: str, body: str):
        async with self._lock:
            loop = asyncio.get_running_loop()
            cmd = f'AT+CMGS="{da}"'
            self._pending = {"cmd": cmd, "lines": [], "future": loop.create_future(),
                             "want_prompt": True, "prompt": loop.create_future()}
            try:
                await self._log("tx", cmd)
                await self.transport.write((cmd + "\r").encode())
                done, _ = await asyncio.wait({self._pending["prompt"], self._pending["future"]},
                                             timeout=8, return_when=asyncio.FIRST_COMPLETED)
                if self._pending["future"].done():
                    _, _, final = self._pending["future"].result()
                    raise RuntimeError(f"modem refused SMS: {final}")
                if not done:
                    await self.transport.write(b"\x1b")  # cancel
                    raise RuntimeError("no '>' prompt from modem")
                await self._log("tx", body + " <Ctrl+Z>")
                await self.transport.write(body.encode() + b"\x1a")
                ok, lines, final = await asyncio.wait_for(self._pending["future"], 60)
                if not ok:
                    raise RuntimeError(f"SMS failed: {final}")
            except asyncio.TimeoutError:
                raise RuntimeError("SMS timed out (no network?)")
            finally:
                self._pending = None

    def _parse_cmgl(self, lines: list[str]) -> list[dict]:
        msgs, cur = [], None
        for l in lines:
            if l.startswith("+CMGL:"):
                f = csv_fields(l[6:])
                # <length> is always the LAST field (A7672S adds fo/pid/dcs/sca before it)
                length = int(f[-1]) if len(f) > 6 and f[-1].strip().isdigit() else None
                cur = {"sim_index": int(f[0]), "status": f[1], "number": decode_addr(f[2]),
                       "time": parse_scts(f[4]) if len(f) > 4 else now_iso(),
                       "_len": length, "_body": []}
                msgs.append(cur)
            elif cur is not None:
                cur["_body"].append(l)
        for m in msgs:
            m["text"] = decode_text("\n".join(m.pop("_body")), m.pop("_len"))
        return msgs

    async def list_sms(self) -> list[dict]:
        ok, lines, final = await self.cmd('AT+CMGL="ALL"', 20)
        if not ok:
            raise RuntimeError(final)
        return self._parse_cmgl(lines)

    async def read_sms(self, index: int):
        ok, lines, _ = await self.cmd(f"AT+CMGR={index}", 10)
        if not ok or not lines or not lines[0].startswith("+CMGR:"):
            return
        f = csv_fields(lines[0][6:])
        length = int(f[-1]) if len(f) > 5 and f[-1].strip().isdigit() else None
        msg = {"sim_index": index, "status": f[0], "number": decode_addr(f[1]),
               "time": parse_scts(f[3]) if len(f) > 3 else now_iso(),
               "text": decode_text("\n".join(lines[1:]), length)}
        await self.emit({"type": "sms_in", "msg": msg})

    async def delete_sms(self, index: int):
        await self.cmd_ok(f"AT+CMGD={index}")

    # ------------------------------------------------------------ calls
    async def dial(self, number: str):
        number = re.sub(r"[^\d+*#]", "", number)
        if not number:
            raise ValueError("invalid phone number")
        await self._set_call("dialing", number, "out")
        ok, _, final = await self.cmd(f"ATD{number};", 30)
        if not ok:
            await self._set_call("idle", number, "out")
            raise RuntimeError(f"dial failed: {final}")

    async def answer(self):
        await self.cmd_ok("ATA", 10)
        await self._set_call("active", self.call.number, "in")

    async def hangup(self):
        ok, _, _ = await self.cmd("AT+CHUP", 8)
        if not ok:
            await self.cmd("ATH", 8)
        await self._set_call("idle", self.call.number, self.call.direction)

    async def dtmf(self, digit: str):
        await self.cmd_ok(f"AT+VTS={digit[:1]}")

    async def set_volume(self, level: int):
        await self.cmd_ok(f"AT+CLVL={max(0, min(5, int(level)))}")

    # ------------------------------------------------------------ audio files on the module
    async def list_files(self) -> dict:
        """Files in the module's C:/ flash plus free space."""
        files = []
        for l in await self._q("AT+FSLS"):
            if l.startswith("+FSLS") or not l.strip():
                continue
            name = l.strip()
            size = 0
            for a in await self._q(f'AT+FSATTRI="{name}"'):
                if a.startswith("+FSATTRI:"):
                    size = int(a.split(":")[1].strip() or 0)
            files.append({"name": name, "size": size})
        total = free = 0
        for l in await self._q("AT+FSMEM"):
            mm = re.search(r"\((\d+),(\d+)\)", l)
            if mm:
                total, used = int(mm.group(1)), int(mm.group(2))
                free = total - used
        return {"files": files, "total": total, "free": free}

    async def _send_with_prompt(self, cmd: str, payload: bytes, timeout: float) -> tuple[bool, list[str], str]:
        """Send a command that answers '>' then takes raw bytes (CFTRANRX / FSWRITE)."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            self._pending = {"cmd": cmd, "lines": [], "future": loop.create_future(),
                             "want_prompt": True, "prompt": loop.create_future()}
            try:
                await self._log("tx", cmd)
                await self.transport.write((cmd + "\r").encode())
                done, _ = await asyncio.wait({self._pending["prompt"], self._pending["future"]},
                                             timeout=8, return_when=asyncio.FIRST_COMPLETED)
                if self._pending["future"].done():
                    return self._pending["future"].result()
                if not done:
                    return False, [], "no '>' prompt"
                self._pending["want_prompt"] = False
                await self.transport.write(payload)
                await self._log("tx", f"<{len(payload)} bytes>")
                return await asyncio.wait_for(self._pending["future"], timeout)
            except asyncio.TimeoutError:
                return False, self._pending["lines"], "TIMEOUT"
            finally:
                self._pending = None

    async def upload_file(self, name: str, data: bytes, progress=None):
        """Write a file into the module's C:/ flash in 8 KB chunks.
        (A single AT+CFTRANRX transfer fails above ~20-30 KB on this firmware.)"""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
            raise ValueError("file name: use letters, digits, _ . - only")
        await self.cmd(f'AT+FSDEL="{name}"')                       # overwrite cleanly
        lines = await self.cmd_ok(f'AT+FSOPEN="c:/{name}",1')
        handle = next((l.split(":")[1].strip() for l in lines if l.startswith("+FSOPEN:")), None)
        if handle is None:
            raise RuntimeError("module did not open the file")
        CH = 8192
        try:
            for i in range(0, len(data), CH):
                part = data[i:i + CH]
                ok, _, final = await self._send_with_prompt(f"AT+FSWRITE={handle},{len(part)},10", part, 20)
                if not ok:
                    raise RuntimeError(f"write failed at {i // 1024} KB: {final}")
                if progress:
                    await progress(min(len(data), i + CH), len(data))
        finally:
            await self.cmd(f"AT+FSCLOSE={handle}")
        size = 0
        for a in await self._q(f'AT+FSATTRI="{name}"'):
            if a.startswith("+FSATTRI:"):
                size = int(a.split(":")[1].strip() or 0)
        if size != len(data):
            raise RuntimeError(f"file check failed: wrote {len(data)} bytes, module has {size}")

    async def delete_file(self, name: str):
        await self.cmd_ok(f'AT+FSDEL="{name}"')

    async def play_file(self, name: str, remote: bool = True, repeat: int = 0):
        """Play a module file. remote=True -> into the call (the other person hears it)."""
        await self.cmd(f"AT+CDTAM={1 if remote else 0}")
        await self.cmd_ok(f'AT+CCMXPLAY="c:/{name}",{1 if remote else 0},{repeat}', 10)

    async def stop_audio(self):
        await self.cmd("AT+CCMXSTOP")

    async def speak(self, text: str, remote: bool = True):
        """Text-to-speech (English) into the call."""
        text = re.sub(r'[^A-Za-z0-9 .,?!\'-]', " ", text)[:250]
        await self.cmd(f"AT+CDTAM={1 if remote else 0}")
        await self.cmd_ok(f'AT+CTTS=2,"{text}"', 10)
