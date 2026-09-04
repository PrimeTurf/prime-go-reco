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

        # EVERYTHING SHAZAM ACTUALLY KNOWS, not just the album.
        # The metadata rows come back as free-form {title, text} pairs and the
        # set varies by track: Album, Released, Label, Producer and more. We
        # used to walk them, take "Album", and throw the rest away — so the
        # release year and the label, which are exactly what tells two versions
        # of a song apart, were fetched and discarded on every single lookup.
        meta_all = {}
        try:
            for section in track.get("sections") or []:
                for meta in section.get("metadata") or []:
                    k = str(meta.get("title", "")).strip()
                    v = meta.get("text")
                    if k and v and k not in meta_all:
                        meta_all[k] = v
        except Exception:
            meta_all = {}

        def _m(*names):
            for n in names:
                for k, v in meta_all.items():
                    if k.lower() == n:
                        return v
            return None

        released = _m("released", "release date", "year")
        year = None
        try:
            import re as _re
            _y = _re.search(r"(19|20)\d{2}", str(released or ""))
            year = int(_y.group(0)) if _y else None
        except Exception:
            year = None

        genre = None
        try:
            genre = ((track.get("genres") or {}).get("primary")) or None
        except Exception:
            genre = None

        return {
            "matched": True,
            "title": track.get("title", ""),
            "artist": track.get("subtitle", ""),
            "cover": cover,
            "album": _m("album"),
            "label": _m("label"),
            "released": released,
            "year": year,
            "genre": genre,
            "isrc": track.get("isrc") or None,
            "shazam_url": track.get("url") or None,
            # everything else Shazam sent, so a field we have not named yet is
            # still there instead of being dropped on the floor
            "meta": meta_all,
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

    # FAIL FAST. YouTube refuses this server's address — it is a datacenter IP
    # and every player client comes back "failed to extract any player
    # response". With no limits set, yt-dlp worked through all five clients
    # with retries for FORTY SIX SECONDS before giving up, and the phone just
    # sat there. It is going to fail either way; it should fail quickly enough
    # for the app to move on to SoundCloud, which works.
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Prefer m4a, then mp3, then any bestaudio.
        "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best",
        "extractor_args": _YT_EXTRACTOR_ARGS,
        "socket_timeout": 8,
        "retries": 0,
        "extractor_retries": 0,
        "fragment_retries": 0,
        "noplaylist": True,
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
        # Say WHICH service refused, so the phone can tell the difference
        # between "this song is not on SoundCloud" and "YouTube will not talk
        # to this server", and so the logs are readable at a glance.
        return JSONResponse(status_code=502,
                            content={"error": str(e)[:300], "src": src,
                                     "hint": ("YouTube refuses this server's address; "
                                              "SoundCloud is the audio path")
                                     if src == "youtube" else ""})

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
    if not artist:
        arr = info.get("artists")
        if isinstance(arr, list) and arr:
            artist = ", ".join(str(a).strip() for a in arr if a).strip()
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


def _artist_by_length(title: str, dur_sec, tol_sec: float = 6.0) -> str:
    """Find the real artist for a title-only track by matching the SONG LENGTH.
    When SoundCloud hands back a bare title and no artist, the duration is the
    fingerprint: the recording of that name whose length matches is almost
    always the right track, and it carries the artist credit. Uses MusicBrainz
    (free, no key). Best effort — never raises, returns "" on any miss."""
    t = (title or "").strip()
    if not t or not dur_sec:
        return ""
    try:
        dur_ms = int(float(dur_sec) * 1000)
    except Exception:
        return ""
    try:
        q = re.sub(r'["\\]', " ", t).strip()
        r = httpx.get(
            "https://musicbrainz.org/ws/2/recording/",
            params={"query": f'recording:"{q}"', "fmt": "json", "limit": 25},
            headers={"User-Agent": "PrimeGoReco/1.0 (https://prime-go-reco.onrender.com)"},
            timeout=20, follow_redirects=True,
        )
        if r.status_code != 200:
            return ""
        recs = (r.json() or {}).get("recordings") or []
    except Exception:
        return ""
    tol_ms = max(3000, int(tol_sec * 1000))
    best, best_gap = "", tol_ms + 1
    for rec in recs:
        length = rec.get("length")
        if not length:
            continue
        try:
            gap = abs(int(length) - dur_ms)
        except Exception:
            continue
        if gap <= tol_ms and gap < best_gap:
            ac = rec.get("artist-credit") or []
            name = "".join((c.get("name") or "") + (c.get("joinphrase") or "")
                           for c in ac).strip()
            if name:
                best, best_gap = name, gap
    return best


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
    # PEAK CAP: hot masters keep their full level (we do NOT loudness-normalize
    # rips, that would gut club weight), but a master sitting at or above full
    # scale clips on a phone speaker — the "speaker about to burst" distortion.
    # A true-peak brickwall limiter at -1.5 dBFS with no makeup gain shaves only
    # the overs and leaves loudness untouched. Best effort: if it fails we keep
    # the original file rather than lose the rip.
    try:
        capped = os.path.join(workdir, "a.cap.mp3")
        pc = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-i", mp3,
             "-af", "alimiter=limit=0.841:level=false",
             "-codec:a", "libmp3lame", "-q:a", "0", capped],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
        if pc.returncode == 0 and os.path.exists(capped) and os.path.getsize(capped) > 0:
            os.replace(capped, mp3)
    except Exception:
        pass
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
        # No artist off the upload? Find it by the song's LENGTH. A SoundCloud
        # track with just a title still has a real artist somewhere — the
        # recording of that name whose duration matches is the one.
        if (not artist or artist.lower() in ("", "unknown", "unknown artist", "various", "va")):
            found = await asyncio.to_thread(_artist_by_length, title, dur)
            if found:
                artist = found
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
    dur: str = Query(""),
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
    try:
        _dur = int(float(dur)) if dur else None
    except Exception:
        _dur = None
    q.append({
        "src": src, "id": str(id),
        "title": title or "Untitled", "artist": artist or "",
        "thumb": thumb or "", "dur": _dur, "added": int(_t.time()),
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


# ---------------------------------------------------------------------------
# SHARE: copy one or more tracks from one person's cloud space into another's,
# so a shared song or playlist becomes truly theirs — the audio and artwork are
# copied server-side (R2 to R2, no download), and the tracks are appended to the
# recipient's library the phone reads.
# ---------------------------------------------------------------------------
@app.post("/share_add")
async def share_add(
    frm: str = Query(...),      # source space (who shared)
    to: str = Query(...),       # recipient space (who is adding)
    ids: str = Query(...),      # comma-separated raw_ids to copy
):
    frm = _SAFE.sub("", str(frm)); to = _SAFE.sub("", str(to))
    if not frm or not to:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad space"})
    if frm == to:
        return {"ok": True, "added": 0, "already": True}   # your own track
    s3 = _r2()
    if s3 is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    bucket = os.getenv("R2_BUCKET", "").strip()
    if not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "R2_BUCKET not set"})
    import json as _j
    want = [x.strip() for x in str(ids).split(",") if x.strip()][:200]
    if not want:
        return {"ok": True, "added": 0}

    def _load_lib(space):
        try:
            obj = s3.get_object(Bucket=bucket, Key=f"u/{space}/library.json")
            cur = _j.loads(obj["Body"].read().decode("utf-8"))
            t = cur.get("tracks") if isinstance(cur, dict) else cur
            return cur if isinstance(cur, dict) else {"tracks": t or []}, (t or [])
        except Exception:
            return {"tracks": []}, []

    src_lib, src_tracks = _load_lib(frm)
    by_id = {str(t.get("raw_id")): t for t in src_tracks}
    dst_lib, dst_tracks = _load_lib(to)
    have = {str(t.get("raw_id")) for t in dst_tracks}

    added = 0
    import time as _t
    for rid in want:
        if rid in have:
            continue
        meta = by_id.get(rid)
        if not meta:
            continue
        # copy the audio + cover R2->R2 (server side, fast). Cover is best effort.
        try:
            s3.copy_object(Bucket=bucket, Key=f"u/{to}/audio/{rid}.mp3",
                           CopySource={"Bucket": bucket, "Key": f"u/{frm}/audio/{rid}.mp3"})
        except Exception as e:
            continue   # no audio to copy = nothing to add
        try:
            s3.copy_object(Bucket=bucket, Key=f"u/{to}/cover/{rid}.jpg",
                           CopySource={"Bucket": bucket, "Key": f"u/{frm}/cover/{rid}.jpg"})
        except Exception:
            pass
        entry = dict(meta)
        entry["added"] = int(_t.time())
        entry["shared_from"] = frm
        dst_tracks.append(entry)
        have.add(rid)
        added += 1

    if added:
        dst_lib["tracks"] = dst_tracks
        try:
            s3.put_object(Bucket=bucket, Key=f"u/{to}/library.json",
                          Body=_j.dumps(dst_lib).encode("utf-8"),
                          ContentType="application/json",
                          CacheControl="public, max-age=15")
        except Exception as e:
            return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:160]})
    return {"ok": True, "added": added}


