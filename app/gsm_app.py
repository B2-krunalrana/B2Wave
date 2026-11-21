"""
B2Wave - browser app for the KTRON ESP32-S3 + A7672S 4G board.

    python gsm_app.py            -> opens http://127.0.0.1:8080
    python gsm_app.py --lan      -> also reachable from phones on your Wi-Fi

Connects to the board over Wi-Fi (ESP32 bridge firmware, auto-discovered)
or over a USB cable (A7672S AT COM port). Send/receive SMS, place/answer
calls, live AT console.
"""
from __future__ import annotations

import argparse
import sys
import time
import re
import os
import asyncio
import json
import logging
import uuid
import webbrowser
from collections import deque
from dataclasses import asdict
from pathlib import Path

import aiohttp
from aiohttp import web, WSMsgType

import modem as m
import flasher
import campaign as camp_mod
import bulksms
import mailer
import livekit_api as lk
import backup as bk
import base64

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("B2W_DATA") or ROOT / "data")
STATIC = ROOT / "static"
DATA.mkdir(exist_ok=True)
AUDIO = DATA / "audio"          # PC-side prompt library (source of truth)
AUDIO.mkdir(exist_ok=True)
REC_DIR = DATA / "recordings"          # campaign call recordings (AMR)
REC_DIR.mkdir(exist_ok=True)

log = logging.getLogger("gsm")


# ======================================================================= storage
class Store:
    def __init__(self, path: Path, default):
        self.path, self.default = path, default
        try:
            self.data = json.loads(path.read_text("utf-8"))
        except Exception:
            self.data = default

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(self.path)


config = Store(DATA / "config.json", {"mode": "auto", "host": "", "serial_port": "", "baud": 115200,
                                      "last_host": ""})
messages = Store(DATA / "messages.json", [])
calls = Store(DATA / "calls.json", [])
campaigns = Store(DATA / "campaigns.json", [])
bulk = Store(DATA / "bulk.json", [])
ai_calls = Store(DATA / "ai_calls.json", [])
drafts = Store(DATA / "drafts.json", [])
sms_templates = Store(DATA / "sms_templates.json", [
    {"id": "t_confirm", "name": "Thank you / confirmed", "text": "Thank you {name|}! Your response has been recorded: {choice|confirmed}."},
    {"id": "t_missed", "name": "We tried to call you", "text": "Hello {name|there}, we tried to call you today. Please call us back at your convenience."},
    {"id": "t_reminder", "name": "Appointment reminder", "text": "Hi {name|there}, this is a reminder about your appointment tomorrow. Reply YES to confirm."},
])
AGENT_DIR = ROOT.parent / "agent"
dtmf_log = deque(maxlen=200)
console = deque(maxlen=400)
clients: set[web.WebSocketResponse] = set()
reconnect_evt = asyncio.Event()


