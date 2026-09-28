"""One shared UI standard: HTML parse mode, consistent headers, emoji set,
inline-keyboard layouts and footer hints. Every handler builds messages
through these helpers so the bot looks and feels professional everywhere.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

HEADER = "🎬"
DIVIDER = "━━━━━━━━━━━━━━━"
FOOTER_TIP = (
    "\n\n💡 <i>Type any movie name to search — add the year "
    "(e.g. <code>2023</code>) for exact matches.</i>"
)

PAGE_SIZE = 10  # movies per result page


# ---------------------------------------------------------------- text ---

def header(title: str) -> str:
    return f"{HEADER} <b>{title}</b>"


def movie_list_text(query: str, page: int, total_pages: int,
                    total_movies: int) -> str:
    lines = [
        f"{HEADER} <b>Results for “{query}”</b>",
        f"<i>{total_movies} movie{'s' if total_movies != 1 else ''} found</i>",
        DIVIDER,
    ]
    return "\n".join(lines)


def movie_row_label(idx: int, movie: dict) -> str:
    n = len(movie["files"])
    year = f" ({movie['year']})" if movie.get("year") else ""
    quals = ", ".join(sorted({f.get("quality") or "?" for f in movie["files"]
                              if f.get("quality")}))
    extra = f" <i>[{quals}]</i>" if quals else ""
    return f"{idx}. {movie['display']}{year} — {n} file{'s' if n != 1 else ''}{extra}"


def detail_text(display: str, meta: dict | None, files: list[dict]) -> str:
    title = (meta or {}).get("title") or display
    year = (meta or {}).get("year")
    year_s = f" ({year})" if year else ""
    rating = (meta or {}).get("rating") or 0
    rating_s = f" ⭐ <b>{rating}</b>" if rating else ""
    genres = ", ".join((meta or {}).get("genres") or [])
    genres_s = f"\n🎭 <i>{genres}</i>" if genres else ""
    plot = ((meta or {}).get("plot") or "")[:350]
    plot_s = f"\n\n📝 {plot}" if plot else ""
    lines = [f"{HEADER} <b>{title}</b>{year_s}{rating_s}{genres_s}{plot_s}",
             "", DIVIDER, f"📁 <b>{len(files)} file{'s' if len(files) != 1 else ''} available:</b>"]
    return "\n".join(lines)


def file_button_label(f: dict) -> str:
    q = f.get("quality") or "?"
    lang = f.get("language") or ""
    size = human_size(f.get("file_size"))
    lang_s = f" • {lang}" if lang else ""
    return f"📄 {q}{lang_s} • {size}"


def human_size(num: int | None) -> str:
    if not num:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}TB"


def not_found_text(query: str) -> str:
    return (
        f"😔 <b>Couldn't find “{query}”</b> in the library yet.\n"
        "Check the spelling, or tap a suggestion below 👇"
    )


def join_prompt_text(missing: list[str]) -> str:
    chans = "\n".join(f"• <code>{c}</code>" for c in missing)
    return (
        "🔐 <b>One quick step!</b>\n\n"
        "Join our channel(s) to unlock downloads:\n"
        f"{chans}\n\n"
        "Then tap <b>✅ I've Joined</b> — your results will appear automatically."
    )


# -------------------------------------------------------------- keyboards ---

def movie_list_keyboard(sid: str, movies: list[dict], page: int,
                        total_pages: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    start = page * PAGE_SIZE
    for i, movie in enumerate(movies[start:start + PAGE_SIZE], start=start + 1):
        rows.append([InlineKeyboardButton(
            f"🎞 {movie['display']}"
            + (f" ({movie['year']})" if movie.get("year") else ""),
            callback_data=f"mv:{sid}:{i - 1}",
        )])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sl:{sid}:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}",
                                        callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"sl:{sid}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("📩 Request this movie",
                                      callback_data=f"req:{sid}")])
    return InlineKeyboardMarkup(rows)


def detail_keyboard(sid: str, idx: int, page: int, files: list[dict],
                    watch_urls: dict[int, tuple[str, str]]) -> InlineKeyboardMarkup:
    """File buttons + stream/download url buttons + back.

    ``watch_urls``: file db id -> (watch_url, download_url); empty when the
    stream player is disabled.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for f in files[:12]:
        rows.append([InlineKeyboardButton(file_button_label(f),
                                          callback_data=f"get:{f['id']}")])
    if watch_urls:
        rows.append([
            InlineKeyboardButton("▶️ Watch Online", callback_data=f"w:{sid}:{idx}"),
            InlineKeyboardButton("⬇️ Download", callback_data=f"d:{sid}:{idx}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ Back to results",
                                      callback_data=f"bk:{sid}:{page}")])
    return InlineKeyboardMarkup(rows)


def stream_choice_keyboard(urls: list[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """[(label, watch_url, dl_url)] -> Telegram WebApp buttons.

    Each opens the watch web app (in-app player + download with progress);
    the raw dl_url is only used *inside* the web app.
    """
    rows = []
    for label, watch_url, _dl_url in urls:
        rows.append([
            InlineKeyboardButton(f"▶️ {label}",
                                 web_app=WebAppInfo(url=watch_url)),
        ])
    return InlineKeyboardMarkup(rows)


def suggestions_keyboard(sid: str, suggestions: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🔍 {s}", callback_data=f"dym:{sid}:{i}")]
            for i, s in enumerate(suggestions)]
    rows.append([InlineKeyboardButton("📩 Request this movie",
                                      callback_data=f"req:{sid}")])
    return InlineKeyboardMarkup(rows)


def join_keyboard(missing: list[str], pkey: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for ch in missing:
        if ch.startswith("@"):
            rows.append([InlineKeyboardButton(f"📢 Join {ch}",
                                              url=f"https://t.me/{ch[1:]}")])
        else:
            rows.append([InlineKeyboardButton(f"📢 Join channel {ch}",
                                              callback_data="noop")])
    rows.append([InlineKeyboardButton("✅ I've Joined",
                                      callback_data=f"join:{pkey}")])
    return InlineKeyboardMarkup(rows)


def trending_keyboard(trending: list[tuple[str, int]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🔥 {q[:40]}", callback_data=f"tq:{q[:50]}")]
            for q, _ in trending[:6]]
    return InlineKeyboardMarkup(rows)
