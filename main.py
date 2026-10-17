import os
import re
import time
import secrets
import logging
import asyncio
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv
import yt_dlp
from yt_dlp.version import __version__ as YTDLP_VERSION

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# تنظیمات عضویت اجباری از طریق ربات ادمین مرکزی
ADMIN_API_URL = os.getenv("ADMIN_API_URL", "https://admin-dl-bot.onrender.com").rstrip("/")
API_KEY = os.getenv("API_KEY", "")

# اختیاری: فایل کوکی برای توییت‌های محدودشده (حساس/نیازمند لاگین)
COOKIES_PATH = os.getenv("COOKIES_PATH", "cookies.txt")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# جلوگیری از چاپ توکن ربات توی لاگ (httpx آدرس کامل درخواست را لاگ می‌کند)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info("yt-dlp version: %s", YTDLP_VERSION)
logger.info(
    "Cookie file '%s' exists: %s", COOKIES_PATH, os.path.exists(COOKIES_PATH)
)

# سقف ارسال ویدئو با Bot API معمولی
MAX_FILE_SIZE = 49 * 1024 * 1024
# سقف عکس
MAX_PHOTO_SIZE = 10 * 1024 * 1024
# حداکثر تعداد ویدئو که از یک توییت جداگانه فرستاده می‌شود
MAX_VIDEOS_PER_TWEET = 4

# حداکثر تعداد دانلود همزمان (سرور رایگان منابع کمی دارد)
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)

# فاصله‌ی حداقل بین دو درخواست یک کاربر (ثانیه)
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "10"))
_last_request: dict[int, float] = {}

# درخواست‌های منتظر انتخاب کیفیت: token -> اطلاعات
PENDING: dict[str, dict] = {}
PENDING_TTL = 30 * 60  # ۳۰ دقیقه

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# متن دکمه استارت (در یک جا تعریف شده تا همه‌جا یکسان باشد)
START_BUTTON_TEXT = "🚀 استارت"


# ---------------- Health Server ----------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Twitter bot is alive")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health server started on port %s", port)
    server.serve_forever()


# ---------------- Bot Commands (منوی آبی کنار کیبورد) ----------------

async def setup_bot_commands(app: Application):
    commands = [
        BotCommand("start", START_BUTTON_TEXT),
    ]

    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeDefault(),
    )

    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllPrivateChats(),
    )

    logger.info("Bot commands configured.")


# ---------------- Reply Keyboard (کیبورد پایین) ----------------

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(START_BUTTON_TEXT)],
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="لینک توییت را بفرست یا استارت بزن...",
    )


# ---------------- URL helpers ----------------

TWITTER_HOSTS = {
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "x.com",
    "www.x.com",
}

# سرویس‌های پیش‌نمایش که به x.com تبدیل می‌شوند
MIRROR_HOSTS = {"fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com"}

STATUS_RE = re.compile(r"/status(?:es)?/(\d+)")


def extract_url(text: str):
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None


