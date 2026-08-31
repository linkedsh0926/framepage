from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.pipeline import OpenRouterPolicyBlocked, Pipeline
from app.schemas import ReviewDecision


def _page(number: int) -> dict:
    return {
        "page_number": number,
        "start_ms": (number - 1) * 10_000,
        "end_ms": number * 10_000,
        "keyframe_ms": number * 10_000 - 500,
        "paragraphs": [f"page {number}"],
        "confidence": 0.9,
        "preview_key": f"page-{number}.jpg",
        "audit": {},
    }


def test_real_review_rechecks_every_page_after_actions(tmp_path: Path) -> None:
    db = MagicMock()
    db.get_job.return_value = {"cancel_requested": 0}
    pipeline = Pipeline(
        db,
        Settings(
            mode="real",
            data_dir=tmp_path,
            token_secret="unit-test-real-mode-secret-0123456789abcdef",
        ),
    )
    pages = [_page(1), _page(2), _page(3)]
    pipeline._request_review_decision = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            ReviewDecision(action="resample_local", reason="partial", confidence=0.91),
            ReviewDecision(action="search_later", reason="transition", confidence=0.9),
            ReviewDecision(action="low_confidence", reason="ambiguous", confidence=0.5),
            ReviewDecision(action="keep", reason="stable", confidence=0.95),
            ReviewDecision(action="keep", reason="stable", confidence=0.94),
            ReviewDecision(action="keep", reason="stable", confidence=0.93),
        ]
    )
    applied: list[tuple[str, int]] = []

    def record_action(action, current_pages, index, source, workspace, fps):
        applied.append((action, current_pages[index]["page_number"]))
        return action != "keep"

    pipeline._apply_review_action = record_action  # type: ignore[method-assign]
    pipeline._review_real(
        "job_test",
        pages,
        rounds=2,
        key="not-sent-by-mock",
        source=tmp_path / "source.mp4",
        workspace=tmp_path,
        resample_fps=10,
    )

    assert pipeline._request_review_decision.call_count == 6  # type: ignore[attr-defined]
    assert applied == [
        ("resample_local", 1),
        ("search_later", 2),
        ("low_confidence", 3),
        ("keep", 1),
        ("keep", 2),
        ("keep", 3),
    ]
    events = [call.args[1] for call in db.add_event.call_args_list]
    assert events.count("review_round_started") == 2
    assert "review_round_failed" in events
    assert "review_round_passed" in events
    assert events[-1] == "review_stopped"


def test_real_review_retries_only_unavailable_pages_when_selection_is_unchanged(
    tmp_path: Path,
) -> None:
    db = MagicMock()
    db.get_job.return_value = {"cancel_requested": 0}
    pipeline = Pipeline(
        db,
        Settings(
            mode="real",
            data_dir=tmp_path,
            token_secret="unit-test-real-mode-secret-0123456789abcdef",
        ),
    )
    pages = [_page(1), _page(2), _page(3)]
    attempts: dict[int, int] = {}

    def review(_pages, index, _key):
        page_number = _pages[index]["page_number"]
        attempts[page_number] = attempts.get(page_number, 0) + 1
        if page_number == 2 and attempts[page_number] == 1:
            raise ValueError("malformed provider response")
        return ReviewDecision(action="keep", reason="stable", confidence=0.95)

    pipeline._request_review_decision = review  # type: ignore[method-assign]
    pipeline._apply_review_action = MagicMock(return_value=False)  # type: ignore[method-assign]
    pipeline._review_real(
        "job_test",
        pages,
        rounds=3,
        key="not-sent-by-mock",
        source=tmp_path / "source.mp4",
        workspace=tmp_path,
        resample_fps=10,
    )

    assert attempts == {1: 1, 2: 2, 3: 1}
    starts = [
        call.args[2]["issue_count"]
        for call in db.add_event.call_args_list
        if call.args[1] == "review_round_started"
    ]
    assert starts == [3, 1]
    events = [call.args[1] for call in db.add_event.call_args_list]
    assert "review_round_failed" in events
    assert "review_round_passed" in events


def test_real_mode_never_silently_falls_back_without_media_tools(
    tmp_path: Path, monkeypatch
) -> None:
    pipeline = Pipeline(
        MagicMock(),
        Settings(
            mode="real",
            data_dir=tmp_path,
            token_secret="unit-test-real-mode-secret-0123456789abcdef",
        ),
    )
    pipeline._start_stage = MagicMock()  # type: ignore[method-assign]
    pipeline._run_demo = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("app.pipeline.shutil.which", lambda _name: None)

    with pytest.raises(RuntimeError, match="requires yt-dlp"):
        pipeline._run_real(
            "job_test",
            {"youtube_url": "https://youtu.be/dQw4w9WgXcQ"},
            {},
            tmp_path,
        )

    pipeline._run_demo.assert_not_called()  # type: ignore[attr-defined]


