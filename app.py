import os
# === SPEED OPTIMIZATION V2 ===
# Defaults are tuned for Heroku: moderate concurrency and low retry overhead.
import re
import time
import asyncio
import sqlite3
import logging
import urllib.request
import subprocess
import base64
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from dotenv import load_dotenv
import httpx
import yt_dlp
from ytmusicapi import YTMusic


# =========================================================
# LOAD ENVIRONMENT VARIABLES
# =========================================================

load_dotenv()


# =========================================================
# CONFIGURATION
# =========================================================

DOWNLOAD_DIR = os.getenv(
    "DOWNLOAD_DIR",
    "downloads"
)

CACHE_EXPIRE_HOURS = float(
    os.getenv(
        "CACHE_EXPIRE_HOURS",
        "0"
    )
)

MAX_VIDEO_QUALITY = os.getenv(
    "MAX_VIDEO_QUALITY",
    "720"
)

PORT = int(
    os.getenv(
        "PORT",
        "8000"
    )
)

COOKIE_URL = os.getenv("COOKIE_URL", "").strip()
# Backward/alternate names supported so deployment changes do not silently
# disable cookie authentication.
COOKIE_FILE_URL = os.getenv("COOKIE_FILE_URL", "").strip()
COOKIE_BASE64 = os.getenv("COOKIE_BASE64", "").strip()

# YouTube player clients. Avoid the deprecated/problematic tv_downgraded
# client that can cause "The page needs to be reloaded" errors.
YOUTUBE_PLAYER_CLIENTS = os.getenv(
    "YOUTUBE_PLAYER_CLIENTS",
    "default"
).strip()

YOUTUBE_POT_PROVIDER_URL = os.getenv(
    "YOUTUBE_POT_PROVIDER_URL",
    "http://127.0.0.1:4416"
).strip()

YOUTUBE_USER_AGENT = os.getenv(
    "YOUTUBE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
).strip()

COOKIES_FILE = "cookies.txt"
USE_COOKIES = os.getenv(
    "USE_COOKIES",
    "false"
).strip().lower() in {"1", "true", "yes", "on"}
DEBUG_ERRORS = os.getenv(
    "DEBUG_ERRORS",
    "false"
).strip().lower() in {"1", "true", "yes", "on"}

DB_FILE = "cache.db"

# =========================================================
# API KEY AUTHENTICATION
# =========================================================
# Set API_KEY in Heroku Config Vars. Keep this value secret.
# Client requests should send: X-API-Key: <your-key>
# Authorization: Bearer <your-key> is also accepted.
# For compatibility, ?api_key=<your-key> is also accepted.

API_KEY = os.getenv("API_KEY", "").strip()


async def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
    api_key: Optional[str] = Query(default=None, description="API key (legacy/query compatibility)")
):
    """Protect API endpoints with a server-side API key."""

    if not API_KEY:
        logger.error("API_KEY is not configured on the server.")
        raise HTTPException(
            status_code=503,
            detail="API authentication is not configured on the server."
        )

    # Prefer the HTTP header. Also accept ?api_key=... for compatibility
    # with existing Music Bot clients.
    supplied_key = (x_api_key or api_key or "").strip()

    # Also accept Authorization: Bearer <key> for clients that prefer it.
    if not supplied_key and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied_key = token.strip()

    if not supplied_key or supplied_key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key."
        )

    return True


# =========================================================
# DOWNLOAD PERFORMANCE SETTINGS
# =========================================================

CONCURRENT_FRAGMENT_DOWNLOADS = int(os.getenv("CONCURRENT_FRAGMENT_DOWNLOADS", "15"))

HTTP_CHUNK_SIZE = int(os.getenv("HTTP_CHUNK_SIZE", "10485760"))

SOCKET_TIMEOUT = int(os.getenv("SOCKET_TIMEOUT", "15"))

RETRIES = int(os.getenv("RETRIES", "3"))

FRAGMENT_RETRIES = int(os.getenv("FRAGMENT_RETRIES", "3"))

# Optional low-latency upstream. When configured, /stream proxies the
# upstream's already-streaming audio response instead of running yt-dlp and
# FFmpeg locally before sending the first byte.
REMOTE_API_URL = os.getenv("REMOTE_API_URL", "").strip().rstrip("/")
# Reuse the local key by default when both API deployments share a key.
REMOTE_API_KEY = os.getenv("REMOTE_API_KEY", API_KEY).strip()
REMOTE_CONNECT_TIMEOUT = float(os.getenv("REMOTE_CONNECT_TIMEOUT", "5"))
REMOTE_READ_TIMEOUT = float(os.getenv("REMOTE_READ_TIMEOUT", "300"))
REMOTE_CHUNK_SIZE = int(os.getenv("REMOTE_CHUNK_SIZE", "65536"))


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


# =========================================================
# DOWNLOAD DIRECTORY
# =========================================================

os.makedirs(
    DOWNLOAD_DIR,
    exist_ok=True
)


# =========================================================
# DATABASE & CACHE SYSTEM
# =========================================================

