"""Offline MS3 library and MS1 import contracts."""
from base64 import b64encode
from collections import deque
from dataclasses import astuple, replace
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

TOOLS = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(TOOLS), str(TOOLS / "herdremote")]

import hrlibrary as library
import podlink


TRACK = library.DeviceTrack(
    17, "/<HDD0>/Music/PodLink/Jellyfin/東京 song.flac",
    "夜の音", "Artist; Guest", "Album", "Artist", "Jazz", "Composer", "Group",
    "Line one\twith tabs\r\n" + "星" * 133, 2004, 2, 3, 85_000, 925,
    5, 7, 215_000, 33, 2_200_000_000, True)


def chunks(data):
    for offset in range(0, len(data), 366):
        end = offset + 366
        flag = b"." if end >= len(data) else b"+"
        yield b"MS3 DATA " + flag + b" " + b64encode(data[offset:end])


def record(track):
    return b"\0".join(
        str(int(field) if isinstance(field, bool) else field)
        .encode("utf-8", "surrogateescape")
        for field in astuple(track)
    ) + b"\0"


class FakeLink:
    max_message = 512
    timeout = 1

    def __init__(self, rows=(TRACK,)):
        self.rows = list(rows)
        self.requests = []
        self.frames = deque()
        self.pending = deque()
        self.media_busy = False
        self.changed = False
        self.art_reply = b"MS3 NOART"
        self.art_pixels = b""
        self.refreshes = 0
        self.stored = []
        self.next_upload_error = None
        self.incoming = None
        self.stops = 0

    def send(self, kind, payload):
        assert not self.frames
        if isinstance(payload, str):
            payload = payload.encode()
        self.requests.append(payload)
        reply = self.command(kind, payload)
        assert len(reply) <= 500
        self.frames.append(SimpleNamespace(type=podlink.TEXT, payload=reply))

    def receive(self, timeout):
        return self.frames.popleft()

    def command(self, kind, payload):
        if kind == podlink.BINARY:
            assert payload.startswith(b"MSD1") and self.incoming is not None
            self.incoming.extend(payload[4:])
            error, self.next_upload_error = self.next_upload_error, None
            return error or b"MS1 ACK"
        if payload.startswith(b"MS3 "):
            return self.library_command(payload[4:])
        if self.pending:
            return payload[:3] + b" ERR busy"
        if payload == b"MS1 REFRESH":
            self.refreshes += 1
            return b"MS1 QUEUED"
        if payload.startswith(b"MS1 PUT "):
            _, _, size, crc, self.incoming_name = payload.split()
            self.incoming_size = int(size, 16)
            self.incoming_crc = int(crc, 16)
            self.incoming = bytearray()
            return b"MS1 READY"
        if payload == b"MS1 END":
            assert len(self.incoming) == self.incoming_size
            assert zlib.crc32(self.incoming) == self.incoming_crc
            self.stored.append(bytes(self.incoming))
            self.incoming = None
            return b"MS1 STORED"
        if payload == b"MS1 STOP":
            self.stops += 1
            self.incoming = None
            return b"MS1 STOPPED"
        raise AssertionError(payload)

    def library_command(self, command):
        if self.media_busy:
            return b"MS3 ERR busy"
        if command == b"STOP":
            self.pending.clear()
            return b"MS3 STOPPED"
        if command == b"NEXT" and self.pending:
            if self.changed:
                self.pending.clear()
                return b"MS3 ERR database changed"
            return self.pending.popleft()
        if self.pending:
            return b"MS3 ERR busy"
        if command == b"SUMMARY":
            self.pending.append(
                f"MS3 SUMMARY ipod6g\tbuild\t80000000000\t9000000000\t"
                f"{len(self.rows)}\t51\t1\t0".encode())
            return b"MS3 MORE"
        if command == b"FILES" or command.startswith((b"ARTBIN ", b"STREAM ")):
            return b"MS3 ERR command"
        if command == b"DB":
            self.pending.append(b"MS3 MORE")
            for row in self.rows:
                self.pending.extend(chunks(record(row)))
            self.pending.append(b"MS3 END")
            return b"MS3 READY"
        if command.startswith(b"DETAIL "):
            assert command[7:] == TRACK.path.encode()
            return b"MS3 DETAIL 2389102\tFLAC\t44100\t2\t1"
        if command.startswith(b"ART "):
            assert command.split(b" ", 2)[2] == TRACK.path.encode()
            self.pending.extend(chunks(self.art_pixels))
            return self.art_reply
        if command.startswith(b"DEL "):
            path = command[4:].decode("utf-8")
            if ".." in path.split("/") or not path.startswith("/<HDD0>/Music/PodLink/"):
                return b"MS3 ERR unmanaged path"
            self.rows = [row for row in self.rows if row.path != path]
            return b"MS3 DELETED"
        raise AssertionError(command)