# ======================================================================= events
async def broadcast(evt: dict):
    dead = []
    data = json.dumps(evt, ensure_ascii=False)
    for ws in clients:
        try:
            await ws.send_str(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


def add_message(msg: dict) -> dict | None:
    key = (msg["number"], msg["time"], msg["text"])
    for x in messages.data:
        if (x["number"], x["time"], x["text"]) == key:
            if msg.get("sim_index") is not None:
                x["sim_index"] = msg["sim_index"]
            return None
    msg.setdefault("id", uuid.uuid4().hex[:12])
    messages.data.append(msg)
    messages.save()
    return msg


async def apply_sim_identity():
    """Manual number for SIMs that don't store their own number; detect SIM swaps."""
    st = modem.status
    if not st.iccid:
        return None
    manual = (config.data.get("sim_numbers") or {}).get(st.iccid)
    if manual and st.phone_src != "sim":
        st.phone, st.phone_src = manual, "manual"
    prev = config.data.get("last_iccid")
    if prev != st.iccid:
        config.data["last_iccid"] = st.iccid
        config.save()
        if prev:
            log.info("SIM changed %s -> %s (%s)", prev[-6:], st.iccid[-6:], st.phone or "number unknown")
            return {"type": "sim_changed", "iccid": st.iccid, "phone": st.phone, "operator": st.operator}
    return None


async def on_modem_event(evt: dict):
    t = evt["type"]
    if t == "status":
        changed = await apply_sim_identity()
        evt["status"] = asdict(modem.status)
        if changed:
            await broadcast(changed)
    if t == "log":
        console.append(evt)
        logging.getLogger("gsm.modem").debug("%s %s", evt["dir"], evt["line"])
    elif t == "dtmf":
        runner.on_dtmf(evt["key"])
        dtmf_log.append(evt)
    elif t in ("call_event", "audio_event"):
        runner.on_event(evt)
        if evt.get("ev") == "cause" and calls.data:          # enrich the manual call log
            last = calls.data[-1]
            if not last.get("cause"):
                last["cause"], last["ended_by"] = evt.get("detail", ""), evt.get("ended_by", "")
                calls.save()
                await broadcast({"type": "calls", "calls": calls.data})
    elif t == "sms_in":
        raw = evt["msg"]
        for x in messages.data:                     # SIM slot reused -> unlink old entry
            if x.get("sim_index") == raw["sim_index"]:
                x["sim_index"] = None
        msg = add_message({"dir": "in", "number": raw["number"], "text": raw["text"],
                           "time": raw["time"], "sim_index": raw["sim_index"], "read": False})
        if msg:
            await broadcast({"type": "message", "msg": msg, "notify": True})
        return
    elif t == "call_ended":
        rec = {k: evt[k] for k in ("number", "direction", "answered", "duration", "time")}
        rec["id"] = uuid.uuid4().hex[:12]
        calls.data.append(rec)
        calls.data[:] = calls.data[-500:]
        calls.save()
        await broadcast({"type": "call_log", "call": rec})
        return
    await broadcast(evt)


modem = m.Modem(on_modem_event)
runner = camp_mod.CampaignRunner(modem, broadcast, campaigns)


class AgentWorker:
    """Runs agent/agent.py as a child process so users never touch a terminal."""
    def __init__(self):
        self.proc = None
        self.state = "stopped"          # stopped | installing | starting | ready | error
        self.log = deque(maxlen=300)
        self.task = None

    @property
    def python(self) -> Path:
        return AGENT_DIR / ".venv" / "Scripts" / "python.exe"

    def info(self) -> dict:
        return {"state": self.state, "installed": self.python.exists(), "running": bool(self.proc and self.proc.returncode is None),
                "log": list(self.log)[-80:], "env": (AGENT_DIR / ".env").exists()}

    async def _line(self, text: str):
        text = text.rstrip()
        if not text:
            return
        try:                                    # agents print JSON logs: show the message (+ the real error)
            j = json.loads(text)
            text = f"{j.get('level', '')[:4]} {j.get('message', '')}" + (f" ({j['name']})" if j.get("name", "").startswith("b2wave") else "")
            if j.get("exc_info"):
                last = [l for l in str(j["exc_info"]).splitlines() if l.strip()][-1:]
                text += "  -> " + (last[0].strip()[:220] if last else "")
        except Exception:
            pass
        self.log.append(text)
        if "registered worker" in text:
            self.state = "ready"
        elif "worker failed" in text or "Traceback" in text:
            self.state = "error"
        await broadcast({"type": "agent_worker", "line": text, "state": self.state})

    async def _run(self, *cmd, cwd=None) -> int:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd or AGENT_DIR, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT,
                                                    creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))
        async for raw in proc.stdout:
            await self._line(raw.decode(errors="replace"))
        return await proc.wait()

    async def install(self):
        self.state = "installing"
        await self._line("Setting up the AI agent (one time, about 2-4 minutes)...")
        try:
            if not self.python.exists():
                import shutil
                if shutil.which("py"):
                    code = await self._run("py", "-3.12", "-m", "venv", str(AGENT_DIR / ".venv"))
                    if code != 0:
                        code = await self._run(sys.executable, "-m", "venv", str(AGENT_DIR / ".venv"))
                else:
                    code = await self._run(sys.executable, "-m", "venv", str(AGENT_DIR / ".venv"))
                if code != 0:
                    raise RuntimeError("could not create the agent environment")
            code = await self._run(str(self.python), "-m", "pip", "install", "--disable-pip-version-check",
                                   "--progress-bar", "off", "-r", "requirements.txt")
            if code != 0:
                raise RuntimeError("pip install failed (see log)")
            self.state = "stopped"
            await self._line("Agent installed. Click Start.")
        except Exception as e:
            self.state = "error"
            await self._line(f"Install failed: {e}")

    async def start(self):
        if self.proc and self.proc.returncode is None:
            return
        if not self.python.exists():
            raise RuntimeError("Install the agent first.")
        if not (AGENT_DIR / ".env").exists():
            raise RuntimeError("Save your LiveKit settings first (step 1).")
        self.state = "starting"
        self.proc = await asyncio.create_subprocess_exec(
            str(self.python), "agent.py", "start", cwd=AGENT_DIR, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))
        await self._line(f"Agent worker started (pid {self.proc.pid})")

        async def pump(proc):
            async for raw in proc.stdout:
                await self._line(raw.decode(errors="replace"))
            code = await proc.wait()
            if self.proc is proc:
                self.state = "stopped" if code in (0, 1, -15) and self.state != "error" else "error"
                await self._line(f"Agent worker exited (code {code})")
        self.task = asyncio.create_task(pump(self.proc))

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            if sys.platform == "win32":             # the venv launcher spawns a child: end the whole tree
                await asyncio.to_thread(__import__("subprocess").run, ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                                        capture_output=True, creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))
            else:
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 8)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.state = "stopped"
        await self._line("Agent worker stopped")


agent_worker = AgentWorker()


async def record_sent(number: str, text: str, status: str, error: str):
    """Bulk SMS also appears in the normal Messages threads."""
    msg = {"dir": "out", "number": number, "text": text, "time": m.now_iso(), "read": True,
           "status": status, "error": error, "id": uuid.uuid4().hex[:12], "bulk": True}
    messages.data.append(msg)
    messages.save()
    await broadcast({"type": "message", "msg": msg})


async def email_report(kind: str, obj: dict):
    """Email a finished campaign / bulk job to its recipients (if set and SMTP configured)."""
    to = obj.get("email_to") or ""
    smtp = config.data.get("smtp") or {}
    if not mailer.parse_recipients(to):
        return
    if not mailer.configured(smtp):
        obj["email_status"] = "not sent: email is not set up in Settings"
        return
    if kind == "campaign":
        subject, html_body, text = mailer.campaign_report(obj)
        att = [(re.sub(r"[^\w-]+", "_", obj["name"]) + ".csv", camp_mod.to_csv(obj).encode("utf-8-sig"), "text/csv")]
    else:
        subject, html_body, text = mailer.bulk_report(obj)
        att = [(re.sub(r"[^\w-]+", "_", obj["name"]) + ".csv", bulksms.to_csv(obj).encode("utf-8-sig"), "text/csv")]
    try:
        sent_to = await mailer.send(smtp, to, subject, html_body, text, att)
        obj["email_status"] = "sent to " + ", ".join(sent_to)
        log.info("report emailed to %s", sent_to)
    except Exception as e:
        obj["email_status"] = f"failed: {e}"
        log.warning("report email failed: %s", e)


