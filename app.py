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
# 3b. GET /list  -- every track in a SoundCloud set, likes page or profile
# ---------------------------------------------------------------------------
# Prime Go could rip one SoundCloud song at a time. A whole list — a set, a
# DJ's likes, an artist page — meant doing that fifty times. Paste the list's
# url into search on the phone and this returns its tracks in the same shape
# /search returns, so the phone can show them and rip them all in one go.
# Public SoundCloud pages only; nothing here signs in to anything.
_SC_LIST = re.compile(r"^https?://(www\.|m\.|on\.)?soundcloud\.com/[^\s]+$", re.I)
# a YouTube playlist: youtube.com/playlist?list=..., or a watch link carrying list=
_YT_LIST = re.compile(r"^https?://(www\.|m\.|music\.)?youtube\.com/(playlist\?|watch\?)[^\s]*list=[^\s&]+", re.I)


def _list_sync(url: str, limit: int):
    is_yt = bool(_YT_LIST.match(url))
    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                "skip_download": True, "playlistend": limit, "socket_timeout": 15}
    if is_yt:
        ydl_opts["extractor_args"] = _YT_EXTRACTOR_ARGS
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = (info or {}).get("entries")
    if entries is None and info:
        entries = [info]                     # a single track url: a list of one
    out = []
    sets_skipped = 0
    for e in (entries or []):
        if not e:
            continue
        if is_yt:
            # a YouTube playlist entry: the 11 character id is the whole story.
            # Ripping these is the laptop's job (YouTube blocks it here), so
            # the phone queues them for the laptop instead of ripping.
            the_id = e.get("id") or e.get("url")
            title = e.get("title") or ""
            artist = e.get("uploader") or e.get("channel") or ""
            if not artist and " - " in title:
                artist, title = [x.strip() for x in title.split(" - ", 1)]
            thumb = e.get("thumbnail")
            if not thumb:
                ts = e.get("thumbnails") or []
                if ts:
                    thumb = ts[-1].get("url")
            out.append({"id": the_id, "title": title, "artist": artist,
                        "duration": e.get("duration"), "thumb": thumb,
                        "views": e.get("view_count"), "src": "youtube"})
            continue
        the_id = e.get("url") or e.get("permalink_url") or e.get("id")
        # a profile page lists the DJ's own sets between the tracks. A set is
        # not a song: ripping it would pull a whole playlist under one name.
        # Paste the set's own link to rip that.
        if "/sets/" in str(the_id):
            sets_skipped += 1
            continue
        thumb = e.get("thumbnail")
        if not thumb:
            ts = e.get("thumbnails") or []
            if ts:
                thumb = ts[-1].get("url")
        title = e.get("title") or ""
        artist = e.get("uploader") or e.get("channel") or e.get("uploader_id") or ""
        if not artist:
            # a flat profile listing carries only url + title. The artist is
            # in the title ("Artist - Song") or, failing that, the permalink's
            # own user slug (soundcloud.com/<user>/<song>).
            if " - " in title:
                artist, title = [x.strip() for x in title.split(" - ", 1)]
            else:
                m = re.match(r"https?://(?:www\.|m\.)?soundcloud\.com/([^/?#]+)/", str(the_id))
                if m:
                    artist = m.group(1).replace("-", " ").replace("_", " ").title()
        out.append({
            "id": the_id, "title": title, "artist": artist,
            "duration": e.get("duration"),
            "thumb": thumb, "views": e.get("view_count"), "src": "soundcloud",
        })
    return {"name": (info or {}).get("title") or "", "tracks": out, "sets_skipped": sets_skipped,
            "src": "youtube" if is_yt else "soundcloud"}


@app.get("/list")
async def list_tracks(url: str = Query(...), limit: int = Query(300)):
    url = (url or "").strip()
    if not (_SC_LIST.match(url) or _YT_LIST.match(url)):
        return JSONResponse(status_code=200, content={"tracks": [], "error": "Paste a soundcloud.com link or a YouTube playlist link"})
    try:
        limit = max(1, min(int(limit), 500))
        return await asyncio.to_thread(_list_sync, url, limit)
    except Exception as e:
        return JSONResponse(status_code=200, content={"tracks": [], "error": str(e)[:200]})


# ---------------------------------------------------------------------------
# 4. GET /stream  -- resolve best audio and proxy it to the phone
# ---------------------------------------------------------------------------
def _is_hls(url: str, proto: str = "") -> bool:
    """Is this a playlist rather than a file?"""
    u = (url or "").split("?")[0].lower()
    return "m3u8" in (proto or "").lower() or u.endswith(".m3u8")


