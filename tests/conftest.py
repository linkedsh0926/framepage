from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        mode="test",
        data_dir=tmp_path / "data",
        token_secret="unit-test-token-secret",
        rate_limit_per_minute=1000,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def job_payload() -> dict:
    return {
        "youtube_url": "https://youtu.be/dQw4w9WgXcQ",
        "profile_id": "profile_smart_default",
        "profile_version": 1,
        "profile_overrides": {},
        "learning_policy": "suggest",
        "transcript_style": "polished",
        "include_timestamps": True,
        "page_density": "comfortable",
    }


def create_demo_job(client: TestClient, payload: dict, key: str = "demo-job-key-001"):
    response = client.post(
        "/api/v1/jobs",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    data = response.json()
    return data, {"Authorization": f"Bearer {data['access_token']}"}
