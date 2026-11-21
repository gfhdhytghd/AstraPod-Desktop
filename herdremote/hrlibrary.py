"""Browse and update the iPod library through a borrowed PodLink connection."""

from base64 import b64decode
from collections import defaultdict
from contextlib import suppress
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import struct
from tempfile import NamedTemporaryFile, TemporaryDirectory
import time

import podlink
from hrmediasync import music_tags


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


ART_DIMENSIONS = {"list": 44, "coverflow": 128, "wps": 300}
COUNT_REPLY_TIMEOUT = 30.0


def _request(link, command, *, timeout=None):
    # Other resident services can announce themselves on the same link.
    link.send(podlink.TEXT, b"MS3 " + command)
    deadline = time.monotonic() + (link.timeout if timeout is None else timeout)
    while (remaining := deadline - time.monotonic()) > 0:
        message = link.receive(remaining)
        if message.type != podlink.TEXT or not message.payload.startswith(b"MS3 "):
            continue
        if len(message.payload) > 500:
            raise RuntimeError("Oversized MS3 reply")
        reply = message.payload[4:]
        if reply.startswith(b"ERR "):
            raise RuntimeError("iPod library: " + reply[4:].decode("ascii", "replace"))
        return reply
    raise TimeoutError("iPod library reply timed out")


def _stop(link):
    with suppress(OSError, RuntimeError, ValueError, TimeoutError):
        _request(link, b"STOP")


def _parse_bool(value):
    if value not in (b"0", b"1"):
        raise ValueError("expected 0 or 1")
    return value == b"1"


def _fields(reply, kind, count):
    fields = reply[len(kind) + 1:].split(b"\t")
    if not reply.startswith(kind + b" ") or len(fields) != count:
        raise ValueError("unexpected reply fields")
    return fields


def summary(link: podlink.Link) -> DeviceSummary:
    timeout = max(link.timeout, COUNT_REPLY_TIMEOUT)
    reply = _request(link, b"SUMMARY", timeout=timeout)
    try:
        while reply == b"MORE":
            reply = _request(link, b"NEXT", timeout=timeout)
    except BaseException:
        _stop(link)
        raise
    try:
        model, version, total, free, count, commit, ready, busy = _fields(
            reply, b"SUMMARY", 8)
        active = _parse_bool(busy)
        usable = _parse_bool(ready) and not active
        return DeviceSummary(model.decode(), version.decode(), int(total),
                             int(free), int(count), int(commit), usable)
    except ValueError as error:
        raise RuntimeError("Invalid iPod library summary") from error


def _append_data(reply, buffer, limit):
    if not reply.startswith((b"DATA + ", b"DATA . ")):
        raise ValueError("expected a data chunk")
    data = b64decode(reply[7:], validate=True)
    if not data or len(buffer) + len(data) > limit:
        raise ValueError("invalid data chunk length")
    buffer.extend(data)
    return reply[5:6] == b"."


def _track_from_data(record):
    fields = record.split(b"\0")
    if len(fields) != 22 or fields[-1]:
        raise ValueError("expected 21 track fields")
    index = int(fields[0])
    # Rockbox stores filenames as UTF-8. Preserve even malformed tag bytes.
    strings = [value.decode("utf-8", "surrogateescape") for value in fields[1:10]]
    values = [int(value) for value in fields[10:20]]
    return DeviceTrack(index, *strings, *values, _parse_bool(fields[20]))


def tracks(link: podlink.Link, progress=None) -> list[DeviceTrack]:
    if _request(link, b"DB") != b"READY":
        raise RuntimeError("Invalid iPod library walk reply")
    result = []
    record = bytearray()
    ended = False
    try:
        while True:
            reply = _request(link, b"NEXT")
            if reply == b"END":
                ended = True
                if record:
                    raise RuntimeError("Truncated iPod track record")
                return result
            if reply == b"MORE" and not record:
                continue
            try:
                if not _append_data(reply, record, 8192):
                    continue
                track = _track_from_data(record)
            except ValueError as error:
                raise RuntimeError("Invalid iPod track record") from error
            record.clear()
            result.append(track)
            if progress is not None:
                progress(len(result))
    finally:
        if not ended:
            _stop(link)


