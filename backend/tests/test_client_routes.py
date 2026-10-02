"""Every route the Qt client builds must exist on the server.

This is a mechanical check, and it exists because the defect it catches is
invisible to both test suites on their own:

* the client's tests answer a request from a canned table, so a path that the
  server does not serve still "works";
* the server's tests call each route directly, so a path nobody calls is still
  covered.

Nothing anywhere asked whether the two agreed.  The audit that added this file
found no disagreement in the routing -- the rating path and the artwork paths
are both right -- but it did find three ways for *this* check to pass over an
empty list, each of which had to be fixed before the check meant anything:

* ``route.path`` already carries its router's prefix, so adding it again gave
  ``/api/v1/admin/admin/backup`` and silently doubled every other prefix;
* the route modules use ``from __future__ import annotations``, so
  ``parameter.annotation is deps.ProfileDep`` is never true -- the annotation
  is the *string* ``"ProfileDep"``;
* a literal like ``"/profiles/"`` followed by ``QString::number(...)`` is a
  path *prefix*, and leaving its trailing slash put ``//{}`` into the
  comparison and made every interpolated route look missing.

The client is the reference here rather than the server, because the server's
routes are the documented public contract (see API.md) and a client that
disagrees with the contract is the bug.  Where the two genuinely differ, this
test is the place that says so loudly, in one list, rather than leaving it to
be discovered one 404 at a time.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from streamserver.api.v1 import (  # noqa: E402
    admin_routes,
    auth_routes,
    library_routes,
    media_routes,
    profile_routes,
)

CLIENT_SOURCE = REPO_ROOT / "client" / "src" / "apiclient.cpp"
CLIENT_SCREENS = REPO_ROOT / "client" / "src" / "credentialaccessmanager.cpp"

#: `/api/v1` is a literal in the client, not a route on any router.
API_ROOT = "/api/v1"


#: The modules whose routers are part of the v1 contract, and the streaming
#: router that is mounted beside them rather than inside one.
_ROUTER_MODULES = (auth_routes, profile_routes, library_routes,
                   media_routes, admin_routes)


def _routers():
    """Every router that ends up mounted under ``/api/v1``.

    Yielded rather than returned as a list so that the route listing and the
    profile-scope check below agree about which routers exist -- a second,
    hand-written list of "the routers" is how those two would drift.
    """
    for module in _ROUTER_MODULES:
        yield module.router
        stream_router = getattr(module, "stream_router", None)
        if stream_router is not None:
            yield stream_router


def _routes() -> set[str]:
    """Every path the server serves, with parameter names erased.

    ``{media_id}`` and ``{profile_id}`` are the same shape -- one segment -- so
    the check compares shapes, not names.  Anything else would make a rename
    look like a missing route.

    ``route.path`` already carries the router's prefix -- FastAPI folds it in
    when the route is added -- so this must not add ``router.prefix`` again.
    Doing so produced ``/api/v1/admin/admin/backup`` and quietly doubled every
    other prefix, which is the kind of bug this file exists to find.
    """
    paths: set[str] = set()
    for router in _routers():
        for route in router.routes:
            paths.add(_shape(API_ROOT + route.path))
    return paths


def _shape(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def _client_paths() -> set[str]:
    """Every URL the client builds, with interpolation erased.

    Read out of the source rather than maintained by hand: a hand-maintained
    list is a second copy of the client's routing that is wrong the moment
    somebody adds a screen.  The patterns cover the two ways a path is spelled
    there -- a literal after ``kApiRoot``, and a literal followed by
    ``QString::number(...)`` for an interpolated id.
    """
    source = CLIENT_SOURCE.read_text(encoding="utf-8")

    paths: set[str] = set()
    for match in re.finditer(
        r'kApiRoot \+ QStringLiteral\("([^"]*)"\)(.*?)(?:;\n|\n    request\.)',
        source, re.S,
    ):
        literal, tail = match.group(1), match.group(2)
        # Anything appended after the literal is either an id or a suffix.
        suffixes = re.findall(r'QStringLiteral\("([^"]*)"\)', tail)
        numbers = len(re.findall(r"QString::number\(", tail))
        # The literal is a *prefix*: "/profiles/" followed by an id, not a path
        # of its own.  Leaving the trailing slash would put "//{}" in the
        # comparison and make every such route "missing".
        path = (API_ROOT + literal).rstrip("/")
        for _ in range(numbers):
            path += "/{}"
        for suffix in suffixes:
            path += suffix
        paths.add(_shape(path))

    # `streamUrl` and `artworkUrl` build by hand rather than through `Pending`.
    paths.add(_shape(API_ROOT + "/stream/{}"))
    paths.add(_shape(API_ROOT + "/media/{}/poster"))
    paths.add(_shape(API_ROOT + "/media/{}/backdrop"))

    # `/api/v1/media/{id}` on its own is the artwork and playback *root*: the
    # client derives poster, backdrop and stream URLs from it and never
    # requests it.  Dropped here so the path check does not demand a route
    # that is deliberately only a prefix.
    paths.discard(_shape(API_ROOT + "/media/{}"))
    return paths


def test_the_contract_was_actually_read() -> None:
    """A parser that silently matches nothing would make this suite vacuous.

    The check below is only worth anything if it found the routes it was
    written to find; this asserts that it did, so a refactor of the client's
    path-building cannot quietly turn twenty assertions into twenty passes over
    an empty list.
    """
    server = _routes()
    assert len(server) > 50, f"only {len(server)} routes parsed out of the API"
    assert API_ROOT + "/auth/login" in server
    assert API_ROOT + "/media/{}/playback" in server

    client = _client_paths()
    assert len(client) > 15, f"only {len(client)} client paths parsed"
    assert API_ROOT + "/auth/login" in client
    assert API_ROOT + "/media/{}/playback" in client


@pytest.mark.parametrize("path", sorted(_client_paths()))
def test_every_client_path_is_a_server_route(path: str) -> None:
    missing = sorted(_routes() - {path})
    assert path in _routes(), (
        f"the client builds {path}, which the server does not serve.\n"
        f"The server's paths near it: "
        f"{[p for p in missing if p.split('/')[:5] == path.split('/')[:5]] or missing[:8]}"
    )


def test_the_artwork_routes_the_client_needs_exist() -> None:
    """`CredentialAccessManager` attaches a token to exactly these two paths.

    It is the only thing that lets a poster load, because `QQuickImage` cannot
    send a header.  If a route it names stops existing the manager keeps
    authenticating requests to nothing, and every poster in the app goes blank
    with no error anywhere -- so it is asserted by name rather than by the
    shape check above, which would accept a differently-shaped route.
    """
    source = CLIENT_SCREENS.read_text(encoding="utf-8")
    routes = _routes()
    checked: list[str] = []

    for literal in re.findall(r'QLatin1String\("([^"]*)"\)', source):
        if not literal.startswith(API_ROOT):
            continue
        checked.append(literal)
        # The manager tests a *prefix*, so the route that has to exist is the
        # shortest one it would authenticate: a poster or a backdrop.
        assert any(route.startswith(literal) for route in routes), (
            f"CredentialAccessManager authenticates {literal}*, which the "
            f"server serves no route under"
        )

    assert len(checked) >= 2, (
        f"only {len(checked)} credentialed paths parsed out of "
        f"{CLIENT_SCREENS.name}: {checked}.  If that drops, this test is "
        f"checking nothing."
    )


def test_the_profile_header_reaches_every_scoped_route() -> None:
    """Every profile-scoped route has a client request behind it.

    A route annotated ``ProfileDep`` or ``StreamProfileDep`` is resolved
    through ``_resolve_profile``, which reads ``X-Profile-Id``.  The client is
    the only thing that sends that header, so a route it has no request for is
    a route that can only ever resolve against a token -- and a login token
    carries no profile, which is a 404 or, worse, the wrong profile.

    The annotation is matched by name because the route modules use
    ``from __future__ import annotations``: at runtime every parameter
    annotation is the *string* ``"ProfileDep"``, so comparing with ``is``
    silently matches nothing.  That is not hypothetical -- the first version of
    this test did exactly that and passed over an empty list, which is why the
    count assertion below is here.
    """
    import inspect

    aliases = {"ProfileDep", "StreamProfileDep"}
    client = CLIENT_SOURCE.read_text(encoding="utf-8")
    checked: list[str] = []

    for router in _routers():
        for route in router.routes:
            target = getattr(route, "endpoint", None)
            if target is None:
                continue
            names = {
                getattr(parameter.annotation, "__name__", None)
                or str(parameter.annotation)
                for parameter in inspect.signature(target).parameters.values()
            }
            if not names & aliases:
                continue

            # A profile-scoped route is not automatically one the client
            # should call: `by-slug` is an alternative spelling of a route the
            # client already has by id, and adding a client method for it would
            # be a second way to do one thing.  The requirement is therefore on
            # the *roots* -- if the client builds any request under a root, the
            # header has to be reaching it.
            root = "/" + route.path.strip("/").split("/", 1)[0]
            checked.append(root)

            # /stream is the exception: the media backend fetches it, not the
            # network access manager, so `ApiClient` never builds the request.
            # It is checked separately, by the artwork test above, which is
            # where the credential actually has to be attached.
            if root == "/stream":
                assert "/stream/" in client, (
                    "the streaming route needs a credential but the client "
                    "builds no stream URL for it"
                )
                continue

            assert f'"{root}/"' in client or f'"{root}"' in client, (
                f"{API_ROOT}{route.path} is profile-scoped, but the client "
                f"builds no request under {API_ROOT}{root}"
            )

    assert len(checked) >= 10, (
        f"only {len(checked)} profile-scoped routes were recognised: {checked}.  "
        f"If that count drops, this test is checking nothing."
    )
    # The three roots the client can be scoped under.  A profile-scoped route
    # under a fourth root would not be covered by this test, so the set is
    # asserted rather than assumed.
    assert set(checked) <= {"/library", "/media", "/profiles", "/stream"}, (
        f"a profile-scoped route appeared under an unexpected root: "
        f"{sorted(set(checked) - {'/library', '/media', '/profiles', '/stream'})}"
    )
    assert {"/library", "/media"} <= set(checked), (
        f"expected the client to be profile-scoped under /library and /media; "
        f"found {sorted(set(checked))}"
    )


# ---------------------------------------------------------------------------
# The default-password gate
# ---------------------------------------------------------------------------
#: The aliases that apply it.  ``require_uncapped`` is reached through one of
#: these rather than named directly, because a route takes the dependency as an
#: ``Annotated[...]`` alias -- and under ``from __future__ import annotations``
#: that alias is a *string* at runtime, which is exactly the trap documented
#: above: comparing with ``is`` matches nothing and the test passes over an
#: empty list.
UNCAPPED_ALIASES = {"UncappedCallerDep", "UncappedStreamCallerDep", "AdminDep"}

#: The routes that are deliberately *not* gated, each with its reason.  Most of
#: these are not reads at all -- signing out revokes a session row, streaming
#: records "started watching" -- and refusing them would leave a capped
#: account unable to *fix* the problem it has.
UNCAPPED_EXEMPT = {
    f"POST {API_ROOT}/auth/login": "no caller yet: authentication cannot be refused",
    f"POST {API_ROOT}/auth/refresh": "the refresh token is the credential, and refusing "
                          "it would strand the session with no way to renew it",
    f"POST {API_ROOT}/auth/logout": "revoking a session is the safest thing a capped "
                         "account can do, and it must work",
    f"POST {API_ROOT}/auth/logout-all": "as above, and more so",
    f"POST {API_ROOT}/auth/password": "this is the gate's own exit",
    f"POST {API_ROOT}/auth/setup": "unauthenticated bootstrap, and a fresh account is "
                        "never flagged",
}


def _mutating_routes() -> dict[str, object]:
    """Every route that can write, keyed ``"METHOD /path"`` with ids erased."""
    routes: dict[str, object] = {}
    for router in _routers():
        for route in router.routes:
            for method in route.methods:
                if method in ("GET", "HEAD", "OPTIONS"):
                    continue
                routes[f"{method} {_shape(API_ROOT + route.path)}"] = route.endpoint
    return routes


def test_the_gate_is_wired_to_every_mutating_route() -> None:
    """Every mutating route resolves a caller through the default-password gate.

    This check found the gate was inert.  ``require_uncapped`` existed, was
    documented, and was wired into *nothing*: :data:`UncappedDep` had no
    callers, and four mutating routes took no caller at all.  An account on the
    shipped default password could therefore favourite, rate, start a playback
    session and write watch history -- exactly the state the first-run flow
    exists to stop somebody else leaving behind -- and every one of those
    requests was refused with a 403 that nothing could trigger.

    The gate is a per-route dependency rather than middleware so that it reads
    as part of the route's own signature, where a reader of the handler can see
    it.  The cost of that choice is that it can be left off by accident, which
    is what this test is for.
    """
    import inspect

    mutating = _mutating_routes()
    assert set(UNCAPPED_EXEMPT) <= set(mutating), (
        f"the exemption list names routes that do not exist or are not "
        f"mutating: {sorted(set(UNCAPPED_EXEMPT) - set(mutating))}"
    )
    assert len(mutating) > 20, (
        f"only {len(mutating)} mutating routes parsed: {sorted(mutating)}.  "
        f"If this drops, the check below is checking nothing."
    )

    ungated: list[str] = []
    for key, endpoint in sorted(mutating.items()):
        if key in UNCAPPED_EXEMPT:
            continue
        names = {
            getattr(parameter.annotation, "__name__", None)
            or str(parameter.annotation)
            for parameter in inspect.signature(endpoint).parameters.values()
        }
        if not names & UNCAPPED_ALIASES:
            ungated.append(key)

    assert not ungated, (
        f"these mutating routes can be reached by an account that has not "
        f"changed the shipped default password:\n  " + "\n  ".join(ungated)
    )


def test_per_profile_routes_refuse_an_unnamed_profile() -> None:
    """Favourites, history and ratings must not answer for a profile nobody chose.

    A session token names an *account*.  Favourites, watch history and ratings
    are per profile by definition, so a request that names neither the header
    nor a playback token has to be refused rather than answered with the
    account's first profile -- two people sharing an account would otherwise see
    each other's lists, and the client would have no way to tell.

    This is asserted here rather than in one HTTP test because the point is
    *which routes take the strict dependency*: adding a per-profile route and
    giving it the lenient one is exactly the mistake that would ship.
    """
    import inspect

    strict = "OwnProfileDep"
    # Every other way a route can resolve a profile.  A route whose answer is
    # per profile but which takes none of these is not profile-scoped at all,
    # and that is its own finding rather than this test's subject.
    lenient = {"ProfileDep", "OwnProfileDep", "StreamProfileDep"}
    checked: list[str] = []

    for router in _routers():
        for route in router.routes:
            names = {
                getattr(parameter.annotation, "__name__", None)
                or str(parameter.annotation)
                for parameter in inspect.signature(route.endpoint).parameters.values()
            }
            # The streaming route is profile-scoped too, but it takes the
            # *stream* alias: it accepts a playback token, which names its own
            # profile, so it is strict by a different mechanism.
            if not names & lenient:
                continue
            path = _shape(API_ROOT + route.path)
            # Anything under /library that is not the catalogue, a title's own
            # metadata, search or a suggestion is per profile by definition.
            per_profile = (
                "/favorites" in path or "/history" in path
                or "/continue-watching" in path or "/rating" in path
                or path.endswith("/home")
            )
            checked.append(path)
            if per_profile and "StreamProfileDep" not in names:
                assert strict in names, (
                    f"{path} answers with per-profile state but takes "
                    f"{lenient}: a request naming no profile would be answered "
                    f"with the account's first one."
                )

    assert len(checked) >= 15, (
        f"only {len(checked)} profile-scoped routes recognised: {checked}.  "
        f"If this drops, the check above is checking nothing."
    )
    for path in ("/api/v1/library/favorites", "/api/v1/library/history",
                 "/api/v1/library/home", "/api/v1/library/media/{}/rating"):
        assert path in checked, f"{path} is no longer profile-scoped at all"
    # The catalogue is account-scoped and must stay lenient: refusing it would
    # make the very first request after sign-in an error.
    assert strict not in {
        name
        for router in _routers()
        for route in router.routes
        if route.path.endswith("/media")
        for name in {
            getattr(parameter.annotation, "__name__", None) or str(parameter.annotation)
            for parameter in inspect.signature(route.endpoint).parameters.values()
        }
    }


def test_the_gate_does_not_block_the_streaming_route() -> None:
    """Streaming is a GET that writes, and it is gated by method alone nowhere.

    ``require_uncapped`` skips GET/HEAD/OPTIONS because the client has to be
    able to read the library in order to render the change-password screen.
    Streaming is the exception: it records "started watching" before it serves
    a byte, so the method test alone would leave the exact state the gate
    protects writable with no write of its own to intercept.
    """
    import inspect

    from streamserver.api import deps

    stream = None
    for route in media_routes.stream_router.routes:
        stream = route.endpoint
        break
    assert stream is not None, "the streaming route disappeared"

    names = {
        getattr(parameter.annotation, "__name__", None)
        or str(parameter.annotation)
        for parameter in inspect.signature(stream).parameters.values()
    }
    assert names & UNCAPPED_ALIASES, (
        "the streaming route takes no uncapped dependency, so a capped "
        "account can stream and leave watch history behind"
    )
    assert deps._is_stream(_FakeRequest("GET", "/api/v1/stream/7"))
    assert deps._is_stream(_FakeRequest("GET", "/api/v1/stream"))
    assert not deps._is_stream(_FakeRequest("GET", "/api/v1/library/media"))
    assert not deps._is_stream(_FakeRequest("GET", "/api/v1/media/7/poster"))


class _FakeRequest:
    """The two attributes ``_is_stream`` reads, and nothing else.

    Building a real ``Request`` would mean fabricating an ASGI scope, which is
    most of what the function under test does not look at.
    """

    def __init__(self, method: str, path: str) -> None:
        self.method = method
        self.url = type("URL", (), {"path": path})()


# ---------------------------------------------------------------------------
# The default-password gate
# ---------------------------------------------------------------------------
#: The aliases that apply it.  ``require_uncapped`` is reached through one of
#: these rather than named directly, because a route takes the dependency as an
#: ``Annotated[...]`` alias -- and under ``from __future__ import annotations``
#: that alias is a *string* at runtime, which is exactly the trap documented
#: above: comparing with ``is`` matches nothing and the test passes over an
#: empty list.
UNCAPPED_ALIASES = {"UncappedCallerDep", "UncappedStreamCallerDep", "AdminDep"}

#: The routes that are deliberately *not* gated, each with its reason.  Most of
#: these are not reads at all -- signing out revokes a session row, streaming
#: records "started watching" -- and refusing them would leave a capped
#: account unable to *fix* the problem it has.
UNCAPPED_EXEMPT = {
    f"POST {API_ROOT}/auth/login": "no caller yet: authentication cannot be refused",
    f"POST {API_ROOT}/auth/refresh": "the refresh token is the credential, and refusing "
                          "it would strand the session with no way to renew it",
    f"POST {API_ROOT}/auth/logout": "revoking a session is the safest thing a capped "
                         "account can do, and it must work",
    f"POST {API_ROOT}/auth/logout-all": "as above, and more so",
    f"POST {API_ROOT}/auth/password": "this is the gate's own exit",
    f"POST {API_ROOT}/auth/setup": "unauthenticated bootstrap, and a fresh account is "
                        "never flagged",
}


def _mutating_routes() -> dict[str, object]:
    """Every route that can write, keyed ``"METHOD /path"`` with ids erased."""
    routes: dict[str, object] = {}
    for router in _routers():
        for route in router.routes:
            for method in route.methods:
                if method in ("GET", "HEAD", "OPTIONS"):
                    continue
                routes[f"{method} {_shape(API_ROOT + route.path)}"] = route.endpoint
    return routes


def test_the_gate_is_wired_to_every_mutating_route() -> None:
    """Every mutating route resolves a caller through the default-password gate.

    This check found the gate was inert.  ``require_uncapped`` existed, was
    documented, and was wired into *nothing*: :data:`UncappedDep` had no
    callers, and four mutating routes took no caller at all.  An account on the
    shipped default password could therefore favourite, rate, start a playback
    session and write watch history -- exactly the state the first-run flow
    exists to stop somebody else leaving behind -- and every one of those
    requests was refused with a 403 that nothing could trigger.

    The gate is a per-route dependency rather than middleware so that it reads
    as part of the route's own signature, where a reader of the handler can see
    it.  The cost of that choice is that it can be left off by accident, which
    is what this test is for.
    """
    import inspect

    mutating = _mutating_routes()
    assert set(UNCAPPED_EXEMPT) <= set(mutating), (
        f"the exemption list names routes that do not exist or are not "
        f"mutating: {sorted(set(UNCAPPED_EXEMPT) - set(mutating))}"
    )
    assert len(mutating) > 20, (
        f"only {len(mutating)} mutating routes parsed: {sorted(mutating)}.  "
        f"If this drops, the check below is checking nothing."
    )

    ungated: list[str] = []
    for key, endpoint in sorted(mutating.items()):
        if key in UNCAPPED_EXEMPT:
            continue
        names = {
            getattr(parameter.annotation, "__name__", None)
            or str(parameter.annotation)
            for parameter in inspect.signature(endpoint).parameters.values()
        }
        if not names & UNCAPPED_ALIASES:
            ungated.append(key)

    assert not ungated, (
        f"these mutating routes can be reached by an account that has not "
        f"changed the shipped default password:\n  " + "\n  ".join(ungated)
    )


def _exempt_key(method: str, path: str) -> set[str]:
    return {key for key in UNCAPPED_EXEMPT if key == f"{method} {path}"}


def test_per_profile_routes_refuse_an_unnamed_profile() -> None:
    """Favourites, history and ratings must not answer for a profile nobody chose.

    A session token names an *account*.  Favourites, watch history and ratings
    are per profile by definition, so a request that names neither the header
    nor a playback token has to be refused rather than answered with the
    account's first profile -- two people sharing an account would otherwise see
    each other's lists, and the client would have no way to tell.

    This is asserted here rather than in one HTTP test because the point is
    *which routes take the strict dependency*: adding a per-profile route and
    giving it the lenient one is exactly the mistake that would ship.
    """
    import inspect

    strict = "OwnProfileDep"
    # Every other way a route can resolve a profile.  A route whose answer is
    # per profile but which takes none of these is not profile-scoped at all,
    # and that is its own finding rather than this test's subject.
    lenient = {"ProfileDep", "OwnProfileDep", "StreamProfileDep"}
    checked: list[str] = []

    for router in _routers():
        for route in router.routes:
            names = {
                getattr(parameter.annotation, "__name__", None)
                or str(parameter.annotation)
                for parameter in inspect.signature(route.endpoint).parameters.values()
            }
            # The streaming route is profile-scoped too, but it takes the
            # *stream* alias: it accepts a playback token, which names its own
            # profile, so it is strict by a different mechanism.
            if not names & lenient:
                continue
            path = _shape(API_ROOT + route.path)
            # Anything under /library that is not the catalogue, a title's own
            # metadata, search or a suggestion is per profile by definition.
            per_profile = (
                "/favorites" in path or "/history" in path
                or "/continue-watching" in path or "/rating" in path
                or path.endswith("/home")
            )
            checked.append(path)
            if per_profile and "StreamProfileDep" not in names:
                assert strict in names, (
                    f"{path} answers with per-profile state but takes "
                    f"{lenient}: a request naming no profile would be answered "
                    f"with the account's first one."
                )

    assert len(checked) >= 15, (
        f"only {len(checked)} profile-scoped routes recognised: {checked}.  "
        f"If this drops, the check above is checking nothing."
    )
    for path in ("/api/v1/library/favorites", "/api/v1/library/history",
                 "/api/v1/library/home", "/api/v1/library/media/{}/rating"):
        assert path in checked, f"{path} is no longer profile-scoped at all"
    # The catalogue is account-scoped and must stay lenient: refusing it would
    # make the very first request after sign-in an error.
    assert strict not in {
        name
        for router in _routers()
        for route in router.routes
        if route.path.endswith("/media")
        for name in {
            getattr(parameter.annotation, "__name__", None) or str(parameter.annotation)
            for parameter in inspect.signature(route.endpoint).parameters.values()
        }
    }


def test_the_gate_does_not_block_the_streaming_route() -> None:
    """Streaming is a GET that writes, and it is gated by method alone nowhere.

    ``require_uncapped`` skips GET/HEAD/OPTIONS because the client has to be
    able to read the library in order to render the change-password screen.
    Streaming is the exception: it records "started watching" before it serves
    a byte, so the method test alone would leave the exact state the gate
    protects writable with no write of its own to intercept.
    """
    import inspect

    from streamserver.api import deps

    stream = None
    for route in media_routes.stream_router.routes:
        stream = route.endpoint
        break
    assert stream is not None, "the streaming route disappeared"

    names = {
        getattr(parameter.annotation, "__name__", None)
        or str(parameter.annotation)
        for parameter in inspect.signature(stream).parameters.values()
    }
    assert names & UNCAPPED_ALIASES, (
        "the streaming route takes no uncapped dependency, so a capped "
        "account can stream and leave watch history behind"
    )
    assert deps._is_stream(_FakeRequest("GET", "/api/v1/stream/7"))
    assert deps._is_stream(_FakeRequest("GET", "/api/v1/stream"))
    assert not deps._is_stream(_FakeRequest("GET", "/api/v1/library/media"))
    assert not deps._is_stream(_FakeRequest("GET", "/api/v1/media/7/poster"))


class _FakeRequest:
    """The two attributes ``_is_stream`` reads, and nothing else.

    Building a real ``Request`` would mean fabricating an ASGI scope, which is
    most of what the function under test does not look at.
    """

    def __init__(self, method: str, path: str) -> None:
        self.method = method
        self.url = type("URL", (), {"path": path})()
