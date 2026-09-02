# Prime Go cloud service
# =======================
# A small always-on backend for the "Prime Go" phone music player.
#
# It provides three things the phone can't do on its own:
#   1. FREE song identification ("Shazam-style") via shazamio.
#   2. YouTube / SoundCloud search (metadata only, no download) via yt-dlp.
#   3. An audio *proxy* stream for a chosen YouTube / SoundCloud track, so the
#      phone's <audio> element can play it (the raw source URLs are IP-locked
#      to this server and cannot be played by the phone directly).
#
# Designed to run on Render (Docker, free tier). The phone is a static web app
# served from a Cloudflare R2 domain and calls this service directly over the
# internet, so CORS is fully open.
#
# Run with:  uvicorn app:app --host 0.0.0.0 --port $PORT

import os
import re
import shutil
import tempfile
import subprocess
import asyncio
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, Response

import yt_dlp

app = FastAPI(title="Prime Go reco-service")

# ---------------------------------------------------------------------------
# CORS: fully open. The phone lives on a Cloudflare R2 origin and calls us
# cross-origin, so we allow every origin and the methods/headers we use.
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health check (Render pings this).
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"ok": True}


# A catch-all OPTIONS handler so any preflight always gets a permissive answer,
# even for paths CORSMiddleware might not special-case.
@app.options("/{rest_of_path:path}")
async def preflight(rest_of_path: str):
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "86400",
        },
    )


# ---------------------------------------------------------------------------
# 2. POST /reco  -- FREE song identification via shazamio
# ---------------------------------------------------------------------------
@app.post("/reco")
async def reco(request: Request):
    """
    Body: raw audio bytes (webm / mp4 / wav / ...). We transcode to mono 16kHz
    WAV with ffmpeg, then hand the file to shazamio.
    Never raises 500 on a normal no-match.
    """
    src_path = None
    wav_path = None
    try:
        raw = await request.body()
        if not raw:
            return {"matched": False, "error": "empty body"}

        # Persist the uploaded bytes to a temp file (unknown container).
        fd, src_path = tempfile.mkstemp(suffix=".input")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)

        # Transcode -> mono 16kHz WAV (what shazamio expects to fingerprint).
        fd2, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd2)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", src_path,
            "-ac", "1", "-ar", "16000", "-f", "wav", wav_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await proc.communicate()
        if proc.returncode != 0 or not os.path.getsize(wav_path):
            return {"matched": False, "error": "transcode failed"}

        # shazamio: newer versions expose recognize(); older recognize_song().
        from shazamio import Shazam
        shazam = Shazam()
        method = getattr(shazam, "recognize", None) or getattr(
            shazam, "recognize_song", None
        )
        if method is None:
            return {"matched": False, "error": "shazamio has no recognize method"}
        result = await method(wav_path)

        track = (result or {}).get("track")
        if not track:
            return {"matched": False}

        images = track.get("images") or {}
        cover = images.get("coverart") or images.get("coverarthq")

        album = None
        # Album often lives inside sections[].metadata[] with title "Album".
        try:
            for section in track.get("sections") or []:
                for meta in section.get("metadata") or []:
                    if str(meta.get("title", "")).lower() == "album":
                        album = meta.get("text")
                        break
                if album:
                    break
        except Exception:
            album = None

        return {
            "matched": True,
            "title": track.get("title", ""),
            "artist": track.get("subtitle", ""),
            "cover": cover,
            "album": album,
        }
    except Exception as e:  # never crash the process on a bad request
        return {"matched": False, "error": str(e)}
    finally:
        for p in (src_path, wav_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 3. GET /search  -- YouTube / SoundCloud search (no download)
# ---------------------------------------------------------------------------
def _search_sync(query: str, src: str, limit: int):
    if src == "soundcloud":
        search = f"scsearch{limit}:{query}"
    else:
        src = "youtube"
        search = f"ytsearch{limit}:{query}"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,   # fast: don't resolve each entry fully
        "skip_download": True,
        "default_search": "auto",
    }

    results = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(search, download=False)
        for entry in (info or {}).get("entries", []) or []:
            if not entry:
                continue
            # thumbnail: flat entries sometimes have `thumbnails` list.
            thumb = entry.get("thumbnail")
            if not thumb:
                thumbs = entry.get("thumbnails") or []
                if thumbs:
                    thumb = thumbs[-1].get("url")
            results.append({
                "id": entry.get("id") or entry.get("url"),
                "title": entry.get("title"),
                "artist": entry.get("uploader")
                or entry.get("channel")
                or entry.get("uploader_id"),
                "duration": entry.get("duration"),
                "thumb": thumb,
                "src": src,
            })
    return results


