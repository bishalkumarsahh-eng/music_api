# MAGMA Music API

Lean FastAPI YouTube/YouTube Music downloader API for music-bot backends.

## Audio path
1. Check SQLite cache.
2. If cached, return immediately.
3. Otherwise use one yt-dlp + FFmpeg download path.
4. Save the finished MP3 and metadata to cache.

There is no remote downloader dependency or duplicate audio fallback path. This avoids hidden upstream TTFB delays.

### Optional fast upstream

Set `REMOTE_API_URL` to use another Magma-compatible API for `/stream`. The
proxy forwards the request headers and audio bytes as they arrive, so it does
not wait for the complete MP3 before sending the first byte:

```env
REMOTE_API_URL=https://your-fast-api.example.com
REMOTE_API_KEY=your-remote-api-key
```

When `REMOTE_API_URL` is empty, `/stream` keeps using the local yt-dlp +
FFmpeg path. The remote API key is read from the environment and is never
returned to callers.

YouTube cookies are disabled by default because stale or IP-bound cookies can
cause YouTube's `The page needs to be reloaded` error. Set
`USE_COOKIES=true` only when a restricted video requires the cookie file.

For temporary troubleshooting, set `DEBUG_ERRORS=true` to include the
underlying downloader error in the API response. Turn it back off after
diagnosis.

## Endpoints
- `GET /` — developer portal
- `GET /health` — health/status
- `GET /search?query=...` — YouTube Music search
- `GET /thumbnail?url=...` — thumbnail metadata
- `GET /download?url=...` — JSON metadata for downloaded MP3
- `GET /download?url=...&type=audio` — direct audio compatibility stream
- `GET /stream?url=...` — direct MP3 response
- `GET /video?url=...` — video download metadata
- `GET /video-stream?url=...` — direct video response
- `GET /files/{filename}` — cached file response

All protected endpoints require `X-API-Key`, `Authorization: Bearer ...`, or the legacy `api_key` query parameter.

## Docker
```bash
docker build -t magma-api .
docker run -d --name magma-api -p 8000:8000 --env-file .env magma-api
```

The container installs FFmpeg and Node.js for yt-dlp's JavaScript challenge support.
