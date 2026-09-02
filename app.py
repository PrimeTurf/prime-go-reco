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

# YouTube blocks the default "web" client from datacenter IPs ("Sign in to
# confirm you're not a bot"). These alternate player clients often resolve
# without cookies; yt-dlp tries them in order. Overridable via env.
_YT_CLIENTS = [c.strip() for c in os.getenv(
    "YT_PLAYER_CLIENTS", "tv,mweb,web_safari,android,ios").split(",") if c.strip()]
_YT_EXTRACTOR_ARGS = {"youtube": {"player_client": _YT_CLIENTS}}

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
            # SoundCloud must be resolved by its permalink URL, not its numeric
            # id; YouTube resolves fine by the 11-char video id.
            if src == "soundcloud":
                the_id = entry.get("url") or entry.get("permalink_url") or entry.get("id")
            else:
                the_id = entry.get("id") or entry.get("url")
            results.append({
                "id": the_id,
                "title": entry.get("title"),
                "artist": entry.get("uploader")
                or entry.get("channel")
                or entry.get("uploader_id"),
                "duration": entry.get("duration"),
                "thumb": thumb,
                "views": entry.get("view_count"),
                "src": src,
            })
    # Prefer the most-played clean audio over the official music video. A DJ
    # wants the full track, not a video edit with intro talking.
    if src == "youtube":
        results.sort(key=_yt_rank, reverse=True)
    return results


def _yt_rank(r: dict) -> float:
    import math
    title = (r.get("title") or "").lower()
    artist = (r.get("artist") or "").lower()
    views = r.get("views") or 0
    score = 0.0
    if views:
        score += math.log10(views + 10) * 10        # most played rises
    if artist.endswith("- topic") or " - topic" in artist:
        score += 30                                  # YouTube's clean auto audio
    for k in ("audio", "lyric", "full", "original mix", "extended mix", "hq"):
        if k in title:
            score += 8
    for k in ("official video", "official music video", "music video",
              "official mv", " m/v", "live", "remix video", "visualizer"):
        if k in title:
            score -= 14
    return score


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
        "extractor_args": _YT_EXTRACTOR_ARGS,
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


# ---------------------------------------------------------------------------
# 5. POST /rip  -- download a chosen track and DROP IT into the user's cloud
#    library, so it plays offline everywhere and syncs like a desktop rip.
#    Fully standalone: no laptop needed. Writes straight to Cloudflare R2.
# ---------------------------------------------------------------------------
#
# R2 write access comes from env vars set on the service (never in the phone):
#   R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_URL
# Optional: PG_ANALYZE=0 turns off BPM/key detection without a redeploy.

def _r2():
    """boto3 S3 client pointed at this account's R2, from env. None if unset."""
    acct = os.getenv("R2_ACCOUNT_ID", "").strip()
    key = os.getenv("R2_ACCESS_KEY_ID", "").strip()
    sec = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
    if not (acct and key and sec):
        return None
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=f"https://{acct}.r2.cloudflarestorage.com",
        aws_access_key_id=key,
        aws_secret_access_key=sec,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        region_name="auto",
    )


_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _rid(src: str, vid: str) -> str:
    """A stable, filesystem/URL-safe raw_id. Same song re-ripped -> same id,
    so it overwrites cleanly instead of duplicating."""
    tag = _SAFE.sub("", str(vid))[:40] or "x"
    return f"{'sc' if src == 'soundcloud' else 'yt'}_{tag}"


def _split_meta(info: dict):
    title = (info.get("track") or "").strip()
    artist = (info.get("artist") or info.get("creator") or "").strip()
    raw = (info.get("title") or "").strip()
    if not title:
        if not artist and " - " in raw:
            a, t = raw.split(" - ", 1)
            artist, title = a.strip(), t.strip()
        else:
            title = raw
    if not artist:
        artist = (info.get("uploader") or info.get("channel") or "").strip()
    return (title or "Untitled"), artist


