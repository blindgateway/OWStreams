"""ffprobe integration.

Reads the real container, so the library stores what the file actually is
rather than what its name claims.  Also the only place that decides whether a
file is playable.

Robustness matters here: ffprobe is fed files with truncated containers, bogus
indexes and deliberately malformed metadata.  Every field is parsed defensively
and a failure yields a :class:`ProbeResult` with ``ok=False`` rather than an
exception, so one bad file cannot abort a 1000-title import.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Extensions treated as playable video.  Kept narrow on purpose: importing a
#: stray .srt as a movie is worse than ignoring it.
VIDEO_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".mpg", ".mpeg", ".m2ts", ".ts", ".m2v", ".ogv", ".rmvb", ".divx",
    ".3gp", ".vob", ".iso", ".mdf",
})

#: Recognised but not imported as primary media; used to enrich a movie row.
SIDECAR_EXTENSIONS = (".nfo", ".json", ".yaml", ".yml")

MAX_PROBE_TIMEOUT = 60
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


class ProbeError(Exception):
    pass


@dataclass(slots=True)
class VideoStream:
    index: int
    codec: str | None = None
    profile: str | None = None
    width: int | None = None
    height: int | None = None
    aspect_ratio: str | None = None
    frame_rate: float | None = None
    bitrate: int | None = None
    hdr_format: str | None = None
    color_space: str | None = None
    color_transfer: str | None = None
    pix_fmt: str | None = None
    title: str | None = None
    is_default: bool = False

    @property
    def quality_label(self) -> str:
        if not self.height:
            return "unknown"
        for threshold, label in (
            (2160, "8K"), (1400, "4K"), (1000, "1080p"),
            (700, "720p"), (480, "480p"),
        ):
            if self.height >= threshold:
                return label
        return "SD"


@dataclass(slots=True)
class AudioStream:
    index: int
    codec: str | None = None
    language: str | None = None
    title: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    bitrate: int | None = None
    is_default: bool = False


@dataclass(slots=True)
class SubtitleStream:
    index: int
    codec: str | None = None
    language: str | None = None
    title: str | None = None
    is_default: bool = False
    is_forced: bool = False


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    path: Path
    duration_seconds: float = 0.0
    container: str | None = None
    format_name: str | None = None
    bitrate: int | None = None
    video: VideoStream | None = None
    audio: list[AudioStream] = field(default_factory=list)
    subtitles: list[SubtitleStream] = field(default_factory=list)
    file_size_bytes: int = 0
    error: str | None = None

    @property
    def has_video(self) -> bool:
        return self.video is not None

    def as_media_fields(self) -> dict[str, Any]:
        """Flatten into the ``media`` table's column set."""
        video = self.video
        primary_audio = next((a for a in self.audio if a.is_default), None) or (
            self.audio[0] if self.audio else None
        )
        audio_langs = _unique([a.language for a in self.audio if a.language])
        subtitle_langs = _unique([s.language for s in self.subtitles if s.language])
        fields: dict[str, Any] = {
            "file_size_bytes": self.file_size_bytes,
            "container": (self.container or "").split(",")[0] or None,
            "audio_languages": ", ".join(audio_langs) or None,
            "subtitle_languages": ", ".join(subtitle_langs) or None,
            "bitrate_bps": self.bitrate,
        }
        if self.duration_seconds > 0:
            fields["runtime_minutes"] = max(1, round(self.duration_seconds / 60))
        if video:
            fields.update({
                "video_codec": video.codec,
                "video_profile": video.profile,
                "width": video.width,
                "height": video.height,
                "aspect_ratio": video.aspect_ratio,
                "frame_rate": video.frame_rate,
                "hdr_format": video.hdr_format,
                "color_space": video.color_space,
            })
        if primary_audio:
            fields.update({
                "audio_codec": primary_audio.codec,
                "audio_channels": primary_audio.channels,
            })
        return fields

    def as_stream_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if self.video:
            rows.append({
                "stream_id": self.video.index,
                "kind": "video",
                "index_no": self.video.index,
                "language": None,
                "title": self.video.title,
                "codec": self.video.codec,
                "channels": None,
                "channel_layout": None,
                "is_default": int(self.video.is_default),
                "is_forced": 0,
            })
        for audio in self.audio:
            rows.append({
                "stream_id": audio.index,
                "kind": "audio",
                "index_no": audio.index,
                "language": audio.language,
                "title": audio.title,
                "codec": audio.codec,
                "channels": audio.channels,
                "channel_layout": audio.channel_layout,
                "is_default": int(audio.is_default),
                "is_forced": 0,
            })
        for subtitle in self.subtitles:
            rows.append({
                "stream_id": subtitle.index,
                "kind": "subtitle",
                "index_no": subtitle.index,
                "language": subtitle.language,
                "title": subtitle.title,
                "codec": subtitle.codec,
                "channels": None,
                "channel_layout": None,
                "is_default": int(subtitle.is_default),
                "is_forced": int(subtitle.is_forced),
            })
        return rows


