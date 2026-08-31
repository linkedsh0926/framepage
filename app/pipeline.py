from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import selectors
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Settings
from .db import Database
from .schemas import ReviewDecision, VisionCorrection
from .security import resolve_storage_key
from .youtube import inspect_video, parse_youtube_url


STAGES = (
    "downloading",
    "proposing_frames",
    "transcribing",
    "reviewing_frames",
    "correcting",
    "rendering",
)
TERMINAL_STATUSES = {"completed", "terminal_failed", "retryable_failed", "cancelled"}
ALLOWED_REVIEW_ACTIONS = {
    "keep",
    "drop",
    "merge",
    "merge_previous",
    "merge_next",
    "search_later",
    "search_earlier",
    "resample_local",
    "low_confidence",
}

class Cancelled(RuntimeError):
    pass


class OpenRouterPolicyBlocked(RuntimeError):
    """The account or key policy excludes every endpoint for the requested model."""


class LocalArtifactStore:
    """Filesystem implementation of the object-store boundary used by the pipeline."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        # Job IDs are generated internally, and resolution still enforces containment.
        path = resolve_storage_key(self.root, f"jobs/{job_id}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, key: str) -> Path:
        return resolve_storage_key(self.root, key)

    def key(self, path: Path) -> str:
        resolved = path.resolve()
        if self.root not in resolved.parents:
            raise ValueError("artifact is outside storage root")
        return str(resolved.relative_to(self.root))


class Pipeline:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.store = LocalArtifactStore(settings.data_dir)

    def _check_cancelled(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if not job or job["cancel_requested"]:
            raise Cancelled()

    def _start_stage(self, job_id: str, stage: str, progress: float) -> None:
        self._check_cancelled(job_id)
        self.db.update_job(
            job_id,
            status=stage,
            stage=stage,
            progress=progress,
            # Do not invent a short ETA before workload units are known. Stages
            # with measurable work publish a live estimate below.
            estimated_remaining_seconds=None,
        )
        self.db.add_event(job_id, "stage", {"stage": stage})
        self.db.add_event(
            job_id,
            "progress",
            {
                "stage": stage,
                "progress": progress,
                "completed_units": 0,
                "total_units": 0,
            },
        )

    def _warning(self, job_id: str, code: str, message: str) -> None:
        job = self.db.get_job(job_id)
        warnings = json.loads(job["warnings_json"] or "[]") if job else []
        warning = {"code": code, "message": message}
        warnings.append(warning)
        self.db.update_job(job_id, warnings_json=json.dumps(warnings, ensure_ascii=False))
        self.db.add_event(job_id, "warning", warning)

    def run(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if not job or job["status"] == "completed":
            return
        try:
            request = json.loads(job["request_json"])
            profile = json.loads(job["profile_snapshot_json"])
            profile.update(request.get("profile_overrides", {}))
            workspace = self.store.job_dir(job_id)
            pages = self._run_real(job_id, request, profile, workspace)
            self._start_stage(job_id, "rendering", 0.9)
            pdf_path = workspace / "result.pdf"
            self._render_pdf(request, pages, pdf_path)
            result_key = self.store.key(pdf_path)
            self.db.replace_pages(job_id, pages)
            self.db.update_job(
                job_id,
                status="completed",
                stage="completed",
                progress=1.0,
                page_count=len(pages),
                estimated_remaining_seconds=0,
                result_key=result_key,
                retryable=0,
                error_code=None,
                error_message=None,
            )
            if request.get("learning_policy") == "suggest" and len(pages) >= 2:
                self.db.create_proposal(self.db.get_job(job_id), min(int(profile["review_rounds"]), 3))  # type: ignore[arg-type]
            self.db.add_event(
                job_id,
                "progress",
                {
                    "stage": "rendering",
                    "progress": 1.0,
                    "completed_units": len(pages),
                    "total_units": len(pages),
                    "estimated_remaining_seconds": 0,
                },
            )
            self.db.add_event(
                job_id,
                "completed",
                {
                    "page_count": len(pages),
                    "result_url": f"/api/v1/jobs/{job_id}/result",
                },
            )
        except Cancelled:
            self.db.update_job(
                job_id,
                status="cancelled",
                stage="cancelled",
                progress=0,
                estimated_remaining_seconds=0,
                result_key=None,
            )
            self.db.add_event(job_id, "failed", {
                "code": "job_cancelled",
                "message": "The job was cancelled and its result was revoked.",
                "retryable": False,
                "stage": "cancelled",
            })
        except OpenRouterPolicyBlocked:
            current = self.db.get_job(job_id) or {}
            message = (
                "OpenRouter 的隐私或 Guardrail 设置阻止了 DeepSeek Vision。请在 "
                "OpenRouter Settings → Privacy 中允许该模型的上游数据策略后重试。"
            )
            self.db.update_job(
                job_id,
                status="retryable_failed",
                error_code="openrouter_policy_blocked",
                error_message=message,
                retryable=1,
                estimated_remaining_seconds=0,
            )
            self.db.add_event(
                job_id,
                "failed",
                {
                    "code": "openrouter_policy_blocked",
                    "message": message,
                    "retryable": True,
                    "stage": current.get("stage", "queued"),
                },
            )
        except Exception as exc:
            current = self.db.get_job(job_id) or {}
            # Never expose command output, upstream bodies, paths, or credentials.
            message = "Processing failed at this stage. Retry the job or use a public video."
            self.db.update_job(
                job_id,
                status="retryable_failed",
                error_code="pipeline_failed",
                error_message=message,
                retryable=1,
                estimated_remaining_seconds=0,
            )
            self.db.add_event(
                job_id,
                "failed",
                {
                    "code": "pipeline_failed",
                    "message": message,
                    "retryable": True,
                    "stage": current.get("stage", "queued"),
                    "error_type": type(exc).__name__,
                },
            )

    def _run_real(
        self, job_id: str, request: dict[str, Any], profile: dict[str, Any], workspace: Path
    ) -> list[dict[str, Any]]:
        self._start_stage(job_id, "downloading", 0.05)
        required = [shutil.which("yt-dlp"), shutil.which("ffmpeg"), shutil.which("ffprobe")]
        if self.settings.mode != "real":
            self._warning(
                job_id,
                "degraded_demo_pipeline",
                "Demo mode produced deterministic local artifacts.",
            )
            return self._run_demo(job_id, profile, workspace)
        if not all(required):
            raise RuntimeError("real mode requires yt-dlp, ffmpeg, and ffprobe")

        ref = parse_youtube_url(request["youtube_url"])
        metadata = inspect_video(ref.canonical_url, self.settings)
        if metadata["video"]["duration_seconds"] > self.settings.max_video_seconds:
            raise RuntimeError("video exceeds duration limit")
        duration_seconds = int(metadata["video"]["duration_seconds"])
        downstream_eta = max(45, int(duration_seconds * 0.12 + 45))
        self.db.update_job(job_id, estimated_remaining_seconds=downstream_eta)
        source = self._download_video(
            job_id,
            ref.canonical_url,
            workspace,
            downstream_eta=downstream_eta,
        )
        if source.stat().st_size > self.settings.max_download_bytes:
            raise RuntimeError("video exceeds file size limit")
        self._probe_media(source)
        analysis_video = workspace / "analysis.mp4"
        audio = workspace / "asr.opus"
        self._normalize(
            job_id,
            source,
            analysis_video,
            audio,
            duration_seconds=duration_seconds,
            downstream_eta=downstream_eta,
        )
        self._start_stage(job_id, "proposing_frames", 0.2)
        candidate_started = time.monotonic()
        candidate_last_emit = 0.0

        def report_candidate_scan(completed: int, total: int) -> None:
            nonlocal candidate_last_emit
            now = time.monotonic()
            if now - candidate_last_emit < 0.3 and completed < total:
                return
            candidate_last_emit = now
            ratio = min(1.0, completed / max(1, total))
            elapsed = max(0.1, now - candidate_started)
            scan_eta = int(max(0, elapsed / max(ratio, 0.01) - elapsed))
            remaining = max(1, downstream_eta + scan_eta)
            progress = 0.20 + ratio * 0.13
            self.db.update_job(
                job_id,
                progress=progress,
                estimated_remaining_seconds=remaining,
            )
            self.db.add_event(job_id, "progress", {
                "stage": "proposing_frames",
                "phase": "candidate_scan",
                "progress": progress,
                "completed_units": completed,
                "total_units": total,
                "unit_label": "分析帧",
                "estimated_remaining_seconds": remaining,
            })

        pages = self._opencv_candidates(
            analysis_video,
            workspace,
            int(metadata["video"]["duration_seconds"] * 1000),
            float(profile["change_sensitivity"]),
            on_progress=report_candidate_scan,
        )
        hq_started = time.monotonic()

        def report_hq_frame(completed: int, total: int) -> None:
            ratio = min(1.0, completed / max(1, total))
            elapsed = max(0.1, time.monotonic() - hq_started)
            hq_eta = int(max(0, elapsed / max(ratio, 0.01) - elapsed))
            remaining = max(1, downstream_eta + hq_eta)
            progress = 0.34 + ratio * 0.08
            self.db.update_job(
                job_id,
                progress=progress,
                estimated_remaining_seconds=remaining,
            )
            self.db.add_event(job_id, "progress", {
                "stage": "proposing_frames",
                "phase": "hq_frames",
                "progress": progress,
                "completed_units": completed,
                "total_units": total,
                "unit_label": "候选页",
                "estimated_remaining_seconds": remaining,
            })

        self._extract_hq_frames(source, pages, on_progress=report_hq_frame)
        # Remove exact visual repeats and sub-two-second fragments before they
        # consume ASR/VLM calls; run the same guard again after semantic review.
        self._collapse_duplicate_and_micro_pages(pages)
        if pages:
            self._resample_local(
                source,
                workspace / "local-resample.mp4",
                pages[0]["start_ms"],
                min(pages[0]["end_ms"], pages[0]["start_ms"] + 3000),
                int(profile["local_resample_fps"]),
            )
        self._start_stage(job_id, "transcribing", 0.43)
        key = self.settings.openrouter_api_key
        if key:
            chunk_total = sum(
                len(self._audio_chunk_windows(int(page["start_ms"]), int(page["end_ms"])))
                for page in pages
            )
            chunks_completed = 0
            transcription_started = time.monotonic()
            review_rounds = min(int(profile["review_rounds"]), 3)

            def downstream_seconds() -> float:
                # Review is four-way concurrent; correction is sequential.
                return len(pages) * (2.8 + 0.55 * review_rounds) + 15

            initial_eta = int(chunk_total * 2.0 + downstream_seconds())
            self.db.update_job(job_id, estimated_remaining_seconds=max(1, initial_eta))
            self.db.add_event(job_id, "progress", {
                "stage": "transcribing",
                "progress": 0.43,
                "completed_units": 0,
                "total_units": chunk_total,
                "unit_label": "音频分片",
                "estimated_remaining_seconds": max(1, initial_eta),
            })

            def report_transcription_chunk() -> None:
                nonlocal chunks_completed
                chunks_completed += 1
                elapsed = max(0.001, time.monotonic() - transcription_started)
                observed_seconds = elapsed / chunks_completed
                seconds_per_chunk = min(12.0, max(1.0, observed_seconds))
                remaining_chunks = max(0, chunk_total - chunks_completed)
                eta = int(remaining_chunks * seconds_per_chunk + downstream_seconds())
                progress = 0.43 + 0.16 * (
                    chunks_completed / max(1, chunk_total)
                )
                self.db.update_job(
                    job_id,
                    progress=progress,
                    estimated_remaining_seconds=max(1, eta),
                )
                self.db.add_event(job_id, "progress", {
                    "stage": "transcribing",
                    "progress": progress,
                    "completed_units": chunks_completed,
                    "total_units": chunk_total,
                    "unit_label": "音频分片",
                    "estimated_remaining_seconds": max(1, eta),
                })

            for page in pages:
                transcript, audit = self._transcribe_page(
                    audio,
                    workspace,
                    page,
                    key,
                    on_chunk_complete=report_transcription_chunk,
                )
                page["paragraphs"] = [transcript] if transcript else ["本页没有可辨识的讲解。"]
                page["audit"]["asr"] = audit
        else:
            self._warning(job_id, "openrouter_unconfigured", "OPENROUTER_API_KEY is not configured; deterministic transcript cleanup was used.")
            for page in pages:
                page["paragraphs"] = [f"第 {page['page_number']} 页的讲解文本（降级模式）。"]
        if key:
            self._review_real(
                job_id,
                pages,
                min(int(profile["review_rounds"]), 3),
                key,
                source,
                workspace,
                int(profile["local_resample_fps"]),
            )
        else:
            self._review_demo(job_id, pages, min(int(profile["review_rounds"]), 3))
        self._collapse_duplicate_and_micro_pages(pages)
        self._start_stage(job_id, "correcting", 0.73)
        if key:
            page_total = max(1, len(pages))
            correction_started = time.monotonic()
            self.db.update_job(
                job_id,
                estimated_remaining_seconds=max(1, int(page_total * 3.0 + 15)),
            )
            for page_index, page in enumerate(pages):
                self._check_cancelled(job_id)
                corrected, audit = self._correct(
                    page,
                    workspace,
                    key,
                    transcript_style=request.get("transcript_style", "polished"),
                )
                page.update(corrected)
                page["audit"]["vision"] = audit
                progress = 0.73 + ((page_index + 1) / page_total) * 0.15
                completed_pages = page_index + 1
                elapsed = max(0.001, time.monotonic() - correction_started)
                seconds_per_page = min(20.0, max(1.5, elapsed / completed_pages))
                remaining_pages = page_total - completed_pages
                self.db.update_job(
                    job_id,
                    progress=progress,
                    estimated_remaining_seconds=max(
                        1, int(remaining_pages * seconds_per_page + 15)
                    ),
                )
                self.db.add_event(job_id, "progress", {
                    "stage": "correcting",
                    "progress": progress,
                    "completed_units": completed_pages,
                    "total_units": page_total,
                    "unit_label": "章节页",
                    "estimated_remaining_seconds": max(
                        1, int(remaining_pages * seconds_per_page + 15)
                    ),
                })
        return pages

    def _run_demo(
        self, job_id: str, profile: dict[str, Any], workspace: Path
    ) -> list[dict[str, Any]]:
        self._start_stage(job_id, "proposing_frames", 0.2)
        pages: list[dict[str, Any]] = []
        for number in range(1, 4):
            preview = workspace / f"page-{number:04}.jpg"
            self._write_demo_slide(preview, number)
            start = (number - 1) * 90_000
            pages.append(
                {
                    "page_number": number,
                    "start_ms": start,
                    "end_ms": number * 90_000,
                    "keyframe_ms": number * 90_000 - 800,
                    "title": f"演示页 {number}",
                    "paragraphs": [],
                    "confidence": round(0.96 - number * 0.01, 2),
                    "preview_key": self.store.key(preview),
                    "audit": {
                        "candidate_source": "deterministic_demo",
                        "dedup_basis": "perceptual_hash_and_layout",
                        "boundary_confidence": 0.96,
                        "final_state_confidence": 0.95,
                    },
                }
            )
        self._start_stage(job_id, "transcribing", 0.43)
        demo_text = [
            "本页介绍 FramePage 如何识别逐步展开的课件，并保留内容加载完成后的最终画面。",
            "系统按页面时间范围切分中英混讲音频，再把讲解与高清关键帧一起校订。",
            "最终生成 A4 图文 PDF；参数学习只形成建议，必须由用户确认后应用。",
        ]
        for page, paragraph in zip(pages, demo_text, strict=True):
            page["paragraphs"] = [paragraph]
            page["audit"]["asr"] = {"mode": "demo", "model": "qwen/qwen3-asr-1.7b"}
        self._review_demo(job_id, pages, min(int(profile["review_rounds"]), 3))
        self._start_stage(job_id, "correcting", 0.73)
        for page in pages:
            page["audit"]["vision"] = {
                "mode": "demo",
                "model": "deepseek/deepseek-v4-flash-vision-exp",
                "visual_evidence": ["slide title", "progressive content"],
                "uncertain_terms": [],
            }
        return pages

    def _review_demo(self, job_id: str, pages: list[dict[str, Any]], rounds: int) -> None:
        self._start_stage(job_id, "reviewing_frames", 0.6)
        # The adapter applies only the bounded actions documented by the product spec.
        actual_rounds = min(max(rounds, 1), 3)
        for round_number in range(1, actual_rounds + 1):
            issue_count = 1 if round_number == 1 else 0
            self.db.add_event(job_id, "review_round_started", {
                "round": round_number,
                "issue_count": issue_count,
            })
            action = (
                "low_confidence" if actual_rounds == 1 else
                "resample_local" if round_number == 1 else "keep"
            )
            if action not in ALLOWED_REVIEW_ACTIONS:
                raise RuntimeError("vision returned an unsupported review action")
            self.db.add_event(job_id, "review_action", {
                "round": round_number,
                "action": action,
                "window_start_ms": pages[0]["start_ms"],
                "window_end_ms": min(pages[0]["end_ms"], pages[0]["start_ms"] + 3000),
                "reason": "Confirm the nearest stable final frame" if round_number == 1 else "Selection is stable",
            })
            if round_number == 1 and actual_rounds > 1:
                self.db.add_event(job_id, "review_round_failed", {
                    "round": round_number,
                    "reason": "A partial-load candidate requires bounded local resampling",
                    "retryable": True,
                })
                continue
            self.db.add_event(job_id, "review_round_passed", {
                "round": round_number,
                "remaining_issue_count": 0 if actual_rounds > 1 else 1,
            })
            if round_number >= 2 or actual_rounds == 1:
                self.db.add_event(job_id, "review_stopped", {
                    "round": round_number,
                    "condition": "stable_across_two_rounds" if round_number >= 2 else "configured_round_limit",
                })
                break

    def _review_real(
        self,
        job_id: str,
        pages: list[dict[str, Any]],
        rounds: int,
        key: str,
        source: Path,
        workspace: Path,
        resample_fps: int,
    ) -> None:
        """Run bounded VLM review and translate its whitelist action server-side."""
        self._start_stage(job_id, "reviewing_frames", 0.6)
        max_rounds = min(3, max(1, rounds))
        review_targets = list(pages)
        for round_number in range(1, max_rounds + 1):
            self._check_cancelled(job_id)
            self.db.add_event(job_id, "review_round_started", {
                "round": round_number,
                "issue_count": len(review_targets),
            })
            indexed_decisions: list[tuple[int, dict[str, Any], ReviewDecision]] = []
            upstream_errors: list[str] = []
            unavailable_pages: list[dict[str, Any]] = []
            # Every round reevaluates every current page, including pages changed by
            # the prior round. If selection did not change, only malformed or failed
            # provider responses are retried rather than charging for the whole set.
            # Decisions are collected before mutations so context is consistent.
            round_pages = list(pages)
            round_targets = [page for page in review_targets if page in round_pages]
            workers = min(4, max(1, len(round_targets)))
            review_units_remaining = len(round_targets) + (
                max_rounds - round_number
            ) * len(round_pages)
            self.db.update_job(
                job_id,
                estimated_remaining_seconds=max(
                    1,
                    int(
                        review_units_remaining * 2.2 / workers
                        + len(pages) * 3.0
                        + 15
                    ),
                ),
            )

            def review_one(
                page_index: int, candidate: dict[str, Any]
            ) -> tuple[int, dict[str, Any], ReviewDecision | None, str | None]:
                try:
                    decision = self._request_review_decision(round_pages, page_index, key)
                    if decision.action not in ALLOWED_REVIEW_ACTIONS:
                        raise ValueError("unsupported review action")
                    return page_index, candidate, decision, None
                except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                    return page_index, candidate, None, type(exc).__name__

            self._check_cancelled(job_id)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(review_one, round_pages.index(candidate), candidate)
                    for candidate in round_targets
                ]
                for checked, future in enumerate(as_completed(futures), 1):
                    self._check_cancelled(job_id)
                    page_index, page, decision, error_type = future.result()
                    if decision is not None:
                        indexed_decisions.append((page_index, page, decision))
                    else:
                        upstream_errors.append(error_type or "invalid_response")
                        unavailable_pages.append(page)
                        page["confidence"] = min(float(page["confidence"]), 0.5)
                        page["audit"]["review"] = {
                            "status": "unavailable",
                            "round": round_number,
                            "low_confidence": True,
                        }
                    progress = 0.6 + 0.12 * (
                        (round_number - 1 + checked / max(1, len(round_targets))) / max_rounds
                    )
                    self.db.update_job(
                        job_id,
                        progress=progress,
                        estimated_remaining_seconds=max(
                            1,
                            int(
                                (
                                    len(round_targets) - checked
                                    + (max_rounds - round_number) * len(round_pages)
                                ) * 2.2 / workers
                                + len(pages) * 3.0
                                + 15
                            ),
                        ),
                    )
                    self.db.add_event(job_id, "progress", {
                        "stage": "reviewing_frames",
                        "progress": progress,
                        "completed_units": checked,
                        "total_units": len(round_targets),
                        "unit_label": "候选页",
                        "round": round_number,
                        "estimated_remaining_seconds": max(
                            1,
                            int(
                                (
                                    len(round_targets) - checked
                                    + (max_rounds - round_number) * len(round_pages)
                                ) * 2.2 / workers
                                + len(pages) * 3.0
                                + 15
                            ),
                        ),
                    })

            decisions = [
                (page, decision)
                for _, page, decision in sorted(indexed_decisions, key=lambda item: item[0])
            ]

            changed_count = 0
            for page, decision in decisions:
                if page not in pages:
                    continue
                index = pages.index(page)
                self.db.add_event(job_id, "review_action", {
                    "round": round_number,
                    "action": decision.action,
                    "window_start_ms": page["start_ms"],
                    "window_end_ms": page["end_ms"],
                    "reason": decision.reason,
                    "confidence": decision.confidence,
                })
                changed = self._apply_review_action(
                    decision.action, pages, index, source, workspace, resample_fps
                )
                if changed:
                    changed_count += 1
                # A dropped/merged-away page must not be written after mutation.
                if page in pages:
                    page["audit"]["review"] = {
                        "round": round_number,
                        "action": decision.action,
                        "confidence": decision.confidence,
                        "reason": decision.reason,
                    }
            for number, page in enumerate(pages, 1):
                page["page_number"] = number

            if not upstream_errors and changed_count == 0 and len(decisions) == len(round_targets):
                self.db.add_event(job_id, "review_round_passed", {
                    "round": round_number,
                    "remaining_issue_count": 0,
                })
                self.db.add_event(job_id, "review_stopped", {
                    "round": round_number,
                    "condition": "stable_selection",
                })
                break
            review_targets = (
                list(pages)
                if changed_count
                else [page for page in unavailable_pages if page in pages]
            )
            reason = (
                "vision_review_unavailable:" + ",".join(sorted(set(upstream_errors)))
                if upstream_errors
                else "selection_changed_and_requires_full_re_evaluation"
            )
            self.db.add_event(job_id, "review_round_failed", {
                "round": round_number,
                "reason": reason,
                "retryable": round_number < max_rounds,
            })
            if round_number == max_rounds:
                self.db.add_event(job_id, "review_stopped", {
                    "round": round_number,
                    "condition": (
                        "vision_unavailable_after_budget"
                        if upstream_errors and not decisions
                        else (
                            "partial_vision_unavailable_after_budget"
                            if upstream_errors
                            else "configured_round_limit"
                        )
                    ),
                })

    def _request_review_decision(
        self, pages: list[dict[str, Any]], index: int, key: str
    ) -> ReviewDecision:
        page = pages[index]
        previous = pages[index - 1]["paragraphs"][-1] if index > 0 and pages[index - 1]["paragraphs"] else ""
        following = pages[index + 1]["paragraphs"][0] if index + 1 < len(pages) and pages[index + 1]["paragraphs"] else ""
        prompt = (
            "Judge whether the image is the stable final state after progressive content has fully "
            "loaded and, together with its ASR, represents a relatively independent chapter. "
            "The product requires ONE page per original PPT slide, not one page per animation step. "
            "Compare all three images before deciding. If CURRENT and NEXT retain the same slide "
            "background, heading, layout, or topic while NEXT adds or reveals any content, CURRENT "
            "is an incomplete progressive state and you MUST choose merge_next, even when its narration "
            "is substantive. Keep only the last, most complete state of that slide. Drop meaningless "
            "transitions. Merge a true continuation or duplicate into the correct neighbor. For an "
            "unstable capture use search_later, search_earlier, or resample_local. Never keep a frame "
            "merely because it is sharp or visually stable for a few seconds. Drop non-content "
            "splash screens, legal/investment disclaimers, subscribe prompts, and empty section "
            "transitions unless the user would need that frame to understand the lecture. "
            "Return JSON only with "
            "action, reason, confidence. action must be one of keep, drop, merge_previous, "
            "merge_next, search_later, search_earlier, resample_local, low_confidence. "
            "confidence must be a numeric value from 0 to 1, never a word such as high or low. "
            "Use merge_previous when the current narration continues the preceding page; use "
            "merge_next when the current image is a partial state of the following page. "
            "Do not change global thresholds.\n"
            f"Window: {page['start_ms']}-{page['end_ms']} ms\n"
            f"Previous context: {previous[-300:]}\n"
            f"Current narration: {' '.join(page['paragraphs'])[:3000]}\n"
            f"Next context: {following[:300]}"
        )
        last_error: Exception | None = None
        prior: str | None = None
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for label, candidate_index in (
            ("PREVIOUS IMAGE", index - 1),
            ("CURRENT IMAGE", index),
            ("NEXT IMAGE", index + 1),
        ):
            if not 0 <= candidate_index < len(pages):
                continue
            candidate_image = base64.b64encode(
                self.store.path(pages[candidate_index]["preview_key"]).read_bytes()
            ).decode()
            content.extend([
                {"type": "text", "text": label},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{candidate_image}"},
                },
            ])
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": content,
        }]
        for attempt in range(2):
            if attempt:
                if prior:
                    messages.append({"role": "assistant", "content": prior})
                messages.append({"role": "user", "content": "Repair that response into valid JSON using only an allowed action."})
            try:
                response = httpx.post(
                    f"{self.settings.openrouter_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "deepseek/deepseek-v4-flash-vision-exp",
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "temperature": 0,
                        # This is bounded classification, not open-ended reasoning.
                        # Disabling reasoning leaves the completion budget for JSON.
                        "max_tokens": 1024,
                        "reasoning": {"effort": "none", "exclude": True},
                    },
                    timeout=90,
                )
                self._raise_for_openrouter_status(response)
                prior = self._message_text(
                    response.json()["choices"][0]["message"]["content"]
                )
                payload = self._json_object(prior)
                confidence = payload.get("confidence")
                if isinstance(confidence, str):
                    candidate = confidence.strip().lower().replace("-", "_").replace(" ", "_")
                    qualitative = {
                        "certain": 0.98,
                        "very_high": 0.95,
                        "high": 0.90,
                        "medium_high": 0.78,
                        "moderate": 0.65,
                        "medium": 0.65,
                        "medium_low": 0.50,
                        "low": 0.35,
                        "very_low": 0.15,
                        "uncertain": 0.35,
                    }
                    if candidate in qualitative:
                        confidence = qualitative[candidate]
                    else:
                        percent = candidate.endswith("%")
                        if percent:
                            candidate = candidate[:-1].strip()
                        try:
                            confidence = float(candidate)
                        except ValueError:
                            # Confidence is metadata, not the decision itself. An
                            # unknown label must not discard an otherwise valid action.
                            confidence = 0.5
                        if percent:
                            confidence /= 100
                if isinstance(confidence, (int, float)) and 1 < confidence <= 100:
                    confidence = confidence / 100
                action = payload.get("action")
                reason = str(payload.get("reason") or "No reason provided")[:500]
                if isinstance(action, str):
                    action = action.strip().lower().replace("-", "_").replace(" ", "_")
                    aliases = {
                        "retain": "keep",
                        "discard": "drop",
                        "remove": "drop",
                        "merge_with_previous": "merge_previous",
                        "merge_into_previous": "merge_previous",
                        "merge_with_next": "merge_next",
                        "merge_into_next": "merge_next",
                    }
                    action = aliases.get(action, action)
                    if action == "merge":
                        lowered_reason = reason.lower()
                        action = (
                            "merge_previous"
                            if any(word in lowered_reason for word in ("previous", "preceding", "continuation"))
                            else "merge_next"
                        )
                normalized = {
                    "action": action,
                    "reason": reason,
                    "confidence": confidence if confidence is not None else 0.5,
                }
                return ReviewDecision.model_validate(normalized)
            except OpenRouterPolicyBlocked:
                raise
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last_error = exc
        if isinstance(last_error, httpx.HTTPError):
            raise last_error
        raise ValueError("Vision review did not return a valid bounded action") from last_error

    @staticmethod
    def _raise_for_openrouter_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code == 404:
            try:
                error = response.json().get("error", {})
                message = str(error.get("message") or "").lower()
            except (ValueError, AttributeError):
                message = ""
            if "guardrail restrictions and data policy" in message:
                raise OpenRouterPolicyBlocked()
        response.raise_for_status()

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "\n".join(parts)
        raise ValueError("OpenRouter message content is not textual")

    @staticmethod
    def _json_object(content: str) -> dict[str, Any]:
        """Extract the first complete JSON object without trusting markdown fences."""
        candidate = content.strip()
        fence = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            candidate,
            re.DOTALL | re.IGNORECASE,
        )
        if fence:
            candidate = fence.group(1).strip()
        decoder = json.JSONDecoder()
        for offset, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise ValueError("OpenRouter response did not contain a JSON object")

    @staticmethod
    def _vision_correction(
        payload: dict[str, Any], page: dict[str, Any]
    ) -> VisionCorrection:
        def string_list(value: Any) -> list[str]:
            if isinstance(value, str):
                return [value.strip()] if value.strip() else []
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
            return []

        title = str(payload.get("title") or page.get("title") or "未命名章节").strip()
        paragraphs = string_list(payload.get("paragraphs"))
        if not paragraphs:
            paragraphs = string_list(page.get("paragraphs"))
        return VisionCorrection.model_validate({
            "title": title[:300],
            "paragraphs": paragraphs,
            "visual_evidence": string_list(payload.get("visual_evidence")),
            "uncertain_terms": string_list(payload.get("uncertain_terms")),
            "warnings": string_list(payload.get("warnings")),
        })

    def _apply_review_action(
        self,
        action: str,
        pages: list[dict[str, Any]],
        index: int,
        source: Path,
        workspace: Path,
        resample_fps: int,
    ) -> bool:
        page = pages[index]
        if action == "drop" and len(pages) > 1:
            pages.pop(index)
            return True
        if action == "merge_previous" and index > 0:
            previous = pages[index - 1]
            previous["end_ms"] = page["end_ms"]
            previous["paragraphs"].extend(page["paragraphs"])
            pages.pop(index)
            return True
        if action in {"merge", "merge_next"} and index + 1 < len(pages):
            following = pages[index + 1]
            following["start_ms"] = page["start_ms"]
            following["paragraphs"] = page["paragraphs"] + following["paragraphs"]
            pages.pop(index)
            return True
        if action == "search_earlier":
            page["keyframe_ms"] = max(page["start_ms"], page["keyframe_ms"] - 500)
            self._extract_frame_at(source, page)
            return True
        if action == "search_later":
            page["keyframe_ms"] = min(page["end_ms"] - 1, page["keyframe_ms"] + 500)
            self._extract_frame_at(source, page)
            return True
        if action == "resample_local":
            resampled = workspace / f"review-resample-{page['page_number']:04}.mp4"
            self._resample_local(
                source,
                resampled,
                page["start_ms"],
                page["end_ms"],
                resample_fps,
            )
            selected_ms = self._select_stable_resampled_frame(
                resampled, page["start_ms"], resample_fps
            )
            if selected_ms is not None:
                page["keyframe_ms"] = min(page["end_ms"] - 1, selected_ms)
            self._extract_frame_at(source, page)
            return True
        if action == "low_confidence":
            page["confidence"] = min(float(page["confidence"]), 0.5)
            return True
        return False

    def _extract_frame_at(self, source: Path, page: dict[str, Any]) -> None:
        target = self.store.path(page["preview_key"])
        command = [
            shutil.which("ffmpeg") or "ffmpeg", "-y", "-ss", f"{page['keyframe_ms']/1000:.3f}",
            "-i", str(source), "-frames:v", "1", "-vf", "scale='min(1920,iw)':-2", "-q:v", "3", str(target),
        ]
        if subprocess.run(command, capture_output=True, timeout=60, check=False).returncode:
            raise RuntimeError("bounded frame search failed")

    @staticmethod
    def _select_stable_resampled_frame(
        video: Path, window_start_ms: int, fps: int
    ) -> int | None:
        """Choose a sharp later frame after the last material local change."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            return None
        capture = cv2.VideoCapture(str(video))
        actual_fps = capture.get(cv2.CAP_PROP_FPS) or float(fps)
        samples: list[tuple[int, float, Any]] = []
        previous = None
        last_change = 0
        index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (64, 36))
            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            timestamp = window_start_ms + int(index / actual_fps * 1000)
            samples.append((timestamp, sharpness, small))
            if previous is not None:
                difference = float(np.mean(cv2.absdiff(previous, small))) / 255.0
                if difference >= 0.055:
                    last_change = index
            previous = small
            index += 1
        capture.release()
        if not samples:
            return None
        stable_wait_frames = max(1, round(actual_fps * 0.5))
        later_floor = round(len(samples) * 0.6)
        start_index = min(
            len(samples) - 1,
            max(last_change + stable_wait_frames, later_floor),
        )
        stable_candidates = samples[start_index:] or samples[-1:]
        return max(stable_candidates, key=lambda item: item[1])[0]

    @staticmethod
    def _progress_number(value: str | None) -> float | None:
        if not value or value.strip().lower() in {"na", "none", "unknown"}:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @classmethod
    def _parse_download_progress(cls, line: str) -> dict[str, float | str] | None:
        marker = "__YTB2PDF_PROGRESS__"
        if marker not in line:
            return None
        fields = line.split(marker, 1)[1].strip().split("|")
        if len(fields) != 6:
            return None
        format_id, downloaded_raw, total_raw, estimated_raw, speed_raw, eta_raw = fields
        downloaded = cls._progress_number(downloaded_raw) or 0.0
        total = cls._progress_number(total_raw) or cls._progress_number(estimated_raw)
        speed = cls._progress_number(speed_raw)
        eta = cls._progress_number(eta_raw)
        result: dict[str, float | str] = {
            "format_id": format_id.strip() or "media",
            "downloaded_bytes": downloaded,
        }
        if total:
            result["total_bytes"] = total
        if speed:
            result["bytes_per_second"] = speed
        if eta is not None:
            result["stage_eta_seconds"] = eta
        return result

    def _download_video(
        self,
        job_id: str,
        canonical_url: str,
        workspace: Path,
        *,
        downstream_eta: int,
    ) -> Path:
        template = str(workspace / "source.%(ext)s")
        command = [
            shutil.which("yt-dlp") or "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--newline",
            "--progress",
            "--progress-delta",
            "0.35",
            "--progress-template",
            (
                "download:__YTB2PDF_PROGRESS__%(info.format_id)s|"
                "%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
                "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s"
            ),
            "--max-filesize",
            str(self.settings.max_download_bytes),
            "-f",
            "bestvideo+bestaudio/best",
            "--merge-output-format",
            "mp4",
            "-o",
            template,
            canonical_url,
        ]
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if proc.stdout is None:
            proc.kill()
            raise RuntimeError("yt-dlp progress stream is unavailable")
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        started = time.monotonic()
        deadline = started + 900
        format_order: list[str] = []
        last_emit = 0.0
        last_progress = 0.05
        try:
            while True:
                self._check_cancelled(job_id)
                if time.monotonic() >= deadline:
                    proc.kill()
                    raise RuntimeError("yt-dlp download timed out")
                ready = selector.select(timeout=0.25)
                for key, _ in ready:
                    line = key.fileobj.readline()
                    if not line:
                        continue
                    sample = self._parse_download_progress(line)
                    if not sample:
                        continue
                    format_id = str(sample["format_id"])
                    if format_id not in format_order:
                        format_order.append(format_id)
                    item_index = format_order.index(format_id)
                    downloaded = float(sample["downloaded_bytes"])
                    total = float(sample.get("total_bytes") or 0)
                    speed_value = float(sample.get("bytes_per_second") or 0)
                    stage_eta_value = float(sample.get("stage_eta_seconds") or 0)
                    if total <= 0 and speed_value > 0 and stage_eta_value > 0:
                        total = downloaded + speed_value * stage_eta_value
                    item_ratio = min(1.0, downloaded / total) if total > 0 else 0.0
                    # bestvideo+bestaudio normally has two resources. Reserving
                    # half the stage for each avoids a backwards jump when yt-dlp
                    # switches from the video stream to the audio stream.
                    stage_ratio = min(1.0, (item_index + item_ratio) / 2.0)
                    progress = max(last_progress, 0.05 + stage_ratio * 0.09)
                    now = time.monotonic()
                    if now - last_emit < 0.3 and item_ratio < 1:
                        continue
                    last_emit = now
                    last_progress = progress
                    stage_eta = int(stage_eta_value)
                    remaining = max(1, downstream_eta + stage_eta)
                    self.db.update_job(
                        job_id,
                        progress=progress,
                        estimated_remaining_seconds=remaining,
                    )
                    self.db.add_event(job_id, "progress", {
                        "stage": "downloading",
                        "phase": "video" if item_index == 0 else "audio",
                        "progress": progress,
                        "completed_units": int(downloaded),
                        "total_units": int(total),
                        "unit_label": "bytes",
                        "downloaded_bytes": int(downloaded),
                        "total_bytes": int(total) if total > 0 else None,
                        "bytes_per_second": int(speed_value),
                        "stage_eta_seconds": stage_eta,
                        "estimated_remaining_seconds": remaining,
                    })
                returncode = proc.poll()
                if returncode is not None:
                    # Consume any final buffered progress line before leaving.
                    for line in proc.stdout:
                        sample = self._parse_download_progress(line)
                        if sample:
                            last_progress = max(last_progress, 0.135)
                    break
        finally:
            selector.close()
            proc.stdout.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        if proc.returncode is None:
            proc.wait(timeout=5)
        if proc.returncode != 0:
            raise RuntimeError("yt-dlp download failed")
        candidates = sorted(workspace.glob("source.*"))
        if len(candidates) != 1:
            raise RuntimeError("download did not produce one media file")
        self.db.update_job(
            job_id,
            progress=0.14,
            estimated_remaining_seconds=downstream_eta,
        )
        self.db.add_event(job_id, "progress", {
            "stage": "downloading",
            "phase": "download_complete",
            "progress": 0.14,
            "completed_units": candidates[0].stat().st_size,
            "total_units": candidates[0].stat().st_size,
            "unit_label": "bytes",
            "downloaded_bytes": candidates[0].stat().st_size,
            "total_bytes": candidates[0].stat().st_size,
            "bytes_per_second": 0,
            "stage_eta_seconds": 0,
            "estimated_remaining_seconds": downstream_eta,
        })
        return candidates[0]

    def _normalize(
        self,
        job_id: str,
        source: Path,
        analysis_video: Path,
        audio: Path,
        *,
        duration_seconds: int,
        downstream_eta: int,
    ) -> None:
        self.db.update_job(
            job_id,
            status="downloading",
            stage="normalizing",
            progress=0.14,
            estimated_remaining_seconds=downstream_eta,
        )
        self.db.add_event(job_id, "stage", {"stage": "normalizing"})
        video_cmd = [
            shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(source),
            "-vf", "scale=-2:360,fps=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-an", "-progress", "pipe:1", "-nostats", str(analysis_video),
        ]
        audio_cmd = [
            shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(source),
            "-vn", "-ar", "16000", "-ac", "1", "-c:a", "libopus", "-b:a", "32k",
            "-progress", "pipe:1", "-nostats", str(audio),
        ]
        phases = (
            (video_cmd, "analysis_video", 0.14, 0.175),
            (audio_cmd, "audio_track", 0.175, 0.20),
        )
        for command, phase, progress_start, progress_end in phases:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            if proc.stdout is None:
                proc.kill()
                raise RuntimeError("ffmpeg progress stream is unavailable")
            phase_started = time.monotonic()
            last_emit = 0.0
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            deadline = phase_started + 900
            try:
                while True:
                    self._check_cancelled(job_id)
                    if time.monotonic() >= deadline:
                        raise RuntimeError("ffmpeg normalization timed out")
                    ready = selector.select(timeout=0.25)
                    for key, _ in ready:
                        line = key.fileobj.readline()
                        if not line or not line.startswith("out_time_us="):
                            continue
                        processed = (self._progress_number(line.split("=", 1)[1].strip()) or 0) / 1_000_000
                        ratio = min(1.0, processed / max(1, duration_seconds))
                        now = time.monotonic()
                        if now - last_emit < 0.3 and ratio < 1:
                            continue
                        last_emit = now
                        progress = progress_start + (progress_end - progress_start) * ratio
                        elapsed = max(0.1, now - phase_started)
                        phase_eta = int(max(0, elapsed / max(ratio, 0.01) - elapsed))
                        remaining = max(1, downstream_eta + phase_eta)
                        self.db.update_job(
                            job_id,
                            progress=progress,
                            estimated_remaining_seconds=remaining,
                        )
                        self.db.add_event(job_id, "progress", {
                            "stage": "normalizing",
                            "phase": phase,
                            "progress": progress,
                            "completed_units": min(duration_seconds, int(processed)),
                            "total_units": duration_seconds,
                            "unit_label": "视频秒",
                            "estimated_remaining_seconds": remaining,
                        })
                    if proc.poll() is not None:
                        break
            finally:
                selector.close()
                proc.stdout.close()
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
            if proc.returncode != 0:
                raise RuntimeError("ffmpeg normalization failed")

    def _probe_media(self, source: Path) -> dict[str, Any]:
        command = [
            shutil.which("ffprobe") or "ffprobe",
            "-v", "error",
            "-show_streams",
            "-show_format",
            "-of", "json",
            str(source),
        ]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode:
            raise RuntimeError("ffprobe validation failed")
        try:
            data = json.loads(proc.stdout)
            duration = float(data.get("format", {}).get("duration") or 0)
            streams = data.get("streams", [])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("ffprobe returned invalid metadata") from exc
        if duration <= 0 or duration > self.settings.max_video_seconds:
            raise RuntimeError("media duration is outside the supported range")
        if not any(stream.get("codec_type") == "video" for stream in streams):
            raise RuntimeError("download has no video stream")
        if not any(stream.get("codec_type") == "audio" for stream in streams):
            raise RuntimeError("download has no audio stream")
        return data

    def _extract_hq_frames(
        self,
        source: Path,
        pages: list[dict[str, Any]],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        total = len(pages)
        for index, page in enumerate(pages, 1):
            target = self.store.path(page["preview_key"])
            command = [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y", "-ss", f"{page['keyframe_ms'] / 1000:.3f}", "-i", str(source),
                "-frames:v", "1", "-vf", "scale='min(1920,iw)':-2", "-q:v", "3", str(target),
            ]
            if subprocess.run(command, capture_output=True, timeout=60, check=False).returncode:
                raise RuntimeError("high-resolution frame extraction failed")
            if on_progress:
                on_progress(index, total)

    @staticmethod
    def _resample_local(
        source: Path, target: Path, start_ms: int, end_ms: int, fps: int
    ) -> None:
        fps = min(12, max(8, fps))
        command = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-y", "-ss", f"{start_ms / 1000:.3f}", "-t", f"{max(0.1, (end_ms-start_ms)/1000):.3f}",
            "-i", str(source), "-vf", f"scale=-2:360,fps={fps}", "-an", str(target),
        ]
        if subprocess.run(command, capture_output=True, timeout=120, check=False).returncode:
            raise RuntimeError("local frame resampling failed")

    def _opencv_candidates(
        self,
        video: Path,
        workspace: Path,
        duration_ms: int,
        sensitivity: float,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            import cv2
            import numpy as np
        except ImportError:
            return self._fallback_pages(workspace, duration_ms)
        cap = cv2.VideoCapture(str(video))
        fps = cap.get(cv2.CAP_PROP_FPS) or 4.0
        frame_total = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
        sample_stride = max(1, round(fps))
        samples: list[tuple[int, Any, float]] = []
        index = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if index % sample_stride == 0:
                timestamp = int(index / fps * 1000)
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                # Auto-mask the subtitle band. Changing subtitles otherwise dominate
                # every frame comparison in lecture videos.
                visual_region = gray[: max(1, int(gray.shape[0] * 0.86)), :]
                small = cv2.resize(visual_region, (128, 64))
                sharpness = float(cv2.Laplacian(visual_region, cv2.CV_64F).var())
                samples.append((timestamp, small, sharpness))
            index += 1
            if on_progress and (index % max(12, sample_stride * 3) == 0):
                on_progress(min(index, frame_total), frame_total)
        cap.release()
        if on_progress:
            on_progress(frame_total, frame_total)
        if len(samples) < 2:
            return self._fallback_pages(workspace, duration_ms)

        def visual_difference(left: Any, right: Any) -> float:
            pixel = float(np.mean(cv2.absdiff(left, right))) / 255.0
            left_edges = cv2.Canny(left, 60, 160)
            right_edges = cv2.Canny(right, 60, 160)
            edge = float(np.mean(left_edges != right_edges))
            return pixel * 0.65 + edge * 0.35

        scored = [
            (sample_index, samples[sample_index][0], visual_difference(
                samples[sample_index - 1][1], samples[sample_index][1]
            ))
            for sample_index in range(1, len(samples))
        ]
        # Higher sensitivity lowers the one-second change threshold. The default
        # stays near 0.07: lower values over-segment ordinary PPT build animations
        # and make the VLM solve a much noisier selection problem.
        threshold = min(0.095, max(0.05, 0.095 - sensitivity * 0.045))
        peaks: list[tuple[int, int, float]] = []
        for scored_index, item in enumerate(scored):
            sample_index, timestamp, score = item
            neighborhood = (
                scored[max(0, scored_index - 2):scored_index]
                + scored[scored_index + 1:scored_index + 3]
            )
            if score < threshold or any(score < neighbor[2] for neighbor in neighborhood):
                continue
            if peaks and timestamp - peaks[-1][1] < 4000:
                if score > peaks[-1][2]:
                    peaks[-1] = item
                continue
            peaks.append(item)
        if len(peaks) > 240:
            strongest = sorted(peaks, key=lambda item: item[2], reverse=True)[:240]
            peaks = sorted(strongest, key=lambda item: item[1])
        boundaries = [0] + [timestamp for _, timestamp, _ in peaks]
        if boundaries[-1] != duration_ms:
            boundaries.append(duration_ms)
        if len(boundaries) < 3:
            return self._fallback_pages(workspace, duration_ms)
        reader = cv2.VideoCapture(str(video))
        pages: list[dict[str, Any]] = []
        for page_number, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
            candidates = [item for item in samples if start <= item[0] < end]
            if not candidates:
                continue
            stable = [item for item in candidates if item[0] <= end - 500] or candidates
            timestamp, _, _ = max(stable[-8:], key=lambda item: item[2])
            reader.set(cv2.CAP_PROP_POS_MSEC, timestamp)
            ok, frame = reader.read()
            if not ok:
                continue
            preview = workspace / f"page-{page_number:04}.jpg"
            cv2.imwrite(str(preview), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            pages.append({
                "page_number": page_number,
                "start_ms": start,
                "end_ms": end,
                "keyframe_ms": timestamp,
                "title": f"视频页 {page_number}",
                "paragraphs": [],
                "confidence": 0.82,
                "preview_key": self.store.key(preview),
                "audit": {
                    "candidate_source": "opencv_one_second_peak_edges_pixels",
                    "subtitle_mask_fraction": 0.14,
                    "change_threshold": round(threshold, 4),
                    "dedup_basis": "visual_distance_and_vlm_neighbor_context",
                    "boundary_confidence": 0.78,
                    "final_state_confidence": 0.76,
                },
            })
        reader.release()
        return pages or self._fallback_pages(workspace, duration_ms)

    def _collapse_duplicate_and_micro_pages(
        self, pages: list[dict[str, Any]]
    ) -> None:
        """Remove exact visual repeats and sub-two-second tail fragments."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            return

        def feature(page: dict[str, Any]) -> Any:
            image = cv2.imread(str(self.store.path(page["preview_key"])), cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ValueError("candidate image is unreadable")
            region = image[: max(1, int(image.shape[0] * 0.86)), :]
            return cv2.resize(region, (128, 64))

        index = 0
        while index < len(pages):
            page = pages[index]
            duration = int(page["end_ms"]) - int(page["start_ms"])
            duplicate_previous = False
            if index > 0:
                try:
                    difference = float(np.mean(cv2.absdiff(
                        feature(pages[index - 1]), feature(page)
                    ))) / 255.0
                    duplicate_previous = difference < 0.018
                except ValueError:
                    duplicate_previous = False
            if index > 0 and (duration < 2000 or duplicate_previous):
                previous = pages[index - 1]
                previous["end_ms"] = page["end_ms"]
                previous["paragraphs"].extend(page["paragraphs"])
                previous["audit"].setdefault("collapsed_pages", []).append({
                    "reason": "micro_page" if duration < 2000 else "visual_duplicate",
                    "start_ms": page["start_ms"],
                    "end_ms": page["end_ms"],
                })
                pages.pop(index)
                continue
            if index == 0 and duration < 2000 and len(pages) > 1:
                following = pages[1]
                following["start_ms"] = page["start_ms"]
                following["paragraphs"] = page["paragraphs"] + following["paragraphs"]
                pages.pop(0)
                continue
            index += 1
        for page_number, page in enumerate(pages, 1):
            page["page_number"] = page_number

    def _fallback_pages(self, workspace: Path, duration_ms: int) -> list[dict[str, Any]]:
        count = max(1, min(12, round(duration_ms / 90_000)))
        span = max(1, duration_ms // count)
        pages = []
        for page_number in range(1, count + 1):
            preview = workspace / f"page-{page_number:04}.jpg"
            self._write_demo_slide(preview, page_number)
            start = (page_number - 1) * span
            end = duration_ms if page_number == count else page_number * span
            pages.append({
                "page_number": page_number,
                "start_ms": start,
                "end_ms": end,
                "keyframe_ms": max(start, end - 500),
                "title": f"视频页 {page_number}",
                "paragraphs": [],
                "confidence": 0.5,
                "preview_key": self.store.key(preview),
                "audit": {"candidate_source": "timed_fallback", "low_confidence": True},
            })
        return pages

    @staticmethod
    def _audio_clip(audio: Path, clip: Path, start_ms: int, end_ms: int) -> None:
        start = max(0, start_ms - 400) / 1000
        duration = max(0.1, (end_ms - start_ms + 800) / 1000)
        command = [
            shutil.which("ffmpeg") or "ffmpeg", "-y", "-ss", str(start), "-t", str(duration),
            "-i", str(audio), "-c:a", "copy", str(clip),
        ]
        if subprocess.run(command, capture_output=True, timeout=120, check=False).returncode:
            raise RuntimeError("audio slicing failed")

    @staticmethod
    def _audio_chunk_windows(start: int, end: int) -> list[tuple[int, int]]:
        chunks: list[tuple[int, int]] = []
        cursor = start
        while cursor < end:
            # OpenRouter providers have a bounded processing window. Keep source
            # audio below 25s; _audio_clip adds at most 800ms of boundary context.
            chunk_end = min(end, cursor + 24_000)
            chunks.append((cursor, chunk_end))
            if chunk_end >= end:
                break
            cursor = max(cursor + 1, chunk_end - 600)
        return chunks

    def _transcribe_page(
        self,
        audio: Path,
        workspace: Path,
        page: dict[str, Any],
        key: str,
        *,
        on_chunk_complete: Callable[[], None] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Split page audio into timeout-safe chunks and de-duplicate overlap."""
        start = int(page["start_ms"])
        end = int(page["end_ms"])
        chunks = self._audio_chunk_windows(start, end)
        transcripts: list[str] = []
        audits: list[dict[str, Any]] = []
        for chunk_number, (chunk_start, chunk_end) in enumerate(chunks, 1):
            clip = workspace / f"audio-{page['page_number']:04}-{chunk_number:03}.opus"
            self._audio_clip(audio, clip, chunk_start, chunk_end)
            text, audit = self._transcribe(clip, key)
            transcripts.append(text)
            audits.append({
                **audit,
                "chunk_number": chunk_number,
                "start_ms": chunk_start,
                "end_ms": chunk_end,
            })
            if on_chunk_complete is not None:
                on_chunk_complete()
        merged = ""
        for transcript in transcripts:
            merged = self._merge_text_overlap(merged, transcript)
        return merged.strip(), {
            "model": "qwen/qwen3-asr-1.7b",
            "chunk_count": len(chunks),
            "overlap_ms": 600,
            "chunks": audits,
        }

    @staticmethod
    def _merge_text_overlap(left: str, right: str) -> str:
        if not left:
            return right
        if not right:
            return left
        maximum = min(len(left), len(right), 500)
        overlap = 0
        for size in range(maximum, 2, -1):
            if left[-size:].casefold() == right[:size].casefold():
                overlap = size
                break
        separator = "" if overlap or left.endswith((" ", "\n")) else " "
        return left + separator + right[overlap:]

    def _transcribe(self, clip: Path, key: str) -> tuple[str, dict[str, Any]]:
        encoded = base64.b64encode(clip.read_bytes()).decode()
        payload = {
            "model": "qwen/qwen3-asr-1.7b",
            "input_audio": {"data": encoded, "format": "opus"},
            "temperature": 0,
        }
        response: httpx.Response | None = None
        last_transport_error: httpx.TransportError | None = None
        attempts = 2
        for attempt in range(attempts):
            try:
                response = httpx.post(
                    f"{self.settings.openrouter_base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                    timeout=httpx.Timeout(150, connect=15),
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt + 1 < attempts:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                response.raise_for_status()
                break
            except httpx.TransportError as exc:
                last_transport_error = exc
                if attempt + 1 >= attempts:
                    raise
                time.sleep(1.5 * (attempt + 1))
        if response is None:
            if last_transport_error:
                raise last_transport_error
            raise RuntimeError("transcription provider returned no response")
        result = response.json()
        audit = {
            "model": payload["model"],
            "generation_id": response.headers.get("X-Generation-Id"),
            "usage": result.get("usage", {}),
            "capability": "simple_json",
            "attempts": attempt + 1,
        }
        return str(result.get("text") or "").strip(), audit

    def _correct(
        self,
        page: dict[str, Any],
        workspace: Path,
        key: str,
        *,
        transcript_style: str = "polished",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        image_path = self.store.path(page["preview_key"])
        image_data = base64.b64encode(image_path.read_bytes()).decode()
        style_instruction = (
            "Correct terminology, punctuation, repeated filler, and obvious ASR errors into a "
            "readable edited transcript."
            if transcript_style == "polished"
            else "Correct only obvious ASR term and punctuation errors; preserve the speaker's "
            "wording, repetitions, and sentence order as closely as possible."
        )
        prompt = (
            f"{style_instruction} Use the image only as evidence. Do not add facts. "
            "Return JSON with title, paragraphs, visual_evidence, uncertain_terms, warnings.\n"
            + "Narration: " + "\n".join(page["paragraphs"])
        )
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
            ],
        }]
        last_error: Exception | None = None
        prior_content: str | None = None
        for attempt in range(2):
            if attempt:
                if prior_content:
                    messages.append({"role": "assistant", "content": prior_content})
                messages.append({"role": "user", "content": "Repair the prior response into valid JSON only."})
            try:
                response = httpx.post(
                    f"{self.settings.openrouter_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "deepseek/deepseek-v4-flash-vision-exp",
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "temperature": 0,
                        "max_tokens": 4096,
                        "reasoning": {"effort": "none", "exclude": True},
                    },
                    timeout=90,
                )
                self._raise_for_openrouter_status(response)
                result = response.json()
                content = self._message_text(result["choices"][0]["message"]["content"])
                prior_content = content
                validated = self._vision_correction(self._json_object(content), page)
                return {
                    "title": validated.title,
                    "paragraphs": validated.paragraphs,
                }, {
                    "model": "deepseek/deepseek-v4-flash-vision-exp",
                    "generation_id": response.headers.get("X-Generation-Id"),
                    "usage": result.get("usage", {}),
                    "repair_attempted": bool(attempt),
                    "visual_evidence": validated.visual_evidence,
                    "uncertain_terms": validated.uncertain_terms,
                    "warnings": validated.warnings,
                }
            except OpenRouterPolicyBlocked:
                raise
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last_error = exc
        try:
            text_response = httpx.post(
                f"{self.settings.openrouter_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": "deepseek/deepseek-v4-flash-0731",
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "max_tokens": 4096,
                    "reasoning": {"effort": "none", "exclude": True},
                },
                timeout=90,
            )
            self._raise_for_openrouter_status(text_response)
            text_result = text_response.json()
            validated = self._vision_correction(
                self._json_object(
                    self._message_text(text_result["choices"][0]["message"]["content"])
                ),
                page,
            )
            return {
                "title": validated.title,
                "paragraphs": validated.paragraphs,
            }, {
                "model": "deepseek/deepseek-v4-flash-0731",
                "generation_id": text_response.headers.get("X-Generation-Id"),
                "usage": text_result.get("usage", {}),
                "fallback_from_vision": True,
                "visual_evidence": validated.visual_evidence,
                "uncertain_terms": validated.uncertain_terms,
                "warnings": validated.warnings,
            }
        except OpenRouterPolicyBlocked:
            raise
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            last_error = exc
        # Deterministic fallback preserves ASR rather than hallucinating.
        return {
            "title": page["title"],
            "paragraphs": [" ".join(text.split()) for text in page["paragraphs"]],
        }, {
            "model": "deterministic_asr_cleanup",
            "fallback_reason": type(last_error).__name__ if last_error else "invalid_response",
        }

    def _render_pdf(
        self, request: dict[str, Any], pages: list[dict[str, Any]], output: Path
    ) -> None:
        source = html.escape(request["youtube_url"])
        density = request.get("page_density", "comfortable")
        body_size, line_height = {
            "compact": (9.5, 1.45),
            "comfortable": (11, 1.65),
            "spacious": (12.5, 1.8),
        }.get(density, (11, 1.65))
        body = [
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><style>",
            "@page{size:A4 portrait;margin:16mm}body{font-family:'Noto Sans CJK SC',sans-serif;color:#17201d}",
            ".cover{page-break-after:always}article{page-break-before:always}img{width:100%;max-height:120mm;object-fit:contain}",
            f"h1{{font-size:24pt}}h2{{font-size:16pt}}p{{font-size:{body_size}pt;line-height:{line_height}}}.meta{{color:#52605b}}",
            "</style></head><body><section class='cover'><h1>FramePage 图文讲解</h1>",
            f"<p>来源：<a href='{source}'>{source}</a></p>",
            f"<p>生成时间：{html.escape(datetime.now(timezone.utc).isoformat())}</p>",
            f"<p>页数：{len(pages)}</p>",
            f"<p>参数：Profile {html.escape(request['profile_id'])} v{request['profile_version']}；"
            f"讲稿 {html.escape(request['transcript_style'])}；密度 {html.escape(request['page_density'])}</p></section>",
        ]
        for page in pages:
            image_path = self.store.path(page["preview_key"])
            image_data = base64.b64encode(image_path.read_bytes()).decode()
            body.extend([
                f"<article><h2>{html.escape(page['title'])}</h2>",
                f"<img src='data:image/jpeg;base64,{image_data}' alt='关键帧 {page['page_number']}'>",
            ])
            if request.get("include_timestamps", True):
                body.append(
                    f"<p class='meta'>{self._format_ms(page['start_ms'])} - {self._format_ms(page['end_ms'])}</p>"
                )
            body.extend(f"<p>{html.escape(paragraph)}</p>" for paragraph in page["paragraphs"])
            body.append("</article>")
        body.append("</body></html>")
        document = "".join(body)
        try:
            from ctypes.util import find_library

            if not find_library("gobject-2.0") or not find_library("pango-1.0"):
                raise ImportError("WeasyPrint native libraries are unavailable")
            from weasyprint import HTML

            HTML(string=document, base_url=str(output.parent)).write_pdf(str(output))
        except (ImportError, OSError):
            try:
                self._reportlab_pdf(request, pages, output)
            except (ImportError, OSError, ValueError):
                self._minimal_pdf(output, len(pages))
        if not output.exists() or not output.read_bytes().startswith(b"%PDF"):
            raise RuntimeError("PDF rendering did not produce a valid PDF")

    def _reportlab_pdf(
        self, request: dict[str, Any], pages: list[dict[str, Any]], output: Path
    ) -> None:
        """Portable A4 fallback when WeasyPrint's native libraries are absent."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen.canvas import Canvas

        font_name = "FramePageCJK"
        if font_name not in pdfmetrics.getRegisteredFontNames():
            candidates = (
                Path("/Library/Fonts/Arial Unicode.ttf"),
                Path("/System/Library/Fonts/STHeiti Medium.ttc"),
                Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
            )
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                try:
                    pdfmetrics.registerFont(TTFont(font_name, str(candidate)))
                    break
                except (OSError, ValueError):
                    continue
            else:
                font_name = "STSong-Light"
                if font_name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(UnicodeCIDFont(font_name))

        width, height = A4
        margin = 46.0
        canvas = Canvas(str(output), pagesize=A4, pageCompression=1)
        canvas.setTitle("FramePage 图文讲解")
        canvas.setAuthor("FramePage")
        canvas.setSubject("YouTube 关键帧与对齐讲稿")

        def wrapped(text: str, size: float, max_width: float) -> list[str]:
            result: list[str] = []
            for source_line in (text.splitlines() or [""]):
                current = ""
                for character in source_line:
                    candidate = current + character
                    if current and pdfmetrics.stringWidth(candidate, font_name, size) > max_width:
                        result.append(current.rstrip())
                        current = character.lstrip()
                    else:
                        current = candidate
                result.append(current.rstrip())
            return result or [""]

        def draw_header(page_number: int, continuation: bool = False) -> float:
            canvas.setFillColorRGB(0.08, 0.24, 0.21)
            canvas.setFont(font_name, 9)
            canvas.drawString(margin, height - 34, "帧页 · 视频讲义")
            label = f"{page_number:02d}" + (" · 续" if continuation else "")
            canvas.drawRightString(width - margin, height - 34, label)
            canvas.setStrokeColorRGB(0.82, 0.87, 0.85)
            canvas.line(margin, height - 44, width - margin, height - 44)
            return height - 74

        canvas.setFillColorRGB(0.08, 0.24, 0.21)
        canvas.setFont(font_name, 12)
        canvas.drawString(margin, height - 62, "FRAMEPAGE / 视频讲义")
        canvas.setFont(font_name, 28)
        canvas.drawString(margin, height - 116, "关键帧与对齐讲稿")
        canvas.setFillColorRGB(0.89, 0.42, 0.28)
        canvas.roundRect(margin, height - 154, 105, 8, 4, stroke=0, fill=1)
        canvas.setFillColorRGB(0.29, 0.36, 0.33)
        canvas.setFont(font_name, 11)
        cover_lines = [
            f"共 {len(pages)} 个关键帧章节",
            f"生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"Profile：{request['profile_id']} v{request['profile_version']}",
            f"讲稿：{request['transcript_style']}    排版：{request['page_density']}",
        ]
        y = height - 210
        for line in cover_lines:
            canvas.drawString(margin, y, line)
            y -= 25
        canvas.setFillColorRGB(0.38, 0.43, 0.41)
        canvas.setFont(font_name, 9.5)
        for line in wrapped(f"来源：{request['youtube_url']}", 9.5, width - margin * 2):
            canvas.drawString(margin, y, line)
            y -= 16
        canvas.showPage()

        density = request.get("page_density", "comfortable")
        body_size, leading = {
            "compact": (9.5, 14.5),
            "comfortable": (10.8, 17.5),
            "spacious": (12.0, 21.0),
        }.get(density, (10.8, 17.5))
        for page in pages:
            y = draw_header(page["page_number"])
            canvas.setFillColorRGB(0.08, 0.13, 0.12)
            canvas.setFont(font_name, 18)
            title_lines = wrapped(page["title"], 18, width - margin * 2)[:3]
            for line in title_lines:
                canvas.drawString(margin, y, line)
                y -= 24
            y -= 8

            image_path = self.store.path(page["preview_key"])
            from PIL import Image

            with Image.open(image_path) as image:
                image_width, image_height = image.size
            max_image_width = width - margin * 2
            max_image_height = 345.0
            scale = min(max_image_width / image_width, max_image_height / image_height)
            draw_width = image_width * scale
            draw_height = image_height * scale
            image_x = margin + (max_image_width - draw_width) / 2
            image_y = y - draw_height
            canvas.setFillColorRGB(0.95, 0.95, 0.92)
            canvas.roundRect(margin - 5, image_y - 5, max_image_width + 10, draw_height + 10, 5, stroke=0, fill=1)
            canvas.drawImage(
                str(image_path), image_x, image_y, draw_width, draw_height,
                preserveAspectRatio=True, anchor="c", mask="auto",
            )
            y = image_y - 24
            if request.get("include_timestamps", True):
                canvas.setFillColorRGB(0.37, 0.43, 0.40)
                canvas.setFont(font_name, 9)
                canvas.drawString(
                    margin,
                    y,
                    f"{self._format_ms(page['start_ms'])} - {self._format_ms(page['end_ms'])}",
                )
                y -= 24
            canvas.setStrokeColorRGB(0.90, 0.42, 0.28)
            canvas.setLineWidth(3)
            canvas.line(margin, y, margin + 78, y)
            y -= 24

            text_lines: list[str] = []
            for paragraph in page["paragraphs"]:
                text_lines.extend(wrapped(paragraph, body_size, width - margin * 2))
                text_lines.append("")
            while text_lines:
                if y < margin + leading:
                    canvas.showPage()
                    y = draw_header(page["page_number"], continuation=True)
                    canvas.setFillColorRGB(0.08, 0.13, 0.12)
                    canvas.setFont(font_name, 15)
                    for line in wrapped(page["title"] + "（续）", 15, width - margin * 2)[:3]:
                        canvas.drawString(margin, y, line)
                        y -= 21
                    y -= 10
                line = text_lines.pop(0)
                canvas.setFillColorRGB(0.16, 0.20, 0.19)
                canvas.setFont(font_name, body_size)
                if line:
                    canvas.drawString(margin, y, line)
                y -= leading
            canvas.showPage()
        canvas.save()

    @staticmethod
    def _format_ms(value: int) -> str:
        seconds = value // 1000
        return f"{seconds // 60:02}:{seconds % 60:02}"

    @staticmethod
    def _write_demo_slide(output: Path, number: int) -> None:
        try:
            from PIL import Image, ImageDraw

            image = Image.new("RGB", (1280, 720), "#f3f0e8")
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((65, 55, 1215, 665), radius=24, fill="#ffffff", outline="#bed0c8", width=4)
            draw.rectangle((65, 55, 85, 665), fill="#e16b48")
            draw.text((130, 105), f"FramePage Demo / Slide {number}", fill="#143e35", font=None)
            draw.line((130, 155, 1110, 155), fill="#d7dfdc", width=3)
            colors = ["#f4c95d", "#63a88f", "#de7659"]
            for index in range(number + 1):
                top = 215 + index * 105
                draw.ellipse((140, top, 175, top + 35), fill=colors[index % len(colors)])
                draw.rounded_rectangle((210, top, 1010 - index * 45, top + 32), radius=12, fill="#dce8e3")
                draw.rounded_rectangle((210, top + 48, 850 - index * 35, top + 66), radius=8, fill="#edf2f0")
            image.save(output, "JPEG", quality=88, optimize=True)
        except ImportError:
            # OpenCV is part of the media installation and is a secondary fallback.
            import cv2
            import numpy as np

            image = np.full((720, 1280, 3), (232, 240, 243), dtype=np.uint8)
            cv2.rectangle(image, (65, 55), (1215, 665), (255, 255, 255), -1)
            cv2.putText(image, f"FramePage Demo / Slide {number}", (130, 125), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (53, 62, 20), 2)
            cv2.imwrite(str(output), image, [cv2.IMWRITE_JPEG_QUALITY, 88])

    @staticmethod
    def _minimal_pdf(output: Path, page_count: int) -> None:
        """Dependency-free, valid mock PDF for demo/degraded installations."""
        lines = ["FramePage demo PDF", f"Video pages: {page_count}", "Install WeasyPrint for styled A4 output."]
        stream = "BT /F1 16 Tf 72 760 Td " + " ".join(
            f"({line}) Tj 0 -28 Td" for line in lines
        ) + " ET"
        objects = [
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            f"<< /Length {len(stream.encode())} >>\nstream\n{stream}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        data = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for index, obj in enumerate(objects, 1):
            offsets.append(len(data))
            data.extend(f"{index} 0 obj\n{obj}\nendobj\n".encode())
        xref = len(data)
        data.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            data.extend(f"{offset:010} 00000 n \n".encode())
        data.extend(
            f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        )
        output.write_bytes(data)


def request_digest(data: dict[str, Any]) -> tuple[str, str]:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return canonical, hashlib.sha256(canonical.encode()).hexdigest()