def _pick_progressive(info):
    """The best plain http(s) audio file in this result, or None.

    WHY THIS EXISTS — the bug that kept New in Dance silent (2026-09-04).
    SoundCloud hands yt-dlp an HLS stream by default. The proxy took whatever
    url came back and streamed it under Content-Type audio/mp4, so what reached
    the phone was 35 KB beginning "#EXTM3U" — a PLAYLIST OF SEGMENTS, labelled
    as a song. No audio element can play that. It downloaded fine, it was the
    right length for a text file, and it failed every single time.

    A progressive format is one file over http, which is what an <audio> tag
    wants. SoundCloud publishes one alongside the HLS; this finds it."""
    best = None
    for fmt in (info.get("formats") or []):
        u = fmt.get("url")
        if not u or fmt.get("acodec") == "none":
            continue
        if _is_hls(u, fmt.get("protocol") or ""):
            continue
        rank = fmt.get("abr") or fmt.get("tbr") or 0
        if best is None or rank > best[0]:
            best = (rank, u, (fmt.get("ext") or "").lower(),
                    fmt.get("http_headers") or info.get("http_headers") or {})
    if not best:
        return None
    return best[1], best[2], best[3]


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
        # PROGRESSIVE ONLY. Ask for a plain http(s) file, never HLS. See the
        # note below _pick_progressive for why this matters more than anything
        # else in this function.
        "format": ("bestaudio[protocol^=http][ext=m4a]/bestaudio[protocol^=http][ext=mp3]"
                   "/bestaudio[protocol^=http]/bestaudio/best"),
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
    proto = (info.get("protocol") or "").lower()

    if not url or _is_hls(url, proto):
        picked = _pick_progressive(info)
        if picked:
            url, ext, http_headers = picked
        elif not url:
            raise RuntimeError("could not resolve audio url")
        else:
            # Only HLS on offer. Serving the playlist would hand the phone a
            # text file named like a song, which is exactly the bug this
            # guards: better a clear error than silent nonsense.
            raise RuntimeError("only an HLS stream is available for this track")

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

    # ---------------------------------------------------------------------
    # RANGE, PROPERLY. This is what kept New in Dance silent on the phone.
    #
    # A phone does not fetch audio in one go. It asks for a few bytes first, as
    # "Range: bytes=0-1", and expects 206 with a Content-Range telling it how
    # big the file is. This proxy used to answer that with 200 and the whole
    # file while still advertising "Accept-Ranges: bytes" — because SoundCloud's
    # CDN ignores the Range and the 200 was passed straight through. Claiming
    # range support and then not honouring it is the one answer a phone will
    # not accept, so it gave up and the app said it could not play the song.
    # Songs from the crate were fine: those come from the bucket, which does
    # ranges properly. Exactly the split John was seeing.
    #
    # So: if the source honoured the range, pass it through. If it ignored it,
    # satisfy the range HERE by skipping to the offset and stopping at the end.
    # And if the size is unknown, say "Accept-Ranges: none" instead of promising
    # something we cannot do — an honest no is playable, a false yes is not.
    # ---------------------------------------------------------------------
    try:
        total = int(upstream.headers.get("content-length"))
    except (TypeError, ValueError):
        total = None

    start = end = None
    if client_range and upstream.status_code == 200 and total:
        m = re.match(r"^\s*bytes=(\d*)-(\d*)\s*$", client_range)
        if m:
            s_txt, e_txt = m.group(1), m.group(2)
            if s_txt == "" and e_txt:                 # the last N bytes
                start, end = max(0, total - int(e_txt)), total - 1
            elif s_txt != "":
                start = int(s_txt)
                end = int(e_txt) if e_txt else total - 1
            if start is None or start >= total:
                start = end = None                    # unsatisfiable: send it all
            else:
                end = min(end if end is not None else total - 1, total - 1)

    # LET THE WARM UP COUNT. The phone fetches the NEXT song while the current
    # one plays, so the handover on a locked screen needs no new network load —
    # iOS does not start those from a page in the background. That only helps
    # if the bytes it warmed can be reused by the player a minute later, which
    # needs the browser to be allowed to keep them. Private, half an hour.
    resp_headers = {"Access-Control-Allow-Origin": "*",
                    "Cache-Control": "private, max-age=1800"}
    if start is not None:
        status_code = 206
        resp_headers["Accept-Ranges"] = "bytes"
        resp_headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        resp_headers["Content-Length"] = str(end - start + 1)
    elif upstream.status_code == 206 and "content-range" in upstream.headers:
        status_code = 206
        resp_headers["Accept-Ranges"] = "bytes"
        resp_headers["Content-Range"] = upstream.headers["content-range"]
        if "content-length" in upstream.headers:
            resp_headers["Content-Length"] = upstream.headers["content-length"]
    else:
        status_code = 200
        # only promise ranges when the size is known, so a second request for
        # bytes 500000- can actually be answered
        resp_headers["Accept-Ranges"] = "bytes" if total else "none"
        if total:
            resp_headers["Content-Length"] = str(total)

    async def body_iter():
        try:
            if start is None:
                async for chunk in upstream.aiter_bytes(chunk_size=64 * 1024):
                    yield chunk
            else:
                pos, want = 0, end + 1
                async for chunk in upstream.aiter_bytes(chunk_size=64 * 1024):
                    nxt = pos + len(chunk)
                    if nxt > start:                 # some of this chunk is wanted
                        lo = max(0, start - pos)
                        hi = min(len(chunk), want - pos)
                        if hi > lo:
                            yield chunk[lo:hi]
                    pos = nxt
                    if pos >= want:
                        break
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
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target, download=True)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)   # a refused download leaves no temp dir behind
        raise
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
    # A PREVIEW IS NOT THE SONG. SoundCloud hands back a 30 second snippet for
    # a Go+ track and the download "succeeds"; the phone then had a song that
    # cut off at half a minute and no way to know why. Measure what actually
    # arrived against the length SoundCloud advertised: a file a fraction of
    # the advertised length is a preview, and a preview is a refusal — the
    # phone hands the song to the laptop, which can rip it another way.
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", mp3],
            capture_output=True, text=True, timeout=30)
        got = float((pr.stdout or "0").strip() or 0)
        want = float((info or {}).get("duration") or 0)
        if got and want and want > 90 and got < want * 0.6:
            shutil.rmtree(workdir, ignore_errors=True)
            raise RuntimeError(
                f"Preview only — SoundCloud handed back {int(got)}s of a {int(want)}s song (Go+ track)")
    except RuntimeError:
        raise
    except Exception:
        pass
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


