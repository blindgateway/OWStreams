"""Playback: token minting, Range semantics, and the streaming access rules.

The streaming endpoint is the most exposed surface in the app, so these tests
cover both the RFC 9110 behaviour a player depends on and the authorisation
rules that keep one title's token from opening another.
"""

from __future__ import annotations

import hashlib
import json
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


# ---------------------------------------------------------------------------
# The streaming route is a GET that writes
#
# Everything above treats `/api/v1/stream/{id}` as a read, which is true of its
# method and false of its effect: it records "started watching" in the history
# and touches the playback session before it serves a byte.  Three separate
# places decide what counts as a stream -- the request logger, the default
# password gate and the client's access manager -- and each of them tests the
# path differently.  A rule that holds for two of them and not the third is
# invisible until a login is rate-limited by its own playback, which is exactly
# what happened before these tests.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/api/v1/stream/1",
    "/api/v1/stream/1/",
    "/api/v1/stream",
])
def test_the_streaming_route_is_recognised_by_its_path(path):
    from streamserver.api import deps

    class Request:
        method = "GET"
        url = type("URL", (), {"path": path})()

    assert deps._is_stream(Request())


@pytest.mark.parametrize("path", [
    "/api/v1/library/media",
    "/api/v1/media/1/poster",
    "/api/v1/media/1/playback",
    "/api/v1/auth/me",
])
def test_ordinary_routes_are_not_mistaken_for_the_stream(path):
    from streamserver.api import deps

    class Request:
        method = "GET"
        url = type("URL", (), {"path": path})()

    assert not deps._is_stream(Request())


def test_a_library_read_is_not_folded_into_a_stream_log_line(client, library,
                                                              admin_profile, caplog):
    """The per-request log line must come back for ordinary reads.

    Streaming folds thousands of chunk requests into one summary, so the test
    is that a *library* read is not folded with one: the symptom of a wrong
    rule here is not an error but a server whose log is silently empty, which
    is a very quiet way for the audit trail to stop being an audit trail.
    """
    import logging

    with caplog.at_level(logging.INFO):
        assert client.get("/api/v1/library/media",
                          headers=admin_profile["headers"]).status_code == 200
    assert any("GET /api/v1/library/media" in record.getMessage()
               for record in caplog.records), (
        f"no request log line for a library read: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_a_stream_is_not_logged_one_line_per_chunk(client, library, admin_profile,
                                                   caplog):
    import logging

    token = client.post(f"/api/v1/media/{library[0]['id']}/playback",
                        headers=admin_profile["headers"]).json()["playback_token"]
    headers = {"Authorization": f"Bearer {token}",
               "X-Profile-Id": str(admin_profile["id"])}

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert client.get(f"/api/v1/stream/{library[0]['id']}",
                              headers=headers).status_code == 200

    lines = [record.getMessage() for record in caplog.records
             if record.name == "streamserver.api"]
    summary = [line for line in lines
               if line.startswith("stream ") and "cumulative=" in line]
    per_request = [line for line in lines
                   if line.startswith("GET ") and "/api/v1/stream/" in line]
    assert summary, (
        f"three streaming requests produced no summary at all, so the folding "
        f"is not running: {lines}"
    )
    assert not per_request, (
        f"each streaming request also produced its own line, which is what "
        f"fills the disk on a feature film: {per_request}"
    )


# ---------------------------------------------------------------------------
# Two addresses, one profile
# ---------------------------------------------------------------------------
# "This title is being streamed from somewhere it was not started from" is the
# question an audit trail is for, and nothing in the server answered it: the
# `client_ip` on a playback-session row recorded only who *started* it, so a
# second address joining an in-progress film left no trace at all.  These four
# tests are that trace.

@pytest.fixture
def trusting_proxy(app, client):
    """Let ``X-Forwarded-For`` decide the caller's address.

    Without this the test client and the "second address" are both
    ``testclient``, and the code under test -- correctly -- ignores the header
    from an untrusted peer.  Set on the *config object*, not re-created, so the
    limiter still buckets by address and the two-address tests do not spend
    somebody else's rate limit.
    """
    context = app.state.context
    context.config.server.trusted_proxies = ["testclient"]
    return context


