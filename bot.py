"""
AURA Music Player — Telegram Bot v2
────────────────────────────────────
Команды:
  /start          — Меню
  /status         — Статус плеера и статистика
  /tracks         — Список треков
  /delete <id>    — Удалить трек
  /block          — Заблокировать плеер
  /unblock        — Разблокировать плеер
  /download <url> — Скачать трек с YouTube/SoundCloud → загрузить в базу

Webhook:
  POST /webhook/track-added  — Supabase шлёт при добавлении трека
  POST /webhook/telegram     — Telegram updates
"""

import os
import re
import asyncio
import logging
import tempfile
import json
import time
from pathlib import Path
from datetime import datetime

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes
)
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# ──────────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID  = int(os.getenv("ADMIN_CHAT_ID", "0"))
SB_URL         = os.getenv("SB_URL", "https://jzrepyzzeocepgvqdlwa.supabase.co")
SB_KEY         = os.getenv("SB_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imp6cmVweXp6ZW9jZXBndnFkbHdhIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzIxODU1ODQsImV4cCI6MjA4Nzc2MTU4NH0.Qdm7baXlJ22mkfjpzZIKJZuP_SJt4s0PZ4R6bLEviWQ")
SB_BUCKET      = os.getenv("SB_BUCKET", "tracks")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "aura_secret_2024")
PORT           = int(os.getenv("PORT", "8000"))
PUBLIC_URL     = os.getenv("PUBLIC_URL", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("aura-bot")

# ──────────────────────────────────────────────────────────
#  SUPABASE HELPERS
# ──────────────────────────────────────────────────────────
SB_HEADERS = {
    "apikey": SB_KEY,
    "Authorization": f"Bearer {SB_KEY}",
    "Content-Type": "application/json",
}

async def sb_get(path: str, params: dict = None):
    url = f"{SB_URL}/rest/v1/{path}"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url, headers=SB_HEADERS, params=params or {})
        r.raise_for_status()
        return r.json()

async def sb_post(path: str, body: dict):
    url = f"{SB_URL}/rest/v1/{path}"
    h = {**SB_HEADERS, "Prefer": "return=representation"}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers=h, json=body)
        if not r.is_success:
            raise Exception(f"Supabase {r.status_code}: {r.text}")
        data = r.json()
        return data[0] if isinstance(data, list) and data else data

async def sb_delete_row(path: str):
    url = f"{SB_URL}/rest/v1/{path}"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.delete(url, headers=SB_HEADERS)
        r.raise_for_status()

async def sb_upsert_settings(body: dict):
    url = f"{SB_URL}/rest/v1/settings"
    h = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, headers=h, json=body)
        if not r.is_success:
            raise Exception(f"Settings update failed {r.status_code}: {r.text}")

