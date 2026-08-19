"""Stream resolution (yt-dlp) and audio/video capture (ffmpeg)."""

from __future__ import annotations

import collections
import datetime
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

import requests
import yt_dlp

from yln.config import app_dir

BYTES_PER_SEC = 32000  # 16000 samples/s * 2 bytes/sample (mono, s16le)

FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


class CaptureError(Exception):
    """Raised for stream resolution / ffmpeg failures. Message is Korean-facing."""


@dataclass
class StreamInfo:
    media_url: str
    title: str
    is_live: bool
    is_local: bool = False


def resolve_stream(url: str) -> StreamInfo:
    """Resolve a YouTube URL to a direct playable media URL via yt-dlp."""
    local = Path(url)
    if local.exists() and local.is_file():
        return StreamInfo(media_url=str(local), title=local.stem, is_live=False, is_local=True)

    ydl_opts = {
        "format": "best[height<=1080]/bestvideo[height<=1080]+bestaudio/best",
        "quiet": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise CaptureError(f"유튜브 스트림 정보를 가져오지 못했습니다: {e}") from e

    if info is None:
        raise CaptureError("유튜브 스트림 정보를 가져오지 못했습니다.")

    media_url: Optional[str] = info.get("url")

    if not media_url:
        formats = info.get("formats") or []
        candidates = [
            f
            for f in formats
            if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none") and f.get("url")
        ]
        if candidates:
            candidates.sort(key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)))
            media_url = candidates[-1]["url"]
        elif info.get("manifest_url"):
            media_url = info["manifest_url"]
        elif formats:
            media_url = formats[-1].get("url")

    is_live = bool(info.get("is_live"))
    if not media_url:
        if is_live:
            raise CaptureError("재생 가능한 스트림 URL을 찾을 수 없습니다.")
        # VOD mode downloads the file itself (download_vod); the direct URL is
        # never used, so an extraction without one must not fail the session.
        media_url = ""

    return StreamInfo(
        media_url=media_url,
        title=info.get("title") or url,
        is_live=is_live,
    )


