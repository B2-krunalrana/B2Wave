"""
Board debug tool - talk to the GSM Bridge board directly (USB or Wi-Fi).

  python tools/board_debug.py diag                 full hardware report (saved to logs/)
  python tools/board_debug.py log                  board's internal log
  python tools/board_debug.py status
  python tools/board_debug.py at "AT+CSQ"          raw AT command to the 4G module
  python tools/board_debug.py reset4g              hard-reset the 4G module
  python tools/board_debug.py setrst 0|1           force 4G_RESET idle level
  python tools/board_debug.py flash                flash firmware/bin/gsm_bridge.bin
  python tools/board_debug.py sms +91XXXXXXXXXX "hello"   send via the running app
  python tools/board_debug.py applog               tail of app/data/app.log

Options: --port COM4 (USB)  or  --host 172.20.10.2 (Wi-Fi). Default: auto.
Run with the app's venv:  app\\.venv\\Scripts\\python tools\\board_debug.py diag
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import flasher  # noqa: E402
import modem as m  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


async def find_port() -> str:
    for p in flasher.serial.tools.list_ports.comports():
        if p.vid and p.vid != flasher.SIMCOM_VID:
            try:
                if await flasher.ping(p.device, 1):
                    return p.device
            except Exception:
                pass
    return ""


async def find_host() -> str:
    try:
        cfg = json.loads((ROOT / "app" / "data" / "config.json").read_text())
        if cfg.get("host") or cfg.get("last_host"):
            return cfg.get("host") or cfg["last_host"]
    except Exception:
        pass
    found = await m.discover_bridges()
    return found[0]["ip"] if found else ""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--port", default="")
    ap.add_argument("--host", default="")
    a = ap.parse_args()

    if a.cmd == "applog":
        f = ROOT / "app" / "data" / "app.log"
        print("".join(f.read_text("utf-8", errors="replace").splitlines(True)[-200:]) if f.exists() else "no app.log")
        return
    if a.cmd == "sms":
        import aiohttp
        async with aiohttp.ClientSession() as s, s.ws_connect("http://127.0.0.1:8080/ws") as ws:
            await ws.receive_json()
            await ws.send_json({"op": "send_sms", "rid": 1, "to": a.args[0], "text": " ".join(a.args[1:])})
            while True:
                e = await ws.receive_json()
                if e.get("type") == "reply":
                    print("OK" if e["ok"] else f"FAILED: {e['error']}")
                    return

    port, host = a.port, a.host
    if not port and not host:
        port = await find_port()
        if not port:
            host = await find_host()
    print(f"# via {'USB ' + port if port else 'Wi-Fi ' + host if host else 'NOTHING FOUND'}")
    if not port and not host:
        return

    if a.cmd == "diag":
        print(await flasher.diagnostics(port, host))
    elif a.cmd == "log":
        print("\n".join(await flasher.collect(port, "LOG", 10)) if port else await flasher.http_get(host, "/api/log"))
    elif a.cmd == "status":
        print(await flasher.command(port, "STATUS", "STATUS") if port else await flasher.http_get(host, "/api/status"))
    elif a.cmd == "at":
        c = a.args[0]
        if port:
            print(await flasher.command(port, c, "ATR", 6))
        else:
            from urllib.parse import quote
            print(await flasher.http_get(host, f"/api/at?c={quote(c)}"))
    elif a.cmd == "reset4g":
        print(await flasher.command(port, "RESET4G", "RESET4G", 6) if port else await flasher.http_get(host, "/api/reset4g"))
    elif a.cmd == "setrst":
        v = a.args[0]
        print(await flasher.command(port, f"SETRST {v}", "SETRST") if port else await flasher.http_get(host, f"/api/setrst?v={v}"))
    elif a.cmd == "flash":
        if not port:
            print("flash needs USB (--port)")
            return

        async def emit(e):
            if "pct" not in e or e.get("pct") in (0, 100):
                print(e["msg"])
        ok = await flasher.flash(port, emit)
        print("flashed" if ok else "FLASH FAILED")
        if ok:
            print("firmware:", await flasher.wait_for_firmware(port))
    else:
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