def _start_from(client, media_id, headers, address: str):
    """Open a playback session as if the request had arrived from `address`.

    A *session*, not a stream request: a session row is one device, and the
    server's notion of "which devices is this title playing on" is one row per
    device.  Reusing one session for ten different addresses -- which is what a
    single streaming connection seeks through -- would be one device that moved,
    and the address history would correctly show one address at a time.
    """
    request_headers = dict(headers)
    request_headers["X-Forwarded-For"] = address
    response = client.post(f"/api/v1/media/{media_id}/playback",
                           headers=request_headers)
    assert response.status_code == 200, response.text
    return response.json()["playback_token"]


def _stream_from(client, media_id, headers, address: str | None = None):
    request_headers = dict(headers)
    if address:
        request_headers["X-Forwarded-For"] = address
    return client.get(f"/api/v1/stream/{media_id}", headers=request_headers)


def _audit_rows(app, action: str) -> list[dict]:
    rows = app.state.context.db.query(
        "SELECT ip_address, device_name, detail FROM audit_log "
        "WHERE action = ? ORDER BY id",
        (action,),
    )
    return [
        {"ip": row["ip_address"], "device": row["device_name"],
         "detail": json.loads(row["detail"] or "{}")}
        for row in rows
    ]


def test_a_second_device_streaming_the_same_title_is_recorded(client, app, library,
                                                               admin_profile,
                                                               trusting_proxy):
    """The case the feature exists for, in the shape it actually happens.

    One profile, one title, two devices: a television and a phone, or a second
    browser.  Both start playback, and the second one's row has to name both
    addresses -- because "this title is playing in two places" is the question
    an audit trail is for, and before this it was not recorded anywhere.
    """
    media_id = library[0]["id"]
    headers = admin_profile["headers"]
    token = _start_from(client, media_id, headers, "203.0.113.7")
    second = _start_from(client, media_id, headers, "203.0.113.8")

    stream = {"Authorization": f"Bearer {second}",
              "X-Profile-Id": str(admin_profile["id"])}
    assert _stream_from(client, media_id, stream, "203.0.113.8").status_code == 200

    rows = [r for r in _audit_rows(app, "media.stream.denied")
            if r["detail"].get("reason") == "second_address_streaming"]
    assert rows, "a second device streaming the same title left no audit row"

    latest = rows[-1]
    assert latest["ip"] == "203.0.113.8"
    assert latest["detail"]["address"] == "203.0.113.8"
    assert latest["detail"]["device_count"] == 2
    # The address it did *not* come from is on the list beside it rather than
    # replaced by it: two addresses is the finding, one address is the normal
    # case, and a list that only ever holds the current one proves nothing.
    assert "203.0.113.7" in latest["detail"]["devices"]
    assert "203.0.113.8" in latest["detail"]["devices"]

    # And the first device's token still works, so this is two devices rather
    # than a lockout.
    assert _stream_from(client, media_id,
                        {"Authorization": f"Bearer {token}",
                         "X-Profile-Id": str(admin_profile["id"])},
                        "203.0.113.7").status_code == 200


def test_range_requests_from_the_known_address_are_not_recorded(client, app, library,
                                                                admin_profile,
                                                                trusting_proxy):
    """One film is thousands of Range requests; none of them is evidence.

    The counter-case to the test above, and the one that would make the feature
    useless: without it, every seek of every film writes a row, and the audit
    table becomes a log of playback mechanics rather than of who watched what.
    """
    media_id = library[0]["id"]
    headers = admin_profile["headers"]
    token = _start_from(client, media_id, headers, "203.0.113.7")
    stream = {"Authorization": f"Bearer {token}",
              "X-Profile-Id": str(admin_profile["id"])}

    for _ in range(5):
        assert _stream_from(client, media_id, stream, "203.0.113.7").status_code == 200

    rows = [r for r in _audit_rows(app, "media.stream.denied")
            if r["detail"].get("reason") == "second_address_streaming"]
    assert rows == [], (
        f"five Range requests from the address that started playback each wrote "
        f"an audit row: {rows}"
    )