_LIB_LOCK = asyncio.Lock()   # library.json writer, one at a time


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

        # read-modify-write the library the phone reads. ONE AT A TIME: a list
        # rip fires several of these together, and two writers reading the
        # same library.json would each save a copy missing the other's song.
        async with _LIB_LOCK:
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
    sc: str = Query(""),
    why: str = Query(""),
):
    src = "soundcloud" if src == "soundcloud" else "youtube"
    space = _SAFE.sub("", str(space))
    if not space:
        return JSONResponse(status_code=400, content={"ok": False, "error": "no space"})
    # THE SOUNDCLOUD ORIGINAL RIDES ALONG. A song SoundCloud refused the phone
    # (DRM, preview) is queued for the laptop as a YouTube rip, but the laptop
    # can also hand the SoundCloud link to MusicVerter for the real file. The
    # search hands back api.soundcloud.com urls, which a converter site will
    # not take, so resolve those to the public permalink here.
    sc = (sc or "").strip()
    if sc and "api.soundcloud.com" in sc:
        try:
            def _perma(u):
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
                    info = ydl.extract_info(u, download=False)
                return (info or {}).get("webpage_url") or (info or {}).get("permalink_url") or ""
            sc = (await asyncio.to_thread(_perma, sc)) or sc
        except Exception:
            pass
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
        "sc": sc or "", "why": (why or "")[:120],
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
# Now on SiriusXM: the last few plays on a channel, from xmplaylist, which logs
# every channel's airplay and publishes it free. Proxied here so the phone
# needs no cross origin permission and the service's one required header (a
# user agent) is set once. Cached a minute per channel; the feed itself
# updates about every two.
# ---------------------------------------------------------------------------
_XM_UA = "PrimeRip/1.0 (DJ crate tool; contact office@primecarega.org)"
_XM_CACHE: dict = {}


@app.get("/xm_now")
async def xm_now(channel: str = Query(...), limit: int = Query(12)):
    import re as _re, time as _t
    ch = _re.sub(r"[^a-z0-9]", "", (channel or "").lower())[:40]
    if not ch:
        return JSONResponse(status_code=400, content={"ok": False, "error": "channel?"})
    limit = max(1, min(int(limit), 40))
    now = _t.time()
    hit = _XM_CACHE.get(ch)
    if hit and now - hit[0] < 60:
        return {"ok": True, "channel": ch, "plays": hit[1][:limit], "cached": True}
    try:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": _XM_UA, "Accept": "application/json"}) as c:
            r = await c.get(f"https://xmplaylist.com/api/station/{ch}")
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        if hit:
            return {"ok": True, "channel": ch, "plays": hit[1][:limit], "stale": True}
        return JSONResponse(status_code=200, content={"ok": False, "error": f"xmplaylist did not answer: {str(e)[:100]}"})
    rows = data.get("results") if isinstance(data, dict) else data
    plays = []
    for row in (rows or [])[:40]:
        tr = row.get("track") or {}
        title = str(tr.get("title") or "").strip()
        if not title:
            continue
        arts = tr.get("artists") or []
        sp = row.get("spotify") or {}
        sc = yt = ""
        for ln in row.get("links") or []:
            site, url = str(ln.get("site") or ""), str(ln.get("url") or "")
            if site == "soundcloud" and url:
                sc = url
            elif site == "youtube" and url:
                m = _re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", url)
                yt = m.group(1) if m else ""
        plays.append({"artist": ", ".join(str(a) for a in arts if a) if isinstance(arts, list) else str(arts or ""),
                      "title": title, "art": sp.get("albumImageMedium") or sp.get("albumImageLarge") or "",
                      "ts": row.get("timestamp") or "", "sc": sc, "yt": yt})
    _XM_CACHE[ch] = (now, plays)
    return {"ok": True, "channel": ch, "plays": plays[:limit]}


@app.post("/list_add")
async def list_add(
    space: str = Query(...),     # the caller's own space
    url: str = Query(...),       # an Apple Music playlist link
    name: str = Query(""),
):
    """An Apple playlist link the phone wants as one of its own lists. Queued in
    u/<space>/inbox_lists.json; the desktop adds it to Find playlists and
    publishes within the minute. The key is the same one the desktop will use,
    so the phone can pin the list before it has been published."""
    import hashlib, json as _j, re as _re, time as _t
    space = _SAFE.sub("", str(space))
    url = (url or "").strip()
    is_apple = bool(_re.match(r"^https?://(music\.apple\.com|geo\.music\.apple\.com)/[a-z]{2}/playlist/[^/\s]+/pl\.[0-9a-zA-Z]+", url, _re.I))
    sp = _re.match(r"^https?://open\.spotify\.com/(?:embed/)?playlist/([A-Za-z0-9]+)", url, _re.I)
    if not space or not (is_apple or sp):
        return JSONResponse(status_code=400, content={"ok": False, "error": "That is not an Apple Music or Spotify playlist link."})
    url = _re.sub(r"[?#].*$", "", url)
    if sp:
        url = "https://open.spotify.com/playlist/" + sp.group(1)   # the same clean form the desktop keys on
    key = "cu-" + hashlib.sha1(url.lower().encode("utf-8")).hexdigest()[:10]
    s3 = _r2()
    if s3 is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    bucket = os.getenv("R2_BUCKET", "").strip()
    if not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "R2_BUCKET not set"})
    ik = f"u/{space}/inbox_lists.json"
    cur = []
    try:
        obj = s3.get_object(Bucket=bucket, Key=ik)
        got = _j.loads(obj["Body"].read().decode("utf-8"))
        cur = got.get("lists", []) if isinstance(got, dict) else (got or [])
    except Exception:
        cur = []
    cur = [x for x in cur if (x or {}).get("key") != key]
    cur.append({"key": key, "url": url, "name": (name or "").strip()[:80], "ts": int(_t.time() * 1000)})
    cur = cur[-50:]
    try:
        s3.put_object(Bucket=bucket, Key=ik, Body=_j.dumps({"lists": cur}).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:160]})
    return {"ok": True, "key": key}


# ---------------------------------------------------------------------------
# THE CREW'S AI KEY. Friends' copies have no key of their own; the owner's keys
# live here as ANTHROPIC_API_KEY / OPENAI_API_KEY. A Prime Rip install with a
# claimed cloud space sends its AI call here, the key is added, the upstream
# answer goes back untouched (status and body), so the app reads it exactly as
# a direct call. Only known upstreams, only known spaces. No daily cap, by the
# owner's choice; the space check keeps strangers off the key.
# ---------------------------------------------------------------------------
_AI_UPSTREAMS = {
    "https://api.anthropic.com/v1/messages": "anthropic",
    "https://api.openai.com/v1/chat/completions": "openai",
    "https://api.openai.com/v1/images/generations": "openai",
}
_SPACE_OK: dict = {}


