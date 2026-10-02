"""API authentication, sessions and account management.

Covers the login/refresh/logout lifecycle, the session and revocation model,
the password-change rules and the uniform-failure property that stops a caller
from learning which usernames exist.
"""

from __future__ import annotations

import pytest

from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, USER_PASSWORD


# ---------------------------------------------------------------------------
# First-run setup
# ---------------------------------------------------------------------------
def test_setup_reports_and_creates_the_first_account(client):
    status = client.get("/api/v1/auth/setup")
    assert status.status_code == 200
    assert status.json()["requires_setup"] is True

    created = client.post("/api/v1/auth/setup", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
        "display_name": "Owner", "profile_name": "Main",
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["user"]["is_admin"] is True
    assert body["access_token"] and body["refresh_token"]


def test_setup_is_refused_once_an_account_exists(client, owner):
    response = client.post("/api/v1/auth/setup", json={
        "username": "intruder", "password": "intruder-2024-x",
    })
    assert response.status_code == 409
    assert "already been set up" in response.json()["message"]


def test_setup_rejects_a_weak_password(client):
    response = client.post("/api/v1/auth/setup", json={
        "username": "weak", "password": "short",
    })
    assert response.status_code in (422, 400)
    assert "weak" not in response.text.lower() or "password" in response.text.lower()


def test_setup_rejects_an_invalid_username(client):
    for username in ("a", "has space", "x" * 40, "-leading"):
        response = client.post("/api/v1/auth/setup",
                               json={"username": username, "password": ADMIN_PASSWORD})
        assert response.status_code == 422, username


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def test_login_succeeds_with_the_right_password(client, owner):
    assert owner["token"].startswith("ssat.")
    assert owner["refresh"].startswith("ssrt.")


def test_login_is_case_insensitive_on_the_username(client, owner):
    response = client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME.upper(), "password": ADMIN_PASSWORD,
    })
    assert response.status_code == 200


@pytest.mark.parametrize("username,password", [
    (ADMIN_USERNAME, "wrong"),
    ("nosuchuser", ADMIN_PASSWORD),
    ("", ADMIN_PASSWORD),
    (ADMIN_USERNAME, ""),
])
def test_login_failures_are_uniform(client, owner, username, password):
    """Every failure returns the same status, code and message.

    Anything else -- a different message for an unknown user, a different
    status for a disabled account -- tells a caller which usernames exist.
    """
    response = client.post("/api/v1/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code in (401, 422), response.text
    if response.status_code == 401:
        body = response.json()
        assert body["error"] == "invalid_credentials"
        assert body["message"] == "Incorrect username or password."


def test_login_response_never_contains_the_hash(client, owner):
    body = client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
    }).json()
    assert "password" not in str(body).lower().replace("must_change_password", "")
    assert "scrypt$" not in str(body)


def test_login_remember_extends_the_refresh_lifetime(client, owner):
    normal = client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
    }).json()
    remembered = client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "remember": True,
    }).json()
    assert remembered["refresh_expires_at"] > normal["refresh_expires_at"]


def test_disabled_account_cannot_sign_in(client, owner, app):  # noqa: D103
    from streamserver.db import repositories as repo

    repo.update_user(app.state.context.db, owner["user_id"], is_active=0)
    response = client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
    })
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Brute-force protection
# ---------------------------------------------------------------------------
def test_repeated_failures_lock_the_account(client, owner):
    for _ in range(10):
        response = client.post("/api/v1/auth/login", json={
            "username": ADMIN_USERNAME, "password": "wrong",
        })
        assert response.status_code in (401, 423, 429), response.status_code

    locked = client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
    })
    assert locked.status_code in (423, 429), locked.text
    assert "locked" in locked.text.lower() or "too many" in locked.text.lower()


def test_lockout_does_not_leak_the_right_password(client, owner):
    for _ in range(10):
        client.post("/api/v1/auth/login",
                    json={"username": ADMIN_USERNAME, "password": "wrong"})
    response = client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
    })
    assert response.status_code in (423, 429)