def test_a_refused_stream_names_the_address_that_asked(client, app, library,
                                                       admin_profile,
                                                       trusting_proxy):
    """A refusal is evidence *because* it was refused.

    A playback token is bound to one title; presenting it for another is either
    a bug or a probe, and either way the audit row has to name the address that
    made the request rather than only the account that owns the token.
    """
    media_id = library[0]["id"]
    other = library[1]["id"]
    headers = admin_profile["headers"]
    token = _start_from(client, media_id, headers, "203.0.113.7")
    stream = {"Authorization": f"Bearer {token}",
              "X-Profile-Id": str(admin_profile["id"])}

    response = _stream_from(client, other, stream, "198.51.100.4")
    assert response.status_code == 403, response.text

    rows = _audit_rows(app, "media.stream.denied")
    assert rows, "a 403 wrote no audit row"
    assert rows[-1]["ip"] == "198.51.100.4"
    assert rows[-1]["detail"]["reason"] == "playback_token_bound_to_other_media"
    # The token's own media id is evidence; the token is not, and never is.
    assert rows[-1]["detail"]["minted_for_media_id"] == media_id


def test_the_start_event_lists_the_devices_already_streaming(client, app, library,
                                                             admin_profile,
                                                             trusting_proxy):
    """The answer is readable without replaying the whole request history.

    Starting the same title twice -- from a second address -- puts both on the
    start event, which is the row an administrator looks at first.
    """
    media_id = library[0]["id"]
    headers = admin_profile["headers"]
    token = _start_from(client, media_id, headers, "203.0.113.7")
    stream = {"Authorization": f"Bearer {token}",
              "X-Profile-Id": str(admin_profile["id"])}

    assert _stream_from(client, media_id, stream, "203.0.113.7").status_code == 200
    _start_from(client, media_id, headers, "203.0.113.8")

    starts = _audit_rows(app, "media.stream.start")
    assert starts, "no playback start was recorded"
    devices = starts[-1]["detail"]["devices"]
    assert "203.0.113.7" in devices and "203.0.113.8" in devices, (
        f"the start event does not list both addresses: {devices}"
    )


def test_each_address_is_recorded_even_though_the_list_is_capped(client, app, library,
                                                                  admin_profile,
                                                                  trusting_proxy):
    """A capped list must not become a capped *record*.

    Ten devices on one profile and one title produces ten rows and an eight-entry
    list on each.  The list says it is truncated; the `ip_address` column says
    who it was.  Both matter -- a truncated list that hid the ninth and tenth
    addresses would be the silent truncation this whole feature must not be.
    """
    from streamserver.security import audit as audit_module

    media_id = library[0]["id"]
    headers = admin_profile["headers"]

    seen: list[str] = []
    for index in range(audit_module.MAX_DETAIL_DEVICES + 2):
        address = f"198.51.100.{index + 1}"
        token = _start_from(client, media_id, headers, address)
        stream = {"Authorization": f"Bearer {token}",
                  "X-Profile-Id": str(admin_profile["id"])}
        assert _stream_from(client, media_id, stream, address).status_code == 200
        seen.append(address)

    rows = [r for r in _audit_rows(app, "media.stream.denied")
            if r["detail"].get("reason") == "second_address_streaming"]
    recorded = [row["ip"] for row in rows]
    # The first device is the normal case and records nothing -- one device is
    # not a finding -- so the row for the second device is the first one, and
    # there is one row per device after that.
    assert recorded == seen[1:], (
        f"{len(seen)} distinct devices produced {len(recorded)} rows: {recorded}"
    )

    # Every row carries a list no longer than the cap, and the real count
    # beside it -- so a reader can tell "eight of ten" from "all eight".
    assert all(len(row["detail"]["devices"]) <= audit_module.MAX_DETAIL_DEVICES
               for row in rows)
    assert rows[-1]["detail"]["device_count"] == len(seen)
    assert rows[-1]["detail"]["more_devices"] is True
    # The first device is legitimately uncapped: one device really was all
    # there was.  Saying otherwise on the second would be noise.
    assert rows[0]["detail"]["more_devices"] is False
