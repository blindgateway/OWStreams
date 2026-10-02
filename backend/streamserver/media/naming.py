"""Filename parsing.

Real libraries are full of ``Dune.Part.Two.2024.2160p.UHD.BluRay.x265-GROUP``,
``[SubsPlease] Frieren - 12 (1080p) [ABCD1234].mkv`` and ``Amelie.2001.FRENCH.
1080p.mkv``.  This module turns any of those into
``{title, year, quality, release, edition, part, audio, group, language}``
with no external dependency, so a metadata lookup has something to search with
even before a sidecar or a provider is consulted.

Strategy: release names are written *front-to-back* -- the title comes first and
the technical markers trail -- so extraction works from the front backwards,
and every rule is anchored so it only removes tokens it is confident about.
That is what keeps real title words safe: "Se7en", "1917", "Alien",
"3.10 to Yuma" and "Frieren - Beyond Journey's End" all survive, while
"1080p", "BluRay", "x265-GRP" and "DDP5.1" do not leak into a title.

Pure functions, no I/O -- the code most likely to be wrong on real filenames is
also the code most worth testing cheaply.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

VIDEO_SUFFIXES = (
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".mpg", ".mpeg", ".m2ts", ".ts", ".ogv", ".divx", ".vob", ".3gp", ".m2v",
)

QUALITY_PATTERNS = (
    ("2160p", re.compile(r"(?:2160p|4k|uhd|3840\s*[x×]\s*2160)", re.I)),
    ("1440p", re.compile(r"(?:1440p|2560\s*[x×]\s*1440|2k)", re.I)),
    ("1080p", re.compile(r"(?:1080p|1920\s*[x×]\s*1080|full\s*hd|fhd)", re.I)),
    ("720p", re.compile(r"(?:720p|1280\s*[x×]\s*720|\bhd\b)", re.I)),
    ("480p", re.compile(r"(?:480p|848\s*[x×]\s*480|\bsd\b)", re.I)),
)

RELEASE_TYPES = (
    ("remux", re.compile(r"\bremux\b", re.I)),
    ("bluray", re.compile(r"\b(?:blu[\s._-]?ray|bdrip|bdremux|brrip)\b", re.I)),
    ("webdl", re.compile(r"\b(?:web[\s._-]?dl|webdl|web[\s._-]?rip|webrip)\b", re.I)),
    ("web", re.compile(r"\bweb\b", re.I)),
    ("hdtv", re.compile(r"\b(?:hdtv|pdtv)\b", re.I)),
    ("dvd", re.compile(r"\b(?:dvd[rs]?rip|dvd[rs]?scr|dvd)\b", re.I)),
    ("cam", re.compile(r"\b(?:camrip|hdcam|cam|telesync|telecine)\b", re.I)),
)

AUDIO_PATTERNS = (
    ("truehd", re.compile(r"\btruehd\b", re.I)),
    ("atmos", re.compile(r"\batmos\b", re.I)),
    ("dtshd", re.compile(r"\bdts[\s._-]?hd\b", re.I)),
    ("dts-x", re.compile(r"\bdts[\s._-]?x\b", re.I)),
    ("dts", re.compile(r"\bdts\b", re.I)),
    ("eac3", re.compile(r"\b(?:e[\s._-]?ac[\s._-]?3|dd\+?|ddp\d*)\b", re.I)),
    ("ac3", re.compile(r"\b(?:ac[\s._-]?3|dd\d?)\b", re.I)),
    ("flac", re.compile(r"\bflac\b", re.I)),
    ("opus", re.compile(r"\bopus\b", re.I)),
    ("aac", re.compile(r"\baac\b", re.I)),
    ("mp3", re.compile(r"\bmp3\b", re.I)),
)

#: Language hints mapped to the ISO codes used across the schema.
LANGUAGE_HINTS = (
    ("ja", re.compile(r"\b(?:jpn|jap|jp|japanese|subless)\b", re.I)),
    ("en", re.compile(r"\b(?:eng|english|englishsubbed)\b", re.I)),
    ("de", re.compile(r"\b(?:ger|german|deutsch)\b", re.I)),
    ("fr", re.compile(r"\b(?:fre|french|francais|vff|vfq|truefrench)\b", re.I)),
    ("es", re.compile(r"\b(?:spa|spanish|espanol|castellano)\b", re.I)),
    ("it", re.compile(r"\b(?:ita|italian)\b", re.I)),
    ("pt", re.compile(r"\b(?:por|portuguese|ptbr)\b", re.I)),
    ("ru", re.compile(r"\b(?:rus|russian)\b", re.I)),
    ("ko", re.compile(r"\b(?:kor|korean)\b", re.I)),
    ("zh", re.compile(r"\b(?:chi|chinese|mandarin|cantonese)\b", re.I)),
    ("hi", re.compile(r"\b(?:hin|hindi)\b", re.I)),
    ("nl", re.compile(r"\b(?:dut|dutch)\b", re.I)),
    ("pl", re.compile(r"\b(?:pol|polish)\b", re.I)),
    ("sv", re.compile(r"\b(?:swe|swedish)\b", re.I)),
    ("ta", re.compile(r"\b(?:tam|tamil)\b", re.I)),
    ("te", re.compile(r"\b(?:tel|telugu)\b", re.I)),
)

#: Never the first word of a title in a release name.
MARKER_WORDS = (
    r"subsplease|subsp|animesquad|assembler"
    r"|blu[\s._-]?ray|bdrip|bdremux|remux"
    r"|web[\s._-]?dl|webdl|web[\s._-]?rip|webrip|web"
    r"|hdtv|pdtv|dvdrip|dvdscr|dvd|hdrip|camrip|hdcam|cam|telesync|telecine"
    r"|2160p|1440p|1080p|720p|480p|4k|2k|8k|uhd|full\s*hd|fhd|hd|sd"
    r"|hdr10\+?|hdr|sdr|dolby[\s._-]?vision|dovi|dv|10[\s._-]?bit|8[\s._-]?bit"
    r"|x264|x265|h\.?264|h\.?265|hevc|avc|vc-?1|mpeg-?[24]"
    r"|aac\d*|ac[\s._-]?3|eac3|dd\+?\d*|ddp\d*|truehd|atmos|flac|opus|mp3|lpcm|pcm"
    r"|dts(?:[\s._-]?(?:hd|x|ma|es))?"
    r"|proper|repack|extended|unrated|remastered|remaster|limited|imax|3d|sbs|hsbs"
    r"|directors?[\s._-]?cut|theatrical|final[\s._-]?cut|ultimate[\s._-]?edition"
    r"|uncensored|uncen|censored|complete|batch"
    r"|multi(?:[\s._-]?audio)?|dual(?:[\s._-]?audio)?|stereo|5\.1|7\.1"
    r"|french|german|english|italian|spanish|portuguese|russian|japanese|korean"
    r"|hindi|dutch|polish|swedish|tamil|telugu"
    r"|atmos|dubbed|subbed|subs?\b|engsub|\x00"
)
_MARKER_RE = re.compile(MARKER_WORDS, re.I)
_MARKER_SET = frozenset(
    re.sub(r"[\s._\\+\\-]+", "", alt) for alt in MARKER_WORDS.split("|")
)
_MARKER_ANCHORED = re.compile(r"\s+(?:" + MARKER_WORDS + r")(?!\w)[\s._-]*", re.I)

#: Strict release-group suffix: "-x265-GRP", "-SPARKS".  Must contain a letter
#: so that "the-thing-1982" is not read as group "1982".
GROUP_RE = re.compile(r"-([A-Za-z0-9_]*[A-Za-z][A-Za-z0-9_.]{1,19})$")
#: Looser form that also allows spaces ("-C Classics"), accepted only when the
#: token before the dash is a release marker -- otherwise "Dune-Part Two" would
#: lose a real title word.
GROUP_LOOSE_RE = re.compile(r"-([A-Za-z][A-Za-z0-9_. ]{1,18})$")
#: Anime release tags carry no dash: "... 1080p CR", "... 1080p [horrible]".
GROUP_TRAILING_UPPER_RE = re.compile(r"[\s._-]([A-Z][A-Z0-9]{1,4})$")
#: "Movie (1).mkv", "Movie [2].mkv", "Movie - Copy.mkv".  A *bare* number is not
#: treated as a copy marker, because a trailing "- 12" is an episode number.
COPY_SUFFIX_RE = re.compile(
    r"(?:[\s._-]*(?:\(\s*\d+\s*\)|\[\s*\d+\s*\]"
    r"|-?\s*(?:copy|final|new|bak|backup|old|orig|original|temp|tmp)))\s*$",
    re.I,
)
YEAR_RE = re.compile(r"(?<![\d:])((?:19|20)\d{2})(?![\d])")
SEASON_EPISODE_RE = re.compile(
    r"(?:^|[^\w])[Ss](\d{1,3})[\s._-]*(?:[Ee][Pp]?|[Xx])(\d{1,3})(?:[vV]\d)?"
    r"(?:\s*-\s*(\d{1,3}))?(?![\d])"
)
#: A bare season marker with no episode number ("Breaking.Bad.S01.1080p").
SEASON_ONLY_RE = re.compile(r"(?:^|[\s._-])[Ss](\d{1,3})(?:[\s._-]*(?:1080p|720p|2160p))?(?![\w])")
EDITION_RE = re.compile(
    r"\b(imax|directors?[\s._-]?cut|extended(?:[\s._-]?(?:cut|edition))?|"
    r"unrated|remastered|ultimate[\s._-]?edition|final[\s._-]?cut|"
    r"anniversary|special[\s._-]?edition|theatrical)\b",
    re.I,
)
PART_RE = re.compile(r"\b(?:part|pt|vol|volume|cd|disc|disk)\s*(\d{1,2})\b", re.I)

_SEPARATORS = re.compile(r"[._]+")
_BRACKETS = re.compile(r"[\[\(][^\]\)]*[\]\)]")
#: Audio codec glued to a channel count, in any dot/space form.  Replaced by a
#: sentinel before separator normalisation so "DDP5.1" cannot become "DDP5 1".
_AUDIO_CHANNEL_RE = re.compile(
    r"\b(?:ddp?\d?|dd|ac[\s._-]?3|eac3|aac\d*|truehd|atmos|flac|opus|mp3|lpcm|pcm"
    r"|dts(?:[\s._-]?(?:hd|x|ma|es))?)[\s._]*\d[\s._]*\d\b",
    re.I,
)
#: Bare codec tokens whose digits would otherwise be split off ("H.265").
_CODEC_DIGITS_RE = re.compile(r"\b[Hh][\s._-]?(?:26[45])\b|\b[Vv][\s._-]?\d\b")
_CHANNEL_RE = re.compile(r"\b\d[\s._]*\.\s*\d\b")
_SENTINEL = "\x00"
#: "3.10 to Yuma" -- a digit.digit pair at the very start is part of a title.
_LEADING_NUMBER_TITLE_RE = re.compile(r"^(\d)[._\s]+(\d{2})(?=\s|$)")
#: Channel notation that is not glued to a codec, e.g. "TrueHD 7 1".
_CHANNELS_ONLY_RE = re.compile(r"\b(\d)\s+(\d)\b")

SMALL_WORDS = frozenset({
    "a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or",
    "the", "to", "up", "vs", "von", "der", "die", "das", "und", "la", "le",
})


@dataclass(slots=True)
class ParsedName:
    """Everything inferable from a filename, before any metadata lookup."""

    title: str
    year: int | None = None
    quality: str | None = None
    release: str | None = None
    edition: str | None = None
    part: int | None = None
    audio: str | None = None
    hdr: str | None = None
    bit_depth: int | None = None
    group: str | None = None
    language: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    container: str | None = None
    version_tags: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        if self.season is not None and self.episode is not None:
            return "episode"
        return "movie"

    def search_terms(self) -> str:
        parts = [self.title]
        if self.year:
            parts.append(str(self.year))
        return " ".join(p for p in parts if p)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "year": self.year,
            "quality": self.quality,
            "release": self.release,
            "edition": self.edition,
            "part": self.part,
            "audio": self.audio,
            "hdr": self.hdr,
            "bit_depth": self.bit_depth,
            "group": self.group,
            "language": self.language,
            "season": self.season,
            "episode": self.episode,
            "episode_end": self.episode_end,
            "content_type": self.content_type,
            "container": self.container,
            "version_tags": list(self.version_tags),
        }


def parse_filename(filename: str, *, strip_suffix: bool = True) -> ParsedName:
    """Parse a media filename into structured hints."""
    if not isinstance(filename, str):
        filename = str(filename)
    stem = unicodedata.normalize("NFC", filename).strip()

    container: str | None = None
    if strip_suffix:
        lowered = stem.lower()
        for suffix in VIDEO_SUFFIXES:
            if lowered.endswith(suffix):
                container = suffix.lstrip(".")
                stem = stem[: -len(suffix)]
                break

    stem = COPY_SUFFIX_RE.sub("", stem).strip(" -_.")

    # Season/episode first: extracting them changes the string the release
    # group is looked for in, so doing it the other way round can hide a tag
    # that sits behind "S01E05" ("... 1080p CR").
    season = episode = episode_end = None
    se_match = SEASON_EPISODE_RE.search(stem)
    if se_match:
        season = int(se_match.group(1))
        episode = int(se_match.group(2))
        episode_end = int(se_match.group(3)) if se_match.group(3) else None
        stem = stem[: se_match.start()] + " " + stem[se_match.end():]
    else:
        season_only = SEASON_ONLY_RE.search(stem)
        if season_only:
            season = int(season_only.group(1))
            stem = stem[: season_only.start()] + " " + stem[season_only.end():]

    group: str | None = None
    group_match = GROUP_RE.search(stem)
    if group_match and len(stem) - len(group_match.group(1)) > 8:
        group = group_match.group(1)
        stem = stem[: group_match.start()]
    else:
        group, stem = _extract_loose_group(stem)

    bracket_groups = [b.strip() for b in _BRACKETS.findall(stem)]
    without_brackets = _BRACKETS.sub(" ", stem)

    version_tags: list[str] = []
    scan_space = bracket_groups + [_SEPARATORS.sub(" ", without_brackets)]
    for chunk in scan_space:
        lowered = chunk.lower()
        if not lowered:
            continue
        for needle, tag in (
            ("subsplease", "fansub"), ("subsp", "fansub"),
            ("10bit", "10bit"), ("8bit", "8bit"),
            ("hdr10+", "hdr10+"), ("hdr10plus", "hdr10+"),
            ("dolby vision", "dolby-vision"), ("dovi", "dolby-vision"),
            ("atmos", "atmos"), ("imax", "imax"), ("remastered", "remastered"),
            ("proper", "proper"), ("repack", "repack"), ("uncensored", "uncensored"),
        ):
            if needle in lowered:
                version_tags.append(tag)

    quality = _match_first(without_brackets, QUALITY_PATTERNS)
    release = _match_first(without_brackets, RELEASE_TYPES)
    audio = _match_first(without_brackets, AUDIO_PATTERNS)
    edition_match = EDITION_RE.search(without_brackets)
    edition = edition_match.group(0).strip().lower() if edition_match else None

    part_match = PART_RE.search(without_brackets)
    part = int(part_match.group(1)) if part_match else None

    language = None
    for code, pattern in LANGUAGE_HINTS:
        if pattern.search(stem) or pattern.search(without_brackets):
            language = code
            break

    year = None
    year_matches = YEAR_RE.findall(without_brackets)
    if year_matches:
        year = int(year_matches[-1])

    title = clean_title(without_brackets)

    hdr = None
    if "hdr10+" in version_tags:
        hdr = "HDR10+"
    elif "dolby-vision" in version_tags:
        hdr = "Dolby Vision"
    elif re.search(r"\bhdr\b", without_brackets, re.I) and (quality == "2160p" or year):
        hdr = "HDR10"

    bit_depth = 10 if "10bit" in version_tags else (8 if "8bit" in version_tags else None)

    return ParsedName(
        title=title,
        year=year,
        quality=quality,
        release=release,
        edition=edition,
        part=part,
        audio=audio,
        hdr=hdr,
        bit_depth=bit_depth,
        group=group,
        language=language,
        season=season,
        episode=episode,
        episode_end=episode_end,
        container=container,
        version_tags=sorted(set(version_tags)),
    )


def _last_token(text: str) -> str:
    """Last dot- or space-delimited token, lowercased."""
    parts = [p for p in re.split(r"[\s._-]+", text.strip(" ._-")) if p]
    return parts[-1].lower() if parts else ""


def _extract_loose_group(stem: str) -> tuple[str | None, str]:
    """Pull a release group off the end when the strict rule did not match."""
    loose = GROUP_LOOSE_RE.search(stem)
    if loose:
        candidate = loose.group(1).strip()
        before = stem[: loose.start()]
        # Only when the token immediately before the dash is a release marker.
        head = _last_token(before)
        if head and (_MARKER_RE.fullmatch(head) or head in {"x264", "x265"}):
            if len(before) - len(head) > 3:
                return candidate, before[: -len(head)].rstrip(" ._-")
    upper = GROUP_TRAILING_UPPER_RE.search(stem)
    if upper:
        candidate = upper.group(1)
        before = stem[: upper.start()].rstrip(" ._-")
        head = _last_token(before)
        if candidate.lower() not in SMALL_WORDS and (not head or _MARKER_RE.fullmatch(head)):
            if len(before) > 3:
                return candidate, before
    return None, stem


def _last_token(text: str) -> str:
    """Last dot- or space-delimited token, lowercased."""
    parts = [p for p in re.split(r"[\s._-]+", text.strip(" ._-")) if p]
    return parts[-1].lower() if parts else ""


def _extract_loose_group(stem: str) -> tuple[str | None, str]:
    """Pull a release group off the end when the strict rule did not match."""
    loose = GROUP_LOOSE_RE.search(stem)
    if loose:
        candidate = loose.group(1).strip()
        before = stem[: loose.start()]
        # Only when the token immediately before the dash is a release marker.
        head = _last_token(before)
        if head and (_MARKER_RE.fullmatch(head) or head in {"x264", "x265"}):
            if len(before) - len(head) > 3:
                return candidate, before[: -len(head)].rstrip(" ._-")
    upper = GROUP_TRAILING_UPPER_RE.search(stem)
    if upper:
        candidate = upper.group(1)
        before = stem[: upper.start()].rstrip(" ._-")
        head = _last_token(before)
        if candidate.lower() not in SMALL_WORDS and (not head or _MARKER_RE.fullmatch(head)):
            if len(before) > 3:
                return candidate, before
    return None, stem


def _match_first(text: str, patterns: tuple[tuple[str, re.Pattern], ...]) -> str | None:
    for label, pattern in patterns:
        if pattern.search(text):
            return label
    return None


def clean_title(raw: str) -> str:
    """Reduce a scrambled stem to a plausible title.

    Front-to-back, because that is how these names are written.  Each step is
    anchored so that a word only disappears when the rule is certain about it.
    """
    text = raw
    # Defensive: this function is meant to see a basename.  If a caller passes
    # a full path, keep only the final component so a title can never contain
    # a directory separator, and drop control characters so a title can never
    # corrupt a log line or a UI string.
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(ch for ch in text if ch.isprintable() or ch == " ")
    text = _BRACKETS.sub(" ", text)
    text = COPY_SUFFIX_RE.sub("", text).strip(" -_.")

    # Protect channel notation before separator normalisation splits it.
    text = _AUDIO_CHANNEL_RE.sub(f" {_SENTINEL} ", text)
    text = _CHANNELS_ONLY_RE.sub(f" {_SENTINEL} ", text)
    text = _CODEC_DIGITS_RE.sub(f" {_SENTINEL} ", text)
    text = _SEPARATORS.sub(" ", text)
    text = _LEADING_NUMBER_TITLE_RE.sub(r"\1:\2", text)

    # A leading marker group: "[SubsPlease] Frieren", "WEB-DL Movie Title".
    for _ in range(3):
        stripped = _strip_leading_marker(text)
        if stripped == text:
            break
        text = stripped

    # Leading "10bit", "DDP5.1", "Atmos" -- technical openers.
    text = re.sub(r"^\s*\d{1,2}\s*bits?\s+", "", text, flags=re.I)
    leading_marker = re.compile(r"^\s*(?:" + MARKER_WORDS + r")(?!\w)\s+", re.I)
    for _ in range(8):
        stripped = leading_marker.sub(" ", text, count=1)
        if stripped == text:
            break
        text = stripped

    # Trailing markers, repeatedly until stable.
    for _ in range(12):
        stripped = _MARKER_ANCHORED.sub(" ", text).strip(" -_.")
        if stripped == text:
            break
        text = stripped

    # The year -- but only when a title survives without it, because "1917" is
    # both a release year and the name of a film.
    without_year = YEAR_RE.sub(" ", text).strip(" -_.")
    if without_year:
        text = without_year

    # "[SubsPlease] Frieren - 12" leaves a trailing "- 12".
    text = re.sub(r"\s*[-–]\s*\d{1,3}\s*$", " ", text)
    text = text.replace(_SENTINEL, " ")
    # Collapse separators but keep hyphens *inside* a word: "Spider-Man".
    text = re.sub(r"\s+-\s+", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[._]+", " ", text).strip(" -_.")

    if not text:
        return "Untitled"
    if text.isupper() or text.islower():
        text = _smart_title_case(text)
    return text[:200]


def _strip_leading_marker(text: str) -> str:
    """Remove one leading marker token (max 3 words) if that is what it is."""
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9_+.\- ]{0,24}?)\s+(?=\S)", text)
    if not match:
        return text
    candidate = match.group(1).strip()
    # Only when *every* word in the candidate is a marker.
    words = [w for w in re.split(r"[\s._\-]+", candidate) if w]
    if words and all(
        re.sub(r"[\s._\\+\-]+", "", w).lower() in _MARKER_SET or _MARKER_RE.fullmatch(w)
        for w in words
    ):
        return text[match.end():]
    return text


def _smart_title_case(text: str) -> str:
    """Title-case input that carried no case information of its own.

    Only ever applied to all-lower or all-upper text, so deliberate stylisation
    such as "Spider-Man" or "iRobot" is never touched.
    """
    out: list[str] = []
    for index, word in enumerate(text.split(" ")):
        if not word:
            continue
        if index > 0 and word.lower().strip(",:;") in SMALL_WORDS:
            out.append(word.lower())
            continue
        out.append("-".join(p[:1].upper() + p[1:] if p else p for p in word.split("-")))
    return " ".join(out)


def normalise_title(value: str) -> str:
    """Normalise a title that came from metadata rather than a filename."""
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:200] or "Untitled"


def sort_key(title: str, *, ignore_articles: bool = True) -> str:
    """Sort title with a leading article removed (theatre-style ordering)."""
    text = normalise_title(title).lower()
    if ignore_articles:
        for article in ("the ", "a ", "an "):
            if text.startswith(article):
                return text[len(article):]
    return text


def parse_directory_hint(directory_name: str) -> str | None:
    """A directory like ``Dune (2021)`` or ``Dune.2021.1080p`` names its contents."""
    parsed = parse_filename(directory_name)
    if parsed.title and parsed.title != "Untitled":
        return parsed.title
    return None


def year_from_directory(directory_name: str) -> int | None:
    return parse_filename(directory_name).year