async def sb_upload_file(path: str, data: bytes, content_type: str) -> str:
    url = f"{SB_URL}/storage/v1/object/{SB_BUCKET}/{path}"
    headers = {
        "apikey": SB_KEY,
        "Authorization": f"Bearer {SB_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(url, headers=headers, content=data)
        if not r.is_success:
            raise Exception(f"Storage upload failed {r.status_code}: {r.text}")
    return f"{SB_URL}/storage/v1/object/public/{SB_BUCKET}/{path}"

async def sb_delete_file(path: str):
    url = f"{SB_URL}/storage/v1/object/{SB_BUCKET}/{path}"
    async with httpx.AsyncClient(timeout=15) as c:
        await c.delete(url, headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"})

async def get_site_config() -> dict:
    try:
        rows = await sb_get("settings", {"id": "eq.1"})
        return rows[0] if rows else {}
    except Exception:
        return {}

def fmt_dur(seconds) -> str:
    s = int(float(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"

def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_CHAT_ID

# ──────────────────────────────────────────────────────────
#  КОМАНДЫ
# ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    text = (
        "🎵 *AURA Bot* — панель управления\n\n"
        "/status — статус и статистика плеера\n"
        "/tracks — список всех треков\n"
        "/download `<url>` — скачать с YouTube/SoundCloud\n"
        "/block — заблокировать плеер\n"
        "/unblock — разблокировать плеер\n"
        "/delete `<id>` — удалить трек по ID\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        tracks = await sb_get("tracks", {"select": "id,play_count"})
        cfg = await get_site_config()
        total = sum(int(t.get("play_count") or 0) for t in tracks)
        blocked = cfg.get("blocked", False)
        icon = "🔴 Заблокирован" if blocked else "🟢 Открыт"
        text = (
            f"📊 *Статус AURA*\n\n"
            f"🌐 Плеер: {icon}\n"
            f"🎵 Треков в базе: *{len(tracks)}*\n"
            f"▶️ Всего прослушиваний: *{total}*\n"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🟢 Разблокировать" if blocked else "🔴 Заблокировать",
                callback_data="toggle_block"
            ),
            InlineKeyboardButton("🔄 Обновить", callback_data="refresh_status"),
        ]])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_tracks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        tracks = await sb_get("tracks", {
            "select": "id,title,artist,duration,play_count",
            "order": "created_at.desc",
            "limit": "50"
        })
        if not tracks:
            await update.message.reply_text("📭 Треков нет.")
            return
        lines = ["🎵 *Треки* (последние 50):\n"]
        for t in tracks:
            dur = fmt_dur(t.get("duration", 0))
            plays = t.get("play_count") or 0
            title = t.get("title", "?")
            artist = t.get("artist", "?")
            lines.append(f"`{t['id']:>4}` | {dur} | ▶{plays} | {title} — {artist}")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n…"
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        await sb_upsert_settings({"id": 1, "blocked": True})
        await update.message.reply_text("🔴 Плеер *заблокирован*.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        await sb_upsert_settings({"id": 1, "blocked": False})
        await update.message.reply_text("🟢 Плеер *разблокирован*.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /delete <id>")
        return
    track_id = ctx.args[0]
    try:
        rows = await sb_get("tracks", {"id": f"eq.{track_id}"})
        if not rows:
            await update.message.reply_text(f"❌ Трек #{track_id} не найден.")
            return
        tr = rows[0]
        for field in ("audio_url", "art_url"):
            url = tr.get(field) or ""
            if url and f"/public/{SB_BUCKET}/" in url:
                path = url.split(f"/public/{SB_BUCKET}/")[1]
                try:
                    await sb_delete_file(path)
                except Exception:
                    pass
        await sb_delete_row(f"playlist_tracks?track_id=eq.{track_id}")
        await sb_delete_row(f"tracks?id=eq.{track_id}")
        await update.message.reply_text(
            f"✅ Трек *{tr.get('title', '?')}* удалён.", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ──────────────────────────────────────────────────────────
#  СКАЧИВАНИЕ ТРЕКА
# ──────────────────────────────────────────────────────────
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text(
            "Использование: /download <url>\n\n"
            "Примеры:\n"
            "`/download https://youtu.be/dQw4w9WgXcQ`\n"
            "`/download https://soundcloud.com/artist/track`",
            parse_mode="Markdown"
        )
        return

    url = ctx.args[0]
    if not url.startswith("http"):
        await update.message.reply_text("❌ Некорректная ссылка.")
        return

    try:
        import yt_dlp
    except ImportError:
        await update.message.reply_text("❌ yt-dlp не установлен.")
        return

    msg = await update.message.reply_text("⏳ Получаю информацию о треке...")

    loop = asyncio.get_event_loop()

    with tempfile.TemporaryDirectory() as tmpdir:

        # Сначала только получаем метаданные (без скачивания)
        meta_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False,
        }
        try:
            info = await loop.run_in_executor(None, lambda: _ydl_info(url, meta_opts))
        except Exception as e:
            await msg.edit_text(f"❌ Не удалось получить информацию: {e}")
            return

        title    = info.get("title") or "Unknown"
        artist   = (info.get("artist") or info.get("uploader") or
                    info.get("creator") or info.get("channel") or "Unknown")
        album    = info.get("album") or info.get("playlist") or ""
        duration = float(info.get("duration") or 0)
        thumb_url = info.get("thumbnail") or ""

        await msg.edit_text(
            f"⏳ Скачиваю: *{title}*\n👤 {artist}",
            parse_mode="Markdown"
        )

        # Скачиваем только аудио в mp3
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": f"{tmpdir}/%(title)s.%(ext)s",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            # Важно: только аудио форматы
            "match_filter": None,
            "quiet": True,
            "no_warnings": True,
            # Обходим блокировки YouTube
            "extractor_args": {
                "youtube": {"player_client": ["web_creator", "tv", "ios"]}
            },
            # Не скачиваем thumbnail отдельно — сделаем сами через httpx
            "writethumbnail": False,
        }

        try:
            await loop.run_in_executor(None, lambda: _ydl_download(url, ydl_opts))
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка скачивания: {e}")
            return

        # Найти mp3
        mp3_files = list(Path(tmpdir).glob("*.mp3"))
        if not mp3_files:
            # Попробуем любой аудиофайл
            audio_exts = ["*.m4a", "*.ogg", "*.opus", "*.flac", "*.wav", "*.webm"]
            for ext in audio_exts:
                found = list(Path(tmpdir).glob(ext))
                if found:
                    mp3_files = found
                    break

        if not mp3_files:
            await msg.edit_text("❌ Аудиофайл не найден после скачивания.")
            return

        audio_path = mp3_files[0]
        audio_ext = audio_path.suffix.lstrip(".")

        await msg.edit_text(
            f"⏳ Загружаю в базу...\n🎵 *{title}*",
            parse_mode="Markdown"
        )

        # Генерируем уникальное имя
        safe_name = re.sub(r"[^\w\-]", "_", title)[:50]
        ts = int(time.time())
        audio_key = f"audio/{ts}_{safe_name}.{audio_ext}"

        # Загружаем аудио в Supabase Storage
        content_type_map = {
            "mp3": "audio/mpeg", "m4a": "audio/mp4", "ogg": "audio/ogg",
            "opus": "audio/opus", "flac": "audio/flac", "wav": "audio/wav",
            "webm": "audio/webm",
        }
        ct = content_type_map.get(audio_ext, "audio/mpeg")
        try:
            audio_url = await sb_upload_file(audio_key, audio_path.read_bytes(), ct)
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка загрузки аудио в хранилище: {e}")
            return

        # Загружаем обложку
        art_url = None
        if thumb_url:
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.get(thumb_url, follow_redirects=True)
                if r.status_code == 200:
                    art_key = f"art/{ts}_{safe_name}.jpg"
                    art_url = await sb_upload_file(art_key, r.content, "image/jpeg")
            except Exception:
                pass  # обложка необязательна

        # Записываем в БД
        try:
            row = await sb_post("tracks", {
                "title":     str(title)[:200],
                "artist":    str(artist)[:200],
                "audio_url": audio_url,
                "art_url":   art_url,
                "favorite":  False,
                "duration":  round(duration, 2),
                "play_count": 0,
            })
            track_id = row.get("id", "?") if isinstance(row, dict) else "?"
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка записи в базу: {e}")
            return

    # Итоговое сообщение
    art_status = "✅" if art_url else "⚠️ нет"
    text = (
        f"✅ *Трек добавлен!*\n\n"
        f"🎵 *{title}*\n"
        f"👤 {artist}\n"
    )
    if album:
        text += f"💿 {album}\n"
    text += (
        f"⏱ {fmt_dur(duration)}\n"
        f"🖼 Обложка: {art_status}\n"
        f"🆔 ID: `{track_id}`"
    )
    await msg.edit_text(text, parse_mode="Markdown")


def _ydl_info(url: str, opts: dict) -> dict:
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _ydl_download(url: str, opts: dict) -> dict:
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True) or {}


