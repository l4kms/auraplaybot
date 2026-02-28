"""
AURA Music Player — Telegram Bot
─────────────────────────────────
Команды:
  /start         — Приветствие
  /status        — Статистика плеера
  /tracks        — Список всех треков
  /delete <id>   — Удалить трек по ID
  /block         — Заблокировать плеер
  /unblock       — Разблокировать плеер
  /download <url>— Скачать трек с YouTube/SoundCloud и загрузить в базу

Webhook endpoint:
  POST /webhook/track-added  — Supabase триггер шлёт сюда при добавлении трека
"""

import os
import re
import asyncio
import logging
import tempfile
import json
import mimetypes
from pathlib import Path
from datetime import datetime

import httpx
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# ──────────────────────────────────────────────────────────
#  CONFIG  (из переменных окружения)
# ──────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")          # @BotFather
ADMIN_CHAT_ID   = int(os.getenv("ADMIN_CHAT_ID", "0"))# твой chat_id
SB_URL          = os.getenv("SB_URL", "https://jzrepyzzeocepgvqdlwa.supabase.co")
SB_KEY          = os.getenv("SB_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imp6cmVweXp6ZW9jZXBndnFkbHdhIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzIxODU1ODQsImV4cCI6MjA4Nzc2MTU4NH0.Qdm7baXlJ22mkfjpzZIKJZuP_SJt4s0PZ4R6bLEviWQ")
SB_BUCKET       = os.getenv("SB_BUCKET", "tracks")
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET", "aura_secret_2024")  # придумай своё
PORT            = int(os.getenv("PORT", "8000"))
PUBLIC_URL      = os.getenv("PUBLIC_URL", "")  # https://твой-домен.railway.app

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

async def sb_get(path: str, params: dict = None) -> list | dict:
    url = f"{SB_URL}/rest/v1/{path}"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url, headers=SB_HEADERS, params=params)
        r.raise_for_status()
        return r.json()

async def sb_post(path: str, body: dict) -> dict:
    url = f"{SB_URL}/rest/v1/{path}"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers={**SB_HEADERS, "Prefer": "return=representation"}, json=body)
        r.raise_for_status()
        return r.json()

async def sb_patch(path: str, body: dict):
    url = f"{SB_URL}/rest/v1/{path}"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.patch(url, headers=SB_HEADERS, json=body)
        r.raise_for_status()

async def sb_delete(path: str):
    url = f"{SB_URL}/rest/v1/{path}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.delete(url, headers=SB_HEADERS)
        r.raise_for_status()

async def sb_upload(path: str, data: bytes, content_type: str) -> str:
    """Upload file to Supabase Storage, return public URL."""
    url = f"{SB_URL}/storage/v1/object/{SB_BUCKET}/{path}"
    headers = {
        "apikey": SB_KEY,
        "Authorization": f"Bearer {SB_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(url, headers=headers, content=data)
        r.raise_for_status()
    return f"{SB_URL}/storage/v1/object/public/{SB_BUCKET}/{path}"

async def get_site_config() -> dict:
    try:
        rows = await sb_get("settings", {"id": "eq.1"})
        if rows:
            return rows[0]
    except Exception:
        pass
    return {"blocked": False, "pw_enabled": True}

async def set_site_blocked(blocked: bool):
    """Включить/выключить блокировку плеера через таблицу settings."""
    try:
        # Попробуем upsert
        url = f"{SB_URL}/rest/v1/settings"
        body = {"id": 1, "blocked": blocked}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                url,
                headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
                json=body
            )
            r.raise_for_status()
    except Exception as e:
        log.error(f"set_site_blocked error: {e}")
        raise

def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_CHAT_ID

def fmt_dur(seconds: float) -> str:
    s = int(seconds)
    return f"{s//60}:{s%60:02d}"

