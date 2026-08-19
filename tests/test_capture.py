"""Tests for VOD acquisition in yln.capture.

2026년 현재 yt-dlp 기본 클라이언트로는 VOD도 다운로드가 403으로 막힌다
(android_vr URL은 사망, web_safari는 SABR 전용). 실측으로 검증된 우회는
web_embedded 클라이언트 + deno JS 런타임(전 화질, PO 토큰 불필요)이고,
임베드 비허용 영상 등에서는 android 클라이언트의 progressive itag 18(360p)로
폴백한다. download_vod가 정확히 이 옵션 조합으로 yt-dlp를 호출하는지를
YoutubeDL 테스트 더블로 검증한다.
"""

import io
from pathlib import Path
from typing import List

import pytest
import yt_dlp

import yln.capture as capture
from yln.capture import Capturer, CaptureError, StreamInfo, download_vod, ensure_deno, resolve_stream


# ---------------------------------------------------------------------------
# StreamInfo.is_local: 세션이 "이미 로컬 파일" 입력과 "다운로드가 필요한 VOD"를
# 구분하는 유일한 신호.
# ---------------------------------------------------------------------------


def test_streaminfo_is_local_defaults_false() -> None:
    info = StreamInfo(media_url="https://example.com/x", title="t", is_live=False)
    assert info.is_local is False


def test_resolve_stream_local_file_sets_is_local(tmp_path: Path) -> None:
    local = tmp_path / "lecture.mp4"
    local.write_bytes(b"fake")

    info = resolve_stream(str(local))

    assert info.is_local is True
    assert info.is_live is False
    assert info.media_url == str(local)


# ---------------------------------------------------------------------------
# ensure_deno: ensure_ffmpeg와 같은 탐색 순서(app_dir -> app_dir/bin -> PATH)
# 이되, 없어도 예외 없이 None을 반환해야 한다 — deno 부재는 치명이 아니라
# android 360p 폴백으로의 강등일 뿐이다.
# ---------------------------------------------------------------------------


def test_ensure_deno_returns_none_when_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(capture, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)

    assert ensure_deno() is None


