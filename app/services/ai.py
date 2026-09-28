"""Optional AI layer (OpenAI-compatible chat API).

Everything here is best-effort: with no AI_API_KEY configured the bot runs
100% on smart templates. Calls are short, cached, timed out, and never raise
into the request path.
"""
from __future__ import annotations

import hashlib
import logging

import httpx

from app.config import settings
from app.state import hot_get, hot_set

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def enabled() -> bool:
    return bool(settings.AI_API_KEY.strip())


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.AI_BASE_URL.rstrip("/"),
            timeout=httpx.Timeout(20.0, connect=8.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={
                "Authorization": f"Bearer {settings.AI_API_KEY}",
                "Content-Type": "application/json",
            },
        )
    return _client


async def _chat(system: str, user: str, max_tokens: int = 120) -> str | None:
    """One short chat completion; None on any failure or when disabled."""
    if not enabled():
        return None
    digest = hashlib.sha256(f"{system}|{user}".encode()).hexdigest()[:16]
    cache_key = f"ai:{digest}"
    cached = hot_get(cache_key)
    if cached:
        return cached  # type: ignore[return-value]
    try:
        resp = await _get_client().post(
            "/chat/completions",
            json={
                "model": settings.AI_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.7,
            },
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        if text:
            hot_set(cache_key, text, ttl=24 * 3600)
            return text
    except Exception as exc:  # noqa: BLE001 - AI must never break the bot
        log.warning("AI call failed: %s", exc)
    return None


def not_found_template(query: str, suggestions: list[str]) -> str:
    lines = [
        f"😔 <b>Couldn't find “{query}”</b> in the library yet.",
    ]
    if suggestions:
        lines.append("Check the spelling, or try one of these:")
    else:
        lines.append("Check the spelling and try again — or request it below 👇")
    return "\n".join(lines)


async def not_found_message(query: str, suggestions: list[str]) -> str:
    """Friendly not-found reply: AI-generated when a key exists, else template."""
    if enabled():
        sug = ", ".join(suggestions[:3]) if suggestions else "none"
        text = await _chat(
            system=(
                "You are the friendly assistant of a Telegram movie bot. "
                "Reply in one or two short sentences, warm tone, no markdown, "
                "plain text only. Never invent download links."
            ),
            user=(
                f'A user searched for the movie "{query}" and it was not found. '
                f"Similar titles in the library: {sug}. "
                "Write a friendly reply apologizing and nudging them to tap a "
                "suggestion or request the movie."
            ),
            max_tokens=90,
        )
        if text:
            return text
    return not_found_template(query, suggestions)


async def personalized_line(recent_queries: list[str]) -> str | None:
    """'Because you searched X…' line; None when disabled or no history."""
    if not enabled() or not recent_queries:
        return None
    shown = ", ".join(recent_queries[:4])
    return await _chat(
        system=(
            "You are the friendly assistant of a Telegram movie bot. "
            "Write exactly one short playful sentence, plain text, no markdown."
        ),
        user=(
            f"A user recently searched for: {shown}. Write one short playful "
            "sentence recommending they explore similar movies in the bot."
        ),
        max_tokens=60,
    )
