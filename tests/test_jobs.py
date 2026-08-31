from __future__ import annotations

import json
import sqlite3
from io import BytesIO

from PIL import Image
from pypdf import PdfReader

from .conftest import create_demo_job


def test_job_idempotency_and_token_is_hashed(client, job_payload) -> None:
    first, auth = create_demo_job(client, job_payload, "idem-create-0001")
    replay = client.post(
        "/api/v1/jobs",
        json=job_payload,
        headers={"Idempotency-Key": "idem-create-0001"},
    )
    assert replay.status_code == 202
    assert replay.json()["job_id"] == first["job_id"]
    assert replay.json()["access_token"] == first["access_token"]
    assert replay.json()["idempotent_replay"] is True

    different = {**job_payload, "page_density": "compact"}
    conflict = client.post(
        "/api/v1/jobs",
        json=different,
        headers={"Idempotency-Key": "idem-create-0001"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    db_path = client.app.state.settings.database_path
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT token_hash, request_json FROM jobs WHERE id=?", (first["job_id"],)
        ).fetchone()
    assert row is not None
    assert row[0] != first["access_token"]
    assert first["access_token"] not in row[0]
    assert first["access_token"] not in row[1]

    assert client.get(f"/api/v1/jobs/{first['job_id']}").status_code == 401
    assert client.get(
        f"/api/v1/jobs/{first['job_id']}",
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 401
    assert client.get(f"/api/v1/jobs/{first['job_id']}", headers=auth).status_code == 200


def test_complete_demo_job_sse_pages_preview_and_pdf(client, job_payload) -> None:
    created, auth = create_demo_job(client, job_payload, "complete-demo-01")
    job_id = created["job_id"]

    status = client.get(f"/api/v1/jobs/{job_id}", headers=auth)
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert status.json()["progress"] == 1.0
    assert status.json()["page_count"] == 3
    assert status.json()["warnings"][0]["code"] == "degraded_demo_pipeline"

    events = client.get(f"/api/v1/jobs/{job_id}/events", headers=auth)
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    event_types = [
        line.removeprefix("event: ")
        for line in events.text.splitlines()
        if line.startswith("event: ")
    ]
    assert "stage" in event_types
    assert "progress" in event_types
    assert "review_round_started" in event_types
    assert "review_round_failed" in event_types
    assert "review_action" in event_types
    assert "review_round_passed" in event_types
    assert "review_stopped" in event_types
    assert event_types[-1] == "completed"
    assert events.text.index("event: review_round_failed") < events.text.index("event: review_round_passed")

    pages = client.get(f"/api/v1/jobs/{job_id}/pages", headers=auth)
    assert pages.status_code == 200
    items = pages.json()["items"]
    assert len(items) == 3
    assert items[0]["paragraphs"]
    preview = client.get(items[0]["preview_url"], headers=auth)
    assert preview.status_code == 200
    with Image.open(BytesIO(preview.content)) as image:
        assert image.size == (1280, 720)

    result = client.get(f"/api/v1/jobs/{job_id}/result", headers=auth)
    assert result.status_code == 200
    result_data = result.json()
    assert result_data["content_type"] == "application/pdf"
    assert result_data["size_bytes"] > 500
    assert "access_token" not in json.dumps(result_data)
    download = client.get(result_data["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    assert download.content.startswith(b"%PDF-")
    assert download.content.rstrip().endswith(b"%%EOF")
    pdf = PdfReader(BytesIO(download.content))
    assert len(pdf.pages) == 4  # cover + one A4 page per demo keyframe
    assert round(float(pdf.pages[0].mediabox.width)) == 595
    assert round(float(pdf.pages[0].mediabox.height)) == 842


def test_profile_proposal_apply_is_bearer_protected_and_idempotent(client, job_payload) -> None:
    created, auth = create_demo_job(client, job_payload, "proposal-demo-1")
    proposal_response = client.get(
        f"/api/v1/jobs/{created['job_id']}/profile-proposal", headers=auth
    )
    assert proposal_response.status_code == 200
    proposal = proposal_response.json()["proposal"]
    assert proposal["algorithm_vlm_agreement"] is True
    assert proposal["changes"][0]["old_value"] == 900

    apply_url = f"/api/v1/profile-proposals/{proposal['id']}/apply"
    body = {"action": "create", "name": "建议版 Profile"}
    assert client.post(
        apply_url,
        json=body,
        headers={"Idempotency-Key": "proposal-apply-01"},
    ).status_code == 401
    applied = client.post(
        apply_url,
        json=body,
        headers={**auth, "Idempotency-Key": "proposal-apply-01"},
    )
    assert applied.status_code == 200
    assert applied.json()["profile"]["stable_wait_ms"] == 1050
    replay = client.post(
        apply_url,
        json=body,
        headers={**auth, "Idempotency-Key": "proposal-apply-01"},
    )
    assert replay.status_code == 200
    assert replay.json()["profile"]["id"] == applied.json()["profile"]["id"]


def test_learning_off_has_no_proposal(client, job_payload) -> None:
    payload = {**job_payload, "learning_policy": "off"}
    created, auth = create_demo_job(client, payload, "learning-off-000")
    response = client.get(
        f"/api/v1/jobs/{created['job_id']}/profile-proposal", headers=auth
    )
    assert response.status_code == 204
    assert response.headers["X-Request-ID"].startswith("req_")


def test_api_never_echoes_openrouter_key(client, monkeypatch) -> None:
    secret = "sk-or-this-must-never-appear"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    responses = [client.get("/healthz"), client.get("/api/v1/profiles"), client.get("/openapi.json")]
    assert all(secret not in response.text for response in responses)


def test_retry_response_returns_fresh_event_cursor(client, job_payload) -> None:
    created, auth = create_demo_job(client, job_payload, "retry-cursor-demo-1")
    job_id = created["job_id"]
    client.app.state.db.update_job(
        job_id,
        status="retryable_failed",
        retryable=1,
        error_code="temporary_failure",
        error_message="temporary failure",
    )

    response = client.post(f"/api/v1/jobs/{job_id}/retry", headers=auth)

    assert response.status_code == 202
    cursor = response.json()["event_cursor"]
    assert isinstance(cursor, int) and cursor > 0
    events = client.app.state.db.list_events(job_id, cursor - 1)
    assert events[0]["id"] == cursor
    assert events[0]["event_type"] == "retry_started"
    assert events[0]["data"]["attempt"] == 2
