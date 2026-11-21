"""
B2Wave AI phone agent (LiveKit Agents + OpenAI Realtime).

B2Wave joins a LiveKit room for every AI call and dispatches this agent by name.
The persona (instructions, greeting, voice, language) arrives as dispatch metadata,
so you can change it from the B2Wave "AI Agent" page without touching this file.

Run:   run_agent.bat           (creates .venv, installs, starts the worker)
Needs: agent/.env with LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, OPENAI_API_KEY
       (B2Wave writes this file when you save the AI Agent settings).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli
from livekit.plugins import openai

load_dotenv(Path(__file__).with_name(".env"))
log = logging.getLogger("b2wave-agent")

AGENT_NAME = os.getenv("B2W_AGENT_NAME", "b2wave-agent")
PHONE_RULES = (
    "You are speaking on a live phone call. Audio quality may be low. "
    "Keep every reply short: one or two sentences. Ask one question at a time. "
    "Never read out URLs, markdown or lists. If the person wants to stop, say goodbye politely."
)
DEFAULT_PERSONA = "You are a friendly, professional assistant calling on behalf of the business."


async def entrypoint(ctx: JobContext):
    try:
        cfg = json.loads(ctx.job.metadata or "{}")
    except json.JSONDecodeError:
        cfg = {}
    await ctx.connect()
    log.info("call room=%s number=%s mode=%s", ctx.room.name, cfg.get("number"), cfg.get("mode"))

    instructions = (cfg.get("instructions") or DEFAULT_PERSONA).strip()
    if cfg.get("language"):
        instructions += f"\nAlways speak in {cfg['language']}, even if the person mixes languages."
    if cfg.get("number"):
        instructions += f"\nThe person's phone number is {cfg['number']}."
    instructions += "\n" + PHONE_RULES

    session = AgentSession(llm=openai.realtime.RealtimeModel(voice=cfg.get("voice") or "alloy"))
    await session.start(room=ctx.room, agent=Agent(instructions=instructions))

    greeting = (cfg.get("greeting") or "").strip()
    await session.generate_reply(
        instructions=(f"Start the call by saying this greeting naturally: \"{greeting}\"" if greeting
                      else "Greet the person briefly, say who you are, and ask how you can help."))


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME))
