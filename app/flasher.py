"""
Setup-wizard backend: find the board on USB, flash the prebuilt firmware with
esptool (no Arduino IDE needed), run board tests and push Wi-Fi settings.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

import serial
import serial.tools.list_ports

ROOT = Path(__file__).resolve().parent.parent
FIRMWARE = ROOT / "firmware" / "bin" / "gsm_bridge.bin"
FIRMWARE_INFO = ROOT / "firmware" / "bin" / "version.json"
SIMCOM_VID = 0x1E0E
USB_UART_VIDS = {0x1A86: "CH340/CH343", 0x10C4: "CP210x", 0x0403: "FTDI", 0x303A: "Espressif USB"}

Emit = Callable[[dict], Awaitable[None]]


def firmware_info() -> dict:
    info = {"available": FIRMWARE.exists(), "version": "?", "size": 0}
    if FIRMWARE.exists():
        info["size"] = FIRMWARE.stat().st_size
        try:
            info.update(json.loads(FIRMWARE_INFO.read_text()))
        except Exception:
            pass
    return info


# ---------------------------------------------------------------- serial helpers
def _open(port: str, timeout: float = 0.2) -> serial.Serial:
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, 115200, timeout
    s.dtr = False          # keep EN / IO0 released -> board keeps running
    s.rts = False
    s.open()
    return s


def _command(port: str, cmd: str, expect: str, timeout: float) -> Optional[str]:
    """Send one line to the bridge firmware, return the reply line starting with `expect`."""
    with _open(port) as s:
        s.reset_input_buffer()
        s.write((cmd + "\n").encode())
        buf, t0 = b"", time.time()
        while time.time() - t0 < timeout:
            buf += s.read(512)
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode(errors="replace").strip()
                if line.startswith(expect):
                    return line[len(expect):].strip()
    return None


async def command(port: str, cmd: str, expect: str, timeout: float = 4) -> Optional[str]:
    return await asyncio.to_thread(_command, port, cmd, expect, timeout)


def _collect(port: str, cmd: str, timeout: float) -> list[str]:
    """Multi-line reply: every line starts with `cmd `, ends with `cmd END`."""
    out = []
    with _open(port) as s:
        s.reset_input_buffer()
        s.write((cmd + "\n").encode())
        buf, t0 = b"", time.time()
        while time.time() - t0 < timeout:
            buf += s.read(1024)
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode(errors="replace").rstrip("\r")
                if line == f"{cmd} END":
                    return out
                if line.startswith(cmd + " "):
                    out.append(line[len(cmd) + 1:])
    out.append(f"(timeout after {timeout}s)")
    return out


async def collect(port: str, cmd: str, timeout: float = 30) -> list[str]:
    return await asyncio.to_thread(_collect, port, cmd, timeout)


# ---------------------------------------------------------------- Wi-Fi debug (HTTP on the board)
async def http_get(host: str, path: str, timeout: float = 30) -> str:
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.get(f"http://{host}{path}") as r:
            return await r.text()


async def diagnostics(port: str = "", host: str = "") -> str:
    """Full board report via USB (preferred) or Wi-Fi. Also saved under logs/."""
    hdr = [f"GSM Bridge diagnostics  {time.strftime('%Y-%m-%d %H:%M:%S')}",
           f"via: {'USB ' + port if port else 'Wi-Fi ' + host}", ""]
    try:
        if port:
            st = await command(port, "STATUS", "STATUS", 4)
            body = [f"status: {st}"] + await collect(port, "DIAG", 40)
        elif host:
            body = [f"status: {await http_get(host, '/api/status', 8)}",
                    await http_get(host, "/api/diag", 40)]
        else:
            body = ["no USB port or Wi-Fi host known"]
    except Exception as e:
        body = [f"ERROR: {e!r}"]
    report = "\n".join(hdr + body)
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    (logs / f"diag-{time.strftime('%Y%m%d-%H%M%S')}.txt").write_text(report, "utf-8")
    (logs / "diag-latest.txt").write_text(report, "utf-8")
    return report


async def ping(port: str, tries: int = 2) -> Optional[str]:
    for _ in range(tries):
        try:
            r = await command(port, "PING", "PONG", 1.5)
            if r:
                return r.split()[-1]
        except Exception:
            return None
    return None


# ---------------------------------------------------------------- esptool
async def _esptool(args: list[str], on_line: Callable[[str], Awaitable[None]] | None = None,
                   timeout: float = 240) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "esptool", *args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out, buf = [], b""

    async def pump():
        nonlocal buf
        while True:
            chunk = await proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for p in parts:
                line = p.decode(errors="replace").strip()
                if line:
                    out.append(line)
                    if on_line:
                        await on_line(line)

    try:
        await asyncio.wait_for(pump(), timeout)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "\n".join(out + ["timeout"])
    return proc.returncode, "\n".join(out)


async def chip_id(port: str) -> Optional[str]:
    code, out = await _esptool(["--port", port, "--baud", "115200", "chip-id"], timeout=25)
    m = re.search(r"(?:Chip is|Chip type:|Detecting chip type\.*)\s*(ESP32[-\w]*)", out)
    return m.group(1) if m else None


async def flash(port: str, emit: Emit) -> bool:
    if not FIRMWARE.exists():
        await emit({"stage": "flash", "level": "err", "msg": f"Firmware file missing: {FIRMWARE}"})
        return False

    async def on_line(line: str):
        m = re.search(r"\(\s*(\d+)\s*%\)|(\d+(?:\.\d+)?)\s*%", line)
        evt = {"stage": "flash", "msg": line}
        if m and "Writing" in line:
            evt["pct"] = min(99.0, float(m.group(1) or m.group(2)))
        await emit(evt)

    for baud in ("921600", "460800", "115200"):
        await emit({"stage": "flash", "msg": f"Flashing at {baud} baud...", "pct": 0})
        code, out = await _esptool(["--chip", "esp32s3", "--port", port, "--baud", baud,
                                    "--before", "default-reset", "--after", "hard-reset",
                                    "write-flash", "-z", "0x0", str(FIRMWARE)], on_line)
        if code == 0:
            await emit({"stage": "flash", "msg": "Flash complete, board restarting", "pct": 100})
            return True
        if "Failed to connect" in out or "No serial data" in out or "could not open port" in out.lower():
            break  # baud will not help
    return False


# ---------------------------------------------------------------- discovery
def _simcom_driver_missing() -> bool:
    """SIMCom device plugged in but Windows has no COM port for it."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PnpDevice -PresentOnly | ? { $_.InstanceId -match 'VID_1E0E' -and $_.Status -ne 'OK' } | Measure | % Count"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        return out not in ("", "0")
    except Exception:
        return False