def init_db():

    """Initializes the SQLite database for caching metadata safely."""

    try:

        with sqlite3.connect(
            DB_FILE,
            timeout=15.0
        ) as conn:

            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT,
                    title TEXT,
                    file_name TEXT,
                    file_path TEXT,
                    file_type TEXT,
                    file_size INTEGER,
                    duration INTEGER,
                    created_time REAL,
                    thumbnail TEXT,
                    UNIQUE(video_id, file_type)
                )
                '''
            )

            conn.commit()

        logger.info(
            "SQLite database initialized."
        )

    except Exception as e:

        logger.error(
            f"Database initialization failed: {e}"
        )


def get_cached_metadata(
    video_id: str,
    file_type: str
) -> Optional[Dict[str, Any]]:

    """Retrieves cached metadata from SQLite and verifies file existence."""

    try:

        with sqlite3.connect(
            DB_FILE,
            timeout=15.0
        ) as conn:

            conn.row_factory = sqlite3.Row

            cur = conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM downloads
                WHERE video_id = ?
                AND file_type = ?
                """,
                (
                    video_id,
                    file_type
                )
            )

            row = cur.fetchone()

            if row:

                if (
                    os.path.isfile(
                        row["file_path"]
                    )
                    and
                    os.path.getsize(
                        row["file_path"]
                    ) > 0
                ):

                    return dict(row)

                else:

                    logger.warning(
                        f"File {row['file_name']} "
                        "missing from disk. "
                        "Removing DB entry."
                    )

                    cur.execute(
                        """
                        DELETE FROM downloads
                        WHERE id = ?
                        """,
                        (
                            row["id"],
                        )
                    )

                    conn.commit()

            return None

    except Exception as e:

        logger.error(
            f"Error accessing cache DB: {e}"
        )

        return None


def save_cached_metadata(
    data: Dict[str, Any],
    file_type: str
):

    """Saves download metadata to SQLite."""

    try:

        with sqlite3.connect(
            DB_FILE,
            timeout=15.0
        ) as conn:

            conn.execute(
                '''
                INSERT OR REPLACE INTO downloads
                (
                    video_id,
                    title,
                    file_name,
                    file_path,
                    file_type,
                    file_size,
                    duration,
                    created_time,
                    thumbnail
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    data["videoId"],
                    data["title"],
                    data["filename"],
                    data["path"],
                    file_type,
                    data["filesize"],
                    data["duration"],
                    time.time(),
                    data["thumbnail"]
                )
            )

            conn.commit()

    except Exception as e:

        logger.error(
            f"Error saving to cache DB: {e}"
        )


def find_legacy_cached_file(
    video_id: str,
    ext: str
) -> Optional[str]:

    """Fallback to check un-indexed files downloaded before SQLite was added."""

    if not video_id:

        return None

    suffix = f"_{video_id}.{ext}"

    try:

        with os.scandir(
            DOWNLOAD_DIR
        ) as entries:

            for entry in entries:

                if entry.name.endswith(
                    suffix
                ):

                    return entry.name

    except Exception as e:

        logger.error(
            f"Error reading {DOWNLOAD_DIR}: {e}"
        )

    return None


# =========================================================
# CACHE CLEANUP
# =========================================================

async def cache_cleanup_task():

    """Background task to delete old files and clean up database."""

    while True:

        try:

            logger.info(
                "Running advanced cache cleanup..."
            )

            # CACHE_EXPIRE_HOURS <= 0 means permanent cache.
            # Never scan/delete cached media in permanent-cache mode.
            if CACHE_EXPIRE_HOURS <= 0:
                logger.info("Permanent cache enabled — skipping automatic file deletion.")
                await asyncio.sleep(3600)
                continue

            expiry_time = (
                time.time()
                -
                (
                    CACHE_EXPIRE_HOURS
                    * 3600
                )
            )

            def perform_cleanup():

                deleted_files = 0
                db_cleaned = 0

                with sqlite3.connect(
                    DB_FILE,
                    timeout=15.0
                ) as conn:

                    conn.row_factory = sqlite3.Row

                    cur = conn.cursor()

                    # -----------------------------------------
                    # 1. Scan disk for expired files
                    # -----------------------------------------

                    if os.path.exists(
                        DOWNLOAD_DIR
                    ):

                        for entry in os.scandir(
                            DOWNLOAD_DIR
                        ):

                            if entry.is_file():

                                file_stat = entry.stat()

                                if (
                                    file_stat.st_mtime
                                    <
                                    expiry_time
                                ):

                                    try:

                                        os.remove(
                                            entry.path
                                        )

                                        deleted_files += 1

                                        cur.execute(
                                            """
                                            DELETE FROM downloads
                                            WHERE file_name = ?
                                            """,
                                            (
                                                entry.name,
                                            )
                                        )

                                    except Exception as e:

                                        logger.warning(
                                            f"Could not delete old "
                                            f"file {entry.name}: {e}"
                                        )

                    # -----------------------------------------
                    # 2. Remove phantom DB records
                    # -----------------------------------------

                    cur.execute(
                        """
                        SELECT id, file_path
                        FROM downloads
                        """
                    )

                    all_records = cur.fetchall()

                    for record in all_records:

                        if not os.path.exists(
                            record["file_path"]
                        ):

                            cur.execute(
                                """
                                DELETE FROM downloads
                                WHERE id = ?
                                """,
                                (
                                    record["id"],
                                )
                            )

                            db_cleaned += 1

                    conn.commit()

                return (
                    deleted_files,
                    db_cleaned
                )

            deleted_files, db_cleaned = (
                await asyncio.to_thread(
                    perform_cleanup
                )
            )

            if (
                deleted_files > 0
                or
                db_cleaned > 0
            ):

                logger.info(
                    f"Cleanup complete: "
                    f"Deleted {deleted_files} "
                    f"old files on disk, "
                    f"cleared {db_cleaned} "
                    f"orphaned DB records."
                )

            else:

                logger.info(
                    "Cleanup complete: "
                    "No expired files found."
                )

        except Exception as e:

            logger.error(
                "Cache cleanup encountered an error "
                f"(will retry next cycle): {e}"
            )

        await asyncio.sleep(
            3600
        )


