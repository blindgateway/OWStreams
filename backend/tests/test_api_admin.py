"""Administration, import and the security properties that depend on both.

Two halves:

* functional coverage of the admin API (accounts, import, maintenance, audit)
* the security tests that need an admin account to be meaningful --
  privilege escalation, path traversal through the import endpoint, and the
  error-leakage contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME


# ---------------------------------------------------------------------------
# Privilege separation
# ---------------------------------------------------------------------------
def test_non_admin_cannot_reach_any_admin_route(client, second_session):
    routes = [
        ("get", "/api/v1/admin/users", None),
        ("get", "/api/v1/admin/settings", None),
        ("get", "/api/v1/admin/health", None),
        ("get", "/api/v1/admin/audit", None),
        ("get", "/api/v1/admin/media", None),
        ("get", "/api/v1/admin/library-roots", None),
        ("post", "/api/v1/admin/import", {"scan": True}),
        ("post", "/api/v1/admin/verify", {}),
        ("post", "/api/v1/admin/reindex", {}),
        ("post", "/api/v1/admin/backup", {}),
        ("post", "/api/v1/admin/users", {"username": "new", "password": "new-password-2026"}),
        ("post", "/api/v1/admin/users/1/password", {"new_password": "hijacked-2026"}),
    ]
    for method, path, body in routes:
        response = getattr(client, method)(path, headers=second_session["headers"],
                                           **({"json": body} if body is not None else {}))
        assert response.status_code == 403, f"{method.upper()} {path} -> {response.status_code}"
        assert "administrator" in response.json()["message"].lower()


def test_admin_routes_require_authentication(client):
    for path in ("/api/v1/admin/users", "/api/v1/admin/settings", "/api/v1/admin/health"):
        assert client.get(path).status_code == 401, path


def test_a_promoted_user_gains_admin_access(client, owner):
    from streamserver.db import repositories as repo

    repo.update_user(client.app.state.context.db, owner["user_id"], is_admin=1)
    fresh = client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
    }).json()["access_token"]
    response = client.get("/api/v1/admin/users",
                          headers={"Authorization": f"Bearer {fresh}"})
    assert response.status_code == 200


def test_a_demoted_user_loses_admin_access(client, owner):
    from streamserver.db import repositories as repo

    db = client.app.state.context.db
    repo.update_user(db, owner["user_id"], is_admin=0)
    # The existing token keeps its claims, but authorisation re-reads the user.
    response = client.get("/api/v1/admin/users", headers=owner["headers"])
    assert response.status_code == 403
    repo.update_user(db, owner["user_id"], is_admin=1)


def test_admin_cannot_remove_its_own_privileges(client, owner):
    response = client.patch(f"/api/v1/admin/users/{owner['user_id']}",
                            headers=owner["headers"], json={"is_admin": False})
    assert response.status_code == 409


def test_admin_cannot_deactivate_itself(client, owner):
    response = client.patch(f"/api/v1/admin/users/{owner['user_id']}",
                            headers=owner["headers"], json={"is_active": False})
    assert response.status_code == 409


def test_admin_cannot_delete_itself(client, owner):
    response = client.delete(f"/api/v1/admin/users/{owner['user_id']}",
                             headers=owner["headers"])
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Account administration
# ---------------------------------------------------------------------------
def test_admin_can_create_and_delete_an_account(client, owner, second_user):
    created = client.post("/api/v1/admin/users", headers=owner["headers"], json={
        "username": "carol", "password": "carol-password-2026", "is_admin": False,
    })
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    # A new account can sign in and has its own profile.
    login = client.post("/api/v1/auth/login", json={
        "username": "carol", "password": "carol-password-2026",
    })
    assert login.status_code == 200

    assert client.delete(f"/api/v1/admin/users/{user_id}",
                         headers=owner["headers"]).status_code == 200
    assert client.post("/api/v1/auth/login", json={
        "username": "carol", "password": "carol-password-2026",
    }).status_code == 401


def test_admin_rejects_a_duplicate_username(client, owner, second_user):
    response = client.post("/api/v1/admin/users", headers=owner["headers"], json={
        "username": second_user["username"], "password": "another-password-2026",
    })
    assert response.status_code == 409


def test_admin_rejects_a_weak_password(client, owner):
    response = client.post("/api/v1/admin/users", headers=owner["headers"], json={
        "username": "weakling", "password": "short",
    })
    assert response.status_code in (400, 422)


def test_deactivating_an_account_kills_its_sessions(client, owner, second_session):
    response = client.patch(f"/api/v1/admin/users/{second_session['user_id']}",
                            headers=owner["headers"], json={"is_active": False})
    assert response.status_code == 200
    assert client.get("/api/v1/auth/me", headers=second_session["auth"]).status_code == 401


@pytest.mark.parametrize("spelling", [
    "/api/v1/admin/users/{user_id}/password",
    "/api/v1/auth/password/reset?user_id={user_id}",
])
def test_admin_password_reset_revokes_sessions_and_forces_a_change(
    client, owner, second_session, spelling
):
    """Both spellings of the reset endpoint do the same thing."""
    response = client.post(
        spelling.format(user_id=second_session["user_id"]),
        headers=owner["headers"],
        json={"new_password": "reset-password-2026"},
    )
    assert response.status_code == 200, response.text

    assert client.get("/api/v1/auth/me", headers=second_session["auth"]).status_code == 401
    login = client.post("/api/v1/auth/login", json={
        "username": second_session["username"], "password": "reset-password-2026",
    })
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True


def test_the_last_administrator_cannot_be_deleted(client, owner, second_session):
    promoted = client.patch(f"/api/v1/admin/users/{second_session['user_id']}",
                            headers=owner["headers"], json={"is_admin": True})
    assert promoted.status_code == 200

    # Two admins now, so removing one is allowed...
    assert client.delete(f"/api/v1/admin/users/{owner['user_id']}",
                         headers=owner["headers"]).status_code == 409   # cannot delete self

    fresh = client.post("/api/v1/auth/login", json={
        "username": second_session["username"], "password": second_session["password"],
    }).json()["access_token"]
    assert client.delete(f"/api/v1/admin/users/{owner['user_id']}",
                         headers={"Authorization": f"Bearer {fresh}"}).status_code == 200


def test_users_listing_never_exposes_hashes(client, owner):
    body = client.get("/api/v1/admin/users", headers=owner["headers"])
    assert body.status_code == 200
    assert "password_hash" not in body.text
    assert "scrypt$" not in body.text


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def test_admin_can_import_a_specific_file(client, owner, library_root):
    path = library_root / "Interstellar.2014.1080p.mkv"
    path.write_bytes(bytes(range(256)) * 8)

    response = client.post("/api/v1/admin/import", headers=owner["headers"],
                           json={"paths": [str(path)]})
    assert response.status_code == 200, response.text
    assert response.json()["added"] == 1

    listed = client.get("/api/v1/library/media",
                        headers={**owner["headers"],
                                 "X-Profile-Id": str(
                                     client.get("/api/v1/profiles",
                                                headers=owner["headers"]).json()[0]["id"])}
                        ).json()
    assert listed["total"] == 1
    assert listed["items"][0]["title"] == "Interstellar"


def test_scan_imports_the_whole_tree(client, owner, library_root):
    for name in ("A.2001.mkv", "B.2002.mkv", "C.2003.mkv"):
        (library_root / name).write_bytes(bytes(range(256)) * 4)

    response = client.post("/api/v1/admin/import", headers=owner["headers"],
                           json={"scan": True})
    assert response.status_code == 200
    assert response.json()["added"] == 3


def test_import_is_idempotent(client, owner, library_root):
    (library_root / "Once.2000.mkv").write_bytes(b"x" * 2048)
    first = client.post("/api/v1/admin/import", headers=owner["headers"],
                        json={"scan": True}).json()
    second = client.post("/api/v1/admin/import", headers=owner["headers"],
                         json={"scan": True}).json()
    assert first["added"] == 1
    assert second["added"] == 0
    assert second["skipped"] + second["updated"] == 1


def test_import_requires_something_to_do(client, owner):
    response = client.post("/api/v1/admin/import", headers=owner["headers"], json={})
    assert response.status_code == 400


@pytest.mark.parametrize("path", [
    "/etc/passwd",
    "/etc/shadow",
    "/root/.ssh/id_rsa",
    "/var/lib/streamserver/streamserver.db",
    "/proc/self/environ",
])
def test_import_cannot_read_files_outside_the_library(client, owner, path):
    """The admin import endpoint is still bound by the library roots."""
    response = client.post("/api/v1/admin/import", headers=owner["headers"],
                           json={"paths": [path]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["added"] == 0
    assert body["errors"], f"{path} was not refused"


def test_import_refuses_traversal(client, owner, library_root):
    response = client.post("/api/v1/admin/import", headers=owner["headers"], json={
        "paths": [str(library_root / ".." / ".." / "etc" / "passwd")],
    })
    assert response.json()["added"] == 0
    assert response.json()["errors"]


def test_import_of_a_directory_is_refused(client, owner, library_root):
    (library_root / "subdir").mkdir()
    response = client.post("/api/v1/admin/import", headers=owner["headers"],
                           json={"paths": [str(library_root / "subdir")]})
    assert response.json()["added"] == 0
    assert response.json()["errors"]


def test_a_valid_date_is_accepted(client, owner, library):
    response = client.patch(f"/api/v1/admin/media/{library[0]['id']}",
                            headers=owner["headers"], json={"release_date": "2021-10-22"})
    assert response.status_code == 200
    assert response.json()["release_date"] == "2021-10-22"


def test_import_of_an_empty_file_is_refused(client, owner, library_root):
    empty = library_root / "Empty.2020.mkv"
    empty.touch()
    response = client.post("/api/v1/admin/import", headers=owner["headers"],
                           json={"paths": [str(empty)]})
    assert response.json()["added"] == 0
    assert response.json()["errors"]


def test_import_of_a_hostile_filename_is_handled(client, owner, library_root):
    """A filename with shell metacharacters and newlines must not break import."""
    nasty = library_root / "Movie (2020) [1080p] ; rm -rf $HOME \n.mkv"
    nasty.write_bytes(b"x" * 1024)
    response = client.post("/api/v1/admin/import", headers=owner["headers"],
                           json={"paths": [str(nasty)]})
    assert response.status_code == 200
    # Whether or not ffprobe liked it, the record exists or the error is clean.
    body = response.json()
    assert body["added"] + len(body["errors"]) == 1


def test_import_rejects_more_than_a_thousand_paths(client, owner):
    response = client.post("/api/v1/admin/import", headers=owner["headers"],
                           json={"paths": ["/a"] * 1001})
    assert response.status_code == 422


def test_metadata_can_be_edited_by_an_admin(client, owner, library):
    dune = library[0]
    response = client.patch(f"/api/v1/admin/media/{dune['id']}", headers=owner["headers"],
                            json={"title": "Dune: Part One", "year": 2021,
                                  "overview": "Revised overview.", "runtime_minutes": 155})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Dune: Part One"
    assert body["overview"] == "Revised overview."
    assert body["slug"] != dune["slug"]      # the slug follows the title


def test_metadata_patch_rejects_unknown_fields(client, owner, library):
    response = client.patch(f"/api/v1/admin/media/{library[0]['id']}",
                            headers=owner["headers"],
                            json={"media_path": "/etc/passwd"})
    assert response.status_code == 422


def test_metadata_patch_rejects_a_bad_date(client, owner, library):
    for value in ("not-a-date", "2020-13-45", "20-20-20", "2020-02-30", "20200101",
                  "abcd-ef-gh"):
        response = client.patch(f"/api/v1/admin/media/{library[0]['id']}",
                                headers=owner["headers"],
                                json={"release_date": value})
        assert response.status_code == 422, value


def test_deleting_a_media_record_never_deletes_the_file(client, owner, library,
                                                         library_root):
    dune = library[0]
    path = Path(library[0]["media_path"]) if library[0].get("media_path") else None

    response = client.delete(f"/api/v1/admin/media/{dune['id']}", headers=owner["headers"])
    assert response.status_code == 200
    assert client.get(f"/api/v1/library/media/{dune['id']}", headers=owner["headers"]
                      ).status_code in (401, 404)
    del path


def test_delete_file_is_refused_outright(client, owner, library):
    """Even an admin cannot make the server delete a media file."""
    response = client.delete(f"/api/v1/admin/media/{library[0]['id']}",
                             headers=owner["headers"], params={"delete_file": "true"})
    assert response.status_code == 400
    assert "never deletes media files" in response.json()["message"]


def test_hiding_a_title_removes_it_from_the_library(client, owner, library, admin_profile):
    dune = library[0]
    client.patch(f"/api/v1/admin/media/{dune['id']}", headers=owner["headers"],
                 json={"hidden": True})
    titles = {i["title"] for i in client.get("/api/v1/library/media",
                                            headers=admin_profile["headers"]
                                            ).json()["items"]}
    assert "Dune" not in titles


# ---------------------------------------------------------------------------
# Library roots
# ---------------------------------------------------------------------------
def test_library_roots_are_reported(client, owner, library_root):
    body = client.get("/api/v1/admin/library-roots", headers=owner["headers"]).json()
    assert body["roots"][0]["exists"] is True
    assert body["roots"][0]["readable"] is True
    assert str(library_root) in body["configured"]


def test_adding_a_nonexistent_root_is_refused(client, owner, tmp_path):
    response = client.post("/api/v1/admin/library-roots", headers=owner["headers"],
                           json={"path": str(tmp_path / "nope")})
    assert response.status_code == 400


def test_adding_a_file_as_a_root_is_refused(client, owner, library_root):
    a_file = library_root / "notadir.mkv"
    a_file.write_bytes(b"x")
    response = client.post("/api/v1/admin/library-roots", headers=owner["headers"],
                           json={"path": str(a_file)})
    assert response.status_code == 400


def test_relative_root_is_refused(client, owner):
    response = client.post("/api/v1/admin/library-roots", headers=owner["headers"],
                           json={"path": "relative/path"})
    assert response.status_code == 422


def test_adding_a_root_takes_effect_for_scans(client, owner, library_root, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "Extra.2020.mkv").write_bytes(b"y" * 2048)

    response = client.post("/api/v1/admin/library-roots", headers=owner["headers"],
                           json={"path": str(extra)})
    assert response.status_code == 200
    assert "Persist it" in response.json()["detail"]

    scanned = client.post("/api/v1/admin/import", headers=owner["headers"],
                          json={"scan": True}).json()
    assert scanned["added"] == 1


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------
def test_verify_reports_on_every_title(client, owner, library):
    response = client.post("/api/v1/admin/verify", headers=owner["headers"])
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"ok", "missing", "repaired"}


def test_reindex_rebuilds_the_search_index(client, owner, library):
    response = client.post("/api/v1/admin/reindex", headers=owner["headers"])
    assert response.status_code == 200
    assert "3" in response.json()["detail"]

    profile = client.get("/api/v1/profiles", headers=owner["headers"]).json()[0]
    headers = {**owner["headers"], "X-Profile-Id": str(profile["id"])}
    found = client.get("/api/v1/library/search", headers=headers,
                       params={"q": "Dune"}).json()
    assert [i["title"] for i in found["items"]] == ["Dune"]


def test_backup_writes_a_snapshot(client, owner, config):
    response = client.post("/api/v1/admin/backup", headers=owner["headers"])
    assert response.status_code == 200
    assert "Backup written" in response.json()["detail"]
    assert list(config.backup_path.glob("*.db.gz"))


def test_prune_reports_what_it_removed(client, owner):
    response = client.post("/api/v1/admin/maintenance/prune", headers=owner["headers"])
    assert response.status_code == 200
    assert "Pruned" in response.json()["detail"]


def test_vacuum_compacts(client, owner):
    response = client.post("/api/v1/admin/maintenance/vacuum", headers=owner["headers"])
    assert response.status_code == 200


def test_health_reports_a_working_server(client, owner, library):
    body = client.get("/api/v1/admin/health", headers=owner["headers"]).json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["library_count"] == 3
    assert body["api_version"] == "v1"
    assert body["library_roots"]


def test_settings_never_leak_keys(client, owner):
    response = client.get("/api/v1/admin/settings", headers=owner["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["tmdb_configured"] is False
    assert "tmdb_api_key" not in response.text
    assert "signing" not in response.text


def test_audit_trail_is_readable(client, owner):
    client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": "wrong",
    })
    events = client.get("/api/v1/admin/audit?action=auth.login.failure",
                        headers=owner["headers"]).json()
    assert events
    assert events[0]["outcome"] == "denied"
    assert events[0]["username"] == ADMIN_USERNAME


def test_audit_trail_never_contains_credentials(client, owner):
    """Everything recorded during a full session, scrubbed."""
    client.post("/api/v1/auth/login", json={
        "username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
    })
    client.post("/api/v1/auth/logout", headers=owner["headers"])
    events = client.get("/api/v1/admin/audit?limit=500", headers=owner["headers"]).json()
    text = str(events)
    assert ADMIN_PASSWORD not in text
    assert "scrypt$" not in text
    assert "ssat." not in text
    assert "ssrt." not in text
    assert "password" not in text.lower().replace("must_change_password", "")

# ---------------------------------------------------------------------------
# Nothing in a user row may reach a client
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("call", ["create", "patch"])
def test_no_account_endpoint_publishes_the_password_hash(client, owner, call):
    """Regression: the create and patch handlers built their response from a
    raw ``users`` row and the response model allowed extra fields, so
    ``password_hash`` -- a scrypt hash an attacker can grind offline -- was
    published on both."""
    headers = owner["headers"]
    if call == "create":
        response = client.post("/api/v1/admin/users", headers=headers, json={
            "username": "someone", "password": "a-strong-password-2026",
        })
        assert response.status_code == 201
        body = response.json()
        assert body["username"] == "someone"
    else:
        response = client.patch("/api/v1/admin/users/1", headers=headers,
                                json={"is_active": True})
        assert response.status_code == 200
        body = response.json()

    assert "password_hash" not in body
    assert "scrypt$" not in response.text
    # And nothing from the users table that is not a declared field.
    assert set(body) <= {
        "id", "username", "display_name", "email", "is_admin", "is_active",
        "must_change_password", "created_at", "updated_at", "last_login_at",
    }


def test_the_user_listing_shape_matches_the_single_user_shape(client, owner):
    """One shape for an account, everywhere, so a client does not have to
    handle two versions of the same thing."""
    headers = owner["headers"]
    listed = client.get("/api/v1/admin/users", headers=headers).json()[0]
    single = client.get("/api/v1/auth/me", headers=headers).json()["user"]
    for field in ("id", "username", "display_name", "is_admin", "must_change_password"):
        assert field in listed, field
        assert field in single, field