@app.get("/search")
async def search(
    q: str = Query(...),
    src: str = Query("youtube"),
    limit: int = Query(15),
):
    try:
        limit = max(1, min(int(limit), 40))
        src = "soundcloud" if src == "soundcloud" else "youtube"
        results = await asyncio.to_thread(_search_sync, q, src, limit)
        return {"results": results}
    except Exception as e:
        return JSONResponse(status_code=200, content={"results": [], "error": str(e)})


# ---------------------------------------------------------------------------
# 4. GET /stream  -- resolve best audio and proxy it to the phone
# ---------------------------------------------------------------------------
def _resolve_audio_sync(src: str, vid: str):
    """Return (url, http_headers, content_type) for the best audio-only format."""
    if src == "soundcloud":
        # yt-dlp accepts full soundcloud URLs; ids from search are usually URLs.
        target = vid if str(vid).startswith("http") else f"https://soundcloud.com/{vid}"
    else:
        target = vid if str(vid).startswith("http") else f"https://www.youtube.com/watch?v={vid}"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Prefer m4a, then mp3, then any bestaudio.
        "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target, download=False)

    # If a playlist/search slipped through, take the first entry.
    if info and "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("no playable entry")
        info = entries[0]

    url = info.get("url")
    ext = (info.get("ext") or "").lower()
    # yt-dlp exposes per-format request headers needed to fetch the media.
    http_headers = info.get("http_headers") or {}

    if not url:
        # Fall back to scanning formats for a direct audio url.
        for fmt in reversed(info.get("formats") or []):
            if fmt.get("url") and (fmt.get("acodec") not in (None, "none")):
                url = fmt["url"]
                ext = (fmt.get("ext") or ext).lower()
                http_headers = fmt.get("http_headers") or http_headers
                break

    if not url:
        raise RuntimeError("could not resolve audio url")

    if ext in ("m4a", "mp4", "aac"):
        content_type = "audio/mp4"
    elif ext in ("mp3", "mpeg"):
        content_type = "audio/mpeg"
    elif ext == "webm" or ext == "opus":
        content_type = "audio/webm"
    else:
        content_type = "application/octet-stream"

    return url, http_headers, content_type


@app.get("/stream")
async def stream(
    src: str = Query("youtube"),
    id: str = Query(...),
    request: Request = None,
):
    try:
        src = "soundcloud" if src == "soundcloud" else "youtube"
        url, up_headers, content_type = await asyncio.to_thread(
            _resolve_audio_sync, src, id
        )
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})

    # Forward the client's Range header (for seeking) plus the headers yt-dlp
    # says are needed to fetch the media from the CDN.
    fwd_headers = dict(up_headers or {})
    client_range = request.headers.get("range") if request else None
    if client_range:
        fwd_headers["Range"] = client_range

    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True)

    try:
        req = client.build_request("GET", url, headers=fwd_headers)
        upstream = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        return JSONResponse(status_code=502, content={"error": str(e)})

    if upstream.status_code >= 400:
        code = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        return JSONResponse(status_code=502, content={"error": f"upstream {code}"})

    # Pass through the streaming/range-relevant response headers.
    resp_headers = {
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
    }
    for h in ("content-length", "content-range"):
        if h in upstream.headers:
            resp_headers[h] = upstream.headers[h]

    status_code = 206 if (client_range and upstream.status_code == 206) else 200

    async def body_iter():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=status_code,
        media_type=content_type,
        headers=resp_headers,
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