# =========================================================
# FASTAPI LIFESPAN
# =========================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    logger.info(
        "Starting MAGMA Music API..."
    )

    init_db()

    # -----------------------------------------
    # Load YouTube cookies
    # -----------------------------------------
    # Priority: COOKIE_BASE64 -> COOKIE_URL -> COOKIE_FILE_URL -> existing file.
    # A cookie file is considered usable only if it looks like a Netscape
    # cookies.txt file. This prevents yt-dlp from silently running without
    # authentication after a bad/HTML download.

    if COOKIE_BASE64:
        try:
            raw = base64.b64decode(COOKIE_BASE64, validate=True)
            with open(COOKIES_FILE, "wb") as f:
                f.write(raw)
            logger.info("Loaded cookies.txt from COOKIE_BASE64")
        except Exception as e:
            logger.error(f"Failed to decode COOKIE_BASE64: {e}")

    elif COOKIE_URL or COOKIE_FILE_URL:
        try:
            request = urllib.request.Request(
                COOKIE_URL or COOKIE_FILE_URL,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
            with open(COOKIES_FILE, "wb") as f:
                f.write(raw)
            logger.info(
                "Successfully downloaded cookies.txt from configured cookie URL"
            )
        except Exception as e:
            logger.error(f"Failed to download cookies from cookie URL: {e}")

    if os.path.exists(COOKIES_FILE):
        try:
            with open(COOKIES_FILE, "r", encoding="utf-8", errors="ignore") as f:
                cookie_text = f.read(100000)
            valid_cookie_file = (
                "# Netscape HTTP Cookie File" in cookie_text
                or "youtube.com" in cookie_text
                or ".youtube.com" in cookie_text
            )
            if valid_cookie_file:
                logger.info("YouTube cookie file detected and appears valid.")
            else:
                logger.error(
                    "cookies.txt exists but does not look like a Netscape cookie file. "
                    "YouTube authentication will be disabled until valid cookies are supplied."
                )
        except Exception as e:
            logger.error(f"Could not validate cookies.txt: {e}")
    elif USE_COOKIES:
        logger.error(
            "USE_COOKIES=true but no cookies.txt is available. "
            "YouTube downloads requiring authentication will fail."
        )

    # -----------------------------------------
    # Start cleanup worker
    # -----------------------------------------

    cleanup_worker = asyncio.create_task(
        cache_cleanup_task()
    )

    yield

    # -----------------------------------------
    # Shutdown
    # -----------------------------------------

    logger.info(
        "Shutting down MAGMA Music API..."
    )

    cleanup_worker.cancel()


# =========================================================
# FASTAPI APP
# =========================================================

app = FastAPI(
    title="YouTube Downloader & Search API",
    version="2.3.1-Production",
    lifespan=lifespan
)


# =========================================================
# CORS
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)


# =========================================================
# MAGMA.HTML DEVELOPER PORTAL
# =========================================================

HTML_FILE = os.path.join(
    os.path.dirname(
        os.path.abspath(__file__)
    ),
    "Magma.html"
)