def _path(track):
    path = track.path.encode("utf-8", "surrogateescape")
    if not path.startswith(b"/") or b"\0" in path:
        raise ValueError("Expected an absolute iPod file path")
    return path


def detail(link: podlink.Link, track: DeviceTrack) -> TrackDetail:
    reply = _request(link, b"DETAIL " + _path(track))
    try:
        size, codec, rate, channels, art = _fields(reply, b"DETAIL", 5)
        return TrackDetail(int(size), codec.decode(), int(rate), int(channels),
                           _parse_bool(art))
    except ValueError as error:
        raise RuntimeError("Invalid iPod track detail") from error


def artwork(link: podlink.Link, track: DeviceTrack, size="coverflow") -> Artwork | None:
    if size not in ART_DIMENSIONS:
        raise ValueError("Expected list, coverflow or wps artwork")
    reply = _request(link, b"ART " + size.encode("ascii") + b" " + _path(track))
    if reply in (b"NOART", b"PLACEHOLDER"):
        return None
    pixels = bytearray()
    ended = False
    try:
        kind, w, h, layout = reply.split(b" ")
        width, height = int(w), int(h)
        if (kind != b"ART" or layout not in (b"rows", b"columns")
                or not 0 < width <= ART_DIMENSIONS[size]
                or not 0 < height <= ART_DIMENSIONS[size]):
            raise ValueError("invalid artwork dimensions or layout")
        expected = width * height * 2
        while not ended:
            ended = _append_data(_request(link, b"NEXT"), pixels, expected)
        if len(pixels) != expected:
            raise ValueError("truncated artwork")
    except ValueError as error:
        raise RuntimeError("Invalid iPod artwork reply") from error
    finally:
        if not ended:
            _stop(link)

    rgb = bytearray(width * height * 3)
    for source, (pixel,) in enumerate(struct.iter_unpack("<H", pixels)):
        if layout == b"columns":
            x, y = divmod(source, height)
            dest = (y * width + x) * 3
        else:
            dest = source * 3
        red, green, blue = pixel >> 11, (pixel >> 5) & 63, pixel & 31
        rgb[dest] = (red << 3) | (red >> 2)
        rgb[dest + 1] = (green << 2) | (green >> 4)
        rgb[dest + 2] = (blue << 3) | (blue >> 2)
    return Artwork(width, height, bytes(rgb))


def delete(link: podlink.Link, track: DeviceTrack) -> None:
    if not track.managed:
        raise ValueError("Only tracks under /Music/PodLink/ can be deleted")
    if _request(link, b"DEL " + _path(track)) != b"DELETED":
        raise RuntimeError("Invalid iPod delete acknowledgement")
    podlink.refresh_media(link)


def _digest(*values):
    return sha256(json.dumps(values, ensure_ascii=True).encode("ascii")).hexdigest()


