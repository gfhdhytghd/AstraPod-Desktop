"""One-way Jellyfin reconciliation over PodLink."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import socket
from tempfile import TemporaryDirectory
import time
import unicodedata
import urllib.error
import uuid
import urllib.request

import podlink
import hrdisk
from hrjellyfin import JellyfinClient, JellyfinError, Session, https_base, id_or_none, token_from

AUDIO_SUFFIXES = {".flac", ".m4a", ".mp3"}
SYNC_NAME = re.compile(r"[0-9a-f]{64}\.(?:flac|m4a|mp3)")
PAGE_SIZE = 200
DOWNLOAD_LIMIT = 1024 * 1024 * 1024


@dataclass(frozen=True)
class Scope:
    kind: str
    label: str
    item_id: str | None = None


@dataclass(frozen=True)
class MusicTags:
    title: str
    artists: tuple[str, ...]
    album: str
    milliseconds: int
    disc: int
    number: int

    @property
    def key(self):
        return self.title, self.artists, self.album

    def matches(self, other):
        # Title/artist/album already agree. Keep different cuts and disc
        # positions separate. Allow small codec/container timing differences.
        return (abs(self.milliseconds - other.milliseconds) <= 1500
                and (not self.disc or not other.disc or self.disc == other.disc)
                and (not self.number or not other.number or self.number == other.number))


def normalized(value):
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def positive_number(value):
    return value if type(value) is int and value > 0 else 0


def music_tags(title, artists, album, milliseconds, disc, number):
    title, album = normalized(title), normalized(album)
    credits = tuple(sorted({credit for artist in artists
                            if (credit := normalized(artist))}))
    if (not title or not album or not credits or milliseconds <= 0
            or "<untagged>" in (title, album, *credits)):
        return None
    return MusicTags(title, credits, album, milliseconds,
                     positive_number(disc), positive_number(number))


@dataclass(frozen=True)
class Track:
    item_id: str
    label: str
    suffix: str
    name: str
    tags: MusicTags | None = None


@dataclass(frozen=True)
class Plan:
    identity: str
    scope: Scope
    device: str
    tracks: tuple
    owned: frozenset
    matched: frozenset = frozenset()

    @property
    def additions(self):
        return tuple(track for track in self.tracks
                     if track.name not in self.owned and track.name not in self.matched)

    @property
    def originals(self):
        return tuple(track for track in self.tracks
                     if track.name in self.matched and track.name not in self.owned)

    @property
    def removals(self):
        return self.owned - {track.name for track in self.tracks}


class JellyfinSource:
    def __init__(self, url, token="", client=None):
        self.client = client or JellyfinClient(timeout=10)
        base = https_base(url.strip())
        if token:
            token = token_from({"JELLYFIN_API_KEY": token})
        else:
            token = token_from(os.environ)
        initial = Session(base, "podlink", None, token)
        user = id_or_none(self.client.request(initial, "Users/Me").get("Id"))
        if not user:
            raise JellyfinError("Jellyfin requires a user access token")
        user = uuid.UUID(user).hex
        self.session = Session(base, "podlink", None, token, user)
        self.identity = hashlib.sha256(f"{base}\0{user}".encode()).hexdigest()[:24]

    @classmethod
    def with_password(cls, url, username, password, client=None):
        base = https_base(url.strip())
        username = username.strip()
        if not username:
            raise JellyfinError("Jellyfin username is required")
        client = client or JellyfinClient(timeout=10)
        reply = client.authenticate(base, username, password)
        token = reply.get("AccessToken")
        if not isinstance(token, str) or not token:
            raise JellyfinError("Jellyfin login returned no user access token")
        return cls(base, token, client)

    def pages(self, route, **options):
        offset = 0
        expected = None
        while True:
            rows, total = self.client.page(self.session, route, offset, PAGE_SIZE,
                                           EnableImages="false", EnableUserData="false", **options)
            if expected is not None and total != expected:
                raise JellyfinError("Jellyfin changed during enumeration")
            yield from rows
            offset += len(rows)
            if offset == total:
                return
            expected = total

    def scopes(self):
        result = [Scope("all", "账号可见的全部音乐")]
        response = self.client.request(self.session, f"Users/{self.session.user}/Views")
        views = response.get("Items")
        if not isinstance(views, list):
            raise JellyfinError("Jellyfin library list is invalid")
        for row in views:
            if not isinstance(row, dict):
                raise JellyfinError("Jellyfin returned an invalid library")
            if row.get("CollectionType") == "music" and id_or_none(row.get("Id")):
                result.append(Scope("library", row.get("Name") or row["Id"], row["Id"]))
        for row in self.pages("Items", IncludeItemTypes="Playlist", Recursive="true",
                              SortBy="SortName"):
            if id_or_none(row.get("Id")):
                result.append(Scope("playlist", row.get("Name") or row["Id"], row["Id"]))
        return result

    def tracks(self, scope):
        if scope.kind == "playlist":
            rows = self.pages(f"Playlists/{scope.item_id}/Items", Fields="Path,MediaSources")
        else:
            rows = self.pages("Items", ParentId=scope.item_id, IncludeItemTypes="Audio",
                              Recursive="true", SortBy="SortName", Fields="Path,MediaSources")
        tracks = {}
        for row in rows:
            if row.get("Type") != "Audio":
                if scope.kind == "playlist":
                    continue
                raise JellyfinError("Jellyfin returned an unexpected item type")
            item_id = id_or_none(row.get("Id"))
            if not item_id:
                raise JellyfinError("Jellyfin returned an invalid track ID")
            item_id = uuid.UUID(item_id).hex
            suffix = Path(row.get("Path") or "").suffix.lower()
            sources = row.get("MediaSources") or []
            media_size = sources[0].get("Size") if sources else None
            revision = f"{item_id}\0{row.get('Etag')}\0{media_size}\0{suffix}"
            filename = self.identity + hashlib.sha256(revision.encode()).hexdigest()[24:]
            filename += suffix if suffix in AUDIO_SUFFIXES else ".flac"
            artists = row.get("Artists")
            if not isinstance(artists, list):
                artists = []
            tags = music_tags(row.get("Name"), artists, row.get("Album"),
                              positive_number(row.get("RunTimeTicks")) // 10000,
                              row.get("ParentIndexNumber"), row.get("IndexNumber"))
            track = Track(item_id, row.get("Name") or item_id, suffix, filename, tags)
            if item_id in tracks and (scope.kind != "playlist" or tracks[item_id] != track):
                raise JellyfinError("Jellyfin returned inconsistent item pages")
            tracks[item_id] = track
        return tuple(sorted(tracks.values(), key=lambda track: track.item_id))

    def download(self, track, path):
        session = self.session
        request = urllib.request.Request(
            f"{session.base}/Items/{track.item_id}/Download",
            headers={"Authorization": f'MediaBrowser Token="{session.token}"'})
        try:
            with self.client.opener.open(request, timeout=30) as response, path.open("xb") as output:
                size = 0
                limit = response.headers.get("Content-Length")
                limit = int(limit) if limit is not None else None
                if limit is not None and not 0 < limit <= DOWNLOAD_LIMIT:
                    raise JellyfinError("audio file exceeds download limit")
                deadline = time.monotonic() + 20 * 60
                while chunk := response.read1(1024 * 1024):
                    if time.monotonic() > deadline:
                        raise JellyfinError("audio download timed out")
                    size += len(chunk)
                    if size > DOWNLOAD_LIMIT:
                        raise JellyfinError("audio file exceeds download limit")
                    output.write(chunk)
                if not size or (limit is not None and size != limit):
                    raise JellyfinError("Jellyfin returned an incomplete audio file")
        except urllib.error.HTTPError as error:
            error.close()
            raise JellyfinError(f"audio download failed (HTTP {error.code})") from None


@contextmanager
def borrow_serial():
    """Keep a local lease open until the sync operation closes its USB link."""
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(12)
        channel.connect(hrdisk.default_socket())
        channel.sendall(hrdisk.BORROW_SERIAL)
        with channel.makefile("rb") as replies:
            reply = replies.readline(128)
        if reply != b"0 serial released\n":
            raise RuntimeError("The AstraPod background service could not release the iPod connection.")
        yield


@contextmanager
def connect_device(port):
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    device = port or podlink.find_port(ports)
    identity = next((item.serial_number for item in ports if
                     os.path.realpath(item.device) == os.path.realpath(device)), None)
    if not identity:
        raise RuntimeError("PodLink sync requires an iPod with a USB serial number")
    with podlink.serial_port(device, 10) as serial:
        link = podlink.Link(serial, 10, max_message=podlink.CDC_RING_PAYLOAD)
        link.open()
        try:
            yield link, identity
        finally:
            with suppress(OSError, RuntimeError, ValueError):
                link.close()


def owned_name(name, identity):
    return SYNC_NAME.fullmatch(name) is not None and name.startswith(identity)


def inventory(link, identity):
    link.send(podlink.TEXT, b"MS2 LIST")
    try:
        reply = podlink.media_reply(link)
    except TimeoutError as error:
        raise RuntimeError("No Jellyfin sync reply. Check that the iPod is running Jellyfin sync firmware in PodLink USB mode.") from error
    if reply == b"MS2 DONE":
        return frozenset()
    if reply != b"MS2 READY":
        raise RuntimeError("The iPod did not recognize the Jellyfin sync protocol. Update its firmware.")
    owned = set()
    while True:
        link.send(podlink.TEXT, b"MS2 NEXT")
        reply = podlink.media_reply(link)
        if reply == b"MS2 DONE":
            return frozenset(owned)
        if not reply.startswith(b"MS2 FILE "):
            raise RuntimeError("could not read the iPod Jellyfin inventory")
        name = reply[9:].decode("ascii")
        if not SYNC_NAME.fullmatch(name):
            raise RuntimeError("invalid iPod Jellyfin inventory")
        if name.startswith(identity):
            owned.add(name)


def original_matches(link, tracks):
    candidates = defaultdict(list)
    for track in tracks:
        if track.tags:
            candidates[track.tags.key].append(track)
    if not candidates:
        return frozenset()

    link.send(podlink.TEXT, b"MS2 DB")
    reply = podlink.media_reply(link)
    if reply != b"MS2 DBREADY":
        if reply == b"MS2 ERR database unavailable":
            raise RuntimeError("The iPod music database must be ready before checking for duplicates.")
        raise RuntimeError("The iPod cannot read existing music for matching. Update its firmware.")

    matched = set()
    while True:
        link.send(podlink.TEXT, b"MS2 DBNEXT")
        reply = podlink.media_reply(link)
        if reply == b"MS2 DBDONE":
            return frozenset(matched)
        if reply == b"MS2 DBMORE":
            continue
        if not reply.startswith(b"MS2 TRACK "):
            raise RuntimeError("The iPod music database changed or could not be read. Preview again.")
        try:
            duration, disc, number, title, artists, album = reply[10:].decode("utf-8").split("\t")
            tags = music_tags(title, artists.split(";"), album, int(duration),
                              int(disc), int(number))
        except ValueError as error:
            raise RuntimeError("The iPod returned an invalid music database record.") from error
        if tags:
            matches = [track for track in candidates.get(tags.key, ()) if track.tags.matches(tags)]
            if len(matches) == 1:
                matched.add(matches[0].name)


def preview(source, scope, port, *, serial_access=nullcontext):
    tracks = source.tracks(scope)
    with serial_access(), connect_device(port) as (link, device):
        owned = inventory(link, source.identity)
        return Plan(source.identity, scope, device, tracks, owned, original_matches(link, tracks))


def prepare_track(source, track, directory):
    suffix = track.suffix if track.suffix in AUDIO_SUFFIXES else ".audio"
    original = directory / f"source{suffix}"
    source.download(track, original)
    if suffix not in AUDIO_SUFFIXES:
        converted = directory / "source.flac"
        podlink.media_command(
            "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(original),
            "-map", "0:a:0", "-map", "0:v:disp:attached_pic?", "-map_metadata", "0",
            "-c:a", "flac", "-c:v", "copy", str(converted))
        original = converted
    return podlink.prepare_media([original], directory)[0]


def wait_with_link(link, task):
    while not task.done():
        link.poll()
        time.sleep(0.02)
    link.poll()
    return task.result()


def validate_source(source, plan):
    if plan.identity != source.identity or source.tracks(plan.scope) != plan.tracks:
        raise RuntimeError("Jellyfin changed after preview; build a new preview")


def validate_device(link, device, plan):
    if (device != plan.device or inventory(link, plan.identity) != plan.owned
            or original_matches(link, plan.tracks) != plan.matched):
        raise RuntimeError("iPod changed after preview; build a new preview")


def apply(source, plan, port, *, serial_access=nullcontext):
    validate_source(source, plan)
    with (serial_access(), connect_device(port) as (link, device),
          ThreadPoolExecutor(max_workers=1) as worker):
        validate_device(link, device, plan)
        added = set()
        for track in plan.additions:
            with TemporaryDirectory(prefix="podlink-jellyfin-") as tmp:
                task = worker.submit(prepare_track, source, track, Path(tmp))
                try:
                    prepared = wait_with_link(link, task)
                    podlink.upload_media_track(link, prepared, track.name)
                finally:
                    # Keep the worker's staging directory alive until it finishes.
                    with suppress(Exception):
                        task.result()
                added.add(track.name)

        if plan.removals:
            current = wait_with_link(link, worker.submit(source.tracks, plan.scope))
            if current != plan.tracks:
                raise RuntimeError("Jellyfin changed during sync; deletion was skipped")
            if inventory(link, source.identity) != plan.owned | added:
                raise RuntimeError("iPod changed during sync; deletion was skipped")
            for name in sorted(plan.removals):
                link.send(podlink.TEXT, f"MS2 DEL {name}")
                podlink.expect_media(link, b"MS2 DELETED")
        if added or plan.removals:
            podlink.refresh_media(link)
    return len(added), len(plan.removals)
