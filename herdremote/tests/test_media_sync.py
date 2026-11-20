"""Offline contracts for Jellyfin reconciliation and deletion gating."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(TOOLS), str(TOOLS / "herdremote")]

import hrmediasync as sync
import podlink

USER = "a" * 32
LIBRARY = "b" * 32
PLAYLIST = "c" * 32
ITEM = "d" * 32


class SourceTests(unittest.TestCase):
    def test_password_login_exchanges_password_for_user_token(self):
        opener = Mock()
        opener.open.side_effect = [
            io.BytesIO(b'{"AccessToken":"returned-token"}'),
            io.BytesIO(json.dumps({"Id": USER}).encode()),
        ]
        client = sync.JellyfinClient(opener=opener)
        source = sync.JellyfinSource.with_password(
            "https://audio.test/jellyfin", " listener ", "test password", client)
        auth, identify = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(auth.get_method(), "POST")
        self.assertEqual(auth.full_url, "https://audio.test/jellyfin/Users/AuthenticateByName")
        self.assertEqual(json.loads(auth.data), {"Username": "listener", "Pw": "test password"})
        self.assertIn('Client="AstraPod"', auth.get_header("Authorization"))
        self.assertEqual(identify.get_header("Authorization"), 'MediaBrowser Token="returned-token"')
        self.assertIsNone(identify.data)
        self.assertEqual(source.session.user, USER)
        self.assertEqual(source.session.token, "returned-token")

    def test_password_login_rejects_http_before_sending_credentials(self):
        client = Mock()
        with self.assertRaisesRegex(sync.JellyfinError, "HTTPS"):
            sync.JellyfinSource.with_password("http://audio.test", "listener", "test", client)
        client.authenticate.assert_not_called()

    def test_missing_token_does_not_fall_back_to_another_account(self):
        client = Mock()
        client.authenticate.return_value = {}
        with self.assertRaisesRegex(sync.JellyfinError, "no user access token"):
            sync.JellyfinSource.with_password("https://audio.test", "listener", "test", client)
        client.request.assert_not_called()

    def test_scope_selection_and_stable_track_identity(self):
        client = Mock()
        client.request.side_effect = [
            {"Id": USER},
            {"Items": [{"Id": LIBRARY, "Name": "Albums", "CollectionType": "music"}]},
        ]
        song = {"Type": "Audio", "Id": ITEM, "Name": "Track",
                "Path": "/media/Track.FLAC", "Etag": "v1"}
        client.page.side_effect = [
            ([{"Id": PLAYLIST, "Name": "On the go"}], 1),
            ([song], 1), ([song], 1), ([song, song, {"Type": "Movie"}], 3),
            ([dict(song, Etag="v2")], 1),
        ]
        source = sync.JellyfinSource("https://audio.test", "token", client)
        all_music, library, playlist = source.scopes()
        self.assertEqual([all_music.kind, library.kind, playlist.kind],
                         ["all", "library", "playlist"])
        all_tracks = source.tracks(all_music)
        self.assertEqual(source.tracks(library), all_tracks)
        self.assertEqual(source.tracks(playlist), all_tracks)
        self.assertNotEqual(source.tracks(library), all_tracks)
        calls = client.page.call_args_list
        self.assertIsNone(calls[1].kwargs["ParentId"])
        self.assertEqual(calls[2].kwargs["ParentId"], LIBRARY)
        self.assertEqual(calls[3].args[1], f"Playlists/{PLAYLIST}/Items")


class OriginalMatchingTests(unittest.TestCase):
    def test_matches_unicode_tags_and_near_duration_but_not_other_editions(self):
        def track(title, artist, album, duration, disc, number, key):
            tags = sync.music_tags(title, [artist], album, duration, disc, number)
            return sync.Track(key, title, ".flac", key, tags)

        same = track("ＣＡＦÉ", "The   Artist", "The Album", 182000, 1, 5, "same")
        live = track("CAFÉ", "The Artist", "The Album", 190000, 1, 5, "live")
        alternate_disc = track("CAFÉ", "The Artist", "The Album", 182000, 2, 5, "disc")
        other_album = track("CAFÉ", "The Artist", "Other Album", 182000, 1, 5, "album")
        tracks = (same, live, alternate_disc, other_album)
        link = Mock(timeout=1)
        link.receive.side_effect = [
            podlink.Frame(podlink.TEXT, 0, line) for line in (
                b"MS2 DBREADY", b"MS2 DBMORE",
                "MS2 TRACK 181900\t1\t5\tCafe\u0301\tThe Artist\tThe Album".encode(),
                b"MS2 DBDONE")]
        matches = sync.original_matches(link, tracks)
        plan = sync.Plan("owner", sync.Scope("all", "All"), "usb", tracks,
                         frozenset({"old-managed"}), matches)
        self.assertEqual(plan.originals, (same,))
        self.assertEqual(plan.additions, (live, alternate_disc, other_album))
        self.assertEqual(plan.removals, {"old-managed"})

    def test_ambiguous_and_incomplete_tags_cannot_suppress_uploads(self):
        tags = sync.music_tags("Title", ["Artist"], "Album", 180000, 1, 5)
        tracks = (sync.Track("a", "Title", ".flac", "one", tags),
                  sync.Track("b", "Title", ".flac", "two", tags))
        link = Mock(timeout=1)
        link.receive.side_effect = [
            podlink.Frame(podlink.TEXT, 0, line) for line in (
                b"MS2 DBREADY", b"MS2 TRACK 180000\t1\t5\tTitle\tArtist\tAlbum",
                b"MS2 DBDONE")]
        self.assertEqual(sync.original_matches(link, tracks), frozenset())
        self.assertIsNone(sync.music_tags("Title", ["Artist"], "<Untagged>", 180000, 1, 5))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        identity = "e" * 24
        self.addition = sync.Track(ITEM, "Track", ".flac", identity + "a" * 40 + ".flac")
        self.removal = identity + "b" * 40 + ".flac"
        self.source = Mock(identity=identity)
        self.link = Mock(timeout=1)
        self.plan = sync.Plan(identity, sync.Scope("all", "All"), "usb-unit-1",
                              (self.addition,), frozenset([self.removal]))
        self.source.tracks.return_value = self.plan.tracks

    @contextmanager
    def connection(self, port):
        yield self.link, "usb-unit-1"

    def test_inventory_ignores_other_accounts(self):
        other = "c" * 64 + ".flac"
        self.link.receive.side_effect = [
            podlink.Frame(podlink.TEXT, 0, line) for line in
            (b"DH\t1\tdisk-mode-v1", b"ci\t1\t131072\tneighbors",
             b"MS2 READY", f"MS2 FILE {self.removal}".encode(),
             f"MS2 FILE {other}".encode(), b"MS2 DONE")]
        self.assertEqual(sync.inventory(self.link, self.plan.identity), self.plan.owned)

    def test_add_then_delete_only_previewed_files(self):
        with patch.object(sync, "connect_device", self.connection), \
                patch.object(sync, "inventory", side_effect=[
                    self.plan.owned, self.plan.owned | {self.addition.name}]), \
                patch.object(sync, "prepare_track"), \
                patch.object(podlink, "upload_media_track") as upload, \
                patch.object(podlink, "expect_media"), \
                patch.object(podlink, "refresh_media"):
            self.assertEqual(sync.apply(self.source, self.plan, None), (1, 1))
        upload.assert_called_once()
        self.link.send.assert_called_once_with(podlink.TEXT, f"MS2 DEL {self.removal}")

    def test_upload_failure_skips_deletion(self):
        with patch.object(sync, "connect_device", self.connection), \
                patch.object(sync, "inventory", return_value=self.plan.owned), \
                patch.object(sync, "prepare_track"), \
                patch.object(podlink, "upload_media_track", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                sync.apply(self.source, self.plan, None)
        self.link.send.assert_not_called()

    def test_source_change_skips_deletion(self):
        self.source.tracks.side_effect = [self.plan.tracks, ()]
        with patch.object(sync, "connect_device", self.connection), \
                patch.object(sync, "inventory", return_value=self.plan.owned), \
                patch.object(sync, "prepare_track"), \
                patch.object(podlink, "upload_media_track"):
            with self.assertRaisesRegex(RuntimeError, "deletion was skipped"):
                sync.apply(self.source, self.plan, None)
        self.link.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