def _rip_sync(src: str, vid: str):
    """Download bestaudio -> mp3, return (mp3_path, info, thumb_bytes)."""
    if src == "soundcloud":
        target = vid if str(vid).startswith("http") else f"https://soundcloud.com/{vid}"
    else:
        target = vid if str(vid).startswith("http") else f"https://www.youtube.com/watch?v={vid}"
    workdir = tempfile.mkdtemp(prefix="pgrip_")
    outtmpl = os.path.join(workdir, "a.%(ext)s")
    ydl_opts = {
        "quiet": True, "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "extractor_args": _YT_EXTRACTOR_ARGS,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target, download=True)
    if info and "entries" in info:
        ents = [e for e in (info.get("entries") or []) if e]
        if ents:
            info = ents[0]
    mp3 = os.path.join(workdir, "a.mp3")
    if not os.path.exists(mp3):
        cand = [f for f in os.listdir(workdir) if f.endswith(".mp3")]
        if cand:
            mp3 = os.path.join(workdir, cand[0])
    if not os.path.exists(mp3):
        raise RuntimeError("audio did not download")
    # thumbnail bytes -> jpg (best effort)
    thumb_jpg = None
    thumb = info.get("thumbnail")
    if not thumb:
        ths = info.get("thumbnails") or []
        if ths:
            thumb = ths[-1].get("url")
    if thumb:
        try:
            raw = httpx.get(thumb, timeout=20, follow_redirects=True).content
            src_img = os.path.join(workdir, "cover.src")
            with open(src_img, "wb") as f:
                f.write(raw)
            out_jpg = os.path.join(workdir, "cover.jpg")
            p = subprocess.run(
                ["ffmpeg", "-y", "-i", src_img, "-vf",
                 "scale='min(640,iw)':-1", out_jpg],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if p.returncode == 0 and os.path.exists(out_jpg):
                with open(out_jpg, "rb") as f:
                    thumb_jpg = f.read()
        except Exception:
            thumb_jpg = None
    return mp3, info, thumb_jpg


def _analyze(mp3_path: str):
    """Best-effort BPM + musical key from a 60s excerpt. Never raises."""
    if os.getenv("PG_ANALYZE", "1") == "0":
        return {}
    try:
        import numpy as np
        import librosa
        y, sr = librosa.load(mp3_path, sr=22050, mono=True, offset=30.0, duration=60.0)
        if y is None or len(y) < sr:
            y, sr = librosa.load(mp3_path, sr=22050, mono=True, duration=60.0)
        out = {}
        try:
            tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
            bpm = int(round(float(np.atleast_1d(tempo)[0])))
            if 40 <= bpm <= 220:
                out["bpm"] = bpm
        except Exception:
            pass
        try:
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
            prof = chroma.mean(axis=1)
            names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
            # Krumhansl-Schmuckler major/minor templates
            maj = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
            minor = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
            best, bkey = -9, ""
            for i in range(12):
                for tmpl, mode in ((maj, ""), (minor, "m")):
                    r = float(np.corrcoef(prof, np.roll(tmpl, i))[0, 1])
                    if r > best:
                        best, bkey = r, names[i] + mode
            if bkey:
                out["key"] = bkey
        except Exception:
            pass
        return out
    except Exception:
        return {}


@app.post("/rip")
async def rip(
    src: str = Query("youtube"),
    id: str = Query(...),
    space: str = Query(...),
):
    """Rip one track into u/<space>/ and append it to that space's library.json."""
    src = "soundcloud" if src == "soundcloud" else "youtube"
    space = _SAFE.sub("", str(space))
    if not space:
        return JSONResponse(status_code=400, content={"ok": False, "error": "no space"})
    s3 = _r2()
    if s3 is None:
        return JSONResponse(status_code=503, content={
            "ok": False,
            "error": "Cloud ripping is not set up yet (the service is missing its R2 keys)."})
    bucket = os.getenv("R2_BUCKET", "").strip()
    pub = os.getenv("R2_PUBLIC_URL", "").strip().rstrip("/")
    if not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "R2_BUCKET not set"})

    workdir = None
    try:
        mp3_path, info, thumb = await asyncio.to_thread(_rip_sync, src, id)
        workdir = os.path.dirname(mp3_path)
        title, artist = _split_meta(info)
        dur = info.get("duration")
        dur_ms = int(float(dur) * 1000) if dur else None
        rid = _rid(src, id)

        meta = await asyncio.to_thread(_analyze, mp3_path)

        with open(mp3_path, "rb") as f:
            audio_bytes = f.read()
        s3.put_object(Bucket=bucket, Key=f"u/{space}/audio/{rid}.mp3",
                      Body=audio_bytes, ContentType="audio/mpeg",
                      CacheControl="public, max-age=31536000")
        if thumb:
            s3.put_object(Bucket=bucket, Key=f"u/{space}/cover/{rid}.jpg",
                          Body=thumb, ContentType="image/jpeg",
                          CacheControl="public, max-age=31536000")

        import time as _t
        entry = {
            "raw_id": rid, "id": rid,
            "title": title, "display_title": title, "artist": artist,
            "album": "", "genre": "",
            "bpm": meta.get("bpm"),
            "key": meta.get("key", ""), "music_key": meta.get("key", ""),
            "camelot": "", "energy": None,
            "duration": dur, "dur": dur, "duration_ms": dur_ms,
            "source": src, "source_id": str(id), "added": int(_t.time()),
        }

        # read-modify-write the library the phone reads
        lib = {"tracks": []}
        try:
            obj = s3.get_object(Bucket=bucket, Key=f"u/{space}/library.json")
            import json as _j
            cur = _j.loads(obj["Body"].read().decode("utf-8"))
            if isinstance(cur, dict) and isinstance(cur.get("tracks"), list):
                lib = cur
            elif isinstance(cur, list):
                lib = {"tracks": cur}
        except Exception:
            pass
        tracks = lib.get("tracks", [])
        tracks = [t for t in tracks if str(t.get("raw_id")) != rid
                  and not (t.get("source_id") and str(t.get("source_id")) == str(id)
                           and t.get("source") == src)]
        tracks.append(entry)   # newest at the end — matches the app's recent order
        lib["tracks"] = tracks
        import json as _j
        s3.put_object(Bucket=bucket, Key=f"u/{space}/library.json",
                      Body=_j.dumps(lib).encode("utf-8"),
                      ContentType="application/json",
                      CacheControl="public, max-age=60")

        return {"ok": True, "track": entry,
                "audio": f"{pub}/u/{space}/audio/{rid}.mp3" if pub else ""}
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:200]})
    finally:
        if workdir and os.path.isdir(workdir):
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. POST /queue  -- hand a YouTube track to the desktop to rip.
#    YouTube blocks downloading from this server's IP, but the user's desktop
#    runs on a home IP that YouTube does not block. So instead of ripping here,
#    we drop the track into u/<space>/rip_queue.json. Prime Rip on the desktop
#    watches that file, rips each track cleanly (with BPM + key), uploads it to
#    the same R2 space, and removes it from the queue.
# ---------------------------------------------------------------------------
@app.post("/queue")
async def queue(
    src: str = Query("youtube"),
    id: str = Query(...),
    space: str = Query(...),
    title: str = Query(""),
    artist: str = Query(""),
    thumb: str = Query(""),
):
    src = "soundcloud" if src == "soundcloud" else "youtube"
    space = _SAFE.sub("", str(space))
    if not space:
        return JSONResponse(status_code=400, content={"ok": False, "error": "no space"})
    s3 = _r2()
    if s3 is None:
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "The rip list is not set up yet (missing R2 keys)."})
    bucket = os.getenv("R2_BUCKET", "").strip()
    if not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "R2_BUCKET not set"})
    key = f"u/{space}/rip_queue.json"
    import json as _j, time as _t
    data = {"queue": []}
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        cur = _j.loads(obj["Body"].read().decode("utf-8"))
        if isinstance(cur, dict) and isinstance(cur.get("queue"), list):
            data = cur
        elif isinstance(cur, list):
            data = {"queue": cur}
    except Exception:
        pass
    q = data.get("queue", [])
    # already queued? then it's a no-op success
    if any(str(it.get("id")) == str(id) and it.get("src") == src for it in q):
        return {"ok": True, "queued": len(q), "already": True}
    q.append({
        "src": src, "id": str(id),
        "title": title or "Untitled", "artist": artist or "",
        "thumb": thumb or "", "added": int(_t.time()),
        "status": "waiting",
    })
    data["queue"] = q
    try:
        s3.put_object(Bucket=bucket, Key=key,
                      Body=_j.dumps(data).encode("utf-8"),
                      ContentType="application/json",
                      CacheControl="public, max-age=15")
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:160]})
    return {"ok": True, "queued": len(q)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