async def _campaign_done(camp):
    await email_report("campaign", camp)

runner.on_done = _campaign_done
runner.record_sent = None   # set after record_sent is defined
runner.rec_dir = REC_DIR


async def _make_room(need: int, keep: str):
    """Free module flash for a recording: stale recordings first, then prompts that have a PC copy."""
    st = await modem.list_files()
    files = st["files"]
    free = st["free"]
    stale = [f for f in files if re.fullmatch(r"r[0-9a-f]{6}\d+\.amr", f["name"])]
    ours = [f for f in files if f["name"] != keep and (AUDIO / f["name"]).exists()]
    for f in stale + sorted(ours, key=lambda f: (AUDIO / f["name"]).stat().st_mtime):
        if free >= need:
            break
        await modem.delete_file(f["name"])
        free += f["size"]
        log.info("freed %s (%d KB) on the module for a call recording", f["name"], f["size"] // 1024)

runner.make_room = _make_room


async def _bulk_done(job):
    await email_report("bulk", job)

bulk_runner = bulksms.BulkSmsRunner(modem, broadcast, bulk, record_sent, _bulk_done)
runner.record_sent = record_sent


def repair_stored_messages():
    """Older versions stored some Unicode SMS as raw UCS2 hex - decode them once."""
    changed = 0
    for x in messages.data:
        num = x.get("number", "")
        # undo old mis-decoding of numeric short codes ("57575701" -> CJK characters)
        if num and all(ord(ch) > 0x2E7F for ch in num) and len(num) <= 8:
            x["number"] = m.ucs2_encode(num)
            changed += 1
        if x.get("dir") == "in" and m.looks_ucs2(x.get("text", "")):
            x["text"] = m.ucs2_decode(x["text"])
            changed += 1
    if changed:
        messages.save()
        log.info("repaired %d stored Unicode messages", changed)


repair_stored_messages()

for _j in bulk.data:                          # app restarted while a bulk job was running
    if _j.get("status") == "running":
        _j["status"] = "interrupted"
        for _r in _j["results"]:
            if _r["status"] in ("pending", "sending"):
                _r["status"] = "not sent (interrupted)"
bulk.save()
for _c in campaigns.data:                     # app restarted while a campaign was running
    if _c.get("status") == "running":
        _c["status"] = "interrupted"
        for _r in _c["results"]:
            if _r["status"] in ("dialing", "ringing", "pending"):
                _r["status"] = "not called (interrupted)"
campaigns.save()


async def sync_inbox():
    try:
        sim_msgs = await modem.list_sms()
    except Exception as e:
        log.warning("inbox sync failed: %s", e)
        return
    for x in messages.data:
        if x["dir"] == "in":
            x["sim_index"] = None
    for s in sim_msgs:
        add_message({"dir": "in", "number": s["number"], "text": s["text"], "time": s["time"],
                     "sim_index": s["sim_index"], "read": s["status"] != "REC UNREAD"})
    messages.save()
    await broadcast({"type": "messages", "messages": messages.data})


# ======================================================================= connection supervisor
async def probe_serial(port: str) -> bool:
    t = m.SerialTransport(port)
    try:
        await t.open()
        await t.write(b"AT\r")
        buf = b""
        for _ in range(10):
            buf += await t.read()
            if b"OK" in buf:
                return True
        return False
    except Exception:
        return False
    finally:
        await t.close()


def find_simcom_at_port() -> str:
    """The 4G module's AT port, whatever COM number Windows gave it this time."""
    ports = [p for p in m.list_serial_ports() if p["simcom"]]
    ports.sort(key=lambda p: ("AT" not in p["description"].upper(), p["device"]))
    return ports[0]["device"] if ports else ""


async def bridge_reachable(host: str) -> bool:
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, m.BRIDGE_PORT), 2)
        w.close()
        return True
    except Exception:
        return False


async def pick_transport():
    """Choose the link to the 4G module, robust to COM-number changes and unplugged cables.
    Order: configured link -> same module on a new COM port -> any SIMCom AT port -> Wi-Fi bridge."""
    c = config.data
    modem.status.notice = ""
    if c["mode"] == "wifi" and c["host"]:
        return m.TcpTransport(c["host"])
    if c["mode"] == "serial":
        ports = {p["device"] for p in m.list_serial_ports()}
        if c.get("serial_port") in ports:
            return m.SerialTransport(c["serial_port"], c.get("baud", 115200))
        at = find_simcom_at_port()
        if at:                                               # replugged -> new COM number
            log.info("4G module moved from %s to %s", c.get("serial_port"), at)
            c["serial_port"] = at
            config.save()
            return m.SerialTransport(at)
        host = c.get("last_host") or c.get("host")
        if host and await bridge_reachable(host):            # cable unplugged -> Wi-Fi fallback
            modem.status.notice = "4G_USB cable not found, using the Wi-Fi bridge instead."
            return m.TcpTransport(host)
        modem.status.error = ("4G_USB cable not detected. Plug cable 2 into the 4G_USB port, "
                              "keep the USB_SELECT switch on 4G.")
        return None
    # auto: SIMCom USB port first (most reliable), then Wi-Fi bridge
    at = find_simcom_at_port()
    if at and await probe_serial(at):
        return m.SerialTransport(at)
    if c.get("last_host") and await bridge_reachable(c["last_host"]):
        return m.TcpTransport(c["last_host"])
    found = await m.discover_bridges()
    if found:
        c["last_host"] = found[0]["ip"]
        config.save()
        return m.TcpTransport(found[0]["ip"])
    return None


