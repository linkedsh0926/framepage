from __future__ import annotations

import pytest

from app.youtube import InvalidYouTubeURL, parse_youtube_url


@pytest.mark.parametrize(
    ("url", "video_id"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=30", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ],
)
def test_supported_youtube_urls(url: str, video_id: str) -> None:
    parsed = parse_youtube_url(url)
    assert parsed.video_id == video_id
    assert parsed.canonical_url == f"https://www.youtube.com/watch?v={video_id}"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
        "https://user:password@youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com:8443/watch?v=dQw4w9WgXcQ",
        "https://youtube.com:notaport/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=too-short",
        "https://youtube.com/watch?v=dQw4w9WgXcQ&list=PL123",
        "https://youtube.com/playlist?list=PL123",
        "https://youtu.be/dQw4w9WgXcQ/extra",
        "file:///etc/passwd",
    ],
)
def test_rejects_unsafe_or_unsupported_urls(url: str) -> None:
    with pytest.raises(InvalidYouTubeURL):
        parse_youtube_url(url)


def test_inspect_is_deterministic_and_has_request_id(client) -> None:
    first = client.post(
        "/api/v1/videos/inspect",
        json={"youtube_url": "https://youtu.be/dQw4w9WgXcQ"},
    )
    second = client.post(
        "/api/v1/videos/inspect",
        json={"youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["video"] == second.json()["video"]
    assert first.json()["estimate"] == second.json()["estimate"]
    assert first.json()["request_id"].startswith("req_")
    assert first.headers["X-Request-ID"] == first.json()["request_id"]


def test_inspect_rejects_non_youtube_host(client) -> None:
    response = client.post(
        "/api/v1/videos/inspect",
        json={"youtube_url": "https://example.com/watch?v=dQw4w9WgXcQ"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_youtube_url"
