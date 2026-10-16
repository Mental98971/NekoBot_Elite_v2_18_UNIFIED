"""
Neko Dashboard — a small, separate, READ-ONLY status service.

The original nova_guard_bot's docker-compose.yml referenced a `dashboard`
service built from `dashboard/Dockerfile` that didn't exist anywhere in
the uploaded project — this is that dashboard, actually built.

Deliberately minimal and read-only: it opens its own connection to the
same database the bot uses (via DATABASE_URL) and only ever SELECTs.
It does not import python-telegram-bot, pyrogram, or pytgcalls — it
reuses bot.config and bot.core.database (models + settings only), which
keeps this container's dependencies tiny.

Access control: set DASHBOARD_TOKEN and every page except /health asks for
HTTP Basic credentials (any username, the token as the password). Without a
token the dashboard is open, so docker-compose publishes it on localhost
only by default. The data is read-only (counts, leaderboards, user/chat ids
and moderation reasons — no tokens or secrets) but still personal; put it
behind TLS (a reverse proxy) before exposing it beyond your own machine.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import desc, func, select

from bot.config import settings
from bot.utils.logger import get_logger
from bot.core.database import (
    Ban,
    BotStats,
    ChatMember,
    Mute,
    User,
    Warn,
    async_session,
)

logger = get_logger(__name__)

app = FastAPI(title="Neko Dashboard")

_basic = HTTPBasic(auto_error=False)

if not settings.dashboard_token:
    logger.warning("dashboard_unauthenticated", hint="set DASHBOARD_TOKEN to require a password")


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """No-op unless DASHBOARD_TOKEN is set; then demands it as the Basic-auth password."""
    token = settings.dashboard_token
    if not token:
        return
    supplied = credentials.password if credentials else ""
    # Constant-time comparison on bytes (str compare_digest rejects non-ASCII input).
    if not secrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Neko Dashboard"'},
        )


async def _gather_stats() -> dict:
    async with async_session() as session:
        heartbeat = await session.get(BotStats, 1)

        top_result = await session.execute(
            select(ChatMember, User)
            .join(User, User.id == ChatMember.user_id)
            .order_by(desc(ChatMember.xp))
            .limit(10)
        )
        top_users = [
            {
                "name": u.first_name or u.username or str(u.id),
                "level": m.level or 0,
                "xp": m.xp or 0,
                "chat_id": m.chat_id,
            }
            for m, u in top_result.all()
        ]

        recent_warns = (await session.execute(
            select(Warn).order_by(desc(Warn.id)).limit(10)
        )).scalars().all()
        recent_bans = (await session.execute(
            select(Ban).order_by(desc(Ban.id)).limit(10)
        )).scalars().all()

        total_warns = (await session.execute(select(func.count()).select_from(Warn))).scalar() or 0
        total_bans = (await session.execute(select(func.count()).select_from(Ban))).scalar() or 0
        total_mutes = (await session.execute(select(func.count()).select_from(Mute))).scalar() or 0

    return {
        "heartbeat": {
            "started_at": heartbeat.started_at.isoformat() if heartbeat and heartbeat.started_at else None,
            "last_heartbeat": heartbeat.last_heartbeat.isoformat() if heartbeat and heartbeat.last_heartbeat else None,
            "total_chats": heartbeat.total_chats if heartbeat else 0,
            "total_users": heartbeat.total_users if heartbeat else 0,
            "live_music_active_chats": heartbeat.live_music_active_chats if heartbeat else 0,
        } if heartbeat else None,
        "top_users": top_users,
        "moderation": {
            "total_warns": total_warns,
            "total_bans": total_bans,
            "total_mutes": total_mutes,
            "recent_warns": [{"chat_id": w.chat_id, "user_id": w.user_id, "reason": w.reason} for w in recent_warns],
            "recent_bans": [{"chat_id": b.chat_id, "user_id": b.user_id, "reason": b.reason} for b in recent_bans],
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/stats", dependencies=[Depends(require_auth)])
async def api_stats():
    return JSONResponse(await _gather_stats(), headers={"Cache-Control": "no-store"})


def _bot_is_stale(heartbeat: dict | None) -> bool:
    if not heartbeat or not heartbeat.get("last_heartbeat"):
        return True
    last = datetime.fromisoformat(heartbeat["last_heartbeat"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() > 120


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def index():
    data = await _gather_stats()
    hb = data["heartbeat"]
    stale = _bot_is_stale(hb)
    status_color = "#ef4444" if stale else "#22c55e"
    status_text = "OFFLINE / STALE" if stale else "ONLINE"

    top_rows = "".join(
        f"<tr><td>{i + 1}</td><td>{_esc(u['name'])}</td><td>{u['level']}</td><td>{u['xp']:,}</td></tr>"
        for i, u in enumerate(data["top_users"])
    ) or "<tr><td colspan='4' class='muted'>No activity yet</td></tr>"

    warn_rows = "".join(
        f"<tr><td>{w['chat_id']}</td><td>{w['user_id']}</td><td>{_esc(w['reason'] or '')}</td></tr>"
        for w in data["moderation"]["recent_warns"]
    ) or "<tr><td colspan='3' class='muted'>None</td></tr>"

    ban_rows = "".join(
        f"<tr><td>{b['chat_id']}</td><td>{b['user_id']}</td><td>{_esc(b['reason'] or '')}</td></tr>"
        for b in data["moderation"]["recent_bans"]
    ) or "<tr><td colspan='3' class='muted'>None</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Neko Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2.5rem 1.5rem; background: #0b0e14; color: #e6e8ee;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }}
  .wrap {{ max-width: 960px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; font-weight: 650; margin: 0 0 .25rem; letter-spacing: -0.01em; }}
  .sub {{ color: #8a91a3; font-size: .875rem; margin-bottom: 2rem; }}
  .status {{ display: inline-flex; align-items: center; gap: .4rem; font-size: .8rem; font-weight: 600;
             padding: .3rem .7rem; border-radius: 999px; background: #161a24; margin-bottom: 2rem; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; background: {status_color}; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 2.5rem; }}
  .card {{ background: #12151d; border: 1px solid #1f2430; border-radius: 12px; padding: 1.25rem; }}
  .card .num {{ font-size: 1.8rem; font-weight: 700; }}
  .card .label {{ color: #8a91a3; font-size: .8rem; margin-top: .25rem; }}
  h2 {{ font-size: 1rem; font-weight: 600; margin: 0 0 .75rem; color: #c7cbd6; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 2rem; font-size: .85rem; }}
  th {{ text-align: left; color: #8a91a3; font-weight: 500; padding: .5rem .6rem; border-bottom: 1px solid #1f2430; }}
  td {{ padding: .5rem .6rem; border-bottom: 1px solid #161a24; }}
  .muted {{ color: #565d70; text-align: center; }}
  footer {{ color: #565d70; font-size: .75rem; margin-top: 2rem; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>🛡️ Neko</h1>
  <div class="sub">Read-only status dashboard</div>
  <div class="status"><span class="dot"></span> {status_text}</div>

  <div class="grid">
    <div class="card"><div class="num">{hb['total_chats'] if hb else '—'}</div><div class="label">Chats</div></div>
    <div class="card"><div class="num">{hb['total_users'] if hb else '—'}</div><div class="label">Known users</div></div>
    <div class="card"><div class="num">{hb['live_music_active_chats'] if hb else '—'}</div><div class="label">Active voice chats</div></div>
    <div class="card"><div class="num">{data['moderation']['total_warns']}</div><div class="label">Total warns</div></div>
    <div class="card"><div class="num">{data['moderation']['total_bans']}</div><div class="label">Total bans</div></div>
    <div class="card"><div class="num">{data['moderation']['total_mutes']}</div><div class="label">Total mutes</div></div>
  </div>

  <h2>🏆 Top XP (across all chats)</h2>
  <table><tr><th>#</th><th>User</th><th>Level</th><th>XP</th></tr>{top_rows}</table>

  <h2>⚠️ Recent warns</h2>
  <table><tr><th>Chat</th><th>User</th><th>Reason</th></tr>{warn_rows}</table>

  <h2>🔨 Recent bans</h2>
  <table><tr><th>Chat</th><th>User</th><th>Reason</th></tr>{ban_rows}</table>

  <footer>Last heartbeat: {hb['last_heartbeat'] if hb else 'never'} UTC · Auto-refreshes every 30s</footer>
</div>
<script>setTimeout(() => location.reload(), 30000);</script>
</body>
</html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def _esc(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
