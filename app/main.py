from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import BackgroundTasks, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings
from .db import Database
from .pipeline import Pipeline, TERMINAL_STATUSES, request_digest
from .schemas import (
    DuplicateProfileRequest,
    InspectRequest,
    JobCreate,
    ProfileCreate,
    ProfilePatch,
    ProposalApplyRequest,
)
from .security import (
    derive_job_token,
    hash_token,
    sign_download,
    token_matches,
    valid_download_signature,
)
from .youtube import InvalidYouTubeURL, VideoInspectionError, inspect_video, parse_youtube_url


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers or {}


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, settings: Settings):
        super().__init__(app)
        self.settings = settings
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if 1 <= len(supplied) <= 100 else f"req_{uuid.uuid4().hex}"
        request.state.request_id = request_id
        content_length = request.headers.get("content-length")
        try:
            request_size = int(content_length) if content_length else 0
        except ValueError:
            request_size = self.settings.max_request_bytes + 1
        if request_size > self.settings.max_request_bytes:
            response = _error_response(
                request_id, 413, "request_too_large", "Request body is too large."
            )
            response.headers["X-Request-ID"] = request_id
            return response
        if request.url.path.startswith("/api/"):
            now = time.monotonic()
            key = request.client.host if request.client else "unknown"
            bucket = self.hits[key]
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= self.settings.rate_limit_per_minute:
                response = _error_response(
                    request_id,
                    429,
                    "rate_limited",
                    "Too many requests. Try again in one minute.",
                )
                response.headers["Retry-After"] = "60"
                response.headers["X-Request-ID"] = request_id
                return response
            bucket.append(now)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def _rid(request: Request) -> str:
    return request.state.request_id


def _error_response(
    request_id: str, status: int, code: str, message: str, details: Any = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "request_id": request_id,
            "error": {
                "code": code,
                "message": message,
                "details": details,
            },
        },
    )


def _parse_if_match(value: str | None) -> int:
    if value is None:
        raise APIError(428, "if_match_required", "If-Match with the current profile version is required.")
    candidate = value.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:]
    candidate = candidate.strip('"')
    try:
        version = int(candidate)
    except ValueError as exc:
        raise APIError(400, "invalid_if_match", "If-Match must contain a profile version.") from exc
    if version < 1:
        raise APIError(400, "invalid_if_match", "If-Match must contain a positive profile version.")
    return version