def parse_tweet_url(url: str):
    """(tweet_id, username) را برمی‌گرداند؛ اگر لینک توییت نبود None."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in TWITTER_HOSTS and host not in MIRROR_HOSTS:
            return None
        match = STATUS_RE.search(parsed.path)
        if not match:
            return None
        segments = [s for s in parsed.path.split("/") if s]
        username = "i"
        if segments and segments[0].lower() not in ("i", "web"):
            username = segments[0]
        return match.group(1), username
    except Exception:
        return None


# ---------------- Tweet data (FxTwitter API) ----------------

async def fetch_tweet(tweet_id: str, username: str):
    """اطلاعات توییت (متن، عکس‌ها، ویدئوها) را از API عمومی FxTwitter می‌گیرد."""
    urls = [f"https://api.fxtwitter.com/{username}/status/{tweet_id}"]
    if username != "i":
        urls.append(f"https://api.fxtwitter.com/i/status/{tweet_id}")

    headers = {"User-Agent": "Mozilla/5.0 (compatible; TelegramMediaBot/1.0)"}

    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, headers=headers
    ) as client:
        for api_url in urls:
            try:
                resp = await client.get(api_url)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                tweet = data.get("tweet") or data.get("status")
                if tweet:
                    return tweet
            except Exception as e:
                logger.warning("FxTwitter request failed: %s", e)
    return None


def get_media(tweet: dict):
    """(لیست آدرس عکس‌ها، لیست ویدئوها) را از اطلاعات توییت برمی‌گرداند."""
    media = (tweet or {}).get("media") or {}
    photos = [p["url"] for p in (media.get("photos") or []) if p.get("url")]
    videos = [v for v in (media.get("videos") or []) if v.get("url")]
    return photos, videos


def author_line(tweet: dict) -> str:
    author = (tweet or {}).get("author") or {}
    name = author.get("name") or ""
    screen = author.get("screen_name") or ""
    if screen:
        return f"{name} (@{screen})".strip()
    return name


def build_text(tweet: dict, tweet_id: str) -> str:
    parts = [f"👤 {author_line(tweet)}"]

    text = (tweet.get("text") or "").strip()
    if text:
        parts.append(text)

    quote = tweet.get("quote")
    if quote:
        q_text = (quote.get("text") or "").strip()
        header = f"↪️ نقل‌قول از {author_line(quote)}"
        parts.append(f"{header}:\n{q_text}" if q_text else header)

    article = tweet.get("article")
    if isinstance(article, dict) and article.get("title"):
        parts.append(f"📰 مقاله: {article['title']}")

    parts.append(tweet.get("url") or f"https://x.com/i/status/{tweet_id}")
    return "\n\n".join(parts)


def split_text(text: str, size: int = 4000):
    """متن‌های بلند را برای محدودیت ۴۰۹۶ کاراکتری تلگرام تکه می‌کند."""
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


# ---------------- Download helpers ----------------

async def download_file(url: str, path: Path, max_bytes: int) -> str:
    """فایل را با httpx دانلود می‌کند. خروجی: 'ok' | 'too_large' | 'error'."""
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return "error"
                length = resp.headers.get("content-length")
                if length and int(length) > max_bytes:
                    return "too_large"
                total = 0
                with path.open("wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            return "too_large"
                        f.write(chunk)
        return "ok"
    except Exception:
        logger.exception("download_file failed")
        return "error"


async def send_photos(context, chat_id: int, urls: list):
    """عکس‌ها را (تا ۱۰ تا در هر آلبوم) می‌فرستد؛ اگر لینک مستقیم کار نکرد، دانلود و آپلود می‌کند."""
    for i in range(0, len(urls), 10):
        group = urls[i:i + 10]

        try:
            if len(group) == 1:
                await context.bot.send_photo(chat_id=chat_id, photo=group[0])
            else:
                await context.bot.send_media_group(
                    chat_id=chat_id,
                    media=[InputMediaPhoto(media=u) for u in group],
                )
            continue
        except Exception:
            logger.warning("Sending photos by URL failed; trying upload", exc_info=True)

        blobs = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for u in group:
                try:
                    resp = await client.get(u)
                    if resp.status_code == 200 and len(resp.content) <= MAX_PHOTO_SIZE:
                        blobs.append(resp.content)
                except Exception:
                    pass

        if not blobs:
            await context.bot.send_message(
                chat_id=chat_id,
                text="🖼 " + "\n".join(group),
                link_preview_options=NO_PREVIEW,
            )
        elif len(blobs) == 1:
            await context.bot.send_photo(chat_id=chat_id, photo=blobs[0])
        else:
            await context.bot.send_media_group(
                chat_id=chat_id,
                media=[InputMediaPhoto(media=b) for b in blobs],
            )


async def send_video_from_url(context, chat_id: int, video: dict) -> bool:
    """ویدئو را از لینک مستقیم دانلود و ارسال می‌کند؛ اگر بزرگ بود لینک می‌دهد."""
    url = video.get("url")
    if not url:
        return False

    async with DOWNLOAD_SEMAPHORE:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "video.mp4"
            result = await download_file(url, path, MAX_FILE_SIZE)

            if result == "too_large":
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⚠️ حجم این ویدئو از ۵۰ مگابایت بیشتر است.\n"
                        f"لینک مستقیم دانلود:\n{url}"
                    ),
                    link_preview_options=NO_PREVIEW,
                )
                return True

            if result != "ok":
                return False

            with path.open("rb") as f:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    supports_streaming=True,
                    width=video.get("width"),
                    height=video.get("height"),
                    duration=int(video.get("duration") or 0) or None,
                    caption="🎬",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
                )
    return True


# ---------------- yt-dlp helpers (quality selection) ----------------

def prepare_cookie_file(src: str, dst: Path) -> int:
    """فایل کوکی را به فرمت درست Netscape (با Tab) تبدیل می‌کند."""
    lines = ["# Netscape HTTP Cookie File"]
    with open(src, encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.replace("\ufeff", "").strip()
            if not line:
                continue
            if line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            parts = line.split(None, 6)
            if len(parts) < 7:
                continue
            lines.append("\t".join(parts))
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 1


def base_opts(tmpdir: str) -> dict:
    opts = {
        "outtmpl": str(Path(tmpdir) / "video.%(ext)s"),
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": 30,
    }
    if os.path.exists(COOKIES_PATH):
        cookie_copy = Path(tmpdir) / "cookies.txt"
        if prepare_cookie_file(COOKIES_PATH, cookie_copy) > 0:
            opts["cookiefile"] = str(cookie_copy)
    return opts


def estimate_size(fmt: dict, duration):
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)
    tbr = fmt.get("tbr")
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return None


def build_quality_options(info: dict) -> dict:
    """گزینه‌های کیفیت قابل ارسال (تصویر+صدا، بدون ffmpeg) را می‌سازد.

    خروجی: {format_id: {"height": int, "size": int|None, ...}}
    """
    entries = info.get("entries")
    if entries:
        entries = [e for e in entries if e]
        if entries:
            info = entries[0]

    duration = info.get("duration")
    best_per_height: dict[int, dict] = {}

    for f in info.get("formats") or []:
        protocol = (f.get("protocol") or "").lower()
        if not protocol.startswith("http") or "m3u8" in protocol:
            continue
        if f.get("vcodec") in (None, "none"):
            continue
        height = f.get("height")
        if not height:
            continue
        size = estimate_size(f, duration)
        if size is not None and size > MAX_FILE_SIZE:
            continue
        prev = best_per_height.get(height)
        if prev is None or (f.get("tbr") or 0) > (prev.get("tbr") or 0):
            best_per_height[height] = {
                "format_id": f["format_id"],
                "height": int(height),
                "width": f.get("width"),
                "size": size,
                "tbr": f.get("tbr"),
            }

    return {
        v["format_id"]: v
        for v in sorted(best_per_height.values(), key=lambda x: x["height"])
    }


def cleanup_pending():
    now = time.time()
    for token in [t for t, e in PENDING.items() if now - e["ts"] > PENDING_TTL]:
        PENDING.pop(token, None)


def human_size(size):
    return f"{size / 1024 / 1024:.1f} MB" if size else "?"


async def offer_quality(message, context, url: str, user_id: int, fx_video):
    """کیفیت‌های ویدئو را با yt-dlp می‌خواند و دکمه‌ی انتخاب نشان می‌دهد.

    اگر yt-dlp جواب نداد و لینک مستقیم FxTwitter داریم، همان را می‌فرستد.
    """
    q_status = await message.reply_text("⏳ در حال بررسی کیفیت‌های ویدئو...")

    try:
        with TemporaryDirectory() as tmpdir:
            opts = base_opts(tmpdir)
            opts["skip_download"] = True

            def extract():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)

            info = await asyncio.to_thread(extract)

        options = build_quality_options(info)
        if not options:
            raise RuntimeError("no downloadable formats")

        cleanup_pending()
        token = secrets.token_hex(4)
        entries = info.get("entries")
        meta = [e for e in entries if e][0] if entries and any(entries) else info
        PENDING[token] = {
            "url": url,
            "user_id": user_id,
            "options": options,
            "duration": meta.get("duration"),
            "fx_video": fx_video,
            "ts": time.time(),
            "busy": False,
        }

        buttons = [
            InlineKeyboardButton(
                f"{opt['height']}p  ({human_size(opt['size'])})",
                callback_data=f"q:{token}:{fid}",
            )
            for fid, opt in options.items()
        ]
        markup = InlineKeyboardMarkup([[b] for b in buttons])
        await q_status.edit_text("🎬 کیفیت را انتخاب کن:", reply_markup=markup)
        return

    except Exception as e:
        logger.warning("Quality extraction failed: %s", str(e)[:300])

    # جایگزین: لینک مستقیم FxTwitter (بهترین کیفیت)
    if fx_video:
        await q_status.edit_text("⏳ در حال دانلود ویدئو...")
        try:
            ok = await send_video_from_url(context, message.chat_id, fx_video)
        except Exception:
            logger.exception("Fallback video send failed")
            ok = False
        if ok:
            await q_status.delete()
            return

    await q_status.edit_text(
        "❌ ارسال ویدئو ممکن نشد.\n"
        "ممکن است توییت ویدئو نداشته باشد، محدودشده (خصوصی/حساس) باشد، "
        "یا ویدئو خیلی بزرگ باشد."
    )


# ---------------- Mandatory Join (Central Admin API) ----------------

async def check_membership(user_id: int):
    """خروجی: (اجازه دسترسی, کانال‌های باقی‌مانده برای عضویت)."""
    if not API_KEY:
        logger.error("API_KEY is not configured; mandatory join check is blocked")
        return False, []

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/check",
                params={"user_id": user_id},
                headers={"X-API-Key": API_KEY},
            )
            response.raise_for_status()
            data = response.json()

        # اگر عضویت اجباری در پنل خاموش باشد، همه مجازند.
        if data.get("enabled") is False:
            return True, []

        missing = data.get("missing") or []
        allowed = bool(data.get("ok")) and not missing
        return allowed, missing
    except Exception:
        logger.exception("Central membership API request failed")
        # در صورت خرابی API دسترسی را باز نمی‌کنیم.
        return False, []


def _channel_value(channel, *keys):
    if isinstance(channel, dict):
        for key in keys:
            value = channel.get(key)
            if value:
                return str(value)
        return ""
    return str(channel) if channel else ""


async def membership_markup(missing):
    """دکمه‌های عضویت را از اطلاعات کانال‌های برگشتی API می‌سازد."""
    rows = []
    # بعضی نسخه‌های API اطلاعات لینک را در missing می‌فرستند؛ اگر نه، از /api/channels می‌گیریم.
    channels = missing if all(isinstance(item, dict) for item in missing) else []
    if missing and not channels:
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(
                    f"{ADMIN_API_URL}/api/channels",
                    headers={"X-API-Key": API_KEY},
                )
                response.raise_for_status()
                payload = response.json()
            all_channels = payload.get("channels", payload) if isinstance(payload, dict) else payload
            if isinstance(all_channels, list):
                wanted = {str(item).lstrip("@").lower() for item in missing}
                for channel in all_channels:
                    if not isinstance(channel, dict):
                        continue
                    identifiers = {
                        _channel_value(channel, "id", "channel_id", "username", "channel", "chat_id").lstrip("@").lower(),
                        _channel_value(channel, "username", "channel", "chat_id").lstrip("@").lower(),
                    }
                    if wanted.intersection(identifiers):
                        channels.append(channel)
        except Exception:
            logger.exception("Could not load channel buttons from central API")

    for channel in channels:
        url = _channel_value(channel, "join_url", "url", "invite_link", "channel_url", "link")
        name = _channel_value(channel, "title", "name", "username", "channel") or "عضویت در کانال"
        if url.startswith("http://") or url.startswith("https://"):
            rows.append([InlineKeyboardButton(f"عضویت در {name}", url=url)])

    rows.append([InlineKeyboardButton("✅ عضو شدم؛ بررسی دوباره", callback_data="join:check")])
    return InlineKeyboardMarkup(rows)


async def require_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False

    allowed, missing = await check_membership(user.id)
    if allowed:
        return True

    markup = await membership_markup(missing)
    await message.reply_text(
        "🔒 برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شو؛ سپس روی «عضو شدم» بزن.",
        reply_markup=markup,
    )
    return False


async def on_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    allowed, missing = await check_membership(query.from_user.id)
    if allowed:
        await query.edit_message_text(
            "✅ عضویتت تأیید شد. حالا لینک توییت / X را برای ربات بفرست."
        )
    else:
        await query.edit_message_text(
            "❌ هنوز عضویتت تأیید نشده. در کانال‌های لازم عضو شو و دوباره بررسی کن.",
            reply_markup=await membership_markup(missing),
        )


# ---------------- Bot Commands ----------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await require_membership(update, context):
        return
    await update.effective_message.reply_text(
        "سلام 👋\n\n"
        "لینک یک توییت (پست) از توییتر / X را بفرست.\n"
        "متن، عکس‌ها و ویدئوهای توییت را برایت می‌فرستم؛ "
        "برای ویدئو می‌توانی کیفیت را انتخاب کنی. 🎬🖼\n\n"
        "فقط محتوایی را دانلود کن که مال خودت است یا اجازه‌اش را داری.",
        reply_markup=get_main_reply_keyboard(),
    )


# ---------------- هندلر دکمه استارت از کیبورد پایین ----------------

async def handle_reply_keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message or not message.text:
        return

    if message.text.strip() == START_BUTTON_TEXT:
        await start(update, context)


async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message or not message.text:
        return

    if not await require_membership(update, context):
        return

    raw_url = extract_url(message.text)
    parsed = parse_tweet_url(raw_url) if raw_url else None

    if not parsed:
        await message.reply_text(
            "❌ لطفاً لینک یک توییت بفرست.\n"
            "نمونه: x.com/username/status/123456789"
        )
        return

    tweet_id, username = parsed
    url = f"https://x.com/i/status/{tweet_id}"

    # محدودیت فاصله‌ی زمانی برای هر کاربر
    user_id = update.effective_user.id if update.effective_user else 0
    now = time.monotonic()
    last = _last_request.get(user_id, 0)
    if now - last < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last)) + 1
        await message.reply_text(f"⏳ لطفاً {wait} ثانیه‌ی دیگر دوباره تلاش کن.")
        return
    _last_request[user_id] = now

    status = await message.reply_text("⏳ در حال بررسی توییت...")
    chat_id = message.chat_id

    try:
        tweet = await fetch_tweet(tweet_id, username)

        try:
            await status.delete()
        except Exception:
            pass

        # اگر API در دسترس نبود، فقط بخش ویدئو (yt-dlp) را امتحان می‌کنیم
        if not tweet:
            await offer_quality(message, context, url, user_id, None)
            return

        # ۱) متن توییت
        for chunk in split_text(build_text(tweet, tweet_id)):
            await context.bot.send_message(
                chat_id=chat_id, text=chunk, link_preview_options=NO_PREVIEW
            )

        photos, videos = get_media(tweet)

        # ۲) عکس‌ها
        if photos:
            await send_photos(context, chat_id, photos)

        # عکس‌های توییت نقل‌قول‌شده
        quote = tweet.get("quote")
        if quote:
            q_photos, q_videos = get_media(quote)
            if q_photos:
                await send_photos(context, chat_id, q_photos)
            if q_videos:
                q_url = quote.get("url") or ""
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "ℹ️ توییت نقل‌قول‌شده ویدئو دارد. "
                        f"برای دریافتش لینک همان توییت را جداگانه بفرست:\n{q_url}"
                    ),
                    link_preview_options=NO_PREVIEW,
                )

        # ۳) ویدئوها
        if len(videos) == 1:
            await offer_quality(message, context, url, user_id, videos[0])
        elif len(videos) > 1:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🎬 این توییت {len(videos)} ویدئو دارد؛ همه با بهترین کیفیت ممکن ارسال می‌شوند...",
            )
            for video in videos[:MAX_VIDEOS_PER_TWEET]:
                ok = False
                try:
                    ok = await send_video_from_url(context, chat_id, video)
                except Exception:
                    logger.exception("Sending one of several videos failed")
                if not ok:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ ارسال یکی از ویدئوها ممکن نشد. لینک مستقیم:\n{video.get('url')}",
                        link_preview_options=NO_PREVIEW,
                    )
            if len(videos) > MAX_VIDEOS_PER_TWEET:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"ℹ️ فقط {MAX_VIDEOS_PER_TWEET} ویدئوی اول ارسال شد.",
                )

    except Exception as e:
        logger.exception("Tweet handling failed")
        await context.bot.send_message(
            chat_id=chat_id, text=f"❌ خطا:\n{str(e)[:700]}"
        )


async def on_quality(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    data = query.data or ""

    allowed, missing = await check_membership(query.from_user.id)
    if not allowed:
        await query.answer("ابتدا باید در کانال‌های لازم عضو شوی.", show_alert=True)
        await query.message.reply_text(
            "🔒 برای ادامه، ابتدا عضو کانال‌های زیر شو و عضویتت را بررسی کن.",
            reply_markup=await membership_markup(missing),
        )
        return

    try:
        _, token, fid = data.split(":", 2)
    except ValueError:
        await query.answer()
        return

    entry = PENDING.get(token)
    option = entry["options"].get(fid) if entry else None

    if not entry or not option:
        await query.answer()
        await query.edit_message_text(
            "⌛ این درخواست منقضی شده؛ لینک توییت را دوباره بفرست."
        )
        return

    if query.from_user.id != entry["user_id"]:
        await query.answer("این دکمه برای تو نیست.", show_alert=True)
        return

    if entry["busy"]:
        await query.answer("در حال انجام است...")
        return

    entry["busy"] = True
    await query.answer()
    await query.edit_message_text(f"⏳ در حال دانلود ({option['height']}p)...")

    chat_id = query.message.chat_id

    try:
        async with DOWNLOAD_SEMAPHORE:
            with TemporaryDirectory() as tmpdir:
                opts = base_opts(tmpdir)
                opts["format"] = fid

                def download():
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.extract_info(entry["url"], download=True)

                info = await asyncio.to_thread(download)

                entries = info.get("entries")
                if entries and any(entries):
                    info = [e for e in entries if e][0]

                video_exts = {".mp4", ".mkv", ".webm", ".mov"}
                files = [
                    p for p in Path(tmpdir).iterdir()
                    if p.is_file()
                    and p.suffix.lower() in video_exts
                    and p.stat().st_size > 0
                ]

                if not files:
                    raise FileNotFoundError("Downloaded video was not found")

                filename = max(files, key=lambda p: p.stat().st_size)

                if filename.stat().st_size > MAX_FILE_SIZE:
                    await query.edit_message_text(
                        "❌ حجم این ویدئو از سقف ارسال ربات (حدود ۵۰ مگابایت) بیشتر است."
                    )
                    entry["busy"] = False
                    return

                await query.edit_message_text("📤 در حال ارسال...")

                with filename.open("rb") as video:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=video,
                        supports_streaming=True,
                        duration=int(info.get("duration") or entry.get("duration") or 0) or None,
                        width=option.get("width"),
                        height=option.get("height"),
                        caption="🎬",
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

        await query.message.delete()
        PENDING.pop(token, None)

    except Exception as e:
        logger.exception("Tweet video download failed")

        # جایگزین: لینک مستقیم FxTwitter
        fx_video = entry.get("fx_video")
        if fx_video:
            try:
                await query.edit_message_text("⏳ در حال تلاش با روش دوم...")
                if await send_video_from_url(context, chat_id, fx_video):
                    await query.message.delete()
                    PENDING.pop(token, None)
                    return
            except Exception:
                logger.exception("Fallback video send failed")

        entry["busy"] = False
        await query.edit_message_text(f"❌ خطا:\n{str(e)[:700]}")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    # خطاهای شبکه‌ی گذرا (مثل Conflict هنگام دیپلوی) را فقط کوتاه لاگ می‌کنیم
    logger.warning("Bot error: %s", context.error)


# ---------------- Main ----------------

async def post_init(app: Application):
    await setup_bot_commands(app)


def main():
    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CallbackQueryHandler(on_join_check, pattern=r"^join:check$"))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q:"))

    # هندلر دکمه استارت از کیبورد پایین (قبل از handle_url)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r"^🚀 استارت$"),
            handle_reply_keyboard,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url,
        )
    )

    app.add_error_handler(on_error)

    logger.info("Twitter bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