def _space_known(space: str) -> bool:
    """A space is real when its manifest sits in the bucket. Remembered ten minutes."""
    import time as _t
    space = _SAFE.sub("", str(space or ""))
    if not space:
        return False
    hit = _SPACE_OK.get(space)
    if hit and _t.time() - hit[1] < 600:
        return hit[0]
    ok = False
    try:
        s3 = _r2(); bucket = os.getenv("R2_BUCKET", "").strip()
        if s3 is not None and bucket:
            s3.head_object(Bucket=bucket, Key=f"u/{space}/manifest.json")
            ok = True
    except Exception:
        ok = False
    _SPACE_OK[space] = (ok, _t.time())
    return ok


def _ai_headers(vendor: str) -> dict | None:
    if vendor == "anthropic":
        k = os.getenv("ANTHROPIC_API_KEY", "").strip()
        return {"x-api-key": k, "anthropic-version": "2023-06-01", "content-type": "application/json"} if k else None
    k = os.getenv("OPENAI_API_KEY", "").strip()
    return {"Authorization": f"Bearer {k}", "content-type": "application/json"} if k else None


# WHAT THE CREW'S AI COSTS, PER SPACE. Every relayed call adds its tokens and an
# estimated dollar figure to fam/ai_usage.json, one row per space per month, so
# the owner's roster can show who is spending what. Estimates from list prices;
# the vendor's bill is the truth.
_AI_PRICES = {   # dollars per million tokens: (in, out)
    "claude-haiku-4-5": (1.00, 5.00), "claude-3-5-haiku": (0.80, 4.00), "claude-3-haiku": (0.25, 1.25),
    "claude-sonnet-4": (3.00, 15.00), "claude-3-5-sonnet": (3.00, 15.00),
    "gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00), "gpt-4.1-mini": (0.40, 1.60), "gpt-4.1": (2.00, 8.00),
    "gpt-5-mini": (0.25, 2.00), "gpt-5": (1.25, 10.00),
}
_AI_USAGE_KEY = "fam/ai_usage.json"
_AI_USAGE_LOCK = None


def _ai_price(model: str):
    m = (model or "").lower()
    for k in sorted(_AI_PRICES, key=len, reverse=True):
        if m.startswith(k):
            return _AI_PRICES[k]
    return (1.00, 5.00)


def _ai_meter(space: str, vendor: str, kind: str, model: str, tokens_in: int, tokens_out: int, usd: float) -> None:
    """Best effort, never raises. One read-modify-write of a small file."""
    import json as _j, time as _t, datetime as _dt
    try:
        s3 = _r2(); bucket = os.getenv("R2_BUCKET", "").strip()
        if s3 is None or not bucket:
            return
        month = _dt.datetime.utcnow().strftime("%Y-%m")
        cur = {}
        try:
            obj = s3.get_object(Bucket=bucket, Key=_AI_USAGE_KEY)
            cur = _j.loads(obj["Body"].read().decode("utf-8")) or {}
        except Exception:
            cur = {}
        spaces = cur.setdefault("spaces", {})
        row = spaces.setdefault(space, {})
        if row.get("month") != month:
            # a new month: keep last month's line, start fresh
            row["prev"] = {"month": row.get("month"), "calls": row.get("calls", 0), "usd": row.get("usd", 0.0)} if row.get("month") else None
            row.update({"month": month, "calls": 0, "in": 0, "out": 0, "usd": 0.0, "by_kind": {}})
        row["calls"] = int(row.get("calls", 0)) + 1
        row["in"] = int(row.get("in", 0)) + int(tokens_in or 0)
        row["out"] = int(row.get("out", 0)) + int(tokens_out or 0)
        row["usd"] = round(float(row.get("usd", 0.0)) + float(usd or 0.0), 6)
        bk = row.setdefault("by_kind", {})
        bk[kind] = int(bk.get(kind, 0)) + 1
        row["last"] = int(_t.time() * 1000)
        row["vendor_last"] = vendor
        cur["updated"] = int(_t.time() * 1000)
        s3.put_object(Bucket=bucket, Key=_AI_USAGE_KEY, Body=_j.dumps(cur).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")
    except Exception:
        pass


def _ai_cost_from(vendor: str, url: str, body: dict, content) -> tuple:
    """(kind, model, tokens_in, tokens_out, usd) read off the vendor's answer."""
    model = str((body or {}).get("model") or "")
    if url.endswith("/images/generations"):
        n = int((body or {}).get("n") or 1)
        return ("cover art", model or "image", 0, 0, 0.04 * n)
    usage = (content or {}).get("usage") or {} if isinstance(content, dict) else {}
    if vendor == "anthropic":
        ti, to = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    else:
        ti, to = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    pi, po = _ai_price(model)
    return ("naming", model, ti, to, (ti * pi + to * po) / 1_000_000.0)


@app.post("/ai/relay")
async def ai_relay(request: Request):
    space = request.headers.get("x-prime-space", "")
    if not _space_known(space):
        return JSONResponse(status_code=403, content={"error": "unknown space"})
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "bad json"})
    url = str((payload or {}).get("url") or "")
    body = (payload or {}).get("body")
    vendor = _AI_UPSTREAMS.get(url)
    if not vendor or not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "not an allowed AI call"})
    headers = _ai_headers(vendor)
    if headers is None:
        return JSONResponse(status_code=503, content={"error": f"the crew has no {vendor} key set"})
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
            up = await client.post(url, headers=headers, json=body)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:200]})
    try:
        content = up.json()
    except Exception:
        content = {"error": (up.text or "")[:400]}
    if up.status_code < 300:
        try:
            kind, model, ti, to, usd = _ai_cost_from(vendor, url, body, content)
            _ai_meter(_SAFE.sub("", str(space)), vendor, kind, model, ti, to, usd)
        except Exception:
            pass
    return JSONResponse(status_code=up.status_code, content=content)