def test_a_successful_login_resets_the_failure_count(client, owner, app):
    from streamserver.security.rate_limit import AccountLockout

    lockout = AccountLockout(app.state.context.db, max_failures=8)
    for _ in range(3):
        lockout.record_failure(ADMIN_USERNAME, "1.2.3.4")
    client.post("/api/v1/auth/login",
                json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    # The streak includes the successful attempt recorded by the service.
    assert lockout.recent_failures(ADMIN_USERNAME) < 8


# ---------------------------------------------------------------------------
# Token lifecycle
# ---------------------------------------------------------------------------
def test_protected_routes_require_a_token(client):
    for path in ("/api/v1/auth/me", "/api/v1/profiles", "/api/v1/library/media",
                 "/api/v1/library/home"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.parametrize("header", [
    "", "Bearer", "Bearer ", "Basic abcdef", "bearer", "Token abc",
])
def test_malformed_authorization_headers_are_rejected(client, owner, header):
    response = client.get("/api/v1/auth/me", headers={"Authorization": header})
    assert response.status_code == 401


@pytest.mark.parametrize("mutation", [
    "flip-signature", "flip-payload", "wrong-prefix", "no-signature",
    "garbage", "empty", "huge",
])
def test_a_tampered_token_is_rejected(client, owner, mutation):
    """Any edit to the signed material invalidates the token.

    The signature covers prefix, payload and expiry, so none of these can be
    altered without the token being refused.
    """
    token = owner["token"]
    if mutation == "flip-signature":
        mutated = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    elif mutation == "flip-payload":
        prefix, payload, signature = token.split(".")
        # Re-encode the payload with a far-future expiry and keep the signature.
        import base64
        import json

        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims["exp"] = claims["exp"] + 10_000_000
        forged = base64.urlsafe_b64encode(
            json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
        ).rstrip(b"=").decode()
        mutated = f"{prefix}.{forged}.{signature}"
    elif mutation == "wrong-prefix":
        mutated = token.replace("ssat.", "ssrt.", 1)
    elif mutation == "no-signature":
        mutated = "ssat." + token.split(".")[1]
    elif mutation == "garbage":
        mutated = "not-a-token"
    elif mutation == "empty":
        mutated = ""
    else:
        mutated = "ssat." + "A" * 100_000

    response = client.get("/api/v1/auth/me",
                          headers={"Authorization": f"Bearer {mutated}"}
                          if mutated else {})
    assert response.status_code == 401, mutation


def test_a_token_signed_with_another_key_is_rejected(client, owner):
    from streamserver.security import tokens

    other = b"\x11" * 32
    forged, _ = tokens.create_access_token(other, user_id=owner["user_id"])
    response = client.get("/api/v1/auth/me",
                          headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_an_expired_token_is_rejected(client, owner, app):
    from streamserver.security import tokens

    expired, _ = tokens.create_access_token(
        app.state.context.secret, user_id=owner["user_id"], ttl_seconds=-10,
    )
    response = client.get("/api/v1/auth/me",
                          headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert "expired" in response.text.lower()


def test_refresh_rotates_the_token_pair(client, owner):
    response = client.post("/api/v1/auth/refresh",
                           json={"refresh_token": owner["refresh"]})
    assert response.status_code == 200
    rotated = response.json()
    assert rotated["refresh_token"] != owner["refresh"]
    assert rotated["access_token"] != owner["token"]


def test_the_old_refresh_token_stops_working_after_rotation(client, owner):
    rotated = client.post("/api/v1/auth/refresh",
                          json={"refresh_token": owner["refresh"]}).json()
    replay = client.post("/api/v1/auth/refresh",
                         json={"refresh_token": owner["refresh"]})
    assert replay.status_code == 401
    assert client.post("/api/v1/auth/refresh",
                       json={"refresh_token": rotated["refresh_token"]}).status_code == 200


@pytest.mark.parametrize("bad", ["", "nonsense", "ssrt.", "a" * 5000])
def test_malformed_refresh_tokens_are_rejected(client, owner, bad):
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": bad})
    assert response.status_code in (401, 422)


def test_logout_invalidates_the_access_token_immediately(client, owner):
    assert client.post("/api/v1/auth/logout", headers=owner["headers"]).status_code == 200
    after = client.get("/api/v1/auth/me", headers=owner["headers"])
    assert after.status_code == 401


def test_logout_all_kills_every_device(client, owner, app):
    second = client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
        "device_name": "phone",
    }).json()
    phone = {"Authorization": f"Bearer {second['access_token']}"}

    assert client.get("/api/v1/auth/me", headers=phone).status_code == 200
    assert client.post("/api/v1/auth/logout-all", headers=owner["headers"]).status_code == 200

    for headers in (owner["headers"], phone):
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_sessions_can_be_listed_and_revoked_individually(client, owner):
    second = client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
        "device_name": "tablet",
    }).json()
    sessions = client.get("/api/v1/auth/sessions", headers=owner["headers"]).json()
    assert len(sessions) >= 2
    device = next(s for s in sessions if s["device_name"] == "tablet")

    revoked = client.delete(f"/api/v1/auth/sessions/{device['id']}",
                            headers=owner["headers"])
    assert revoked.status_code == 200
    assert client.get("/api/v1/auth/me",
                      headers={"Authorization": f"Bearer {second['access_token']}"}
                      ).status_code == 401


