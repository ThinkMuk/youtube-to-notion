from io import BytesIO

from PIL import Image

from yln.aggregator import Decision, Segment, SegmentAggregator
from yln.transcriber import _clean_transcript

_PARAMS = dict(
    min_slide_duration_sec=15,
    min_transcript_chars=30,
    substantial_chars=120,
    max_merged_segments=6,
    max_merged_duration_sec=300,
    max_images_per_section=4,
)


def _make_aggregator() -> SegmentAggregator:
    return SegmentAggregator(**_PARAMS)


def _seg(start: float, end: float, transcript: str = "", image: bytes = b"fake-jpeg") -> Segment:
    return Segment(start_ts=start, end_ts=end, image=image, transcript=transcript)


def _solid_jpeg(color) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 36), color=color).save(buf, format="JPEG")
    return buf.getvalue()


# 1. 정상 30초 슬라이드: 30자+ 전사, 30초 span -> 첫 add에서 PUBLISH
def test_normal_slide_publishes_on_first_add():
    agg = _make_aggregator()
    decision = agg.add(_seg(0.0, 30.0, transcript="가" * 35))
    assert decision is Decision.PUBLISH


# 2. 4초x3 연쇄 (빈 전사): 전부 KEEP, content_chars 0 유지
def test_short_empty_segments_all_keep():
    agg = _make_aggregator()
    for i in range(3):
        decision = agg.add(_seg(i * 4.0, (i + 1) * 4.0, transcript=""))
        assert decision is Decision.KEEP
    assert agg.content_chars() == 0


# 3. 4초 빈 전사 x2 후 30초 알찬 세그먼트: KEEP, KEEP, PUBLISH
def test_carry_over_then_content_segment_publishes_full_span():
    agg = _make_aggregator()
    assert agg.add(_seg(0.0, 4.0, transcript="", image=b"img0")) is Decision.KEEP
    assert agg.add(_seg(4.0, 8.0, transcript="", image=b"img1")) is Decision.KEEP
    assert agg.add(_seg(8.0, 38.0, transcript="가" * 35, image=b"img2")) is Decision.PUBLISH

    section = agg.pop_section()
    assert section.start_ts == 0.0
    assert section.end_ts == 38.0
    assert len(section.images) == 3  # 디코딩 불가 바이트 -> dedupe 없이 그대로 유지
    assert not agg.has_pending()


# 4. 짧지만 알찬 12초(150자 전사): substantial_chars 경로로 첫 add에서 PUBLISH
def test_short_but_substantial_segment_publishes_via_substantial_chars():
    agg = _make_aggregator()
    decision = agg.add(_seg(0.0, 12.0, transcript="가" * 150))
    assert decision is Decision.PUBLISH


# 5. 6개 누적 -> FORCE (개수 상한)
def test_segment_count_cap_forces():
    agg = _make_aggregator()
    decisions = [agg.add(_seg(float(i), float(i) + 1.0, transcript="")) for i in range(6)]
    assert decisions[-1] is Decision.FORCE
    assert agg.pending_count() == 6


# 6. 638초 무발화 단일 세그먼트 -> FORCE (span 상한), has_content=False
def test_long_silent_segment_forces_by_duration():
    agg = _make_aggregator()
    decision = agg.add(_seg(0.0, 638.0, transcript=""))
    assert decision is Decision.FORCE

    section = agg.pop_section()
    assert section.has_content is False


# 7. mark_failed_attempt: 2회 증가 확인, pop_section 후 0 리셋
def test_mark_failed_attempt_increments_and_resets_on_pop():
    agg = _make_aggregator()
    assert agg.mark_failed_attempt() == 1
    assert agg.mark_failed_attempt() == 2
    assert agg.attempts == 2

    agg.add(_seg(0.0, 1.0, transcript=""))
    agg.pop_section()
    assert agg.attempts == 0


# 8. merged_transcript 마커: 빈 전사는 건너뛰고 [HH:MM:SS] 마커 포함
def test_merged_transcript_markers_skip_empty_segments():
    agg = _make_aggregator()
    agg.add(_seg(0.0, 4.0, transcript=""))
    agg.add(_seg(10.0, 14.0, transcript="안녕하세요"))

    merged = agg.merged_transcript()
    assert "[00:00:10] 안녕하세요" in merged
    assert merged.count("[") == 1


# 9. 이미지 dedupe: 동일쌍은 1장으로 줄고 뒤 프레임이 유지되는지 확인
def test_image_dedupe_keeps_later_frame_of_similar_pair():
    agg = _make_aggregator()
    img_a = _solid_jpeg((10, 10, 10))
    img_a_later = _solid_jpeg((12, 12, 12))  # 거의 동일한 색상 -> change_ratio < 0.15
    img_b = _solid_jpeg((240, 240, 240))     # 확연히 다른 색상

    agg.add(_seg(0.0, 2.0, transcript="", image=img_a))
    agg.add(_seg(2.0, 4.0, transcript="", image=img_a_later))
    agg.add(_seg(4.0, 6.0, transcript="", image=img_b))

    section = agg.pop_section()
    assert len(section.images) == 2
    assert section.images[0] == img_a_later  # 앞 프레임(img_a)은 버려짐
    assert section.images[1] == img_b
    assert len(section.images) <= _PARAMS["max_images_per_section"]


# 9-보조. dedupe 후에도 max_images_per_section 초과 시 첫/마지막 포함 + 균등 샘플
def test_image_dedupe_respects_max_images_cap():
    agg = _make_aggregator()
    # 디코딩 불가 바이트를 사용해 dedupe를 우회하고 6장을 그대로 pending에 쌓는다.
    for i in range(6):
        agg.add(_seg(float(i), float(i) + 1.0, transcript="", image=f"img{i}".encode()))

    section = agg.pop_section()
    assert len(section.images) == _PARAMS["max_images_per_section"]
    assert section.images[0] == b"img0"
    assert section.images[-1] == b"img5"


# 10. _clean_transcript
def test_clean_transcript_removes_hallucination_denylist_phrase():
    # 정규식(`구독\s*(과|하)?\s*좋아요`)이 실제로 매칭하는 환각 문구.
    assert _clean_transcript("구독과 좋아요") == ""


def test_clean_transcript_collapses_repeated_sentences():
    text = "판다스는 데이터프레임을 다룹니다. 판다스는 데이터프레임을 다룹니다. 판다스는 데이터프레임을 다룹니다."
    assert _clean_transcript(text) == "판다스는 데이터프레임을 다룹니다."


def test_clean_transcript_leaves_normal_sentence_untouched():
    text = "오늘은 파이썬의 리스트 컴프리헨션에 대해 알아보겠습니다."
    assert _clean_transcript(text) == text