@app.post("/ai/whisper")
async def ai_whisper(request: Request):
    """The lyric ear: the clip rides here as multipart and goes to Whisper with
    the crew's OpenAI key. Answers plain text like Whisper does."""
    from fastapi.responses import PlainTextResponse
    space = request.headers.get("x-prime-space", "")
    if not _space_known(space):
        return JSONResponse(status_code=403, content={"error": "unknown space"})
    k = os.getenv("OPENAI_API_KEY", "").strip()
    if not k:
        return JSONResponse(status_code=503, content={"error": "the crew has no openai key set"})
    try:
        form = await request.form()
        f = form.get("file")
        if f is None:
            return JSONResponse(status_code=400, content={"error": "no file"})
        data = await f.read()
        if len(data) > 25 * 1024 * 1024:
            return JSONResponse(status_code=413, content={"error": "clip too big"})
        async with httpx.AsyncClient(timeout=httpx.Timeout(150.0)) as client:
            up = await client.post("https://api.openai.com/v1/audio/transcriptions",
                                   headers={"Authorization": f"Bearer {k}"},
                                   files={"file": (getattr(f, "filename", "clip.mp3") or "clip.mp3", data, "audio/mpeg")},
                                   data={"model": str(form.get("model") or "whisper-1"),
                                         "response_format": str(form.get("response_format") or "text")})
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)[:200]})
    if up.status_code < 300:
        # Whisper bills by the minute; the clip is about a minute
        _ai_meter(_SAFE.sub("", str(space)), "openai", "listening", "whisper-1", 0, 0, 0.006)
    return PlainTextResponse(up.text or "", status_code=up.status_code)


# ---------------------------------------------------------------------------
# The crew: every space in the bucket with its DJ name and size, for a phone
# that wants to browse the homies' libraries. Cached ten minutes.
# ---------------------------------------------------------------------------
_CREW_CACHE = {"at": 0.0, "rows": []}


@app.get("/crew")
async def crew(space: str = Query("")):
    import json as _j, time as _t
    me = _SAFE.sub("", str(space or ""))
    if not _space_known(me):
        return JSONResponse(status_code=403, content={"ok": False, "error": "unknown space"})
    if _t.time() - _CREW_CACHE["at"] > 600 or not _CREW_CACHE["rows"]:
        rows = []
        try:
            s3 = _r2(); bucket = os.getenv("R2_BUCKET", "").strip()
            pref = s3.list_objects_v2(Bucket=bucket, Prefix="u/", Delimiter="/")
            spaces = [p["Prefix"].split("/")[1] for p in pref.get("CommonPrefixes", [])][:60]
            for sp in spaces:
                dj, count, pls = "", None, 0
                try:
                    m = s3.get_object(Bucket=bucket, Key=f"u/{sp}/meta.json")
                    dj = (_j.loads(m["Body"].read().decode("utf-8")) or {}).get("dj") or ""
                except Exception:
                    pass
                try:
                    lib = s3.get_object(Bucket=bucket, Key=f"u/{sp}/library.json")
                    got = _j.loads(lib["Body"].read().decode("utf-8"))
                    arr = got.get("tracks", []) if isinstance(got, dict) else (got or [])
                    count = len(arr)
                except Exception:
                    continue                      # no library: not a crew space
                try:
                    pl = s3.get_object(Bucket=bucket, Key=f"u/{sp}/playlists.json")
                    pg = _j.loads(pl["Body"].read().decode("utf-8"))
                    pls = len(pg.get("playlists", []) if isinstance(pg, dict) else (pg or []))
                except Exception:
                    pls = 0
                if count:
                    rows.append({"space": sp, "dj": dj, "count": count, "playlists": pls})
        except Exception:
            rows = _CREW_CACHE["rows"]
        _CREW_CACHE.update({"at": _t.time(), "rows": rows})
    out = [r for r in _CREW_CACHE["rows"] if r["space"] != me]
    out.sort(key=lambda r: -(r.get("count") or 0))
    return {"ok": True, "crew": out}


# ---------------------------------------------------------------------------
# Lyrics: LRCLIB (open, free) behind the service, so the phone never depends on
# a third party's CORS headers. Timed words when it has them, plain otherwise.
# ---------------------------------------------------------------------------
_LY_CACHE: dict = {}


@app.get("/lyrics")
async def lyrics(artist: str = Query(""), title: str = Query(""), duration: int = Query(0)):
    import time as _t
    artist = (artist or "").strip()[:200]; title = (title or "").strip()[:200]
    if not title:
        return JSONResponse(status_code=400, content={"ok": False, "error": "no title"})
    key = f"{artist.lower()}|{title.lower()}|{int(duration or 0)}"
    hit = _LY_CACHE.get(key)
    if hit and _t.time() - hit[1] < 86400:
        return hit[0]
    out = {"ok": True, "synced": "", "plain": ""}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0), headers={"Lrclib-Client": "PrimeRip/2.0"}) as client:
            params = {"artist_name": artist, "track_name": title}
            if duration:
                params["duration"] = int(duration)
            r = await client.get("https://lrclib.net/api/get", params=params)
            j = r.json() if r.status_code == 200 else None
            if not j:
                # loose search when the exact pair misses (a remix tag, a feat.)
                r2 = await client.get("https://lrclib.net/api/search", params={"track_name": title, "artist_name": artist} if artist else {"q": title})
                arr = r2.json() if r2.status_code == 200 else []
                if isinstance(arr, list) and arr:
                    # prefer a timed result close to the length we know
                    def score(x):
                        d = abs(int(x.get("duration") or 0) - int(duration or 0)) if duration else 0
                        return (0 if x.get("syncedLyrics") else 1, d)
                    j = sorted(arr, key=score)[0]
            if isinstance(j, dict):
                out["synced"] = j.get("syncedLyrics") or ""
                out["plain"] = j.get("plainLyrics") or ""
    except Exception as e:
        out = {"ok": True, "synced": "", "plain": "", "note": str(e)[:100]}
    if len(_LY_CACHE) > 5000:
        _LY_CACHE.clear()
    _LY_CACHE[key] = (out, _t.time())
    return out