async def wait_reconnect(timeout: float) -> bool:
    try:
        await asyncio.wait_for(reconnect_evt.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        reconnect_evt.clear()


def friendly_error(e: Exception) -> str:
    t = str(e) or e.__class__.__name__
    if "could not open port" in t or "PermissionError" in t or "Access is denied" in t:
        return "The 4G USB port is busy or was unplugged. Close other serial tools, check cable 2, then wait."
    if "ClearCommError" in t or "device does not recognize" in t.lower() or "WriteFile failed" in t:
        return "The 4G_USB cable was unplugged. Plug it back in and B2Wave reconnects by itself."
    if "does not answer AT" in t:
        return "Connected, but the 4G module isn't answering. It may be starting up (wait 20 s) or need more power."
    if "refused" in t or "timed out" in t.lower() or "WinError 1225" in t:
        return "Can't reach the board over Wi-Fi. Is it powered and on the same network?"
    return t


async def supervisor():
    while True:
        reader = None
        try:
            modem.status.error = "Searching for the board..."
            await modem.emit_status()
            transport = await pick_transport()
            if transport is None:
                if not modem.status.error or modem.status.error.startswith("Searching"):
                    modem.status.error = ("Board not found. Plug the 4G_USB cable (switch on 4G), "
                                          "or put the board and this PC on the same Wi-Fi.")
                await modem.emit_status()
                await wait_reconnect(5)
                continue
            log.info("connecting via %s", transport.label)
            reader = await modem.connect(transport)
            await modem.initialize()
            await sync_inbox()
            while not reader.done():
                done, _ = await asyncio.wait({reader}, timeout=15)
                if reconnect_evt.is_set():
                    reconnect_evt.clear()
                    break
                if not done:
                    await modem.poll_status()
            if reader.done() and reader.exception():
                raise reader.exception()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("connection problem: %s", e)
            modem.status.error = friendly_error(e)
            log.debug("supervisor error", exc_info=True)
        finally:
            if reader and not reader.done():
                reader.cancel()
            await modem.disconnect()
            await modem.emit_status()
        await wait_reconnect(3)


# ======================================================================= websocket API
async def library_state() -> dict:
    """PC library + what is on the module."""
    mod = {"files": [], "total": 0, "free": 0, "error": ""}
    if modem.status.connected:
        try:
            mod = await modem.list_files()
            mod["error"] = ""
        except Exception as e:
            mod["error"] = str(e)
    else:
        mod["error"] = "board offline"
    on_mod = {f["name"]: f["size"] for f in mod["files"]}
    lib = []
    for f in sorted(AUDIO.glob("*")):
        if f.is_file():
            size = f.stat().st_size
            lib.append({"name": f.name, "size": size, "on_pc": True,
                        "on_module": on_mod.get(f.name) == size})
    names = {x["name"] for x in lib}
    for n, size in on_mod.items():                     # files that exist only on the module
        if n not in names:
            lib.append({"name": n, "size": size, "on_pc": False, "on_module": True})
    return {"files": lib, "total": mod["total"], "free": mod["free"], "module_error": mod["error"]}


async def ensure_on_module(name: str):
    """Copy a PC library prompt to the module if missing/changed, making room if needed."""
    src = AUDIO / name
    if not src.exists():
        return                                         # module-only file: nothing to sync
    data = src.read_bytes()
    st = await modem.list_files()
    if any(f["name"] == name and f["size"] == len(data) for f in st["files"]):
        return
    free = st["free"] + next((f["size"] for f in st["files"] if f["name"] == name), 0)
    if len(data) > free - 8192:
        # make room: remove other B2Wave prompts (ones we also have on the PC), oldest first
        ours = [f for f in st["files"] if f["name"] != name and (AUDIO / f["name"]).exists()]
        ours.sort(key=lambda f: (AUDIO / f["name"]).stat().st_mtime)
        for f in ours:
            await modem.delete_file(f["name"])
            free += f["size"]
            log.info("removed %s from module to make room", f["name"])
            if len(data) <= free - 8192:
                break
    if len(data) > free - 8192:
        raise RuntimeError(f"needs {len(data)//1024} KB but the module has only {max(0, free - 8192)//1024} KB free")

    async def prog(done, total):
        await broadcast({"type": "upload_progress", "name": name, "done": done, "total": total})
    await modem.upload_file(name, data, prog)


async def handle_op(op: str, d: dict):
    if op == "send_sms":
        to, text = d["to"].strip(), d["text"]
        msg = {"dir": "out", "number": to, "text": text, "time": m.now_iso(), "read": True,
               "status": "sending", "id": uuid.uuid4().hex[:12]}
        messages.data.append(msg)
        await broadcast({"type": "message", "msg": msg})
        try:
            parts = await modem.send_sms(to, text)
            msg["status"] = "sent" if parts == 1 else f"sent ({parts} parts)"
        except Exception as e:
            msg["status"] = "failed"
            msg["error"] = str(e)
            raise
        finally:
            messages.save()
            await broadcast({"type": "message", "msg": msg})
        return {"id": msg["id"]}
    if op == "delete_message":
        msg = next((x for x in messages.data if x["id"] == d["id"]), None)
        if msg:
            if msg.get("sim_index") is not None and modem.status.connected:
                try:
                    await modem.delete_sms(msg["sim_index"])
                except Exception as e:
                    log.warning("SIM delete failed: %s", e)
            messages.data.remove(msg)
            messages.save()
        await broadcast({"type": "messages", "messages": messages.data})
        return {}
    if op == "delete_thread":
        for msg in [x for x in messages.data if x["number"] == d["number"]]:
            if msg.get("sim_index") is not None and modem.status.connected:
                try:
                    await modem.delete_sms(msg["sim_index"])
                except Exception:
                    pass
            messages.data.remove(msg)
        messages.save()
        await broadcast({"type": "messages", "messages": messages.data})
        return {}
    if op == "mark_read":
        for x in messages.data:
            if x["number"] == d["number"]:
                x["read"] = True
        messages.save()
        return {}
    if op == "refresh_inbox":
        await sync_inbox()
        return {}
    if op == "dial":
        await modem.dial(d["number"])
        return {}
    if op == "answer":
        await modem.answer()
        return {}
    if op == "hangup":
        await modem.hangup()
        return {}
    if op == "dtmf":
        await modem.dtmf(d["digit"])
        return {}
    if op == "volume":
        await modem.set_volume(d["level"])
        return {}
    if op == "audio_get":
        return await modem.audio_get()
    if op == "audio_set":
        return await modem.audio_set(d)
    if op == "clear_calls":
        calls.data.clear()
        calls.save()
        await broadcast({"type": "calls", "calls": []})
        return {}
    if op == "at":
        ok, lines, final = await modem.cmd(d["cmd"].strip(), float(d.get("timeout", 10)))
        return {"ok": ok, "lines": lines, "final": final}
    if op == "discover":
        return {"bridges": await m.discover_bridges(), "ports": m.list_serial_ports()}
    if op == "set_config":
        for k in ("mode", "host", "serial_port"):
            if k in d:
                config.data[k] = d[k]
        config.save()
        reconnect_evt.set()
        await broadcast({"type": "config", "config": config.data})
        return {}
    if op == "reconnect":
        reconnect_evt.set()
        return {}
    # ---------------- audio files on the module
    if op == "files_list":
        return await library_state()
    if op == "file_upload":
        name = d["name"]
        if not m.re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
            raise ValueError("file name: use letters, digits, _ . - only")
        data = base64.b64decode(d["data"])
        (AUDIO / name).write_bytes(data)                 # always keep it on the PC
        try:
            await ensure_on_module(name)
        except Exception as e:
            st = await library_state()
            st["warning"] = f"Saved on PC, but not copied to the module yet: {e}"
            return st
        return await library_state()
    if op == "file_push":
        await ensure_on_module(d["name"])
        return await library_state()
    if op == "file_delete":
        name = d["name"]
        if d.get("module", True) and modem.status.connected:
            try:
                await modem.delete_file(name)
            except Exception:
                pass
        if d.get("pc", True) and (AUDIO / name).exists():
            (AUDIO / name).unlink()
        return await library_state()
    if op == "file_download_test":                       # diagnostics: read a file back from the module
        files = {f["name"]: f["size"] for f in (await modem.list_files())["files"]}
        name = d["name"]
        data = await modem.download_file(name, files.get(name, 0))
        return {"name": name, "size": len(data), "head": data[:16].hex()}
    if op == "file_play":
        await modem.play_file(d["name"], remote=bool(d.get("remote")), repeat=int(d.get("repeat", 0)))
        return {}
    if op == "audio_stop":
        await modem.stop_audio()
        return {}
    if op == "tts_play":
        await modem.speak(d["text"], remote=bool(d.get("remote")))
        return {}
    # ---------------- campaigns
    if op == "campaigns_list":
        return {"campaigns": campaigns.data, "running": runner.running, "dtmf": list(dtmf_log)}
    if op == "campaign_start":
        if bulk_runner.running:
            raise RuntimeError("A bulk SMS job is running. Wait for it or stop it first.")
        if d.get("audio") and (d.get("mode") or "call") != "sms":
            await ensure_on_module(d["audio"])
            f = AUDIO / d["audio"]
            if f.exists():
                d["audio_size"] = f.stat().st_size
        return await runner.start(d)
    if op == "campaign_stop":
        await runner.stop()
        return {}
    if op == "campaign_delete":
        if runner.current and runner.current["id"] == d["id"]:
            raise RuntimeError("stop the campaign first")
        campaigns.data[:] = [c for c in campaigns.data if c["id"] != d["id"]]
        campaigns.save()
        import shutil
        shutil.rmtree(REC_DIR / re.sub(r"[^0-9a-f]", "", d["id"]), ignore_errors=True)   # its recordings
        return {"campaigns": campaigns.data}
    if op == "campaign_csv":
        c = next((c for c in campaigns.data if c["id"] == d["id"]), None)
        if not c:
            raise ValueError("campaign not found")
        return {"csv": camp_mod.to_csv(c), "name": c["name"]}
    # ---------------- email (SMTP)
    if op == "smtp_get":
        s = dict(config.data.get("smtp") or {})
        has = bool(s.pop("password", ""))
        return {"smtp": s, "has_password": has, "configured": mailer.configured(config.data.get("smtp")),
                "presets": mailer.PRESETS}
    if op == "smtp_save":
        cur = dict(config.data.get("smtp") or {})
        for k in ("host", "port", "user", "from_name", "default_to", "preset"):
            if k in d:
                cur[k] = d[k].strip() if isinstance(d[k], str) else d[k]
        if d.get("password"):                       # empty = keep the saved one
            cur["password"] = d["password"].replace(" ", "")
        cur["port"] = int(cur.get("port") or 587)
        config.data["smtp"] = cur
        config.save()
        return {"configured": mailer.configured(cur)}
    if op == "smtp_test":
        smtp = config.data.get("smtp") or {}
        to = d.get("to") or smtp.get("default_to") or smtp.get("user")
        html_body = mailer._shell("Test email", "<p>Your B2Wave email settings work. Campaign and bulk SMS reports will arrive like this.</p>")
        sent = await mailer.send(smtp, to, "B2Wave test email", html_body, "Your B2Wave email settings work.")
        return {"sent_to": sent}
    if op == "report_email":
        kind = d.get("kind", "campaign")
        store = campaigns if kind == "campaign" else bulk
        obj = next((x for x in store.data if x["id"] == d["id"]), None)
        if not obj:
            raise ValueError("not found")
        if d.get("to"):
            obj["email_to"] = d["to"]
        if not mailer.parse_recipients(obj.get("email_to")):
            raise ValueError("enter at least one email address")
        await email_report(kind, obj)
        store.save()
        if obj["email_status"].startswith("failed") or obj["email_status"].startswith("not sent"):
            raise RuntimeError(obj["email_status"])
        await broadcast({"type": "campaign" if kind == "campaign" else "bulk", ("campaign" if kind == "campaign" else "job"): obj})
        return {"status": obj["email_status"]}
    # ---------------- SIM number
    if op == "sim_set_number":
        if not modem.status.iccid:
            raise RuntimeError("No SIM detected yet. Insert a SIM and wait until it shows READY.")
        num = m.normalize_number(d.get("number", ""))
        if len(re.sub(r"\D", "", num)) < 8:
            raise ValueError("Enter the full mobile number, e.g. +91 98xxxxxxxx")
        config.data.setdefault("sim_numbers", {})[modem.status.iccid] = num
        config.save()
        modem.status.phone, modem.status.phone_src = num, "manual"
        await modem.emit_status()
        return {"phone": num}
    if op == "sim_refresh":
        await modem.read_sim_identity()
        await modem.emit_status()
        return asdict(modem.status)
    # ---------------- campaign drafts + SMS templates
    if op == "drafts_list":
        return {"drafts": drafts.data}
    if op == "draft_save":
        dr = dict(d.get("draft") or {})
        dr["id"] = dr.get("id") or uuid.uuid4().hex[:10]
        dr["updated"] = m.now_iso()
        drafts.data[:] = [x for x in drafts.data if x.get("id") != dr["id"]]
        drafts.data.insert(0, dr)
        drafts.data[:] = drafts.data[:100]
        drafts.save()
        return {"draft": dr, "drafts": drafts.data}
    if op == "draft_delete":
        drafts.data[:] = [x for x in drafts.data if x.get("id") != d["id"]]
        drafts.save()
        return {"drafts": drafts.data}
    if op == "templates_list":
        return {"templates": sms_templates.data}
    if op == "template_save":
        t = {"id": d.get("id") or uuid.uuid4().hex[:10], "name": (d.get("name") or "Untitled").strip()[:60],
             "text": (d.get("text") or "").strip()}
        if not t["text"]:
            raise ValueError("Write the message text")
        sms_templates.data[:] = [x for x in sms_templates.data if x.get("id") != t["id"]]
        sms_templates.data.insert(0, t)
        sms_templates.save()
        return {"templates": sms_templates.data}
    if op == "template_delete":
        sms_templates.data[:] = [x for x in sms_templates.data if x.get("id") != d["id"]]
        sms_templates.save()
        return {"templates": sms_templates.data}
    # ---------------- backup summary
    if op == "backup_info":
        sd = DATA / "backups"
        safety = sorted([f.name for f in sd.glob("before-restore-*.zip")], reverse=True)[:10] if sd.exists() else []
        return {"counts": bk.counts(_stores(), AUDIO), "safety": safety,
                "has_secrets": bool((config.data.get("smtp") or {}).get("password") or (config.data.get("ai") or {}).get("api_secret"))}
    # ---------------- AI voice agent (LiveKit)
    if op == "ai_get":
        a = dict(config.data.get("ai") or {})
        has_secret = bool(a.pop("api_secret", ""))
        env = AGENT_DIR / ".env"
        env_txt = env.read_text("utf-8") if env.exists() else ""
        return {"ai": a, "has_secret": has_secret, "configured": lk.configured(config.data.get("ai")),
                "openai_set": any(l.startswith("OPENAI_API_KEY=") and l.strip() != "OPENAI_API_KEY="
                                  for l in env_txt.splitlines()),
                "agent_env": env.exists(), "agent_venv": (AGENT_DIR / ".venv" / "Scripts" / "python.exe").exists(),
                "agent_dir": str(AGENT_DIR)}
    if op == "ai_save":
        a = dict(config.data.get("ai") or {})
        for k in ("url", "api_key", "agent_name", "instructions", "greeting", "voice", "language", "preset",
                  "engine", "stt", "llm", "tts", "tts_voice"):
            if k in d:
                a[k] = d[k].strip() if isinstance(d[k], str) else d[k]
        if d.get("api_secret"):
            a["api_secret"] = d["api_secret"].strip()
        a["agent_name"] = a.get("agent_name") or "b2wave-agent"
        config.data["ai"] = a
        config.save()
        # agent/.env for the LiveKit worker (secrets stay on this PC, git-ignored)
        env = AGENT_DIR / ".env"
        old = {}
        if env.exists():
            for line in env.read_text("utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    old[k.strip()] = v.strip()
        vals = {**old, "LIVEKIT_URL": a.get("url", ""), "LIVEKIT_API_KEY": a.get("api_key", ""),
                "B2W_AGENT_NAME": a["agent_name"]}
        if a.get("api_secret"):
            vals["LIVEKIT_API_SECRET"] = a["api_secret"]
        if d.get("openai_key"):
            vals["OPENAI_API_KEY"] = d["openai_key"].strip()
        AGENT_DIR.mkdir(exist_ok=True)
        env.write_text("# written by B2Wave - keep private\n" + "".join(f"{k}={v}\n" for k, v in vals.items()), "utf-8")
        return {"configured": lk.configured(a), "openai_set": bool(vals.get("OPENAI_API_KEY"))}
    if op == "ai_test":
        a = config.data.get("ai") or {}
        if not lk.configured(a):
            raise RuntimeError("Enter the LiveKit URL, API key and API secret first.")
        try:
            rooms = await lk.list_rooms(a)
        except aiohttp.ClientError as e:
            raise RuntimeError(f"Could not reach LiveKit at {a.get('url')}: {e}")
        return {"rooms": len(rooms)}
    if op == "ai_token":
        a = config.data.get("ai") or {}
        if not lk.configured(a):
            raise RuntimeError("LiveKit is not set up. Fill in the keys on the AI Agent page.")
        room = f"b2w-{d.get('mode', 'test')}-{uuid.uuid4().hex[:8]}"
        meta = {k: a.get(k, "") for k in ("instructions", "greeting", "voice", "language", "engine", "stt", "llm", "tts", "tts_voice")}
        meta.update(mode=d.get("mode", "test"), number=d.get("number", ""))
        return {"url": a["url"], "room": room, "identity": "b2wave-bridge",
                "token": lk.call_token(a, room, "b2wave-bridge", meta), "agent_name": a.get("agent_name") or "b2wave-agent"}
    if op == "agent_status":
        return agent_worker.info()
    if op == "agent_install":
        if agent_worker.state == "installing":
            raise RuntimeError("Already installing")
        asyncio.create_task(agent_worker.install())
        return {}
    if op == "agent_start":
        await agent_worker.start()
        return agent_worker.info()
    if op == "agent_stop":
        await agent_worker.stop()
        return agent_worker.info()
    if op == "agent_restart":
        await agent_worker.stop()
        await agent_worker.start()
        return agent_worker.info()
    if op == "ai_log_save":
        rec = {k: d.get(k) for k in ("room", "number", "mode", "started", "ended", "duration", "agent_joined",
                                     "transcript", "outcome", "error")}
        rec["id"] = uuid.uuid4().hex[:10]
        ai_calls.data.insert(0, rec)
        ai_calls.data[:] = ai_calls.data[:200]
        ai_calls.save()
        return {"calls": ai_calls.data}
    if op == "ai_calls":
        return {"calls": ai_calls.data}
    if op == "ai_call_delete":
        ai_calls.data[:] = [c for c in ai_calls.data if c["id"] != d["id"]]
        ai_calls.save()
        return {"calls": ai_calls.data}
    # ---------------- bulk SMS
    if op == "bulk_list":
        return {"jobs": bulk.data, "running": bulk_runner.running}
    if op == "bulk_start":
        return await bulk_runner.start(d)
    if op == "bulk_stop":
        await bulk_runner.stop()
        return {}
    if op == "bulk_delete":
        if bulk_runner.running and bulk.data and bulk.data[0]["id"] == d["id"] and bulk.data[0]["status"] == "running":
            raise RuntimeError("stop the job first")
        bulk.data[:] = [j for j in bulk.data if j["id"] != d["id"]]
        bulk.save()
        return {"jobs": bulk.data}
    if op == "bulk_csv":
        j = next((j for j in bulk.data if j["id"] == d["id"]), None)
        if not j:
            raise ValueError("job not found")
        return {"csv": bulksms.to_csv(j), "name": j["name"]}
    if op.startswith("wizard_"):
        return await handle_wizard(op[7:], d)
    raise ValueError(f"unknown op {op}")


async def wiz_emit(evt: dict):
    evt["type"] = "wizard"
    await broadcast(evt)


async def handle_wizard(op: str, d: dict):
    port = d.get("port", "")
    if op == "info":
        return {"firmware": flasher.firmware_info()}
    if op == "detect":
        return await flasher.scan_usb(wiz_emit)
    if op == "flash":
        ok = await flasher.flash(port, wiz_emit)
        if not ok:
            raise RuntimeError("Could not flash. Hold BOOT, tap RESET, release BOOT, then try again.")
        fw = await flasher.wait_for_firmware(port)
        if not fw:
            raise RuntimeError("Flashed, but the board did not start the new firmware. Press RESET.")
        return {"firmware": fw}
    if op == "ping":
        return {"firmware": await flasher.ping(port)}
    if op == "status":
        r = await flasher.command(port, "STATUS", "STATUS", 4)
        return json.loads(r) if r else {}
    if op == "wifi_reach":                      # can this PC reach the board over Wi-Fi?
        host = d.get("host", "")
        try:
            r = json.loads(await flasher.http_get(host, "/api/status", 5))
            return {"ok": True, "status": r}
        except Exception as e:
            return {"ok": False, "error": str(e) or e.__class__.__name__}
    if op == "usb_modem_test":                  # try the given port, then every SIMCom port
        ports = [port] + [p["device"] for p in flasher.list_simcom_ports() if p["device"] != port]
        last = {"ok": False, "error": "no SIMCom ports"}
        for p in ports:
            try:
                r = await flasher.usb_modem_test(p)
            except Exception as e:
                r = {"ok": False, "error": f"{p}: {e}"}
            if r.get("ok"):
                r["port"] = p
                return r
            last = r
        return last
    if op == "diag":
        host = d.get("host") or config.data.get("host") or config.data.get("last_host", "")
        return {"report": await flasher.diagnostics(port, "" if port else host)}
    if op == "reset4g":
        await flasher.command(port, "RESET4G", "RESET4G", 6)
        return {}
    if op == "modem_test":
        return await flasher.modem_test(port)
    if op == "wifi_scan":
        return {"networks": await flasher.wifi_scan(port)}
    if op == "wifi_join":
        r = await flasher.wifi_join(port, d["ssid"], d.get("password", ""))
        if r["ok"]:
            config.data.update(mode="wifi", host=r["ip"], last_host=r["ip"], setup_done=True)
            config.save()
            reconnect_evt.set()
        return r
    if op == "simcom_ports":
        ports = flasher.list_simcom_ports()
        missing = False if ports else await asyncio.to_thread(flasher._simcom_driver_missing)
        return {"ports": ports, "driver_missing": missing}
    if op == "use_serial":
        config.data.update(mode="serial", serial_port=port, setup_done=True)
        config.save()
        reconnect_evt.set()
        return {}
    if op == "skip":
        config.data["setup_done"] = True
        config.save()
        return {}
    raise ValueError(f"unknown op {op}")


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    clients.add(ws)
    await ws.send_json({"type": "snapshot", "status": asdict(modem.status), "call": asdict(modem.call),
                        "messages": messages.data, "calls": calls.data, "config": config.data,
                        "console": list(console)})
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            d = json.loads(msg.data)

            async def run(d=d):
                try:
                    res = await handle_op(d.get("op", ""), d)
                    await ws.send_json({"type": "reply", "rid": d.get("rid"), "ok": True, "data": res})
                except Exception as e:
                    await ws.send_json({"type": "reply", "rid": d.get("rid"), "ok": False, "error": str(e)})

            asyncio.create_task(run())
    finally:
        clients.discard(ws)
    return ws


async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


async def on_startup(app):
    app["supervisor"] = asyncio.create_task(supervisor())


async def on_cleanup(app):
    try:
        await agent_worker.stop()
    except Exception:
        pass
    app["supervisor"].cancel()
    await modem.disconnect()


def _local(request) -> bool:
    return request.remote in ("127.0.0.1", "::1")


def _stores() -> dict:
    return {"messages": messages, "calls": calls, "campaigns": campaigns, "bulk": bulk, "ai_calls": ai_calls,
            "drafts": drafts, "sms_templates": sms_templates}


async def backup_handler(request):
    if not _local(request):
        raise web.HTTPForbidden(text="Backups can only be downloaded on the PC running B2Wave.")
    secrets = request.query.get("secrets") == "1"
    blob = await asyncio.to_thread(bk.build, _stores(), config.data, AUDIO, AGENT_DIR / ".env", secrets)
    name = time.strftime("B2Wave-backup-%Y%m%d-%H%M.zip")
    return web.Response(body=blob, content_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


async def restore_handler(request):
    if not _local(request):
        raise web.HTTPForbidden(text="Restore is only allowed on the PC running B2Wave.")
    blob = await request.read()
    try:
        if request.query.get("dry") == "1":
            return web.json_response({"ok": True, **bk.inspect(blob)})
        if runner.running or bulk_runner.running:
            raise ValueError("Stop the running campaign / bulk SMS job before restoring.")
        if modem.call.state != "idle":
            raise ValueError("Finish the current call before restoring.")
        mode = "replace" if request.query.get("mode") == "replace" else "merge"
        # safety copy of the current data first
        safe_dir = DATA / "backups"
        safe_dir.mkdir(exist_ok=True)
        safe = safe_dir / time.strftime("before-restore-%Y%m%d-%H%M%S.zip")
        safe.write_bytes(await asyncio.to_thread(bk.build, _stores(), config.data, AUDIO, AGENT_DIR / ".env", True))
        for old in sorted(safe_dir.glob("before-restore-*.zip"))[:-10]:
            old.unlink()                                       # keep the last 10 safety copies
        res = await asyncio.to_thread(bk.restore, blob, mode, _stores(), config, AUDIO, AGENT_DIR / ".env")
        res["safety_copy"] = safe.name
        log.info("restore (%s) done: %s", mode, res["restored"])
        await broadcast({"type": "restored", "result": res})
        reconnect_evt.set()                                    # re-apply connection settings
        return web.json_response({"ok": True, **res})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception as e:
        log.exception("restore failed")
        return web.json_response({"ok": False, "error": f"Restore failed: {e}"}, status=500)


async def shutdown_handler(request):
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden()
    log.info("shutdown requested by a newer instance")
    try:
        await agent_worker.stop()                 # don't leave the AI worker running
    except Exception:
        pass
    asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
    return web.Response(text="bye")


def replace_running_instance(port: int):
    """If B2Wave is already running on this port, ask it to exit so we can take over."""
    import socket
    import time
    import urllib.request
    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return
    print("  Another B2Wave is running - replacing it...")
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", method="POST"), timeout=3)
    except Exception:
        pass
    for _ in range(20):
        time.sleep(0.25)
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                time.sleep(0.5)          # let it release the serial port
                return
    print(f"  Port {port} is still busy. Close the other program or use --port 8081")


def main():
    ap = argparse.ArgumentParser(description="GSM Bridge browser app")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--lan", action="store_true", help="listen on all interfaces (phones on your Wi-Fi)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    fh = RotatingFileHandler(DATA / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    logging.getLogger().setLevel(logging.DEBUG)
    for noisy in ("aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for h in logging.getLogger().handlers:
        if h is not fh:
            h.setLevel(logging.INFO)

    app = web.Application(client_max_size=bk.MAX_ZIP)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", STATIC)
    app.router.add_static("/doc", ROOT.parent / "doc")
    app.router.add_static("/audio", AUDIO)
    app.router.add_static("/recordings", REC_DIR)
    app.router.add_post("/api/shutdown", shutdown_handler)
    app.router.add_get("/api/backup", backup_handler)
    app.router.add_get("/favicon.ico", lambda r: web.FileResponse(STATIC / "icons" / "favicon.ico"))
    app.router.add_post("/api/restore", restore_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    replace_running_instance(a.port)
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    url = f"http://127.0.0.1:{a.port}"
    print(f"\n  B2Wave running ->  {url}\n  (Ctrl+C to stop)\n")
    if not a.no_browser:
        import threading
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    web.run_app(app, host=host, port=a.port, print=None)


if __name__ == "__main__":
    main()
