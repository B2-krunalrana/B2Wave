"""
B2Wave REST API v1 (for other systems, e.g. through an SSH tunnel).

Auth:  Authorization: Bearer <key>   (or  X-API-Key: <key>)
Keys are stored as SHA-256 hashes in data/config.json -> "api_keys" and shown only once.

  GET  /api/v1/status
  POST /api/v1/sms                 {to, text | template_id, vars}
  POST /api/v1/calls               broadcast call (optionally with SMS rules / recording)
  POST /api/v1/campaigns           {draft_id, numbers | rows, name}   launch a saved draft
  GET  /api/v1/campaigns           list
  GET  /api/v1/campaigns/{id}      status + per-number results
  POST /api/v1/campaigns/{id}/stop
  GET  /api/v1/templates | /drafts | /audio | /messages?since=&limit=

Webhooks (optional): POST JSON events to a URL, header X-B2Wave-Signature: sha256=<hmac>.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import deque

import aiohttp
from aiohttp import web

import modem as m
from bulksms import render as render_template

RATE_LIMIT = 120          # requests per minute per key


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class ApiV1:
    def __init__(self, deps: dict):
        self.d = deps                        # config, modem, runner, drafts, sms_templates, messages, campaigns, ...
        self.log = deque(maxlen=300)
        self._hits: dict[str, deque] = {}

    # ------------------------------------------------------------ keys
    @property
    def cfg(self):
        return self.d["config"].data

    def keys(self) -> list:
        return self.cfg.setdefault("api_keys", [])

    def create_key(self, name: str) -> dict:
        raw = "b2w_" + secrets.token_urlsafe(30)
        k = {"id": secrets.token_hex(4), "name": (name or "API key").strip()[:40], "hash": _hash(raw),
             "prefix": raw[:10], "created": m.now_iso(), "last_used": "", "uses": 0}
        self.keys().append(k)
        self.d["config"].save()
        return {**{x: k[x] for x in ("id", "name", "prefix", "created")}, "key": raw}

    def revoke_key(self, kid: str):
        self.cfg["api_keys"] = [k for k in self.keys() if k["id"] != kid]
        self.d["config"].save()

    def public_keys(self) -> list:
        return [{x: k.get(x) for x in ("id", "name", "prefix", "created", "last_used", "uses")} for k in self.keys()]

    def _auth(self, request):
        h = request.headers.get("Authorization", "")
        raw = h[7:].strip() if h.lower().startswith("bearer ") else request.headers.get("X-API-Key", "").strip()
        if not raw:
            return None
        digest = _hash(raw)
        for k in self.keys():
            if hmac.compare_digest(k["hash"], digest):
                return k
        return None

    # ------------------------------------------------------------ middleware
    @web.middleware
    async def middleware(self, request, handler):
        if not request.path.startswith("/api/v1/"):
            return await handler(request)
        t0 = time.time()
        key = self._auth(request)
        status = 500
        try:
            if not key:
                status = 401
                return self._err("missing or invalid API key (Authorization: Bearer <key>)", 401)
            hits = self._hits.setdefault(key["id"], deque())
            now = time.time()
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= RATE_LIMIT:
                status = 429
                return self._err(f"rate limit: {RATE_LIMIT} requests per minute", 429)
            hits.append(now)
            key["last_used"], key["uses"] = m.now_iso(), key.get("uses", 0) + 1
            request["api_key"] = key
            resp = await handler(request)
            status = resp.status
            return resp
        except web.HTTPException as e:
            status = e.status
            raise
        except ApiError as e:
            status = e.status
            return self._err(str(e), e.status)
        except Exception as e:
            status = 500
            return self._err(f"internal error: {e}", 500)
        finally:
            self.log.appendleft({"time": m.now_iso(), "key": (key or {}).get("name", "-"), "method": request.method,
                                 "path": request.path, "status": status, "ms": int((time.time() - t0) * 1000),
                                 "ip": request.remote})
            if key and key.get("uses", 0) % 10 == 1:
                self.d["config"].save()

    @staticmethod
    def _err(msg, status):
        return web.json_response({"ok": False, "error": msg}, status=status)

    @staticmethod
    def _ok(**data):
        return web.json_response({"ok": True, **data})

    # ------------------------------------------------------------ helpers
    async def _body(self, request) -> dict:
        try:
            return await request.json()
        except Exception:
            raise ApiError("body must be JSON", 400)

    def _template_text(self, tid: str) -> str:
        t = next((t for t in self.d["sms_templates"].data if t["id"] == tid), None)
        if not t:
            raise ApiError(f"template_id '{tid}' not found (GET /api/v1/templates)", 404)
        return t["text"]

    def _rows(self, b: dict) -> list:
        rows = b.get("rows") or [{"number": n} for n in (b.get("numbers") or ([b["to"]] if isinstance(b.get("to"), str) else b.get("to") or []))]
        rows = [r if isinstance(r, dict) else {"number": str(r)} for r in rows]
        if not rows:
            raise ApiError("give 'numbers' (list), 'rows' (list of objects with 'number') or 'to'", 400)
        return rows

    def _need_board(self):
        if not self.d["modem"].status.connected:
            raise ApiError("the 4G board is offline", 503)

    def _need_idle(self):
        if self.d["runner"].running or self.d["bulk_runner"].running:
            raise ApiError("a campaign is already running; try again later or stop it", 409)

    def _sms_rules(self, rules) -> list:
        out = []
        for r in rules or []:
            text = r.get("text") or (self._template_text(r["template_id"]) if r.get("template_id") else "")
            if text.strip():
                out.append({"when": r.get("when", "always"), "text": text})
        return out

    async def _launch(self, cfg: dict) -> dict:
        self._need_board()
        self._need_idle()
        if cfg.get("audio") and cfg.get("mode") != "sms":
            await self.d["ensure_on_module"](cfg["audio"])
            f = self.d["audio_dir"] / cfg["audio"]
            if f.exists():
                cfg["audio_size"] = f.stat().st_size
        try:
            camp = await self.d["runner"].start(cfg)
        except (ValueError, RuntimeError) as e:
            raise ApiError(str(e), 400)
        return {"campaign_id": camp["id"], "status": camp["status"], "recipients": len(camp["results"]), "mode": camp["mode"]}

    # ------------------------------------------------------------ endpoints
    async def status(self, request):
        st = self.d["modem"].status
        return self._ok(board={"connected": st.connected, "link": st.transport, "sim": st.sim, "number": st.phone,
                               "operator": st.operator, "network": st.network, "signal": st.signal},
                        busy={"call": self.d["modem"].call.state, "campaign_running": self.d["runner"].running})

    async def sms(self, request):
        b = await self._body(request)
        text = b.get("text") or (self._template_text(b["template_id"]) if b.get("template_id") else "")
        if not text.strip():
            raise ApiError("give 'text' or 'template_id'", 400)
        rows = self._rows(b)
        common = b.get("vars") or {}
        if len(rows) == 1 and not b.get("as_campaign"):
            self._need_board()
            row = {**common, **rows[0]}
            num = re.sub(r"[^\d+]", "", row["number"])
            if len(num.lstrip("+")) < 5:
                raise ApiError("invalid phone number", 400)
            body = render_template(text, {**row, "number": num}).strip()
            try:
                parts = await self.d["modem"].send_sms(num, body)
                await self.d["record_sent"](num, body, "sent", "")
            except Exception as e:
                await self.d["record_sent"](num, body, "failed", str(e))
                raise ApiError(f"SMS failed: {e}", 502)
            return self._ok(to=num, parts=parts, text=body)
        res = await self._launch({"name": b.get("name") or "API SMS", "mode": "sms",
                                  "rows": [{**common, **r} for r in rows], "sms": {"pre": text},
                                  "gap": b.get("gap", 3), "email_to": b.get("email_to", "")})
        return self._ok(**res)

    async def calls(self, request):
        b = await self._body(request)
        rules = self._sms_rules(b.get("sms_rules"))
        if b.get("sms_template_id") and not rules:                 # one SMS after every call
            rules = [{"when": "always", "text": self._template_text(b["sms_template_id"])}]
        mode = b.get("mode") or ("call_sms" if rules else "call")
        pre = b.get("sms_text") or (self._template_text(b["pre_sms_template_id"]) if b.get("pre_sms_template_id") else "")
        cfg = {"name": b.get("name") or "API broadcast call", "mode": mode, "rows": self._rows(b),
               "audio": b.get("audio", ""), "tts": b.get("tts", ""), "repeat": b.get("repeat", 1),
               "ring_timeout": b.get("ring_timeout", 35), "key_wait": b.get("key_wait", 10), "gap": b.get("gap", 5),
               "stop_on_key": b.get("stop_on_key", True), "record": bool(b.get("record")),
               "keymap": b.get("keymap") or {}, "sms": {"pre": pre, "rules": rules, "delay": b.get("sms_delay", 30)},
               "email_to": b.get("email_to", "")}
        if not cfg["audio"] and not cfg["tts"]:
            raise ApiError("give 'audio' (file name from GET /api/v1/audio) or 'tts'", 400)
        return self._ok(**await self._launch(cfg))

    async def campaign_from_draft(self, request):
        b = await self._body(request)
        did = b.get("draft_id") or ""
        dr = next((x for x in self.d["drafts"].data if x.get("id") == did), None)
        if not dr:
            raise ApiError(f"draft_id '{did}' not found (GET /api/v1/drafts)", 404)
        cfg = {k: dr.get(k) for k in ("mode", "audio", "audio_size", "tts", "repeat", "ring_timeout", "key_wait", "gap",
                                       "stop_on_key", "record", "keymap", "sms", "email_to") if dr.get(k) is not None}
        cfg["name"] = b.get("name") or dr.get("name") or "API campaign"
        cfg["rows"] = self._rows(b) if (b.get("numbers") or b.get("rows") or b.get("to")) else (dr.get("rows") or [])
        if b.get("vars"):
            cfg["rows"] = [{**b["vars"], **r} for r in cfg["rows"]]
        if b.get("email_to"):
            cfg["email_to"] = b["email_to"]
        return self._ok(**await self._launch(cfg))

    def _camp_view(self, c: dict, full: bool) -> dict:
        sm = c.get("summary")
        if not sm:
            from campaign import summarize
            sm = summarize(c)
        v = {"id": c["id"], "name": c["name"], "mode": c.get("mode", "call"), "status": c["status"],
             "created": c["created"], "summary": sm}
        if full:
            km = c.get("keymap") or {}
            v["results"] = [{"number": r["number"], "name": (r.get("vars") or {}).get("name", ""), "status": r["status"],
                             "outcome": r.get("outcome"), "keys": r.get("keys", ""), "choice": km.get((r.get("keys") or "")[:1], ""),
                             "ring_s": r.get("ring_s"), "talk_s": r.get("talk_s"), "ended_by": r.get("ended_by"),
                             "network_reason": r.get("cause"), "recording": r.get("recording", ""),
                             "sms": [{k: s.get(k) for k in ("when", "status", "text", "time", "error")} for s in r.get("sms", [])],
                             "time": r.get("time")} for r in c["results"]]
        return v

    async def campaigns_list(self, request):
        lim = min(100, int(request.query.get("limit", 20)))
        return self._ok(campaigns=[self._camp_view(c, False) for c in self.d["campaigns"].data[:lim]])

    async def campaign_get(self, request):
        c = next((c for c in self.d["campaigns"].data if c["id"] == request.match_info["cid"]), None)
        if not c:
            raise ApiError("campaign not found", 404)
        return self._ok(campaign=self._camp_view(c, True))

    async def campaign_stop(self, request):
        r = self.d["runner"]
        if not r.current or r.current["id"] != request.match_info["cid"]:
            raise ApiError("that campaign is not running", 409)
        await r.stop()
        return self._ok(stopping=True)

    async def templates(self, request):
        return self._ok(templates=self.d["sms_templates"].data)

    async def drafts(self, request):
        return self._ok(drafts=[{"id": x["id"], "name": x.get("name", ""), "mode": x.get("mode", "call"),
                                 "audio": x.get("audio", ""), "tts": x.get("tts", ""), "recipients_saved": len(x.get("rows") or []),
                                 "updated": x.get("updated", "")} for x in self.d["drafts"].data])

    async def audio(self, request):
        lib = await self.d["library_state"]()
        return self._ok(audio=[{"name": f["name"], "bytes": f["size"], "on_module": f["on_module"]} for f in lib["files"]])

    async def messages(self, request):
        since = request.query.get("since", "")
        lim = min(500, int(request.query.get("limit", 100)))
        msgs = [x for x in self.d["messages"].data if not since or x.get("time", "") > since]
        return self._ok(messages=[{k: x.get(k) for k in ("id", "dir", "number", "text", "time", "status")} for x in msgs[-lim:]])

    def routes(self, app):
        r = app.router
        r.add_get("/api/v1/status", self.status)
        r.add_post("/api/v1/sms", self.sms)
        r.add_post("/api/v1/calls", self.calls)
        r.add_post("/api/v1/campaigns", self.campaign_from_draft)
        r.add_get("/api/v1/campaigns", self.campaigns_list)
        r.add_get("/api/v1/campaigns/{cid}", self.campaign_get)
        r.add_post("/api/v1/campaigns/{cid}/stop", self.campaign_stop)
        r.add_get("/api/v1/templates", self.templates)
        r.add_get("/api/v1/drafts", self.drafts)
        r.add_get("/api/v1/audio", self.audio)
        r.add_get("/api/v1/messages", self.messages)

    # ------------------------------------------------------------ webhooks
    async def webhook(self, event: str, data: dict):
        wh = self.cfg.get("webhook") or {}
        if not wh.get("url") or event not in (wh.get("events") or ["sms.received", "call.ended", "campaign.finished"]):
            return
        body = json.dumps({"event": event, "time": m.now_iso(), "data": data}, ensure_ascii=False).encode()
        sig = hmac.new((wh.get("secret") or "").encode(), body, hashlib.sha256).hexdigest()
        entry = {"time": m.now_iso(), "key": "webhook", "method": "POST", "path": f"{event} → {wh['url'][:60]}", "status": 0, "ms": 0, "ip": ""}
        t0 = time.time()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.post(wh["url"], data=body, headers={"Content-Type": "application/json",
                                                                 "X-B2Wave-Event": event, "X-B2Wave-Signature": f"sha256={sig}"}) as r:
                    entry["status"] = r.status
        except Exception as e:
            entry["status"] = f"error: {str(e)[:60]}"
        entry["ms"] = int((time.time() - t0) * 1000)
        self.log.appendleft(entry)


class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status