def test_ensure_deno_prefers_bundled_binary(tmp_path: Path, monkeypatch) -> None:
    exe_name = "deno.exe" if capture.os.name == "nt" else "deno"
    bundled = tmp_path / exe_name
    bundled.write_bytes(b"")
    monkeypatch.setattr(capture, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(capture.shutil, "which", lambda name: "/somewhere/else/deno")

    assert ensure_deno() == str(bundled)


def test_ensure_deno_falls_back_to_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(capture, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(capture.shutil, "which", lambda name: "/usr/local/bin/deno")

    assert ensure_deno() == "/usr/local/bin/deno"


def test_ensure_deno_ignores_directory_named_deno(tmp_path: Path, monkeypatch) -> None:
    exe_name = "deno.exe" if capture.os.name == "nt" else "deno"
    (tmp_path / exe_name).mkdir()  # 디렉터리는 런타임 경로가 될 수 없다
    monkeypatch.setattr(capture, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)

    assert ensure_deno() is None


# ---------------------------------------------------------------------------
# download_vod
# ---------------------------------------------------------------------------


class _FakeYDL:
    """yt_dlp.YoutubeDL 테스트 더블: 생성 시 받은 옵션을 기록하고, 시도 순서에
    따라 미리 정한 동작(성공 또는 예외)을 수행한다. 성공 시 outtmpl 자리에
    실제 mp4 파일을 만들어 download_vod가 경로를 확정할 수 있게 한다."""

    calls: List[dict] = []
    behaviors: List = []

    def __init__(self, opts):
        self.opts = opts
        type(self).calls.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        idx = len(type(self).calls) - 1
        behavior = type(self).behaviors[min(idx, len(type(self).behaviors) - 1)]
        if isinstance(behavior, Exception):
            raise behavior
        out = Path(str(self.opts["outtmpl"]).replace("%(ext)s", "mp4"))
        out.write_bytes(b"video")
        return {"title": "테스트 영상", "requested_downloads": [{"filepath": str(out)}]}


@pytest.fixture()
def fake_ydl(monkeypatch):
    _FakeYDL.calls = []
    _FakeYDL.behaviors = ["ok"]
    monkeypatch.setattr(capture.yt_dlp, "YoutubeDL", _FakeYDL)
    return _FakeYDL


def test_download_vod_uses_web_embedded_and_deno(tmp_path: Path, monkeypatch, fake_ydl) -> None:
    monkeypatch.setattr(capture, "ensure_deno", lambda: "/opt/deno/bin/deno")

    path, title = download_vod(
        "https://youtube.com/watch?v=x", tmp_path, "/usr/bin/ffmpeg", max_height=720
    )

    assert path.exists() and path.suffix == ".mp4"
    assert title == "테스트 영상"
    assert len(fake_ydl.calls) == 1
    opts = fake_ydl.calls[0]
    assert opts["extractor_args"]["youtube"]["player_client"] == ["web_embedded"]
    assert opts["js_runtimes"] == {"deno": {"path": "/opt/deno/bin/deno"}}
    assert "height<=720" in opts["format"]
    assert opts["merge_output_format"] == "mp4"
    assert opts["ffmpeg_location"] == "/usr/bin/ffmpeg"
    assert opts["noplaylist"] is True
    assert str(tmp_path) in str(opts["outtmpl"])


def test_download_vod_without_deno_omits_js_runtimes(tmp_path: Path, monkeypatch, fake_ydl) -> None:
    monkeypatch.setattr(capture, "ensure_deno", lambda: None)

    download_vod("https://youtube.com/watch?v=x", tmp_path, "/usr/bin/ffmpeg")

    assert "js_runtimes" not in fake_ydl.calls[0]


def test_download_vod_falls_back_to_android_itag18(tmp_path: Path, monkeypatch, fake_ydl) -> None:
    monkeypatch.setattr(capture, "ensure_deno", lambda: "/opt/deno/bin/deno")
    fake_ydl.behaviors = [yt_dlp.utils.DownloadError("embed blocked"), "ok"]
    messages: List[str] = []

    path, _ = download_vod(
        "https://youtube.com/watch?v=x", tmp_path, "/usr/bin/ffmpeg", on_status=messages.append
    )

    assert path.exists()
    assert len(fake_ydl.calls) == 2
    fallback = fake_ydl.calls[1]
    assert fallback["extractor_args"]["youtube"]["player_client"] == ["android"]
    assert fallback["format"].startswith("18")
    assert "js_runtimes" not in fallback
    # 화질 강등 사실을 사용자에게 고지하고, 어떤 문구도 "완료"로 시작하지 않는다.
    assert any("360p" in m or "화질" in m for m in messages)
    assert not any(m.startswith("완료") for m in messages)


def test_download_vod_raises_capture_error_when_all_clients_fail(
    tmp_path: Path, monkeypatch, fake_ydl
) -> None:
    monkeypatch.setattr(capture, "ensure_deno", lambda: None)
    fake_ydl.behaviors = [yt_dlp.utils.DownloadError("dead")]

    with pytest.raises(CaptureError):
        download_vod("https://youtube.com/watch?v=x", tmp_path, "/usr/bin/ffmpeg")


# ---------------------------------------------------------------------------
# 기록 시작 시간(start_offset_sec): 강의가 영상 중반(예: 1:40:00)에야 시작하는
# 녹화본을 위해, Capturer가 ffmpeg 입력 시킹(-ss, 입력 옵션이라 키프레임 단위로
# 즉시)을 걸어 이전 구간을 통째로 건너뛴다. 프레임/오디오 타임라인은 0부터
# 다시 시작하므로 기존 정렬 로직(read_audio_span 등)은 무수정으로 유효하다.
# ---------------------------------------------------------------------------


def _spawn_capturer_cmd(monkeypatch, tmp_path: Path, **kwargs) -> List[str]:
    """Capturer.start()가 실제로 조립한 ffmpeg 명령을 가로채 반환한다."""
    cmds: List[List[str]] = []

    class _FakeProc:
        stdin = None
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def poll(self):
            return 0

    def fake_popen(cmd, **_kw):
        cmds.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(capture.subprocess, "Popen", fake_popen)
    capturer = Capturer("input.mp4", 2.0, tmp_path, "/usr/bin/ffmpeg", **kwargs)
    capturer.start()
    return cmds[0]


def test_capturer_offset_inserts_ss_before_input(tmp_path: Path, monkeypatch) -> None:
    cmd = _spawn_capturer_cmd(monkeypatch, tmp_path, start_offset_sec=6000.0)

    assert "-ss" in cmd
    ss_idx = cmd.index("-ss")
    assert cmd[ss_idx + 1] == "6000"
    # 입력 옵션이어야 빠른 시킹이다 — 출력 옵션 -ss는 전 구간을 디코드한다.
    assert ss_idx < cmd.index("-i")


def test_capturer_without_offset_omits_ss(tmp_path: Path, monkeypatch) -> None:
    cmd = _spawn_capturer_cmd(monkeypatch, tmp_path)

    assert "-ss" not in cmd


def test_download_vod_progress_is_throttled(tmp_path: Path, monkeypatch, fake_ydl) -> None:
    monkeypatch.setattr(capture, "ensure_deno", lambda: None)
    messages: List[str] = []

    download_vod(
        "https://youtube.com/watch?v=x", tmp_path, "/usr/bin/ffmpeg", on_status=messages.append
    )

    hooks = fake_ydl.calls[0]["progress_hooks"]
    assert hooks
    hook = hooks[0]
    for downloaded in (0, 10, 55, 56, 57, 100):
        hook({"status": "downloading", "downloaded_bytes": downloaded, "total_bytes": 100})
    progress = [m for m in messages if "%" in m]
    # 0->10->50대->100 수준으로만 보고: 1% 단위 스팸(55,56,57 각각)이 아니어야 한다.
    assert 1 <= len(progress) <= 4