def test_asr_page_uses_short_overlapping_chunks(tmp_path: Path) -> None:
    pipeline = Pipeline(
        MagicMock(),
        Settings(mode="test", data_dir=tmp_path, token_secret="test-secret"),
    )
    slices: list[tuple[int, int]] = []
    completed_chunks: list[int] = []

    def record_slice(_audio, _clip, start_ms, end_ms):
        slices.append((start_ms, end_ms))

    pipeline._audio_clip = record_slice  # type: ignore[method-assign]
    pipeline._transcribe = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            ("第一段", {}),
            ("第二段", {}),
            ("第三段", {}),
        ]
    )
    transcript, audit = pipeline._transcribe_page(
        tmp_path / "audio.opus",
        tmp_path,
        {
            "page_number": 1,
            "start_ms": 0,
            "end_ms": 60_000,
        },
        "not-sent-by-mock",
        on_chunk_complete=lambda: completed_chunks.append(len(completed_chunks) + 1),
    )

    assert slices == [(0, 24_000), (23_400, 47_400), (46_800, 60_000)]
    assert all(end - start <= 24_000 for start, end in slices)
    assert audit["overlap_ms"] == 600
    assert audit["chunk_count"] == 3
    assert completed_chunks == [1, 2, 3]
    assert transcript == "第一段 第二段 第三段"


def test_stage_start_does_not_publish_fake_percentage_units(tmp_path: Path) -> None:
    db = MagicMock()
    db.get_job.return_value = {"cancel_requested": 0}
    pipeline = Pipeline(
        db,
        Settings(mode="test", data_dir=tmp_path, token_secret="test-secret"),
    )

    pipeline._start_stage("job_test", "transcribing", 0.43)

    assert db.update_job.call_args.kwargs["estimated_remaining_seconds"] is None
    progress_events = [
        call.args[2]
        for call in db.add_event.call_args_list
        if call.args[1] == "progress"
    ]
    assert progress_events == [{
        "stage": "transcribing",
        "progress": 0.43,
        "completed_units": 0,
        "total_units": 0,
    }]


def test_asr_retries_one_transport_timeout(tmp_path: Path, monkeypatch) -> None:
    pipeline = Pipeline(
        MagicMock(),
        Settings(mode="test", data_dir=tmp_path, token_secret="test-secret"),
    )
    clip = tmp_path / "clip.opus"
    clip.write_bytes(b"valid-test-audio-placeholder")
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {"X-Generation-Id": "gen_test"}
    response.json.return_value = {"text": "转写完成", "usage": {}}
    post = MagicMock(side_effect=[httpx.ReadTimeout("temporary timeout"), response])
    monkeypatch.setattr("app.pipeline.httpx.post", post)
    monkeypatch.setattr("app.pipeline.time.sleep", lambda _seconds: None)

    transcript, audit = pipeline._transcribe(clip, "not-sent-by-mock")

    assert transcript == "转写完成"
    assert audit["attempts"] == 2
    assert post.call_count == 2


def test_review_accepts_fenced_json_and_normalizes_bounded_fields(
    tmp_path: Path, monkeypatch
) -> None:
    pipeline = Pipeline(
        MagicMock(),
        Settings(mode="test", data_dir=tmp_path, token_secret="test-secret"),
    )
    (tmp_path / "page-1.jpg").write_bytes(b"image-placeholder")
    response = MagicMock(spec=httpx.Response)
    response.is_success = True
    response.status_code = 200
    response.json.return_value = {
        "choices": [{
            "message": {
                "content": "```json\n{\"action\":\" KEEP \",\"reason\":\"stable\",\"confidence\":\"95%\",\"extra\":true}\n```"
            }
        }]
    }
    post = MagicMock(return_value=response)
    monkeypatch.setattr("app.pipeline.httpx.post", post)

    decision = pipeline._request_review_decision([_page(1)], 0, "not-sent-by-mock")

    assert decision.action == "keep"
    assert decision.confidence == pytest.approx(0.95)
    assert decision.reason == "stable"
    request_payload = post.call_args.kwargs["json"]
    assert request_payload["max_tokens"] >= 1024
    assert request_payload["reasoning"]["effort"] == "none"


