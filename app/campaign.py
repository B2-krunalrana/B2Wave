"""
Voice campaigns (batch IVR calls) with per-call analytics.

For every number: dial -> wait for answer -> play audio / TTS into the call ->
collect keypad presses -> hang up -> store a full report:

  outcome      Responded / Listened, no response / Hung up during prompt / No answer /
               Busy / Rejected / Unreachable / Invalid number / Network error / Failed
  ended_by     B2Wave / other side / network
  ring_s       seconds from dial to answer
  talk_s       seconds connected
  prompt       played fully / partly / not played
  keys         digits pressed, each with the second it was pressed
  holds        times the other side put the call on hold
  cause        network reason (AT+CEER), e.g. "16 Normal call clearing"
  events       timeline of everything that happened, in seconds from dialing
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


def wav_seconds(size: int) -> float:
    return max(1.0, (size - 44) / 16000)        # 8 kHz, 16-bit mono


# 3GPP TS 24.008 cause values -> outcome for calls that were never answered
CAUSE_OUTCOME = {1: "Invalid number", 3: "Unreachable", 16: "No answer", 17: "Busy", 18: "No answer",
                 19: "No answer", 20: "Unreachable", 21: "Rejected", 22: "Invalid number", 27: "Unreachable",
                 28: "Invalid number", 31: "No answer", 34: "Network error", 38: "Network error",
                 41: "Network error", 42: "Network error", 44: "Network error", 47: "Network error"}


class CampaignRunner:
    def __init__(self, modem: m.Modem, emit: Emit, store):
        self.modem, self.emit, self.store = modem, emit, store
        self.task: asyncio.Task | None = None
        self.current: dict | None = None
        self._rec: dict | None = None
        self._t0 = 0.0
        self._keys: list[str] = []
        self._key_evt = asyncio.Event()
        self._cause_evt = asyncio.Event()
        self._play_done = asyncio.Event()
        self._stop = False

    # ------------------------------------------------------------ live events from the modem
    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    def _ev(self, ev: str, detail: str = ""):
        if self._rec is not None:
            self._rec["events"].append({"t": round(time.time() - self._t0, 1), "ev": ev, "detail": detail})

    def on_dtmf(self, key: str):
        self._keys.append(key)
        if self._rec is not None:
            t = round(time.time() - self._t0, 1)
            self._rec["key_times"].append({"key": key, "t": t})
            self._ev("key pressed", key)
        self._key_evt.set()

    def on_event(self, evt: dict):
        if self._rec is None:
            return
        r, t = self._rec, evt.get("type")
        if t == "call_event":
            ev = evt.get("ev")
            if ev == "state":
                self._ev("state", f"{evt.get('prev')} -> {evt.get('detail')}")
            elif ev == "remote_hold":
                r["holds"] += 1
                self._ev("put on hold", evt.get("detail", ""))
            elif ev == "remote_resume":
                self._ev("resumed", evt.get("detail", ""))
            elif ev == "end_code":
                r["end_code"] = evt.get("detail", "")
                self._ev("network said", evt.get("detail", ""))
            elif ev == "cause":
                r["cause"] = evt.get("detail", "")
                r["cause_code"] = evt.get("cause_code")
                r["ended_by"] = evt.get("ended_by", r.get("ended_by", ""))
                self._ev("end reason", evt.get("detail", ""))
                self._cause_evt.set()
            elif ev == "service":
                self._ev("network info", evt.get("detail", ""))
        elif t == "audio_event":
            line = evt.get("line", "")
            if "play stop" in line or line.startswith("+CCMXSTOP"):
                if r.get("_playing"):
                    r["_playing"] = False
                    self._ev("prompt stopped", "")
                    self._play_done.set()

    # ------------------------------------------------------------ control
    async def start(self, cfg: dict) -> dict:
        if self.running:
            raise RuntimeError("a campaign is already running")
        numbers = [re.sub(r"[^\d+]", "", n) for n in cfg.get("numbers", [])]
        numbers = [n for n in numbers if len(n.lstrip("+")) >= 5]
        if not numbers:
            raise ValueError("add at least one phone number")
        if not cfg.get("audio") and not cfg.get("tts"):
            raise ValueError("choose an audio file or enter text to speak")
        camp = {
            "id": uuid.uuid4().hex[:10], "name": cfg.get("name") or time.strftime("Campaign %d %b %H:%M"),
            "created": m.now_iso(), "status": "running",
            "audio": cfg.get("audio", ""), "audio_size": int(cfg.get("audio_size", 0)),
            "tts": cfg.get("tts", ""), "repeat": max(1, min(5, int(cfg.get("repeat", 1)))),
            "ring_timeout": max(10, min(90, int(cfg.get("ring_timeout", 35)))),
            "key_wait": max(3, min(60, int(cfg.get("key_wait", 10)))),
            "gap": max(2, min(120, int(cfg.get("gap", 5)))),
            "stop_on_key": bool(cfg.get("stop_on_key", True)),
            "results": [self._blank(n) for n in numbers],
        }
        self.store.data.insert(0, camp)
        self.store.data[:] = self.store.data[:50]
        self.store.save()
        self._stop = False
        self.current = camp
        self.task = asyncio.create_task(self._run(camp))
        await self.emit({"type": "campaign", "campaign": camp})
        return camp

    @staticmethod
    def _blank(number: str) -> dict:
        return {"number": number, "status": "pending", "outcome": "", "keys": "", "key_times": [],
                "ring_s": None, "talk_s": 0, "duration": 0, "prompt": "", "ended_by": "", "holds": 0,
                "cause": "", "cause_code": None, "end_code": "", "events": [], "time": "", "error": ""}

    async def stop(self):
        self._stop = True
        if self.modem.call.state != "idle":
            try:
                await self.modem.hangup()
            except Exception:
                pass

    # ------------------------------------------------------------ engine
    async def _push(self, camp):
        self.store.save()
        await self.emit({"type": "campaign", "campaign": camp})

    async def _run(self, camp):
        try:
            for i, r in enumerate(camp["results"]):
                if self._stop:
                    r.update(status="skipped", outcome="Skipped")
                    continue
                while self.modem.call.state != "idle":       # never interrupt a manual call
                    await asyncio.sleep(1)
                try:
                    await self._call_one(camp, r)
                except Exception as e:
                    r.update(status="failed", outcome="Failed", error=str(e))
                finally:
                    self._rec = None
                    r.pop("_playing", None)
                    r.pop("_stopped_by_key", None)
                await self._push(camp)
                if i < len(camp["results"]) - 1 and not self._stop:
                    await asyncio.sleep(camp["gap"])
            camp["status"] = "stopped" if self._stop else "done"
        except Exception as e:
            camp["status"] = f"error: {e}"
        finally:
            self.current = None
            camp["summary"] = summarize(camp)
            await self._push(camp)

    async def _wait_state(self, states: set, timeout: float) -> str:
        t0 = time.time()
        while time.time() - t0 < timeout and not self._stop:
            if self.modem.call.state in states:
                return self.modem.call.state
            await asyncio.sleep(0.15)
        return self.modem.call.state

    async def _finish(self, r, answered_at: float | None):
        """Wait briefly for the network's end reason, then classify the call."""
        if self.modem.call.state != "idle":
            await self.modem.hangup()
        try:
            await asyncio.wait_for(self._cause_evt.wait(), 4)
        except asyncio.TimeoutError:
            pass
        if answered_at:
            r["talk_s"] = r["duration"] = int(time.time() - answered_at)
        r["keys"] = "".join(self._keys)
        r["outcome"] = classify(r, answered_at is not None)
        r["status"] = "done"

    async def _call_one(self, camp, r):
        self._keys.clear()
        self._key_evt.clear()
        self._cause_evt.clear()
        self._play_done.clear()
        r.update(self._blank(r["number"]))
        r.update(status="dialing", time=m.now_iso())
        self._rec, self._t0 = r, time.time()
        self._ev("dialing", r["number"])
        await self._push(camp)
        try:
            await self.modem.dial(r["number"])
        except Exception as e:
            r.update(error=str(e))
            self._ev("dial failed", str(e))
            await self._finish(r, None)
            return
        r["status"] = "ringing"
        await self._push(camp)
        st = await self._wait_state({"active", "idle"}, camp["ring_timeout"])
        if st != "active":
            if st != "idle":
                self._ev("no answer", f"gave up after {camp['ring_timeout']} s")
                r["end_code"] = r["end_code"] or "TIMEOUT"
            await self._finish(r, None)
            return

        answered = time.time()
        r["ring_s"] = round(answered - self._t0, 1)
        r["status"] = "answered"
        self._ev("answered", f"after {r['ring_s']} s")
        await self._push(camp)
        await asyncio.sleep(0.8)                                  # let the voice path settle

        play_len = 0.0
        if self.modem.call.state == "active":
            try:
                if camp["audio"]:
                    await self.modem.play_file(camp["audio"], remote=True, repeat=max(0, camp["repeat"] - 1))
                    play_len = wav_seconds(camp["audio_size"]) * camp["repeat"]
                else:
                    await self.modem.speak(camp["tts"], remote=True)
                    play_len = max(2.0, len(camp["tts"]) * 0.075)
                r["_playing"] = True
                r["prompt"] = "playing"
                self._ev("prompt started", camp["audio"] or "text-to-speech")
            except Exception as e:
                r["error"] = f"playback: {e}"
                r["prompt"] = "not played"
                self._ev("prompt failed", str(e))
        play_start = time.time()
        await self._push(camp)

        # listen for keys until: key (if stop_on_key) / timeout / they hang up
        deadline = play_start + play_len + camp["key_wait"]
        while time.time() < deadline and not self._stop:
            if self.modem.call.state == "idle":
                break
            if r.get("_playing") and play_len and time.time() - play_start > play_len + 1.5:
                r["_playing"] = False                             # no stop event from module: assume finished
                self._ev("prompt finished", "")
            try:
                await asyncio.wait_for(self._key_evt.wait(), 0.25)
            except asyncio.TimeoutError:
                continue
            self._key_evt.clear()
            r["keys"] = "".join(self._keys)
            await self._push(camp)
            if camp["stop_on_key"]:
                if r.get("_playing"):
                    r["_stopped_by_key"] = round(time.time() - play_start, 1)
                    await self.modem.stop_audio()
                await asyncio.sleep(1.5)                           # allow a second digit
                break

        heard = time.time() - play_start
        if r["prompt"] == "playing" and r.get("_stopped_by_key") is not None:
            r["prompt"] = f"stopped by key press at {r['_stopped_by_key']:.0f} s"
        elif r["prompt"] == "playing":
            r["prompt"] = "played fully" if (not r.get("_playing") or heard >= play_len - 0.5) else f"partly ({heard:.0f} of {play_len:.0f} s)"
        if self.modem.call.state == "idle":
            self._ev("other side hung up", f"{heard:.0f} s after prompt start")
        await self._finish(r, answered)


