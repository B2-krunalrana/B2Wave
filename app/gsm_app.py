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
import os
import asyncio
import json
import logging
import uuid
import webbrowser
from collections import deque
from dataclasses import asdict
from pathlib import Path

from aiohttp import web, WSMsgType

import modem as m
import flasher
import campaign as camp_mod
import base64

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("B2W_DATA") or ROOT / "data")
STATIC = ROOT / "static"
DATA.mkdir(exist_ok=True)

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


async def on_modem_event(evt: dict):
    t = evt["type"]
    if t == "log":
        console.append(evt)
        logging.getLogger("gsm.modem").debug("%s %s", evt["dir"], evt["line"])
    elif t == "dtmf":
        runner.on_dtmf(evt["key"])
        dtmf_log.append(evt)
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


def repair_stored_messages():
    """Older versions stored some Unicode SMS as raw UCS2 hex - decode them once."""
    changed = 0
    for x in messages.data:
        if x.get("dir") == "in" and m.looks_ucs2(x.get("text", "")):
            x["text"] = m.ucs2_decode(x["text"])
            changed += 1
    if changed:
        messages.save()
        log.info("repaired %d stored Unicode messages", changed)


repair_stored_messages()


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


async def pick_transport():
    c = config.data
    if c["mode"] == "wifi" and c["host"]:
        return m.TcpTransport(c["host"])
    if c["mode"] == "serial" and c["serial_port"]:
        return m.SerialTransport(c["serial_port"], c.get("baud", 115200))
    # auto: last bridge -> discovery -> SIMCom USB port
    if c.get("last_host"):
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(c["last_host"], m.BRIDGE_PORT), 2)
            w.close()
            return m.TcpTransport(c["last_host"])
        except Exception:
            pass
    found = await m.discover_bridges()
    if found:
        host = found[0]["ip"]
        c["last_host"] = host
        config.save()
        return m.TcpTransport(host)
    for p in m.list_serial_ports():
        if p["simcom"] and await probe_serial(p["device"]):
            return m.SerialTransport(p["device"])
    return None


async def wait_reconnect(timeout: float) -> bool:
    try:
        await asyncio.wait_for(reconnect_evt.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        reconnect_evt.clear()


async def supervisor():
    while True:
        reader = None
        try:
            modem.status.error = "Searching for the board..."
            await modem.emit_status()
            transport = await pick_transport()
            if transport is None:
                modem.status.error = ("Board not found. Check it is powered and on the same Wi-Fi, "
                                      "or set the IP / COM port in Settings.")
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
            modem.status.error = str(e) or e.__class__.__name__
            log.debug("supervisor error", exc_info=True)
        finally:
            if reader and not reader.done():
                reader.cancel()
            await modem.disconnect()
            await modem.emit_status()
        await wait_reconnect(3)


# ======================================================================= websocket API
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
        return await modem.list_files()
    if op == "file_upload":
        data = base64.b64decode(d["data"])
        info = await modem.list_files()
        if len(data) > info["free"] - 8192:
            raise RuntimeError(f"not enough space on the module: file {len(data)//1024} KB, free {info['free']//1024} KB")
        await modem.upload_file(d["name"], data)
        return await modem.list_files()
    if op == "file_delete":
        await modem.delete_file(d["name"])
        return await modem.list_files()
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
        return await runner.start(d)
    if op == "campaign_stop":
        await runner.stop()
        return {}
    if op == "campaign_delete":
        if runner.current and runner.current["id"] == d["id"]:
            raise RuntimeError("stop the campaign first")
        campaigns.data[:] = [c for c in campaigns.data if c["id"] != d["id"]]
        campaigns.save()
        return {"campaigns": campaigns.data}
    if op == "campaign_csv":
        c = next((c for c in campaigns.data if c["id"] == d["id"]), None)
        if not c:
            raise ValueError("campaign not found")
        return {"csv": camp_mod.to_csv(c), "name": c["name"]}
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
    app["supervisor"].cancel()
    await modem.disconnect()


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

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", STATIC)
    app.router.add_static("/doc", ROOT.parent / "doc")
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    host = "0.0.0.0" if a.lan else "127.0.0.1"
    url = f"http://127.0.0.1:{a.port}"
    print(f"\n  B2Wave running ->  {url}\n  (Ctrl+C to stop)\n")
    if not a.no_browser:
        import threading
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    web.run_app(app, host=host, port=a.port, print=None)


if __name__ == "__main__":
    main()
