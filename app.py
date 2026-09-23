"""VelocityBots - fast YouTube audio/video API.

The API is intentionally small and predictable:
  /download       cache-first audio metadata
  /stream         cache-first direct MP3 response
  /video          video metadata
  /video-stream   direct MP4 response
  /search         YouTube Music search

Set API_KEY before exposing this service publicly.  FFmpeg is required for
audio conversion and video merging.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

import yt_dlp
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from ytmusicapi import YTMusic


load_dotenv()

SERVICE_NAME = "VelocityBots"
VERSION = "1.0.0"
PORT = int(os.getenv("PORT", "8000"))
API_KEY = os.getenv("API_KEY", "").strip()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads")).resolve()
DB_FILE = Path(os.getenv("DB_FILE", "velocitybots.db")).resolve()
COOKIE_FILE = Path(os.getenv("COOKIE_FILE", "cookies.txt")).resolve()
COOKIE_URL = os.getenv("COOKIE_URL", "").strip()
CACHE_EXPIRE_HOURS = float(os.getenv("CACHE_EXPIRE_HOURS", "0"))
MAX_VIDEO_QUALITY = int(os.getenv("MAX_VIDEO_QUALITY", "720"))
CONCURRENT_FRAGMENT_DOWNLOADS = int(
    os.getenv("CONCURRENT_FRAGMENT_DOWNLOADS", "30")
)
HTTP_CHUNK_SIZE = int(os.getenv("HTTP_CHUNK_SIZE", "10485760"))
SOCKET_TIMEOUT = int(os.getenv("SOCKET_TIMEOUT", "20"))
RETRIES = int(os.getenv("RETRIES", "3"))
FRAGMENT_RETRIES = int(os.getenv("FRAGMENT_RETRIES", "3"))

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("velocitybots")


def init_db() -> None:
    with sqlite3.connect(DB_FILE, timeout=15) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                video_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                title TEXT,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                duration INTEGER NOT NULL DEFAULT 0,
                thumbnail TEXT,
                uploader TEXT,
                created_at REAL NOT NULL,
                PRIMARY KEY (video_id, media_type)
            )
            """
        )
        connection.commit()


def cleanup_expired_cache() -> None:
    if CACHE_EXPIRE_HOURS <= 0:
        return

    cutoff = time.time() - (CACHE_EXPIRE_HOURS * 3600)
    with sqlite3.connect(DB_FILE, timeout=15) as connection:
        rows = connection.execute(
            "SELECT file_path FROM downloads WHERE created_at < ?",
            (cutoff,),
        ).fetchall()
        for (file_path,) in rows:
            try:
                Path(file_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove expired file: %s", file_path)
        connection.execute("DELETE FROM downloads WHERE created_at < ?", (cutoff,))
        connection.commit()


async def cache_worker() -> None:
    while True:
        await asyncio.sleep(900)
        try:
            await asyncio.to_thread(cleanup_expired_cache)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Cache cleanup failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if COOKIE_URL:
        try:
            urllib.request.urlretrieve(COOKIE_URL, COOKIE_FILE)
            logger.info("Downloaded configured cookie file")
        except Exception:
            logger.exception("Could not download COOKIE_URL")

    worker = asyncio.create_task(cache_worker())
    yield
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="VelocityBots API",
    description="Fast, cache-first media downloads for bots and applications.",
    version=VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "*").split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
    api_key: Optional[str] = Query(
        default=None,
        description="Legacy compatibility. Prefer X-API-Key.",
    ),
) -> bool:
    """Accept the two common bot auth headers plus legacy query auth."""
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail="API_KEY is not configured on the server.",
        )

    supplied = (x_api_key or api_key or "").strip()
    if not supplied and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied = token.strip()

    if supplied != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
    return True


def extract_video_id(value: str) -> Optional[str]:
    value = value.strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", value):
        return value
    match = re.search(
        r"(?:youtu\.be/|[?&]v=|/shorts/|/embed/|/live/|/v/)"
        r"([0-9A-Za-z_-]{11})",
        value,
    )
    return match.group(1) if match else None