class LibraryTests(unittest.TestCase):
    def test_summary_continues_and_ignores_unrelated_frames(self):
        class ResidentLink(FakeLink):
            def send(self, kind, payload):
                super().send(kind, payload)
                self.frames.appendleft(
                    SimpleNamespace(type=podlink.TEXT, payload=b"CAPS other"))

        self.assertEqual(library.summary(ResidentLink()),
                         library.DeviceSummary("ipod6g", "build", 80_000_000_000,
                                               9_000_000_000, 1, 51, True))

    def test_summary_reports_database_busy(self):
        class BusyDatabase(FakeLink):
            def library_command(self, command):
                assert command == b"SUMMARY"
                return b"MS3 SUMMARY ipod6g\tbuild\t8192\t4096\t0\t51\t0\t1"

        result = library.summary(BusyDatabase())
        self.assertFalse(result.database_ready)
        self.assertEqual(result.track_count, 0)

    def test_summary_allows_slow_count_replies(self):
        class SlowCount(FakeLink):
            timeout = 10

            def receive(self, timeout):
                if timeout < 15:
                    raise TimeoutError("slow count")
                return super().receive(timeout)

        link = SlowCount()
        self.assertEqual(library.summary(link).track_count, 1)
        self.assertEqual(link.requests, [b"MS3 SUMMARY", b"MS3 NEXT"])

    def test_multi_reply_track_records_and_progress(self):
        other = replace(TRACK, index=31, title="Malformed tag\udcff",
                        path="/Music/song.mp3", managed=False)
        link = FakeLink([TRACK, other])
        received = []
        self.assertGreater(len(record(TRACK)), 500)
        self.assertEqual(library.tracks(link, received.append), [TRACK, other])
        self.assertEqual(received, [1, 2])
        self.assertNotIn(b"MS3 STOP", link.requests)

    def test_db_changes_mid_walk(self):
        link = FakeLink([TRACK, replace(TRACK, index=18)])

        def change_database(done):
            self.assertEqual(done, 1)
            link.changed = True

        with self.assertRaisesRegex(RuntimeError, "database changed"):
            library.tracks(link, change_database)
        self.assertEqual(link.requests[-1], b"MS3 STOP")

    def test_busy_interlock(self):
        link = FakeLink()
        link.media_busy = True
        with self.assertRaisesRegex(RuntimeError, "busy"):
            library.tracks(link)
        self.assertEqual(link.requests, [b"MS3 FILES"])
        link.media_busy = False
        link.send(podlink.TEXT, b"MS3 DB")
        self.assertEqual(link.receive(1).payload, b"MS3 READY")
        for command in (b"MS1 REFRESH", b"MS2 LIST"):
            link.send(podlink.TEXT, command)
            self.assertEqual(link.receive(1).payload, command[:3] + b" ERR busy")

    def test_detail_and_delete(self):
        link = FakeLink()
        self.assertEqual(library.detail(link, TRACK),
                         library.TrackDetail(2389102, "FLAC", 44100, 2, True))
        with self.assertRaisesRegex(ValueError, "PodLink"):
            library.delete(link, replace(TRACK, managed=False))
        with self.assertRaisesRegex(RuntimeError, "unmanaged path"):
            library.delete(link, replace(TRACK, path=TRACK.path + "/../test"))
        self.assertEqual(link.refreshes, 0)
        library.delete(link, TRACK)
        self.assertEqual(link.refreshes, 1)
        self.assertEqual(link.rows, [])

    def test_artwork_columns_and_rows(self):
        red, green, blue = 0xf800, 0x07e0, 0x001f
        white, black, yellow = 0xffff, 0, 0xffe0
        expected = bytes.fromhex("ff0000 00ff00 0000ff ffffff 000000 ffff00")
        link = FakeLink()
        link.art_reply = b"MS3 ART 3 2 columns"
        link.art_pixels = struct.pack("<6H", red, white, green, black, blue, yellow)
        self.assertEqual(library.artwork(link, TRACK), library.Artwork(3, 2, expected))
        link.art_reply = b"MS3 ART 3 2 rows"
        link.art_pixels = struct.pack("<6H", red, green, blue, white, black, yellow)
        self.assertEqual(library.artwork(link, TRACK, "list"),
                         library.Artwork(3, 2, expected))

    def test_artwork_multiple_replies(self):
        link = FakeLink()
        link.art_reply = b"MS3 ART 20 20 rows"
        link.art_pixels = struct.pack("<H", 0x001f) * 400
        self.assertEqual(library.artwork(link, TRACK, "list").rgb888,
                         b"\0\0\xff" * 400)
        self.assertEqual(link.requests.count(b"MS3 NEXT"), 3)

    def test_artwork_missing_and_placeholder(self):
        link = FakeLink()
        self.assertIsNone(library.artwork(link, TRACK))
        link.art_reply = b"MS3 PLACEHOLDER"
        self.assertIsNone(library.artwork(link, TRACK))
        binary, text = b"MS3 ARTBIN coverflow ", b"MS3 ART coverflow "
        self.assertEqual(link.requests, [
            command + TRACK.path.encode()
            for command in (b"MS3 STREAM ARTBIN coverflow ", binary, text, binary, text)
        ])

    def test_artwork_rejects_truncated_stream(self):
        link = FakeLink()
        link.art_reply = b"MS3 ART 3 2 rows"
        link.art_pixels = b"\0\0"
        with self.assertRaisesRegex(RuntimeError, "Invalid iPod artwork"):
            library.artwork(link, TRACK)

    def test_cache_is_keyed_by_serial_and_commit(self):
        with TemporaryDirectory() as directory:
            cache = library.LibraryCache(Path(directory))
            cache.store("../device", 51, [TRACK])
            self.assertEqual(cache.load("../device", 51), [TRACK])
            self.assertIsNone(cache.load("other device", 51))
            self.assertIsNone(cache.load("../device", 52))
            first = cache.art_path("../device", TRACK, "list")
            self.assertEqual(first.suffix, ".png")
            self.assertTrue(first.is_relative_to(directory))
            self.assertNotEqual(first, cache.art_path("../device", TRACK, "wps"))
            self.assertNotEqual(first, cache.art_path("other", TRACK, "list"))