try:

    with open(
        HTML_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        DEVELOPER_PORTAL_HTML = f.read()

    logger.info(
        "Magma.html loaded successfully."
    )

except Exception as e:

    logger.error(
        f"Failed to load Magma.html: {e}"
    )

    DEVELOPER_PORTAL_HTML = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>MAGMA API</title>
    </head>
    <body>
        <h1>MAGMA API</h1>
        <p>
            Developer portal could not be loaded.
        </p>
    </body>
    </html>
    """


# =========================================================
# YOUTUBE MUSIC
# =========================================================

ytmusic = YTMusic()


# =========================================================
# VIDEO ID EXTRACTION
# =========================================================

def extract_video_id(
    url: str
) -> Optional[str]:

    """Extracts the 11-character YouTube Video ID."""

    if not url:

        return None

    if re.match(
        r"^[0-9A-Za-z_-]{11}$",
        url
    ):

        return url

    pattern = (
        r"(?:youtu\.be\/|v=|\/shorts\/|"
        r"\/embed\/|\/v\/)"
        r"([0-9A-Za-z_-]{11})"
    )

    match = re.search(
        pattern,
        url
    )

    if match:

        return match.group(1)

    match = re.search(
        r"[0-9A-Za-z_-]{11}",
        url
    )

    return (
        match.group(0)
        if match
        else None
    )


# =========================================================
# BASE YT-DLP OPTIONS
# =========================================================

def get_base_ydl_opts() -> Dict[str, Any]:

    opts = {

        "outtmpl":
            f"{DOWNLOAD_DIR}/%(title).150s_%(id)s.%(ext)s",

        "restrictfilenames":
            True,

        "noplaylist":
            True,

        "quiet":
            True,

        "no_warnings":
            True,

        "retries":
            RETRIES,

        "fragment_retries":
            FRAGMENT_RETRIES,

        "socket_timeout":
            SOCKET_TIMEOUT,

        "continuedl":
            True,

        "js_runtimes":
            {
                "node": {}
            },

        "remote_components":
            [
                "ejs:github"
            ]
    }

    if USE_COOKIES and os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        opts["cookiefile"] = COOKIES_FILE
        logger.info("yt-dlp will use cookies.txt for YouTube authentication")

    # Keep the browser identity stable when cookies are supplied. YouTube can
    # bind sessions to request metadata such as the user agent.
    opts["http_headers"] = {
        "User-Agent": YOUTUBE_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }

    return opts


def apply_youtube_extractor_args(opts: Dict[str, Any]) -> Dict[str, Any]:
    """Configure YouTube clients and the local BgUtils PO-token provider."""
    youtube_args = opts.setdefault("extractor_args", {}).setdefault("youtube", {})

    clients = [c.strip() for c in YOUTUBE_PLAYER_CLIENTS.split(",") if c.strip()]
    if clients and [c.lower() for c in clients] != ["default"]:
        youtube_args["player_client"] = clients

    if YOUTUBE_POT_PROVIDER_URL:
        opts["extractor_args"].setdefault("youtubepot-bgutilhttp", {})["base_url"] = YOUTUBE_POT_PROVIDER_URL

    return opts


# =========================================================
# THUMBNAIL
# =========================================================

def fetch_thumbnail_sync(
    url: str
) -> Dict[str, Any]:

    opts = get_base_ydl_opts()

    opts["skip_download"] = True

    try:

        with yt_dlp.YoutubeDL(
            opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

            return {

                "title":
                    info.get("title"),

                "thumbnail":
                    info.get("thumbnail"),

                "videoId":
                    info.get("id")
            }

    except Exception as e:

        logger.error(
            f"Thumbnail fetch error: {e}"
        )

        raise RuntimeError(
            f"Failed to fetch thumbnail: {str(e)}"
        )


# =========================================================
# AUDIO DOWNLOAD
# =========================================================


# =========================================================
# AUDIO DOWNLOAD
# =========================================================

def download_audio_sync(url: str) -> Dict[str, Any]:
    """Download one audio file with a single yt-dlp path.

    Fast path is intentionally simple: cache first, then yt-dlp + FFmpeg.
    Remote downloader APIs and duplicate fallback downloaders are avoided so
    the API has predictable latency and no hidden 10-15s upstream wait.
    """
    video_id = extract_video_id(url)

    if video_id:
        cached_data = get_cached_metadata(video_id, "mp3")
        if cached_data:
            logger.info(f"⚡ [CACHE HIT] {video_id}")
            return {
                "status": True,
                "title": cached_data["title"],
                "duration": cached_data["duration"],
                "thumbnail": cached_data["thumbnail"],
                "filename": cached_data["file_name"],
                "path": cached_data["file_path"],
                "download_url": f"/files/{cached_data['file_name']}",
                "videoId": video_id,
                "uploader": "Cached",
                "filesize": cached_data["file_size"],
            }

    started = time.perf_counter()
    logger.info(f"⚡ [AUDIO] Starting yt-dlp download: {video_id or url}")

    opts = get_base_ydl_opts()
    opts.update({
        "format": "ba[ext=m4a]/ba[ext=webm]/bestaudio/best",
        "writethumbnail": False,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENT_DOWNLOADS,
        "http_chunk_size": HTTP_CHUNK_SIZE,
        "nocheckcertificate": True,
        "noprogress": True,
        "quiet": True,
        "extractor_args": {"youtube": {"fetch_pot": ["always"]}},
        "no_warnings": True,
        "updatetime": False,
        "clean_infojson": False,
        "retries": min(RETRIES, 3),
        "fragment_retries": min(FRAGMENT_RETRIES, 3),
        "socket_timeout": SOCKET_TIMEOUT,
        "postprocessor_args": ["-threads", "0", "-vn", "-sn"],
    })
    apply_youtube_extractor_args(opts)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

            base_path, _ = os.path.splitext(filename)
            final_path = f"{base_path}.mp3"

            if not os.path.isfile(final_path) or os.path.getsize(final_path) <= 0:
                raise RuntimeError("Downloaded file is missing or empty.")

            size = os.path.getsize(final_path)
            elapsed = time.perf_counter() - started
            logger.info(
                f"✅ [AUDIO SUCCESS] {info.get('id', video_id)}: "
                f"{size / 1048576:.2f} MB in {elapsed:.2f}s"
            )

            response_data = {
                "status": True,
                "title": info.get("title", ""),
                "duration": info.get("duration", 0),
                "thumbnail": info.get("thumbnail", ""),
                "filename": os.path.basename(final_path),
                "path": final_path,
                "download_url": f"/files/{os.path.basename(final_path)}",
                "videoId": info.get("id") or video_id,
                "uploader": info.get("uploader"),
                "filesize": size,
            }
            save_cached_metadata(response_data, "mp3")
            return response_data

    except yt_dlp.utils.DownloadError as e:
        message = str(e)
        logger.error(f"❌ [AUDIO FAILED] yt-dlp: {message}")
        lower = message.lower()
        if "sign in to confirm" in lower or "not a bot" in lower:
            if not (USE_COOKIES and os.path.isfile(COOKIES_FILE)):
                raise RuntimeError(
                    "YouTube requires authentication for this request. "
                    "Set USE_COOKIES=true and provide valid cookies.txt via "
                    "COOKIE_BASE64, COOKIE_URL, or COOKIE_FILE_URL."
                ) from e
            raise RuntimeError(
                "YouTube rejected the supplied session as automated traffic. "
                "The cookies may be expired, invalid, or not usable from this server's IP. "
                "Export fresh YouTube cookies and redeploy them."
            ) from e
        raise RuntimeError(f"Download Error: {message}") from e
    except Exception as e:
        logger.error(f"❌ [AUDIO FAILED] {e}")
        raise RuntimeError(f"Internal Server Error: {str(e)}") from e


# =========================================================
# VIDEO DOWNLOAD
# =========================================================

def download_video_sync(
    url: str
) -> Dict[str, Any]:

    video_id = extract_video_id(
        url
    )

    # -----------------------------------------
    # DATABASE CACHE
    # -----------------------------------------

    if video_id:

        cached_data = get_cached_metadata(
            video_id,
            "mp4"
        )

        if cached_data:

            logger.info(
                f"Database cache hit! "
                f"Returning video for {video_id}"
            )

            return {

                "status":
                    True,

                "title":
                    cached_data["title"],

                "thumbnail":
                    cached_data["thumbnail"],

                "filename":
                    cached_data["file_name"],

                "path":
                    cached_data["file_path"],

                "download_url":
                    f"/files/"
                    f"{cached_data['file_name']}",

                "duration":
                    cached_data["duration"],

                "videoId":
                    video_id,

                "uploader":
                    "Cached",

                "filesize":
                    cached_data["file_size"]
            }

        # -----------------------------------------
        # LEGACY CACHE
        # -----------------------------------------

        legacy_file = find_legacy_cached_file(
            video_id,
            "mp4"
        )

        if legacy_file:

            path = os.path.join(
                DOWNLOAD_DIR,
                legacy_file
            )

            if (
                os.path.isfile(path)
                and
                os.path.getsize(path) > 0
            ):

                logger.info(
                    f"Legacy disk cache hit "
                    f"for {video_id}. "
                    "Saving to DB."
                )

                data = {

                    "videoId":
                        video_id,

                    "title":
                        legacy_file[
                            :-
                            len(
                                f"_{video_id}.mp4"
                            )
                        ],

                    "filename":
                        legacy_file,

                    "path":
                        path,

                    "type":
                        "mp4",

                    "filesize":
                        os.path.getsize(path),

                    "duration":
                        0,

                    "thumbnail":
                        f"https://i.ytimg.com/vi/"
                        f"{video_id}/hqdefault.jpg"
                }

                save_cached_metadata(
                    data,
                    "mp4"
                )

                data["status"] = True

                data["download_url"] = (
                    f"/files/{legacy_file}"
                )

                data["uploader"] = "Cached"

                return data

    # -----------------------------------------
    # ACTUAL DOWNLOAD
    # -----------------------------------------

    logger.info(
        f"Starting video download for: {url}"
    )

    opts = get_base_ydl_opts()

    opts.update({

        "format":
            f"bv*[height<={MAX_VIDEO_QUALITY}]"
            f"[ext=mp4]+ba[ext=m4a]/"
            f"b[height<={MAX_VIDEO_QUALITY}]"
            f"[ext=mp4]/best",

        "merge_output_format":
            "mp4",

        "writethumbnail":
            False,

        "embedthumbnail":
            False,

        # -----------------------------------------
        # ENV CONFIGURABLE SPEED SETTINGS
        # -----------------------------------------

        "concurrent_fragment_downloads":
            CONCURRENT_FRAGMENT_DOWNLOADS,

        "http_chunk_size":
            HTTP_CHUNK_SIZE,

        "nocheckcertificate":
            True,

        "noprogress":
            True,

        "quiet":
            True,

        "no_warnings":
            True,

        "updatetime":
            False,

        "clean_infojson":
            False,

        "retries":
            RETRIES,

        "fragment_retries":
            FRAGMENT_RETRIES,

        "socket_timeout":
            SOCKET_TIMEOUT,

        "postprocessor_args": [

            "-threads",
            "0"
        ]
    })
    apply_youtube_extractor_args(opts)

    try:

        with yt_dlp.YoutubeDL(
            opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

            filename = ydl.prepare_filename(
                info
            )

            base_path, _ = os.path.splitext(
                filename
            )

            final_path = (
                f"{base_path}.mp4"
            )

            # -----------------------------------------
            # Check possible output extensions
            # -----------------------------------------

            for ext in [
                ".mp4",
                ".webm",
                ".mkv"
            ]:

                test_path = (
                    f"{base_path}{ext}"
                )

                if (
                    os.path.isfile(
                        test_path
                    )
                    and
                    os.path.getsize(
                        test_path
                    ) > 0
                ):

                    final_path = test_path

                    break

            if not (
                os.path.isfile(
                    final_path
                )
                and
                os.path.getsize(
                    final_path
                ) > 0
            ):

                raise RuntimeError(
                    "Downloaded file not found "
                    "or is empty."
                )

            logger.info(
                f"Successfully downloaded video: "
                f"{final_path}"
            )

            response_data = {

                "status":
                    True,

                "title":
                    info.get(
                        "title",
                        ""
                    ),

                "thumbnail":
                    info.get(
                        "thumbnail",
                        ""
                    ),

                "filename":
                    os.path.basename(
                        final_path
                    ),

                "path":
                    final_path,

                "download_url":
                    f"/files/"
                    f"{os.path.basename(final_path)}",

                "duration":
                    info.get(
                        "duration",
                        0
                    ),

                "videoId":
                    info.get("id"),

                "uploader":
                    info.get("uploader"),

                "filesize":
                    os.path.getsize(
                        final_path
                    )
            }

            save_cached_metadata(
                response_data,
                "mp4"
            )

            return response_data

    except yt_dlp.utils.DownloadError as e:

        logger.error(
            f"yt-dlp error downloading video "
            f"for {url}: {e}"
        )

        raise RuntimeError(
            f"Download Error: {str(e)}"
        )

    except Exception as e:

        logger.error(
            f"Unexpected error downloading video "
            f"for {url}: {e}"
        )

        raise RuntimeError(
            f"Internal Server Error: {str(e)}"
        )


# =========================================================
# ROOT — DEVELOPER PORTAL
# =========================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
async def root():

    return HTMLResponse(
        content=DEVELOPER_PORTAL_HTML,
        status_code=200
    )


# =========================================================
# YOUTUBE AUTHENTICATION TEST
# =========================================================

@app.get("/youtube-test")
async def youtube_test(
    url: str = Query(..., description="YouTube URL or 11-character video ID"),
    _: bool = Depends(require_api_key),
):
    """Probe YouTube extraction without downloading a file.

    This endpoint is intentionally small and never returns cookies or tokens.
    It distinguishes a cookie/session rejection from a format/PO-token error.
    """
    video_id = extract_video_id(url)
    target = f"https://www.youtube.com/watch?v={video_id}" if video_id else url
    opts = get_base_ydl_opts()
    opts["skip_download"] = True
    opts["noplaylist"] = True
    opts["quiet"] = True
    opts["no_warnings"] = False
    apply_youtube_extractor_args(opts)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
        return {
            "status": "ok",
            "video_id": info.get("id"),
            "title": info.get("title"),
            "formats": len(info.get("formats") or []),
            "cookies_loaded": bool(opts.get("cookiefile")),
            "pot_provider": YOUTUBE_POT_PROVIDER_URL or None,
        }
    except yt_dlp.utils.DownloadError as e:
        message = str(e)
        lower = message.lower()
        if "sign in to confirm" in lower or "not a bot" in lower or "login_required" in lower:
            detail = "YouTube rejected the current session. Check cookies and the PO-token provider."
        elif "po token" in lower or "proof-of-origin" in lower:
            detail = "YouTube requires a PO token; check the BgUtils provider in the Heroku logs."
        else:
            detail = message
        return JSONResponse(status_code=502, content={
            "status": "youtube_error",
            "detail": detail,
            "cookies_loaded": bool(opts.get("cookiefile")),
            "pot_provider": YOUTUBE_POT_PROVIDER_URL or None,
            "video_id": video_id,
        })


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
async def health_check():

    return {

        "status":
            "healthy",

        "version":
            "2.4.0",

        "yt_dlp_version":
            yt_dlp.version.__version__,

        "cache_expiry_hours":
            CACHE_EXPIRE_HOURS,
        "youtube_cookies_enabled": USE_COOKIES and os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0,
        "youtube_client_override": YOUTUBE_PLAYER_CLIENTS if YOUTUBE_PLAYER_CLIENTS.lower() != "default" else "yt-dlp-default",
        "youtube_pot_provider": YOUTUBE_POT_PROVIDER_URL or None,
        "youtube_user_agent_configured": bool(YOUTUBE_USER_AGENT),
    }


# =========================================================
# SEARCH
# =========================================================

@app.get("/search")
async def search_youtube_music(

    _: bool = Depends(require_api_key),

    q: str = Query(
        ...,
        description="Search query"
    ),

    limit: int = Query(
        1,
        description=
            "Number of results to return (max 20)"
    )
):

    try:

        logger.info(
            f"Received search request "
            f"for query '{q}' "
            f"with limit {limit}"
        )

        actual_limit = min(
            max(
                1,
                limit
            ),
            20
        )

        def perform_search():

            return ytmusic.search(
                q,
                filter="songs",
                limit=actual_limit
            )

        results = await asyncio.to_thread(
            perform_search
        )

        formatted_results = []

        for r in results:

            artists = ", ".join(
                [
                    a.get(
                        "name",
                        ""
                    )
                    for a in r.get(
                        "artists",
                        []
                    )
                ]
            )

            thumbnails = r.get(
                "thumbnails",
                []
            )

            thumbnail_url = (
                thumbnails[-1].get(
                    "url"
                )
                if thumbnails
                else None
            )

            formatted_results.append({

                "title":
                    r.get("title"),

                "artist":
                    artists,

                "videoId":
                    r.get("videoId"),

                "duration":
                    r.get("duration"),

                "thumbnail":
                    thumbnail_url
            })

        logger.info(
            f"Successfully completed search "
            f"for query '{q}', "
            f"returned "
            f"{len(formatted_results)} "
            f"result(s)"
        )

        if actual_limit == 1:

            return (
                formatted_results[0]
                if formatted_results
                else {}
            )

        return formatted_results

    except Exception as e:

        logger.error(
            f"Search error for query '{q}': {e}"
        )

        raise HTTPException(
            status_code=500,
            detail={

                "error":
                    "Search failed",

                "message":
                    str(e)
            }
        )


# =========================================================
# THUMBNAIL API
# =========================================================

@app.get("/thumbnail")
async def get_thumbnail(

    _: bool = Depends(require_api_key),

    url: str = Query(
        ...,
        description="YouTube URL"
    )
):

    try:

        result = await asyncio.to_thread(
            fetch_thumbnail_sync,
            url
        )

        return result

    except Exception as e:

        logger.error(
            f"Thumbnail API error: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail={

                "error":
                    "Failed to fetch thumbnail",

                "message":
                    str(e)
            }
        )


# =========================================================
# AUDIO DOWNLOAD API
# =========================================================

@app.get("/download")
async def download_audio(

    _: bool = Depends(require_api_key),

    url: str = Query(
        ...,
        description="YouTube URL"
    ),
    download_type: Optional[str] = Query(
        default=None,
        alias="type",
        description="Use audio for a direct audio stream"
    )
):

    # The music bot calls /download?type=audio and expects audio bytes, not
    # the metadata JSON used by the normal /download endpoint. Route that
    # compatibility form through the same low-latency stream path.
    if (download_type or "").lower() in {"audio", "mp3", "stream"}:
        return await stream_audio(True, url)

    try:

        result = await asyncio.to_thread(
            download_audio_sync,
            url
        )

        return JSONResponse(
            content=result
        )

    except Exception as e:

        logger.error(
            f"Audio download API error: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail={

                "error":
                    "Audio download failed",

                "message":
                    str(e)
            }
        )


# =========================================================
# VIDEO DOWNLOAD API
# =========================================================

@app.get("/video")
async def download_video(

    _: bool = Depends(require_api_key),

    url: str = Query(
        ...,
        description="YouTube URL"
    )
):

    try:

        result = await asyncio.to_thread(
            download_video_sync,
            url
        )

        return JSONResponse(
            content=result
        )

    except Exception as e:

        logger.error(
            f"Video download API error: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail={

                "error":
                    "Video download failed",

                "message":
                    str(e)
            }
        )



# =========================================================
# DIRECT MEDIA STREAMING API
# =========================================================

async def proxy_remote_audio(url: str) -> StreamingResponse:
    """Proxy a remote API's audio stream without buffering it locally."""
    if not REMOTE_API_URL:
        raise RuntimeError("REMOTE_API_URL is not configured")

    started = time.perf_counter()
    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(
            REMOTE_READ_TIMEOUT,
            connect=REMOTE_CONNECT_TIMEOUT,
        ),
    )
    upstream = None

    headers = {"Accept": "audio/mpeg"}
    if REMOTE_API_KEY:
        headers["X-API-Key"] = REMOTE_API_KEY

    try:
        request = client.build_request(
            "GET",
            f"{REMOTE_API_URL}/stream",
            params={"url": url},
            headers=headers,
        )
        upstream = await client.send(request, stream=True)

        header_elapsed = time.perf_counter() - started
        if upstream.status_code >= 400:
            detail = (await upstream.aread()).decode("utf-8", errors="replace")[:1000]
            status_code = upstream.status_code
            await upstream.aclose()
            await client.aclose()
            logger.error(
                f"❌ [REMOTE API FAILED] HTTP {status_code} after "
                f"{header_elapsed:.2f}s: {detail}"
            )
            raise HTTPException(
                status_code=502,
                detail=f"Remote audio API returned HTTP {status_code}",
            )

        response_headers = {"Cache-Control": "no-store"}
        for header_name in (
            "content-length",
            "content-disposition",
            "accept-ranges",
        ):
            value = upstream.headers.get(header_name)
            if value:
                response_headers[header_name] = value

        media_type = upstream.headers.get("content-type", "audio/mpeg")
        logger.info(
            f"🚀 [REMOTE API] Response headers received for "
            f"{extract_video_id(url) or url} after {header_elapsed:.2f}s "
            f"(HTTP {upstream.status_code})"
        )

        first_chunk = True

        async def body_iterator():
            nonlocal first_chunk
            transferred = 0
            try:
                async for chunk in upstream.aiter_bytes(
                    chunk_size=REMOTE_CHUNK_SIZE
                ):
                    if first_chunk:
                        first_chunk = False
                        logger.info(
                            f"⚡ [REMOTE API] First audio bytes received for "
                            f"{extract_video_id(url) or url} after "
                            f"{time.perf_counter() - started:.2f}s"
                        )
                    transferred += len(chunk)
                    yield chunk
            finally:
                total_elapsed = time.perf_counter() - started
                logger.info(
                    f"🏁 [REMOTE API] Transfer finished for "
                    f"{extract_video_id(url) or url}: "
                    f"{transferred / 1048576:.2f} MB in "
                    f"{total_elapsed:.2f}s"
                )
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            body_iterator(),
            status_code=upstream.status_code,
            media_type=media_type,
            headers=response_headers,
        )
    except HTTPException:
        raise
    except Exception as e:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        logger.error(f"❌ [REMOTE API ERROR] {e}")
        raise HTTPException(
            status_code=502,
            detail="Remote audio API request failed",
        ) from e


