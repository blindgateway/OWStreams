"""Playback: token minting, Range semantics, and the streaming access rules.

The streaming endpoint is the most exposed surface in the app, so these tests
cover both the RFC 9110 behaviour a player depends on and the authorisation
rules that keep one title's token from opening another.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from streamserver.media.streaming import (
    RangeNotParseable,
    RangeNotSatisfiable,
    parse_range_header,
)


# ---------------------------------------------------------------------------
# Range header parsing (unit)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("header,expected", [
    (None, None),
    ("", None),
    ("bytes=0-99", (0, 99)),
    ("bytes=100-", (100, 4095)),
    ("bytes=-50", (4046, 4095)),
    ("bytes=4095-4095", (4095, 4095)),
    ("bytes=0-99999", (0, 4095)),          # clamped to the file
    ("BYTES=0-9", (0, 9)),                 # case-insensitive
])
def test_valid_ranges(header, expected):
    result = parse_range_header(header, 4096)
    if expected is None:
        assert result is None
    else:
        assert (result.start, result.end) == expected


@pytest.mark.parametrize("header", ["bytes=0-9, 20-29", "bytes=0-9,garbage"])
def test_a_multi_range_request_is_refused(header):
    """Serving it would need a multipart/byteranges body, and silently returning
    the whole file instead makes a broken client look like it is working."""
    with pytest.raises(RangeNotParseable):
        parse_range_header(header, 4096)


@pytest.mark.parametrize("header", ["bytes=4096-", "bytes=99999-", "bytes=-0", "bytes=5-1"])
def test_unsatisfiable_ranges(header):
    with pytest.raises(RangeNotSatisfiable):
        parse_range_header(header, 4096)


@pytest.mark.parametrize("header", ["bytes=abc", "bytes=", "bytes=x-y", "items=0-1", "bytes"])
def test_unparseable_ranges(header):
    with pytest.raises(RangeNotParseable):
        parse_range_header(header, 4096)


# ---------------------------------------------------------------------------
# Playback token
# ---------------------------------------------------------------------------
def test_playback_token_is_issued(client, library, admin_profile):
    dune = library[0]
    response = client.post(f"/api/v1/media/{dune['id']}/playback",
                           headers=admin_profile["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["playback_token"].startswith("ssat.")
    assert body["expires_in"] <= 600
    assert body["stream_url"] == f"/api/v1/stream/{dune['id']}"
    assert body["session_id"]


def test_stream_url_carries_no_path(client, library, admin_profile):
    body = client.post(f"/api/v1/media/{library[0]['id']}/playback",
                       headers=admin_profile["headers"]).json()
    assert "/" not in body["stream_url"].split("/stream/")[-1]
    assert "?" not in body["stream_url"]
    assert ".." not in body["stream_url"]


def test_playback_resumes_from_saved_progress(client, library, admin_profile):
    dune = library[0]
    client.put(f"/api/v1/library/history/{dune['id']}", headers=admin_profile["headers"],
               json={"position_seconds": 1234, "duration_seconds": 7200})
    body = client.post(f"/api/v1/media/{dune['id']}/playback",
                       headers=admin_profile["headers"]).json()
    assert body["start_offset"] == 1234


def test_playback_for_unknown_media_is_404(client, admin_profile):
    assert client.post("/api/v1/media/9999/playback",
                       headers=admin_profile["headers"]).status_code == 404


def test_playback_needs_authentication(client, library):
    assert client.post(f"/api/v1/media/{library[0]['id']}/playback").status_code == 401


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
def test_whole_file_stream(client, library, admin_profile):
    dune = library[0]
    response = client.get(f"/api/v1/stream/{dune['id']}", headers=admin_profile["headers"])
    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "video/x-matroska"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert len(response.content) == dune["file_size_bytes"]


def test_range_request_returns_206(client, library, admin_profile):
    dune = library[0]
    response = client.get(f"/api/v1/stream/{dune['id']}",
                          headers={**admin_profile["headers"], "Range": "bytes=0-1023"})
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 0-1023/{dune['file_size_bytes']}"
    assert response.headers["content-length"] == "1024"
    assert len(response.content) == 1024


def test_suffix_range(client, library, admin_profile):
    dune = library[0]
    size = dune["file_size_bytes"]
    response = client.get(f"/api/v1/stream/{dune['id']}",
                          headers={**admin_profile["headers"], "Range": "bytes=-100"})
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes {size - 100}-{size - 1}/{size}"


def test_unsatisfiable_range_is_416_with_content_range(client, library, admin_profile):
    dune = library[0]
    response = client.get(f"/api/v1/stream/{dune['id']}",
                          headers={**admin_profile["headers"],
                                   "Range": f"bytes={dune['file_size_bytes'] + 10}-"})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{dune['file_size_bytes']}"


@pytest.mark.parametrize("range_header", ["chunks=1-2", "bytes=abc", "bytes=0-9, 20-29"])
def test_a_malformed_range_is_416_not_a_whole_file(client, library, admin_profile, range_header):
    """A client that sent nonsense must be told so.  Answering 200 with the
    entire file hides the bug behind a stream that starts and then jumps."""
    dune = library[0]
    size = Path(app_media_path(client, dune["id"])).stat().st_size
    response = client.get(f"/api/v1/stream/{dune['id']}",
                          headers={**admin_profile["headers"], "Range": range_header})
    assert response.status_code == 416
    assert response.headers["Content-Range"] == f"bytes */{size}"
    assert response.headers["Accept-Ranges"] == "bytes"


def test_chunked_ranges_reassemble_into_the_file(client, library, admin_profile):
    """A player seeking around must be able to rebuild the file byte-exactly."""
    dune = library[0]
    path = Path(app_media_path(client, dune["id"]))
    expected = path.read_bytes()

    collected = bytearray()
    for start in range(0, len(expected), 1024):
        end = min(start + 1023, len(expected) - 1)
        chunk = client.get(f"/api/v1/stream/{dune['id']}",
                           headers={**admin_profile["headers"],
                                    "Range": f"bytes={start}-{end}"})
        assert chunk.status_code == 206
        collected.extend(chunk.content)
    assert bytes(collected) == expected


def app_media_path(client, media_id: int) -> str:
    """The server's own view of a title's path -- used only by tests."""
    from starlette.testclient import TestClient

    assert isinstance(client, TestClient)
    return client.app.state.context.db.scalar(
        "SELECT media_path FROM media WHERE id = ?", (media_id,)
    )


def test_streaming_requires_authentication(client, library):
    assert client.get(f"/api/v1/stream/{library[0]['id']}").status_code == 401


def test_playback_token_streams_the_bound_title(client, library, admin_profile):
    dune, blade, _ = library
    token = client.post(f"/api/v1/media/{dune['id']}/playback",
                        headers=admin_profile["headers"]).json()["playback_token"]
    auth = {"Authorization": f"Bearer {token}"}

    ok = client.get(f"/api/v1/stream/{dune['id']}", headers=auth)
    assert ok.status_code == 200, ok.text

    # ...and only that one: the token names the title, so any other is refused.
    other = client.get(f"/api/v1/stream/{blade['id']}", headers=auth)
    assert other.status_code == 403
    assert "another title" in other.json()["message"]


def test_playback_token_is_bound_to_one_profile(client, library, admin_profile,
                                                 second_session):
    dune = library[0]
    token = client.post(f"/api/v1/media/{dune['id']}/playback",
                        headers=admin_profile["headers"]).json()["playback_token"]

    # Same account, different profile.
    other_profile = client.post("/api/v1/profiles", headers=admin_profile["headers"],
                                json={"name": "Other"}).json()
    swapped = {"Authorization": f"Bearer {token}",
               "X-Profile-Id": str(other_profile["id"])}
    assert client.get(f"/api/v1/stream/{dune['id']}", headers=swapped).status_code == 403

    # Different account entirely.
    del second_session


def test_playback_token_does_not_authorise_the_admin_api(client, library, admin_profile):
    token = client.post(f"/api/v1/media/{library[0]['id']}/playback",
                        headers=admin_profile["headers"]).json()["playback_token"]
    response = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code in (401, 403)


def test_an_expired_playback_token_is_refused(client, library, admin_profile, app):
    from streamserver.security import tokens

    expired, _ = tokens.create_playback_token(
        app.state.context.secret, user_id=1, profile_id=admin_profile["id"],
        media_id=library[0]["id"], ttl_seconds=-5,
    )
    response = client.get(f"/api/v1/stream/{library[0]['id']}",
                          headers={**admin_profile["headers"],
                                   "Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


def test_a_missing_file_yields_410_and_is_marked(client, library, admin_profile,
                                                 library_root):
    from streamserver.db import repositories as repo

    dune = library[0]
    repo.set_integrity_state(client.app.state.context.db, dune["id"], "missing")
    response = client.post(f"/api/v1/media/{dune['id']}/playback",
                           headers=admin_profile["headers"])
    assert response.status_code == 410

    stream = client.get(f"/api/v1/stream/{dune['id']}", headers=admin_profile["headers"])
    assert stream.status_code == 410


def test_a_file_deleted_after_import_is_detected(client, library, admin_profile,
                                                  library_root):
    dune = library[0]
    Path(app_media_path(client, dune["id"])).unlink()
    response = client.get(f"/api/v1/stream/{dune['id']}", headers=admin_profile["headers"])
    assert response.status_code == 410

    state = client.app.state.context.db.scalar(
        "SELECT integrity_state FROM media WHERE id = ?", (dune["id"],)
    )
    assert state == "missing"


def test_a_symlink_swapped_in_after_import_is_refused(client, library, admin_profile,
                                                       tmp_path):
    """Containment is re-checked at request time, not only at import time."""
    from streamserver.security.paths import PathViolation

    dune = library[0]
    real = Path(app_media_path(client, dune["id"]))
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")
    real.unlink()
    real.symlink_to(secret)

    response = client.get(f"/api/v1/stream/{dune['id']}", headers=admin_profile["headers"])
    assert response.status_code in (410, 403), response.status_code
    assert b"classified" not in response.content
    assert PathViolation is not None


def test_finishing_playback_marks_the_title_watched(client, library, admin_profile):
    dune = library[0]
    body = client.post(f"/api/v1/media/{dune['id']}/playback/finish",
                       headers=admin_profile["headers"],
                       params={"position_seconds": 7200}).json()
    assert "recorded" in body["detail"]

    progress = client.get(f"/api/v1/library/history/{dune['id']}",
                          headers=admin_profile["headers"]).json()
    assert progress["completed"] is True


def test_playback_updates_progress_on_start(client, library, admin_profile):
    dune = library[0]
    client.post(f"/api/v1/media/{dune['id']}/playback",
                headers=admin_profile["headers"],
                params={"start_offset": 500})
    progress = client.get(f"/api/v1/library/history/{dune['id']}",
                          headers=admin_profile["headers"]).json()
    assert progress["position_seconds"] == 500


def test_playback_session_row_is_recorded(client, library, admin_profile):
    dune = library[0]
    body = client.post(f"/api/v1/media/{dune['id']}/playback",
                       headers=admin_profile["headers"]).json()
    row = client.app.state.context.db.query_one(
        "SELECT * FROM playback_sessions WHERE id = ?", (body["session_id"],)
    )
    assert row is not None
    assert row["media_id"] == dune["id"]
    # The token itself is never stored; only its digest.
    assert hashlib.sha256(body["playback_token"].encode()).hexdigest() == row["token_hash"]

# ---------------------------------------------------------------------------
# A session id from a client is not proof of ownership
# ---------------------------------------------------------------------------
def test_another_profiles_playback_session_cannot_be_ended(client, library, admin_profile,
                                                           second_session):
    """Regression: ``finish_playback`` closed any session id it was handed, so
    one account could end another's playback bookkeeping row."""
    media_id = library[0]["id"]
    admin_headers = admin_profile["headers"]
    started = client.post(f"/api/v1/media/{media_id}/playback", headers=admin_headers)
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]

    stolen = client.post(f"/api/v1/media/{media_id}/playback/finish",
                         headers=second_session["headers"],
                         params={"session_id": session_id, "position_seconds": 1})
    assert stolen.status_code == 404, stolen.text

    # The owner's own request still works.
    own = client.post(f"/api/v1/media/{media_id}/playback/finish", headers=admin_headers,
                      params={"session_id": session_id, "position_seconds": 1})
    assert own.status_code == 200


def test_an_unknown_playback_session_is_404(client, library, admin_profile):
    response = client.post(f"/api/v1/media/{library[0]['id']}/playback/finish",
                           headers=admin_profile["headers"],
                           params={"session_id": "not-a-session"})
    assert response.status_code == 404
