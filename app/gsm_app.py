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

from aiohttp import web, WSMsgType

import modem as m
import flasher
import campaign as camp_mod
import bulksms
import mailer
import base64

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("B2W_DATA") or ROOT / "data")
STATIC = ROOT / "static"
DATA.mkdir(exist_ok=True)
AUDIO = DATA / "audio"          # PC-side prompt library (source of truth)
AUDIO.mkdir(exist_ok=True)

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


async def _bulk_done(job):
    await email_report("bulk", job)

bulk_runner = bulksms.BulkSmsRunner(modem, broadcast, bulk, record_sent, _bulk_done)


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
        if d.get("audio"):
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
    app["supervisor"].cancel()
    await modem.disconnect()


async def shutdown_handler(request):
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden()
    log.info("shutdown requested by a newer instance")
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

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", STATIC)
    app.router.add_static("/doc", ROOT.parent / "doc")
    app.router.add_static("/audio", AUDIO)
    app.router.add_post("/api/shutdown", shutdown_handler)
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