@app.get("/stream")
async def stream_audio(
    _: bool = Depends(require_api_key),
    url: str = Query(..., description="YouTube URL or video ID")
):
    """Download audio on the server and stream the finished MP3 directly.

    This endpoint is optimized for music bots: one HTTP request and no
    intermediate JSON -> /files round trip.
    """
    if REMOTE_API_URL:
        return await proxy_remote_audio(url)

    try:
        result = await asyncio.to_thread(download_audio_sync, url)
        if not result or not result.get("status"):
            raise HTTPException(status_code=500, detail="Audio download failed")

        file_path = result.get("path")
        filename = os.path.basename(result.get("filename") or file_path or "audio.mp3")
        if not file_path or not os.path.isfile(file_path):
            raise HTTPException(status_code=404, detail="Downloaded audio file not found")

        file_size = os.path.getsize(file_path)
        if file_size <= 1024:
            raise HTTPException(status_code=500, detail="Downloaded audio file is empty")

        logger.info(f"Direct audio stream ready: {filename} ({file_size / 1048576:.2f} MB)")
        return FileResponse(
            path=file_path,
            filename=filename,
            media_type="audio/mpeg",
            headers={"Cache-Control": "no-store"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Direct audio stream error for {url}")
        detail = {"error": "Audio streaming failed"}
        if DEBUG_ERRORS:
            detail["message"] = str(e)
        raise HTTPException(status_code=500, detail=detail)


@app.get("/video-stream")
async def stream_video(
    _: bool = Depends(require_api_key),
    url: str = Query(..., description="YouTube URL or video ID")
):
    """Download video on the server and stream the finished file directly."""
    try:
        result = await asyncio.to_thread(download_video_sync, url)
        if not result or not result.get("status"):
            raise HTTPException(status_code=500, detail="Video download failed")

        file_path = result.get("path")
        filename = os.path.basename(result.get("filename") or file_path or "video.mp4")
        if not file_path or not os.path.isfile(file_path):
            raise HTTPException(status_code=404, detail="Downloaded video file not found")

        file_size = os.path.getsize(file_path)
        if file_size <= 1024:
            raise HTTPException(status_code=500, detail="Downloaded video file is empty")

        logger.info(f"Direct video stream ready: {filename} ({file_size / 1048576:.2f} MB)")
        return FileResponse(
            path=file_path,
            filename=filename,
            media_type="video/mp4",
            headers={"Cache-Control": "no-store"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Direct video stream error for {url}: {e}")
        raise HTTPException(status_code=500, detail="Video streaming failed")

# =========================================================
# FILE SERVING
# =========================================================

@app.get("/files/{filename}")
async def get_file(
    filename: str,
    _: bool = Depends(require_api_key)
):

    filename = os.path.basename(
        filename
    )

    file_path = os.path.join(
        DOWNLOAD_DIR,
        filename
    )

    if not os.path.isfile(
        file_path
    ):

        logger.warning(
            f"Requested file not found: "
            f"{filename}"
        )

        raise HTTPException(
            status_code=404,
            detail="File not found"
        )

    return FileResponse(
        path=file_path,
        filename=filename
    )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=PORT,
        reload=False
    )