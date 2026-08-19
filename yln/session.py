"""Session controller: wires capture -> detection -> transcription -> aggregation
-> summarization -> Notion.

Runs two background threads (capture+detect, processing worker) plus a stop
sequence that finalizes the lecture. `ui_callback` is invoked from whichever
background thread has news to report; the GUI layer is responsible for
marshalling it onto the main thread (e.g. via `root.after`).

Segments detected by the SlideDetector are not published 1:1 anymore: the
worker feeds them into a SegmentAggregator that merges low-content segments
(fast slide flips, code scrolling, silence) into the next content-bearing
section, so failure/apology text never reaches Notion.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from yln.aggregator import Decision, Segment, SegmentAggregator
from yln.capture import Capturer, CaptureError, download_vod, ensure_ffmpeg, new_session_dir, resolve_stream
from yln.config import Config
from yln.detector import SlideChange, SlideDetector
from yln.notion_writer import NotionError, NotionWriter
from yln.summarizer import SummarizerError, build_summarizer
from yln.transcriber import Transcriber

_SENTINEL = object()
_FLUSH = object()  # worker marker: force-publish whatever is pending (session end)

# A pending group is re-summarized at most this many times after invalid LLM
# responses; afterwards it only publishes on a FORCE condition.
_MAX_SUMMARY_ATTEMPTS = 2


@dataclass
class _SlideJob:
    start_ts: float
    end_ts: float
    image: bytes


def _format_hhmmss(seconds: float) -> str:
    total = int(max(0.0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_start_time(text: str) -> float:
    """Parse a user-entered start position ("1:40:00", "45:30", "90") to seconds.

    Raises ValueError with a Korean message suitable for showing directly in a
    GUI warning dialog."""
    parts = [p.strip() for p in text.strip().split(":")]
    if not text.strip() or len(parts) > 3 or not all(p.isdigit() for p in parts):
        raise ValueError("시작 시간 형식이 올바르지 않습니다. 예: 1:40:00, 45:30, 90 (초)")
    nums = [int(p) for p in parts]
    if (len(nums) >= 2 and nums[-1] >= 60) or (len(nums) == 3 and nums[1] >= 60):
        raise ValueError("시작 시간의 분·초는 0~59 범위여야 합니다. 예: 1:40:00")
    total = 0
    for n in nums:
        total = total * 60 + n
    return float(total)


class Session:
    def __init__(
        self,
        config: Config,
        url: str,
        topic_keywords: str,
        ui_callback: Callable[[str], None],
        start_offset_sec: float = 0.0,
    ):
        self.config = config
        self.url = url
        self.topic_keywords = topic_keywords
        self.ui_callback = ui_callback
        self.start_offset_sec = max(0.0, start_offset_sec)
        # Added to every user-facing timestamp (section labels, transcript
        # entries) once the offset is actually applied, so labels match the
        # video's own seek bar instead of restarting at 00:00:00. Set by
        # _run_capture — live streams cannot seek, so it stays 0 there.
        self._display_offset = 0.0

        self._capturer: Optional[Capturer] = None
        self._detector = SlideDetector(change_ratio=config.change_ratio, debounce=config.debounce)
        self._notion = NotionWriter(config.notion_token)
        self._summarizer = build_summarizer(config)
        self._transcriber = Transcriber(config.whisper_model, config.whisper_device)
        self._agg = SegmentAggregator(
            min_slide_duration_sec=config.min_slide_duration_sec,
            min_transcript_chars=config.min_transcript_chars,
            substantial_chars=config.substantial_chars,
            max_merged_segments=config.max_merged_segments,
            max_merged_duration_sec=config.max_merged_duration_sec,
            max_images_per_section=config.max_images_per_section,
            # The dedupe threshold must stay below the detection threshold:
            # otherwise frames the detector considers distinct slides would be
            # collapsed into one image inside a merged section.
            image_similarity_threshold=min(0.15, config.change_ratio * 0.8),
        )

        self._page_id: Optional[str] = None
        self._title = url
        self._job_queue: "queue.Queue" = queue.Queue()
        self._slide_summaries: List[List[str]] = []
        # [section_no or None, hhmmss, raw transcript] — appended as soon as a
        # segment is transcribed (before any summarization/publishing), so the
        # full-transcript sub-page never loses text however publishing goes.
        self._transcripts: List[List] = []
        self._slide_count = 0
        self._last_segment_wallclock = time.monotonic()

        self._capture_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stall_detected = False
        self._vod_path: Optional[Path] = None
        # Worker progress accounting for _wait_for_worker: bumped on every job
        # dequeue and completion. A VOD session enqueues the whole lecture's
        # backlog within minutes, so finalize must wait on progress, not on a
        # fixed timeout.
        self._jobs_done = 0
        self._worker_stall_limit_sec = 600.0
        self._worker_poll_sec = 5.0
        self._stop_requested = threading.Event()
        self._session_failed = threading.Event()
        self._finalize_lock = threading.Lock()
        self._finalized = False

    @property
    def slide_count(self) -> int:
        return self._slide_count

    @property
    def failed(self) -> bool:
        return self._session_failed.is_set()

    def _status(self, message: str) -> None:
        self.ui_callback(message)

    def _fmt_media_ts(self, ts: float) -> str:
        """Format a capture-timeline timestamp as the video's own clock time
        (capture starts at _display_offset when a start time was applied)."""
        return _format_hhmmss(ts + self._display_offset)

    def _error(self, message: str) -> None:
        self.ui_callback(f"[오류] {message}")

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._worker_thread = threading.Thread(target=self._run_worker, daemon=True)
        self._capture_thread = threading.Thread(target=self._run_capture, daemon=True)
        self._worker_thread.start()
        self._capture_thread.start()

    def stop(self) -> None:
        threading.Thread(target=self._run_stop, daemon=True).start()

    # -- capture + detect thread -----------------------------------------

    def _run_capture(self) -> None:
        try:
            self._status("스트림 정보를 가져오는 중...")
            stream_info = resolve_stream(self.url)
            self._title = stream_info.title or self.url

            self._status("Notion 페이지 생성 중...")
            self._page_id = self._notion.create_lecture_page(self.config.notion_parent_page_id, self._title)

            # Resolved here (capture thread) rather than inside Capturer so that
            # first-run auto-download progress reaches the GUI status area.
            ffmpeg_path = ensure_ffmpeg(on_status=self._status)

            session_dir = new_session_dir()

            media_source = stream_info.media_url
            if stream_info.is_live:
                if self.start_offset_sec > 0:
                    self._status("라이브 스트림은 시작 시간을 지정할 수 없어 무시합니다")
                self._status(
                    "주의: 라이브 스트림은 유튜브 서버 차단으로 시작 후 약 30초 만에 "
                    "수신이 끊길 수 있습니다 (업로드된 영상 URL 사용을 권장)"
                )
            elif not stream_info.is_local:
                # Uploaded video: its direct stream URLs 403 under ffmpeg, so
                # download the whole file first and capture from local disk.
                self._status("업로드된 영상 다운로드를 시작합니다...")
                self._vod_path, _ = download_vod(
                    self.url,
                    session_dir,
                    ffmpeg_path,
                    max_height=self.config.vod_max_height,
                    on_status=self._status,
                )
                media_source = str(self._vod_path)
                self._status("다운로드가 끝나 영상 분석을 시작합니다...")

            start_offset = 0.0
            if not stream_info.is_live and self.start_offset_sec > 0:
                start_offset = self.start_offset_sec
                self._display_offset = start_offset
                self._status(
                    f"지정한 시작 시간 {_format_hhmmss(start_offset)}부터 분석합니다 "
                    "(이전 구간은 건너뜁니다)"
                )

            self._capturer = Capturer(
                media_source,
                self.config.frame_interval_sec,
                session_dir,
                ffmpeg_path,
                start_offset_sec=start_offset,
            )
            self._capturer.start()

            self._status("캡처 시작됨 (슬라이드 감지 대기 중)")

            self._watchdog_thread = threading.Thread(target=self._watch_stall, daemon=True)
            self._watchdog_thread.start()

            for jpeg_bytes, ts in self._capturer.frames():
                if self._stop_requested.is_set():
                    break
                change = self._detector.feed(jpeg_bytes, ts)
                if change is not None:
                    self._enqueue_change(change)

            if not self._stop_requested.is_set():
                if self._stall_detected:
                    self._status("수신이 끊긴 지점까지의 내용으로 마무리합니다...")
                else:
                    returncode = self._capturer.wait_returncode()
                    if returncode not in (None, 0):
                        self._surface_ffmpeg_stderr(f"ffmpeg가 비정상 종료했습니다 (코드 {returncode}):")
                    self._status("스트림이 종료되어 자동으로 마무리합니다...")
            self._finalize()

        except CaptureError as e:
            self._session_failed.set()
            self._error(str(e))
            self._job_queue.put(_SENTINEL)
        except NotionError as e:
            self._session_failed.set()
            self._error(f"Notion 페이지 생성 실패: {e}")
            self._job_queue.put(_SENTINEL)
        except Exception as e:  # noqa: BLE001 - top-level thread guard
            self._session_failed.set()
            self._error(f"캡처 중 예상치 못한 오류가 발생했습니다: {e}")
            self._job_queue.put(_SENTINEL)

    def _enqueue_change(self, change: SlideChange) -> None:
        self._job_queue.put(_SlideJob(start_ts=change.start_ts, end_ts=change.prev_end_ts, image=change.image))

    def _watch_stall(self) -> None:
        """Stop the capturer when the audio timeline freezes while ffmpeg lives on.

        YouTube live streams start refusing HLS media segments (HTTP 403) shortly
        after extraction; ffmpeg then retries forever without an EOF, so frames()
        blocks and the session silently captures nothing past the initial window.
        Stopping the capturer here gives frames() its EOF and lets the normal
        finalize path preserve everything captured so far.
        """
        capturer = self._capturer
        timeout = self.config.stall_timeout_sec
        if capturer is None or timeout <= 0:
            return
        poll = min(2.0, max(0.05, timeout / 4))
        last_elapsed = capturer.elapsed_seconds
        frozen_since = time.monotonic()
        while not self._stop_requested.is_set() and capturer.is_running:
            time.sleep(poll)
            elapsed = capturer.elapsed_seconds
            if elapsed != last_elapsed:
                last_elapsed = elapsed
                frozen_since = time.monotonic()
                continue
            if time.monotonic() - frozen_since >= timeout:
                self._stall_detected = True
                self._status(
                    f"경고: 스트림 수신이 {int(timeout)}초간 멈춰 캡처를 종료합니다 "
                    "(유튜브가 라이브 세그먼트 전송을 차단했을 수 있습니다)"
                )
                self._surface_ffmpeg_stderr("ffmpeg:")
                capturer.stop()
                return

    def _surface_ffmpeg_stderr(self, prefix: str) -> None:
        if self._capturer is None:
            return
        tail = [line for line in self._capturer.last_stderr.splitlines() if line.strip()][-3:]
        if tail:
            self._error(prefix + " " + " / ".join(tail))

    # -- processing worker thread -----------------------------------------

    def _run_worker(self) -> None:
        while True:
            try:
                job = self._job_queue.get(timeout=30)
            except queue.Empty:
                # No new segment for a while: publish a lingering pending group
                # so a live viewer is not stuck on "병합 대기 중" for minutes.
                try:
                    self._maybe_idle_flush()
                except Exception as e:  # noqa: BLE001 - a failed flush must not kill the worker
                    self._error(f"유휴 병합 처리 중 오류: {e}")
                continue
            if job is _SENTINEL:
                break
            self._jobs_done += 1
            try:
                if job is _FLUSH:
                    if self._agg.has_pending():
                        self._try_publish(force=True)
                else:
                    self._process_job(job)
            except Exception as e:  # noqa: BLE001 - a failed job must not kill the session
                self._error(f"슬라이드 처리 중 오류: {e}")
            finally:
                self._jobs_done += 1
                self._job_queue.task_done()

    def _maybe_idle_flush(self) -> None:
        if not self._agg.has_pending() or self.config.idle_flush_sec <= 0:
            return
        if time.monotonic() - self._last_segment_wallclock >= self.config.idle_flush_sec:
            self._try_publish(force=True)

    def _process_job(self, job: _SlideJob) -> None:
        audio = b""
        if self._capturer is not None:
            audio = self._capturer.read_audio_span(
                job.start_ts, job.end_ts, wait_timeout=self.config.audio_flush_timeout_sec
            )
        if not audio and job.end_ts - job.start_ts >= 1.0:
            self._status(f"경고: {self._fmt_media_ts(job.start_ts)} 구간의 오디오가 비어 있습니다")

        transcript = self._transcriber.transcribe(audio, self.topic_keywords, on_status=self._status)

        # Preserve the raw transcript immediately — independent of whether (and
        # under which section number) this segment ever gets published.
        self._transcripts.append([None, self._fmt_media_ts(job.start_ts), transcript])

        decision = self._agg.add(
            Segment(start_ts=job.start_ts, end_ts=job.end_ts, image=job.image, transcript=transcript)
        )
        self._last_segment_wallclock = time.monotonic()

        if decision is Decision.KEEP:
            self._status(
                f"세그먼트 병합 대기 중 ({self._agg.pending_count()}개, "
                f"전사 {self._agg.content_chars()}/{self.config.min_transcript_chars}자, "
                f"구간 {self._agg.span():.0f}/{self.config.min_slide_duration_sec:.0f}초)"
            )
            return
        self._try_publish(force=(decision is Decision.FORCE))

    def _try_publish(self, force: bool) -> None:
        """Summarize the pending group and publish it as one Notion section.

        Never raises: without `force`, both an invalid summary and a NO_CONTENT
        verdict keep the group pending (carry-over) — NO_CONTENT additionally
        skips the retry-budget accounting. Under `force`, a NO_CONTENT group
        flushes as a silent one (screenshots only, never a placeholder
        section), and Notion errors are logged — the transcripts are already
        preserved either way.
        """
        has_content = self._agg.content_chars() >= self.config.min_transcript_chars

        bullets: Optional[List[str]] = None
        model_no_content = False
        if has_content and self._agg.attempts < _MAX_SUMMARY_ATTEMPTS:
            result: Optional[List[str]] = None
            call_failed = False
            try:
                result = self._summarizer.summarize_slide(self._agg.merged_transcript(), self.topic_keywords)
            except SummarizerError as e:
                call_failed = True
                self._error(f"요약 호출 실패: {e}")
            except Exception as e:  # noqa: BLE001 - a summarizer bug must not lock publishing for good
                call_failed = True
                self._error(f"요약 호출 실패 ({type(e).__name__}): {e}")
            if result is None:
                # The retry budget meters model-answer quality only. A backend
                # failure (outage, expired login) must not burn it: the lockout
                # would keep skipping the LLM call even after the backend
                # recovers, forcing a placeholder for a summarizable group.
                if not call_failed:
                    self._agg.mark_failed_attempt()
            elif not result:
                # NO_CONTENT: the model judged the group has nothing to
                # summarize yet (chars passed the length gate, but it's
                # hallucination/small-talk). Not a failure — no attempt is
                # burned, so the group keeps its full retry budget as it grows.
                model_no_content = True
            else:
                bullets = result

        if bullets is None and not force:
            # No summary yet for a publishable group: keep carrying it over so
            # its slides end up attached to the next content-bearing section.
            if model_no_content:
                self._status(f"요약할 내용이 아직 없어 다음 화면과 병합합니다 ({self._agg.pending_count()}개 대기)")
            elif self._agg.attempts >= _MAX_SUMMARY_ATTEMPTS:
                self._status(f"요약 재시도 상한 도달 — 병합 상한에서 강제 발행합니다 ({self._agg.pending_count()}개 대기)")
            else:
                self._status(f"요약 결과가 비어 있어 다음 화면과 병합합니다 ({self._agg.pending_count()}개 대기)")
            return

        section = self._agg.pop_section()
        placeholder = False
        if bullets is None:
            if model_no_content or not section.has_content:
                # Silent group (screen flips with no speech): no section at all —
                # attach the screenshots after the previously published section.
                try:
                    upload_ids = [
                        self._notion.upload_image(img, f"slide_{self._slide_count}_extra_{i}.jpg")
                        for i, img in enumerate(section.images)
                    ]
                    self._notion.append_images(self._page_id, upload_ids)
                    self._status("무발화 구간 스크린샷을 직전 섹션에 추가했습니다")
                except Exception as e:  # noqa: BLE001
                    self._error(f"스크린샷 추가 실패: {e}")
                self._stamp_transcripts(max(1, self._slide_count))
                return
            bullets = ["(자동 요약 실패 — 전체 스크립트 서브페이지 참고)"]
            placeholder = True

        slide_no = self._slide_count + 1
        time_label = f"{self._fmt_media_ts(section.start_ts)} – {self._fmt_media_ts(section.end_ts)}"
        try:
            upload_ids = [
                self._notion.upload_image(img, f"slide_{slide_no}_{i}.jpg")
                for i, img in enumerate(section.images)
            ]
            self._notion.append_section(self._page_id, slide_no, time_label, upload_ids, bullets)
        except Exception as e:  # noqa: BLE001
            self._error(f"Slide {slide_no} 업로드 실패: {e}")
            self._stamp_transcripts(max(1, self._slide_count))
            return

        self._stamp_transcripts(slide_no)
        if not placeholder:
            # Placeholder bullets would only pollute the final lecture summary.
            self._slide_summaries.append(bullets)
        self._slide_count += 1
        self._status(f"Slide {slide_no} 업로드 완료 (총 {self._slide_count}장)")

    def _stamp_transcripts(self, section_no: int) -> None:
        """Assign a section number to transcript entries of the group just handled."""
        for entry in self._transcripts:
            if entry[0] is None:
                entry[0] = section_no

    # -- stop / finalize ---------------------------------------------------

    def _wait_for_worker(self) -> None:
        """Wait for the worker to drain the job queue, however long that takes,
        as long as it keeps making progress.

        A VOD session enqueues the entire lecture's transcription backlog
        within minutes of capture, so a fixed join timeout would abandon a
        long lecture mid-transcription; only give up once no job has been
        dequeued or completed for _worker_stall_limit_sec.
        """
        worker = self._worker_thread
        if worker is None:
            return
        last_done = self._jobs_done
        stalled_since = time.monotonic()
        while worker.is_alive():
            worker.join(timeout=self._worker_poll_sec)
            if not worker.is_alive():
                return
            done = self._jobs_done
            if done != last_done:
                last_done = done
                stalled_since = time.monotonic()
                continue
            if time.monotonic() - stalled_since >= self._worker_stall_limit_sec:
                self._error(
                    f"처리 작업이 {int(self._worker_stall_limit_sec)}초간 진척 없이 멈춰 "
                    "지금까지의 내용으로 마무리를 계속합니다"
                )
                return

    def _run_stop(self) -> None:
        # Stop ffmpeg so the capture thread's frames() loop hits EOF and runs
        # _finalize itself — never flush the detector while the capture thread
        # may still be feeding it. The direct _finalize call below only acts
        # (guarded by _finalize_lock/_finalized) if the capture thread died
        # early without finalizing, e.g. a resolve/page-creation failure.
        self._stop_requested.set()
        if self._capturer is not None:
            self._capturer.stop()
        if self._capture_thread is not None and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=30)
        self._finalize()

    def _finalize(self) -> None:
        with self._finalize_lock:
            if self._finalized:
                return
            self._finalized = True

        if self._session_failed.is_set():
            return

        try:
            # Stop ffmpeg first so the audio tail is flushed to disk before the
            # session length is measured (the audio file is the reference
            # timeline). The last frame timestamp bounds it from below in case
            # the audio file lags the video pipe.
            end_ts = 0.0
            if self._capturer is not None:
                self._capturer.stop()
                end_ts = self._capturer.elapsed_seconds
            if self._detector.last_ts is not None:
                end_ts = max(end_ts, self._detector.last_ts + self.config.frame_interval_sec)

            change = self._detector.flush(end_ts)
            if change is not None:
                self._enqueue_change(change)

            self._job_queue.put(_FLUSH)
            self._job_queue.put(_SENTINEL)

            self._wait_for_worker()

            if self._vod_path is not None:
                # The downloaded video is only read by ffmpeg, which has exited
                # by now; the audio/transcripts live elsewhere in session_dir.
                try:
                    self._vod_path.unlink(missing_ok=True)
                except OSError:
                    pass

            if self._session_failed.is_set():
                return

            if self._slide_summaries:
                self._status("전체 강의 요약 생성 중...")
                lecture_summary = self._summarizer.summarize_lecture(self._slide_summaries, self._title)
                self._status("Notion에 전체 요약 기록 중...")
                self._notion.append_final_summary(self._page_id, lecture_summary)
            else:
                self._status("요약된 슬라이드가 없어 전체 요약을 건너뜁니다.")

            # A failure here must not mask the already-successful summary write above,
            # nor prevent the session from reaching its final "완료" status.
            try:
                self._status("전체 스크립트 서브페이지 기록 중...")
                entries = [(no if no is not None else 0, hhmmss, tr) for no, hhmmss, tr in self._transcripts]
                self._notion.create_transcript_subpage(self._page_id, self._title, entries)
            except Exception as e:  # noqa: BLE001 - isolated, non-fatal
                self._error(f"스크립트 서브페이지 기록 실패: {e}")

            self._status("완료 — Notion 기록이 마무리되었습니다.")
        except Exception as e:  # noqa: BLE001 - top-level thread guard
            self._session_failed.set()
            self._error(f"세션 마무리 중 오류가 발생했습니다: {e}")
