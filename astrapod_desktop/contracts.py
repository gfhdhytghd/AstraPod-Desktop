"""Interface 1 and 3 records, used when the real modules are not importable.

They mirror the library and sync records used by HerdRemote. The GUI
reads records only by attribute, so the real classes drop in unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DeviceSummary:
    model: str
    firmware: str
    total_bytes: int
    free_bytes: int
    track_count: int
    commit_id: int
    database_ready: bool


@dataclass(frozen=True)
class DeviceTrack:
    index: int
    path: str
    title: str
    artist: str
    album: str
    album_artist: str
    genre: str
    composer: str
    grouping: str
    comment: str
    year: int
    disc: int
    track: int
    length_ms: int
    bitrate_kbps: int
    play_count: int
    rating: int
    play_time_ms: int
    last_played: int
    mtime: int
    managed: bool


@dataclass(frozen=True)
class TrackDetail:
    size_bytes: int
    codec: str
    sample_rate: int
    channels: int
    has_art: bool


@dataclass(frozen=True)
class Artwork:
    width: int
    height: int
    rgb888: bytes


@dataclass
class PluginInfo:
    id: str
    name: str
    version: str
    description: str
    enabled: bool
    state: str
    device: str | None
    detail: str
    permissions: list = field(default_factory=list)
    settings_schema: object = None
    last_event: dict | None = None


def album_key(track) -> tuple[str, str]:
    artist = (track.album_artist or track.artist or "").strip()
    return ((track.album or "").strip().casefold(), artist.casefold())


def track_record(track) -> dict:
    """JSON-safe dict of a DeviceTrack, for the on-disk fallback cache."""
    return {name: getattr(track, name) for name in DeviceTrack.__dataclass_fields__}
