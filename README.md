# VelocityBots API

VelocityBots is a cache-first FastAPI media API for music bots and small
applications. It is based on the useful parts of the supplied downloader
reference, but has fresh branding, a smaller code path, URL validation, safer
file handling, and no copied cookie file.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /` | Developer portal |
| `GET /health` | Public health check |
| `GET /download?url=...` | Fast cache-first MP3 metadata response |
| `POST /download` | Same endpoint for JSON bot clients |
| `GET /download?url=...&live=true` | Progressive MP3 while downloading |
| `GET /stream?url=...` | Fast cache-first direct MP3 response |
| `GET /live?url=...` | Progressive MP3 while downloading |
| `GET /video?url=...` | Video metadata response |
| `GET /video-stream?url=...` | Direct MP4 response |
| `GET /search?q=...&limit=...` | YouTube Music search |
| `GET /thumbnail?url=...` | Video thumbnail metadata |
| `GET /files/{filename}` | Serve a cached file |
| `GET /docs` | OpenAPI / Swagger docs |

`/download` accepts `url`, `video`, `link`, `video_url`, or `videoId`. If a bot
sends a search phrase instead, it accepts `q`, `query`, `song`, or `search` and
resolves the first YouTube Music result. Add `direct=true` when the bot expects
the endpoint itself to return the complete MP3 bytes instead of JSON. Add
`live=true` when playback should begin as soon as MP3 bytes are available,
before the full song finishes downloading. Progressive mode is not cached;
use the normal endpoint when the next request should be instant from cache.

The JSON response keeps the canonical `download_url` field and also includes
common compatibility aliases: `url`, `file_url`, `audio_url`, and
`downloadUrl`, plus `stream_url`/`streamUrl` for progressive playback. Set
`PUBLIC_BASE_URL` when running behind a proxy so these are absolute URLs
reachable by Telegram bot servers.

All endpoints except `/`, `/health`, and `/docs` require the configured
`API_KEY`. Prefer `X-API-Key: your-key`; `Authorization: Bearer your-key` and
the legacy `?api_key=your-key` form are also accepted.

## Run locally

1. Install FFmpeg.
2. Create `.env` from `.env.example` and set a strong `API_KEY`.
3. Install Python dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Start the API:

   ```bash
   ./start.sh
   ```

Then open `http://localhost:8000/`.

## Example

```bash
curl -H "X-API-Key: change-this-to-a-long-random-key" \
  "http://localhost:8000/download?url=https://youtu.be/VIDEO_ID"
```

Search-based compatibility request:

```bash
curl -H "X-API-Key: change-this-to-a-long-random-key" \
  "http://localhost:8000/download?q=Never%20Gonna%20Give%20You%20Up"
```

JSON POST compatibility request:

```bash
curl -X POST -H "Content-Type: application/json" \
  -H "X-API-Key: change-this-to-a-long-random-key" \
  -d '{"url":"https://youtu.be/VIDEO_ID"}' \
  "http://localhost:8000/download"
```

The first request downloads and converts the audio to MP3. Repeated requests
for the same YouTube video reuse the local cache. `/stream` skips the JSON-to-
file follow-up when a bot wants the audio bytes directly. The default audio
quality is 192 kbps and can be changed with `AUDIO_QUALITY`; transfer speed
still depends on the source, server, Telegram, and client networks.

## Docker

```bash
docker build -t velocitybots .
docker run --rm -p 8000:8000 --env-file .env velocitybots
```

## Heroku

Heroku deployment instructions, including FFmpeg setup, Docker Container Stack,
config vars, and the optional private `COOKIE_URL`, are in
`README_HEROKU.md`.