def _unique(values: list[str | None]) -> list[str]:
    seen, out = set(), []
    for value in values:
        key = (value or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


class MediaProbe:
    """Thin, defensive wrapper around ``ffprobe -show_format -show_streams``."""

    def __init__(self, ffprobe_path: str = "ffprobe", timeout: int = 30) -> None:
        self.ffprobe_path = ffprobe_path
        self.timeout = timeout
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = shutil.which(self.ffprobe_path) is not None
            if not self._available:
                log.warning(
                    "ffprobe not found at %r; media will be imported without "
                    "technical metadata", self.ffprobe_path
                )
        return self._available

    def probe(self, path: Path | str) -> ProbeResult:
        file_path = Path(path)
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            return ProbeResult(ok=False, path=file_path, error=f"cannot stat file: {exc}")

        if size == 0:
            return ProbeResult(ok=False, path=file_path, file_size_bytes=0,
                               error="file is empty",
                               container=file_path.suffix.lstrip(".").lower() or None)
        if not self.available:
            # No ffprobe: the file is treated as importable with whatever the
            # name says, and no technical metadata.  The container is still
            # worth keeping -- it is the one field the extension is reliable
            # about, and the player needs it to choose a demuxer.
            return ProbeResult(ok=True, path=file_path, file_size_bytes=size,
                               error=None,
                               container=file_path.suffix.lstrip(".").lower() or None)

        command = [
            self.ffprobe_path,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            # Never let ffprobe read from a network/pipe attached to the file.
            "-protocol_whitelist", "file",
            str(file_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=min(self.timeout, MAX_PROBE_TIMEOUT),
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except subprocess.TimeoutExpired:
            return ProbeResult(ok=False, path=file_path, file_size_bytes=size,
                               error="ffprobe timed out")
        except OSError as exc:
            return ProbeResult(ok=False, path=file_path, file_size_bytes=size,
                               error=f"cannot run ffprobe: {exc}")

        if completed.returncode != 0:
            stderr = (completed.stderr or b"").decode("utf-8", "replace")[:400]
            return ProbeResult(ok=False, path=file_path, file_size_bytes=size,
                               error=f"ffprobe failed: {stderr.strip() or 'unknown error'}")
        if len(completed.stdout) > MAX_OUTPUT_BYTES:
            return ProbeResult(ok=False, path=file_path, file_size_bytes=size,
                               error="ffprobe output is implausibly large")

        try:
            payload = json.loads(completed.stdout.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            return ProbeResult(ok=False, path=file_path, file_size_bytes=size,
                               error=f"ffprobe produced invalid JSON: {exc}")
        if not isinstance(payload, dict):
            return ProbeResult(ok=False, path=file_path, file_size_bytes=size,
                               error="ffprobe output was not an object")
        return self._parse(file_path, payload, size)

    def _parse(self, file_path: Path, payload: dict[str, Any], size: int) -> ProbeResult:
        fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
        raw_streams = payload.get("streams")
        if not isinstance(raw_streams, list):
            raw_streams = []

        result = ProbeResult(
            ok=True,
            path=file_path,
            file_size_bytes=size,
            container=(str(fmt.get("format_name") or "").split(",")[0] or None),
            format_name=fmt.get("format_long_name"),
            duration_seconds=_as_float(fmt.get("duration")),
            bitrate=_as_int(fmt.get("bit_rate")),
        )

        audio: list[AudioStream] = []
        subtitles: list[SubtitleStream] = []
        for entry in raw_streams:
            if not isinstance(entry, dict):
                continue
            index = _as_int(entry.get("index"))
            if index is None:
                continue
            codec_type = str(entry.get("codec_type") or "").lower()
            disposition = entry.get("disposition")
            disposition = disposition if isinstance(disposition, dict) else {}
            tags = entry.get("tags")
            tags = tags if isinstance(tags, dict) else {}

            if codec_type == "video":
                # Attached cover art is a still image, not the programme.
                disposition_pic = disposition.get("attached_pic")
                if result.video is not None or (disposition_pic and int(disposition_pic or 0)):
                    continue
                if str(entry.get("codec_name") or "").lower() in {"mjpeg", "png", "bmp", "gif"}:
                    continue
                result.video = VideoStream(
                    index=index,
                    codec=_clean(entry.get("codec_name")),
                    profile=_clean(entry.get("profile")),
                    width=_as_int(entry.get("width")),
                    height=_as_int(entry.get("height")),
                    aspect_ratio=_aspect(entry.get("display_aspect_ratio"),
                                         entry.get("width"), entry.get("height")),
                    frame_rate=_as_float(entry.get("avg_frame_rate")) or _as_float(entry.get("r_frame_rate")),
                    bitrate=_as_int(entry.get("bit_rate")),
                    hdr_format=_hdr_format(tags, entry),
                    color_space=_clean(entry.get("color_space")),
                    color_transfer=_clean(entry.get("color_transfer")),
                    pix_fmt=_clean(entry.get("pix_fmt")),
                    title=_clean(tags.get("title")),
                    is_default=bool(int(disposition.get("default") or 0)),
                )
            elif codec_type == "audio":
                audio.append(AudioStream(
                    index=index,
                    codec=_clean(entry.get("codec_name")),
                    language=_language(tags.get("language"), tags.get("LANGUAGE")),
                    title=_clean(tags.get("title")),
                    channels=_as_int(entry.get("channels")),
                    channel_layout=_clean(entry.get("channel_layout")),
                    bitrate=_as_int(entry.get("bit_rate")),
                    is_default=bool(int(disposition.get("default") or 0)),
                ))
            elif codec_type == "subtitle":
                forced = bool(int(disposition.get("forced") or 0))
                subtitles.append(SubtitleStream(
                    index=index,
                    codec=_clean(entry.get("codec_name")),
                    language=_language(tags.get("language"), tags.get("LANGUAGE")),
                    title=_clean(tags.get("title")),
                    is_default=bool(int(disposition.get("default") or 0)),
                    is_forced=forced,
                ))

        result.audio = audio
        result.subtitles = subtitles

        if result.duration_seconds <= 0 and result.video and result.video.frame_rate:
            # Some containers omit the duration; ffprobe's stream duration is a
            # weaker but usable fallback.
            stream_duration = 0.0
            for entry in raw_streams:
                if isinstance(entry, dict) and entry.get("codec_type") == "video":
                    stream_duration = _as_float(entry.get("duration")) or 0.0
                    break
            result.duration_seconds = stream_duration

        if not result.has_video:
            result.ok = False
            result.error = "no video stream found"
        return result


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"unknown", "N/A", "und"}:
        return None
    return text[:64]


def _as_int(value: Any) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _as_float(value: Any) -> float:
    """Parse ``"30000/1001"`` and similar ffprobe rationals, plus junk."""
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text or text in {"N/A", "unknown", "nan", "inf"}:
        return 0.0
    try:
        if "/" in text:
            numerator, _, denominator = text.partition("/")
            num = float(numerator)
            den = float(denominator)
            if den == 0 or num != num or den != den:
                return 0.0
            parsed = num / den
        else:
            parsed = float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return 0.0
    return parsed


def _aspect(display: Any, width: Any, height: Any) -> str | None:
    value = _clean(display)
    if value and ":" in value:
        return value[:16]
    w, h = _as_int(width), _as_int(height)
    if not w or not h:
        return None
    from math import gcd

    divisor = gcd(w, h) or 1
    return f"{w // divisor}:{h // divisor}"


def _language(primary: Any, fallback: Any) -> str | None:
    for candidate in (primary, fallback):
        text = _clean(candidate)
        if text and text.lower() not in {"und", "unknown"}:
            return text.lower()[:16]
    return None


def _hdr_format(tags: dict[str, Any], stream: dict[str, Any]) -> str | None:
    """Detect HDR from the colour-transfer side data or the container tag.

    PQ (``smpte2084``) and HLG (``arib-std-b67``) are *both* HDR10-family
    transfer functions, so either one means HDR10.  Whether the source is a
    Dolby Vision master is not derivable from either value -- it needs the
    side-data block -- so it is not claimed here.
    """
    transfer = _clean(stream.get("color_transfer"))
    if transfer and transfer.lower() in {"smpte2084", "arib-std-b67"}:
        return "HDR10"
    for key in ("HDR", "hdr", "hdr_format", "MASTERING_DISPLAY_METADATA"):
        value = _clean(tags.get(key))
        if value:
            return value[:16]
    return None