"""Process-local ephemeral state.

Single-process assumption (one uvicorn worker): search sessions, pending
force-sub actions, hot-query caches and a short-lived settings cache live
here with TTLs. If you ever scale to multiple workers, replace these with
Redis — the access functions are the seam.
"""
from __future__ import annotations

import secrets
import time
from collections import OrderedDict

# search_id -> {"movies": [...], "query": str, "page": int, "created": float}
SEARCH_SESSIONS: dict[str, dict] = {}

# user_id -> {"type": "search"|"file", ...} pending force-sub delivery
PENDING: dict[int, dict] = {}

# cache_key -> (expires_at, value)
_HOT: OrderedDict[str, tuple[float, object]] = OrderedDict()
_HOT_MAX = 256

# settings key -> (expires_at, value)
_SETTINGS_CACHE: dict[str, tuple[float, object]] = {}

SEARCH_TTL = 30 * 60  # search sessions live 30 min
HOT_TTL = 5 * 60  # hot query cache 5 min
SETTINGS_TTL = 60  # settings cache 60 s


def new_search_id() -> str:
    return secrets.token_hex(4)


def store_search(session: dict) -> str:
    prune()
    sid = new_search_id()
    session["created"] = time.time()
    SEARCH_SESSIONS[sid] = session
    return sid


def get_search(sid: str) -> dict | None:
    sess = SEARCH_SESSIONS.get(sid)
    if not sess:
        return None
    if time.time() - sess.get("created", 0) > SEARCH_TTL:
        SEARCH_SESSIONS.pop(sid, None)
        return None
    return sess


def set_pending(user_id: int, payload: dict) -> str:
    """Store a pending post-force-sub action; returns its key."""
    key = secrets.token_hex(4)
    payload["created"] = time.time()
    payload["pkey"] = key
    PENDING[user_id] = payload
    return key


def pop_pending(user_id: int) -> dict | None:
    payload = PENDING.pop(user_id, None)
    if payload and time.time() - payload.get("created", 0) > SEARCH_TTL:
        return None
    return payload


def hot_get(key: str):
    entry = _HOT.get(key)
    if not entry:
        return None
    expires, value = entry
    if time.time() > expires:
        _HOT.pop(key, None)
        return None
    _HOT.move_to_end(key)
    return value


def hot_set(key: str, value: object, ttl: int = HOT_TTL) -> None:
    _HOT[key] = (time.time() + ttl, value)
    _HOT.move_to_end(key)
    while len(_HOT) > _HOT_MAX:
        _HOT.popitem(last=False)


def settings_cache_get(key: str):
    entry = _SETTINGS_CACHE.get(key)
    if not entry:
        return None
    expires, value = entry
    if time.time() > expires:
        _SETTINGS_CACHE.pop(key, None)
        return None
    return value


def settings_cache_set(key: str, value: object) -> None:
    _SETTINGS_CACHE[key] = (time.time() + SETTINGS_TTL, value)


def settings_cache_invalidate(key: str | None = None) -> None:
    if key is None:
        _SETTINGS_CACHE.clear()
    else:
        _SETTINGS_CACHE.pop(key, None)


def prune() -> None:
    now = time.time()
    for sid in [s for s, v in SEARCH_SESSIONS.items()
                if now - v.get("created", 0) > SEARCH_TTL]:
        SEARCH_SESSIONS.pop(sid, None)
    for uid in [u for u, v in PENDING.items()
                if now - v.get("created", 0) > SEARCH_TTL]:
        PENDING.pop(uid, None)
