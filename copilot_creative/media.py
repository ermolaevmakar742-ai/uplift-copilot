"""Video handling: safe upload to a private temp dir, format/size/magic validation, ffmpeg probing, scene cuts, silences, frame extraction.
ffmpeg is the existing binary on PATH, else the static one shipped by the `imageio-ffmpeg` wheel (no ffprobe: metadata is parsed from `ffmpeg -i`).
All subprocesses are asyncio-based (never block the event loop), time-limited, and receive only server-generated paths."""
import asyncio
import hashlib
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field

from copilot_creative.config import ALLOWED_EXT, Config


class MediaError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def ffmpeg_path() -> str | None:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def uploads_dir(cfg: Config) -> str:
    d = os.path.join(cfg.data_dir, "uploads")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def frames_dir(cfg: Config, analysis_id: int) -> str:
    d = os.path.join(cfg.data_dir, "frames", str(int(analysis_id)))
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def safe_ext(filename: str | None) -> str:
    ext = os.path.splitext(os.path.basename(filename or ""))[1].lower()
    if ext not in ALLOWED_EXT:
        raise MediaError("unsupported_format", f"Unsupported file type '{ext or '?'}'. Allowed: {', '.join(sorted(ALLOWED_EXT))}")
    return ext


def check_magic(head: bytes, ext: str) -> None:
    kind = ALLOWED_EXT[ext]
    ok = head[4:8] == b"ftyp" if kind == "iso" else head[:4] == b"\x1a\x45\xdf\xa3"
    if not ok:
        raise MediaError("not_a_video", "File content does not match its video extension")


async def save_upload(chunks, filename: str | None, cfg: Config) -> tuple[str, str, int]:
    """Stream an upload (async iterator of bytes) to <data>/uploads/<uuid><ext>. Returns (path, sha256, size). The client-supplied name is never used."""
    ext = safe_ext(filename)
    path = os.path.join(uploads_dir(cfg), uuid.uuid4().hex + ext)
    h, size, head = hashlib.sha256(), 0, b""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            async for chunk in chunks:
                if not chunk:
                    continue
                if len(head) < 16:
                    head += chunk[:16 - len(head)]
                    if len(head) >= 12:
                        check_magic(head, ext)
                size += len(chunk)
                if size > cfg.max_upload_bytes:
                    raise MediaError("file_too_large", f"File exceeds {cfg.max_upload_mb} MB")
                h.update(chunk)
                f.write(chunk)
        if size == 0:
            raise MediaError("empty_file", "Empty file")
        if len(head) < 12:
            raise MediaError("not_a_video", "File too small to be a video")
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path, h.hexdigest(), size


def purge_old(cfg: Config, keep: set[str] = frozenset()) -> int:
    """Delete uploads older than the TTL that no pending analysis still needs."""
    n, cutoff = 0, time.time() - cfg.upload_ttl_h * 3600
    d = uploads_dir(cfg)
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if p not in keep and os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.unlink(p)
                n += 1
        except OSError:
            pass
    return n


async def _run(args: list[str], timeout: float = 120) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise MediaError("ffmpeg_timeout", "ffmpeg timed out")
    return proc.returncode, out, err


@dataclass
class Probe:
    duration_s: float = 0.0
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    has_audio: bool = False
    audio_codec: str | None = None
    container: str | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def parse_probe(stderr: str) -> Probe:
    p = Probe()
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if m:
        p.duration_s = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Input #0,\s*([^,]+)", stderr)
    if m:
        p.container = m[1].strip()
    for line in stderr.splitlines():
        if "Stream #" not in line:
            continue
        if " Video:" in line and p.video_codec is None and "attached pic" not in line:
            p.video_codec = (re.search(r"Video:\s*([\w\d]+)", line) or [None, None])[1]
            r = re.search(r"[,\s](\d{2,5})x(\d{2,5})", line)
            if r:
                p.width, p.height = int(r[1]), int(r[2])
            f = re.search(r"(\d+(?:\.\d+)?)\s*fps", line)
            if f:
                p.fps = float(f[1])
            rot = re.search(r"rotate\s*:\s*(-?\d+)", stderr)
            if rot and abs(int(rot[1])) % 180 == 90 and p.width and p.height:
                p.width, p.height = p.height, p.width
        elif " Audio:" in line and not p.has_audio:
            p.has_audio = True
            p.audio_codec = (re.search(r"Audio:\s*([\w\d]+)", line) or [None, None])[1]
    return p


async def probe(path: str) -> Probe:
    ff = ffmpeg_path()
    if not ff:
        raise MediaError("ffmpeg_missing", "ffmpeg is not installed (pip install imageio-ffmpeg or apt install ffmpeg)")
    _, _, err = await _run([ff, "-hide_banner", "-nostdin", "-i", path], 30)       # exit code 1 without output file is expected
    p = parse_probe(err.decode("utf-8", "replace"))
    if p.video_codec is None or p.duration_s <= 0:
        raise MediaError("unreadable_video", "ffmpeg could not read a video stream from this file")
    return p


async def scene_cuts(path: str, threshold: float = 0.3, limit: int = 200) -> list[float]:
    ff = ffmpeg_path()
    _, _, err = await _run([ff, "-hide_banner", "-nostdin", "-i", path, "-an", "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"], 180)
    ts = [round(float(m), 2) for m in re.findall(r"pts_time:(\d+(?:\.\d+)?)", err.decode("utf-8", "replace"))]
    return ts[:limit]


async def silences(path: str, noise_db: int = -35, min_s: float = 0.4) -> list[dict]:
    ff = ffmpeg_path()
    _, _, err = await _run([ff, "-hide_banner", "-nostdin", "-i", path, "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"], 180)
    text = err.decode("utf-8", "replace")
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?\d+(?:\.\d+)?)", text)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*(-?\d+(?:\.\d+)?)", text)]
    return [{"start": round(max(0, s), 2), "end": round(e, 2)} for s, e in zip(starts, ends)][:100]


def pick_timestamps(duration: float, cuts: list[float], n: int) -> list[float]:
    """Frame times: always the first moment (hook) and near the end, then scene cuts (just after each cut), then uniform fill. Sorted, de-duplicated."""
    if duration <= 0 or n <= 0:
        return []
    end = max(0.0, duration - 0.15)
    cand = [min(0.2, end)] + [min(c + 0.12, end) for c in cuts] + [end * k / (n - 1) for k in range(1, n)] if n > 1 else [0.0]
    chosen: list[float] = []
    min_gap = max(0.25, duration / (n * 2.5))
    for t in sorted(set(round(x, 2) for x in cand)):
        if all(abs(t - c) >= min_gap for c in chosen):
            chosen.append(t)
    if len(chosen) > n:                                         # keep first, last and evenly spread picks
        idx = sorted({round(i * (len(chosen) - 1) / (n - 1)) for i in range(n)})
        chosen = [chosen[i] for i in idx]
    return chosen


async def extract_frame(path: str, t: float, out: str, width: int) -> None:
    ff = ffmpeg_path()
    rc, _, err = await _run([ff, "-hide_banner", "-nostdin", "-y", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                             "-vf", f"scale='min({width},iw)':-2", "-q:v", "5", out], 60)
    if rc != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        raise MediaError("frame_failed", f"could not extract frame at {t:.2f}s")


async def extract_audio(path: str, out: str) -> None:
    ff = ffmpeg_path()
    rc, _, _ = await _run([ff, "-hide_banner", "-nostdin", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", out], 120)
    if rc != 0:
        raise MediaError("audio_failed", "could not extract the audio track")