@app.post("/remove")
async def remove(
    space: str = Query(...),     # the caller's own space
    ids: str = Query(...),       # comma-separated raw_ids to delete
):
    """Delete tracks from a space: drop them out of library.json and remove the
    audio + cover objects from R2. This is the phone's Delete from library."""
    space = _SAFE.sub("", str(space))
    if not space:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad space"})
    s3 = _r2()
    if s3 is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    bucket = os.getenv("R2_BUCKET", "").strip()
    if not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "R2_BUCKET not set"})
    import json as _j
    want = {x.strip() for x in str(ids).split(",") if x.strip()}
    if not want:
        return {"ok": True, "removed": 0}
    key = f"u/{space}/library.json"
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        cur = _j.loads(obj["Body"].read().decode("utf-8"))
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": f"no library: {str(e)[:120]}"})
    tracks = cur.get("tracks") if isinstance(cur, dict) else cur
    tracks = tracks if isinstance(tracks, list) else []
    keep = [t for t in tracks if str(t.get("raw_id")) not in want]
    removed = len(tracks) - len(keep)
    # delete the audio + cover for each removed id (best effort)
    for rid in want:
        for k in (f"u/{space}/audio/{rid}.mp3", f"u/{space}/cover/{rid}.jpg"):
            try:
                s3.delete_object(Bucket=bucket, Key=k)
            except Exception:
                pass
    if removed:
        if isinstance(cur, dict):
            cur["tracks"] = keep
        else:
            cur = {"tracks": keep}
        try:
            s3.put_object(Bucket=bucket, Key=key,
                          Body=_j.dumps(cur).encode("utf-8"),
                          ContentType="application/json",
                          CacheControl="public, max-age=15")
        except Exception as e:
            return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:160]})
    return {"ok": True, "removed": removed}


