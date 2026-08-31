from __future__ import annotations


def test_profile_crud_duplicate_and_optimistic_lock(client) -> None:
    listing = client.get("/api/v1/profiles")
    assert listing.status_code == 200
    default = listing.json()["profiles"][0]
    assert default["id"] == "profile_smart_default"
    assert default["is_default"] is True
    assert default["review_rounds"] == 3

    created_response = client.post(
        "/api/v1/profiles",
        json={
            "name": "渐进式课件",
            "change_sensitivity": 0.62,
            "stable_wait_ms": 1100,
            "local_resample_fps": 12,
            "review_rounds": 3,
            "applicable_features": ["slow_reveal"],
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()["profile"]
    assert created_response.headers["ETag"] == '"1"'

    updated_response = client.patch(
        f"/api/v1/profiles/{created['id']}",
        json={"stable_wait_ms": 1250},
        headers={"If-Match": '"1"'},
    )
    assert updated_response.status_code == 200
    assert updated_response.json()["profile"]["version"] == 2
    assert updated_response.json()["profile"]["stable_wait_ms"] == 1250

    conflict = client.patch(
        f"/api/v1/profiles/{created['id']}",
        json={"stable_wait_ms": 1400},
        headers={"If-Match": '"1"'},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "profile_version_conflict"
    assert conflict.json()["error"]["details"]["current_version"] == 2

    duplicate = client.post(
        f"/api/v1/profiles/{created['id']}/duplicate",
        json={"name": "屏幕录制"},
    )
    assert duplicate.status_code == 201
    duplicate_data = duplicate.json()["profile"]
    assert duplicate_data["source_profile_id"] == created["id"]
    assert duplicate_data["version"] == 1

    deleted = client.delete(f"/api/v1/profiles/{duplicate_data['id']}")
    assert deleted.status_code == 204
    assert deleted.headers["X-Request-ID"].startswith("req_")


def test_default_profile_is_immutable(client) -> None:
    update = client.patch(
        "/api/v1/profiles/profile_smart_default",
        json={"stable_wait_ms": 1200},
        headers={"If-Match": '"1"'},
    )
    assert update.status_code == 403
    assert update.json()["error"]["code"] == "default_profile_immutable"
    delete = client.delete("/api/v1/profiles/profile_smart_default")
    assert delete.status_code == 403


def test_profile_patch_requires_if_match(client) -> None:
    profile = client.post("/api/v1/profiles", json={"name": "Whiteboard"}).json()["profile"]
    response = client.patch(
        f"/api/v1/profiles/{profile['id']}", json={"review_rounds": 2}
    )
    assert response.status_code == 428
    assert response.json()["error"]["code"] == "if_match_required"
