"""Tests for Session._try_publish's summary-verdict handling.

The regression under test: groups whose transcript passed the length gate but
was hallucination (e.g. Whisper prompt echoes) used to burn the retry budget on
NO_CONTENT responses and end up published as an
"(자동 요약 실패 — 전체 스크립트 서브페이지 참고)" placeholder section. A
NO_CONTENT verdict must instead carry the group over WITHOUT burning attempts
(so its slides join the next content-bearing section), and only flush it as a
silent one (screenshots only) under force. Invalid responses (None) keep the
old behavior: carry-over with attempts accounting, then placeholder only as a
forced last resort.
"""

import threading
import time
from pathlib import Path
from typing import List, Optional

import yln.session as session_mod
from yln.aggregator import Segment
from yln.capture import StreamInfo
from yln.config import Config
from yln.session import Session
from yln.summarizer import SummarizerError

# 30자 게이트를 확실히 넘기는 더미 전사 (실제로는 환각일 수 있는 텍스트).
LONG_TRANSCRIPT = "이것은 길이 게이트를 통과할 만큼 충분히 긴 세그먼트 전사 텍스트입니다."


class _StubSummarizer:
    """summarize_slide가 미리 정한 응답(들)을 순서대로 반환하는 테스트 더블.
    응답이 예외 인스턴스면 raise한다. 응답 목록이 소진되면 마지막 응답을 반복한다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def summarize_slide(self, transcript: str, topic_keywords: str = "") -> Optional[List[str]]:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


class _StubNotion:
    def __init__(self):
        self.sections = []       # append_section 호출 기록
        self.image_appends = []  # append_images 호출 기록
        self.time_labels = []    # append_section이 받은 시간 라벨
        self.uploads = 0

    def upload_image(self, image: bytes, filename: str) -> str:
        self.uploads += 1
        return f"upload-{self.uploads}"

    def append_section(self, page_id, slide_no, time_label, upload_ids, bullets):
        self.sections.append((slide_no, upload_ids, bullets))
        self.time_labels.append(time_label)

    def append_images(self, page_id, upload_ids):
        self.image_appends.append(upload_ids)

    def create_lecture_page(self, parent_page_id, title):
        return "test-page"

    def append_final_summary(self, page_id, summary):
        pass

    def create_transcript_subpage(self, page_id, title, entries):
        pass


def _make_session(*summary_responses: Optional[List[str]]):
    config = Config(
        notion_token="test-token",
        notion_parent_page_id="test-parent",
        summarizer_backend="claude_code",
    )
    session = Session(config, url="http://test", topic_keywords="AI", ui_callback=lambda msg: None)
    session._summarizer = _StubSummarizer(*summary_responses)
    session._notion = _StubNotion()
    session._page_id = "test-page"
    return session


def _add_segment(session: Session, start: float = 0.0, end: float = 20.0) -> None:
    session._agg.add(Segment(start_ts=start, end_ts=end, image=b"jpeg", transcript=LONG_TRANSCRIPT))


# 1. NO_CONTENT 판정([]) + force: placeholder 섹션 대신 스크린샷만 추가된다.
def test_no_content_verdict_publishes_screenshots_not_placeholder() -> None:
    session = _make_session([])
    _add_segment(session)

    session._try_publish(force=True)

    assert session._notion.sections == []
    assert session._notion.image_appends == [["upload-1"]]
    assert session._agg.attempts == 0        # 판정은 재시도 예산을 태우지 않는다
    assert not session._agg.has_pending()
    assert session._slide_count == 0


# 2. NO_CONTENT 판정 + 비강제: carry-over 지속 — 그룹을 버리지 않고 재시도
# 예산도 태우지 않는다. (즉시 flush하면 이후 실제 발화 섹션이 형성되지 못한다.)
def test_no_content_verdict_without_force_carries_over() -> None:
    session = _make_session([])
    _add_segment(session)

    session._try_publish(force=False)

    assert session._notion.sections == []
    assert session._notion.image_appends == []
    assert session._agg.has_pending()
    assert session._agg.attempts == 0


# 3. carry-over 설계의 핵심: NO_CONTENT 그룹은 뒤에 실제 발화가 오면 그 슬라이드
# 이미지까지 포함한 하나의 정상 섹션으로 발행된다.
def test_no_content_group_joins_next_content_section() -> None:
    session = _make_session([], ["F1 스코어는 정밀도와 재현율의 조화평균"])
    _add_segment(session, start=0.0, end=20.0)
    session._try_publish(force=False)         # NO_CONTENT -> carry-over
    _add_segment(session, start=20.0, end=40.0)
    session._try_publish(force=False)         # 실제 내용 합류 -> 정상 발행

    assert len(session._notion.sections) == 1
    slide_no, upload_ids, bullets = session._notion.sections[0]
    assert slide_no == 1
    assert len(upload_ids) == 2               # 앞선 무내용 세그먼트의 이미지도 포함
    assert bullets == ["F1 스코어는 정밀도와 재현율의 조화평균"]
    assert not session._agg.has_pending()


# 4. 무효 응답(None) + 비강제: 기존대로 carry-over 하고 재시도 예산만 소진한다.
def test_invalid_response_without_force_carries_over() -> None:
    session = _make_session(None)
    _add_segment(session)

    session._try_publish(force=False)

    assert session._notion.sections == []
    assert session._notion.image_appends == []
    assert session._agg.has_pending()
    assert session._agg.attempts == 1


# 5. 무효 응답(None) + force: 최후 수단 placeholder 발행은 그대로 유지된다.
def test_invalid_response_with_force_still_publishes_placeholder() -> None:
    session = _make_session(None)
    _add_segment(session)

    session._try_publish(force=True)

    assert len(session._notion.sections) == 1
    slide_no, upload_ids, bullets = session._notion.sections[0]
    assert slide_no == 1
    assert bullets == ["(자동 요약 실패 — 전체 스크립트 서브페이지 참고)"]
    assert session._slide_summaries == []    # placeholder는 전체 요약에 섞이지 않는다
    assert not session._agg.has_pending()


# 6. 정상 불릿: 섹션 발행 + 전체 요약 수집 (기존 동작 회귀 확인).
def test_valid_bullets_publish_a_section() -> None:
    session = _make_session(["F1 스코어는 정밀도와 재현율의 조화평균"])
    _add_segment(session)

    session._try_publish(force=False)

    assert len(session._notion.sections) == 1
    assert session._slide_count == 1
    assert session._slide_summaries == [["F1 스코어는 정밀도와 재현율의 조화평균"]]
    assert not session._agg.has_pending()


# 7. 백엔드 장애(SummarizerError)는 재시도 예산을 태우지 않는다 — 예산은 "모델
# 응답 품질" 계량이므로, 장애가 소진시키면 복구 후에도 lockout 때문에 호출이
# 시도조차 안 되고 placeholder로 끝난다.
def test_summarizer_error_does_not_burn_attempts_and_recovers() -> None:
    session = _make_session(
        SummarizerError("백엔드 장애"), ["F1 스코어는 정밀도와 재현율의 조화평균"]
    )
    _add_segment(session, start=0.0, end=20.0)
    session._try_publish(force=False)

    assert session._agg.attempts == 0
    assert session._agg.has_pending()
    assert session._notion.sections == []

    _add_segment(session, start=20.0, end=40.0)
    session._try_publish(force=False)         # 백엔드 복구 -> 정상 발행

    assert len(session._notion.sections) == 1
    assert session._notion.sections[0][2] == ["F1 스코어는 정밀도와 재현율의 조화평균"]
    assert not session._agg.has_pending()


# 8. SummarizerError 외의 예외(요약기 버그 등)도 발행 루프를 잠그지 않는다 —
# pop_section 이전에 예외가 전파되면 그룹이 영영 비워지지 않아 남은 세션 전체가
# 한 섹션도 발행되지 않는 최악의 경로가 있었다.
def test_unexpected_summarizer_exception_does_not_lock_publishing() -> None:
    session = _make_session(ValueError("응답 파싱 실패"), ["정상 요약 불릿"])
    _add_segment(session, start=0.0, end=20.0)

    session._try_publish(force=False)         # 예외가 전파되면 여기서 실패

    assert session._agg.attempts == 0
    assert session._agg.has_pending()

    _add_segment(session, start=20.0, end=40.0)
    session._try_publish(force=False)

    assert len(session._notion.sections) == 1
    assert session._notion.sections[0][2] == ["정상 요약 불릿"]


# 9. 무효 응답(None)으로 재시도 예산 2회가 소진되면 이후에는 LLM 호출 자체를
# 건너뛴다 (lockout 분기 커버).
def test_attempts_lockout_skips_llm_calls() -> None:
    session = _make_session(None)
    _add_segment(session, start=0.0, end=20.0)
    session._try_publish(force=False)
    _add_segment(session, start=20.0, end=40.0)
    session._try_publish(force=False)

    assert session._agg.attempts == 2
    calls_at_lockout = session._summarizer.calls

    _add_segment(session, start=40.0, end=60.0)
    session._try_publish(force=False)

    assert session._summarizer.calls == calls_at_lockout
    assert session._agg.has_pending()


# ---------------------------------------------------------------------------
# Stall watchdog: YouTube가 라이브 HLS 세그먼트를 403으로 끊으면 ffmpeg는 EOF
# 없이 영원히 재시도만 하며 audio.pcm 성장이 멈춘다. 워치독은 이 "조용한 정지"를
# 감지해 캡처를 종료시켜 세션이 지금까지의 내용으로 마무리되게 해야 한다.
# ---------------------------------------------------------------------------


class _StubCapturer:
    """elapsed_seconds를 미리 정한 순서대로 돌려주는 워치독 테스트 더블.

    시퀀스가 소진되면 마지막 값을 반복한다. hang=True면 소진 후에도 is_running이
    True로 남아 ffmpeg가 EOF 없이 매달린 상황(403 루프)을 흉내 낸다."""

    def __init__(self, elapsed_seq, stderr="", hang=False):
        self._seq = list(elapsed_seq)
        self._i = 0
        self._stderr = stderr
        self._hang = hang
        self.stop_called = False

    @property
    def elapsed_seconds(self) -> float:
        if self._i < len(self._seq):
            value = self._seq[self._i]
            self._i += 1
            return value
        return self._seq[-1]

    @property
    def is_running(self) -> bool:
        if self.stop_called:
            return False
        return self._hang or self._i < len(self._seq)

    @property
    def last_stderr(self) -> str:
        return self._stderr

    def stop(self) -> None:
        self.stop_called = True


def _make_watch_session(stall_timeout: float):
    config = Config(
        notion_token="test-token",
        notion_parent_page_id="test-parent",
        summarizer_backend="claude_code",
        stall_timeout_sec=stall_timeout,
    )
    messages: List[str] = []
    session = Session(config, url="http://test", topic_keywords="", ui_callback=messages.append)
    return session, messages


# 10. 오디오가 멈춘 채 ffmpeg만 살아 있으면 워치독이 캡처를 종료시키고, 경고와
# ffmpeg stderr 꼬리를 사용자에게 보여준다. 어떤 메시지도 "완료"로 시작하면 안
# 된다 (app.py의 세션 종료 UI 트리거).
def test_watchdog_stops_capture_on_frozen_audio() -> None:
    session, messages = _make_watch_session(stall_timeout=0.2)
    capturer = _StubCapturer([25.0], stderr="Failed to open segment 123\nHTTP error 403 Forbidden", hang=True)
    session._capturer = capturer

    session._watch_stall()

    assert capturer.stop_called
    assert session._stall_detected
    assert any("경고" in m for m in messages)
    assert any("403" in m for m in messages)
    assert not any(m.startswith("완료") for m in messages)


# 11. 오디오가 계속 자라는 동안에는 절대 개입하지 않고, ffmpeg가 스스로 끝나면
# (is_running False) 조용히 물러난다.
def test_watchdog_ignores_growing_audio() -> None:
    session, messages = _make_watch_session(stall_timeout=0.2)
    capturer = _StubCapturer([1.0, 2.0, 3.0, 4.0, 5.0])
    session._capturer = capturer

    session._watch_stall()

    assert not capturer.stop_called
    assert not session._stall_detected
    assert messages == []


# 12. 타임아웃보다 짧게 멈췄다 다시 자라기 시작하면 스톨로 판정하지 않는다
# (frozen 타이머가 성장 재개 시 리셋되는지 검증).
def test_watchdog_resets_timer_when_audio_resumes() -> None:
    session, _ = _make_watch_session(stall_timeout=1.0)
    capturer = _StubCapturer([5.0, 5.0, 5.0, 6.0, 7.0])
    session._capturer = capturer

    session._watch_stall()

    assert not capturer.stop_called
    assert not session._stall_detected


# 13. 사용자가 이미 종료를 요청했으면 워치독은 아무것도 하지 않는다.
def test_watchdog_noop_after_user_stop() -> None:
    session, messages = _make_watch_session(stall_timeout=0.2)
    capturer = _StubCapturer([25.0], hang=True)
    session._capturer = capturer
    session._stop_requested.set()

    session._watch_stall()

    assert not capturer.stop_called
    assert messages == []


# 14. stall_timeout_sec=0이면 워치독이 비활성화된다.
def test_watchdog_disabled_when_timeout_zero() -> None:
    session, messages = _make_watch_session(stall_timeout=0.0)
    capturer = _StubCapturer([25.0], hang=True)
    session._capturer = capturer

    session._watch_stall()

    assert not capturer.stop_called
    assert messages == []


# 15. 설정 기본값: stall_timeout_sec은 60초.
def test_config_stall_timeout_default() -> None:
    config = Config(notion_token="t", notion_parent_page_id="p")
    assert config.stall_timeout_sec == 60.0


# ---------------------------------------------------------------------------
# VOD 분기: 라이브가 아닌 유튜브 URL은 (스트림 URL을 ffmpeg에 직접 먹이면
# 403이므로) 먼저 통째로 다운로드한 뒤 그 로컬 파일을 캡처해야 한다. 로컬 파일
# 입력과 라이브는 기존 경로를 유지한다.
# ---------------------------------------------------------------------------


class _RunFakeCapturer:
    """_run_capture 전체 흐름 검증용: 생성 인자를 기록하고 프레임 없이 즉시
    EOF를 내 세션이 마무리 경로로 흘러가게 한다."""

    instances: List["_RunFakeCapturer"] = []

    def __init__(self, media_url, frame_interval_sec, session_dir, ffmpeg_path, start_offset_sec=0.0):
        self.media_url = media_url
        self.start_offset_sec = start_offset_sec
        type(self).instances.append(self)

    def start(self) -> None:
        pass

    def frames(self):
        return iter(())

    def stop(self) -> None:
        pass

    def wait_returncode(self, timeout: float = 3.0):
        return 0

    @property
    def elapsed_seconds(self) -> float:
        return 0.0

    @property
    def is_running(self) -> bool:
        return False

    @property
    def last_stderr(self) -> str:
        return ""


def _make_run_session(monkeypatch, tmp_path: Path, *, is_live: bool, is_local: bool, start_offset_sec: float = 0.0):
    _RunFakeCapturer.instances = []
    config = Config(
        notion_token="test-token",
        notion_parent_page_id="test-parent",
        summarizer_backend="claude_code",
    )
    messages: List[str] = []
    session = Session(
        config,
        url="https://youtube.com/watch?v=x",
        topic_keywords="",
        ui_callback=messages.append,
        start_offset_sec=start_offset_sec,
    )
    session._notion = _StubNotion()

    resolved = StreamInfo(
        media_url="https://dead-403.example/stream", title="강의", is_live=is_live, is_local=is_local
    )
    downloads: List[dict] = []
    vod_file = tmp_path / "vod.mp4"

    def fake_download_vod(url, session_dir, ffmpeg_path, max_height=1080, on_status=None):
        downloads.append({"url": url, "session_dir": session_dir, "max_height": max_height})
        vod_file.write_bytes(b"video")
        return vod_file, "강의"

    monkeypatch.setattr(session_mod, "resolve_stream", lambda url: resolved)
    monkeypatch.setattr(session_mod, "ensure_ffmpeg", lambda on_status=None: "/usr/bin/ffmpeg")
    monkeypatch.setattr(session_mod, "new_session_dir", lambda: tmp_path)
    monkeypatch.setattr(session_mod, "download_vod", fake_download_vod)
    monkeypatch.setattr(session_mod, "Capturer", _RunFakeCapturer)
    return session, messages, downloads, vod_file


# 16. VOD URL: 다운로드가 호출되고, 캡처는 (403 나는 스트림 URL이 아니라)
# 다운로드된 로컬 파일 위에서 돌며, 처리 후 파일은 정리된다.
def test_run_capture_vod_downloads_then_captures_local_file(monkeypatch, tmp_path: Path) -> None:
    session, messages, downloads, vod_file = _make_run_session(
        monkeypatch, tmp_path, is_live=False, is_local=False
    )

    session._run_capture()

    assert len(downloads) == 1
    assert downloads[0]["url"] == "https://youtube.com/watch?v=x"
    assert downloads[0]["max_height"] == session.config.vod_max_height
    assert _RunFakeCapturer.instances[0].media_url == str(vod_file)
    assert any("다운로드" in m for m in messages)
    assert not session.failed
    assert not vod_file.exists()  # 처리 후 임시 영상 삭제


# 17. 라이브 URL: 다운로드 없이 기존 경로 + 차단 가능성 경고 1줄.
def test_run_capture_live_keeps_streaming_path_with_warning(monkeypatch, tmp_path: Path) -> None:
    session, messages, downloads, _ = _make_run_session(
        monkeypatch, tmp_path, is_live=True, is_local=False
    )

    session._run_capture()

    assert downloads == []
    assert _RunFakeCapturer.instances[0].media_url == "https://dead-403.example/stream"
    assert any("라이브" in m for m in messages)


# 18. 로컬 파일 입력: 다운로드도 경고도 없이 그대로 캡처한다.
def test_run_capture_local_file_passthrough(monkeypatch, tmp_path: Path) -> None:
    session, messages, downloads, _ = _make_run_session(
        monkeypatch, tmp_path, is_live=False, is_local=True
    )

    session._run_capture()

    assert downloads == []
    assert _RunFakeCapturer.instances[0].media_url == "https://dead-403.example/stream"
    assert not any("라이브" in m for m in messages)
    assert not any("다운로드" in m for m in messages)


# ---------------------------------------------------------------------------
# 진행 기반 워커 대기: VOD는 캡처가 실시간보다 훨씬 빨리 끝나 전사 백로그가
# 통째로 큐에 쌓인다. 90분 강의의 전사는 10분(구 고정 join 타임아웃)을 훌쩍
# 넘을 수 있으므로, "진척이 있는 한 무기한 대기, 진척이 멈추면 포기"여야 한다.
# ---------------------------------------------------------------------------


# 19. 느리지만 진척 중인 워커는 총 소요가 stall 한도를 넘어도 끝까지 기다린다.
def test_wait_for_worker_outlasts_slow_but_progressing_worker() -> None:
    session, messages = _make_watch_session(stall_timeout=60.0)
    session._worker_stall_limit_sec = 0.3
    session._worker_poll_sec = 0.02

    def slow_worker() -> None:
        for _ in range(10):
            time.sleep(0.06)
            session._jobs_done += 1

    worker = threading.Thread(target=slow_worker)
    session._worker_thread = worker
    worker.start()

    session._wait_for_worker()  # 총 ~0.6초 > 한도 0.3초지만 진척이 있으므로 대기

    assert not worker.is_alive()
    assert messages == []


# 20. 진척 없이 매달린 워커는 stall 한도 후 포기하고 마무리를 계속한다.
def test_wait_for_worker_gives_up_on_stalled_worker() -> None:
    session, messages = _make_watch_session(stall_timeout=60.0)
    session._worker_stall_limit_sec = 0.15
    session._worker_poll_sec = 0.05

    release = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    session._worker_thread = worker
    worker.start()

    start = time.monotonic()
    session._wait_for_worker()
    elapsed = time.monotonic() - start
    release.set()

    assert elapsed < 2.0
    assert any("멈춰" in m or "진척" in m for m in messages)


# 21. 설정 기본값: vod_max_height는 1080 (슬라이드 가독성 기준).
def test_config_vod_max_height_default() -> None:
    config = Config(notion_token="t", notion_parent_page_id="p")
    assert config.vod_max_height == 1080


# ---------------------------------------------------------------------------
# 기록 시작 시간: 강의가 영상 중반(예: 1:40:00)에야 시작하는 녹화본은 지정
# 시간부터만 분석한다. 오프셋은 Capturer의 ffmpeg 입력 시킹으로 전달되고,
# 사용자에게 보이는 모든 시간 라벨(섹션 구간·전사 타임스탬프)은 오프셋을 더한
# "실제 영상 시각"으로 표시해 유튜브 탐색바와 일치시킨다. 라이브에서는 시킹이
# 불가능하므로 무시하고 그 사실을 고지한다.
# ---------------------------------------------------------------------------


# 22. parse_start_time: H:MM:SS / MM:SS / 초 단위 정수를 모두 받는다.
def test_parse_start_time_accepts_common_formats() -> None:
    assert session_mod.parse_start_time("1:40:00") == 6000.0
    assert session_mod.parse_start_time("45:30") == 2730.0
    assert session_mod.parse_start_time("90") == 90.0
    assert session_mod.parse_start_time(" 0:05 ") == 5.0


# 23. parse_start_time: 빈 값·비숫자·자리수 초과·60 넘는 분/초는 한국어 메시지의
# ValueError — app.py가 그대로 경고창에 띄운다.
def test_parse_start_time_rejects_invalid_input() -> None:
    import pytest

    for bad in ("", "abc", "1:2:3:4", "-5", "1:75:00", "10:99"):
        with pytest.raises(ValueError):
            session_mod.parse_start_time(bad)


# 24. VOD + 오프셋: Capturer에 오프셋이 전달되고, 시작 지점을 사용자에게
# 고지한다 (어떤 문구도 "완료"로 시작하지 않는다).
def test_run_capture_vod_passes_start_offset_to_capturer(monkeypatch, tmp_path: Path) -> None:
    session, messages, downloads, _ = _make_run_session(
        monkeypatch, tmp_path, is_live=False, is_local=False, start_offset_sec=6000.0
    )

    session._run_capture()

    assert len(downloads) == 1
    assert _RunFakeCapturer.instances[0].start_offset_sec == 6000.0
    assert any("01:40:00" in m for m in messages)
    assert not any(m.startswith("완료") and "01:40:00" in m for m in messages)


# 25. 라이브 + 오프셋: 시킹이 불가능하므로 오프셋 없이 캡처하고 무시 사실을
# 고지한다.
def test_run_capture_live_ignores_start_offset_with_notice(monkeypatch, tmp_path: Path) -> None:
    session, messages, downloads, _ = _make_run_session(
        monkeypatch, tmp_path, is_live=True, is_local=False, start_offset_sec=6000.0
    )

    session._run_capture()

    assert downloads == []
    assert _RunFakeCapturer.instances[0].start_offset_sec == 0.0
    assert any("무시" in m for m in messages)


# 26. 로컬 파일 입력도 녹화본이므로 오프셋이 적용된다.
def test_run_capture_local_file_applies_start_offset(monkeypatch, tmp_path: Path) -> None:
    session, _, _, _ = _make_run_session(
        monkeypatch, tmp_path, is_live=False, is_local=True, start_offset_sec=300.0
    )

    session._run_capture()

    assert _RunFakeCapturer.instances[0].start_offset_sec == 300.0


# 27. 섹션 시간 라벨: 오프셋이 적용된 세션에서는 상대 타임라인(0부터)이 아니라
# 실제 영상 시각으로 발행된다.
def test_section_time_label_reflects_start_offset() -> None:
    session = _make_session(["정상 요약 불릿"])
    session._display_offset = 6000.0  # _run_capture가 오프셋 적용 시 설정하는 값
    _add_segment(session, start=0.0, end=20.0)

    session._try_publish(force=True)

    assert session._notion.time_labels == ["01:40:00 – 01:40:20"]


# 28. 전사 타임스탬프도 실제 영상 시각으로 보존된다.
def test_transcript_timestamp_reflects_start_offset() -> None:
    session = _make_session(["정상 요약 불릿"])
    session._display_offset = 6000.0

    class _StubTranscriber:
        def transcribe(self, audio, topic_keywords="", on_status=None):
            return LONG_TRANSCRIPT

    session._transcriber = _StubTranscriber()
    session._process_job(session_mod._SlideJob(start_ts=0.0, end_ts=20.0, image=b"jpeg"))

    assert session._transcripts[0][1] == "01:40:00"
