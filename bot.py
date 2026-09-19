"""
ربات دانلودر تلگرام — آماده‌ی استقرار روی Railway
هر لینکی را می‌گیرد:
  • فایل مستقیم (ویدیو/صدا/عکس/سند)  → دانلود و ارسال
  • لینک یوتیوب و سایت‌های ویدیویی   → با yt-dlp دانلود و ارسال
  • لینک صفحه‌ی وب معمولی            → متن صفحه را استخراج و می‌فرستد
توکن را از متغیر محیطی BOT_TOKEN می‌خواند.
"""
import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import httpx
import yt_dlp
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# API رسمی ربات تلگرام سقف آپلود ۵۰ مگابایت دارد
MAX_SIZE = 49 * 1024 * 1024
MAX_TEXT = 3800

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".wav", ".flac", ".opus"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

WELCOME = (
    "👋 سلام!\n"
    "هر لینکی را برایم بفرست تا محتوایش را برایت بفرستم:\n"
    "• 🎬 لینک مستقیم فایل (ویدیو، موزیک، عکس، PDF…)\n"
    "• ▶️ یوتیوب و سایت‌های ویدیویی\n"
    "• 📄 لینک صفحه‌ی وب → متن صفحه\n\n"
    "⚠️ فایل‌های بزرگ‌تر از ۴۹ مگابایت را نمی‌توانم بفرستم (محدودیت خود تلگرام)."
)

URL_RE = r"(https?://\S+)"


def human_size(n: int) -> str:
    for unit in ("بایت", "کیلوبایت", "مگابایت", "گیگابایت"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "بایت" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} گیگابایت"


# ───────────────────────── دانلود فایل مستقیم ─────────────────────────

async def download_direct(url: str, dest: Path, state: dict) -> bool:
    """دانلود استریمی؛ اگر فایل بزرگ‌تر از سقف بود، وسط کار انصراف می‌دهد."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            length = resp.headers.get("content-length")
            if length and int(length) > MAX_SIZE:
                state["too_big"] = int(length)
                return False
            downloaded = 0
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(64 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_SIZE:
                        state["too_big"] = None
                        return False
                    f.write(chunk)
            state["size"] = downloaded
    return True


def filename_from_response(url: str, headers: dict) -> str:
    cd = headers.get("content-disposition", "")
    if "filename=" in cd:
        name = cd.split("filename=")[-1].strip().strip('"').strip("'")
        if name:
            return name
    path = httpx.URL(url).path
    name = Path(path).name or "file"
    return name if Path(name).suffix else name + ".bin"


# ───────────────────────── دانلود با yt-dlp ─────────────────────────

def ytdlp_download(url: str, workdir: Path) -> Path | None:
    """انتخاب بهترین فرمت زیر ۴۹ مگ؛ خروجی: مسیر فایل دانلودشده."""
    opts = {
        "outtmpl": str(workdir / "%(title).60s.%(ext)s"),
        "format": "b[filesize<50M]/b[filesize_approx<50M]/b",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
        "max_filesize": MAX_SIZE,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            return None
        if "entries" in info:  # پلی‌لیست: اولین ویدیو
            entries = [e for e in info["entries"] if e]
            if not entries:
                return None
            info = entries[0]
        return Path(ydl.prepare_filename(info))


# ───────────────────────── ارسال فایل ─────────────────────────

async def send_file(bot_msg, path: Path):
    ext = path.suffix.lower()
    caption = f"📥 {path.name} ({human_size(path.stat().st_size)})"
    if ext in VIDEO_EXT:
        await bot_msg.reply_video(video=open(path, "rb"), caption=caption, supports_streaming=True)
    elif ext in AUDIO_EXT:
        await bot_msg.reply_audio(audio=open(path, "rb"), caption=caption)
    elif ext in IMAGE_EXT:
        await bot_msg.reply_photo(photo=open(path, "rb"), caption=caption)
    else:
        await bot_msg.reply_document(document=open(path, "rb"), caption=caption)


# ───────────────────────── متن صفحه‌ی وب ─────────────────────────

async def send_page_text(bot_msg, url: str):
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        title = soup.title.get_text(strip=True) if soup.title else url
        body = "\n".join(
            line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
        )
    text = f"📄 {title}\n\n{body}"[:MAX_TEXT]
    await bot_msg.reply_text(text + ("\n\n… (ادامه‌ی متن کوتاه شد)" if len(body) > MAX_TEXT else ""))


# ───────────────────────── هندلر اصلی ─────────────────────────

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    msg = await update.message.reply_text("🔎 در حال بررسی لینک…")

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # ۱) امتحان دانلود مستقیم (هدر content-type راهنماست)
    state: dict = {}
    dest = Path(tempfile.mkdtemp()) / "file"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            head = await client.head(url)
        ctype = head.headers.get("content-type", "")
    except Exception:
        ctype = ""

    is_direct = any(
        ctype.startswith(p) for p in ("video/", "audio/", "image/", "application/octet-stream")
    ) or (not ctype.startswith("text/html") and Path(httpx.URL(url).path).suffix.lower() in VIDEO_EXT | AUDIO_EXT | IMAGE_EXT)

    if is_direct:
        try:
            await msg.edit_text("⬇️ در حال دانلود فایل…")
            ok = await download_direct(url, dest, state)
            if ok:
                name = filename_from_response(url, head.headers)
                final = dest.with_name(name)
                shutil.move(dest, final)
                await send_file(msg, final)
                await msg.delete()
                return
            limit = human_size(state.get("too_big") or MAX_SIZE)
            await msg.edit_text(f"⚠️ فایل بزرگ‌تر از سقف تلگرام است ({limit}).")
            return
        except Exception as e:
            print(f"direct download failed: {e!r} — falling back to yt-dlp")

    # ۲) yt-dlp برای یوتیوب و سایت‌های ویدیویی
    await msg.edit_text("🎬 در حال استخراج رسانه…")
    workdir = Path(tempfile.mkdtemp())
    try:
        path = await asyncio.to_thread(ytdlp_download, url, workdir)
        if path and path.exists() and path.stat().st_size <= MAX_SIZE:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)
            await send_file(msg, path)
            await msg.delete()
            return
        if path:
            await msg.edit_text("⚠️ ویدیو پیدا شد ولی بزرگ‌تر از سقف ۴۹ مگابایت تلگرام است.")
            return
    except Exception as e:
        print(f"yt-dlp failed: {e!r}")

    # ۳) صفحه‌ی وب معمولی → متن
    try:
        await send_page_text(msg, url)
        await msg.delete()
    except Exception:
        await msg.edit_text(
            "❌ نتونستم از این لینک چیزی بگیرم.\n"
            "مطمئن شو لینک درست است و عمومی است (نه خصوصی/نیازمند لاگین)."
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(dest.parent, ignore_errors=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "فقط لینک را بفرست، همین! 🤖\n"
        "نمونه‌ها:\n"
        "• https://example.com/video.mp4\n"
        "• https://youtu.be/xxxxxxx\n"
        "• https://example.com/maghale"
    )


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("متغیر محیطی BOT_TOKEN تنظیم نشده است.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(URL_RE), handle_link))

    print("✅ ربات روشن شد…")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
