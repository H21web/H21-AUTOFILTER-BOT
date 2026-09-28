"""Stream / download web app (opens inside Telegram as a WebApp).

Signed, expiring links (HMAC, 6h default TTL):

    /watch/{token}       -> full web app: in-browser player, external-player
                           buttons (MX Player / VLC), download with progress,
                           related-movie suggestions.
    /api/related/{token} -> JSON for the related-movies rail.
    /dl/{token}          -> proxies the file from Telegram's servers with HTTP
                           Range support (GET + HEAD), so seeking works.

NOTE: /dl streams bytes through this server, so it consumes *server*
bandwidth (AlwaysData free = 10 GB/month). File *delivery inside Telegram*
(via file_id) uses zero server bandwidth — the player is the only part
that does. Toggle with ENABLE_STREAM_PLAYER / the dashboard setting.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy import func, select

from app.config import settings
from app.db import get_session_factory
from app.handlers.common import stream_enabled
from app.models import File
from app.services import tmdb
from app.services.textutil import clean_title, extract_year

log = logging.getLogger(__name__)

router = APIRouter()

_client: httpx.AsyncClient | None = None

# Containers a phone/desktop browser can actually play natively.
_VIDEO_EXTS = {".mp4", ".m4v", ".webm", ".mov"}
_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac"}


def _secret_key() -> bytes:
    return (settings.WEBHOOK_SECRET or settings.BOT_TOKEN).encode()


def public_base() -> str:
    """Public base URL derived from WEBHOOK_URL (https://host)."""
    url = settings.WEBHOOK_URL.rstrip("/")
    if url.endswith("/webhook"):
        url = url[: -len("/webhook")]
    return url.rstrip("/") or url


def sign_file_token(file_db_id: int, hours: int | None = None) -> str:
    """Create a signed token: b64(payload).expiry.sig."""
    exp = int(time.time()) + int(hours if hours is not None
                                 else settings.STREAM_LINK_TTL_HOURS) * 3600
    payload = f"{file_db_id}.{exp}".encode()
    b64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(_secret_key(), f"{b64}.{exp}".encode(),
                   hashlib.sha256).hexdigest()[:32]
    return f"{b64}.{exp}.{sig}"


def verify_file_token(token: str) -> int | None:
    """Return the file DB id if the token is valid and unexpired."""
    try:
        b64, exp_s, sig = token.split(".")
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < time.time():
        return None
    want = hmac.new(_secret_key(), f"{b64}.{exp}".encode(),
                    hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(want, sig):
        return None
    try:
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        file_db_id_s, exp2_s = payload.decode().split(".")
        if int(exp2_s) != exp:
            return None
        return int(file_db_id_s)
    except (ValueError, UnicodeDecodeError):
        return None


def stream_links_for(file_db_id: int) -> tuple[str, str]:
    """(watch_url, download_url) for a file DB id."""
    token = sign_file_token(file_db_id)
    base = public_base()
    return f"{base}/watch/{token}", f"{base}/dl/{token}"


async def _get_file_row(file_db_id: int) -> File | None:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        return await session.get(File, file_db_id)


def _safe_filename(name: str | None) -> str:
    cleaned = "".join(c for c in (name or "file") if c.isalnum() or c in "._- ")
    return (cleaned.strip() or "file")[:80]


def _http_client() -> httpx.AsyncClient:
    """Shared proxy client.

    No total/read/write timeout: a movie stream legitimately trickles for
    tens of minutes, and a total timeout (the old 120s one) killed playback
    and downloads mid-stream. Only the connect is bounded.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=20.0, read=None, write=None,
                                  pool=20.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=50),
        )
    return _client


async def _resolve_dl(token: str, request: Request):
    """Validate token -> (row, telegram file url, safe filename)."""
    if not await stream_enabled():
        raise HTTPException(status_code=404, detail="Stream player disabled")
    file_db_id = verify_file_token(token)
    if file_db_id is None:
        raise HTTPException(status_code=403, detail="Invalid or expired link")
    row = await _get_file_row(file_db_id)
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    bot = request.app.state.ptb_app.bot
    try:
        tg_file = await bot.get_file(row.file_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("getFile failed: %s", exc)
        raise HTTPException(status_code=502, detail="Telegram file unavailable")
    file_url = (f"https://api.telegram.org/file/bot{settings.BOT_TOKEN}/"
                f"{tg_file.file_path}")
    return row, file_url, _safe_filename(row.file_name)


def _proxy_headers(row: File, safe: str, upstream_headers) -> dict:
    media = row.mime_type or "application/octet-stream"
    out = {
        "Content-Type": media,
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"inline; filename=\"{safe}\"",
        "Cache-Control": "private, max-age=3600",
    }
    for h in ("Content-Range", "Content-Length", "ETag", "Last-Modified"):
        if h in upstream_headers:
            out[h] = upstream_headers[h]
    return out


@router.get("/dl/{token}")
async def download_file(token: str, request: Request):
    row, file_url, safe = await _resolve_dl(token, request)
    range_header = request.headers.get("range")
    headers = {"Range": range_header} if range_header else {}
    client = _http_client()
    try:
        upstream = await client.send(
            client.build_request("GET", file_url, headers=headers), stream=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("upstream fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="Upstream fetch failed")

    if upstream.status_code == 416:
        await upstream.aclose()
        raise HTTPException(status_code=416, detail="Range not satisfiable")
    if upstream.status_code not in (200, 206):
        await upstream.aclose()
        log.warning("upstream status %s for %s", upstream.status_code, safe)
        raise HTTPException(status_code=502, detail="Telegram file unavailable")

    media = row.mime_type or "application/octet-stream"

    async def gen():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(gen(), status_code=upstream.status_code,
                             headers=_proxy_headers(row, safe, upstream.headers),
                             media_type=media)


@router.head("/dl/{token}")
async def download_head(token: str, request: Request):
    """Header probe for external players (MX/VLC) — no body."""
    row, file_url, safe = await _resolve_dl(token, request)
    range_header = request.headers.get("range")
    headers = {"Range": range_header} if range_header else {}
    client = _http_client()
    try:
        upstream = await client.send(
            client.build_request("HEAD", file_url, headers=headers))
    except Exception as exc:  # noqa: BLE001
        log.warning("upstream HEAD failed: %s", exc)
        raise HTTPException(status_code=502, detail="Upstream fetch failed")
    if upstream.status_code not in (200, 206):
        raise HTTPException(status_code=502, detail="Telegram file unavailable")
    return Response(status_code=upstream.status_code,
                    headers=_proxy_headers(row, safe, upstream.headers))


@router.get("/api/related/{token}")
async def api_related(token: str):
    """Related movies for the watch page rail (JSON)."""
    if not await stream_enabled():
        raise HTTPException(status_code=404, detail="Stream player disabled")
    file_db_id = verify_file_token(token)
    if file_db_id is None:
        raise HTTPException(status_code=403, detail="Invalid or expired link")
    row = await _get_file_row(file_db_id)
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    tk = (row.title_key or "").strip()
    if not tk:
        return []

    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        sim = func.similarity(File.title_key, tk)
        stmt = (select(File.id, File.file_name, File.file_size, File.quality,
                       File.language, File.title_key, sim.label("sim"))
                .where(File.id != file_db_id,
                       File.title_key.isnot(None), File.title_key != "")
                .order_by(sim.desc()).limit(80))
        rows = (await session.execute(stmt)).all()

    # one entry per title (biggest file), same-title group first
    seen: dict[str, object] = {}
    for r in rows:
        cur = seen.get(r.title_key)
        if cur is None or (r.file_size or 0) > (cur.file_size or 0):
            seen[r.title_key] = r

    def rank(item) -> tuple:
        key, r = item
        return (0 if key == tk else 1, -(r.sim or 0))

    out = []
    for _key, r in sorted(seen.items(), key=rank)[:12]:
        title = clean_title(r.file_name) or "Untitled"
        year = extract_year(r.file_name)
        meta = await tmdb.get_movie(title, year)
        out.append({
            "title": title,
            "year": (meta or {}).get("year") or year,
            "quality": r.quality,
            "poster": (meta or {}).get("poster_url"),
            "watch_url": f"/watch/{sign_file_token(r.id)}",
        })
    return out


_WATCH_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#0f1115">
<title>__PAGE_TITLE__</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#0f1115;--card:#181b23;--txt:#fff;--mut:#9aa0ab;--acc:#e50914;--blue:#2f80ed}
*{margin:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--txt);min-height:100vh}
.hero{position:relative;padding:26px 18px 16px;overflow:hidden}
.hero::before{content:"";position:absolute;inset:0;background:__BACKDROP__;background-size:cover;background-position:center;filter:blur(26px) brightness(.42);transform:scale(1.25)}
.hero-in{position:relative;display:flex;gap:14px;align-items:center;max-width:860px;margin:0 auto}
.poster{width:92px;height:134px;object-fit:cover;border-radius:10px;background:#222;box-shadow:0 6px 24px rgba(0,0,0,.55)}
h1{font-size:1.12rem;line-height:1.32}
.meta{color:var(--mut);font-size:.8rem;margin-top:6px;line-height:1.55}
.badges{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.badge{font-size:.68rem;font-weight:600;background:rgba(255,255,255,.13);padding:3px 10px;border-radius:20px;letter-spacing:.02em}
.wrap{padding:4px 14px 44px;max-width:860px;margin:0 auto}
.card{background:var(--card);border-radius:14px;padding:14px;margin-top:14px}
.card h2{font-size:.92rem;margin-bottom:10px}
video,audio{width:100%;border-radius:10px;background:#000;max-height:54vh}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:13px;border:none;border-radius:12px;font-size:.93rem;font-weight:700;color:#fff;background:var(--blue);cursor:pointer;margin-top:10px;text-decoration:none;font-family:inherit}
.btn.red{background:var(--acc)}
.btn.ghost{background:rgba(255,255,255,.1)}
.btn.aplayer{display:none}
.btnrow{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.btnrow .btn{margin-top:0}
.notice{background:rgba(255,193,7,.1);border:1px solid rgba(255,193,7,.35);color:#ffd97a;padding:11px 12px;border-radius:10px;font-size:.82rem;line-height:1.55}
.err{background:rgba(229,9,20,.13);border:1px solid rgba(229,9,20,.4);color:#ff9a9a;padding:11px 12px;border-radius:10px;font-size:.82rem;line-height:1.5;display:none;margin-top:10px}
.prog{height:10px;background:rgba(255,255,255,.12);border-radius:6px;overflow:hidden;margin-top:12px;display:none}
.prog>div{height:100%;width:0;background:linear-gradient(90deg,var(--blue),#7ab8ff);transition:width .2s}
.dlstat{font-size:.79rem;color:var(--mut);margin-top:7px;display:none}
.rail{display:flex;gap:10px;overflow-x:auto;padding:4px 2px 10px;scrollbar-width:thin}
.rcard{flex:0 0 104px;text-decoration:none;color:var(--txt)}
.rcard img{width:104px;height:150px;object-fit:cover;border-radius:10px;background:#222;display:block}
.noposter{width:104px;height:150px;border-radius:10px;background:#222;display:flex;align-items:center;justify-content:center;font-size:2rem}
.rcard .t{font-size:.7rem;margin-top:5px;line-height:1.3;height:2.7em;overflow:hidden}
.rcard .q{font-size:.66rem;color:var(--mut);margin-top:2px}
.foot{text-align:center;color:var(--mut);font-size:.7rem;margin:28px 0 6px;line-height:1.7}
.hint{font-size:.76rem;color:var(--mut);margin-top:10px;line-height:1.6}
</style></head><body>
<div class="hero"><div class="hero-in">
__POSTER_IMG__
<div><h1>__TITLE__</h1><div class="meta">__META__</div><div class="badges">__BADGES__</div></div>
</div></div>
<div class="wrap">

<div class="card"><h2>▶️ Player</h2>
__PLAYER__
<div class="err" id="perr"></div>
</div>

<div class="card"><h2>📺 Open in external player</h2>
<div class="btnrow">
<button class="btn aplayer" id="bmx">MX Player</button>
<button class="btn aplayer" id="bmxp">MX Player Pro</button>
</div>
<div class="btnrow" style="margin-top:10px">
<button class="btn aplayer" id="bvlc">VLC</button>
<button class="btn ghost" id="bcopy">🔗 Copy stream link</button>
</div>
<div class="hint" id="extHint">Tip: MX Player / VLC play <b>every</b> format (MKV, AVI…). On iPhone/PC, copy the link → VLC → Open Network Stream.</div>
</div>

<div class="card"><h2>⬇️ Download</h2>
<button class="btn red" id="bdl">⬇️ Download with progress</button>
<div class="prog" id="prog"><div id="pfill"></div></div>
<div class="dlstat" id="dstat"></div>
<div class="err" id="derr"></div>
</div>

<div class="card"><h2>🍿 You may also like</h2>
<div class="rail" id="rail"><div class="meta">Loading…</div></div>
</div>

<div class="foot">🔒 Links expire after __TTL__ hours<br>Made for Moovidex 🎬</div>
</div>
<script>
(function(){
"use strict";
var tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
if (tg) {
  tg.ready(); tg.expand();
  try {
    tg.setHeaderColor('#0f1115'); tg.setBackgroundColor('#0f1115');
    if (tg.themeParams && tg.themeParams.bg_color) {
      document.documentElement.style.setProperty('--bg', tg.themeParams.bg_color);
    }
    if (tg.BackButton) { tg.BackButton.show(); tg.BackButton.onClick(function(){ tg.close(); }); }
  } catch(e){}
}
var DL_URL = "__DL_URL__";
var FILE_NAME = __FILE_NAME_JSON__;
var TOKEN = "__TOKEN__";
var IS_ANDROID = /Android/i.test(navigator.userAgent || '');

function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function toast(msg){
  if (tg && tg.showPopup) { tg.showPopup({message: msg}); }
  else { alert(msg); }
}

/* in-browser player error -> friendly message */
var v = document.getElementById('v');
if (v) {
  v.addEventListener('error', function(){
    var e = document.getElementById('perr');
    e.style.display = 'block';
    e.textContent = '⚠️ Playback failed — the file may be unavailable or expired. Try an external player below, or Download.';
  });
}

/* external players (Android intent URLs) */
if (IS_ANDROID) {
  var btns = document.querySelectorAll('.aplayer');
  for (var i=0;i<btns.length;i++) btns[i].style.display = 'flex';
  var intentFor = function(pkg){
    var u = new URL(DL_URL, window.location.origin);
    return 'intent://' + u.host + u.pathname +
      '#Intent;scheme=' + u.protocol.replace(':','') +
      ';package=' + pkg + ';S.title=' + encodeURIComponent(FILE_NAME) + ';end';
  };
  document.getElementById('bmx').onclick = function(){ window.location.href = intentFor('com.mxtech.videoplayer.ad'); };
  document.getElementById('bmxp').onclick = function(){ window.location.href = intentFor('com.mxtech.videoplayer.pro'); };
  document.getElementById('bvlc').onclick = function(){ window.location.href = intentFor('org.videolan.vlc'); };
  document.getElementById('extHint').innerHTML =
    'Tap a player to open this file directly — MX Player / VLC play <b>every</b> format (MKV, AVI…).';
}
document.getElementById('bcopy').onclick = function(){
  var full = new URL(DL_URL, window.location.origin).href;
  var done = function(){ toast('🔗 Stream link copied'); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(full).then(done, function(){ prompt('Copy link:', full); });
  } else { prompt('Copy link:', full); }
};

/* download with live progress */
var bdl = document.getElementById('bdl');
var prog = document.getElementById('prog');
var pfill = document.getElementById('pfill');
var dstat = document.getElementById('dstat');
var derr = document.getElementById('derr');
var busy = false;
function fmtMB(n){ return (n/1048576).toFixed(1) + ' MB'; }
bdl.addEventListener('click', function(){
  if (busy) return;
  busy = true;
  var ok = false;
  derr.style.display = 'none';
  prog.style.display = 'block'; dstat.style.display = 'block';
  pfill.style.width = '0';
  bdl.textContent = '⏳ Downloading…';
  fetch(DL_URL).then(function(res){
    if (!res.ok) throw new Error('server replied ' + res.status);
    var total = parseInt(res.headers.get('Content-Length') || '0', 10) || 0;
    var reader = res.body.getReader();
    var loaded = 0, writable = null, chunks = [];
    var getWriter = function(){
      if (!window.showSaveFilePicker) return Promise.resolve(null);
      return window.showSaveFilePicker({ suggestedName: FILE_NAME })
        .then(function(h){ return h.createWritable(); })
        .then(function(w){ writable = w; return w; },
              function(e){ if (e && e.name === 'AbortError') throw {cancelled:true}; return null; });
    };
    return getWriter().then(function(){
      function pump(){
        return reader.read().then(function(r){
          if (r.done) return;
          var writeP = writable ? writable.write(r.value) : Promise.resolve(chunks.push(r.value));
          return Promise.resolve(writeP).then(function(){
            loaded += r.value.length;
            if (total) {
              var p = Math.min(100, loaded/total*100);
              pfill.style.width = p + '%';
              dstat.textContent = p.toFixed(1) + '% • ' + fmtMB(loaded) + ' / ' + fmtMB(total);
            } else {
              dstat.textContent = fmtMB(loaded) + ' downloaded…';
            }
            return pump();
          });
        });
      }
      return pump().then(function(){
        if (writable) return writable.close().then(function(){ return {saved:true}; });
        var blob = new Blob(chunks, {type:'application/octet-stream'});
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = FILE_NAME;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function(){ URL.revokeObjectURL(a.href); }, 120000);
        return {saved:true};
      });
    }).then(function(){ ok = true; });
  }).then(function(){
    if (ok) {
      pfill.style.width = '100%';
      dstat.textContent = '✅ Download complete';
      bdl.textContent = '✅ Downloaded';
      if (tg && tg.HapticFeedback) { try{ tg.HapticFeedback.notificationOccurred('success'); }catch(e){} }
    }
  }).catch(function(err){
    if (err && err.cancelled) { /* picker dismissed */ }
    else {
      derr.style.display = 'block';
      derr.textContent = '⚠️ Download failed (' + (err && err.message || err) + '). Opening direct link…';
      setTimeout(function(){ window.location.href = DL_URL; }, 1400);
    }
  }).then(function(){
    busy = false;
    if (!ok) { bdl.textContent = '⬇️ Download with progress'; }
  });
});

/* related movies rail */
fetch('/api/related/' + TOKEN).then(function(r){ return r.json(); }).then(function(items){
  var rail = document.getElementById('rail');
  if (!items || !items.length) { rail.innerHTML = '<div class="meta">No suggestions yet.</div>'; return; }
  var h = '';
  for (var i=0;i<items.length;i++){
    var it = items[i];
    var img = it.poster
      ? '<img loading="lazy" src="' + esc(it.poster) + '" onerror="this.style.display=\'none\'">'
      : '<div class="noposter">🎬</div>';
    h += '<a class="rcard" href="' + esc(it.watch_url) + '">' + img +
         '<div class="t">' + esc(it.title) + '</div>' +
         '<div class="q">' + esc(it.year || '') + (it.quality ? ' • ' + esc(it.quality) : '') + '</div></a>';
  }
  rail.innerHTML = h;
}).catch(function(){
  document.getElementById('rail').innerHTML = '<div class="meta">Couldn\u2019t load suggestions.</div>';
});
})();
</script>
</body></html>"""


def _human_size(num: int | None) -> str:
    if not num:
        return "?"
    n = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


@router.get("/watch/{token}", response_class=HTMLResponse)
async def watch_page(token: str):
    if not await stream_enabled():
        raise HTTPException(status_code=404, detail="Stream player disabled")
    file_db_id = verify_file_token(token)
    if file_db_id is None:
        raise HTTPException(status_code=403, detail="Invalid or expired link")
    row = await _get_file_row(file_db_id)
    if not row:
        raise HTTPException(status_code=404, detail="File not found")

    title = clean_title(row.file_name) or "Watch"
    year = extract_year(row.file_name)
    meta = await tmdb.get_movie(title, year)
    poster = (meta or {}).get("poster_url")
    backdrop = (f"url('{html.escape(poster)}')" if poster
                else "linear-gradient(135deg,#1a1a2e,#16213e)")
    poster_img = (f'<img class="poster" src="{html.escape(poster)}" '
                  f'onerror="this.style.display=\'none\'">' if poster else "")

    ext = (os.path.splitext(row.file_name or "")[1] or "").lower()
    mime = (row.mime_type or "").lower()
    is_video = ext in _VIDEO_EXTS or (not ext and mime.startswith("video"))
    is_audio = ext in _AUDIO_EXTS or (not ext and mime.startswith("audio"))
    dl_url = f"/dl/{token}"

    if is_video:
        player_html = (f'<video id="v" controls playsinline preload="metadata" '
                       f'src="{dl_url}"></video>')
    elif is_audio:
        player_html = (f'<audio id="v" controls preload="metadata" '
                       f'src="{dl_url}"></audio>')
    else:
        player_html = (
            f'<div class="notice">⚠️ <b>{html.escape(ext or "this file")}</b> '
            "can't play inside a browser — no web player supports it. "
            "Use <b>MX Player / VLC</b> below (they play everything), "
            "or download the file.</div>")

    meta_bits = [str(year) if year else None,
                 _human_size(row.file_size),
                 ext.upper().lstrip(".") if ext else None]
    meta_line = " • ".join(b for b in meta_bits if b)
    badges = "".join(
        f'<span class="badge">{html.escape(b)}</span>'
        for b in (row.quality, row.language) if b)

    page = _WATCH_TEMPLATE
    page = page.replace("__PAGE_TITLE__", html.escape(title))
    page = page.replace("__TITLE__", html.escape(title))
    page = page.replace("__META__", html.escape(meta_line))
    page = page.replace("__BADGES__", badges)
    page = page.replace("__BACKDROP__", backdrop)
    page = page.replace("__POSTER_IMG__", poster_img)
    page = page.replace("__PLAYER__", player_html)
    page = page.replace("__DL_URL__", dl_url)
    page = page.replace("__FILE_NAME_JSON__", json.dumps(row.file_name or "file"))
    page = page.replace("__TOKEN__", token)
    page = page.replace("__TTL__", str(settings.STREAM_LINK_TTL_HOURS))
    return page
