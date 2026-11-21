"""
Bulk SMS: send one (personalised) message to many numbers, one after another,
with throttling, live progress, stop, per-number results and an email report.

Template variables: {number}, {name} and any other column from an uploaded CSV,
e.g. "Hi {name|Customer}, your order {order} ships today" ({x|fallback} is used when x is empty).
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
import time
import uuid
from typing import Awaitable, Callable

import modem as m

Emit = Callable[[dict], Awaitable[None]]
VAR_RE = re.compile(r"\{([A-Za-z0-9_]+)(?:\|([^}]*))?\}")


def render(template: str, row: dict) -> str:
    vals = {k.lower(): v for k, v in row.items()}
    def sub(mm):
        v = vals.get(mm.group(1).lower())
        if v is None:
            return mm.group(2) if mm.group(2) is not None else mm.group(0)
        return v if str(v).strip() or mm.group(2) is None else mm.group(2)
    return VAR_RE.sub(sub, template)


class BulkSmsRunner:
    def __init__(self, modem: m.Modem, emit: Emit, store, record_sent, on_done=None):
        self.modem, self.emit, self.store = modem, emit, store
        self.record_sent = record_sent          # async (number, text, status, error) -> None
        self.on_done = on_done                  # async (job) -> None
        self.task: asyncio.Task | None = None
        self._stop = False

    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    async def start(self, cfg: dict) -> dict:
        if self.running:
            raise RuntimeError("a bulk SMS job is already running")
        template = (cfg.get("template") or "").strip()
        if not template:
            raise ValueError("type the message")
        seen, rows = set(), []
        for r in cfg.get("rows", []):
            num = re.sub(r"[^\d+]", "", str(r.get("number", "")))
            if len(num.lstrip("+")) < 5 or num in seen:
                continue
            seen.add(num)
            rows.append({**{k: str(v) for k, v in r.items()}, "number": num})
        if not rows:
            raise ValueError("add at least one valid phone number")
        job = {"id": uuid.uuid4().hex[:10], "name": cfg.get("name") or time.strftime("Bulk SMS %d %b %H:%M"),
               "created": m.now_iso(), "status": "running", "template": template,
               "gap": max(1, min(120, int(cfg.get("gap", 3)))), "email_to": cfg.get("email_to", ""),
               "email_status": "", "results": [{"number": r["number"], "vars": r, "text": render(template, r),
                                                "status": "pending", "error": "", "time": "", "parts": 0} for r in rows]}
        self.store.data.insert(0, job)
        self.store.data[:] = self.store.data[:50]
        self.store.save()
        self._stop = False
        self.task = asyncio.create_task(self._run(job))
        await self.emit({"type": "bulk", "job": job})
        return job

    async def stop(self):
        self._stop = True

    async def _push(self, job):
        self.store.save()
        await self.emit({"type": "bulk", "job": job})

    async def _run(self, job):
        try:
            for i, r in enumerate(job["results"]):
                if self._stop:
                    r["status"] = "skipped"
                    continue
                while self.modem.call.state != "idle" or not self.modem.status.connected:
                    if self._stop:
                        break
                    await asyncio.sleep(1)                     # wait out calls / reconnects
                if self._stop:
                    r["status"] = "skipped"
                    continue
                r.update(status="sending", time=m.now_iso())
                await self._push(job)
                for attempt in (1, 2):                         # one automatic retry
                    try:
                        r["parts"] = await self.modem.send_sms(r["number"], r["text"])
                        r.update(status="sent", error="")
                        break
                    except Exception as e:
                        r.update(status="failed", error=str(e))
                        if attempt == 1:
                            await asyncio.sleep(3)
                try:
                    await self.record_sent(r["number"], r["text"], r["status"], r["error"])
                except Exception:
                    pass
                await self._push(job)
                if i < len(job["results"]) - 1 and not self._stop:
                    await asyncio.sleep(job["gap"])
            job["status"] = "stopped" if self._stop else "done"
        except Exception as e:
            job["status"] = f"error: {e}"
        finally:
            await self._push(job)
            if self.on_done:
                try:
                    await self.on_done(job)
                except Exception:
                    pass
            await self._push(job)


def to_csv(job: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["number", "status", "parts", "time", "error", "message"])
    for r in job["results"]:
        w.writerow([r["number"], r["status"], r.get("parts", 0), r.get("time", ""), r.get("error", ""), r["text"]])
    return out.getvalue()