def probe(*args):
    if "format_tags" not in args[args.index("-show_entries") + 1]:
        return json.dumps({"streams": [{"sample_rate": "44100", "codec_name": "flac"}]})
    name = Path(args[-1]).stem
    return json.dumps({"format": {"duration": "85.4", "tags": {
        "TITLE": TRACK.title if name == "device-copy" else "New cut",
        "ARTIST": "guest; artist",
        "ALBUM": TRACK.album,
        "DISC": "2/4",
        "TRACK": "3/12",
    }}, "streams": [{}]})


class ImportTests(unittest.TestCase):
    def test_duplicate_skip_before_upload_and_one_refresh(self):
        link, events = FakeLink(), []
        with TemporaryDirectory() as work, patch.object(podlink, "media_command", probe):
            paths = [Path(work) / (name + ".flac") for name in
                     ("device-copy", "new", "batch-copy")]
            for path in paths:
                path.write_bytes(b"music" * 600)
            stored = library.import_files(link, paths, events.append)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["source"], str(paths[1]))
        self.assertEqual(link.stored, [b"music" * 600])
        self.assertEqual(link.refreshes, 1)
        self.assertEqual(link.requests.count(b"MS3 DB"), 1)
        self.assertEqual([event["state"] for event in events],
                         ["probe", "skip", "probe", "upload", "done", "probe", "skip"])
        self.assertEqual(events[4]["sent"], 3000)
        self.assertEqual(events[4]["total"], 3000)

    def test_duplicate_check_can_be_disabled(self):
        link, events = FakeLink(), []
        with TemporaryDirectory() as work, patch.object(podlink, "media_command", probe):
            path = Path(work) / "device-copy.flac"
            path.write_bytes(b"music")
            library.import_files(link, [path], events.append, skip_duplicates=False)
        self.assertEqual(link.stored, [b"music"])
        self.assertEqual(link.refreshes, 1)
        self.assertNotIn(b"MS3 DB", link.requests)

    def test_failed_upload_stops_transfer_and_reports_error(self):
        link, events = FakeLink(), []
        link.next_upload_error = b"MS1 ERR write"
        with TemporaryDirectory() as work, patch.object(podlink, "media_command", probe):
            path = Path(work) / "new.flac"
            path.write_bytes(b"music")
            with self.assertRaisesRegex(RuntimeError, "ERR write"):
                library.import_files(link, [path], events.append)
        self.assertEqual(link.stops, 1)
        self.assertEqual(link.refreshes, 0)
        self.assertEqual(events[-1]["state"], "error")
        self.assertIsNotNone(events[-1]["error"])


