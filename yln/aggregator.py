"""Segment aggregation: carry-over merging of low-content segments into publishable sections."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional

import numpy as np

from yln.detector import change_ratio, downscale_gray


@dataclass
class Segment:
    start_ts: float
    end_ts: float
    image: bytes          # JPEG
    transcript: str       # 정제된 STT 텍스트 (빈 문자열 가능)


@dataclass
class Section:
    start_ts: float       # 첫 세그먼트 start
    end_ts: float         # 마지막 세그먼트 end
    images: List[bytes]   # dedupe + 상한 적용 후
    transcript: str       # [HH:MM:SS] 마커로 이어붙인 병합 전사
    has_content: bool     # content_chars >= min_transcript_chars 였는지


class Decision(Enum):
    KEEP = auto()      # carry-over 지속
    PUBLISH = auto()   # 요약 시도 조건 충족
    FORCE = auto()     # 상한 도달 — 강제 처리


def _format_hhmmss(seconds: float) -> str:
    total = int(max(0.0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _dedupe_images(images: List[bytes], threshold: float) -> List[bytes]:
    """Collapse runs of visually-similar adjacent frames (a static slide
    re-captured across several segments, or a code-scroll settling on its
    final state) down to one frame per run, keeping the *later* frame of
    every similar pair — it carries the most information (scroll end state).
    Frames that fail to decode are kept as-is and never used as a comparison
    anchor for the next frame.
    """
    kept: List[bytes] = []
    kept_px: List[Optional[np.ndarray]] = []  # parallel to `kept`

    for img in images:
        try:
            px = downscale_gray(img)
        except Exception:
            kept.append(img)
            kept_px.append(None)
            continue

        if kept_px and kept_px[-1] is not None and change_ratio(px, kept_px[-1]) < threshold:
            # Duplicate of the last kept frame: drop it, keep this later one.
            kept[-1] = img
            kept_px[-1] = px
        else:
            kept.append(img)
            kept_px.append(px)

    return kept


def _sample_evenly(images: List[bytes], max_count: int) -> List[bytes]:
    """Downsample to at most `max_count` images, always keeping the first and
    last and spacing the rest as evenly as possible.
    """
    n = len(images)
    if n <= max_count:
        return images
    if max_count <= 1:
        return images[:1]

    step = (n - 1) / (max_count - 1)
    indices = sorted({round(i * step) for i in range(max_count)})

    # Rounding collisions can leave us short of max_count; top up from the
    # nearest unused indices so the cap is actually reached.
    if len(indices) < max_count:
        used = set(indices)
        for idx in range(n):
            if len(indices) >= max_count:
                break
            if idx not in used:
                indices.append(idx)
                used.add(idx)
        indices = sorted(indices)

    return [images[i] for i in indices]


class SegmentAggregator:
    """Carries low-content segments forward (KEEP) and merges them into one
    publishable Section once enough content (or enough elapsed slide time, or
    a hard cap) has accumulated. Used from a single worker thread only — no
    locking.
    """

    def __init__(
        self,
        *,
        min_slide_duration_sec: float,
        min_transcript_chars: int,
        substantial_chars: int,
        max_merged_segments: int,
        max_merged_duration_sec: float,
        max_images_per_section: int,
        image_similarity_threshold: float = 0.15,
    ):
        self.min_slide_duration_sec = min_slide_duration_sec
        self.min_transcript_chars = min_transcript_chars
        self.substantial_chars = substantial_chars
        self.max_merged_segments = max_merged_segments
        self.max_merged_duration_sec = max_merged_duration_sec
        self.max_images_per_section = max_images_per_section
        self.image_similarity_threshold = image_similarity_threshold

        self.pending: List[Segment] = []
        # PUBLISH 판정 후 LLM이 무효 응답을 반환한 횟수. 상위(session)가 그룹당
        # 재요약 상한(2회)을 거는 데 사용하며, pop_section에서 0으로 리셋된다.
        self.attempts: int = 0

    def add(self, seg: Segment) -> Decision:
        self.pending.append(seg)
        chars = self.content_chars()
        span = self.pending[-1].end_ts - self.pending[0].start_ts

        if len(self.pending) >= self.max_merged_segments or span >= self.max_merged_duration_sec:
            return Decision.FORCE

        if chars >= self.min_transcript_chars and (
            span >= self.min_slide_duration_sec or chars >= self.substantial_chars
        ):
            return Decision.PUBLISH

        return Decision.KEEP

    def content_chars(self) -> int:
        return sum(len(seg.transcript.strip()) for seg in self.pending)

    def merged_transcript(self) -> str:
        parts = [
            f"[{_format_hhmmss(seg.start_ts)}] {seg.transcript.strip()}"
            for seg in self.pending
            if seg.transcript.strip()
        ]
        return " ".join(parts)

    def pop_section(self) -> Section:
        has_content = self.content_chars() >= self.min_transcript_chars
        transcript = self.merged_transcript()

        images = _dedupe_images([seg.image for seg in self.pending], self.image_similarity_threshold)
        images = _sample_evenly(images, self.max_images_per_section)

        section = Section(
            start_ts=self.pending[0].start_ts,
            end_ts=self.pending[-1].end_ts,
            images=images,
            transcript=transcript,
            has_content=has_content,
        )

        self.pending = []
        self.attempts = 0
        return section

    def has_pending(self) -> bool:
        return bool(self.pending)

    def pending_count(self) -> int:
        return len(self.pending)

    def span(self) -> float:
        if not self.pending:
            return 0.0
        return self.pending[-1].end_ts - self.pending[0].start_ts

    def mark_failed_attempt(self) -> int:
        self.attempts += 1
        return self.attempts
