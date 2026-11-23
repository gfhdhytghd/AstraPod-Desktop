"""Browse and update the iPod library through a borrowed PodLink connection."""

from base64 import b64decode
from collections import defaultdict
from contextlib import suppress
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
from pathlib import Path
import struct
from tempfile import NamedTemporaryFile, TemporaryDirectory
import time
from zlib import crc32

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
STREAM_WINDOW = 8


class _MS3Error(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("iPod library: " + reason.decode("ascii", "replace"))


def _request(link, command, *, timeout=None, binary=False):
    link.send(podlink.TEXT, b"MS3 " + command)
    deadline = time.monotonic() + (link.timeout if timeout is None else timeout)
    return _receive(link, deadline, binary=binary)


def _receive(link, deadline, *, binary=False, stopping=False):
    # Other resident services can announce themselves on the same link.
    while (remaining := deadline - time.monotonic()) > 0:
        message = link.receive(remaining)
        if (binary and message.type == podlink.BINARY
                and message.payload.startswith(b"MSF1")):
            return message.payload
        if message.type != podlink.TEXT or not message.payload.startswith(b"MS3 "):
            continue
        if len(message.payload) > 500:
            raise RuntimeError("Oversized MS3 reply")
        reply = message.payload[4:]
        if stopping and reply not in (b"STOPPED", b"ERR busy"):
            continue
        if reply.startswith(b"ERR "):
            raise _MS3Error(reply[4:])
        return reply
    raise TimeoutError("iPod library reply timed out")


def _stop(link):
    with suppress(OSError, RuntimeError, ValueError, TimeoutError):
        link.send(podlink.TEXT, b"MS3 STOP")
        _receive(link, time.monotonic() + link.timeout, stopping=True)


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


def match_cached_tracks(link, commit_id, cached_tracks):
    """Validate a cached library for the USB identity already selected by the caller."""
    if commit_id is None:
        return None
    try:
        current = summary(link)
    except (RuntimeError, TimeoutError):
        return None
    if (current.database_ready and current.commit_id == commit_id
            and current.track_count == len(cached_tracks)):
        return cached_tracks
    return None


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


# TCH10 on-disk layout from apps-ipod/database/tagcache.{c,h}.
_MASTER = "database_idx.tcd"
_TEXT_TAGS = (4, 3, 0, 1, 7, 2, 5, 8, 6)
_NUMERIC_TAGS = (9, 10, 11, 14, 13, 15, 16, 17, 18, 20)
_TAG_FILES = {tag: f"database_{tag}.tcd" for tag in (*_TEXT_TAGS, 12)}
_DATABASE_FILES = {_MASTER, *_TAG_FILES.values()}
_MAGIC = 0x54434810
_MASTER_HEADER = 24
_TAG_HEADER = 12
_EPISODE_DATE = re.compile(rb" \([0-9]{4}-[0-9]{2}-[0-9]{2}\)\Z")


def _managed_path(path):
    if path.startswith(b"/<HDD0>/"):
        path = path[7:]
    prefix = b"/music/podlink/"
    return (path.lower().startswith(prefix)
            and not any(c in path for c in (b"\\", b":", b"<", b">"))
            and all(part not in (b"", b".", b"..")
                    for part in path[len(prefix):].split(b"/")))


def _tag_table(data, endian):
    magic, size, count = struct.unpack_from(endian + "3i", data)
    if magic != _MAGIC or size != len(data) - _TAG_HEADER or count < 0:
        raise ValueError("invalid tag table header")
    offset = _TAG_HEADER
    tags = {}
    for _ in range(count):
        length, _index = struct.unpack_from(endian + "2i", data, offset)
        start = offset + 8
        end = start + length
        if length < 0 or end > len(data):
            raise ValueError("invalid tag length")
        # The length includes the terminator and any alignment padding.
        tags[offset] = bytes(data[start:end]).split(b"\0", 1)[0]
        offset = end
    if offset != len(data):
        raise ValueError("tag table length mismatch")
    return tags


def parse_tagcache(files, *, expected_commit=None) -> list[DeviceTrack]:
    """Decode a complete TCH10 file set into MS3 walk records."""
    try:
        master = files[_MASTER]
        endian = "<" if int.from_bytes(master[:4], "little") == _MAGIC else ">"
        magic, _size, count, _serial, commit, dirty = struct.unpack_from(
            endian + "6i", master)
        entry = struct.Struct(endian + "24i")  # 23 tag slots plus flags
        if (magic != _MAGIC or dirty or count < 0
                or len(master) != _MASTER_HEADER + count * entry.size):
            raise ValueError("invalid TCH10 master index")
        if expected_commit is not None and commit != expected_commit:
            raise ValueError("database changed")

        tables = {tag: _tag_table(files[_TAG_FILES[tag]], endian) for tag in _TEXT_TAGS}
        # db_spoken_build keeps only the first 64 matching genres, in file order.
        spoken = []
        for seek, genre in tables[2].items():
            genre = genre.lower()
            if (genre in (b"book", b"books", b"spoken", b"speech", b"non-music",
                          b"nonmusic", b"podcast", b"podcasts")
                    or any(name in genre for name in (b"audiobook", b"audio book", b"spoken word"))):
                spoken.append((seek, genre))
                if len(spoken) == 64:
                    break
        podcasts = {seek for seek, genre in spoken if genre in (b"podcast", b"podcasts")}

        result = []
        for index, row in enumerate(entry.iter_unpack(memoryview(master)[_MASTER_HEADER:])):
            if row[-1] & 1:  # FLAG_DELETED: string slots now contain hashes.
                continue
            fields = [tables[tag][row[tag]] for tag in _TEXT_TAGS]
            if row[2] in podcasts:
                fields[1] = _EPISODE_DATE.sub(b"", fields[1])
            strings = [value.decode("utf-8", "surrogateescape") for value in fields]
            numeric = [row[tag] for tag in _NUMERIC_TAGS]
            numeric[-1] &= 0xffffffff  # mtime is unsigned on the wire
            result.append(DeviceTrack(index, *strings, *numeric, _managed_path(fields[0])))
        return result
    except (KeyError, struct.error) as error:
        raise ValueError("missing or truncated tagcache data") from error


def _manifest(reply):
    if not reply.startswith(b"FILES "):
        raise ValueError("expected a database manifest")
    commit, generation, removed, stats, *entries = reply[6:].decode("ascii").split("\t")
    files = {}
    for entry in entries:
        name, size = entry.split(":")
        if name not in _DATABASE_FILES or name in files or not 0 < int(size) <= 0xffffffff:
            raise ValueError("invalid database file")
        files[name] = int(size)
    if files.keys() != _DATABASE_FILES:
        raise ValueError("incomplete database manifest")
    return (int(commit), int(generation), int(removed), int(stats)), files


def _open_binary(link, command):
    if not getattr(link, "_ms3_stream_unavailable", False):
        try:
            return _request(link, b"STREAM " + command), True
        except _MS3Error as error:
            if error.reason != b"command":
                raise
            link._ms3_stream_unavailable = True
    return _request(link, command), False


def _read_binary(link, size, label, progress=None, *, streaming=False, collect=True,
                 pace_bytes_per_second=None, sink=None):
    data = bytearray()
    offset = crc = 0
    credits = STREAM_WINDOW
    started = time.monotonic() if pace_bytes_per_second else None
    while True:
        if streaming:
            if not credits:
                if started is not None and offset < size:
                    wait = started + offset / pace_bytes_per_second - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                link.send(podlink.TEXT, b"MS3 CREDIT")
                credits = STREAM_WINDOW
            reply = _receive(link, time.monotonic() + link.timeout, binary=True)
            credits -= 1
        else:
            reply = _request(link, b"NEXT", binary=True)
        if not reply.startswith(b"MSF1"):
            if offset != size or reply != f"DONE {size} {crc:08x}".encode("ascii"):
                raise ValueError(f"{label} length or CRC mismatch")
            return data if collect else crc
        if (not 8 < len(reply) <= link.max_message
                or struct.unpack_from("<I", reply, 4)[0] != offset
                or offset + len(reply) - 8 > size):
            raise ValueError(f"invalid {label} chunk")
        chunk = memoryview(reply)[8:]
        if sink is not None:
            sink.write(chunk)
        elif collect:
            data.extend(chunk)
        offset += len(chunk)
        crc = crc32(chunk, crc)
        if progress is not None:
            progress(offset)


def download_sample(link, path, destination):
    """Write a read-only MS3 sample after validating offsets, length and CRC."""
    destination = Path(destination)
    complete = False
    try:
        opened = _request(link, b"STREAM SAMPLE " + path.encode("utf-8"))
        match = re.fullmatch(rb"SAMPLE ([0-9]{1,9})", opened)
        if match is None or not 0 < (size := int(match[1])) <= 128 * 1024 * 1024:
            raise ValueError("Invalid iPod sample length")
        with TemporaryDirectory(dir=destination.parent) as staging:
            partial = Path(staging) / "sample"
            with partial.open("wb") as output:
                _read_binary(link, size, "sample", streaming=True, collect=False, sink=output)
            complete = True
            partial.replace(destination)
    finally:
        if not complete:
            _stop(link)


def _bulk_tracks(link, manifest, byte_progress, expected_commit):
    snapshot, sizes = _manifest(manifest)
    if expected_commit is not None and snapshot[0] != expected_commit:
        raise ValueError("database changed")
    files = {}
    done, total = 0, sum(sizes.values())

    def progress(received):
        byte_progress(done + received, total)

    for name, size in sizes.items():
        complete = False
        try:
            opened, streaming = _open_binary(link, b"FILE " + name.encode("ascii"))
            if opened != f"FILE {size}".encode("ascii"):
                raise ValueError("database file length changed")
            files[name] = _read_binary(
                link, size, "database", progress if byte_progress is not None else None,
                streaming=streaming)
            done += size
            complete = True
        finally:
            if not complete:
                _stop(link)
    if _manifest(_request(link, b"FILES")) != (snapshot, sizes):
        raise ValueError("database changed")
    return parse_tagcache(files, expected_commit=snapshot[0])


def tracks(link: podlink.Link, progress=None, *, byte_progress=None,
           expected_commit=None) -> list[DeviceTrack]:
    """Read a snapshot. Progress callbacks receive tracks (walk) or bytes (bulk)."""
    try:
        manifest = _request(link, b"FILES")
    except _MS3Error as error:
        if error.reason != b"command":
            raise
        return _walk_tracks(link, progress)
    try:
        return _bulk_tracks(link, manifest, byte_progress, expected_commit)
    except ValueError as error:
        raise RuntimeError(f"Invalid iPod library snapshot: {error}") from error


def _walk_tracks(link, progress=None):
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
    args = size.encode("ascii") + b" " + _path(track)
    command = b"ARTBIN"
    try:
        reply, streaming = _open_binary(link, command + b" " + args)
    except _MS3Error as error:
        if error.reason != b"command":
            raise
        command = b"ART"
        reply = _request(link, command + b" " + args)
    if reply in (b"NOART", b"PLACEHOLDER"):
        return None
    ended = False
    try:
        kind, w, h, layout = reply.split(b" ")
        width, height = int(w), int(h)
        if (kind != command or layout not in (b"rows", b"columns")
                or not 0 < width <= ART_DIMENSIONS[size]
                or not 0 < height <= ART_DIMENSIONS[size]):
            raise ValueError("invalid artwork dimensions or layout")
        expected = width * height * 2
        if command == b"ARTBIN":
            pixels = _read_binary(link, expected, "artwork", streaming=streaming)
            ended = True
        else:
            pixels = bytearray()
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


def import_tag_index(link, *, cached_snapshot=None):
    rows = None
    if cached_snapshot is not None:
        rows = match_cached_tracks(link, *cached_snapshot)
    if rows is None:
        rows = tracks(link)
    matches = defaultdict(list)
    for track in rows:
        tags = music_tags(track.title, track.artist.split(";"), track.album,
                          track.length_ms, track.disc, track.track)
        if tags:
            matches[tags.key].append(tags)
    return matches


def import_batch(paths, progress, skip_duplicates, index, publish):
    """Prepare and report each file identically for the two transports."""
    matches = None
    stored = []

    def event(path, state, sent=0, total=0, error=None):
        if progress is not None:
            progress(dict(file=str(path), state=state, sent=sent, total=total,
                          error=error))

    for value in paths:
        path = Path(value)
        event(path, "probe")
        try:
            tags = _probe_tags(path)
        except (OSError, RuntimeError, ValueError) as error:
            event(path, "error", error=str(error))
            continue

        if skip_duplicates and tags:
            if matches is None:
                try:
                    matches = index()
                except (OSError, RuntimeError, ValueError) as error:
                    event(path, "error", error=str(error))
                    raise
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

            sent = 0

            def report(length):
                nonlocal sent
                sent = length
                event(path, "upload", sent, total)

            event(path, "upload", total=total)
            try:
                stored.append(publish(prepared, report))
            except FileExistsError as error:
                event(path, "skip" if skip_duplicates else "error", total=total,
                      error=None if skip_duplicates else str(error))
                continue
            except (OSError, RuntimeError, ValueError) as error:
                event(path, "error", sent, total, str(error))
                raise
            if skip_duplicates and tags:
                matches[tags.key].append(tags)
            event(path, "done", sent=total, total=total)
    return stored


_PROFILE_STAGES = ("read", "file_crc", "frame_crc", "mailbox_copy", "ring_copy",
                   "dma_copy", "dma_active", "dma_gap")


def profile_download(link, name, mode, *, tagcache=False, mbit=None):
    """Time one validated stream while retaining only the current frame."""
    if mbit is not None and not 1 <= mbit <= 480:
        raise ValueError("rate must be in 1..480 Mbit/s")
    if mode not in ("base", "fast"):
        raise ValueError("mode must be base or fast")
    if tagcache and name not in _DATABASE_FILES:
        raise ValueError("invalid tagcache filename")
    request = b"FILE " if tagcache else b"SAMPLE "
    command = b"PROFILE " + mode.upper().encode("ascii")
    for attempt in range(5):
        try:
            if _request(link, command) != b"PROFILE READY":
                raise RuntimeError("Unexpected profiling acknowledgement")
            break
        except _MS3Error as error:
            if error.reason != b"busy" or attempt == 4:
                raise
            time.sleep(.02)

    original_read = link.port.read
    read_calls = read_bytes = largest_read = read_cpu_ns = 0

    def read(size):
        nonlocal read_calls, read_bytes, largest_read, read_cpu_ns
        start = time.thread_time_ns()
        data = original_read(size)
        read_cpu_ns += time.thread_time_ns() - start
        read_calls += 1
        read_bytes += len(data)
        largest_read = max(largest_read, len(data))
        return data

    try:
        link.port.read = read
        start = time.perf_counter()
        opened = _request(link, b"STREAM " + request + name.encode("utf-8", "surrogateescape"))
        prefix, size = opened.split(b" ", 1)
        if prefix != request.rstrip() or not 0 < int(size) <= 0xffffffff:
            raise ValueError("Invalid profiling stream size")
        size = int(size)
        checksum = _read_binary(
            link, size, "profiling stream", streaming=True, collect=False,
            pace_bytes_per_second=mbit * 125000 if mbit is not None else None)
        seconds = time.perf_counter() - start
    except BaseException:
        _stop(link)
        with suppress(OSError, RuntimeError, ValueError, TimeoutError):
            _request(link, b"PROFILE")
        raise
    finally:
        link.port.read = original_read

    try:
        prefix, *samples = _request(link, b"PROFILE CPU").split()
        if len(samples) != len(_PROFILE_STAGES) + 1:
            raise ValueError("Invalid CPU profiling counters")
        idle_us, wall_us = map(int, samples.pop().split(b","))
        if not 0 <= idle_us <= wall_us:
            raise ValueError("Invalid device idle time")
    except _MS3Error as error:
        if error.reason != b"command":
            raise
        prefix, *samples = _request(link, b"PROFILE").split()
        idle_us = wall_us = None
    if prefix != b"PROFILE" or len(samples) != len(_PROFILE_STAGES):
        raise ValueError("Invalid profiling counters")
    stages = {stage: dict(zip(("count", "bytes", "us", "max_us"),
                            map(int, values.split(b",")), strict=True))
              for stage, values in zip(_PROFILE_STAGES, samples, strict=True)}
    for sample in stages.values():
        sample["elapsed_us_per_mib"] = round(sample["us"] * (1 << 20) / size)
    return dict(mode=mode, limit_mbit=mbit, bytes=size, seconds=seconds,
                bytes_per_second=size / seconds,
                crc32=f"{checksum:08x}", tty_read_calls=read_calls,
                tty_bytes=read_bytes, tty_max_read=largest_read,
                tty_read_cpu_us=read_cpu_ns // 1000, device=stages,
                device_idle_us=idle_us, device_wall_us=wall_us,
                device_idle_percent=100 * idle_us / wall_us if wall_us else None)


def _profile_main():
    import argparse
    from hrmediasync import borrow_serial, connect_device

    parser = argparse.ArgumentParser(description="Profile a PodLink MS3 download")
    parser.add_argument("name", help="music path or tagcache basename")
    parser.add_argument("--tagcache", action="store_true")
    parser.add_argument("--mode", choices=("base", "fast"), default="fast")
    parser.add_argument("--port")
    parser.add_argument("--mbit", type=float, help="pace credits to this rate in Mbit/s")
    args = parser.parse_args()
    try:
        with borrow_serial(), connect_device(args.port) as (link, _):
            result = profile_download(link, args.name, args.mode, tagcache=args.tagcache,
                                      mbit=args.mbit)
            print(json.dumps(result, indent=2))
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"PodLink profile: {error}\n")


def import_files(link: podlink.Link, paths, progress, skip_duplicates=True, *,
                 cached_snapshot=None):
    changed = False
    stopped = False

    def publish(prepared, report):
        nonlocal changed, stopped
        upload = _UploadProgress(link, report)
        try:
            result = podlink.upload_media_track(upload, prepared)
        except (OSError, RuntimeError, ValueError) as error:
            if upload.exists:
                raise FileExistsError(str(error)) from error
            stopped = True
            with suppress(OSError, RuntimeError, ValueError):
                link.send(podlink.TEXT, b"MS1 STOP")
                podlink.expect_media(link, b"MS1 STOPPED")
            raise
        changed = True
        return result

    try:
        return import_batch(paths, progress, skip_duplicates,
                            lambda: import_tag_index(link, cached_snapshot=cached_snapshot),
                            publish)
    finally:
        if changed and not stopped:
            podlink.refresh_media(link)


if __name__ == "__main__":
    _profile_main()