def new_session_dir() -> Path:
    """Create and return a fresh per-session temp directory."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = Path(tempfile.gettempdir()) / "yln_sessions" / timestamp
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _download_ffmpeg_windows(dest_dir: Path, on_status: Optional[Callable[[str], None]] = None) -> str:
    """Download the gyan.dev ffmpeg-essentials build and extract just ffmpeg.exe
    into dest_dir. Returns the path to the extracted exe. Raises CaptureError on
    any failure (network, zip format, missing member)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_exe = dest_dir / "ffmpeg.exe"

    if on_status:
        on_status("ffmpeg 다운로드 중... (최초 1회, 약 80MB)")

    tmp_zip = dest_dir / "ffmpeg_download.tmp.zip"
    try:
        try:
            with requests.get(FFMPEG_DOWNLOAD_URL, stream=True, timeout=300) as resp:
                resp.raise_for_status()
                with open(tmp_zip, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
        except requests.RequestException as e:
            raise CaptureError(
                f"ffmpeg 자동 다운로드에 실패했습니다: {e}\n"
                "수동으로 ffmpeg를 설치하거나, ffmpeg.exe를 실행 파일과 같은 폴더에 넣어주세요."
            ) from e

        try:
            with zipfile.ZipFile(tmp_zip) as zf:
                member = next(
                    (n for n in zf.namelist() if n.replace("\\", "/").endswith("/bin/ffmpeg.exe")),
                    None,
                )
                if member is None:
                    raise CaptureError(
                        "다운로드한 ffmpeg 압축 파일에서 ffmpeg.exe를 찾지 못했습니다. "
                        "수동으로 ffmpeg를 설치하거나, ffmpeg.exe를 실행 파일과 같은 폴더에 넣어주세요."
                    )
                with zf.open(member) as src, open(dest_exe, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        except zipfile.BadZipFile as e:
            raise CaptureError(
                f"다운로드한 ffmpeg 압축 파일이 손상되었습니다: {e}\n"
                "수동으로 ffmpeg를 설치하거나, ffmpeg.exe를 실행 파일과 같은 폴더에 넣어주세요."
            ) from e
    finally:
        try:
            tmp_zip.unlink(missing_ok=True)
        except OSError:
            pass

    return str(dest_exe)


def ensure_ffmpeg(on_status: Optional[Callable[[str], None]] = None) -> str:
    """Resolve (and if necessary, auto-provision) an ffmpeg binary. Order:

    1. <app_dir>/ffmpeg[.exe]        - bundled at build time
    2. <app_dir>/bin/ffmpeg[.exe]    - auto-download target dir (from a previous run)
    3. PATH (shutil.which)
    4. Windows only: auto-download into <app_dir>/bin/ffmpeg.exe
    5. Otherwise: raise a Korean CaptureError.
    """
    base = app_dir()
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

    candidate = base / exe_name
    if candidate.exists():
        return str(candidate)

    candidate = base / "bin" / exe_name
    if candidate.exists():
        return str(candidate)

    which_path = shutil.which("ffmpeg")
    if which_path:
        return which_path

    if os.name == "nt":
        return _download_ffmpeg_windows(base / "bin", on_status=on_status)

    raise CaptureError(
        "ffmpeg 실행 파일을 찾을 수 없습니다. macOS는 'brew install ffmpeg'로 설치하거나, "
        "ffmpeg를 PATH에 등록하거나, 실행 파일과 같은 폴더에 ffmpeg를 넣어주세요."
    )


def ensure_deno() -> Optional[str]:
    """Locate a deno binary (app_dir -> app_dir/bin -> PATH), or None.

    deno is the JS runtime yt-dlp needs to solve YouTube's n-challenge for the
    web_embedded client (the only token-free client whose VOD media URLs still
    work as of 2026; node fails here — its permission sandbox blocks the
    solver). Absence is non-fatal: download_vod degrades to the android
    client's 360p progressive format instead."""
    base = app_dir()
    exe_name = "deno.exe" if os.name == "nt" else "deno"
    for candidate in (base / exe_name, base / "bin" / exe_name):
        # is_file, not exists: a directory named "deno" would otherwise be
        # handed to yt-dlp as the runtime path, silently breaking signature
        # decryption ("No video formats found").
        if candidate.is_file():
            return str(candidate)
    return shutil.which("deno")


def download_vod(
    url: str,
    session_dir: Path,
    ffmpeg_path: str,
    max_height: int = 1080,
    on_status: Optional[Callable[[str], None]] = None,
) -> Tuple[Path, str]:
    """Download an uploaded (non-live) YouTube video into session_dir as one
    merged mp4 and return (path, title).

    yt-dlp's default clients cannot download VODs anymore (android_vr media
    URLs answer 403, web_safari is SABR-only without URLs), so this tries, in
    order:
      1. web_embedded (+ deno for the n-challenge): full DASH ladder, no PO
         token needed; video+audio merged by ffmpeg.
      2. android progressive itag 18 (360p): no JS runtime needed — covers
         embed-disabled videos and missing deno.
    """
    session_dir = Path(session_dir)
    outtmpl = str(session_dir / "vod.%(ext)s")

    last_pct = [-100]

    def _hook(d: dict) -> None:
        if on_status is None:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                pct = int(d.get("downloaded_bytes", 0) * 100 / total)
                if pct >= last_pct[0] + 10:
                    last_pct[0] = pct - pct % 10
                    on_status(f"영상 다운로드 중... {pct}%")
        elif d.get("status") == "finished":
            last_pct[0] = -100  # DASH downloads two tracks; restart the scale

    def _attempt(player_client: str, fmt: str, deno_path: Optional[str]) -> dict:
        # Leftovers from a failed higher-quality attempt must not collide with
        # the fallback's output resolution.
        for stale in session_dir.glob("vod.*"):
            try:
                stale.unlink()
            except OSError:
                pass
        ydl_opts = {
            "format": fmt,
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            "ffmpeg_location": ffmpeg_path,
            "extractor_args": {"youtube": {"player_client": [player_client]}},
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "progress_hooks": [_hook],
        }
        if deno_path:
            ydl_opts["js_runtimes"] = {"deno": {"path": deno_path}}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info is None:
            raise CaptureError("영상 정보를 가져오지 못했습니다.")
        return info

    try:
        info = _attempt(
            "web_embedded",
            f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best",
            ensure_deno(),
        )
    except CaptureError:
        raise
    except Exception as primary_err:  # noqa: BLE001 - yt-dlp raises many types
        if on_status:
            on_status("고화질 추출에 실패해 기본 화질(360p)로 다시 시도합니다...")
        try:
            info = _attempt("android", "18/best", None)
        except Exception as fallback_err:  # noqa: BLE001
            raise CaptureError(
                f"영상 다운로드에 실패했습니다.\n1차(web_embedded): {primary_err}\n2차(android): {fallback_err}"
            ) from fallback_err

    downloads = info.get("requested_downloads") or []
    filepath = downloads[0].get("filepath") if downloads else None
    if not filepath or not Path(filepath).exists():
        candidates = [p for p in session_dir.glob("vod.*") if p.suffix != ".part"]
        if not candidates:
            raise CaptureError("다운로드된 영상 파일을 찾을 수 없습니다.")
        filepath = str(candidates[0])

    return Path(filepath), info.get("title") or url


class Capturer:
    """Spawns a single ffmpeg process producing a JPEG frame stream and a raw PCM audio file."""

    SOI = b"\xff\xd8"
    EOI = b"\xff\xd9"

    def __init__(
        self,
        media_url: str,
        frame_interval_sec: float,
        session_dir: Path,
        ffmpeg_path: str,
        start_offset_sec: float = 0.0,
    ):
        self.media_url = media_url
        self.frame_interval_sec = frame_interval_sec
        self.ffmpeg_path = ffmpeg_path
        self.start_offset_sec = max(0.0, start_offset_sec)
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.audio_path = self.session_dir / "audio.pcm"

        self._proc: Optional[subprocess.Popen] = None
        self._start_time: Optional[float] = None
        self._stderr_lines: "collections.deque[str]" = collections.deque(maxlen=50)
        self._stderr_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
        ]
        if self.start_offset_sec > 0:
            # Input-side -ss: keyframe seek, so skipping hours of dead air is
            # instant. The output timeline restarts at 0, keeping frames() and
            # read_audio_span offsets aligned without further adjustment.
            cmd += ["-ss", f"{self.start_offset_sec:g}"]
        cmd += [
            "-i", self.media_url,
            "-map", "0:v", "-vf", f"fps=1/{self.frame_interval_sec}",
            "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
            "-map", "0:a", "-ac", "1", "-ar", "16000",
            "-f", "s16le", "-c:a", "pcm_s16le", str(self.audio_path),
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
        except OSError as e:
            raise CaptureError(f"ffmpeg 실행에 실패했습니다: {e}") from e

        self._start_time = time.monotonic()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in iter(proc.stderr.readline, b""):
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                self._stderr_lines.append(text)

    def frames(self) -> Iterator[Tuple[bytes, float]]:
        """Yield (jpeg_bytes, media_ts) parsed from the ffmpeg mjpeg stdout stream.

        media_ts is frame_index * frame_interval_sec — the media-timeline
        position of the frame, which stays aligned with byte offsets in
        audio.pcm even when ffmpeg processes faster than realtime (VODs).
        """
        if self._proc is None or self._proc.stdout is None or self._start_time is None:
            raise CaptureError("Capturer가 시작되지 않았습니다.")

        stdout = self._proc.stdout
        buf = bytearray()
        frame_index = 0
        while True:
            chunk = stdout.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                soi = buf.find(self.SOI)
                if soi == -1:
                    break
                eoi = buf.find(self.EOI, soi + 2)
                if eoi == -1:
                    break
                jpeg_bytes = bytes(buf[soi : eoi + 2])
                del buf[: eoi + 2]
                yield jpeg_bytes, frame_index * self.frame_interval_sec
                frame_index += 1

    def read_audio_span(self, t0: float, t1: float, wait_timeout: float = 0.0) -> bytes:
        """Read raw PCM bytes for media-timeline span [t0, t1) seconds since capture start.

        If wait_timeout > 0, poll briefly for ffmpeg to catch up when the audio
        file is still short of the span's end by less than one frame interval's
        worth of audio (a larger shortfall means the timeline has drifted, so
        waiting would not help)."""
        start_byte = int(t0 * BYTES_PER_SEC)
        end_byte = int(t1 * BYTES_PER_SEC)
        start_byte -= start_byte % 2
        end_byte -= end_byte % 2
        if end_byte <= start_byte:
            return b""

        if wait_timeout > 0:
            deadline = time.monotonic() + wait_timeout
            while True:
                try:
                    with open(self.audio_path, "rb") as f:
                        f.seek(0, os.SEEK_END)
                        current_size = f.tell()
                except OSError:
                    break
                if (
                    current_size < end_byte
                    and self.is_running
                    and (end_byte - current_size) < 2 * BYTES_PER_SEC
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.2)
                    continue
                break

        try:
            with open(self.audio_path, "rb") as f:
                f.seek(start_byte)
                return f.read(end_byte - start_byte)
        except FileNotFoundError:
            return b""

    @property
    def last_stderr(self) -> str:
        return "\n".join(self._stderr_lines)

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def wait_returncode(self, timeout: float = 3.0) -> Optional[int]:
        """Return ffmpeg's exit code, waiting briefly for it to finish exiting
        (stdout EOF slightly precedes process exit). None if still running."""
        if self._proc is None:
            return None
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return self._proc.poll()

    @property
    def elapsed_seconds(self) -> float:
        """Media-timeline seconds captured so far, derived from the audio file size
        (same timeline as frames() timestamps and read_audio_span offsets).

        Uses seek-to-end instead of Path.stat() because on Windows the directory
        entry's cached file size lags behind writes to an open file handle."""
        try:
            with open(self.audio_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                return f.tell() / BYTES_PER_SEC
        except OSError:
            return 0.0

    def stop(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.write(b"q\n")
                    proc.stdin.flush()
                except (BrokenPipeError, ValueError, OSError):
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