def normalize_url(value: str) -> str:
    """Accept a video ID or a YouTube URL, and reject unrelated URLs."""
    video_id = extract_video_id(value)
    if video_id and not re.match(r"^https?://", value, re.I):
        return f"https://www.youtube.com/watch?v={video_id}"

    parsed = urlparse(value)
    host = parsed.netloc.lower().split(":")[0]
    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
    if parsed.scheme not in {"http", "https"} or host not in allowed_hosts:
        raise HTTPException(
            status_code=422,
            detail="Only YouTube URLs or 11-character YouTube video IDs are supported.",
        )
    if not extract_video_id(value):
        raise HTTPException(status_code=422, detail="The YouTube video ID is missing.")
    return value


def base_ydl_options() -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "outtmpl": str(DOWNLOAD_DIR / "%(title).150s_%(id)s.%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "continuedl": True,
        "retries": RETRIES,
        "fragment_retries": FRAGMENT_RETRIES,
        "socket_timeout": SOCKET_TIMEOUT,
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENT_DOWNLOADS,
        "http_chunk_size": HTTP_CHUNK_SIZE,
        "nocheckcertificate": True,
    }
    if COOKIE_FILE.is_file():
        options["cookiefile"] = str(COOKIE_FILE)
    return options


def cached_record(video_id: str, media_type: str) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(DB_FILE, timeout=15) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT video_id, media_type, title, file_name, file_path, file_size,
                   duration, thumbnail, uploader, created_at
            FROM downloads
            WHERE video_id = ? AND media_type = ?
            """,
            (video_id, media_type),
        ).fetchone()

    if not row:
        return None
    if CACHE_EXPIRE_HOURS > 0 and (
        time.time() - row["created_at"] > CACHE_EXPIRE_HOURS * 3600
    ):
        return None
    if not Path(row["file_path"]).is_file() or row["file_size"] <= 0:
        return None
    return dict(row)


def save_record(data: Dict[str, Any], media_type: str) -> None:
    with sqlite3.connect(DB_FILE, timeout=15) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO downloads
            (video_id, media_type, title, file_name, file_path, file_size,
             duration, thumbnail, uploader, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["videoId"],
                media_type,
                data.get("title", ""),
                data["filename"],
                data["path"],
                data.get("filesize", 0),
                data.get("duration", 0) or 0,
                data.get("thumbnail", ""),
                data.get("uploader", ""),
                time.time(),
            ),
        )
        connection.commit()


def record_response(record: Dict[str, Any], *, cached: bool = True) -> Dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "status": True,
        "cached": cached,
        "title": record.get("title", ""),
        "duration": record.get("duration", 0) or 0,
        "thumbnail": record.get("thumbnail", ""),
        "filename": record["file_name"] if "file_name" in record else record["filename"],
        "path": record["file_path"] if "file_path" in record else record["path"],
        "download_url": (
            "/files/"
            + (
                record["file_name"]
                if "file_name" in record
                else record["filename"]
            )
        ),
        "videoId": record["video_id"] if "video_id" in record else record["videoId"],
        "uploader": record.get("uploader", ""),
        "filesize": record.get("file_size", record.get("filesize", 0)),
    }


def absolute_url(request: Request, path: str) -> str:
    """Return a URL Telegram bots can fetch from outside this process."""
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    base_url = public_base_url or str(request.base_url).rstrip("/")
    return f"{base_url}/{path.lstrip('/')}"


def telegram_compatible_response(
    result: Dict[str, Any], request: Request
) -> Dict[str, Any]:
    """Add the response aliases used by common Telegram music bots.

    The canonical VelocityBots field is ``download_url``.  The aliases are
    intentionally harmless and make migration easier for bots that already
    expect a different downloader response shape.
    """
    file_url = absolute_url(request, result["download_url"])
    live_url = (
        absolute_url(request, "/download")
        + "?url="
        + quote(result["videoId"])
        + "&live=true"
    )
    response = dict(result)
    response.update(
        {
            "success": True,
            "ok": True,
            "url": file_url,
            "file_url": file_url,
            "audio_url": file_url,
            "downloadUrl": file_url,
            "download_url": file_url,
            "stream_url": live_url,
            "streamUrl": live_url,
            "file_name": result["filename"],
            "fileSize": result["filesize"],
            "video_id": result["videoId"],
            "mime_type": "audio/mpeg",
            "type": "audio",
        }
    )
    return response


def live_audio_generator(url: str):
    """Yield MP3 bytes while yt-dlp downloads and FFmpeg encodes.

    This deliberately does not write to the normal cache: a progressive
    response must begin immediately, while the cache requires a complete,
    verified file. Bots that need cache hits should use /stream or /download.
    """
    ytdlp_args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        "--no-progress",
        "--retries",
        str(RETRIES),
        "--fragment-retries",
        str(FRAGMENT_RETRIES),
        "--socket-timeout",
        str(SOCKET_TIMEOUT),
        "--concurrent-fragments",
        str(CONCURRENT_FRAGMENT_DOWNLOADS),
        "--http-chunk-size",
        str(HTTP_CHUNK_SIZE),
        "--format",
        "ba[ext=m4a]/ba[ext=webm]/bestaudio/best",
        "--output",
        "-",
        url,
    ]
    if COOKIE_FILE.is_file():
        ytdlp_args[2:2] = ["--cookies", str(COOKIE_FILE)]

    ytdlp_process = subprocess.Popen(
        ytdlp_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    ffmpeg_process = subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-i",
            "pipe:0",
            "-vn",
            "-sn",
            "-f",
            "mp3",
            "-b:a",
            os.getenv("AUDIO_QUALITY", "192") + "k",
            "-flush_packets",
            "1",
            "pipe:1",
        ],
        stdin=ytdlp_process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if ytdlp_process.stdout:
        ytdlp_process.stdout.close()

    try:
        if not ffmpeg_process.stdout:
            return
        while True:
            chunk = ffmpeg_process.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
        ffmpeg_process.wait()
        ytdlp_process.wait()
        if ffmpeg_process.returncode != 0 or ytdlp_process.returncode != 0:
            logger.error(
                "Live audio process failed: yt-dlp=%s ffmpeg=%s",
                ytdlp_process.returncode,
                ffmpeg_process.returncode,
            )
    finally:
        for process in (ffmpeg_process, ytdlp_process):
            if process.poll() is None:
                process.kill()
        if ffmpeg_process.stdout:
            ffmpeg_process.stdout.close()


def live_audio_response(url: str) -> StreamingResponse:
    return StreamingResponse(
        live_audio_generator(url),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="velocitybots.mp3"',
            "X-Accel-Buffering": "no",
        },
    )


async def resolve_download_input(
    url: Optional[str],
    query: Optional[str],
) -> str:
    """Resolve either a URL/ID or a search phrase to a YouTube URL."""
    source = (url or "").strip()
    if source:
        return normalize_url(source)

    phrase = (query or "").strip()
    if not phrase:
        raise HTTPException(
            status_code=422,
            detail="Send one of: url, video, link, q, query, song, or search.",
        )

    try:
        results = await asyncio.to_thread(
            lambda: YTMusic().search(phrase, filter="songs", limit=1)
        )
        if not results or not results[0].get("videoId"):
            raise HTTPException(
                status_code=404,
                detail="No matching song was found.",
            )
        return normalize_url(results[0]["videoId"])
    except HTTPException:
        raise
    except Exception as exc:
        raise error_response("Could not resolve the search query", exc)


def download_audio_sync(url: str) -> Dict[str, Any]:
    url = normalize_url(url)
    video_id = extract_video_id(url)
    if video_id:
        cached = cached_record(video_id, "mp3")
        if cached:
            logger.info("Audio cache hit: %s", video_id)
            return record_response(cached, cached=True)

    started = time.perf_counter()
    options = base_ydl_options()
    options.update(
        {
            "format": "ba[ext=m4a]/ba[ext=webm]/bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": os.getenv("AUDIO_QUALITY", "192"),
                }
            ],
            "postprocessor_args": ["-threads", "0", "-vn", "-sn"],
        }
    )

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            prepared = Path(ydl.prepare_filename(info))
            final_path = prepared.with_suffix(".mp3")
            if not final_path.is_file() or final_path.stat().st_size <= 0:
                raise RuntimeError("The MP3 file was not created.")

        result = {
            "title": info.get("title", ""),
            "duration": info.get("duration", 0) or 0,
            "thumbnail": info.get("thumbnail", ""),
            "filename": final_path.name,
            "path": str(final_path),
            "videoId": info.get("id") or video_id,
            "uploader": info.get("uploader", ""),
            "filesize": final_path.stat().st_size,
        }
        save_record(result, "mp3")
        logger.info(
            "Audio ready: %s in %.2fs",
            result["videoId"],
            time.perf_counter() - started,
        )
        return record_response(result, cached=False)
    except yt_dlp.utils.DownloadError as exc:
        raise RuntimeError(f"Download error: {exc}") from exc


def download_video_sync(url: str) -> Dict[str, Any]:
    url = normalize_url(url)
    video_id = extract_video_id(url)
    if video_id:
        cached = cached_record(video_id, "mp4")
        if cached:
            logger.info("Video cache hit: %s", video_id)
            return record_response(cached, cached=True)

    options = base_ydl_options()
    options.update(
        {
            "format": (
                f"bv*[height<={MAX_VIDEO_QUALITY}][ext=mp4]+ba[ext=m4a]/"
                f"b[height<={MAX_VIDEO_QUALITY}][ext=mp4]/best"
            ),
            "merge_output_format": "mp4",
            "postprocessor_args": ["-threads", "0"],
        }
    )

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            prepared = Path(ydl.prepare_filename(info))
            candidates = [
                prepared.with_suffix(".mp4"),
                prepared.with_suffix(".webm"),
                prepared.with_suffix(".mkv"),
            ]
            final_path = next(
                (candidate for candidate in candidates if candidate.is_file()),
                None,
            )
            if not final_path or final_path.stat().st_size <= 0:
                raise RuntimeError("The video file was not created.")

        result = {
            "title": info.get("title", ""),
            "duration": info.get("duration", 0) or 0,
            "thumbnail": info.get("thumbnail", ""),
            "filename": final_path.name,
            "path": str(final_path),
            "videoId": info.get("id") or video_id,
            "uploader": info.get("uploader", ""),
            "filesize": final_path.stat().st_size,
        }
        save_record(result, "mp4")
        return record_response(result, cached=False)
    except yt_dlp.utils.DownloadError as exc:
        raise RuntimeError(f"Download error: {exc}") from exc


def error_response(message: str, exc: Exception) -> HTTPException:
    logger.exception("%s", message)
    return HTTPException(
        status_code=500,
        detail={"error": message, "message": str(exc)},
    )


PORTAL_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>VelocityBots API</title>
  <style>
    :root{color-scheme:dark;--bg:#071019;--panel:#0d1b28;--line:#1d3446;
      --text:#e9f4fb;--muted:#8da5b7;--cyan:#43e0ff;--lime:#b8f36b}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% 0,#123a4f 0,transparent 35%),var(--bg);
      color:var(--text);font:15px/1.6 Inter,system-ui,sans-serif}
    main{max-width:1000px;margin:auto;padding:28px 20px 64px}.nav{display:flex;justify-content:space-between;align-items:center;
      padding:8px 0 54px}.brand{font-weight:800;font-size:20px;letter-spacing:-.5px}.brand b{color:var(--cyan)}
    .badge{border:1px solid #2b5265;border-radius:99px;padding:5px 11px;color:var(--lime);font-size:12px}
    .hero{max-width:720px;padding:20px 0 48px}.eyebrow{color:var(--cyan);font:700 12px ui-monospace,monospace;letter-spacing:1.8px;text-transform:uppercase}
    h1{font-size:clamp(42px,8vw,78px);line-height:.98;letter-spacing:-4px;margin:13px 0 20px}
    h1 span{color:var(--cyan)}.hero p{color:var(--muted);font-size:17px;max-width:610px}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}.card{background:#0b1824cc;border:1px solid var(--line);border-radius:16px;padding:20px}
    .card h2{font-size:16px;margin:0 0 12px}.route{display:flex;gap:10px;align-items:center;font:700 14px ui-monospace,monospace}
    .method{color:var(--lime);font-size:11px}.route code{color:var(--cyan)}.card p{color:var(--muted);margin:8px 0 0;font-size:13px}
    pre{overflow:auto;background:#061019;border:1px solid var(--line);border-radius:12px;padding:16px;color:#c6e5f0;font:13px/1.6 ui-monospace,monospace}
    footer{color:var(--muted);font-size:12px;margin-top:40px}
  </style>
</head>
<body><main>
  <nav class="nav"><div class="brand">Velocity<b>Bots</b></div><div class="badge">● API online</div></nav>
  <section class="hero"><div class="eyebrow">Fast media infrastructure</div>
    <h1>Download at the speed of <span>motion.</span></h1>
    <p>VelocityBots is a cache-first YouTube media API for music bots, apps, and automations. One key, predictable endpoints, no remote downloader hop.</p>
  </section>
  <section class="grid">
    <article class="card"><h2>Audio metadata</h2><div class="route"><span class="method">GET</span><code>/download</code></div><p>Downloads or reuses an MP3 and returns its metadata plus a file URL.</p></article>
    <article class="card"><h2>Direct audio</h2><div class="route"><span class="method">GET</span><code>/stream</code></div><p>Downloads or reuses an MP3 and streams it in the same request.</p></article>
    <article class="card"><h2>Search</h2><div class="route"><span class="method">GET</span><code>/search?q=...</code></div><p>Searches YouTube Music for songs and returns bot-friendly metadata.</p></article>
    <article class="card"><h2>Video</h2><div class="route"><span class="method">GET</span><code>/video</code></div><p>Returns cached or newly downloaded video metadata. Use /video-stream for the file.</p></article>
  </section>
  <section class="card" style="margin-top:14px"><h2>Quick start</h2>
    <pre>curl -H "X-API-Key: YOUR_KEY" \
  "https://YOUR_HOST/download?url=https://youtu.be/VIDEO_ID"</pre>
    <p>Interactive OpenAPI docs: <a href="/docs" style="color:var(--cyan)">/docs</a></p>
  </section>
  <footer>VelocityBots API v1 · API key required for media endpoints · FFmpeg powered</footer>
</main></body></html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root() -> HTMLResponse:
    return HTMLResponse(PORTAL_HTML)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "status": "healthy",
        "version": VERSION,
        "cache_expiry_hours": CACHE_EXPIRE_HOURS,
    }


@app.get("/search")
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(1, ge=1, le=20),
    _: bool = Depends(require_api_key),
) -> Any:
    try:
        results = await asyncio.to_thread(
            lambda: YTMusic().search(q, filter="songs", limit=limit)
        )
        formatted = []
        for item in results:
            thumbnails = item.get("thumbnails") or []
            formatted.append(
                {
                    "title": item.get("title", ""),
                    "artist": ", ".join(
                        artist.get("name", "")
                        for artist in item.get("artists", [])
                    ),
                    "videoId": item.get("videoId"),
                    "duration": item.get("duration"),
                    "thumbnail": thumbnails[-1].get("url") if thumbnails else None,
                }
            )
        return formatted[0] if limit == 1 and formatted else (
            {} if limit == 1 else formatted
        )
    except Exception as exc:
        raise error_response("Search failed", exc)


@app.get("/thumbnail")
async def thumbnail(
    url: str = Query(..., min_length=1, max_length=2048),
    _: bool = Depends(require_api_key),
) -> Dict[str, Any]:
    try:
        normalized = normalize_url(url)
        options = base_ydl_options()
        options["skip_download"] = True
        info = await asyncio.to_thread(
            lambda: yt_dlp.YoutubeDL(options).extract_info(
                normalized, download=False
            )
        )
        return {
            "service": SERVICE_NAME,
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "videoId": info.get("id"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise error_response("Thumbnail lookup failed", exc)


@app.get("/download")
@app.post("/download")
@app.get("/api/download")
@app.post("/api/download")
async def download(
    request: Request,
    url: Optional[str] = Query(
        default=None,
        max_length=2048,
        description="YouTube URL or video ID",
    ),
    video: Optional[str] = Query(
        default=None,
        max_length=2048,
        description="Compatibility alias for url",
    ),
    link: Optional[str] = Query(
        default=None,
        max_length=2048,
        description="Compatibility alias for url",
    ),
    q: Optional[str] = Query(
        default=None,
        max_length=300,
        description="Compatibility alias for query",
    ),
    query: Optional[str] = Query(
        default=None,
        max_length=300,
        description="Song search phrase",
    ),
    song: Optional[str] = Query(
        default=None,
        max_length=300,
        description="Compatibility alias for query",
    ),
    search: Optional[str] = Query(
        default=None,
        max_length=300,
        description="Compatibility alias for query",
    ),
    direct: bool = Query(
        default=False,
        description="Return the audio file instead of JSON metadata",
    ),
    live: bool = Query(
        default=False,
        description="Start sending MP3 bytes while the source is still downloading",
    ),
    payload: Optional[Dict[str, Any]] = Body(
        default=None,
        description="Optional JSON body for POST clients",
    ),
    _: bool = Depends(require_api_key),
) -> Any:
    payload = payload or {}
    source_url = (
        url
        or video
        or link
        or payload.get("url")
        or payload.get("video")
        or payload.get("link")
        or payload.get("video_url")
        or payload.get("videoId")
    )
    source_query = (
        q
        or query
        or song
        or search
        or payload.get("q")
        or payload.get("query")
        or payload.get("song")
        or payload.get("search")
    )
    try:
        resolved_url = await resolve_download_input(source_url, source_query)
        if live or str(payload.get("live", "")).lower() in {
            "1",
            "true",
            "yes",
        }:
            return live_audio_response(resolved_url)
        result = await asyncio.to_thread(download_audio_sync, resolved_url)
        if direct or str(payload.get("direct", "")).lower() in {
            "1",
            "true",
            "yes",
        }:
            return media_file_response(result, "audio/mpeg")
        return JSONResponse(telegram_compatible_response(result, request))
    except HTTPException:
        raise
    except Exception as exc:
        raise error_response("Audio download failed", exc)


@app.get("/video")
async def video(
    url: str = Query(..., min_length=1, max_length=2048),
    _: bool = Depends(require_api_key),
) -> JSONResponse:
    try:
        return JSONResponse(await asyncio.to_thread(download_video_sync, url))
    except HTTPException:
        raise
    except Exception as exc:
        raise error_response("Video download failed", exc)


async def direct_file(
    url: str, *, media_type: str, downloader: Any
) -> FileResponse:
    try:
        result = await asyncio.to_thread(downloader, url)
        return media_file_response(result, media_type)
    except HTTPException:
        raise
    except Exception as exc:
        raise error_response("Media streaming failed", exc)


def media_file_response(result: Dict[str, Any], media_type: str) -> FileResponse:
    path = Path(result["path"]).resolve()
    if DOWNLOAD_DIR not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Downloaded file not found.")
    return FileResponse(
        path=path,
        filename=path.name,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/stream")
async def stream(
    url: str = Query(..., min_length=1, max_length=2048),
    _: bool = Depends(require_api_key),
) -> FileResponse:
    return await direct_file(url, media_type="audio/mpeg", downloader=download_audio_sync)


@app.get("/live")
async def live(
    url: str = Query(..., min_length=1, max_length=2048),
    _: bool = Depends(require_api_key),
) -> StreamingResponse:
    return live_audio_response(normalize_url(url))


@app.get("/video-stream")
async def video_stream(
    url: str = Query(..., min_length=1, max_length=2048),
    _: bool = Depends(require_api_key),
) -> FileResponse:
    return await direct_file(url, media_type="video/mp4", downloader=download_video_sync)


@app.get("/files/{filename}")
async def files(
    filename: str,
    _: bool = Depends(require_api_key),
) -> FileResponse:
    safe_name = Path(filename).name
    path = (DOWNLOAD_DIR / safe_name).resolve()
    if path.parent != DOWNLOAD_DIR or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(
        path=path,
        filename=safe_name,
        headers={"Cache-Control": "public, max-age=3600"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)