# ──────────────────────────────────────────────────────────
#  КОМАНДЫ
# ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    text = (
        "🎵 *AURA Bot* — панель управления\n\n"
        "Команды:\n"
        "/status — статистика плеера\n"
        "/tracks — список треков\n"
        "/block — заблокировать плеер\n"
        "/unblock — разблокировать плеер\n"
        "/download `<url>` — добавить трек с YouTube/SoundCloud\n"
        "/delete `<id>` — удалить трек\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try:
        tracks = await sb_get("tracks", {"select": "id,title,play_count"})
        cfg = await get_site_config()
        total_plays = sum(t.get("play_count", 0) for t in tracks)
        blocked = cfg.get("blocked", False)
        status_icon = "🔴 Заблокирован" if blocked else "🟢 Открыт"
        text = (
            f"📊 *Статус AURA*\n\n"
            f"🌐 Плеер: {status_icon}\n"
            f"🎵 Треков: *{len(tracks)}*\n"
            f"▶️ Всего прослушиваний: *{total_plays}*\n"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 Заблокировать" if not blocked else "🟢 Разблокировать",
                                 callback_data="toggle_block"),
            InlineKeyboardButton("🔄 Обновить", callback_data="refresh_status"),
        ]])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_tracks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try:
        tracks = await sb_get("tracks", {"select": "id,title,artist,duration,play_count", "order": "created_at.desc", "limit": "50"})
        if not tracks:
            await update.message.reply_text("📭 Треков нет.")
            return
        lines = ["🎵 *Треки в базе* (последние 50):\n"]
        for t in tracks:
            dur = fmt_dur(t.get("duration", 0))
            plays = t.get("play_count", 0)
            lines.append(f"`{t['id']:>4}` | {dur} | ▶{plays} | *{t['title']}* — {t['artist']}")
        text = "\n".join(lines)
        # Telegram лимит 4096 символов
        if len(text) > 4000:
            text = text[:4000] + "\n…(обрезано)"
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try:
        await set_site_blocked(True)
        await update.message.reply_text("🔴 Плеер *заблокирован*. Пользователи видят страницу блокировки.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try:
        await set_site_blocked(False)
        await update.message.reply_text("🟢 Плеер *разблокирован* и доступен.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /delete <id>")
        return
    track_id = int(args[0])
    try:
        rows = await sb_get("tracks", {"id": f"eq.{track_id}"})
        if not rows:
            await update.message.reply_text(f"❌ Трек #{track_id} не найден.")
            return
        tr = rows[0]
        # Удаляем файлы из storage
        for field in ("audio_url", "art_url"):
            url = tr.get(field, "")
            if url and f"/public/{SB_BUCKET}/" in url:
                path = url.split(f"/public/{SB_BUCKET}/")[1]
                try:
                    await sb_delete_file(path)
                except Exception:
                    pass
        await sb_delete(f"playlist_tracks?track_id=eq.{track_id}")
        await sb_delete(f"tracks?id=eq.{track_id}")
        await update.message.reply_text(f"✅ Трек *{tr['title']}* удалён.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def sb_delete_file(path: str):
    url = f"{SB_URL}/storage/v1/object/{SB_BUCKET}/{path}"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(url, headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"})
        # не падаем если файл уже удалён

# ──────────────────────────────────────────────────────────
#  СКАЧИВАНИЕ ТРЕКА  (yt-dlp)
# ──────────────────────────────────────────────────────────
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "Использование: /download <url>\n\n"
            "Поддерживаются YouTube, SoundCloud и 1000+ других сайтов.\n"
            "Пример: `/download https://youtu.be/dQw4w9WgXcQ`",
            parse_mode="Markdown"
        )
        return

    url = args[0]
    if not url.startswith("http"):
        await update.message.reply_text("❌ Некорректная ссылка.")
        return

    msg = await update.message.reply_text("⏳ Получаю информацию о треке...")

    try:
        import yt_dlp
    except ImportError:
        await msg.edit_text("❌ yt-dlp не установлен. Запусти: `pip install yt-dlp`", parse_mode="Markdown")
        return

    loop = asyncio.get_event_loop()
    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": f"{tmpdir}/%(title)s.%(ext)s",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "quiet": True,
            "no_warnings": True,
        }

        try:
            await msg.edit_text("⏳ Скачиваю аудио...")
            info = await loop.run_in_executor(None, lambda: _ydl_download(url, ydl_opts))
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка скачивания: {e}")
            return

        title  = info.get("title", "Unknown")
        artist = info.get("uploader") or info.get("artist") or "Unknown"
        duration = float(info.get("duration") or 0)

        # Найти скачанный mp3
        mp3_files = list(Path(tmpdir).glob("*.mp3"))
        if not mp3_files:
            await msg.edit_text("❌ Файл не найден после скачивания.")
            return
        mp3_path = mp3_files[0]

        await msg.edit_text(f"⏳ Загружаю *{title}* в Supabase...", parse_mode="Markdown")

        # Upload audio
        safe = re.sub(r'[^\w\-.]', '_', title)[:60]
        audio_key = f"audio/{int(datetime.now().timestamp())}_{safe}.mp3"
        audio_data = mp3_path.read_bytes()
        try:
            audio_url = await sb_upload(audio_key, audio_data, "audio/mpeg")
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка загрузки аудио: {e}")
            return

        # Попробуем скачать обложку
        art_url = None
        thumb_url = info.get("thumbnail")
        if thumb_url:
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.get(thumb_url)
                if r.status_code == 200:
                    ext = "jpg"
                    art_key = f"art/{int(datetime.now().timestamp())}_{safe}.{ext}"
                    art_url = await sb_upload(art_key, r.content, "image/jpeg")
            except Exception:
                pass  # обложка необязательна

        # Insert into DB
        try:
            rows = await sb_post("tracks", {
                "title": title[:200],
                "artist": artist[:200],
                "audio_url": audio_url,
                "art_url": art_url,
                "favorite": False,
                "duration": round(duration, 2),
                "play_count": 0,
            })
            track_id = rows[0]["id"] if isinstance(rows, list) else rows.get("id", "?")
            await msg.edit_text(
                f"✅ Трек добавлен!\n\n"
                f"🎵 *{title}*\n"
                f"👤 {artist}\n"
                f"⏱ {fmt_dur(duration)}\n"
                f"🆔 ID: `{track_id}`",
                parse_mode="Markdown"
            )
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка записи в базу: {e}")

def _ydl_download(url: str, opts: dict) -> dict:
    """Синхронная обёртка для yt-dlp (запускается в executor)."""
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)