def classify(r: dict, answered: bool) -> str:
    code = r.get("cause_code")
    end = (r.get("end_code") or "").upper()
    if answered:
        if r.get("keys"):
            return "Responded"
        if r.get("ended_by") == "other side":
            return "Hung up during prompt" if str(r.get("prompt", "")).startswith("partly") else "Listened, no response"
        return "No response"
    if r.get("error") and not code:
        return "Failed"
    if "BUSY" in end:
        return "Busy"
    if "NO ANSWER" in end or end == "TIMEOUT":
        return "No answer"
    if code in CAUSE_OUTCOME:
        return CAUSE_OUTCOME[code]
    return "Not answered"


def summarize(camp: dict) -> dict:
    res = [r for r in camp["results"] if r.get("status") == "done"]
    ans = [r for r in res if r.get("ring_s") is not None]
    outcomes, keys = {}, {}
    for r in res:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        if r.get("keys"):
            keys[r["keys"][0]] = keys.get(r["keys"][0], 0) + 1
    return {
        "called": len(res), "answered": len(ans),
        "answer_rate": round(100 * len(ans) / len(res)) if res else 0,
        "responded": sum(1 for r in res if r.get("keys")),
        "avg_ring_s": round(sum(r["ring_s"] for r in ans) / len(ans), 1) if ans else 0,
        "avg_talk_s": round(sum(r["talk_s"] for r in ans) / len(ans), 1) if ans else 0,
        "outcomes": outcomes, "keys": keys,
    }


def to_csv(camp: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["number", "outcome", "keys_pressed", "key_times", "ring_seconds", "talk_seconds", "prompt",
                "ended_by", "holds", "network_reason", "time", "error", "timeline"])
    for r in camp["results"]:
        w.writerow([r["number"], r.get("outcome") or r.get("status"), r.get("keys", ""),
                    " ".join(f"{k['key']}@{k['t']}s" for k in r.get("key_times", [])),
                    r.get("ring_s") if r.get("ring_s") is not None else "", r.get("talk_s", 0), r.get("prompt", ""),
                    r.get("ended_by", ""), r.get("holds", 0), r.get("cause", ""), r.get("time", ""), r.get("error", ""),
                    " | ".join(f"{e['t']}s {e['ev']} {e['detail']}".strip() for e in r.get("events", []))])
    return out.getvalue()
