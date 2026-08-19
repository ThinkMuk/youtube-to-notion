"""Slide-change detection via pixel-difference ratio ("50% of the screen changed") with debounce."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

try:
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # older Pillow
    _RESAMPLE = Image.LANCZOS

# Downscale resolution for the pixel comparison. Small enough to be cheap,
# large enough that a "half the screen changed" judgement is meaningful.
_DOWNSCALE = (64, 36)

# Per-pixel absolute grayscale difference (0..255) that counts as "this pixel
# changed". Small value tolerates JPEG/encoding noise without masking real edits.
_PIXEL_NOISE = 25


def downscale_gray(jpeg_bytes: bytes) -> np.ndarray:
    """Decode a JPEG to a small grayscale pixel array for area comparison."""
    img = Image.open(BytesIO(jpeg_bytes)).convert("L").resize(_DOWNSCALE, _RESAMPLE)
    return np.asarray(img, dtype=np.int16)


def change_ratio(a: np.ndarray, b: np.ndarray) -> float:
    """Fraction of pixels (0.0..1.0) whose absolute difference exceeds the noise floor."""
    diff = np.abs(a - b)
    return float(np.count_nonzero(diff > _PIXEL_NOISE)) / diff.size


@dataclass
class SlideChange:
    start_ts: float       # start (in capture-elapsed seconds) of the emitted (outgoing) slide
    prev_end_ts: float    # end of the emitted slide == ts of the first candidate frame of the new slide
    image: bytes          # last stable frame of the outgoing slide
    new_ts: float         # start of the new (now-current) slide == prev_end_ts


class SlideDetector:
    """Detects slide changes from a stream of (jpeg, ts) frames.

    A change is confirmed when `debounce` consecutive frames each differ from
    the current slide over at least `change_ratio` of the screen area, and those
    candidate frames are mutually stable (pairwise change ratio < change_ratio).
    This filters out transient noise (e.g. cursor movement, transition
    animations).
    """

    def __init__(self, change_ratio: float, debounce: int = 2):
        self.change_ratio = change_ratio
        self.debounce = debounce

        self.first_frame_ts: Optional[float] = None
        self.last_ts: Optional[float] = None

        self._current_pixels: Optional[np.ndarray] = None
        self._current_image: Optional[bytes] = None
        self._slide_start_ts: Optional[float] = None

        self._candidates: List[Tuple[bytes, np.ndarray, float]] = []

    def feed(self, jpeg_bytes: bytes, ts: float) -> Optional[SlideChange]:
        self.last_ts = ts
        px = downscale_gray(jpeg_bytes)

        if self._current_pixels is None:
            self.first_frame_ts = ts
            self._current_pixels = px
            self._current_image = jpeg_bytes
            self._slide_start_ts = ts
            return None

        ratio_from_current = change_ratio(px, self._current_pixels)

        if ratio_from_current < self.change_ratio:
            self._candidates = []
            self._current_pixels = px
            self._current_image = jpeg_bytes
            return None

        if self._candidates:
            _, last_candidate_px, _ = self._candidates[-1]
            if change_ratio(px, last_candidate_px) >= self.change_ratio:
                self._candidates = []

        self._candidates.append((jpeg_bytes, px, ts))

        if len(self._candidates) < self.debounce:
            return None

        first_candidate_ts = self._candidates[0][2]
        change = SlideChange(
            start_ts=self._slide_start_ts,
            prev_end_ts=first_candidate_ts,
            image=self._current_image,
            new_ts=first_candidate_ts,
        )

        last_candidate_jpeg, last_candidate_px, _ = self._candidates[-1]
        self._current_pixels = last_candidate_px
        self._current_image = last_candidate_jpeg
        self._slide_start_ts = first_candidate_ts
        self._candidates = []

        return change

    def flush(self, ts: float) -> Optional[SlideChange]:
        """Emit the current (still-on-screen) slide as a final SlideChange at session end."""
        if self._current_pixels is None or self._slide_start_ts is None or self._current_image is None:
            return None
        return SlideChange(
            start_ts=self._slide_start_ts,
            prev_end_ts=ts,
            image=self._current_image,
            new_ts=ts,
        )
