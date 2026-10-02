"""Library, search, favourites, history and ratings over HTTP.

Also the cross-account isolation tests for per-profile state, and the
per-profile independence tests: two profiles of the *same* account must see
different favourites and different history.
"""

from __future__ import annotations

import pytest


def _ids(page: dict) -> list[int]:
    """Ids from a paged response body."""
    return [item["id"] for item in page["items"]]


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------
def test_empty_library_returns_an_empty_page(client, owner, admin_profile):
    response = client.get("/api/v1/library/media", headers=admin_profile["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [], "total": 0, "offset": 0, "limit": 50, "has_more": False}


def test_browse_lists_library_titles(client, library, admin_profile):
    response = client.get("/api/v1/library/media", headers=admin_profile["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == len(library)
    assert {item["title"] for item in body["items"]} == {"Dune", "Blade Runner", "Arrival"}


def test_media_items_never_expose_the_file_path(client, library, admin_profile):
    response = client.get("/api/v1/library/media", headers=admin_profile["headers"])
    text = response.text
    assert "media_path" not in text
    assert "/movies/" not in text
    assert "library_root" not in text


def test_media_item_carries_metadata_and_relations(client, library, admin_profile):
    detail = client.get(f"/api/v1/library/media/{library[0]['id']}",
                        headers=admin_profile["headers"]).json()
    assert detail["title"] == "Dune"
    assert detail["year"] == 2021
    assert detail["runtime_minutes"] == 120
    assert detail["overview"]
    assert {g["slug"] for g in detail["genres"]} == {"science-fiction", "adventure"}
    assert detail["is_favorite"] is False
    assert detail["user_rating"] is None
    assert "streams" in detail


def test_media_by_slug(client, library, admin_profile):
    slug = library[0]["slug"]
    response = client.get(f"/api/v1/library/media/by-slug/{slug}",
                          headers=admin_profile["headers"])
    assert response.status_code == 200
    assert response.json()["slug"] == slug


def test_unknown_media_is_404(client, admin_profile):
    for path in ("/api/v1/library/media/999999",
                 "/api/v1/library/media/by-slug/nope"):
        assert client.get(path, headers=admin_profile["headers"]).status_code == 404


def test_a_non_positive_id_is_rejected_by_validation(client, admin_profile):
    """An id is an identifier, not a number to look up.  Rejecting it during
    validation is both cheaper and more honest than returning "not found" for a
    request that was malformed rather than merely unsatisfied."""
    for path in ("/api/v1/library/media/0",
                 "/api/v1/library/media/-1",
                 "/api/v1/profiles/0"):
        response = client.get(path, headers=admin_profile["headers"])
        assert response.status_code == 422, path
        assert response.json()["error"] == "validation_error"


@pytest.mark.parametrize("params,expected_dune", [
    ({"sort": "title", "direction": "asc"}, True),
    ({"sort": "year", "direction": "asc"}, True),
    ({"year": 2021}, True),
    ({"year_from": 2017, "year_to": 2021}, True),
    ({"decade": 2020}, True),
    ({"genre": "science-fiction"}, True),
    ({"genre": "horror"}, False),
    ({"content_type": "movie"}, True),
    ({"content_type": "series"}, False),
])
def test_filters(client, library, admin_profile, params, expected_dune):
    response = client.get("/api/v1/library/media", headers=admin_profile["headers"],
                          params=params)
    assert response.status_code == 200, response.text
    titles = {item["title"] for item in response.json()["items"]}
    assert ("Dune" in titles) is expected_dune


@pytest.mark.parametrize("params", [
    {"sort": "nonsense"},
    {"direction": "sideways"},
    {"content_type": "hologram"},
    {"resolution": "8k"},
    {"limit": 0},
    {"limit": 5000},
    {"offset": -1},
    {"year": 1200},
    {"year_from": 2020, "year_to": 2010},
    {"min_rating": "high"},
])
def test_bad_filters_are_rejected(client, admin_profile, params):
    response = client.get("/api/v1/library/media", headers=admin_profile["headers"],
                          params=params)
    assert response.status_code == 422, response.text


def test_pagination(client, library, admin_profile):
    first = client.get("/api/v1/library/media", headers=admin_profile["headers"],
                       params={"limit": 2, "offset": 0}).json()
    assert first["total"] == 3 and first["has_more"] is True
    second = client.get("/api/v1/library/media", headers=admin_profile["headers"],
                        params={"limit": 2, "offset": 2}).json()
    assert second["has_more"] is False
    assert not set(_ids(first)) & set(_ids(second))
    assert first["offset"] == 0 and second["offset"] == 2


def test_categories_expose_facets(client, library, admin_profile):
    body = client.get("/api/v1/library/categories", headers=admin_profile["headers"]).json()
    genres = {g["slug"]: g["count"] for g in body["genres"]}
    assert genres["science-fiction"] == 3
    assert genres["drama"] == 1
    assert {y["year"] for y in body["years"]} >= {2016, 2017, 2021}
    assert body["stats"]["total"] == 3


def test_stats_and_home_rails(client, library, admin_profile):
    stats = client.get("/api/v1/library/stats", headers=admin_profile["headers"]).json()
    assert stats["total"] == 3

    home = client.get("/api/v1/library/home", headers=admin_profile["headers"]).json()
    for rail in ("continue_watching", "recently_added", "recommended", "popular",
                 "top_rated", "favorites", "recently_watched", "categories"):
        assert rail in home, rail
    assert len(home["recently_added"]) == 3
    assert isinstance(home["categories"], list)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query,expected", [
    ("Dune", {"Dune"}),
    ("dune", {"Dune"}),
    ("blade runner", {"Blade Runner"}),
    ("blade", {"Blade Runner"}),
    ("arriv", {"Arrival"}),
    ("OVERVIEW", {"Dune", "Blade Runner", "Arrival"}),   # description match
    ("science", {"Dune", "Blade Runner", "Arrival"}),    # genre match
    ("nomatchxyz", set()),
])
def test_search_matches_title_overview_and_genre(client, library, admin_profile,
                                                 query, expected):
    response = client.get("/api/v1/library/search", headers=admin_profile["headers"],
                          params={"q": query})
    assert response.status_code == 200, response.text
    assert {item["title"] for item in response.json()["items"]} == expected


def test_search_without_a_query_browses(client, library, admin_profile):
    response = client.get("/api/v1/library/search", headers=admin_profile["headers"],
                          params={"genre": "drama"})
    assert {item["title"] for item in response.json()["items"]} == {"Arrival"}


@pytest.mark.parametrize("hostile", [
    'title:" OR 1=1 --',
    "*",
    '" OR "a"="a',
    "NEAR(a b)",
    "^ AND",
    "a" * 500,
])
def test_search_input_is_never_fts_syntax(client, library, admin_profile, hostile):
    """Free text is quoted before it reaches the FTS parser, so a query that
    looks like MATCH syntax is just text."""
    response = client.get("/api/v1/library/search", headers=admin_profile["headers"],
                          params={"q": hostile})
    assert response.status_code in (200, 422), response.text
    if response.status_code == 200:
        assert isinstance(response.json()["items"], list)


def test_suggestions(client, library, admin_profile):
    response = client.get("/api/v1/library/suggest", headers=admin_profile["headers"],
                          params={"q": "du", "limit": 5})
    assert response.status_code == 200
    assert [s["title"] for s in response.json()] == ["Dune"]


# ---------------------------------------------------------------------------
# Favourites
# ---------------------------------------------------------------------------
def test_favourites_round_trip(client, library, admin_profile):
    dune, blade, _ = library

    added = client.put(f"/api/v1/library/favorites/{dune['id']}",
                       headers=admin_profile["headers"])
    assert added.status_code == 200

    listed = client.get("/api/v1/library/favorites", headers=admin_profile["headers"]).json()
    assert [i["id"] for i in listed["items"]] == [dune["id"]]
    assert listed["items"][0]["is_favorite"] is True

    # Adding twice is idempotent.
    again = client.put(f"/api/v1/library/favorites/{dune['id']}",
                       headers=admin_profile["headers"])
    assert "already" in again.json()["detail"].lower()

    removed = client.delete(f"/api/v1/library/favorites/{dune['id']}",
                            headers=admin_profile["headers"])
    assert removed.status_code == 200
    assert client.get("/api/v1/library/favorites",
                      headers=admin_profile["headers"]).json()["items"] == []


def test_favourites_can_be_sorted(client, library, admin_profile):
    for item in library:
        client.put(f"/api/v1/library/favorites/{item['id']}",
                   headers=admin_profile["headers"])
    by_year = client.get("/api/v1/library/favorites", headers=admin_profile["headers"],
                         params={"sort": "year", "direction": "asc"}).json()
    years = [i["year"] for i in by_year["items"]]
    assert years == sorted(years)


def test_favouriting_unknown_media_is_404(client, admin_profile):
    assert client.put("/api/v1/library/favorites/9999",
                      headers=admin_profile["headers"]).status_code == 404


# ---------------------------------------------------------------------------
# History and progress
# ---------------------------------------------------------------------------
def test_progress_round_trip(client, library, admin_profile):
    dune = library[0]
    put = client.put(f"/api/v1/library/history/{dune['id']}", headers=admin_profile["headers"],
                     json={"position_seconds": 600, "duration_seconds": 7200})
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["position_seconds"] == 600
    assert 0 < body["percent"] < 1
    assert body["completed"] is False

    got = client.get(f"/api/v1/library/history/{dune['id']}",
                     headers=admin_profile["headers"]).json()
    assert got["position_seconds"] == 600


def test_completion_threshold(client, library, admin_profile):
    dune = library[0]
    client.put(f"/api/v1/library/history/{dune['id']}", headers=admin_profile["headers"],
               json={"position_seconds": 7100, "duration_seconds": 7200})
    body = client.get(f"/api/v1/library/history/{dune['id']}",
                      headers=admin_profile["headers"]).json()
    assert body["completed"] is True

    # Rewinding clears "finished" again, so continue-watching reappears.
    client.put(f"/api/v1/library/history/{dune['id']}", headers=admin_profile["headers"],
               json={"position_seconds": 60, "duration_seconds": 7200})
    body = client.get(f"/api/v1/library/history/{dune['id']}",
                      headers=admin_profile["headers"]).json()
    assert body["completed"] is False


def test_continue_watching_only_shows_unfinished(client, library, admin_profile):
    dune, blade, _ = library
    client.put(f"/api/v1/library/history/{dune['id']}", headers=admin_profile["headers"],
               json={"position_seconds": 600, "duration_seconds": 7200})
    client.put(f"/api/v1/library/history/{blade['id']}", headers=admin_profile["headers"],
               json={"position_seconds": 7100, "duration_seconds": 7200})

    items = client.get("/api/v1/library/continue-watching",
                       headers=admin_profile["headers"]).json()
    assert [i["id"] for i in items] == [dune["id"]]


def test_tiny_positions_do_not_create_continue_watching(client, library, admin_profile):
    """Two seconds in is not "started"."""
    dune = library[0]
    client.put(f"/api/v1/library/history/{dune['id']}", headers=admin_profile["headers"],
               json={"position_seconds": 2, "duration_seconds": 7200})
    assert client.get("/api/v1/library/continue-watching",
                      headers=admin_profile["headers"]).json() == []


def test_history_can_be_deleted_item_by_item_and_whole(client, library, admin_profile):
    dune, blade, _ = library
    for item in library:
        client.put(f"/api/v1/library/history/{item['id']}",
                   headers=admin_profile["headers"],
                   json={"position_seconds": 60, "duration_seconds": 7200})

    assert client.delete(f"/api/v1/library/history/{dune['id']}",
                         headers=admin_profile["headers"]).status_code == 200
    remaining = client.get("/api/v1/library/history", headers=admin_profile["headers"]).json()
    assert len(remaining["items"]) == 2

    cleared = client.delete("/api/v1/library/history", headers=admin_profile["headers"])
    assert cleared.status_code == 200
    assert client.get("/api/v1/library/history",
                      headers=admin_profile["headers"]).json()["items"] == []


def test_deleting_absent_history_is_404(client, library, admin_profile):
    assert client.delete(f"/api/v1/library/history/{library[0]['id']}",
                         headers=admin_profile["headers"]).status_code == 404


def test_media_progress_endpoint_matches_history(client, library, admin_profile):
    dune = library[0]
    client.put(f"/api/v1/media/{dune['id']}/progress", headers=admin_profile["headers"],
               json={"position_seconds": 120, "duration_seconds": 7200})
    body = client.get(f"/api/v1/media/{dune['id']}/progress",
                      headers=admin_profile["headers"]).json()
    assert body["position_seconds"] == 120


def test_absurd_progress_is_clamped(client, library, admin_profile):
    """A client bug must not be able to poison the continue-watching rail."""
    dune = library[0]
    body = client.put(f"/api/v1/library/history/{dune['id']}",
                      headers=admin_profile["headers"],
                      json={"position_seconds": 10_000_000,
                            "duration_seconds": 7200}).json()
    assert body["position_seconds"] <= 7200


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------
def test_rating_round_trip_and_snapping(client, library, admin_profile):
    dune = library[0]
    put = client.put(f"/api/v1/library/media/{dune['id']}/rating",
                     headers=admin_profile["headers"], json={"score": 4.3})
    assert put.status_code == 200
    assert put.json()["score"] == 4.5        # snapped to the nearest half star

    got = client.get(f"/api/v1/library/media/{dune['id']}/rating",
                     headers=admin_profile["headers"]).json()
    assert got["score"] == 4.5

    detail = client.get(f"/api/v1/library/media/{dune['id']}",
                        headers=admin_profile["headers"]).json()
    assert detail["user_rating"] == 4.5


@pytest.mark.parametrize("score", [0, 0.4, 5.1, 10, -1, 1000])
def test_out_of_range_ratings_are_rejected(client, library, admin_profile, score):
    response = client.put(f"/api/v1/library/media/{library[0]['id']}/rating",
                          headers=admin_profile["headers"], json={"score": score})
    assert response.status_code == 422


def test_rating_can_be_removed(client, library, admin_profile):
    dune = library[0]
    client.put(f"/api/v1/library/media/{dune['id']}/rating",
               headers=admin_profile["headers"], json={"score": 3.0})
    assert client.delete(f"/api/v1/library/media/{dune['id']}/rating",
                         headers=admin_profile["headers"]).status_code == 200
    assert client.get(f"/api/v1/library/media/{dune['id']}",
                      headers=admin_profile["headers"]).json()["user_rating"] is None


# ---------------------------------------------------------------------------
# Per-profile independence
# ---------------------------------------------------------------------------
@pytest.fixture
def two_profiles(client, owner):
    """Two profiles of the *same* account, each with its own header set."""
    second = client.post("/api/v1/profiles", headers=owner["headers"],
                         json={"name": "Second"}).json()
    return {
        "one": {**owner["headers"], "X-Profile-Id": str(second["id"] - 1)},
        "two": {**owner["headers"], "X-Profile-Id": str(second["id"])},
        "id_one": second["id"] - 1,
        "id_two": second["id"],
    }


def test_favourites_are_per_profile(client, library, two_profiles):
    dune = library[0]
    client.put(f"/api/v1/library/favorites/{dune['id']}", headers=two_profiles["one"])

    assert client.get("/api/v1/library/favorites",
                      headers=two_profiles["one"]).json()["total"] == 1
    assert client.get("/api/v1/library/favorites",
                      headers=two_profiles["two"]).json()["total"] == 0


def test_history_is_per_profile(client, library, two_profiles):
    dune = library[0]
    client.put(f"/api/v1/library/history/{dune['id']}", headers=two_profiles["one"],
               json={"position_seconds": 600, "duration_seconds": 7200})

    first = client.get(f"/api/v1/library/history/{dune['id']}",
                       headers=two_profiles["one"]).json()
    second = client.get(f"/api/v1/library/history/{dune['id']}",
                        headers=two_profiles["two"]).json()
    assert first["position_seconds"] == 600
    assert second["position_seconds"] == 0


def test_ratings_are_per_profile(client, library, two_profiles):
    blade = library[1]
    client.put(f"/api/v1/library/media/{blade['id']}/rating",
               headers=two_profiles["one"], json={"score": 5.0})
    assert client.get(f"/api/v1/library/media/{blade['id']}",
                      headers=two_profiles["two"]).json()["user_rating"] is None


# ---------------------------------------------------------------------------
# Cross-account isolation
# ---------------------------------------------------------------------------
def test_history_is_invisible_to_another_account(client, library, owner,
                                                 second_session):
    dune = library[0]
    client.put(f"/api/v1/library/history/{dune['id']}", headers=owner["headers"],
               json={"position_seconds": 600, "duration_seconds": 7200})
    client.put(f"/api/v1/library/favorites/{dune['id']}", headers=owner["headers"])

    # Another account may see the title in the library (it is not per-user),
    # but none of the owner's private state.
    assert client.get("/api/v1/library/favorites",
                      headers=second_session["headers"]).json()["total"] == 0
    assert client.get("/api/v1/library/history",
                      headers=second_session["headers"]).json()["items"] == []
    assert client.get("/api/v1/library/continue-watching",
                      headers=second_session["headers"]).json() == []

    detail = client.get(f"/api/v1/library/media/{dune['id']}",
                        headers=second_session["headers"]).json()
    assert detail["is_favorite"] is False
    assert detail["user_rating"] is None


def test_another_account_cannot_clear_my_history(client, library, owner,
                                                 second_session):
    dune = library[0]
    client.put(f"/api/v1/library/history/{dune['id']}", headers=owner["headers"],
               json={"position_seconds": 600, "duration_seconds": 7200})
    client.delete("/api/v1/library/history", headers=second_session["headers"])
    assert client.get(f"/api/v1/library/history/{dune['id']}",
                      headers=owner["headers"]).json()["position_seconds"] == 600