@pytest.mark.parametrize(
    ("provider_value", "expected"),
    [("high", 0.90), ("medium-high", 0.78), ("uncertain", 0.35), ("unexpected label", 0.50)],
)
def test_review_accepts_qualitative_confidence_without_discarding_action(
    tmp_path: Path, monkeypatch, provider_value: str, expected: float
) -> None:
    pipeline = Pipeline(
        MagicMock(),
        Settings(mode="test", data_dir=tmp_path, token_secret="test-secret"),
    )
    (tmp_path / "page-1.jpg").write_bytes(b"image-placeholder")
    response = MagicMock(spec=httpx.Response)
    response.is_success = True
    response.status_code = 200
    response.json.return_value = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "action": "keep",
                    "reason": "stable",
                    "confidence": provider_value,
                })
            }
        }]
    }
    monkeypatch.setattr("app.pipeline.httpx.post", MagicMock(return_value=response))

    decision = pipeline._request_review_decision([_page(1)], 0, "not-sent-by-mock")

    assert decision.action == "keep"
    assert decision.confidence == pytest.approx(expected)


def test_review_policy_block_is_not_retried_or_hidden(
    tmp_path: Path, monkeypatch
) -> None:
    pipeline = Pipeline(
        MagicMock(),
        Settings(mode="test", data_dir=tmp_path, token_secret="test-secret"),
    )
    (tmp_path / "page-1.jpg").write_bytes(b"image-placeholder")
    response = MagicMock(spec=httpx.Response)
    response.is_success = False
    response.status_code = 404
    response.json.return_value = {
        "error": {
            "message": "No endpoints available matching your guardrail restrictions and data policy."
        }
    }
    post = MagicMock(return_value=response)
    monkeypatch.setattr("app.pipeline.httpx.post", post)

    with pytest.raises(OpenRouterPolicyBlocked):
        pipeline._request_review_decision([_page(1)], 0, "not-sent-by-mock")

    assert post.call_count == 1


def test_merge_previous_is_directional_and_preserves_narration(tmp_path: Path) -> None:
    pipeline = Pipeline(
        MagicMock(),
        Settings(mode="test", data_dir=tmp_path, token_secret="test-secret"),
    )
    pages = [_page(1), _page(2)]

    changed = pipeline._apply_review_action(
        "merge_previous",
        pages,
        1,
        tmp_path / "source.mp4",
        tmp_path,
        10,
    )

    assert changed is True
    assert len(pages) == 1
    assert pages[0]["end_ms"] == 20_000
    assert pages[0]["paragraphs"] == ["page 1", "page 2"]


def test_duplicate_micro_tail_is_collapsed_before_correction(tmp_path: Path) -> None:
    pipeline = Pipeline(
        MagicMock(),
        Settings(mode="test", data_dir=tmp_path, token_secret="test-secret"),
    )
    image = Image.new("RGB", (320, 180), "white")
    image.save(tmp_path / "page-1.jpg")
    image.save(tmp_path / "page-2.jpg")
    pages = [_page(1), _page(2)]
    pages[0]["end_ms"] = 10_000
    pages[1]["start_ms"] = 10_000
    pages[1]["end_ms"] = 11_000

    pipeline._collapse_duplicate_and_micro_pages(pages)

    assert len(pages) == 1
    assert pages[0]["end_ms"] == 11_000
    assert pages[0]["paragraphs"] == ["page 1", "page 2"]
    assert pages[0]["audit"]["collapsed_pages"][0]["reason"] == "micro_page"


def test_vision_correction_tolerates_optional_lists_and_extra_fields() -> None:
    page = _page(1)
    correction = Pipeline._vision_correction(
        {
            "title": "校订标题",
            "paragraphs": "校订后的讲稿",
            "extra_model_note": "ignored",
        },
        page,
    )

    assert correction.title == "校订标题"
    assert correction.paragraphs == ["校订后的讲稿"]
    assert correction.visual_evidence == []


def test_download_progress_parser_exposes_bytes_speed_and_eta() -> None:
    sample = Pipeline._parse_download_progress(
        "__YTB2PDF_PROGRESS__137|5242880|10485760|NA|2097152|3\n"
    )

    assert sample == {
        "format_id": "137",
        "downloaded_bytes": 5_242_880,
        "total_bytes": 10_485_760,
        "bytes_per_second": 2_097_152,
        "stage_eta_seconds": 3,
    }


def test_download_progress_parser_uses_estimated_total_and_tolerates_na() -> None:
    sample = Pipeline._parse_download_progress(
        "download:__YTB2PDF_PROGRESS__140|2048|NA|8192|NA|NA"
    )

    assert sample == {
        "format_id": "140",
        "downloaded_bytes": 2048,
        "total_bytes": 8192,
    }


def test_download_progress_parser_ignores_unrelated_output() -> None:
    assert Pipeline._parse_download_progress("[download] Destination: source.mp4") is None