# ──────────────────────────────────────────────────────────
#  CALLBACK BUTTONS
# ──────────────────────────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update): return

    if q.data == "toggle_block":
        cfg = await get_site_config()
        new_state = not cfg.get("blocked", False)
        await set_site_blocked(new_state)
        icon = "🔴 Заблокирован" if new_state else "🟢 Открыт"
        await q.edit_message_text(
            f"Плеер: *{icon}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔴 Заблокировать" if not new_state else "🟢 Разблокировать",
                                     callback_data="toggle_block"),
                InlineKeyboardButton("🔄 Обновить", callback_data="refresh_status"),
            ]])
        )
    elif q.data == "refresh_status":
        tracks = await sb_get("tracks", {"select": "id,play_count"})
        cfg = await get_site_config()
        total_plays = sum(t.get("play_count", 0) for t in tracks)
        blocked = cfg.get("blocked", False)
        await q.edit_message_text(
            f"📊 *Статус AURA*\n\n"
            f"🌐 Плеер: {'🔴 Заблокирован' if blocked else '🟢 Открыт'}\n"
            f"🎵 Треков: *{len(tracks)}*\n"
            f"▶️ Всего прослушиваний: *{total_plays}*\n",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔴 Заблокировать" if not blocked else "🟢 Разблокировать",
                                     callback_data="toggle_block"),
                InlineKeyboardButton("🔄 Обновить", callback_data="refresh_status"),
            ]])
        )

# ──────────────────────────────────────────────────────────
#  FASTAPI — webhook endpoint для Supabase триггера
# ──────────────────────────────────────────────────────────
fastapi_app = FastAPI()
tg_app: Application = None  # будет проинициализирован в main

@fastapi_app.post("/webhook/track-added")
async def on_track_added(request: Request):
    """Supabase Database Trigger шлёт сюда POST когда добавлен новый трек."""
    # Проверяем секрет
    secret = request.headers.get("x-webhook-secret", "")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    title  = body.get("title", "?")
    artist = body.get("artist", "?")
    track_id = body.get("id", "?")

    if tg_app and ADMIN_CHAT_ID:
        text = (
            f"🎵 *Новый трек добавлен!*\n\n"
            f"*{title}* — {artist}\n"
            f"🆔 ID: `{track_id}`"
        )
        try:
            await tg_app.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=text,
                parse_mode="Markdown"
            )
        except Exception as e:
            log.error(f"Telegram notify error: {e}")

    return JSONResponse({"ok": True})

@fastapi_app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Telegram webhook endpoint."""
    body = await request.body()
    data = json.loads(body)
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return JSONResponse({"ok": True})

@fastapi_app.get("/")
async def health():
    return {"status": "ok", "service": "AURA Bot"}

# ──────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────
async def main():
    global tg_app

    if not BOT_TOKEN:
        log.error("BOT_TOKEN не задан! Установи переменную окружения.")
        return

    # Создаём Telegram Application
    tg_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .updater(None)  # webhook mode — updater не нужен
        .build()
    )

    # Регистрируем команды
    tg_app.add_handler(CommandHandler("start",    cmd_start))
    tg_app.add_handler(CommandHandler("status",   cmd_status))
    tg_app.add_handler(CommandHandler("tracks",   cmd_tracks))
    tg_app.add_handler(CommandHandler("block",    cmd_block))
    tg_app.add_handler(CommandHandler("unblock",  cmd_unblock))
    tg_app.add_handler(CommandHandler("delete",   cmd_delete))
    tg_app.add_handler(CommandHandler("download", cmd_download))
    tg_app.add_handler(CallbackQueryHandler(on_callback))

    await tg_app.initialize()

    # Устанавливаем список команд в Telegram
    await tg_app.bot.set_my_commands([
        BotCommand("start",    "Главное меню"),
        BotCommand("status",   "Статус плеера"),
        BotCommand("tracks",   "Список треков"),
        BotCommand("download", "Скачать трек с YouTube/SoundCloud"),
        BotCommand("block",    "Заблокировать плеер"),
        BotCommand("unblock",  "Разблокировать плеер"),
        BotCommand("delete",   "Удалить трек по ID"),
    ])

    # Регистрируем webhook в Telegram
    if PUBLIC_URL:
        webhook_url = f"{PUBLIC_URL}/webhook/telegram"
        await tg_app.bot.set_webhook(url=webhook_url)
        log.info(f"Webhook set: {webhook_url}")
    else:
        log.warning("PUBLIC_URL не задан — webhook не установлен. Установи и перезапусти.")

    await tg_app.start()
    log.info(f"AURA Bot запущен на порту {PORT}")

    # Запускаем FastAPI
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

    await tg_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