@app.post("/share_log")
async def share_log(
    space: str = Query(...),     # the sender's space
    to: str = Query(...),        # who they sent Prime Rip to
    dj: str = Query(""),         # the sender's DJ name
):
    """WHO SHARED PRIME RIP, AND TO WHOM. A phone that sends the download email
    leaves a note in u/<space>/inbox_shares.json; that space's desktop logs it
    to the network on its next tick and clears the inbox, so the owner sees
    every share under Network → Shares."""
    import json as _j, re as _re, time as _t
    space = _SAFE.sub("", str(space))
    to = (to or "").strip().lower()[:120]
    if not space or not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to):
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad request"})
    s3 = _r2()
    bucket = os.getenv("R2_BUCKET", "").strip()
    if s3 is None or not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    ik = f"u/{space}/inbox_shares.json"
    cur = []
    try:
        obj = s3.get_object(Bucket=bucket, Key=ik)
        got = _j.loads(obj["Body"].read().decode("utf-8"))
        cur = got.get("shares", []) if isinstance(got, dict) else (got or [])
    except Exception:
        cur = []
    cur.append({"to": to, "dj": (dj or "").strip()[:60], "via": "prime go", "ts": int(_t.time() * 1000)})
    cur = cur[-200:]
    try:
        s3.put_object(Bucket=bucket, Key=ik, Body=_j.dumps({"shares": cur}).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:160]})
    return {"ok": True}


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
# START A WAITING LIST FROM THE PHONE.
#
# John: "on prgo I want to go to settings where it shows what's currently
# ripping and then be able to also select the pending playlist to rip."
#
# The desktop publishes what is still queued (u/<space>/pending.json) and reads
# this inbox every twenty seconds. A tap leaves one ask here naming which of
# that desktop's OWN groups to start — source, playlist, owner, nothing else.
# It cannot name a file, a path or a command; the desktop looks the group up in
# its own database and runs the same Resume it runs for itself.
# ---------------------------------------------------------------------------
_RESUME_KEY = "resume_inbox.json"
_RESUME_MAX = 20


@app.post("/resume_ask")
async def resume_ask(space: str = Query(...), source: str = Query(""),
                     playlist: str = Query(""), owner: str = Query("")):
    space = _SAFE.sub("", str(space))
    if not space:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad request"})
    source = (source or "").strip()[:60]
    playlist = (playlist or "").strip()[:200]
    owner = (owner or "").strip()[:120]
    if not (source or playlist):
        return JSONResponse(status_code=400, content={"ok": False, "error": "name a list"})
    s3 = _r2()
    bucket = os.getenv("R2_BUCKET", "").strip()
    if s3 is None or not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    import json as _j, time as _t
    key = f"u/{space}/{_RESUME_KEY}"
    asks = []
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        got = _j.loads(obj["Body"].read().decode("utf-8"))
        cur = got.get("asks") if isinstance(got, dict) else got
        if isinstance(cur, list):
            asks = [a for a in cur if isinstance(a, dict)]
    except Exception:
        asks = []
    sig = (source, playlist, owner)
    # asking twice for the same list is one ask, so a double tap costs nothing
    if not any((a.get("source"), a.get("playlist"), a.get("owner")) == sig for a in asks):
        asks.append({"source": source, "playlist": playlist, "owner": owner,
                     "at": int(_t.time())})
    asks = asks[-_RESUME_MAX:]
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=_j.dumps({"asks": asks}).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:160]})
    return {"ok": True, "waiting": len(asks)}


# ---------------------------------------------------------------------------
# THE LISTS YOU KEEP, WHEREVER YOU ARE.
#
# John: "for the desktop library lets also wire in the made for you playlist and
# the playlist that we also save."
#
# Adding a list to your home screen on Prime Go wrote the key into the PHONE'S
# localStorage and nowhere else, so the desktop had no idea those lists existed.
# The bucket is the only thing both ends can reach, so the pins live there:
# u/<space>/pins.json. The phone still keeps its local copy and works offline;
# this is the shared truth both it and Prime Rip read.
# ---------------------------------------------------------------------------
_PINS_KEY = "pins.json"
_PINS_MAX = 200


def _pins_read(s3, bucket: str, space: str) -> list:
    import json as _j
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"u/{space}/{_PINS_KEY}")
        got = _j.loads(obj["Body"].read().decode("utf-8"))
        keys = got.get("keys", []) if isinstance(got, dict) else (got or [])
        if not isinstance(keys, list):
            return []
        out, seen = [], set()
        for k in keys:
            k = str(k or "").strip()[:120]
            if k and k not in seen:
                seen.add(k); out.append(k)
        return out[:_PINS_MAX]
    except Exception:
        return []


@app.post("/pins_push")
async def pins_push(space: str = Query(...), keys: str = Query("")):
    """Replace this space's pinned lists. The phone sends the whole set after
    every change, so an add and a remove are the same call and a lost request
    is fixed by the next one."""
    space = _SAFE.sub("", str(space))
    if not space:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad request"})
    s3 = _r2()
    bucket = os.getenv("R2_BUCKET", "").strip()
    if s3 is None or not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    import json as _j, time as _t
    want, seen = [], set()
    for k in str(keys or "").split(","):
        k = k.strip()[:120]
        if k and k not in seen:
            seen.add(k); want.append(k)
    want = want[:_PINS_MAX]
    try:
        s3.put_object(Bucket=bucket, Key=f"u/{space}/{_PINS_KEY}",
                      Body=_j.dumps({"keys": want, "at": int(_t.time())}).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:160]})
    return {"ok": True, "count": len(want)}


@app.get("/pins")
async def pins_get(space: str = Query(...)):
    """This space's pinned lists. The phone and the desktop both read the public
    object directly when they can; this is the fallback."""
    space = _SAFE.sub("", str(space))
    if not space:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad request"})
    s3 = _r2()
    bucket = os.getenv("R2_BUCKET", "").strip()
    if s3 is None or not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    return {"ok": True, "keys": _pins_read(s3, bucket, space)}