TAGCACHE_FIXTURE = Path(__file__).parent / "fixtures" / "tagcache"


def tagcache_fixture():
    return {file.name: file.read_bytes() for file in TAGCACHE_FIXTURE.glob("*.tcd")}


def fixture_walk():
    class ReferenceWalk(FakeLink):
        def library_command(self, command):
            if command == b"DB":
                self.pending.extend((TAGCACHE_FIXTURE / "walk.txt").read_bytes().splitlines())
                return b"MS3 READY"
            return super().library_command(command)
    return library.tracks(ReferenceWalk())


class BulkLink(FakeLink):
    def __init__(self, files=None):
        super().__init__()
        self.files = tagcache_fixture() if files is None else files
        self.offset = 0
        self.selected = None
        self.manifests = 0
        self.final_generation = 3
        self.offset_error = 0
        self.crc_error = 0

    def send(self, kind, payload):
        if payload == b"MS3 NEXT" and self.selected is not None:
            data = self.files[self.selected]
            if self.offset < len(data):
                assert not self.frames
                self.requests.append(payload)
                chunk = data[self.offset:self.offset + self.max_message - 8]
                frame = b"MSF1" + struct.pack("<I", self.offset + self.offset_error) + chunk
                self.frames.append(SimpleNamespace(type=podlink.BINARY, payload=frame))
                self.offset += len(chunk)
                return
        super().send(kind, payload)

    def library_command(self, command):
        if command == b"FILES":
            self.manifests += 1
            generation = 3 if self.manifests == 1 else self.final_generation
            sizes = "".join(f"\t{name}:{len(data)}" for name, data in self.files.items())
            return f"MS3 FILES 1\t{generation}\t0\t0{sizes}".encode()
        if command.startswith((b"FILE ", b"SAMPLE ")):
            verb, path = command.split(b" ", 1)
            self.selected = path.decode()
            self.offset = 0
            return b"MS3 " + verb + f" {len(self.files[self.selected])}".encode()
        if command == b"NEXT" and self.selected is not None:
            data = self.files[self.selected]
            self.selected = None
            return f"MS3 DONE {len(data)} {zlib.crc32(data) ^ self.crc_error:08x}".encode()
        if command == b"STOP":
            self.selected = None
        return super().library_command(command)


