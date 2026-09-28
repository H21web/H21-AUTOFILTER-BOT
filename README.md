# Autofilter Bot 🎬

A portable, production-ready Telegram **autofilter bot**: `python-telegram-bot`
21.x (async, Bot API only) behind a FastAPI webhook, PostgreSQL via async
SQLAlchemy, Alembic migrations. Files are served by `file_id` through the Bot
API, so the app server uses ~zero bandwidth.

## Features

**🔍 Ultra-fast, relevant search** (`app/services/search.py`)
- Two-stage: `tsvector` GIN full-text rank first, `pg_trgm` similarity
  fallback for typos — "avngers" still finds *Avengers*.
- Queries are normalized: `avengers 2019 1080p hindi` searches "avengers"
  and applies year/quality/language as filters.
- Quality/language auto-detected from filenames
  (`480p/720p/1080p/2160p`, Hindi/Malayalam/Tamil/Telugu/Kannada/English/Multi).
- In-memory LRU cache for hot queries (5 min); every query logged to
  `search_logs` → powers **🔥 trending**.
- Results grouped per movie, paginated (10/page).

**⚡ Auto-filter (groups + PM)** — any text message triggers a search.
Result list → tap a movie → TMDB poster/rating/plot card with quality
buttons → tap a file to receive it instantly. Misses get a friendly reply,
**"did you mean?"** buttons and a **📩 Request this movie** button
(AI-crafted reply when `AI_API_KEY` is set, polished template otherwise).

**📥 Auto-indexing + new-movie alerts** — files posted in channels are
indexed automatically (file_id, name, size, quality/language). The first
file of a new title triggers an alert card in `MAIN_CHANNEL_ID` with poster
and a deep-link button.

**🔐 Smart force-subscribe** — membership in `FORCE_SUB_CHANNELS` is checked
before results/files. Non-members get join buttons + **✅ I've Joined**;
tapping it re-verifies and **auto-delivers the pending results**.

**🎭 TMDB, lightning fast** — 30-day Postgres cache (`tmdb_cache` table):
cache hit = zero API calls. Works fine with no key (filename fallback).

**🤖 Optional AI layer** — any OpenAI-compatible chat endpoint
(`AI_BASE_URL`, default `https://api.openai.com/v1`) for not-found replies
and personalized nudges. No key → smart templates. Never crashes.

**🛠 Admin** — `/stats /broadcast /ban /unban /users /settings /requests`
(admin-only via `ADMIN_IDS`).

**▶️ Stream/download web player** — signed expiring links (HMAC, 6h):
`/watch/{token}` HTML5 player with poster backdrop, `/dl/{token}` with
HTTP Range support (seeking works).

> ⚠️ **Bandwidth note:** `/dl` proxies bytes *through this server*, so it
> consumes **server** bandwidth (AlwaysData free = 10 GB/month). File
> delivery *inside Telegram* (file_id) uses zero server bandwidth — the
> player is the only bandwidth-hungry part. Disable with
> `ENABLE_STREAM_PLAYER=false` or the dashboard toggle.

**🖥 Admin web dashboard** — `https://YOUR-HOST/admin` (login with
`ADMIN_USERNAME`/`ADMIN_PASSWORD`; disabled when the password is empty):
Overview stat cards + top searches, file browser (search/delete), user
management (ban/unban), broadcast composer, request queue, settings
(force-sub channels, toggles, main channel). Dark, mobile-friendly UI.

## Architecture

```
Telegram --webhook--> FastAPI (/webhook) --queue--> PTB Application --Bot API--> Telegram
                              |  |---- /watch, /dl (stream player)
                              |  `---- /admin (dashboard)
                              v
                     PostgreSQL (asyncpg)
```

* **No polling, no MTProto.** PTB runs in webhook mode.
* **12-factor / portable.** All config via environment variables. No vendor
  SDKs, no hardcoded hosts. Runs unchanged on AlwaysData, Render, any VPS,
  or Docker. Switching servers = set env vars + `python set_webhook.py
  https://NEW-HOST/webhook`. Code changes: zero.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | Bot token from @BotFather |