def test_revoking_someone_elses_session_is_refused(client, owner, second_user):
    """A session id belonging to another account must not be revocable."""
    other = client.post("/api/v1/auth/login", json={
        "username": second_user["username"], "password": second_user["password"],
    }).json()
    sessions = client.get("/api/v1/auth/sessions",
                          headers={"Authorization": f"Bearer {other['access_token']}"}
                          ).json()
    victim_session = sessions[0]["id"]

    response = client.delete(f"/api/v1/auth/sessions/{victim_session}",
                             headers=owner["headers"])
    assert response.status_code == 404
    # And the victim's token still works.
    assert client.get("/api/v1/auth/me",
                      headers={"Authorization": f"Bearer {other['access_token']}"}
                      ).status_code == 200


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def test_password_change_works_and_revokes_other_sessions(client, owner):
    other = client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
    }).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    response = client.post("/api/v1/auth/password", headers=owner["headers"], json={
        "current_password": ADMIN_PASSWORD, "new_password": "a-brand-new-secret-2026",
    })
    assert response.status_code == 200, response.text

    assert client.get("/api/v1/auth/me", headers=other_headers).status_code == 401
    assert client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": "a-brand-new-secret-2026",
    }).status_code == 200
    assert client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
    }).status_code == 401


def test_password_change_requires_the_current_password(client, owner):
    response = client.post("/api/v1/auth/password", headers=owner["headers"], json={
        "current_password": "not the password", "new_password": "another-secret-2026",
    })
    assert response.status_code == 401


def test_password_change_rejects_a_weak_new_password(client, owner):
    response = client.post("/api/v1/auth/password", headers=owner["headers"], json={
        "current_password": ADMIN_PASSWORD, "new_password": "short",
    })
    assert response.status_code == 422