# ---------------------------------------------------------------------------
# ONE PLAY HISTORY FOR BOTH PLAYERS.
#
# John: "lets have whatever songs i play on prgo and prime rip be the same
# recent played etc." Before this the phone kept its plays on the phone and the
# desktop kept its plays in the desktop database, so Recently played said two
# different things depending on which screen you were looking at.
#
# The bucket is the only thing both of them can reach, so the log lives there:
# u/<space>/plays.json, newest last, capped. Both ends append to it and both
# ends read it. A play is (raw_id, second) — the same song played twice in the
# same second is one play, so a retry after a dropped connection costs nothing.
# ---------------------------------------------------------------------------
_PLAY_KEY = "plays.json"
_PLAY_MAX = 600


def _plays_read(s3, bucket: str, space: str) -> list:
    import json as _j
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"u/{space}/{_PLAY_KEY}")
        got = _j.loads(obj["Body"].read().decode("utf-8"))
        items = got.get("plays", []) if isinstance(got, dict) else (got or [])
        return [x for x in items if isinstance(x, dict) and x.get("id") is not None] if isinstance(items, list) else []
    except Exception:
        return []


@app.post("/play_log")
async def play_log(
    space: str = Query(...),
    ids: str = Query(...),
    src: str = Query(""),
    ts: int = Query(0),
):
    """Record one or more plays against this space. ids is a comma separated
    list of raw_ids, so the desktop can send a whole set in one call instead of
    one request a song."""
    space = _SAFE.sub("", str(space))
    if not space:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad request"})
    s3 = _r2()
    bucket = os.getenv("R2_BUCKET", "").strip()
    if s3 is None or not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    import json as _j, time as _t
    now = int(ts) if ts else int(_t.time() * 1000)
    src = (src or "").strip()[:12] or "?"
    want = []
    for part in str(ids or "").split(","):
        part = part.strip()
        if part.isdigit():
            want.append(int(part))
    if not want:
        return JSONResponse(status_code=400, content={"ok": False, "error": "no ids"})
    cur = _plays_read(s3, bucket, space)
    seen = {(int(c.get("id") or 0), int((c.get("ts") or 0) // 1000)) for c in cur}
    added = 0
    for rid in want[-100:]:
        sig = (rid, now // 1000)
        if sig in seen:
            continue
        seen.add(sig)
        cur.append({"id": rid, "ts": now, "src": src})
        added += 1
    cur.sort(key=lambda c: c.get("ts") or 0)
    cur = cur[-_PLAY_MAX:]
    try:
        s3.put_object(Bucket=bucket, Key=f"u/{space}/{_PLAY_KEY}",
                      Body=_j.dumps({"plays": cur}).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)[:160]})
    return {"ok": True, "added": added, "count": len(cur)}


@app.get("/plays")
async def plays(space: str = Query(...), limit: int = Query(200)):
    """The shared play history, NEWEST FIRST. The phone and the desktop both
    read this so Recently played says the same thing on both."""
    space = _SAFE.sub("", str(space))
    if not space:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad request"})
    s3 = _r2()
    bucket = os.getenv("R2_BUCKET", "").strip()
    if s3 is None or not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    got = _plays_read(s3, bucket, space)
    got.sort(key=lambda c: c.get("ts") or 0, reverse=True)
    try:
        n = max(1, min(600, int(limit)))
    except Exception:
        n = 200
    return {"ok": True, "plays": got[:n]}


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


# ==================== SONG REQUESTS FROM THE FLOOR ====================
# The Decks page shows a QR. A guest scans it, lands on /req/<space>, types a
# song and a name, and the note goes to u/<space>/inbox_requests.json. That
# space's desktop drains the inbox every few seconds while the Decks page is
# open and the request pops up on the decks with the matching song found.
_REQ_DJ_CACHE: dict = {}
_REQ_RATE: dict = {}


def _req_dj(space: str) -> str:
    import json as _j, time as _t
    hit = _REQ_DJ_CACHE.get(space)
    if hit and _t.time() - hit[0] < 600:
        return hit[1]
    dj = ""
    try:
        s3 = _r2(); bucket = os.getenv("R2_BUCKET", "").strip()
        m = s3.get_object(Bucket=bucket, Key=f"u/{space}/meta.json")
        dj = ((_j.loads(m["Body"].read().decode("utf-8")) or {}).get("dj") or "").strip()[:60]
    except Exception:
        dj = ""
    _REQ_DJ_CACHE[space] = (_t.time(), dj)
    return dj


_REQ_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Request a song</title>
<style>
  :root{color-scheme:dark}
  body{margin:0;background:#0b0d12;color:#e8edf4;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}
  .card{width:min(440px,92vw);padding:28px 22px 26px;background:#141821;border:1px solid #262c38;border-radius:18px;box-shadow:0 20px 60px rgba(0,0,0,.6)}
  .brand{font-size:11px;letter-spacing:.26em;text-transform:uppercase;color:#4de1ff;font-weight:800}
  h1{font-size:24px;margin:10px 0 4px;line-height:1.2}
  .dj{color:#8fa0b5;font-size:14px;margin:0 0 18px}
  label{display:block;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#8fa0b5;font-weight:800;margin:14px 0 6px}
  input{width:100%;box-sizing:border-box;background:#0b0d12;border:1px solid #2a2f3a;border-radius:12px;color:#fff;font-size:17px;padding:14px 14px;outline:none}
  input:focus{border-color:#4de1ff}
  button{width:100%;margin-top:18px;background:#4de1ff;color:#0b0d12;border:0;border-radius:12px;font-size:16px;font-weight:900;letter-spacing:.04em;padding:15px 0;cursor:pointer}
  button:disabled{opacity:.55}
  .done{display:none;text-align:center;padding:14px 0 4px}
  .done b{display:block;font-size:22px;margin-bottom:6px}
  .note{font-size:12.5px;color:#8fa0b5;margin-top:14px;text-align:center}
  .again{background:transparent;color:#4de1ff;border:1px solid #2a2f3a;margin-top:12px}
  .err{color:#ff8f86;font-size:13px;margin-top:10px;min-height:16px;text-align:center}
</style></head><body>
<div class="card">
  <div class="brand">Prime Rip · Requests</div>
  <h1>Request a song</h1>
  <p class="dj">__DJ__</p>
  <form id="f">
    <label for="song">Song and artist</label>
    <input id="song" name="song" maxlength="120" placeholder="Song — Artist" autocomplete="off" autofocus required>
    <label for="name">Your name <span style="opacity:.6;letter-spacing:0;text-transform:none;font-weight:600">(optional)</span></label>
    <input id="name" name="name" maxlength="40" placeholder="So the DJ knows who asked" autocomplete="off">
    <button id="go" type="submit">Send it to the booth</button>
    <div class="err" id="err"></div>
  </form>
  <div class="done" id="done">
    <b>Sent to the booth. 🎧</b>
    <span id="echo"></span>
    <button class="again" id="again" type="button">Request another</button>
  </div>
  <p class="note">Party on.</p>
</div>
<script>
  const f=document.getElementById('f'),go=document.getElementById('go'),err=document.getElementById('err');
  try{document.getElementById('name').value=localStorage.getItem('pr_req_name')||''}catch(e){}
  f.onsubmit=async(e)=>{e.preventDefault();const song=document.getElementById('song').value.trim();const name=document.getElementById('name').value.trim();
    if(!song)return;go.disabled=true;go.textContent='Sending…';err.textContent='';
    try{localStorage.setItem('pr_req_name',name)}catch(e){}
    let ok=false,msg='';
    for(let i=0;i<12&&!ok;i++){try{const r=await fetch('/request?space=__SPACE__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({song,name})});
      const j=await r.json().catch(()=>({}));ok=!!(r.ok&&j.ok);msg=j.error||'';if(!ok&&(r.status===429||r.status===400||r.status===403))break;}
      catch(x){msg='no signal';go.textContent=i<2?'Sending…':'Waking up the booth…';await new Promise(r=>setTimeout(r,3000));}}
    if(ok){f.style.display='none';document.getElementById('done').style.display='block';document.getElementById('echo').textContent='“'+song+'”'+(name?' — '+name:'');}
    else{err.textContent=msg==='slow down'?'Easy — a few requests a minute is plenty.':'Could not reach the booth. Try again in a second.';go.disabled=false;go.textContent='Send it to the booth';}
  };
  document.getElementById('again').onclick=()=>{document.getElementById('done').style.display='none';f.style.display='block';document.getElementById('song').value='';document.getElementById('song').focus();};
</script></body></html>"""


@app.get("/req/{space}")
async def request_page(space: str):
    from fastapi.responses import HTMLResponse
    space = _SAFE.sub("", str(space or ""))
    if not space or not _space_known(space):
        return HTMLResponse("<h1 style='font-family:sans-serif'>That request line is not live.</h1>", status_code=404)
    dj = _req_dj(space)
    line = (f"{dj} is on the decks. What do you want to hear?" if dj else "The DJ is on the decks. What do you want to hear?")
    html = _REQ_PAGE.replace("__DJ__", line.replace("<", "&lt;")).replace("__SPACE__", space)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.post("/request")
async def request_post(request: Request, space: str = Query(...)):
    import json as _j, time as _t
    space = _SAFE.sub("", str(space or ""))
    if not space or not _space_known(space):
        return JSONResponse(status_code=403, content={"ok": False, "error": "unknown space"})
    # a phone gets five a minute; the floor does not need more than that
    ip = (request.client.host if request.client else "") or "?"
    now = _t.time()
    hits = [t for t in _REQ_RATE.get(ip, []) if now - t < 60]
    if len(hits) >= 5:
        return JSONResponse(status_code=429, content={"ok": False, "error": "slow down"})
    hits.append(now); _REQ_RATE[ip] = hits
    if len(_REQ_RATE) > 5000:
        _REQ_RATE.clear()
    try:
        body = await request.json()
    except Exception:
        body = {}
    song = " ".join(str(body.get("song") or "").split())[:120]
    name = " ".join(str(body.get("name") or "").split())[:40]
    if len(song) < 2:
        return JSONResponse(status_code=400, content={"ok": False, "error": "empty"})
    s3 = _r2(); bucket = os.getenv("R2_BUCKET", "").strip()
    if s3 is None or not bucket:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cloud not set up"})
    ik = f"u/{space}/inbox_requests.json"
    cur = []
    try:
        obj = s3.get_object(Bucket=bucket, Key=ik)
        got = _j.loads(obj["Body"].read().decode("utf-8"))
        cur = got.get("requests", []) if isinstance(got, dict) else (got or [])
    except Exception:
        cur = []
    cur.append({"song": song, "name": name, "ts": int(now * 1000)})
    cur = cur[-200:]
    try:
        s3.put_object(Bucket=bucket, Key=ik, Body=_j.dumps({"requests": cur}).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:80]})
    return {"ok": True}


# ==================== INSTALL REPORTS ====================
# When INSTALL ME cannot bring the engine up on a friend's machine, it posts
# its log here on its own. The owner's desktop reads fam/install_reports/
# and lists them under Network, so nobody has to screenshot anything.
@app.post("/install_report")
async def install_report(request: Request, host: str = Query(""), os_: str = Query("", alias="os")):
    import time as _t
    body = await request.body()
    text = body[:65536].decode("utf-8", "ignore")
    if not text.strip():
        return JSONResponse(status_code=400, content={"ok": False})
    s3 = _r2(); bucket = os.getenv("R2_BUCKET", "").strip()
    if s3 is None or not bucket:
        return JSONResponse(status_code=503, content={"ok": False})
    host = _SAFE.sub("", str(host or ""))[:40] or "unknown"
    os_ = _SAFE.sub("", str(os_ or ""))[:10] or "os"
    key = f"fam/install_reports/{int(_t.time())}_{os_}_{host}.txt"
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"), ContentType="text/plain; charset=utf-8")
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:80]})
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