UART_DRIVERS = {
    "10C4": ("CP210x", "https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers"),
    "1A86": ("CH340/CH343", "https://www.wch-ic.com/downloads/CH343SER_EXE.html"),
    "0403": ("FTDI", "https://ftdichip.com/drivers/vcp-drivers/"),
}


def _uart_driver_missing() -> Optional[dict]:
    """USB-to-UART chip plugged in but Windows has no driver for it."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PnpDevice -PresentOnly | ? { $_.Status -ne 'OK' -and $_.InstanceId -match 'VID_(10C4|1A86|0403)' } "
             "| Select -First 1 | % { $_.FriendlyName + '|' + $_.InstanceId }"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    except Exception:
        return None
    if not out:
        return None
    name, _, inst = out.partition("|")
    vid = re.search(r"VID_([0-9A-F]{4})", inst.upper())
    chip, url = UART_DRIVERS.get(vid.group(1) if vid else "", ("USB-UART", ""))
    return {"name": name, "chip": chip, "url": url}


async def scan_usb(emit: Emit) -> dict:
    """Look for the board's ESP_UART port (and a 4G_USB port if the cable is there)."""
    ports = list(serial.tools.list_ports.comports())
    result = {"esp": None, "simcom": [], "others": [], "driver_missing": False}
    for p in ports:
        if p.vid == SIMCOM_VID:
            result["simcom"].append({"device": p.device, "description": p.description})
    candidates = [p for p in ports if p.vid is not None and p.vid != SIMCOM_VID]
    result["others"] = [{"device": p.device, "description": p.description} for p in ports
                        if p.vid != SIMCOM_VID]
    candidates.sort(key=lambda p: p.vid not in USB_UART_VIDS)
    for p in candidates:
        await emit({"stage": "detect", "msg": f"Checking {p.device} ({p.description})..."})
        fw = await ping(p.device)
        if fw:
            result["esp"] = {"device": p.device, "description": p.description, "chip": "ESP32-S3",
                             "firmware": fw}
            break
        chip = await chip_id(p.device)
        if chip:
            result["esp"] = {"device": p.device, "description": p.description, "chip": chip,
                             "firmware": None}
            break
    if not result["simcom"]:
        result["driver_missing"] = await asyncio.to_thread(_simcom_driver_missing)
    result["uart_driver_missing"] = None if result["esp"] else await asyncio.to_thread(_uart_driver_missing)
    return result


