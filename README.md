# Prime Go reco-service

A tiny always-on cloud backend for the **Prime Go** phone music player.

It gives the phone three things it can't do on its own:

1. **Free song identification** ("Shazam-style") via [`shazamio`](https://github.com/shazamio/ShazamIO).
2. **YouTube / SoundCloud search** (metadata only, no download) via `yt-dlp`.
3. An **audio proxy stream** so the phone's `<audio>` element can play a chosen
   YouTube/SoundCloud track. The raw source URLs are IP-locked to this server,
   so we proxy the bytes instead of redirecting.

CORS is fully open — the phone is a static web app on a Cloudflare R2 origin
(`https://pub-f631fc0f09344eac93904f4ec99278ef.r2.dev`) and calls this service
cross-origin.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | `{"ok": true}` — Render health check |
| POST | `/reco`   | Identify a song from raw audio bytes in the body |
| GET  | `/search?q=&src=youtube\|soundcloud&limit=15` | Search, no download |
| GET  | `/stream?src=youtube\|soundcloud&id=<id>` | Proxy best audio-only stream |

### `POST /reco`
Send ~5s of recorded mic audio as the raw request body (webm/mp4/wav, any
`Content-Type`). Returns:

```json
{ "matched": true, "title": "…", "artist": "…", "cover": "https://…", "album": "…" }
```

or `{"matched": false}` on no match (never a 500 for a normal miss).

### `GET /search`
```json
{ "results": [
  { "id": "dQw4w9WgXcQ", "title": "…", "artist": "…", "duration": 213, "thumb": "https://…", "src": "youtube" }
] }
```

### `GET /stream`
Streams `audio/mp4` or `audio/mpeg` bytes back through this service, honoring
HTTP `Range` requests for seeking. On failure returns `502 {"error": "…"}`.

## Deploy to Render (free tier)

1. Push this `reco-service/` folder to a new GitHub repo (the repo root should
   contain `render.yaml`, `Dockerfile`, `app.py`, `requirements.txt`).
2. In the [Render dashboard](https://dashboard.render.com), click
   **New → Blueprint** and pick that repo.
3. Render reads `render.yaml`, sees one Docker web service on the **free** plan,
   and offers to create it — click **Apply**.
4. Wait for the first build/deploy. Your service gets a URL like
   `https://prime-go-reco.onrender.com`. Point the phone app at that base URL.
5. Health checks hit `/health`; `autoDeploy` redeploys on every push to the
   connected branch.

## Caveats

- **Cold starts:** the free plan spins the instance down after ~15 min idle. The
  next request wakes it and can take ~30–60s. Everything after is fast.
- **yt-dlp breakage:** YouTube/SoundCloud change their sites; `yt-dlp` needs
  periodic bumps in `requirements.txt`. If `/search` or `/stream` starts failing,
  update the pinned `yt-dlp` version and redeploy.
- **shazamio native deps:** `shazamio` needs `ffmpeg` at runtime (installed in the
  Dockerfile) to fingerprint audio. The `/reco` transcode step also uses it.

## Local run

```bash
pip install -r requirements.txt   # ffmpeg must be on PATH
uvicorn app:app --host 0.0.0.0 --port 8000
```
