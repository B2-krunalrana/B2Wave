"""
Minimal LiveKit server helpers (standard library only):
  * access tokens (JWT, HS256) incl. agent dispatch via roomConfig
  * connection test through the RoomService API
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import aiohttp


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_token(api_key: str, api_secret: str, identity: str, *, name: str = "", grants: dict | None = None,
               room_config: dict | None = None, metadata: str = "", ttl: int = 3600) -> str:
    now = int(time.time())
    payload = {"iss": api_key, "sub": identity, "nbf": now - 10, "exp": now + ttl, "jti": f"{identity}-{now}",
               "video": grants or {}}
    if name:
        payload["name"] = name
    if metadata:
        payload["metadata"] = metadata
    if room_config:
        payload["roomConfig"] = room_config
    seg = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()) + "." + \
        _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(api_secret.encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64(sig)


def http_url(ws_url: str) -> str:
    u = ws_url.strip().rstrip("/")
    return u.replace("wss://", "https://", 1).replace("ws://", "http://", 1)


def configured(cfg: dict | None) -> bool:
    return bool(cfg and cfg.get("url") and cfg.get("api_key") and cfg.get("api_secret"))


async def list_rooms(cfg: dict) -> list:
    token = make_token(cfg["api_key"], cfg["api_secret"], "b2wave-admin", grants={"roomList": True}, ttl=120)
    url = http_url(cfg["url"]) + "/twirp/livekit.RoomService/ListRooms"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as s:
        async with s.post(url, json={}, headers={"Authorization": f"Bearer {token}"}) as r:
            body = await r.text()
            if r.status == 401:
                raise RuntimeError("LiveKit rejected the API key/secret (401). Copy them again from the LiveKit project settings.")
            if r.status >= 400:
                raise RuntimeError(f"LiveKit error {r.status}: {body[:200]}")
            return (json.loads(body or "{}")).get("rooms", [])


def call_token(cfg: dict, room: str, identity: str, agent_meta: dict) -> str:
    """Token for the B2Wave audio bridge. Joining the room dispatches the named agent."""
    return make_token(cfg["api_key"], cfg["api_secret"], identity, name="B2Wave phone bridge",
                      grants={"roomJoin": True, "room": room, "canPublish": True, "canSubscribe": True,
                              "canPublishData": True, "roomCreate": True},
                      room_config={"agents": [{"agentName": cfg.get("agent_name") or "b2wave-agent",
                                               "metadata": json.dumps(agent_meta)}]},
                      ttl=3 * 3600)
