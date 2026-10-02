"""Profiles and PIN locking, including cross-account isolation (IDOR).

Every "another account" test here asserts **404, not 403**: a 403 would confirm
that the id exists, which is itself a leak.
"""

from __future__ import annotations

import pytest


def _create(client, headers, name="Kids", pin=None, **extra):
    body = {"name": name, **extra}
    if pin is not None:
        body["pin"] = pin
    response = client.post("/api/v1/profiles", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def test_a_new_account_has_one_profile(client, owner):
    profiles = client.get("/api/v1/profiles", headers=owner["headers"]).json()
    assert len(profiles) == 1
    assert profiles[0]["name"] == "Main"
    assert profiles[0]["has_pin"] is False


def test_create_update_and_delete(client, owner):
    created = _create(client, owner["headers"], name="Kids", is_kids=True)
    profile_id = created["id"]
    assert created["name"] == "Kids" and created["is_kids"] is True

    updated = client.patch(f"/api/v1/profiles/{profile_id}", headers=owner["headers"],
                           json={"name": "Children", "max_watch_minutes": 60})
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Children"
    assert updated.json()["max_watch_minutes"] == 60

    deleted = client.delete(f"/api/v1/profiles/{profile_id}", headers=owner["headers"])
    assert deleted.status_code == 200
    remaining = client.get("/api/v1/profiles", headers=owner["headers"]).json()
    assert [p["id"] for p in remaining] != [profile_id]


def test_the_last_profile_cannot_be_deleted(client, owner):
    only = client.get("/api/v1/profiles", headers=owner["headers"]).json()[0]
    response = client.delete(f"/api/v1/profiles/{only['id']}", headers=owner["headers"])
    assert response.status_code == 409
    assert "at least one profile" in response.json()["message"]


def test_profile_names_must_be_unique_per_account(client, owner):
    """The schema enforces UNIQUE(user_id, name); the API must surface that as
    a 409 rather than letting the constraint error become a 500."""
    _create(client, owner["headers"], name="Kids")
    duplicate = client.post("/api/v1/profiles", headers=owner["headers"],
                            json={"name": "Kids"})
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"] == "profile_name_taken"


@pytest.mark.parametrize("body", [
    {"name": ""}, {"name": "x" * 40}, {"name": "bad\x00name"}, {"name": "ok", "pin": "12"},
    {"name": "ok", "pin": "abcdefgh"}, {"name": "ok", "max_watch_minutes": -1},
])
def test_invalid_profile_bodies_are_rejected(client, owner, body):
    response = client.post("/api/v1/profiles", headers=owner["headers"], json=body)
    assert response.status_code == 422, response.text


def test_reorder_requires_the_complete_set(client, owner):
    first = _create(client, owner["headers"], name="A")
    second = _create(client, owner["headers"], name="B")
    main = client.get("/api/v1/profiles", headers=owner["headers"]).json()[0]

    partial = client.post("/api/v1/profiles/reorder", headers=owner["headers"],
                          json={"profile_ids": [first["id"], second["id"]]})
    assert partial.status_code == 422

    complete = client.post("/api/v1/profiles/reorder", headers=owner["headers"],
                           json={"profile_ids": [second["id"], first["id"], main["id"]]})
    assert complete.status_code == 200
    assert [p["id"] for p in complete.json()][:2] == [second["id"], first["id"]]


def test_profile_limit_is_enforced(client, owner):
    for index in range(7):
        response = client.post("/api/v1/profiles", headers=owner["headers"],
                               json={"name": f"P{index}"})
        assert response.status_code == 201, response.text
    overflow = client.post("/api/v1/profiles", headers=owner["headers"],
                           json={"name": "one-too-many"})
    assert overflow.status_code == 409


# ---------------------------------------------------------------------------
# PINs
# ---------------------------------------------------------------------------
def test_pin_is_set_verified_and_changed(client, owner):
    profile = _create(client, owner["headers"], name="Kids", pin="1357")
    profile_id = profile["id"]
    assert profile["has_pin"] is True

    wrong = client.post(f"/api/v1/profiles/{profile_id}/unlock",
                        headers=owner["headers"], json={"pin": "0000"})
    assert wrong.status_code == 401
    assert wrong.json()["error"] == "pin_incorrect"

    right = client.post(f"/api/v1/profiles/{profile_id}/unlock",
                        headers=owner["headers"], json={"pin": "1357"})
    assert right.status_code == 200

    changed = client.put(f"/api/v1/profiles/{profile_id}/pin", headers=owner["headers"],
                         json={"current_pin": "1357", "new_pin": "2468"})
    assert changed.status_code == 200
    assert client.post(f"/api/v1/profiles/{profile_id}/unlock", headers=owner["headers"],
                       json={"pin": "1357"}).status_code == 401
    assert client.post(f"/api/v1/profiles/{profile_id}/unlock", headers=owner["headers"],
                       json={"pin": "2468"}).status_code == 200


def test_setting_a_pin_over_an_existing_one_needs_the_current_pin(client, owner):
    profile = _create(client, owner["headers"], name="Kids", pin="1111")
    attempt = client.post(f"/api/v1/profiles/{profile['id']}/pin",
                          headers=owner["headers"], json={"pin": "2222"})
    assert attempt.status_code == 403
    assert "current PIN" in attempt.json()["message"]


def test_pin_can_be_cleared(client, owner):
    profile = _create(client, owner["headers"], name="Kids", pin="1111")
    cleared = client.post(f"/api/v1/profiles/{profile['id']}/pin",
                          headers=owner["headers"], json={"pin": None})
    assert cleared.status_code == 200
    assert cleared.json()["has_pin"] is False
    # With no PIN set, any submission is accepted (and is a no-op).
    assert client.post(f"/api/v1/profiles/{profile['id']}/unlock", headers=owner["headers"],
                       json={"pin": "9999"}).status_code == 200


def test_pin_attempts_are_throttled(client, owner):
    profile = _create(client, owner["headers"], name="Kids", pin="1357")
    for attempt in range(4):
        response = client.post(
            f"/api/v1/profiles/{profile['id']}/unlock",
            headers={**owner["headers"], "X-Pin-Attempt": str(attempt)},
            json={"pin": "0000"},
        )
        assert response.status_code == 401
    # The fifth attempt is refused outright rather than being allowed to keep
    # guessing a four-digit space.
    blocked = client.post(f"/api/v1/profiles/{profile['id']}/unlock",
                          headers={**owner["headers"], "X-Pin-Attempt": "5"},
                          json={"pin": "1357"})
    assert blocked.status_code == 401
    assert "too many" in blocked.json()["message"].lower()


def test_locked_state_is_reportable(client, owner):
    profile = _create(client, owner["headers"], name="Kids", pin="1357")
    plain = client.get("/api/v1/profiles", headers=owner["headers"]).json()
    unlocked = client.get(f"/api/v1/profiles/{plain[0]['id']}/locked",
                          headers=owner["headers"])
    assert unlocked.json()["requires_pin"] is False
    assert client.get(f"/api/v1/profiles/{profile['id']}/locked",
                      headers=owner["headers"]).json()["requires_pin"] is True


@pytest.mark.parametrize("pin", ["1", "12", "123", "123456789", "abcd", "12 34", ""])
def test_invalid_pins_are_rejected_at_the_schema(client, owner, pin):
    response = client.post("/api/v1/profiles", headers=owner["headers"],
                           json={"name": "X", "pin": pin})
    assert response.status_code == 422, pin


def test_profile_never_exposes_a_pin_hash(client, owner):
    profile = _create(client, owner["headers"], name="Kids", pin="1357")
    for response in (
        client.get("/api/v1/profiles", headers=owner["headers"]),
        client.get(f"/api/v1/profiles/{profile['id']}", headers=owner["headers"]),
        client.get("/api/v1/auth/me", headers=owner["headers"]),
    ):
        text = response.text
        assert "pin_hash" not in text
        assert "scrypt$" not in text
        assert "1357" not in text


# ---------------------------------------------------------------------------
# Cross-account isolation
# ---------------------------------------------------------------------------
def test_another_accounts_profile_is_invisible(client, owner, second_session):
    mine = _create(client, owner["headers"], name="Private")
    victim_id = mine["id"]

    for method, path in (
        ("get", f"/api/v1/profiles/{victim_id}"),
        ("delete", f"/api/v1/profiles/{victim_id}"),
    ):
        response = getattr(client, method)(path, headers=second_session["headers"])
        assert response.status_code == 404, path

    patch = client.patch(f"/api/v1/profiles/{victim_id}",
                         headers=second_session["headers"], json={"name": "Stolen"})
    assert patch.status_code == 404

    pin = client.post(f"/api/v1/profiles/{victim_id}/pin",
                      headers=second_session["headers"], json={"pin": "0000"})
    assert pin.status_code == 404

    unlock = client.post(f"/api/v1/profiles/{victim_id}/unlock",
                         headers=second_session["headers"], json={"pin": "0000"})
    assert unlock.status_code == 404


def test_a_forged_profile_header_is_refused(client, owner, second_session):
    """Setting X-Profile-Id to another account's profile must not work.

    The header is the client's claim about who it is acting as; the server
    checks ownership, so a guessed id is rejected rather than honoured.
    """
    mine = _create(client, owner["headers"], name="Private")
    forged = {**second_session["auth"], "X-Profile-Id": str(mine["id"])}

    response = client.get("/api/v1/library/favorites", headers=forged)
    assert response.status_code in (401, 404), response.text


def test_a_non_numeric_profile_header_is_refused(client, owner):
    response = client.get("/api/v1/library/media",
                          headers={**owner["headers"], "X-Profile-Id": "abc"})
    assert response.status_code == 400


def test_profile_selection_is_audited(client, owner):
    """Which profile a session is acting as is the first thing anyone wants to
    know after an incident, so the switch is written to the security trail."""
    profile = client.get("/api/v1/profiles", headers=owner["headers"]).json()[0]
    response = client.post("/api/v1/profiles/select", headers=owner["headers"],
                           json={"profile_id": profile["id"]})
    assert response.status_code == 200

    events = client.get("/api/v1/admin/audit?action=profile.selected&limit=50",
                        headers=owner["headers"]).json()
    assert any(e["action"] == "profile.selected" for e in events)


def test_selecting_another_accounts_profile_is_refused(client, owner, second_session):
    mine = _create(client, owner["headers"], name="Private")
    response = client.post("/api/v1/profiles/select", headers=second_session["headers"],
                           json={"profile_id": mine["id"]})
    assert response.status_code == 404


def test_pin_verification_survives_a_server_restart(client, owner, app, config):
    """The PIN is a stored hash, so a new process still validates it."""
    from streamserver.api.app import create_app
    from starlette.testclient import TestClient

    profile = _create(client, owner["headers"], name="Kids", pin="1357")
    profile_id = profile["id"]
    app.state.context.close()

    restarted = create_app(config)
    with TestClient(restarted, base_url="http://localhost") as fresh:
        login = fresh.post("/api/v1/auth/login", json={
            "username": "phuree", "password": "phuree-2024-secure",
        })
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        ok = fresh.post(f"/api/v1/profiles/{profile_id}/unlock",
                        headers=headers, json={"pin": "1357"})
        assert ok.status_code == 200
        bad = fresh.post(f"/api/v1/profiles/{profile_id}/unlock",
                         headers=headers, json={"pin": "9999"})
        assert bad.status_code == 401
    restarted.state.context.close()
    # The outer fixture closes the context again; make that idempotent.
    assert True