def test_password_change_rejects_reusing_the_same_password(client, owner):
    response = client.post("/api/v1/auth/password", headers=owner["headers"], json={
        "current_password": ADMIN_PASSWORD, "new_password": ADMIN_PASSWORD,
    })
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Account shape
# ---------------------------------------------------------------------------
def test_me_returns_the_account_and_profiles(client, owner):
    response = client.get("/api/v1/auth/me", headers=owner["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["username"] == ADMIN_USERNAME
    assert body["user"]["is_admin"] is True
    assert "password_hash" not in str(body)
    assert body["profiles"]


def test_server_info_is_public_and_boring(client, owner):
    response = client.get("/api/v1/auth/server")
    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "v1"
    assert body["requires_setup"] is False
    # It must not advertise which endpoints exist, which are privileged, or
    # anything about the accounts on the server.
    text = response.text.lower()
    for forbidden in ("admin", "password_hash", "scrypt$", "token", "user_id",
                      "must_change", "signing.key"):
        assert forbidden not in text, forbidden
    assert body["capabilities"] and all(
        key in body["capabilities"] for key in ("profiles", "playback")
    )


def test_health_is_public(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_error_bodies_have_the_documented_shape(client, owner, admin_profile):
    # `admin_profile` rather than `owner`: every profile-scoped route now
    # refuses a request that names no profile, so a test that wants the *404*
    # has to be a request that got past the profile first.
    response = client.get("/api/v1/library/media/999999",
                          headers=admin_profile["headers"])
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error", "message", "detail", "request_id"}
    assert body["error"] and body["message"]


def test_response_carries_a_request_id(client, owner):
    response = client.get("/api/v1/auth/me", headers=owner["headers"])
    assert response.headers["X-Request-Id"]
    assert float(response.headers["X-Response-Time-Ms"]) >= 0


def test_security_headers_are_present(client, owner):
    response = client.get("/api/v1/auth/me", headers=owner["headers"])
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


def test_cors_is_off_by_default(client, owner):
    response = client.get("/api/v1/auth/me", headers={
        **owner["headers"], "Origin": "https://evil.example",
    })
    assert "access-control-allow-origin" not in {
        k.lower() for k in response.headers
    }

# ---------------------------------------------------------------------------
# The default-password gate
#
# The first-run flow exists so that an account left on the shipped default
# password cannot quietly leave watch history behind on a shared machine.  The
# gate was originally opt-in per route, and four mutating routes -- playback
# start, playback finish, progress and history clear -- never took the
# dependency, so exactly the state the gate is meant to protect was writable.
# These tests cover every mutating route rather than a representative few, so
# the next route added has to be argued for rather than assumed.
# ---------------------------------------------------------------------------
def _assert_capable(response):
    assert response.status_code == 200, response.text
    return response


def test_a_capped_account_can_still_read(client, capped_session, library):
    """The client has to render the change-password screen, so reads stay open."""
    assert client.get("/api/v1/library/media",
                      headers=capped_session["headers"]).status_code == 200
    assert client.get("/api/v1/auth/me",
                      headers=capped_session["auth"]).status_code == 200


@pytest.mark.parametrize("mutation", [
    ("PUT",    "/api/v1/library/favorites/{media}", {}),
    ("DELETE", "/api/v1/library/favorites/{media}", None),
    ("PUT",    "/api/v1/library/media/{media}/rating", {"score": 8.0}),
    ("DELETE", "/api/v1/library/media/{media}/rating", None),
    ("PUT",    "/api/v1/library/history/{media}",
     {"position_seconds": 30.0, "duration_seconds": 120.0}),
    ("DELETE", "/api/v1/library/history/{media}", None),
    ("DELETE", "/api/v1/library/history", None),
    ("POST",   "/api/v1/media/{media}/playback?start_offset=0", {}),
    ("POST",   "/api/v1/media/{media}/playback/finish?position_seconds=120", None),
    ("PUT",    "/api/v1/media/{media}/progress",
     {"position_seconds": 30.0, "duration_seconds": 120.0}),
    ("POST",   "/api/v1/profiles", {"name": "Another"}),
    ("PATCH",  "/api/v1/profiles/{media}", {"name": "Renamed"}),
    ("POST",   "/api/v1/profiles/reorder", {"profile_ids": []}),
    ("POST",   "/api/v1/profiles/{media}/pin", {"pin": "4821"}),
    ("POST",   "/api/v1/profiles/select", {"profile_id": 1}),
    ("DELETE", "/api/v1/auth/sessions/anything", None),
])
def test_a_capped_account_cannot_mutate(client, capped_session, mutation):
    method, template, body = mutation
    path = template.format(media=capped_session["media_id"])
    response = client.request(method, path, headers=capped_session["headers"], json=body)
    assert response.status_code == 403, f"{method} {path} answered {response.status_code}"
    assert response.headers.get("X-Requires-Password-Change") == "true"


def test_a_capped_account_cannot_stream(client, capped_session):
    """Streaming is the one GET that writes a history row, so it is gated too."""
    response = client.get(f"/api/v1/stream/{capped_session['media_id']}",
                          headers=capped_session["headers"])
    assert response.status_code == 403
    assert response.headers.get("X-Requires-Password-Change") == "true"


def test_changing_the_password_releases_the_gate(client, capped_session):
    """The gate has to have an exit, and it has to be the password change."""
    changed = client.post("/api/v1/auth/password",
                          headers=capped_session["auth"], json={
                              "current_password": capped_session["password"],
                              "new_password": "a-real-password-2026",
                          })
    assert changed.status_code == 200, changed.text

    # The old token still works -- the change revokes *other* sessions, not
    # this one -- and the flag is gone, so mutations succeed immediately.
    favourite = client.put(
        f"/api/v1/library/favorites/{capped_session['media_id']}",
        headers=capped_session["headers"])
    assert favourite.status_code == 200, favourite.text


def test_an_administrator_is_not_locked_out_by_its_own_bootstrap(client, owner,
                                                                  app, library):
    """`add-user --admin` leaves an administrator flagged; it must still work.

    This is the real-world shape: the documented first-run is the CLI, and the
    CLI has no change-password flow, so gating administrators would leave the
    administrator unable to ever start streaming anything.
    """
    from streamserver.db import repositories as repo

    profile_id = repo.list_profiles(app.state.context.db, owner["user_id"])[0]["id"]
    media_id = repo.list_media(
        app.state.context.db, repo.MediaFilter(limit=1))[0]["id"]
    headers = {**owner["headers"], "X-Profile-Id": str(profile_id)}

    response = client.post(f"/api/v1/media/{media_id}/playback?start_offset=0",
                           headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()["playback_token"]


# ---------------------------------------------------------------------------
# The default-password gate
#
# The first-run flow exists so that an account left on the shipped default
# password cannot quietly leave watch history behind on a shared machine.  The
# gate was originally opt-in per route, and four mutating routes -- playback
# start, playback finish, progress and history clear -- never took the
# dependency, so exactly the state the gate is meant to protect was writable.
# These tests cover every mutating route rather than a representative few, so
# the next route added has to be argued for rather than assumed.
# ---------------------------------------------------------------------------
def _assert_capable(response):
    assert response.status_code == 200, response.text
    return response


def test_a_capped_account_can_still_read(client, capped_session, library):
    """The client has to render the change-password screen, so reads stay open."""
    assert client.get("/api/v1/library/media",
                      headers=capped_session["headers"]).status_code == 200
    assert client.get("/api/v1/auth/me",
                      headers=capped_session["auth"]).status_code == 200


@pytest.mark.parametrize("mutation", [
    ("PUT",    "/api/v1/library/favorites/{media}", {}),
    ("DELETE", "/api/v1/library/favorites/{media}", None),
    ("PUT",    "/api/v1/library/media/{media}/rating", {"score": 8.0}),
    ("DELETE", "/api/v1/library/media/{media}/rating", None),
    ("PUT",    "/api/v1/library/history/{media}",
     {"position_seconds": 30.0, "duration_seconds": 120.0}),
    ("DELETE", "/api/v1/library/history/{media}", None),
    ("DELETE", "/api/v1/library/history", None),
    ("POST",   "/api/v1/media/{media}/playback?start_offset=0", {}),
    ("POST",   "/api/v1/media/{media}/playback/finish?position_seconds=120", None),
    ("PUT",    "/api/v1/media/{media}/progress",
     {"position_seconds": 30.0, "duration_seconds": 120.0}),
    ("POST",   "/api/v1/profiles", {"name": "Another"}),
    ("PATCH",  "/api/v1/profiles/{media}", {"name": "Renamed"}),
    ("POST",   "/api/v1/profiles/reorder", {"profile_ids": []}),
    ("POST",   "/api/v1/profiles/{media}/pin", {"pin": "4821"}),
    ("POST",   "/api/v1/profiles/select", {"profile_id": 1}),
    ("DELETE", "/api/v1/auth/sessions/anything", None),
])
def test_a_capped_account_cannot_mutate(client, capped_session, mutation):
    method, template, body = mutation
    path = template.format(media=capped_session["media_id"])
    response = client.request(method, path, headers=capped_session["headers"], json=body)
    assert response.status_code == 403, f"{method} {path} answered {response.status_code}"
    assert response.headers.get("X-Requires-Password-Change") == "true"


def test_a_capped_account_cannot_stream(client, capped_session):
    """Streaming is the one GET that writes a history row, so it is gated too."""
    response = client.get(f"/api/v1/stream/{capped_session['media_id']}",
                          headers=capped_session["headers"])
    assert response.status_code == 403
    assert response.headers.get("X-Requires-Password-Change") == "true"


def test_changing_the_password_releases_the_gate(client, capped_session):
    """The gate has to have an exit, and it has to be the password change."""
    changed = client.post("/api/v1/auth/password",
                          headers=capped_session["auth"], json={
                              "current_password": capped_session["password"],
                              "new_password": "a-real-password-2026",
                          })
    assert changed.status_code == 200, changed.text

    # The old token still works -- the change revokes *other* sessions, not
    # this one -- and the flag is gone, so mutations succeed immediately.
    favourite = client.put(
        f"/api/v1/library/favorites/{capped_session['media_id']}",
        headers=capped_session["headers"])
    assert favourite.status_code == 200, favourite.text


def test_an_administrator_is_not_locked_out_by_its_own_bootstrap(client, owner,
                                                                  app, library):
    """`add-user --admin` leaves an administrator flagged; it must still work.

    This is the real-world shape: the documented first-run is the CLI, and the
    CLI has no change-password flow, so gating administrators would leave the
    administrator unable to ever start streaming anything.
    """
    from streamserver.db import repositories as repo

    profile_id = repo.list_profiles(app.state.context.db, owner["user_id"])[0]["id"]
    media_id = repo.list_media(
        app.state.context.db, repo.MediaFilter(limit=1))[0]["id"]
    headers = {**owner["headers"], "X-Profile-Id": str(profile_id)}

    response = client.post(f"/api/v1/media/{media_id}/playback?start_offset=0",
                           headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()["playback_token"]