@app.post("/playlist_push")
async def playlist_push(
    space: str = Query(...),     # the caller's own space
    name: str = Query(...),      # playlist name
    ids: str = Query(...),       # comma-separated raw_ids in the playlist
):
    """A playlist built in Prime Go is sent to the desktop: queued in
    u/<space>/inbox_playlists.json for the desktop to pick up and send to USB."""
    space = _SAFE.sub("", str(space))
    name = (name or "").strip()[:120]
    if not space or not name:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad request"})
    want = [x.strip() for x in str(ids).split(",") if x.strip()][:1000]
    if not want:
        return {"ok": True, "queued": 0}
    s3 = _r2()
    if s3 is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    bucket = os.getenv("R2_BUCKET", "").strip()
    if not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "R2_BUCKET not set"})
    import json as _j, time as _t
    key = f"u/{space}/inbox_playlists.json"
    cur = []
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        got = _j.loads(obj["Body"].read().decode("utf-8"))
        cur = got.get("playlists", []) if isinstance(got, dict) else (got or [])
    except Exception:
        cur = []
    pid = name.lower().replace(" ", "_")
    cur = [p for p in cur if p.get("id") != pid]
    cur.append({"id": pid, "name": name, "ids": want, "count": len(want),
                "ts": int(_t.time() * 1000)})
    cur = cur[-100:]
    try:
        s3.put_object(Bucket=bucket, Key=key,
                      Body=_j.dumps({"playlists": cur}).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:160]})
    return {"ok": True, "queued": len(want)}