class LibraryCache:
    """JSON track snapshots and PNG paths under a separate directory per iPod."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _snapshot(self, serial, commit_id):
        return self.directory / _digest(serial) / (_digest(commit_id) + ".json")

    def load(self, serial, commit_id) -> list[DeviceTrack] | None:
        try:
            data = json.loads(self._snapshot(serial, commit_id).read_text("utf-8"))
            if isinstance(data, list):
                return [DeviceTrack(**row) for row in data]
        except (OSError, ValueError, TypeError):
            pass
        return None

    def store(self, serial, commit_id, tracks) -> None:
        destination = self._snapshot(serial, commit_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        cache = NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                   dir=destination.parent)
        temporary = Path(cache.name)
        try:
            with cache:
                json.dump([asdict(track) for track in tracks], cache)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def art_path(self, serial, track, size) -> Path:
        name = _digest(track.path, track.mtime, size)
        return self.directory / _digest(serial) / "art" / (name + ".png")


def _tag_number(value):
    try:
        return int(str(value).split("/", 1)[0])
    except ValueError:
        return 0


def _probe_tags(path):
    probe = json.loads(podlink.media_command(
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "format=duration:format_tags:stream=duration:stream_tags",
        "-of", "json", "-i", str(path.resolve())))
    streams = probe.get("streams", [])
    if not streams:
        raise ValueError("no audio stream")
    stream, container = streams[0], probe.get("format", {})
    tags = {key.casefold(): value for parent in (stream, container)
            for key, value in parent.get("tags", {}).items()}
    milliseconds = 0
    for duration in (stream.get("duration"), container.get("duration")):
        try:
            milliseconds = round(1000 * float(duration))
        except (TypeError, ValueError, OverflowError):
            continue
        if milliseconds > 0:
            break
    return music_tags(tags.get("title"), tags.get("artist", "").split(";"),
                      tags.get("album"), milliseconds,
                      _tag_number(tags.get("disc")),
                      _tag_number(tags.get("track")))


class _UploadProgress:
    """Count acknowledged bytes while the standard MS1 uploader owns the link."""

    def __init__(self, link, report):
        self.link = link
        self.timeout = link.timeout
        self.max_message = link.max_message
        self.report = report
        self.sent = self.chunk_size = 0
        self.last_report = time.monotonic()
        self.exists = False

    def send(self, kind, payload):
        if kind == podlink.BINARY:
            self.chunk_size = len(payload) - 4
        return self.link.send(kind, payload)

    def receive(self, timeout):
        message = self.link.receive(timeout)
        if message.type == podlink.TEXT:
            if message.payload == b"MS1 ACK":
                self.sent += self.chunk_size
                now = time.monotonic()
                if now - self.last_report >= 0.2:
                    self.report(self.sent)
                    self.last_report = now
            elif message.payload == b"MS1 ERR exists":
                self.exists = True
        return message


def import_files(link: podlink.Link, paths, progress, skip_duplicates=True):
    matches = defaultdict(list)
    indexed = False
    stored = []
    stopped = False

    def event(path, state, sent=0, total=0, error=None):
        if progress is not None:
            progress(dict(file=str(path), state=state, sent=sent, total=total,
                          error=error))

    try:
        for value in paths:
            path = Path(value)
            event(path, "probe")
            try:
                tags = _probe_tags(path)
            except (OSError, RuntimeError, ValueError) as error:
                event(path, "error", error=str(error))
                continue

            if skip_duplicates and tags:
                if not indexed:
                    try:
                        device_tracks = tracks(link)
                    except (OSError, RuntimeError, ValueError) as error:
                        event(path, "error", error=str(error))
                        raise
                    for track in device_tracks:
                        existing = music_tags(track.title, track.artist.split(";"),
                                              track.album, track.length_ms,
                                              track.disc, track.track)
                        if existing:
                            matches[existing.key].append(existing)
                    indexed = True
                if any(tags.matches(existing) for existing in matches[tags.key]):
                    event(path, "skip")
                    continue

            with TemporaryDirectory(prefix="podlink-import-") as work:
                try:
                    prepared, = podlink.prepare_media([path], Path(work))
                    total = prepared.upload.stat().st_size
                except (OSError, RuntimeError, ValueError) as error:
                    event(path, "error", error=str(error))
                    continue

                event(path, "upload", total=total)
                upload = _UploadProgress(
                    link, lambda sent: event(path, "upload", sent, total))
                try:
                    stored.append(podlink.upload_media_track(upload, prepared))
                except (OSError, RuntimeError, ValueError) as error:
                    if upload.exists:
                        event(path, "skip" if skip_duplicates else "error",
                              total=total,
                              error=None if skip_duplicates else str(error))
                        continue
                    stopped = True
                    with suppress(OSError, RuntimeError, ValueError):
                        link.send(podlink.TEXT, b"MS1 STOP")
                        podlink.expect_media(link, b"MS1 STOPPED")
                    event(path, "error", upload.sent, total, str(error))
                    raise
                if skip_duplicates and tags:
                    matches[tags.key].append(tags)
                event(path, "done", sent=total, total=total)
    finally:
        if stored and not stopped:
            podlink.refresh_media(link)
    return stored