class StreamLink(BulkLink):
    """A peer that queues a window behind the legacy metadata."""
    def send(self, kind, payload):
        if payload.startswith((b"MS3 STREAM FILE ", b"MS3 STREAM SAMPLE ")):
            super().send(kind, b"MS3 " + payload[11:])
            self.requests[-1] = payload
        elif payload == b"MS3 CREDIT":
            self.requests.append(payload)
        elif payload == b"MS3 STOP":
            self.requests.append(payload)
            reply = self.library_command(b"STOP")
            self.frames.append(SimpleNamespace(type=podlink.TEXT, payload=reply))
            return
        else:
            super().send(kind, payload)
            return

        for _ in range(8):
            data = self.files[self.selected]
            chunk = data[self.offset:self.offset + self.max_message - 8]
            if not chunk:
                reply = self.library_command(b"NEXT")
                self.frames.append(SimpleNamespace(type=podlink.TEXT, payload=reply))
                break
            frame = b"MSF1" + struct.pack("<I", self.offset + self.offset_error) + chunk
            self.frames.append(SimpleNamespace(type=podlink.BINARY, payload=frame))
            self.offset += len(chunk)


class BulkLibraryTests(unittest.TestCase):
    def test_recording_fetch_uses_credit_and_preserves_output_on_bad_crc(self):
        recording = "/Recordings/PodLink-0001.wav"
        data = b"RIFF" + bytes(range(255)) * 30
        with TemporaryDirectory() as directory:
            output = Path(directory) / "record.wav"
            link = StreamLink({recording: data})
            library.download_sample(link, recording, output)
            self.assertEqual(output.read_bytes(), data)
            self.assertEqual(link.requests[0], b"MS3 STREAM SAMPLE " + recording.encode())
            self.assertIn(b"MS3 CREDIT", link.requests)
            link = StreamLink({recording: b"different data"})
            link.crc_error = 1
            with self.assertRaisesRegex(ValueError, "CRC mismatch"):
                library.download_sample(link, recording, output)
            self.assertEqual(output.read_bytes(), data)
            self.assertEqual(link.requests[-1], b"MS3 STOP")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_stream_uses_credits_without_next_round_trips(self):
        link = StreamLink()
        link.max_message = 48
        self.assertEqual(library.tracks(link), fixture_walk())
        self.assertIn(b"MS3 CREDIT", link.requests)
        self.assertNotIn(b"MS3 NEXT", link.requests)
        self.assertEqual(link.manifests, 2)

    def test_stream_stop_drains_already_queued_frames(self):
        link = StreamLink()
        link.max_message = 48
        link.offset_error = 1
        with self.assertRaisesRegex(RuntimeError, "invalid database chunk"):
            library.tracks(link)
        self.assertEqual(link.requests[-1], b"MS3 STOP")
        self.assertFalse(link.frames)

    def test_profile_download_validates_and_discards_sample(self):
        class ProfileLink(StreamLink):
            def library_command(self, command):
                if command == b"PROFILE FAST":
                    return b"MS3 PROFILE READY"
                if command == b"PROFILE CPU":
                    return b"MS3 PROFILE" + b" 1,2,3,4" * 8 + b" 20,50"
                if command == b"SAMPLE /Music/PodLink/sample.flac":
                    self.selected = "sample"
                    self.offset = 0
                    return f"MS3 SAMPLE {len(self.files['sample'])}".encode()
                return super().library_command(command)

        sample = bytes(range(251)) * 4
        link = ProfileLink({"sample": sample})
        link.port = SimpleNamespace(read=lambda n: b"")
        result = library.profile_download(link, "/Music/PodLink/sample.flac", "fast")
        self.assertEqual(result["bytes"], len(sample))
        self.assertEqual(result["crc32"], f"{zlib.crc32(sample):08x}")
        self.assertEqual(result["device"]["dma_gap"]["max_us"], 4)
        self.assertEqual(result["device_idle_percent"], 40)

    def test_bulk_artwork(self):
        class ArtLink(BulkLink):
            def library_command(self, command):
                if command == b"ARTBIN list " + TRACK.path.encode():
                    self.selected = "art"
                    self.offset = 0
                    return b"MS3 ARTBIN 20 20 columns"
                return super().library_command(command)

        # A column of red, followed by nineteen columns of blue.
        pixels = bytes.fromhex("00 f8") * 20 + bytes.fromhex("1f 00") * 380
        link = ArtLink({"art": pixels})
        image = library.artwork(link, TRACK, "list")
        self.assertEqual(image.rgb888, (b"\xff\0\0" + b"\0\0\xff" * 19) * 20)
        self.assertEqual(link.requests.count(b"MS3 NEXT"), 3)
        self.assertNotIn(b"MS3 STOP", link.requests)

        for field in ("offset_error", "crc_error"):
            with self.subTest(field=field):
                setattr(link, field, 1)
                with self.assertRaisesRegex(RuntimeError, "Invalid iPod artwork"):
                    library.artwork(link, TRACK, "list")
                self.assertEqual(link.requests[-1], b"MS3 STOP")
                setattr(link, field, 0)

    def test_parser_matches_real_firmware_walk(self):
        expected = fixture_walk()
        self.assertEqual(library.parse_tagcache(tagcache_fixture(), expected_commit=1), expected)
        self.assertEqual(len(expected), 4)
        self.assertTrue(expected[0].managed)
        self.assertEqual(expected[1].title, "Pilot")

    def test_deleted_slots_and_unsigned_time(self):
        files = tagcache_fixture()
        master = bytearray(files["database_idx.tcd"])
        # Put invalid seeks in a deleted slot and keep the original live indices.
        struct.pack_into("<24i", master, 24 + 96, *([-1] * 23), 1)
        struct.pack_into("<I", master, 24 + 20 * 4, 0xf0000000)
        struct.pack_into("<i", master, 24 + 15 * 4, 7)
        files["database_idx.tcd"] = master
        tracks = fixture_walk()
        self.assertEqual(library.parse_tagcache(files),
                         [replace(tracks[0], mtime=0xf0000000, play_count=7), *tracks[2:]])

    def test_rejects_truncated_tag_table(self):
        files = tagcache_fixture()
        files["database_4.tcd"] = files["database_4.tcd"][:-1]
        with self.assertRaises(ValueError):
            library.parse_tagcache(files)

    def test_bulk_download_verifies_all_files_and_reports_bytes(self):
        link = BulkLink()
        link.max_message = 384  # Force the master file across multiple frames.
        reports = []
        tracks = library.tracks(link, byte_progress=lambda *event: reports.append(event),
                                expected_commit=1)
        self.assertEqual(tracks, fixture_walk())
        total = sum(map(len, link.files.values()))
        self.assertEqual(reports[-1], (total, total))
        self.assertTrue(all(a < b and size == total == next_size
                            for (a, size), (b, next_size) in zip(reports, reports[1:])))
        self.assertGreater(len(reports), len(link.files))
        self.assertEqual(link.manifests, 2)
        self.assertNotIn(b"MS3 DB", link.requests)

    def test_bad_chunk_or_crc_stops_transfer(self):
        for error, reason in [("offset_error", "invalid database chunk"),
                              ("crc_error", "CRC mismatch")]:
            with self.subTest(error=error):
                link = BulkLink()
                setattr(link, error, 1)
                with self.assertRaisesRegex(RuntimeError, reason):
                    library.tracks(link)
                self.assertEqual(link.requests[-1], b"MS3 STOP")
                self.assertNotIn(b"MS3 DB", link.requests)

    def test_snapshot_changes_are_rejected(self):
        link = BulkLink()
        link.final_generation += 1
        with self.assertRaisesRegex(RuntimeError, "database changed"):
            library.tracks(link)
        self.assertEqual(link.manifests, 2)

        link = BulkLink()
        with self.assertRaisesRegex(RuntimeError, "database changed"):
            library.tracks(link, expected_commit=2)
        self.assertEqual(link.requests, [b"MS3 FILES"])


if __name__ == "__main__":
    unittest.main()