| `DATABASE_URL` | yes | Postgres URL (`postgresql://…` auto-rewritten to `+asyncpg://`) |
| `WEBHOOK_URL` | yes | Public HTTPS URL, e.g. `https://<host>/webhook` |
| `WEBHOOK_SECRET` | no | Verified against `X-Telegram-Bot-Api-Secret-Token`; also HMAC key for stream links |
| `TMDB_API_KEY` | no | TMDB key for metadata/posters; empty = filename fallback |
| `ADMIN_IDS` | no | Comma-separated Telegram user ids |
| `FORCE_SUB_CHANNELS` | no | Comma-separated `@usernames`/ids users must join |
| `MAIN_CHANNEL_ID` | no | Channel id for new-movie alerts; empty = disabled |
| `AI_API_KEY` | no | OpenAI-compatible key for smart replies; empty = templates |
| `AI_BASE_URL` | no | Default `https://api.openai.com/v1` |
| `AI_MODEL` | no | Default `gpt-4o-mini` |
| `ENABLE_STREAM_PLAYER` | no | Default `true`; `/watch` + `/dl` |
| `STREAM_LINK_TTL_HOURS` | no | Default `6` |
| `ADMIN_USERNAME` | no | Dashboard login (default `admin`) |
| `ADMIN_PASSWORD` | no | Dashboard login; **empty = dashboard disabled** |
| `LOG_LEVEL` | no | Default `INFO` |
| `PORT` | no | Default `8000` (hosts like Render inject their own) |

Copy `.env.example` to `.env` for local development. Never commit real values.

## Local run

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env            # then fill in real values
alembic upgrade head            # tables + pg_trgm/unaccent + trigger + feature tables
uvicorn app.main:app --host 0.0.0.0 --port 8000
python set_webhook.py https://YOUR-PUBLIC-HOST/webhook
```

Health check: `GET /health` → `{"status":"ok","db":"ok"}` (db pings Postgres).
Dashboard: `https://YOUR-PUBLIC-HOST/admin`.

> **Group search needs privacy mode off:** talk to @BotFather →
> `/setprivacy` → select your bot → **Disable**. Otherwise the bot only sees
> commands in groups.
>
> **Force-subscribe:** prefer `@username` channels (join buttons link to
> `t.me/...`). The bot must be able to see membership — for private
> channels it needs to be a member/admin.

## Docker run

```bash
docker build -t autofilter-bot .
docker run --env-file .env -p 8000:8000 autofilter-bot
```

## Deploy notes (all three = set env vars, run uvicorn, run set_webhook.py)

**AlwaysData (free, no sleep):** create a Python *Site* pointing at the repo
(or upload), set the env vars in the admin panel, command
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`. No Docker on the free plan —
use the native Python site. Pair with **Neon** free Postgres (0.5 GB).

**Render:** new *Web Service* from the repo (native Python or Docker). Render
injects `$PORT`; the Dockerfile and Procfile both honor it. Free tier sleeps
after 15 min idle — fine for dev, use Starter ($7/mo) for always-on.

**VPS:** clone, create venv, install requirements, run Alembic, then run
uvicorn behind systemd / Caddy / nginx with TLS. `set_webhook.py` works the
same everywhere.

## Migration workflow

```bash
alembic upgrade head        # apply
alembic downgrade -1        # roll back one
alembic revision --autogenerate -m "describe change"   # new migration
```

- `0001_initial` — `files`, `users`, `groups`; `pg_trgm` + `unaccent`;
  `GIN(search_vector)` + trigram index; trigger syncing `search_vector`.
- `0002_features` — `tmdb_cache`, `search_logs`, `requests`, `bot_settings`;
  `files.title_key` + backfill.

## Project layout

```
autofilter-bot/
├── app/
│   ├── config.py        # pydantic-settings, env-only
│   ├── db.py            # async engine + session factory
│   ├── models.py        # files / users / groups / tmdb_cache /
│   │                    #   search_logs / requests / bot_settings
│   ├── state.py         # ephemeral in-memory state (sessions, pending, caches)
│   ├── ui.py            # one shared UI standard (HTML, emoji, keyboards)
│   ├── bot.py           # builds PTB Application, registers handlers
│   ├── main.py          # FastAPI: lifespan, /health, /webhook, /watch, /dl, /admin
│   ├── player.py        # signed stream/download links + Range proxy
│   ├── dashboard/       # /admin router + jinja2 templates + static CSS
│   ├── handlers/        # start, search, callbacks, index, forcesub,
│   │                    #   requests, admin, common
│   └── services/        # search, tmdb, spell, ai, textutil
├── alembic/             # async env + versions/
├── requirements.txt     # pinned
├── Dockerfile           # python:3.11-slim, uvicorn on $PORT
├── Procfile
├── .env.example
├── set_webhook.py
└── README.md
```

## Notes / limitations

- In-memory search sessions + pending force-sub actions assume **one uvicorn
  worker** (`--workers 1`). For multi-worker, move `app/state.py` to Redis.
- `pg_trgm`/`unaccent` extensions are enabled by migration 0001 (needs a DB
  user with permission to create extensions — Neon/AlwaysData/Supabase allow
  this on their managed Postgres).
- Broadcasts (bot + dashboard) are throttled (~20 msg/s) to respect Telegram
  limits; very large user bases will take a while.
