"""
B2Wave AI phone agent (LiveKit Agents).

B2Wave joins a LiveKit room for every AI call and dispatches this agent by name.
Everything that defines the agent arrives as dispatch metadata from the B2Wave
"AI voice agent" page, so you never edit this file:

  engine = "inference"  LiveKit Inference: STT + LLM + TTS through your LiveKit Cloud
                        project. No other API keys needed.
  engine = "openai"     OpenAI Realtime (speech-to-speech) with your own OPENAI_API_KEY.

Run it from B2Wave (AI voice agent > Agent worker > Start), or: run_agent.bat
Needs agent/.env with LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET (+ OPENAI_API_KEY
for the openai engine). B2Wave writes this file when you save the settings.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli

load_dotenv(Path(__file__).with_name(".env"))
log = logging.getLogger("b2wave-agent")

AGENT_NAME = os.getenv("B2W_AGENT_NAME", "b2wave-agent")
PHONE_RULES = (
    "You are speaking on a live phone call. Audio quality may be low. "
    "Keep every reply short: one or two sentences. Ask one question at a time. "
    "Never read out URLs, markdown or lists. If the person wants to stop, say goodbye politely."
)
DEFAULT_PERSONA = "You are a friendly, professional assistant calling on behalf of the business."
DEFAULTS = {
    "stt": "deepgram/nova-3:multi",
    "llm": "openai/gpt-4.1-mini",
    "tts": "cartesia/sonic-2",
    "tts_voice": "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
}


def build_session(cfg: dict) -> AgentSession:
    engine = cfg.get("engine") or "inference"
    if engine == "openai":
        from livekit.plugins import openai
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("engine=openai but OPENAI_API_KEY is missing in agent/.env")
        return AgentSession(llm=openai.realtime.RealtimeModel(voice=cfg.get("voice") or "alloy"))
    # LiveKit Inference: models are strings, billed through the LiveKit Cloud project
    stt = cfg.get("stt") or DEFAULTS["stt"]
    llm = cfg.get("llm") or DEFAULTS["llm"]
    tts = cfg.get("tts") or DEFAULTS["tts"]
    voice = (cfg.get("tts_voice") or "").strip()
    if voice and ":" not in tts:
        tts = f"{tts}:{voice}"
    elif not voice and ":" not in tts and tts.startswith("cartesia"):
        tts = f"{tts}:{DEFAULTS['tts_voice']}"
    log.info("LiveKit Inference stt=%s llm=%s tts=%s", stt, llm, tts)
    return AgentSession(stt=stt, llm=llm, tts=tts, turn_detection="stt")


async def entrypoint(ctx: JobContext):
    try:
        cfg = json.loads(ctx.job.metadata or "{}")
    except json.JSONDecodeError:
        cfg = {}
    await ctx.connect()
    log.info("call room=%s number=%s mode=%s engine=%s", ctx.room.name, cfg.get("number"), cfg.get("mode"), cfg.get("engine"))

    instructions = (cfg.get("instructions") or DEFAULT_PERSONA).strip()
    if cfg.get("language"):
        instructions += f"\nAlways speak in {cfg['language']}, even if the person mixes languages."
    if cfg.get("number"):
        instructions += f"\nThe person's phone number is {cfg['number']}."
    instructions += "\n" + PHONE_RULES

    session = build_session(cfg)
    await session.start(room=ctx.room, agent=Agent(instructions=instructions))

    greeting = (cfg.get("greeting") or "").strip()
    await session.generate_reply(
        instructions=(f"Start the call by saying this greeting naturally: \"{greeting}\"" if greeting
                      else "Greet the person briefly, say who you are, and ask how you can help."))


if __name__ == "__main__":
    # port=0: let Windows pick any free port for the worker's health endpoint
    # (the default 8081 clashes with other local tools).
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME,
                              port=int(os.getenv("B2W_AGENT_PORT", "0"))))
