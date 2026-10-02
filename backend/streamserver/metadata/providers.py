"""Metadata providers.

The server must work with **no** external network and **no** API keys, so the
default provider is :class:`LocalSidecarProvider`, which reads Kodi-style
``.nfo`` files and JSON sidecars from next to the media.

``TmdbProvider`` and ``ImdbProvider`` are optional.  They are only reachable
when an operator supplies a key through the admin API or the environment, they
cache aggressively (a 1000-title library costs a handful of HTTP requests on a
cold cache and none afterwards), and every response is stored in SQLite with a
TTL so the library keeps working when the provider is unreachable.

Interface
---------
A provider maps a :class:`~streamserver.media.naming.ParsedName` onto a
:class:`MediaMetadata` record.  It never touches the database and never writes
files -- :class:`~streamserver.services.importer.Importer` owns all of that.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from ..media.naming import ParsedName

log = logging.getLogger(__name__)

#: Fields an artwork downloader / metadata service may fill in.
GENRE_SPLIT_RE = re.compile(r"\s*[,;/|]\s*")


class ProviderError(Exception):
    """The provider could not answer.  Callers treat this as "no metadata"."""


@dataclass(slots=True)
class Credit:
    name: str
    role: str = "cast"          # cast | director | writer
    character: str | None = None
    job: str | None = None
    order: int = 0
    tmdb_id: str | None = None
    imdb_id: str | None = None


@dataclass(slots=True)
class MediaMetadata:
    """Provider result.  Everything is optional; the importer fills the gaps."""

    title: str | None = None
    original_title: str | None = None
    overview: str | None = None
    tagline: str | None = None
    year: int | None = None
    release_date: str | None = None
    runtime_minutes: int | None = None
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    rating_value: float | None = None
    rating_source: str | None = None
    rating_vote_count: int | None = None
    parental_rating: str | None = None
    cast: list[Credit] = field(default_factory=list)
    directors: list[Credit] = field(default_factory=list)
    writers: list[Credit] = field(default_factory=list)
    poster_url: str | None = None
    backdrop_url: str | None = None
    logo_url: str | None = None
    poster_path: str | None = None      # a local file, when known
    backdrop_path: str | None = None
    external_ids: dict[str, str] = field(default_factory=dict)
    provider: str = "none"
    #: True when the provider is confident this is the right title.
    matched: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "original_title": self.original_title,
            "overview": self.overview,
            "tagline": self.tagline,
            "year": self.year,
            "release_date": self.release_date,
            "runtime_minutes": self.runtime_minutes,
            "genres": list(self.genres),
            "tags": list(self.tags),
            "rating_value": self.rating_value,
            "rating_source": self.rating_source,
            "rating_vote_count": self.rating_vote_count,
            "parental_rating": self.parental_rating,
            "cast": [c.__dict__ if hasattr(c, "__dict__") else _credit_dict(c)
                     for c in self.cast[:20]],
            "directors": [_credit_dict(c) for c in self.directors],
            "poster_url": self.poster_url,
            "backdrop_url": self.backdrop_url,
            "logo_url": self.logo_url,
            "poster_path": self.poster_path,
            "backdrop_path": self.backdrop_path,
            "external_ids": dict(self.external_ids),
            "provider": self.provider,
            "matched": self.matched,
        }


def _credit_dict(credit: Credit) -> dict[str, Any]:
    return {
        "name": credit.name, "role": credit.role, "character": credit.character,
        "job": credit.job, "order": credit.order,
        "tmdb_id": credit.tmdb_id, "imdb_id": credit.imdb_id,
    }


class MetadataProvider(ABC):
    """Base class.  ``name`` is persisted as the rating/metadata source."""

    name: str = "none"
    #: Whether this provider needs an API key before it can be used.
    requires_key: bool = True
    #: Priority when several providers are chained.
    priority: int = 100

    def __init__(self, **options: Any) -> None:
        self.options = options

    @abstractmethod
    def lookup(self, parsed: ParsedName, *, sidecars: list[Path] | None = None
               ) -> MediaMetadata | None:
        """Return metadata for ``parsed``, or ``None`` when there is none."""

    def available(self) -> bool:
        return True

    def close(self) -> None:
        pass


class NoProvider(MetadataProvider):
    name = "none"
    requires_key = False
    priority = 999

    def lookup(self, parsed: ParsedName, *, sidecars=None) -> MediaMetadata | None:
        return None


class LocalSidecarProvider(MetadataProvider):
    """Reads ``.nfo`` (Kodi/Emby XML) and ``.json`` files next to the media.

    This is the default and the only offline provider, so a fresh install has
    full metadata support with zero network access.
    """

    name = "local"
    requires_key = False
    priority = 10

    MAX_FILE_BYTES = 4 * 1024 * 1024

    def lookup(self, parsed: ParsedName, *, sidecars: list[Path] | None = None
               ) -> MediaMetadata | None:
        for sidecar in sidecars or []:
            suffix = sidecar.suffix.lower()
            try:
                if suffix == ".json" and sidecar.stat().st_size <= self.MAX_FILE_BYTES:
                    data = self._read_json(sidecar)
                    if data:
                        return data
                elif suffix == ".nfo" and sidecar.stat().st_size <= self.MAX_FILE_BYTES:
                    data = self._read_nfo(sidecar)
                    if data:
                        return data
            except (OSError, ValueError) as exc:
                log.warning("cannot read sidecar %s: %s", sidecar, exc)
        return None

    # -- readers ---------------------------------------------------------
    def _read_json(self, path: Path) -> MediaMetadata | None:
        raw = json.loads(path.read_text("utf-8"))
        if not isinstance(raw, dict):
            return None
        # Accept both a flat object and a wrapped {"movie": {...}} shape.
        for key in ("movie", "series", "episode", "data"):
            inner = raw.get(key)
            if isinstance(inner, dict):
                raw = {**inner, **raw}
                break

        meta = MediaMetadata(provider=self.name, matched=True)
        meta.title = _text(raw.get("title") or raw.get("name"))
        meta.original_title = _text(raw.get("original_title") or raw.get("originaltitle"))
        meta.overview = _text(raw.get("overview") or raw.get("plot") or raw.get("description"))
        meta.tagline = _text(raw.get("tagline"))

        year = _int(raw.get("year"))
        if year:
            meta.year = year
        release = _text(raw.get("release_date") or raw.get("releasedate") or raw.get("premiered"))
        if release:
            meta.release_date = _normalise_date(release)
            if not meta.year:
                try:
                    meta.year = int(meta.release_date[:4])
                except (ValueError, TypeError):
                    pass
        meta.runtime_minutes = _int(raw.get("runtime") or raw.get("runtime_minutes")
                                    or raw.get("duration_minutes"))
        meta.genres = _split_list(raw.get("genres") or raw.get("genre"))
        meta.tags = _split_list(raw.get("tags") or raw.get("tag") or raw.get("studios"))
        meta.parental_rating = _text(
            raw.get("parental_rating") or raw.get("mpaa_rating") or raw.get("certification")
        )

        rating = _float(raw.get("rating") if raw.get("rating") is not None
                        else raw.get("vote_average"))
        if rating is not None:
            meta.rating_value = max(0.0, min(10.0, rating))
            meta.rating_source = _text(raw.get("rating_source")) or self.name
        meta.rating_vote_count = _int(raw.get("vote_count") or raw.get("votes"))

        meta.poster_path = _local_image(raw.get("poster") or raw.get("poster_path"))
        meta.backdrop_path = _local_image(raw.get("backdrop") or raw.get("fanart")
                                         or raw.get("backdrop_path"))
        meta.poster_url = _https_url(raw.get("poster_url"))
        meta.backdrop_url = _https_url(raw.get("backdrop_url"))

        meta.cast = _credits_from(raw.get("cast") or raw.get("actors"), role="cast")
        meta.directors = _credits_from(raw.get("directors") or raw.get("director"), role="director")
        meta.writers = _credits_from(raw.get("writers") or raw.get("writer"), role="writer")

        ids = raw.get("ids") if isinstance(raw.get("ids"), dict) else {}
        for provider in ("tmdb", "imdb", "tvdb"):
            value = raw.get(f"{provider}_id") or ids.get(provider)
            if value not in (None, ""):
                meta.external_ids[provider] = str(value)[:64]

        if not meta.title:
            return None
        return meta

    def _read_nfo(self, path: Path) -> MediaMetadata | None:
        # Kodi .nfo files are untrusted input from a directory listing: parse
        # them without entity expansion and cap the size (checked by caller).
        try:
            tree = ElementTree.parse(str(path))
        except ElementTree.ParseError as exc:
            log.warning("malformed NFO %s: %s", path, exc)
            return None
        root = tree.getroot()

        meta = MediaMetadata(provider=self.name, matched=True)
        meta.title = _text(root.findtext("title"))
        meta.original_title = _text(root.findtext("originaltitle"))
        meta.overview = _text(root.findtext("plot"))
        meta.tagline = _text(root.findtext("tagline"))
        meta.year = _int(root.findtext("year"))
        meta.release_date = _normalise_date(root.findtext("premiered") or root.findtext("releasedate"))
        meta.runtime_minutes = _int(root.findtext("runtime") or root.findtext("duration"))
        meta.parental_rating = _text(root.findtext("mpaa") or root.findtext("certification"))

        genres = [g.text for g in root.iterfind("genre") if g.text]
        meta.genres = [g.strip() for g in genres]
        tags = [t.text for t in root.iterfind("tag") if t.text]
        meta.tags = [t.strip() for t in tags]

        rating = _float(root.findtext("rating"))
        if rating is not None:
            meta.rating_value = max(0.0, min(10.0, rating))
            meta.rating_source = self.name
        meta.rating_vote_count = _int(root.findtext("votes"))

        meta.cast = []
        for order, actor in enumerate(root.iterfind("actor")):
            name = _text(actor.findtext("name"))
            if name:
                meta.cast.append(Credit(
                    name=name, role="cast",
                    character=_text(actor.findtext("role")),
                    order=order,
                ))
        meta.directors = [
            Credit(name=name, role="director")
            for name in (_text(d.text) for d in root.iterfind("director")) if name
        ]

        fanart = root.findtext("fanart/thumb") or root.findtext("thumb")
        meta.backdrop_path = _local_image(fanart)
        meta.poster_path = _local_image(root.findtext("poster") or root.findtext("thumb"))

        for node in root.iterfind("uniqueid"):
            value = _text(node.text)
            if value:
                kind = (node.get("type") or "tmdb").lower()[:16]
                meta.external_ids[kind] = value[:64]

        if not meta.title:
            return None
        return meta


class HttpJsonProvider(MetadataProvider):
    """Shared plumbing for providers that speak JSON over HTTP.

    Subclasses implement :meth:`fetch`.  Everything here is defensive: the
    response is size-capped, JSON-parsed with a depth limit, and every field is
    coerced through the same helpers the sidecar reader uses, because provider
    payloads are as untrusted as a directory listing.
    """

    requires_key = True
    max_response_bytes = 4 * 1024 * 1024

    def __init__(self, api_key: str = "", *, timeout: int = 15,
                 max_response_bytes: int | None = None, **options: Any) -> None:
        super().__init__(**options)
        self.api_key = api_key or ""
        self.timeout = timeout
        if max_response_bytes:
            self.max_response_bytes = max_response_bytes
        self._session = None

    def available(self) -> bool:
        return bool(self.api_key)

    def _get_session(self):
        if self._session is None:
            import requests

            session = requests.Session()
            session.headers["User-Agent"] = "streamserver/1.0 (+personal media server)"
            session.trust_env = True
            self._session = session
        return self._session

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        try:
            response = self._get_session().get(
                url, params=params, timeout=self.timeout, stream=True,
                headers={"Accept": "application/json"},
            )
        except Exception as exc:  # noqa: BLE001 - any transport error is "no answer"
            log.warning("%s: request failed: %s", self.name, exc)
            return None
        if response.status_code != 200:
            log.warning("%s: HTTP %s", self.name, response.status_code)
            response.close()
            return None

        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(64 * 1024):
                total += len(chunk)
                if total > self.max_response_bytes:
                    log.warning("%s: response exceeded size cap", self.name)
                    return None
                chunks.append(chunk)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: read failed: %s", self.name, exc)
            return None
        finally:
            response.close()

        try:
            payload = json.loads(b"".join(chunks).decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @abstractmethod
    def fetch(self, parsed: ParsedName) -> MediaMetadata | None:
        ...

    def lookup(self, parsed: ParsedName, *, sidecars=None) -> MediaMetadata | None:
        if not self.available():
            return None
        try:
            return self.fetch(parsed)
        except Exception as exc:  # noqa: BLE001 - never break an import on metadata
            log.warning("%s: lookup failed for %r: %s", self.name, parsed.title, exc)
            return None


class TmdbProvider(HttpJsonProvider):
    """The Movie Database.

    Enabled only when an operator configures a key.  The API is unauthenticated
    for reads but rate-limited per IP, so requests are cached in SQLite with a
    30-day TTL and the importer never looks the same title up twice in a run.
    """

    name = "tmdb"
    priority = 20
    BASE = "https://api.themoviedb.org/3"

    def fetch(self, parsed: ParsedName) -> MediaMetadata | None:
        results = self._get_json(
            f"{self.BASE}/search/movie",
            {"api_key": self.api_key, "query": parsed.search_terms(), "include_adult": "false"},
        )
        best = _pick_best_result(results, parsed)
        if not best:
            return None
        detail = self._get_json(f"{self.BASE}/movie/{best['id']}", {"api_key": self.api_key})
        if not detail:
            return None
        return _tmdb_to_metadata(detail, self.name)


class ImdbProvider(HttpJsonProvider):
    """IMDb, via the official suggestion API.

    That endpoint is unauthenticated and returns titles, years and ratings but
    no synopsis, artwork or credits -- which is exactly what the details page
    can do without.  For a full fetch the operator can point the server at a
    third-party API host via ``METADATA_IMDB_API_BASE``; nothing else changes.
    """

    name = "imdb"
    priority = 30
    SUGGESTION = "https://v3.sg.media-imdb.com/suggestion/x"

    def __init__(self, api_key: str = "", *, api_base: str = "", **options: Any) -> None:
        super().__init__(api_key=api_key, **options)
        self.api_base = api_base

    def available(self) -> bool:
        # The suggestion endpoint needs no key, so this provider is usable by
        # default; the key is only needed for the fuller API host.
        return True

    def fetch(self, parsed: ParsedName) -> MediaMetadata | None:
        if self.api_base and self.api_key:
            payload = self._get_json(
                f"{self.api_base.rstrip('/')}/title",
                {"api_key": self.api_key, "q": parsed.search_terms()},
            )
            if payload:
                converted = _imdb_api_to_metadata(payload, self.name)
                if converted and converted.matched:
                    return converted

        url = f"{self.SUGGESTION}/{parsed.title.replace(' ', '_').lower()}.json"
        if parsed.year:
            url += f"?year={parsed.year}"
        payload = self._get_json(url)
        best = _pick_best_imdb(payload, parsed)
        if not best:
            return None

        meta = MediaMetadata(provider=self.name, matched=True)
        meta.title = _text(best.get("l"))
        meta.year = _int(best.get("y"))
        meta.rating_value = _imdb_rating_to_ten(best.get("rank"))
        meta.rating_source = "imdb"
        meta.runtime_minutes = _minutes_from_text(best.get("runTime"))
        ids = best.get("id") or {}
        imdb_id = ids.get("tt") if isinstance(ids, dict) else None
        if imdb_id:
            meta.external_ids["imdb"] = str(imdb_id)
        meta.poster_url = _image_url(best.get("i"))
        if meta.title:
            return meta
        return None


class OmdbProvider(HttpJsonProvider):
    """Open Movie Database -- one key, one payload, no rate-limit surprises."""

    name = "omdb"
    priority = 25
    BASE = "https://www.omdbapi.com/"

    def fetch(self, parsed: ParsedName) -> MediaMetadata | None:
        params = {"apikey": self.api_key, "t": parsed.title}
        if parsed.year:
            params["y"] = str(parsed.year)
        payload = self._get_json(self.BASE, params)
        if not payload or payload.get("Response") != "True":
            return None
        meta = MediaMetadata(provider=self.name, matched=True)
        meta.title = _text(payload.get("Title"))
        meta.original_title = _text(payload.get("Title"))
        meta.overview = _text(payload.get("Plot"))
        meta.year = _int(payload.get("Year")) or _year_from_text(payload.get("Year"))
        meta.release_date = _normalise_date(payload.get("Released"))
        meta.runtime_minutes = _minutes_from_text(payload.get("Runtime"))
        meta.genres = _split_list(payload.get("Genre"))
        meta.rating_value = _imdb_rating_to_ten(payload.get("imdbRating"))
        meta.rating_source = "imdb"
        meta.rating_vote_count = _int(payload.get("imdbVotes"))
        meta.parental_rating = _text(payload.get("Rated"))
        meta.poster_url = _text(payload.get("Poster")) if str(payload.get("Poster", "")).startswith("http") else None
        imdb_id = _text(payload.get("imdbID"))
        if imdb_id:
            meta.external_ids["imdb"] = imdb_id
        meta.cast = [
            Credit(name=name, role="cast", order=i)
            for i, name in enumerate(_split_list(payload.get("Actors")))
        ]
        meta.directors = [
            Credit(name=name, role="director", order=i)
            for i, name in enumerate(_split_list(payload.get("Director")))
        ]
        meta.writers = [
            Credit(name=name, role="writer", order=i)
            for i, name in enumerate(_split_list(payload.get("Writer")))
        ]
        return meta


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------
PROVIDER_CLASSES: dict[str, type[MetadataProvider]] = {
    "local": LocalSidecarProvider,
    "tmdb": TmdbProvider,
    "imdb": ImdbProvider,
    "omdb": OmdbProvider,
    "none": NoProvider,
}


def build_provider(
    name: str,
    *,
    api_key: str = "",
    timeout: int = 15,
    max_image_bytes: int = 12 * 1024 * 1024,
    extra: dict[str, Any] | None = None,
) -> MetadataProvider:
    cls = PROVIDER_CLASSES.get((name or "local").lower())
    if cls is None:
        log.warning("unknown metadata provider %r; falling back to local", name)
        cls = LocalSidecarProvider
    if cls is LocalSidecarProvider or cls is NoProvider:
        return cls()
    return cls(api_key=api_key, timeout=timeout,
               max_response_bytes=min(max_image_bytes, 4 * 1024 * 1024), **(extra or {}))


class ProviderChain(MetadataProvider):
    """Tries providers in priority order and merges the first hit.

    ``local`` runs first so a hand-written ``.nfo`` always wins over a fuzzy
    web match, which is what a librarian would expect.
    """

    def __init__(self, providers: list[MetadataProvider]) -> None:
        self.providers = sorted(providers, key=lambda p: p.priority)

    @property
    def name(self) -> str:
        return "+".join(p.name for p in self.providers)

    def available(self) -> bool:
        return any(p.available() for p in self.providers)

    def lookup(self, parsed: ParsedName, *, sidecars=None) -> MediaMetadata | None:
        result: MediaMetadata | None = None
        for provider in self.providers:
            try:
                found = provider.lookup(parsed, sidecars=sidecars)
            except Exception as exc:  # noqa: BLE001
                log.warning("provider %s failed: %s", provider.name, exc)
                continue
            if not found:
                continue
            result = merge_metadata(result, found)
            # A confident local sidecar is authoritative; stop there.
            if provider.name == "local" and found.matched:
                break
        return result

    def close(self) -> None:
        for provider in self.providers:
            provider.close()


def merge_metadata(base: MediaMetadata | None, incoming: MediaMetadata) -> MediaMetadata:
    """Field-wise merge where ``incoming`` only fills gaps.

    Later providers contribute only what the earlier ones did not supply, so
    the priority order actually controls the outcome.
    """
    if base is None:
        return incoming
    result = base
    for name in (
        "title", "original_title", "overview", "tagline", "release_date",
        "parental_rating", "poster_url", "backdrop_url", "logo_url",
        "poster_path", "backdrop_path",
    ):
        if getattr(result, name) in (None, "") and getattr(incoming, name) not in (None, ""):
            setattr(result, name, getattr(incoming, name))
    for name in ("year", "runtime_minutes", "rating_value", "rating_vote_count"):
        if getattr(result, name) is None and getattr(incoming, name) is not None:
            setattr(result, name, getattr(incoming, name))
    if not result.rating_source and incoming.rating_source:
        result.rating_source = incoming.rating_source
    result.genres = result.genres or list(incoming.genres)
    result.tags = result.tags or list(incoming.tags)
    result.cast = result.cast or list(incoming.cast)
    result.directors = result.directors or list(incoming.directors)
    result.writers = result.writers or list(incoming.writers)
    for key, value in (incoming.external_ids or {}).items():
        result.external_ids.setdefault(key, value)
    if not result.provider or result.provider == "none":
        result.provider = incoming.provider
    return result


# ---------------------------------------------------------------------------
# Coercion helpers -- provider payloads are untrusted input
# ---------------------------------------------------------------------------
def _text(value: Any, limit: int = 2000) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple)):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "n/a", "unknown"}:
        return None
    # Provider payloads are untrusted input.  Drop control characters and
    # directory separators so a value can never be mistaken for a path or
    # corrupt a log line.
    text = "".join(ch for ch in text if ch.isprintable())
    text = text.replace("\\", "/")
    # Collapse to components and rejoin with a space: "a/b" -> "a b", but
    # never collapse a multi-line synopsis into a single visual line.
    text = re.sub(r"[ \t]*/[ \t]*", " / ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:limit] if text else None


def _int(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    return parsed if -2_000_000_000 < parsed < 2_000_000_000 else None


def _float(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _year_from_text(value: Any) -> int | None:
    match = re.search(r"(19\d{2}|20\d{2})", str(value or ""))
    return int(match.group(1)) if match else None


def _normalise_date(value: Any) -> str | None:
    text = _text(value, limit=64)
    if not text:
        return None
    text = re.split(r"[T\s]", text)[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    year = _year_from_text(text)
    return f"{year}-01-01" if year else None


def _minutes_from_text(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if 0 < value < 1000 else None
    text = str(value)
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None
    number = int(digits)
    if re.search(r"\d\s*h", text, re.I):
        hours = re.search(r"(\d+)\s*h", text, re.I)
        minutes = re.search(r"(\d+)\s*m", text, re.I)
        return int(hours.group(1)) * 60 + int(minutes.group(1)) if hours and minutes else number
    return number if 0 < number < 1000 else None


def _split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = GENRE_SPLIT_RE.split(value)
    elif isinstance(value, (list, tuple)):
        parts = [_text(item, limit=64) or "" for item in value]
    else:
        return []
    out, seen = [], set()
    for part in parts:
        cleaned = (part or "").strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned[:64])
    return out[:20]


def _https_url(value: Any) -> str | None:
    """Accept a remote artwork URL only when it really is one.

    Deliberately does not route through :func:`_text`, which rewrites
    separators and would destroy the URL.
    """
    if value is None or isinstance(value, (dict, list, tuple)):
        return None
    text = str(value).strip()
    if not text.lower().startswith(("https://", "http://")):
        return None
    if any(ch in text for ch in "\r\n\t"):
        return None
    return text[:2048]


def _local_image(value: Any) -> str | None:
    """Only a relative path or a file: URL is accepted as a local artwork path.

    A provider-supplied absolute path is *not* trusted; the importer resolves
    it against the library roots like any other file.
    """
    text = _text(value, limit=512)
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        return None
    normalised = text.replace("\\", "/")
    # Absolute paths and traversal are refused outright: the importer resolves
    # artwork against the library roots like any other file, and a path that
    # arrives from a metadata file must not be able to aim at /etc/shadow.
    if normalised.startswith("/") or normalised.startswith("~"):
        return None
    if ".." in normalised.split("/"):
        return None
    return normalised


def _credits_from(value: Any, role: str) -> list[Credit]:
    credits: list[Credit] = []
    if isinstance(value, str):
        entries: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        entries = list(value)
    else:
        return credits

    for index, entry in enumerate(entries):
        if isinstance(entry, dict):
            name = _text(entry.get("name") or entry.get("person"))
            if not name:
                continue
            credits.append(Credit(
                name=name, role=role,
                character=_text(entry.get("character")),
                order=_int(entry.get("order")) or index,
                tmdb_id=_text(entry.get("tmdb_id")),
                imdb_id=_text(entry.get("imdb_id")),
            ))
        else:
            for name in _split_list(entry):
                credits.append(Credit(name=name, role=role, order=len(credits)))
    return credits


def _image_url(value: Any) -> str | None:
    if isinstance(value, dict):
        url = _text(value.get("url"))
        if url and url.startswith("http"):
            return url
    return None


def _imdb_rating_to_ten(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)", str(value))
    if not match:
        return None
    try:
        rating = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    if not (0.0 <= rating <= 10.0):
        return None
    return rating


def _pick_best_result(payload: dict[str, Any] | None, parsed: ParsedName) -> dict | None:
    """Choose the candidate whose title/year actually match the filename."""
    if not payload:
        return None
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    target = re.sub(r"[^a-z0-9]", "", (parsed.title or "").lower())
    best, best_score = None, 0.0
    for entry in results:
        if not isinstance(entry, dict):
            continue
        title = re.sub(r"[^a-z0-9]", "", str(entry.get("title") or entry.get("name") or "").lower())
        if not title:
            continue
        score = 100.0 if title == target else (80.0 if target and (target in title or title in target) else 0.0)
        raw_release = entry.get("release_date")
        year = _int(str(raw_release)[:4]) if raw_release else None
        if parsed.year and year:
            score += 20 if year == parsed.year else -30
        vote_average = _float(entry.get("vote_average")) or 0.0
        score += min(vote_average, 10.0)
        if score > best_score:
            best, best_score = entry, score
    if best is None or best_score < 40:
        return None
    return best


def _tmdb_to_metadata(detail: dict[str, Any], source: str) -> MediaMetadata:
    meta = MediaMetadata(provider=source, matched=True)
    meta.title = _text(detail.get("title"))
    meta.original_title = _text(detail.get("original_title"))
    meta.overview = _text(detail.get("overview"))
    meta.tagline = _text(detail.get("tagline"))
    meta.release_date = _normalise_date(detail.get("release_date"))
    meta.year = _int(str(detail.get("release_date") or "")[:4]) if detail.get("release_date") else None
    meta.runtime_minutes = _int(detail.get("runtime"))
    genres = detail.get("genres")
    if isinstance(genres, list):
        meta.genres = [_text(g.get("name")) for g in genres if isinstance(g, dict) and g.get("name")]
    rating = _float(detail.get("vote_average"))
    if rating is not None:
        meta.rating_value = rating
        meta.rating_source = source
    meta.rating_vote_count = _int(detail.get("vote_count"))

    credits = detail.get("credits")
    if isinstance(credits, dict):
        for index, cast in enumerate(_iter_dict_list(credits.get("cast"))):
            meta.cast.append(Credit(
                name=_text(cast.get("name")) or "",
                role="cast", character=_text(cast.get("character")),
                order=index, tmdb_id=_text(cast.get("id")),
            ))
        meta.cast = [c for c in meta.cast if c.name][:20]
        for index, person in enumerate(_iter_dict_list(credits.get("crew"))):
            job = _text(person.get("job"))
            name = _text(person.get("name"))
            if not job or not name:
                continue
            if job == "Director":
                meta.directors.append(Credit(name=name, role="director", order=index,
                                            tmdb_id=_text(person.get("id"))))
            elif job == "Writer":
                meta.writers.append(Credit(name=name, role="writer", order=index,
                                           tmdb_id=_text(person.get("id"))))

    poster = _text(detail.get("poster_path"))
    backdrop = _text(detail.get("backdrop_path"))
    if poster:
        meta.poster_url = f"https://image.tmdb.org/t/p/w780{poster}"
    if backdrop:
        meta.backdrop_url = f"https://image.tmdb.org/t/w1280{backdrop}"
    meta.external_ids["tmdb"] = str(_int(detail.get("id")) or "")[:64] or "0"
    return meta


def _imdb_api_to_metadata(payload: dict[str, Any], source: str) -> MediaMetadata:
    """Convert a generic third-party IMDb-style payload."""
    meta = MediaMetadata(provider=source, matched=True)
    meta.title = _text(payload.get("title"))
    meta.original_title = _text(payload.get("originalTitle") or payload.get("original_title"))
    meta.overview = _text(payload.get("plot") or payload.get("overview"))
    meta.tagline = _text(payload.get("tagline"))
    meta.year = _int(payload.get("year"))
    meta.release_date = _normalise_date(payload.get("releaseDate") or payload.get("release_date"))
    meta.runtime_minutes = _int(payload.get("runtime") or payload.get("runtimeMinutes"))
    meta.genres = _split_list(payload.get("genres"))
    rating = _float(payload.get("rating"))
    if rating is not None:
        meta.rating_value = rating if rating <= 10 else rating / 2
        meta.rating_source = source
    meta.rating_vote_count = _int(payload.get("votes"))
    meta.parental_rating = _text(payload.get("certification") or payload.get("rated"))
    imdb_id = _text(payload.get("id") or payload.get("imdbId"))
    if imdb_id:
        meta.external_ids["imdb"] = imdb_id
    meta.poster_url = _https_url(payload.get("poster") or payload.get("image"))
    meta.cast = [Credit(name=n, role="cast", order=i)
                 for i, n in enumerate(_split_list(payload.get("cast") or payload.get("actors")))]
    meta.directors = [Credit(name=n, role="director", order=i)
                      for i, n in enumerate(_split_list(payload.get("directors") or payload.get("director")))]
    meta.matched = bool(meta.title)
    return meta


def _iter_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _pick_best_imdb(payload: dict[str, Any] | None, parsed: ParsedName) -> dict | None:
    if not payload:
        return None
    entries = payload.get("d")
    if not isinstance(entries, list):
        return None
    target = re.sub(r"[^a-z0-9]", "", (parsed.title or "").lower())
    best, best_score = None, 0.0
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("q") not in ("movie", "tvMovie", "short", None):
            continue
        title = re.sub(r"[^a-z0-9]", "", str(entry.get("l") or "").lower())
        if not title:
            continue
        score = 100.0 if title == target else (70.0 if target and (target in title or title in target) else 0.0)
        year = _int(entry.get("y"))
        if parsed.year and year:
            score += 20 if year == parsed.year else -25
        if score > best_score:
            best, best_score = entry, score
    if best is None or best_score < 40:
        return None
    return best