# ──────────────────────────────────────────────────────────
#  CALLBACK КНОПКИ
# ──────────────────────────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update):
        return

    if q.data == "toggle_block":
        cfg = await get_site_config()
        new_blocked = not cfg.get("blocked", False)
        await sb_upsert_settings({"id": 1, "blocked": new_blocked})
        icon = "🔴 Заблокирован" if new_blocked else "🟢 Открыт"
        await q.edit_message_text(
            f"🌐 Плеер: *{icon}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🟢 Разблокировать" if new_blocked else "🔴 Заблокировать",
                    callback_data="toggle_block"
                ),
                InlineKeyboardButton("🔄 Обновить", callback_data="refresh_status"),
            ]])
        )

    elif q.data == "refresh_status":
        tracks = await sb_get("tracks", {"select": "id,play_count"})
        cfg = await get_site_config()
        total = sum(int(t.get("play_count") or 0) for t in tracks)
        blocked = cfg.get("blocked", False)
        await q.edit_message_text(
            f"📊 *Статус AURA*\n\n"
            f"🌐 Плеер: {'🔴 Заблокирован' if blocked else '🟢 Открыт'}\n"
            f"🎵 Треков: *{len(tracks)}*\n"
            f"▶️ Прослушиваний: *{total}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🟢 Разблокировать" if blocked else "🔴 Заблокировать",
                    callback_data="toggle_block"
                ),
                InlineKeyboardButton("🔄 Обновить", callback_data="refresh_status"),
            ]])
        )


