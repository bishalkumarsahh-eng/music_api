# Deploy VelocityBots on Heroku

VelocityBots can run on Heroku using either the normal buildpack stack or the
Docker/Container Stack. The Docker path is the most predictable because the
included `Dockerfile` installs FFmpeg inside the image.

## Option A: Docker / Container Stack

From the project directory:

```bash
heroku login
heroku create your-velocitybots-name
heroku stack:set container -a your-velocitybots-name
heroku container:login
heroku container:push web -a your-velocitybots-name
heroku container:release web -a your-velocitybots-name
```

Set the required config vars:

```bash
heroku config:set \
  API_KEY="create-a-long-private-key" \
  PUBLIC_BASE_URL="https://your-velocitybots-name.herokuapp.com" \
  -a your-velocitybots-name
```

For the optional private cookie URL:

```bash
heroku config:set \
  COOKIE_URL="https://private-host.example/cookies.txt" \
  COOKIE_FILE="cookies.txt" \
  -a your-velocitybots-name
```

Check the service:

```bash
curl https://your-velocitybots-name.herokuapp.com/health
```

## Option B: Heroku buildpacks

The repository includes `Aptfile` with FFmpeg and `app.json` with the buildpack
configuration. If you configure buildpacks manually, use this order:

```bash
heroku buildpacks:clear -a your-velocitybots-name
heroku buildpacks:add --index 1 \
  https://github.com/heroku/heroku-buildpack-apt \
  -a your-velocitybots-name
heroku buildpacks:add --index 2 heroku/python -a your-velocitybots-name
git push heroku main
```

Then set the same `API_KEY`, `PUBLIC_BASE_URL`, and optional `COOKIE_URL`
config vars from Option A. The included `Procfile` starts the web process on
Heroku's assigned `$PORT`.

## Telegram bot URL

For progressive playback:

```text
https://your-velocitybots-name.herokuapp.com/download?url=YOUTUBE_URL&live=true
```

Send the API key using the `X-API-Key` header. The normal `/download` response
remains JSON-compatible for bots that expect metadata.

## Important Heroku note

Heroku's local filesystem is ephemeral. Downloaded MP3 cache files can be
deleted whenever the dyno restarts or redeploys. The API still works; repeated
downloads will simply fetch the source again after a restart.

Never commit `cookies.txt` or a real API key. Use Heroku config vars, and use a
private or signed URL for `COOKIE_URL`.