def _bearer(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise APIError(401, "job_token_required", "Use Authorization: Bearer <job access token>.")
    return token.strip()


def _job_for_request(request: Request, job_id: str) -> dict[str, Any]:
    db: Database = request.app.state.db
    job = db.get_job(job_id)
    if not job:
        raise APIError(404, "job_not_found", "Job not found or expired.")
    if datetime.fromisoformat(job["expires_at"]) <= datetime.now(timezone.utc):
        raise APIError(410, "job_expired", "This job and its artifacts have expired.")
    if not token_matches(_bearer(request), job["token_hash"]):
        raise APIError(401, "invalid_job_token", "The job access token is invalid.")
    return job


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["id"],
        "status": job["status"],
        "current_stage": job["stage"],
        "progress": job["progress"],
        "page_count": job["page_count"],
        "estimated_remaining_seconds": job["estimated_remaining_seconds"],
        "warnings": json.loads(job["warnings_json"] or "[]"),
        "error": (
            {
                "code": job["error_code"],
                "message": job["error_message"],
                "retryable": bool(job["retryable"]),
            }
            if job.get("error_code")
            else None
        ),
        "attempt": job["attempt"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "expires_at": job["expires_at"],
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.mode == "real" and not settings.openrouter_api_key:
        raise ValueError("OPENROUTER_API_KEY is required in real mode")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database_path)  # type: ignore[arg-type]
    pipeline = Pipeline(db, settings)
    application = FastAPI(
        title="FramePage API",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )
    application.state.settings = settings
    application.state.db = db
    application.state.pipeline = pipeline
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match", "Last-Event-ID", "X-Request-ID"],
        expose_headers=["X-Request-ID", "ETag", "Content-Disposition"],
    )
    application.add_middleware(RequestContextMiddleware, settings=settings)

    @application.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
        response = _error_response(_rid(request), exc.status_code, exc.code, exc.message, exc.details)
        response.headers.update(exc.headers)
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "location": list(error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return _error_response(
            _rid(request), 422, "validation_error", "The request is invalid.", details
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        message = "Endpoint not found." if exc.status_code == 404 else str(exc.detail)
        return _error_response(_rid(request), exc.status_code, code, message)

    @application.get("/healthz")
    async def health(request: Request) -> dict[str, Any]:
        return {"request_id": _rid(request), "status": "ok", "mode": settings.mode}

    @application.get("/", include_in_schema=False)
    async def root(request: Request) -> Response:
        index_path = Path(__file__).resolve().parent.parent / "index.html"
        if not index_path.exists():
            raise APIError(404, "frontend_not_found", "Frontend artifact is not installed.")
        return FileResponse(index_path, media_type="text/html")

    @application.post("/api/v1/videos/inspect")
    async def inspect(request: Request, body: InspectRequest) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(inspect_video, body.youtube_url, settings)
        except InvalidYouTubeURL as exc:
            raise APIError(422, "invalid_youtube_url", str(exc)) from exc
        except VideoInspectionError as exc:
            raise APIError(422, "video_unavailable", str(exc)) from exc
        return {"request_id": _rid(request), **result}

    @application.get("/api/v1/profiles")
    async def list_profiles(request: Request) -> dict[str, Any]:
        return {"request_id": _rid(request), "profiles": db.list_profiles()}

    @application.get("/api/v1/profiles/{profile_id}")
    async def get_profile(request: Request, profile_id: str) -> JSONResponse:
        profile = db.get_profile(profile_id)
        if not profile:
            raise APIError(404, "profile_not_found", "Profile not found.")
        return JSONResponse(
            {"request_id": _rid(request), "profile": profile},
            headers={"ETag": f'"{profile["version"]}"'},
        )

    @application.post("/api/v1/profiles", status_code=201)
    async def create_profile(request: Request, body: ProfileCreate) -> JSONResponse:
        values = body.model_dump()
        if values.get("source_profile_id") and not db.get_profile(values["source_profile_id"]):
            raise APIError(404, "source_profile_not_found", "Source Profile not found.")
        profile = db.create_profile(values)
        return JSONResponse(
            status_code=201,
            content={"request_id": _rid(request), "profile": profile},
            headers={"ETag": '"1"'},
        )

    @application.patch("/api/v1/profiles/{profile_id}")
    async def update_profile(
        request: Request,
        profile_id: str,
        body: ProfilePatch,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> JSONResponse:
        expected = _parse_if_match(if_match)
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise APIError(422, "empty_profile_update", "Provide at least one field to update.")
        outcome, profile = db.update_profile(profile_id, expected, changes)
        if outcome == "missing":
            raise APIError(404, "profile_not_found", "Profile not found.")
        if outcome == "default":
            raise APIError(403, "default_profile_immutable", "The Smart Default Profile cannot be modified.")
        if outcome == "conflict":
            raise APIError(
                409,
                "profile_version_conflict",
                "The Profile changed. Reload it and retry with the latest version.",
                {"current_version": profile["version"] if profile else None},
            )
        return JSONResponse(
            {"request_id": _rid(request), "profile": profile},
            headers={"ETag": f'"{profile["version"]}"'},  # type: ignore[index]
        )

    @application.post("/api/v1/profiles/{profile_id}/duplicate", status_code=201)
    async def duplicate_profile(
        request: Request,
        profile_id: str,
        body: DuplicateProfileRequest | None = None,
    ) -> JSONResponse:
        source = db.get_profile(profile_id)
        if not source:
            raise APIError(404, "profile_not_found", "Profile not found.")
        values = {
            **source,
            "name": body.name.strip() if body and body.name else f"{source['name']} 副本",
            "source_profile_id": source["id"],
        }
        profile = db.create_profile(values)
        return JSONResponse(
            status_code=201,
            content={"request_id": _rid(request), "profile": profile},
            headers={"ETag": '"1"'},
        )

    @application.delete("/api/v1/profiles/{profile_id}", status_code=204)
    async def delete_profile(request: Request, profile_id: str) -> Response:
        outcome = db.delete_profile(profile_id)
        if outcome == "missing":
            raise APIError(404, "profile_not_found", "Profile not found.")
        if outcome == "default":
            raise APIError(403, "default_profile_immutable", "The Smart Default Profile cannot be deleted.")
        return Response(status_code=204)

    @application.post("/api/v1/jobs", status_code=202)
    async def create_job(
        request: Request,
        body: JobCreate,
        background_tasks: BackgroundTasks,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        if not idempotency_key or not (8 <= len(idempotency_key) <= 200):
            raise APIError(400, "idempotency_key_required", "Provide an Idempotency-Key of 8 to 200 characters.")
        try:
            ref = parse_youtube_url(
                body.youtube_url, allow_demo=settings.mode in {"demo", "test"}
            )
        except InvalidYouTubeURL as exc:
            raise APIError(422, "invalid_youtube_url", str(exc)) from exc
        profile = db.get_profile(body.profile_id)
        if not profile:
            raise APIError(404, "profile_not_found", "Selected Profile not found.")
        if profile["version"] != body.profile_version:
            raise APIError(
                409,
                "profile_version_conflict",
                "The selected Profile version is stale. Reload Profiles before creating the job.",
                {"current_version": profile["version"]},
            )
        request_data = body.model_dump()
        request_data["youtube_url"] = ref.canonical_url
        canonical, digest = request_digest(request_data)
        existing = db.idempotency_lookup("create_job", idempotency_key)
        is_new = existing is None
        if existing and existing["request_hash"] != digest:
            raise APIError(
                409,
                "idempotency_conflict",
                "This Idempotency-Key was already used with a different request.",
            )
        if existing:
            job_id = existing["resource_id"]
            job = db.get_job(job_id)
            if not job:
                raise APIError(409, "idempotency_resource_expired", "The original job has expired; use a new Idempotency-Key.")
        else:
            job_id = f"job_{uuid.uuid4().hex}"
            token = derive_job_token(settings.token_secret, job_id)
            expires = datetime.now(timezone.utc) + timedelta(hours=settings.artifact_ttl_hours)
            created_id, disposition = db.create_job(
                job_id=job_id,
                request_json=canonical,
                request_hash=digest,
                token_hash=hash_token(token),
                profile=profile,
                expires_at=expires.isoformat(),
                idempotency_key=idempotency_key,
            )
            if disposition == "conflict":
                raise APIError(409, "idempotency_conflict", "Idempotency-Key conflict.")
            job_id = created_id
            job = db.get_job(job_id)
            is_new = disposition == "created"
            if is_new:
                background_tasks.add_task(pipeline.run, job_id)
        token = derive_job_token(settings.token_secret, job_id)
        return JSONResponse(
            status_code=202,
            content={
                "request_id": _rid(request),
                "job_id": job_id,
                "access_token": token,
                "status_url": f"/api/v1/jobs/{job_id}",
                "events_url": f"/api/v1/jobs/{job_id}/events",
                "expires_at": job["expires_at"],  # type: ignore[index]
                "idempotent_replay": not is_new,
            },
        )

    @application.get("/api/v1/jobs/{job_id}")
    async def get_job(request: Request, job_id: str) -> dict[str, Any]:
        job = _job_for_request(request, job_id)
        return {"request_id": _rid(request), **_job_view(job)}

    @application.get("/api/v1/jobs/{job_id}/events")
    async def job_events(
        request: Request,
        job_id: str,
        last_event_id: int = Header(default=0, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        _job_for_request(request, job_id)

        async def stream() -> AsyncIterator[str]:
            cursor = max(0, last_event_id)
            while True:
                events = db.list_events(job_id, cursor)
                for event in events:
                    cursor = event["id"]
                    payload = json.dumps(event["data"], ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {payload}\n\n"
                job = db.get_job(job_id)
                if not job or (job["status"] in TERMINAL_STATUSES and not events):
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @application.delete("/api/v1/jobs/{job_id}", status_code=202)
    async def cancel_job(request: Request, job_id: str) -> JSONResponse:
        job = _job_for_request(request, job_id)
        if job["status"] == "cancelled":
            return JSONResponse(status_code=202, content={"request_id": _rid(request), **_job_view(job)})
        if job["status"] in {"terminal_failed"}:
            raise APIError(409, "job_not_cancellable", "This job can no longer be cancelled.")
        db.update_job(
            job_id,
            cancel_requested=1,
            status="cancelled",
            stage="cancelled",
            estimated_remaining_seconds=0,
            result_key=None,
        )
        db.add_event(job_id, "failed", {
            "code": "job_cancelled",
            "message": "The job was cancelled; result access has been revoked and cleanup is queued.",
            "retryable": False,
            "stage": job["stage"],
        })
        return JSONResponse(
            status_code=202,
            content={"request_id": _rid(request), **_job_view(db.get_job(job_id))},  # type: ignore[arg-type]
        )

    @application.post("/api/v1/jobs/{job_id}/retry", status_code=202)
    async def retry_job(
        request: Request, job_id: str, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        job = _job_for_request(request, job_id)
        if job["status"] != "retryable_failed" or not job["retryable"]:
            raise APIError(409, "job_not_retryable", "Only a retryable failed job can be retried.")
        db.update_job(
            job_id,
            status="queued",
            stage="queued",
            progress=0,
            cancel_requested=0,
            retryable=0,
            error_code=None,
            error_message=None,
            attempt=job["attempt"] + 1,
        )
        event_cursor = db.add_event(
            job_id,
            "retry_started",
            {"attempt": job["attempt"] + 1},
        )
        background_tasks.add_task(pipeline.run, job_id)
        return JSONResponse(
            status_code=202,
            content={
                "request_id": _rid(request),
                "event_cursor": event_cursor,
                **_job_view(db.get_job(job_id)),  # type: ignore[arg-type]
            },
        )

    @application.get("/api/v1/jobs/{job_id}/pages")
    async def get_pages(
        request: Request,
        job_id: str,
        cursor: str | None = None,
        limit: int = Query(default=20, ge=1, le=50),
    ) -> dict[str, Any]:
        job = _job_for_request(request, job_id)
        try:
            offset = int(cursor or "0")
        except ValueError as exc:
            raise APIError(400, "invalid_cursor", "The pages cursor is invalid.") from exc
        if offset < 0:
            raise APIError(400, "invalid_cursor", "The pages cursor is invalid.")
        rows = db.list_pages(job_id, offset, limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {
                **{key: value for key, value in page.items() if key != "preview_key"},
                "preview_url": f"/api/v1/jobs/{job_id}/pages/{page['page_number']}/preview",
            }
            for page in rows
        ]
        return {
            "request_id": _rid(request),
            "items": items,
            "next_cursor": str(offset + limit) if has_more else None,
            "total": job["page_count"],
        }

    @application.get("/api/v1/jobs/{job_id}/pages/{page_number}/preview")
    async def page_preview(request: Request, job_id: str, page_number: int) -> Response:
        _job_for_request(request, job_id)
        pages = db.list_pages(job_id, max(0, page_number - 1), 1)
        if not pages or pages[0]["page_number"] != page_number or not pages[0]["preview_key"]:
            raise APIError(404, "page_not_found", "Preview page not found.")
        try:
            path = pipeline.store.path(pages[0]["preview_key"])
        except ValueError as exc:
            raise APIError(404, "artifact_not_found", "Preview artifact not found.") from exc
        if not path.is_file():
            raise APIError(404, "artifact_not_found", "Preview artifact not found.")
        return FileResponse(path, media_type="image/jpeg")

    @application.get("/api/v1/jobs/{job_id}/result")
    async def job_result(request: Request, job_id: str) -> dict[str, Any]:
        job = _job_for_request(request, job_id)
        if job["status"] != "completed" or not job["result_key"]:
            raise APIError(409, "result_not_ready", "The PDF is not ready yet.")
        path = pipeline.store.path(job["result_key"])
        if not path.is_file():
            raise APIError(410, "result_expired", "The PDF artifact has expired.")
        expires = int(time.time()) + settings.signed_url_seconds
        signature = sign_download(settings.token_secret, job_id, expires)
        return {
            "request_id": _rid(request),
            "job_id": job_id,
            "filename": f"framepage-{job_id}.pdf",
            "content_type": "application/pdf",
            "size_bytes": path.stat().st_size,
            "download_url": f"/api/v1/jobs/{job_id}/download?expires={expires}&signature={signature}",
            "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
        }

    @application.get("/api/v1/jobs/{job_id}/download")
    async def download_result(
        request: Request, job_id: str, expires: int, signature: str
    ) -> Response:
        if not valid_download_signature(settings.token_secret, job_id, expires, signature):
            raise APIError(401, "invalid_download_signature", "The download link is invalid or expired.")
        job = db.get_job(job_id)
        if not job or job["status"] != "completed" or not job["result_key"]:
            raise APIError(404, "result_not_found", "PDF result not found.")
        path = pipeline.store.path(job["result_key"])
        if not path.is_file():
            raise APIError(410, "result_expired", "The PDF artifact has expired.")
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"framepage-{job_id}.pdf",
        )

    @application.get("/api/v1/jobs/{job_id}/profile-proposal")
    async def get_profile_proposal(request: Request, job_id: str) -> Response:
        _job_for_request(request, job_id)
        proposal = db.get_proposal_by_job(job_id)
        if not proposal:
            return Response(status_code=204)
        return JSONResponse({"request_id": _rid(request), "proposal": proposal})

    @application.post("/api/v1/profile-proposals/{proposal_id}/apply")
    async def apply_profile_proposal(
        request: Request,
        proposal_id: str,
        body: ProposalApplyRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not idempotency_key or not (8 <= len(idempotency_key) <= 200):
            raise APIError(400, "idempotency_key_required", "Provide an Idempotency-Key of 8 to 200 characters.")
        proposal = db.get_proposal(proposal_id)
        if not proposal:
            raise APIError(404, "proposal_not_found", "Profile proposal not found.")
        _job_for_request(request, proposal["job_id"])
        data = body.model_dump()
        canonical, digest = request_digest(data)
        operation = f"apply_proposal:{proposal_id}"
        existing = db.idempotency_lookup(operation, idempotency_key)
        if existing:
            if existing["request_hash"] != digest:
                raise APIError(409, "idempotency_conflict", "This Idempotency-Key was used for a different apply request.")
            profile = db.get_profile(existing["resource_id"])
            return {"request_id": _rid(request), "proposal_id": proposal_id, "status": "applied", "profile": profile}
        if proposal["status"] == "applied":
            raise APIError(409, "proposal_already_applied", "This proposal was already applied.")
        updates = {change["field"]: change["new_value"] for change in proposal["changes"]}
        if body.action == "update":
            outcome, profile = db.update_profile(
                body.target_profile_id, body.expected_version, updates  # type: ignore[arg-type]
            )
            if outcome == "missing":
                raise APIError(404, "profile_not_found", "Target Profile not found.")
            if outcome == "default":
                raise APIError(403, "default_profile_immutable", "The Smart Default Profile cannot be modified.")
            if outcome == "conflict":
                raise APIError(409, "profile_version_conflict", "The target Profile version is stale.")
        else:
            source = db.get_profile(proposal["profile_id"]) or db.get_profile("profile_smart_default")
            values = {
                **source,
                **updates,
                "name": body.name.strip(),  # type: ignore[union-attr]
                "source_profile_id": proposal["profile_id"],
            }
            profile = db.create_profile(values)
        db.mark_proposal_applied(proposal_id, profile["id"])  # type: ignore[index]
        if not db.store_idempotency(operation, idempotency_key, digest, profile["id"]):  # type: ignore[index]
            raise APIError(409, "idempotency_conflict", "Idempotency-Key conflict.")
        return {
            "request_id": _rid(request),
            "proposal_id": proposal_id,
            "status": "applied",
            "profile": profile,
        }

    return application


app = create_app()