# ──────────────────────────────────────────────────────────
#  FASTAPI
# ──────────────────────────────────────────────────────────
fastapi_app = FastAPI()
tg_app: Application = None


@fastapi_app.post("/webhook/track-added")
async def on_track_added(request: Request):
    """Supabase триггер шлёт сюда POST при INSERT в tracks."""
    secret = request.headers.get("x-webhook-secret", "")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Supabase Realtime Webhook шлёт { type, table, record, old_record }
    record = body.get("record") or body  # fallback — если шлём напрямую
    title  = record.get("title", "?")
    artist = record.get("artist", "?")
    dur    = fmt_dur(record.get("duration", 0))
    ev_type = body.get("type", "INSERT")

    if ev_type == "DELETE":
        old = body.get("old_record") or {}
        text = (
            f"🗑 *Трек удалён*\n\n"
            f"*{old.get('title','?')}* — {old.get('artist','?')}"
        )
    elif ev_type == "UPDATE":
        text = (
            f"✏️ *Трек обновлён*\n\n"
            f"*{title}* — {artist}"
        )
    else:
        text = (
            f"🎵 *Новый трек добавлен!*\n\n"
            f"🎤 *{artist}*\n"
            f"🎼 {title}\n"
            f"⏱ {dur}"
        )

    if tg_app and ADMIN_CHAT_ID:
        try:
            await tg_app.bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=text, parse_mode="Markdown"
            )
        except Exception as e:
            log.error(f"Telegram notify error: {e}")

    return JSONResponse({"ok": True})


@fastapi_app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    body = await request.body()
    data = json.loads(body)
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return JSONResponse({"ok": True})


@fastapi_app.get("/")
async def health():
    return {"status": "ok", "service": "AURA Bot v2"}


# ──────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────
async def main():
    global tg_app

    if not BOT_TOKEN:
        log.error("BOT_TOKEN не задан!")
        return

    tg_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .updater(None)
        .build()
    )

    tg_app.add_handler(CommandHandler("start",    cmd_start))
    tg_app.add_handler(CommandHandler("status",   cmd_status))
    tg_app.add_handler(CommandHandler("tracks",   cmd_tracks))
    tg_app.add_handler(CommandHandler("block",    cmd_block))
    tg_app.add_handler(CommandHandler("unblock",  cmd_unblock))
    tg_app.add_handler(CommandHandler("delete",   cmd_delete))
    tg_app.add_handler(CommandHandler("download", cmd_download))
    tg_app.add_handler(CallbackQueryHandler(on_callback))

    await tg_app.initialize()

    await tg_app.bot.set_my_commands([
        BotCommand("start",    "Главное меню"),
        BotCommand("status",   "Статус плеера"),
        BotCommand("tracks",   "Список треков"),
        BotCommand("download", "Скачать с YouTube/SoundCloud"),
        BotCommand("block",    "Заблокировать плеер"),
        BotCommand("unblock",  "Разблокировать плеер"),
        BotCommand("delete",   "Удалить трек по ID"),
    ])

    if PUBLIC_URL:
        wh = f"{PUBLIC_URL}/webhook/telegram"
        await tg_app.bot.set_webhook(url=wh)
        log.info(f"Webhook: {wh}")
    else:
        log.warning("PUBLIC_URL не задан — webhook не установлен")

    await tg_app.start()
    log.info(f"AURA Bot v2 запущен на порту {PORT}")

    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

    await tg_app.stop()


if __name__ == "__main__":
    asyncio.run(main())
