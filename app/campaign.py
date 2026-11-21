"""
Voice campaigns (batch IVR calls).

For every number: dial -> wait for answer -> play audio file / TTS into the call ->
collect keypad presses (DTMF) -> hang up -> store result. Results stream live to
the browser and are saved in data/campaigns.json.
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


class CampaignRunner:
    def __init__(self, modem: m.Modem, emit: Emit, store):
        self.modem, self.emit, self.store = modem, emit, store
        self.task: asyncio.Task | None = None
        self.current: dict | None = None
        self._keys: list[str] = []
        self._key_evt = asyncio.Event()
        self._stop = False

    # ------------------------------------------------------------ public
    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    def on_dtmf(self, key: str):
        self._keys.append(key)
        self._key_evt.set()

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
            "tts": cfg.get("tts", ""), "repeat": max(0, min(5, int(cfg.get("repeat", 1)))),
            "ring_timeout": max(10, min(90, int(cfg.get("ring_timeout", 35)))),
            "key_wait": max(3, min(60, int(cfg.get("key_wait", 10)))),
            "gap": max(2, min(120, int(cfg.get("gap", 5)))),
            "stop_on_key": bool(cfg.get("stop_on_key", True)),
            "results": [{"number": n, "status": "pending", "keys": "", "duration": 0, "time": ""}
                        for n in numbers],
        }
        self.store.data.insert(0, camp)
        self.store.data[:] = self.store.data[:50]
        self.store.save()
        self._stop = False
        self.current = camp
        self.task = asyncio.create_task(self._run(camp))
        await self.emit({"type": "campaign", "campaign": camp})
        return camp

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
                    r["status"] = "skipped"
                    continue
                while self.modem.call.state != "idle":       # never interrupt a manual call
                    await asyncio.sleep(1)
                await self._call_one(camp, r)
                await self._push(camp)
                if i < len(camp["results"]) - 1 and not self._stop:
                    await asyncio.sleep(camp["gap"])
            camp["status"] = "stopped" if self._stop else "done"
        except Exception as e:
            camp["status"] = f"error: {e}"
        finally:
            self.current = None
            await self._push(camp)

    async def _wait_state(self, states: set, timeout: float) -> str:
        t0 = time.time()
        while time.time() - t0 < timeout and not self._stop:
            if self.modem.call.state in states:
                return self.modem.call.state
            await asyncio.sleep(0.2)
        return self.modem.call.state

    async def _call_one(self, camp, r):
        r.update(status="dialing", time=m.now_iso(), keys="", error="")
        await self._push(camp)
        self._keys.clear()
        self._key_evt.clear()
        try:
            await self.modem.dial(r["number"])
        except Exception as e:
            r.update(status="failed", error=str(e))
            return
        r["status"] = "ringing"
        await self._push(camp)
        st = await self._wait_state({"active", "idle"}, camp["ring_timeout"])
        if st != "active":
            r["status"] = "no answer" if st != "idle" else "not answered / busy"
            if self.modem.call.state != "idle":
                await self.modem.hangup()
            return

        t_ans = time.time()
        r["status"] = "answered"
        await self._push(camp)
        await asyncio.sleep(1.0)                              # let the voice path settle
        try:
            if camp["audio"]:
                await self.modem.play_file(camp["audio"], remote=True, repeat=camp["repeat"])
                play_len = wav_seconds(camp["audio_size"]) * max(1, camp["repeat"])
            else:
                await self.modem.speak(camp["tts"], remote=True)
                play_len = max(2.0, len(camp["tts"]) * 0.075)
        except Exception as e:
            r["error"] = f"playback: {e}"
            play_len = 0

        # collect keys until timeout / first key / hang-up
        deadline = time.time() + play_len + camp["key_wait"]
        while time.time() < deadline and not self._stop:
            if self.modem.call.state == "idle":
                break
            try:
                await asyncio.wait_for(self._key_evt.wait(), 0.3)
            except asyncio.TimeoutError:
                continue
            self._key_evt.clear()
            r["keys"] = "".join(self._keys)
            await self._push(camp)
            if camp["stop_on_key"]:
                await self.modem.stop_audio()
                await asyncio.sleep(1.5)                       # let a 2nd digit arrive
                r["keys"] = "".join(self._keys)
                break
        r["keys"] = "".join(self._keys)
        r["duration"] = int(time.time() - t_ans)
        r["status"] = "answered" if self.modem.call.state != "idle" else "answered (they hung up)"
        if self.modem.call.state != "idle":
            await self.modem.hangup()


def to_csv(camp: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["number", "status", "keys_pressed", "talk_seconds", "time", "error"])
    for r in camp["results"]:
        w.writerow([r["number"], r["status"], r.get("keys", ""), r.get("duration", 0), r.get("time", ""),
                    r.get("error", "")])
    return out.getvalue()