# ---------------------------------------------------------------------------
# Shazam history: one list per space, so a night's IDs are not trapped on one
# phone. localStorage is per browser — open Prime Go from the home screen on
# Monday and from Safari on Tuesday and you are looking at two different
# histories, and clearing site data loses the lot. The phone still keeps its
# local copy and still works with no signal; this is the copy that survives.
# ---------------------------------------------------------------------------
_SHZ_KEY = "shazam_history.json"
_SHZ_MAX = 500


def _shz_read(s3, bucket: str, space: str) -> list:
    import json as _j
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"u/{space}/{_SHZ_KEY}")
        got = _j.loads(obj["Body"].read().decode("utf-8"))
        items = got.get("found", []) if isinstance(got, dict) else (got or [])
        return items if isinstance(items, list) else []
    except Exception:
        return []


@app.post("/shazam_push")
async def shazam_push(
    space: str = Query(...),
    title: str = Query(...),
    artist: str = Query(""),
    cover: str = Query(""),
    ts: int = Query(0),
    album: str = Query(""),
    label: str = Query(""),
    year: str = Query(""),
    genre: str = Query(""),
):
    """Record one identified song against this space. Idempotent: the same
    title + artist + timestamp never lands twice, so a retry after a dropped
    connection is free."""
    space = _SAFE.sub("", str(space))
    title = (title or "").strip()[:200]
    if not space or not title:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad request"})
    s3 = _r2()
    bucket = os.getenv("R2_BUCKET", "").strip()
    if s3 is None or not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    import json as _j, time as _t
    when = int(ts) if ts else int(_t.time() * 1000)
    entry = {"title": title, "artist": (artist or "").strip()[:200],
             "cover": (cover or "").strip()[:600], "ts": when,
             "album": (album or "").strip()[:200] or None,
             "label": (label or "").strip()[:200] or None,
             "year": int(year) if str(year or "").strip().isdigit() else None,
             "genre": (genre or "").strip()[:80] or None}
    cur = _shz_read(s3, bucket, space)
    sig = (entry["title"], entry["artist"], entry["ts"])
    if not any((c.get("title"), c.get("artist"), c.get("ts")) == sig for c in cur):
        cur.append(entry)
    cur.sort(key=lambda c: c.get("ts") or 0)
    cur = cur[-_SHZ_MAX:]
    try:
        s3.put_object(Bucket=bucket, Key=f"u/{space}/{_SHZ_KEY}",
                      Body=_j.dumps({"found": cur}).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:160]})
    return {"ok": True, "count": len(cur)}


@app.get("/shazam_list")
async def shazam_list(space: str = Query(...)):
    """The whole history for this space, oldest first. The phone normally reads
    the public JSON directly; this is the fallback when that object is cached or
    the space has never published one."""
    space = _SAFE.sub("", str(space))
    if not space:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad request"})
    s3 = _r2()
    bucket = os.getenv("R2_BUCKET", "").strip()
    if s3 is None or not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    return {"ok": True, "found": _shz_read(s3, bucket, space)}


# ---------------------------------------------------------------------------
# Sync now: the phone asks the laptop to go, instead of waiting for its timer.
#
# The phone cannot reach the laptop. It has no address for it and the laptop is
# behind a home router. What both of them CAN reach is the bucket, so the phone
# leaves a note there with the time on it and the laptop, which is already
# awake, notices and syncs. That is the whole mechanism.
#
# Before this, Sync from desktop only re-read the cloud. If the laptop had not
# run its own sync yet there was nothing new to read, so the button looked
# broken while doing exactly what it said.
# ---------------------------------------------------------------------------
@app.post("/sync_ping")
async def sync_ping(space: str = Query(...)):
    """Ask the desktop for this space to sync as soon as it sees this."""
    space = _SAFE.sub("", str(space))
    if not space:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad request"})
    s3 = _r2()
    bucket = os.getenv("R2_BUCKET", "").strip()
    if s3 is None or not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    import json as _j, time as _t
    at = int(_t.time() * 1000)
    try:
        s3.put_object(Bucket=bucket, Key=f"u/{space}/sync_request.json",
                      Body=_j.dumps({"at": at}).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:160]})
    return {"ok": True, "at": at}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
