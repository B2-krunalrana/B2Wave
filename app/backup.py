"""
Backup / restore of all B2Wave data as a single .zip.

backup.zip
  manifest.json        app, version, created, counts, includes_secrets
  data/messages.json   data/calls.json   data/campaigns.json   data/bulk.json   data/ai_calls.json
  data/config.json     (secrets removed unless include_secrets)
  data/audio/*.wav     voice-campaign prompts
  agent/.env           only when include_secrets
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from pathlib import Path

APP_VERSION = "1.0"
STORE_FILES = ("messages.json", "calls.json", "campaigns.json", "bulk.json", "ai_calls.json")
SECRET_KEYS = {"smtp": ("password",), "ai": ("api_secret",)}
MAX_ZIP = 300 * 1024 * 1024
SAFE_AUDIO = re.compile(r"^(?!.*\.\.)[A-Za-z0-9_-][A-Za-z0-9_.-]{0,59}\.(wav|amr|mp3)$", re.I)


def _strip_secrets(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    for section, keys in SECRET_KEYS.items():
        for k in keys:
            if isinstance(out.get(section), dict):
                out[section].pop(k, None)
    return out


def counts(stores: dict, audio_dir: Path) -> dict:
    c = {name: len(store.data) for name, store in stores.items() if isinstance(store.data, list)}
    c["audio"] = len([f for f in audio_dir.glob("*") if f.is_file()]) if audio_dir.exists() else 0
    return c


def build(stores: dict, config: dict, audio_dir: Path, agent_env: Path, include_secrets: bool) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, store in stores.items():
            z.writestr(f"data/{name}.json", json.dumps(store.data, ensure_ascii=False, indent=1))
        z.writestr("data/config.json", json.dumps(config if include_secrets else _strip_secrets(config),
                                                  ensure_ascii=False, indent=1))
        if audio_dir.exists():
            for f in sorted(audio_dir.glob("*")):
                if f.is_file():
                    z.write(f, f"data/audio/{f.name}")
        if include_secrets and agent_env.exists():
            z.write(agent_env, "agent/.env")
        z.writestr("manifest.json", json.dumps({
            "app": "B2Wave", "version": APP_VERSION, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "includes_secrets": include_secrets, "counts": counts(stores, audio_dir)}, indent=1))
    return buf.getvalue()


def inspect(blob: bytes) -> dict:
    """Validate a backup and summarise it without changing anything."""
    if len(blob) > MAX_ZIP:
        raise ValueError("Backup is larger than 300 MB")
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise ValueError("This is not a valid .zip file")
    names = set(z.namelist())
    if "manifest.json" not in names:
        raise ValueError("This zip is not a B2Wave backup (manifest.json missing)")
    man = json.loads(z.read("manifest.json"))
    if man.get("app") != "B2Wave":
        raise ValueError("This zip was not created by B2Wave")
    found = {}
    for fn in STORE_FILES:
        if f"data/{fn}" in names:
            try:
                found[fn[:-5]] = len(json.loads(z.read(f"data/{fn}")))
            except Exception:
                raise ValueError(f"data/{fn} in the backup is damaged")
    found["audio"] = len([n for n in names if n.startswith("data/audio/") and not n.endswith("/")])
    return {"manifest": man, "counts": found, "has_config": "data/config.json" in names,
            "has_agent_env": "agent/.env" in names, "size": len(blob)}


def _merge_list(cur: list, new: list, key) -> int:
    seen = {key(x) for x in cur}
    added = 0
    for x in new:
        k = key(x)
        if k not in seen:
            cur.append(x)
            seen.add(k)
            added += 1
    return added


def _key(x: dict):
    if x.get("id"):
        return ("id", x["id"])
    return ("v", x.get("number"), x.get("time"), x.get("text") or x.get("direction"))


def restore(blob: bytes, mode: str, stores: dict, config_store, audio_dir: Path, agent_env: Path) -> dict:
    """mode = 'merge' (add missing records) or 'replace' (overwrite).
    The current connection settings (COM port, bridge IP) are always kept: they belong to this PC."""
    info = inspect(blob)
    z = zipfile.ZipFile(io.BytesIO(blob))
    result = {}
    for name, store in stores.items():
        fn = f"data/{name}.json"
        if fn not in z.namelist():
            continue
        incoming = json.loads(z.read(fn))
        if mode == "replace":
            store.data[:] = incoming
            result[name] = len(incoming)
        else:
            result[name] = _merge_list(store.data, incoming, _key)
        if name == "messages":
            store.data.sort(key=lambda m: m.get("time", ""))
        if name in ("campaigns", "bulk", "ai_calls"):
            store.data.sort(key=lambda c: c.get("created") or c.get("started") or "", reverse=True)
        store.save()

    if "data/config.json" in z.namelist():
        inc = json.loads(z.read("data/config.json"))
        cur = config_store.data
        keep = {k: cur.get(k) for k in ("mode", "host", "serial_port", "baud", "last_host") if k in cur}
        for section in ("smtp", "ai"):
            if isinstance(inc.get(section), dict):
                base = dict(cur.get(section) or {}) if mode == "merge" else {}
                for k, v in inc[section].items():
                    if mode == "replace" or not base.get(k):
                        base[k] = v
                for k in SECRET_KEYS.get(section, ()):         # never wipe a saved secret with "missing"
                    if not inc[section].get(k) and (cur.get(section) or {}).get(k):
                        base[k] = cur[section][k]
                cur[section] = base
        cur["setup_done"] = cur.get("setup_done") or inc.get("setup_done", False)
        cur.update(keep)
        config_store.save()
        result["settings"] = "restored"

    audio_dir.mkdir(exist_ok=True)
    n_audio = 0
    for n in z.namelist():
        if n.startswith("data/audio/") and not n.endswith("/"):
            rel = n[len("data/audio/"):]
            if "/" in rel or "\\" in rel or not SAFE_AUDIO.match(rel):
                continue                                         # only plain audio file names
            target = (audio_dir / rel).resolve()
            if target.parent != audio_dir.resolve():
                continue                                         # never write outside data/audio
            if mode == "merge" and target.exists():
                continue
            target.write_bytes(z.read(n))
            n_audio += 1
    result["audio"] = n_audio

    if "agent/.env" in z.namelist() and (mode == "replace" or not agent_env.exists()):
        agent_env.parent.mkdir(exist_ok=True)
        agent_env.write_bytes(z.read("agent/.env"))
        result["agent_keys"] = "restored"
    return {"mode": mode, "restored": result, "backup": info["manifest"]}