async def wait_for_firmware(port: str, timeout: float = 25) -> Optional[str]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            fw = await ping(port, 1)
            if fw:
                return fw
        except Exception:
            pass
        await asyncio.sleep(1)
    return None


async def modem_test(port: str) -> dict:
    r = await command(port, "MODEM", "MODEM", 15)
    if not r:
        return {"ok": False, "error": "board did not answer"}
    try:
        return json.loads(r)
    except Exception:
        return {"ok": False, "error": r}


async def wifi_scan(port: str) -> list:
    r = await command(port, "SCAN", "SCAN", 15)
    try:
        nets = json.loads(r or "[]")
    except Exception:
        return []
    best: dict[str, dict] = {}
    for n in nets:
        if n["ssid"] and (n["ssid"] not in best or n["rssi"] > best[n["ssid"]]["rssi"]):
            best[n["ssid"]] = n
    return sorted(best.values(), key=lambda n: -n["rssi"])


async def wifi_join(port: str, ssid: str, password: str) -> dict:
    if "\t" in ssid or "\n" in ssid + password:
        return {"ok": False, "error": "invalid characters"}
    r = await command(port, f"WIFI {ssid}\t{password}", "WIFI", 30)
    if r and r.startswith("OK"):
        return {"ok": True, "ip": r[2:].strip()}
    return {"ok": False, "error": (r or "no reply")[5:] if r and r.startswith("FAIL") else (r or "no reply")}


def _at_session(port: str, cmds: list[tuple[str, float]]) -> dict:
    """Run AT commands directly on a 4G-module COM port (4G_USB cable)."""
    res = {}
    s = serial.Serial(port, 115200, timeout=0.1)
    try:
        for cmd, wait in cmds:
            s.reset_input_buffer()
            s.write((cmd + "\r").encode())
            buf, t0 = b"", time.time()
            while time.time() - t0 < wait:
                buf += s.read(512)
                if re.search(rb"\r\n(OK|ERROR|\+CME ERROR[^\r]*|\+CMS ERROR[^\r]*)\r\n", buf):
                    break
            txt = buf.decode(errors="replace").replace("\r", "")
            lines = [l.strip() for l in txt.split("\n") if l.strip() and l.strip() != cmd]
            res[cmd] = " | ".join(lines)
    finally:
        s.close()
    return res


async def usb_modem_test(port: str) -> dict:
    cmds = [("ATE0", 1), ("AT", 1), ("AT+CGMR", 2), ("AT+CPIN?", 3), ("AT+CSQ", 2),
            ("AT+CEREG?", 2), ("AT+CREG?", 2), ("AT+COPS?", 5), ("AT+CGMM", 2)]
    r = await asyncio.to_thread(_at_session, port, cmds)
    return {"ok": "OK" in r.get("AT", ""), "model": r.get("AT+CGMM", "").split(" | ")[0],
            "fwver": r.get("AT+CGMR", ""), "sim": r.get("AT+CPIN?", ""), "csq": r.get("AT+CSQ", ""),
            "reg": r.get("AT+CEREG?", ""), "creg": r.get("AT+CREG?", ""), "op": r.get("AT+COPS?", ""),
            "raw": r}


def list_simcom_ports() -> list[dict]:
    out = []
    for p in serial.tools.list_ports.comports():
        if p.vid == SIMCOM_VID:
            out.append({"device": p.device, "description": p.description,
                        "at": "AT" in p.description.upper()})
    out.sort(key=lambda p: not p["at"])
    return out
