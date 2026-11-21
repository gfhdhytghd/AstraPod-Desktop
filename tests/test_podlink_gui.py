"""Offscreen tests for the AstraPod desktop app, run against the fakes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

TOOLS = Path(__file__).resolve().parents[1]
for _p in (str(TOOLS), str(TOOLS / "herdremote")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import podlink_gui  # noqa: E402  (the entry point must keep importing)
import podlink_settings  # noqa: E402
import podlink_ui_theme  # noqa: E402
from astrapod_desktop import backends, contracts, fakes, models  # noqa: E402
from astrapod_desktop.app import create_application, demo_services  # noqa: E402
from astrapod_desktop.backends import ProbedFile  # noqa: E402
from astrapod_desktop.jobs import JobRunner  # noqa: E402
from astrapod_desktop.pages.extensions import normalize_schema  # noqa: E402
from podlink_ui_theme import QtCore, QtWidgets  # noqa: E402
from astrapod_desktop.qt import QtGui  # noqa: E402
import importlib  # noqa: E402
QtTest = importlib.import_module(f"{podlink_ui_theme.QT_BINDING}.QtTest")
from astrapod_desktop.qt import QtNetwork, Qt  # noqa: E402


def wait_until(app, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents()
    if not predicate():
        raise AssertionError("timed out waiting for Qt UI condition")


def track(index, title, artist="A", album="Album", length=200_000, **extra):
    values = dict(index=index, path=f"/Music/PodLink/{title}.flac", title=title, artist=artist,
                  album=album, album_artist=artist, genre="Pop", composer="", grouping="",
                  comment="", year=2020, disc=1, track=index + 1, length_ms=length,
                  bitrate_kbps=320, play_count=0, rating=0, play_time_ms=0, last_played=0,
                  mtime=0, managed=True)
    values.update(extra)
    return contracts.DeviceTrack(**values)


class GuiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["test"])

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="astrapod-gui-test-")
        root = Path(self._tmp.name)
        (root / "run").mkdir(mode=0o700)
        self._env = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_RUNTIME_DIR": str(root / "run"), "JELLYFIN_URL": "", "JELLYFIN_API_KEY": "",
            "CIDER_API_URL": "", "CIDER_BACKEND": "", "RUFIN_API_URL": "",
            "HERDR_SOCKET_PATH": "", "HERDREMOTE_VOICE_FOCUS": "",
            "HERDREMOTE_USAGE_CACHE_DIR": "", "HERDREMOTE_KEY_TOOL": "", "T3_CONTROL": "", "T3_FOLLOW_IPOD": "", "T3_BIN": "",
        })
        self._env.start()
        self.root = root
        self.appearance = fakes.FakeAppearance()
        self.services = demo_services(delay=False, appearance=self.appearance)
        self.services.daemon.tick = False
        _app, self.theme, self.window = create_application(["test"], self.services)
        self.window.set_motion(False)
        self.window.daemon.start()
        self.errors: list = []
        self.window.show_error = self.errors.append
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.runtime = None
        wait_until(self.app, lambda: not self.window.busy, 10)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self._env.stop()
        self._tmp.cleanup()

    def load_library(self) -> None:
        self.window.library_state.refresh(True)
        wait_until(self.app, lambda: self.window.library_state.source == "device"
                   and not self.window.busy)


class ThemeAndNavigationTests(GuiCase):
    def test_theme_persists_and_switches_live(self) -> None:
        seen = []
        self.theme.theme_changed.connect(lambda mode, resolved: seen.append((mode, resolved)))
        self.window.select_theme("dark")
        self.assertEqual(self.theme.resolved, "dark")
        prefs = json.loads(podlink_ui_theme.ui_prefs_path().read_text())
        self.assertEqual(prefs["theme"], "dark")
        self.assertEqual(seen[-1], ("dark", "dark"))
        self.window.select_theme("light")
        self.assertEqual(self.theme.tokens.name, "light")
        settings = self.window.pages["settings"]
        self.assertEqual(settings.theme_switch.current(), "light")

    def test_device_screen_follows_the_link(self) -> None:
        page = self.window.pages["device"]
        state = self.window.library_state
        state.summary, state.tracks, state.source = None, [], "none"
        self.window.daemon.state["link"] = {"connected": False}
        page.update_view()
        self.assertFalse(page.drawing.connected)
        self.window.daemon.state["link"] = {"connected": True, "mode": "podlink"}
        page.update_view()
        self.assertTrue(page.drawing.connected)

    def shortcut(self, sequence: str):
        wanted = QtGui.QKeySequence(sequence)
        return next(s for s in self.window.findChildren(QtGui.QShortcut) if s.key() == wanted)

    def test_capacity_bar_moves_from_what_it_shows(self) -> None:
        from astrapod_desktop.widgets import MOTION, CapacityBar
        bar = CapacityBar()
        self.addCleanup(bar.deleteLater)
        with patch.dict(MOTION, {"on": True}):
            bar.set_usage(100, 40, 10, 50, animate=False)
            bar.set_usage(100, 40, 10, 50)
            self.assertEqual(bar._drawn(), (0.4, 0.1))
            bar.set_usage(100, 60, 10, 30)
            self.assertEqual(bar._drawn(), (0.4, 0.1))

    def test_sidebar_and_shortcuts_switch_pages(self) -> None:
        for key in ("device", "albums", "import", "sync", "agents", "usage",
                    "extensions", "settings", "logs", "artists", "device.songs",
                    "device.composers"):
            self.window.navigate(key)
            self.assertIs(self.window.stack.currentWidget(), self.window.pages[key])
            self.assertEqual(self.window.sidebar.current_key(), key)
        self.window.sidebar.select("songs")
        self.assertIs(self.window.stack.currentWidget(), self.window.pages["songs"])
        self.assertTrue(self.window.search.isEnabled())
        self.assertEqual(self.window.search.placeholderText(), "搜索 Jellyfin 资料库")
        self.window.navigate("device.albums")
        self.assertEqual(self.window.search.placeholderText(), "搜索 iPod 资料库")
        self.window.navigate("usage")
        self.assertFalse(self.window.search.isEnabled())
        self.assertNotIn("nowplaying", self.window.pages)
        self.assertFalse(hasattr(self.window, "transport"))
        for number, key in enumerate(("device", "songs", "albums", "artists", "genres",
                                      "composers", "import", "sync", "agents"), 1):
            self.shortcut(f"Ctrl+{number}").activated.emit()
            self.assertIs(self.window.stack.currentWidget(), self.window.pages[key])
        self.shortcut("Ctrl+,").activated.emit()
        self.assertIs(self.window.stack.currentWidget(), self.window.pages["settings"])

    def test_device_node_expands_into_its_library(self) -> None:
        sidebar = self.window.sidebar
        rows = sidebar.rows
        children = [f"device.{view}" for view in ("songs", "albums", "artists", "genres",
                                                    "composers")]
        self.assertEqual(sidebar.children("device"), children)
        self.assertFalse(sidebar.is_expanded("device"))
        self.assertTrue(all(sidebar.isRowHidden(rows[key]) for key in children))
        self.window.navigate("device.genres")
        self.assertTrue(sidebar.is_expanded("device"))
        self.assertFalse(any(sidebar.isRowHidden(rows[key]) for key in children))
        self.assertEqual(self.window.prefs["sidebar_expanded"], ["device"])
        self.load_library()
        self.assertEqual(sidebar.model_.item(rows["device"]).text(), "iPod classic")
        self.window.navigate("device.genres")
        self.assertFalse(self.window.fab.isHidden())
        self.window.navigate("genres")
        self.assertTrue(self.window.fab.isHidden())
        self.window.navigate("device.genres")
        sidebar.set_expanded("device", False)
        self.assertTrue(all(sidebar.isRowHidden(rows[key]) for key in children))
        self.assertIs(self.window.stack.currentWidget(), self.window.pages["device"])
        self.assertEqual(self.window.prefs["sidebar_expanded"], [])
        self.window.navigate("device.songs")
        self.assertTrue(sidebar.is_expanded("device"))
        self.assertEqual(sidebar.current_key(), "device.songs")
        self.app.sendEvent(sidebar, QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
                                                    Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(sidebar.current_key(), "device")
        self.app.sendEvent(sidebar, QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
                                                    Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier))
        self.assertFalse(sidebar.is_expanded("device"))
        self.app.sendEvent(sidebar, QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
                                                    Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
        self.assertTrue(sidebar.is_expanded("device"))

    def test_clicking_the_device_node_opens_it_and_toggles_it(self) -> None:
        self.window.resize(1260, 820)
        self.window.show()
        self.window.navigate("songs")
        self.app.processEvents()
        sidebar = self.window.sidebar
        rect = sidebar.visualRect(sidebar.model_.index(sidebar.rows["device"], 0))
        QtTest.QTest.mouseClick(sidebar.viewport(), Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier, rect.center())
        self.assertIs(self.window.stack.currentWidget(), self.window.pages["device"])
        self.assertFalse(sidebar.is_expanded("device"))
        QtTest.QTest.mouseClick(sidebar.viewport(), Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier, rect.center())
        self.assertTrue(sidebar.is_expanded("device"))
        self.window.navigate("songs")
        chevron = sidebar.delegate.chevron_area(QtCore.QRectF(rect)).center().toPoint()
        QtTest.QTest.mouseClick(sidebar.viewport(), Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier, chevron)
        self.assertFalse(sidebar.is_expanded("device"))
        self.assertIs(self.window.stack.currentWidget(), self.window.pages["songs"])

    def test_search_text_is_kept_per_page(self) -> None:
        self.load_library()
        self.window.navigate("device.songs")
        self.window.search.setText("Harbor")
        self.window.navigate("device.albums")
        self.assertEqual(self.window.search.text(), "")
        self.window.navigate("songs")
        self.assertEqual(self.window.search.text(), "")
        self.window.navigate("device.songs")
        self.assertEqual(self.window.search.text(), "Harbor")
        tracks = self.window.pages["device.songs"].table.proxy.tracks()
        self.assertTrue(tracks)
        self.assertTrue(all("harbor" in f"{t.title} {t.artist} {t.album}".casefold() for t in tracks))


class MaterialThemeTests(unittest.TestCase):
    TEXT_PAIRS = (("on_surface", "surface"), ("on_surface", "surface_container"),
                  ("on_surface", "surface_container_highest"),
                  ("on_surface_variant", "surface"), ("on_surface_variant", "surface_container_high"),
                  ("on_primary", "primary"), ("on_primary_container", "primary_container"),
                  ("on_secondary_container", "secondary_container"),
                  ("on_tertiary_container", "tertiary_container"),
                  ("on_error", "error"), ("on_error_container", "error_container"),
                  ("inverse_on_surface", "inverse_surface"), ("primary", "surface"),
                  ("error", "surface"))
    SEEDS = ("#E0245E", "#6750A4", "#1E6FD9", "#00897B", "#7CB342", "#F4A100", "#808080")

    def test_palette_is_deterministic_and_matches_the_reference(self) -> None:
        import podlink_m3
        self.assertEqual(podlink_m3.scheme(0xFF6750A4, False)["primary"].upper(), "#6750A4")
        first = podlink_ui_theme.build_tokens("#E0245E", False)
        podlink_m3.scheme.cache_clear()
        podlink_m3.hct_to_argb.cache_clear()
        self.assertEqual(first, podlink_ui_theme.build_tokens("#E0245E", False))
        for argb in (0xFF0000FF, 0xFFE0245E, 0xFF00897B):
            hue, chroma, _j = podlink_m3.cam16(argb)
            self.assertEqual(podlink_m3.hct_to_argb(hue, chroma, podlink_m3.tone_of(argb)), argb)

    def test_text_roles_meet_contrast_in_both_modes(self) -> None:
        import podlink_m3
        for seed in self.SEEDS:
            for dark in (False, True):
                tokens = podlink_ui_theme.build_tokens(seed, dark)
                for fg, bg in self.TEXT_PAIRS:
                    ratio = podlink_m3.contrast(getattr(tokens, fg), getattr(tokens, bg))
                    self.assertGreaterEqual(ratio, 4.5, f"{seed} dark={dark} {fg}/{bg} {ratio:.2f}")

    def test_seed_from_pixels_picks_the_colourful_hue(self) -> None:
        import podlink_m3
        pixels = [0xFF202020] * 50 + [0xFF1E6FD9] * 20 + [0xFFE8E8E8] * 30
        seed = podlink_m3.seed_from_pixels(pixels)
        self.assertAlmostEqual(podlink_m3.cam16(seed)[0], podlink_m3.cam16(0xFF1E6FD9)[0], delta=8)
        self.assertIsNone(podlink_m3.seed_from_pixels([0xFF808080] * 10))


class SystemThemeTests(GuiCase):
    def test_default_follows_the_desktop_accent_and_scheme_live(self) -> None:
        self.assertEqual(self.theme.mode, "system")
        self.assertEqual(self.window.color_source, "system")
        self.assertEqual(self.theme.seed, "#3584E4")
        settings = self.window.pages["settings"]
        self.assertEqual(settings.theme_switch.button("system").text(), "跟随系统")
        self.assertIn("#3584E4", settings.color_source_help.text())
        self.assertIn("GNOME 设置", settings.color_source_help.text())
        self.appearance.set(scheme="dark")
        self.assertEqual(self.theme.resolved, "dark")
        self.appearance.set(scheme="light", accent="#E62D42")
        self.assertEqual(self.theme.resolved, "light")
        wait_until(self.app, lambda: self.theme.seed == "#E62D42")
        self.assertIn("#E62D42", settings.color_source_help.text())
        self.window.select_theme("dark")
        self.appearance.set(scheme="light")
        self.assertEqual(self.theme.resolved, "dark")

    def test_portal_then_hints_then_gsettings_then_palette(self) -> None:
        theme = podlink_ui_theme
        values = {"portal:color-scheme": None, "portal:accent-color": None,
                  "gs:color-scheme": "prefer-dark", "gs:accent-color": "teal"}
        with patch.object(theme, "read_portal", lambda key: values["portal:" + key]), \
                patch.object(theme, "read_gsettings", lambda key: values["gs:" + key]), \
                patch.object(theme, "qt_hint_scheme", lambda _app: None):
            appearance = theme.SystemAppearance(listen=False)
            self.assertEqual(appearance.scheme(self.app), "dark")
            self.assertEqual(appearance.accent(), ("#2190A4", "gsettings"))
            values.update({"portal:color-scheme": 2, "portal:accent-color": [0.2, 0.4, 0.8]})
            appearance.refresh()
            self.assertEqual(appearance.scheme(self.app), "light")
            self.assertEqual(appearance.accent(), ("#3366CC", "portal"))
            values.update({"portal:color-scheme": 0, "portal:accent-color": [1.5, 0, 0],
                           "gs:color-scheme": None, "gs:accent-color": None})
            appearance.refresh()
            self.assertIsNone(appearance.scheme(self.app))
            palette = podlink_ui_theme.QtGui.QPalette()
            palette.setColor(palette.ColorRole.Highlight, podlink_ui_theme.QtGui.QColor("#6750A4"))
            self.assertEqual(appearance.accent(palette), ("#6750A4", "palette"))
        with patch.object(theme, "read_portal", lambda key: None), \
                patch.object(theme, "read_gsettings", lambda key: "prefer-dark"), \
                patch.object(theme, "qt_hint_scheme", lambda _app: "light"):
            self.assertEqual(theme.SystemAppearance(listen=False).scheme(self.app), "light")

    def test_busctl_replies_parse(self) -> None:
        theme = podlink_ui_theme
        self.assertEqual(theme.portal_value('{"type":"v","data":[{"type":"u","data":2}]}'), 2)
        reply = '{"type":"v","data":[{"type":"(ddd)","data":[0.207,0.518,0.894]}]}'
        self.assertEqual(theme.portal_accent(theme.portal_value(reply)), "#3584E4")
        self.assertIsNone(theme.portal_value(None))
        self.assertIsNone(theme.portal_value("not json"))
        self.assertEqual(theme.portal_scheme(1), "dark")
        self.assertIsNone(theme.portal_scheme(0))
        self.assertEqual(theme.gsettings_scheme("prefer-light"), "light")
        self.assertEqual(theme.gsettings_accent("blue"), "#3584E4")
        self.assertIsNone(theme.gsettings_accent("rainbow"))


class RailTests(GuiCase):
    def test_narrow_window_folds_the_drawer_into_a_rail(self) -> None:
        self.window.resize(1260, 820)
        self.window.show()
        self.app.processEvents()
        self.assertFalse(self.window.compact)
        self.assertEqual(self.window.side_panel.width(), 272)
        self.window.resize(900, 700)
        wait_until(self.app, lambda: self.window.compact)
        self.assertEqual(self.window.side_panel.width(), 88)
        self.assertTrue(self.window.sidebar.compact)
        self.assertFalse(self.window.brand_name.isVisible())
        self.window.sidebar.select("device.albums")
        self.assertIs(self.window.stack.currentWidget(), self.window.pages["device.albums"])
        self.assertTrue(self.window.fab.isVisible())
        self.window.sidebar.select("albums")
        self.assertFalse(self.window.fab.isVisible())
        self.window.navigate("settings")
        self.assertFalse(self.window.fab.isVisible())
        self.window.resize(1260, 820)
        wait_until(self.app, lambda: not self.window.compact)
        self.assertEqual(self.window.side_panel.width(), 272)


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["test"])

    def test_sort_by_any_column_both_ways(self) -> None:
        model = models.SongsModel()
        model.set_tracks([track(0, "b", length=300), track(1, "a", length=100),
                          track(2, "c", length=200)])
        model.sort(models.COLUMN_INDEX["length"], Qt.SortOrder.AscendingOrder)
        self.assertEqual([t.length_ms for t in model.tracks], [100, 200, 300])
        model.sort(models.COLUMN_INDEX["title"], Qt.SortOrder.DescendingOrder)
        self.assertEqual([t.title for t in model.tracks], ["c", "b", "a"])
        self.assertEqual(model.data(model.index(0, models.COLUMN_INDEX["length"])), "0:00")

    def test_chinese_titles_sort_by_pinyin(self) -> None:
        model = models.SongsModel()
        names = ["夏日", "北岸", "苏晓", "陈默"]
        model.set_tracks([track(i, "x", artist=name) for i, name in enumerate(names)])
        model.sort(models.COLUMN_INDEX["artist"], Qt.SortOrder.AscendingOrder)
        self.assertEqual([t.artist for t in model.tracks], ["北岸", "陈默", "苏晓", "夏日"])

    def test_search_matches_every_term_across_fields(self) -> None:
        model = models.SongsModel()
        model.set_tracks([track(0, "Night Drive", artist="Lumen"),
                          track(1, "Morning", artist="Lumen", album="Night Grid"),
                          track(2, "Night", artist="Other")])
        proxy = models.SongsProxy()
        proxy.setSourceModel(model)
        proxy.set_search("lumen night")
        self.assertEqual(sorted(t.index for t in proxy.tracks()), [0, 1])
        proxy.set_search("")
        proxy.set_constraints({"artist": "Other"})
        self.assertEqual([t.index for t in proxy.tracks()], [2])

    def test_status_line_and_albums(self) -> None:
        tracks = [track(0, "a", length=60_000), track(1, "b", length=120_000, album="Other")]
        self.assertEqual(models.summary_line(tracks), "2 首歌曲，3 分钟，约 7.2 MB")
        albums = models.group_albums(tracks)
        self.assertEqual(sorted(a.title for a in albums), ["Album", "Other"])

    def test_ten_thousand_rows_stay_fast(self) -> None:
        tracks = fakes.fake_tracks(10_000)
        model = models.SongsModel()
        started = time.monotonic()
        model.set_tracks(tracks)
        model.sort(models.COLUMN_INDEX["title"], Qt.SortOrder.AscendingOrder)
        proxy = models.SongsProxy()
        proxy.setSourceModel(model)
        proxy.set_search("signal")
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertGreater(proxy.rowCount(), 0)


class LibraryTests(GuiCase):
    def test_library_loads_into_every_view(self) -> None:
        self.load_library()
        count = len(self.services.library.tracks_value)
        self.assertEqual(self.window.songs_model.rowCount(), count)
        self.assertIn(f"{count} 首歌曲", self.window.pages["device.songs"].status.text())
        self.assertGreater(self.window.pages["device.albums"].model.rowCount(), 10)
        artists = self.window.pages["device.artists"]
        self.assertGreater(artists.values.rowCount(), 5)
        artists.value_view.setCurrentIndex(artists.values.index(1))
        value = artists.values.values[0][0]
        self.assertTrue(all(models.field_value(t, "artist") == value
                            for t in artists.table.proxy.tracks()))
        summary = self.window.library_state.summary
        self.assertEqual(self.window.pages["device"].values["tracks"].text(), f"{count} 首")
        self.assertTrue(summary.database_ready)

    def test_album_art_is_fetched_lazily_and_cached_on_disk(self) -> None:
        self.load_library()
        self.window.navigate("device.albums")
        lead = self.window.pages["device.albums"].model.albums[0].lead
        wait_until(self.app, lambda: self.window.art.get(lead, "coverflow") is not None)
        sessions = self.services.library.sessions
        self.window.art._memory.clear()
        self.window.art._none.clear()
        self.assertIsNotNone(self.window.art.get(lead, "coverflow"))
        self.assertEqual(self.services.library.sessions, sessions)

    def test_cover_colour_follows_the_selected_song(self) -> None:
        self.services.library.all_art = True
        self.load_library()
        settings = self.window.pages["settings"]
        settings.color_source.set_current("cover", emit=True)
        self.assertEqual(json.loads(podlink_ui_theme.ui_prefs_path().read_text())["color_source"],
                         "cover")
        fallback = self.window.base_seed()
        self.assertEqual(fallback, "#3584E4")
        wait_until(self.app, lambda: self.theme.seed == fallback)
        self.window.navigate("device.songs")
        table = self.window.pages["device.songs"].table
        table.setCurrentIndex(table.proxy.index(3, 0))
        wait_until(self.app, lambda: self.theme.seed != fallback)
        self.assertNotEqual(self.theme.tokens.primary, podlink_ui_theme.tokens_for(
            self.theme.resolved, fallback).primary)
        self.window.show_track_art(None)
        wait_until(self.app, lambda: self.theme.seed == fallback)

    def test_swatch_is_an_explicit_choice_and_ignores_covers(self) -> None:
        self.services.library.all_art = True
        self.load_library()
        settings = self.window.pages["settings"]
        self.assertEqual(settings.color_source.current(), "system")
        self.assertFalse(any(s.isChecked() for s in settings.swatches.values()))
        settings.swatches["#1E6FD9"].click()
        self.assertEqual(settings.color_source.current(), "swatch")
        prefs = json.loads(podlink_ui_theme.ui_prefs_path().read_text())
        self.assertEqual((prefs["color_source"], prefs["seed"]), ("swatch", "#1E6FD9"))
        wait_until(self.app, lambda: self.theme.seed == "#1E6FD9")
        self.window.navigate("device.songs")
        table = self.window.pages["device.songs"].table
        table.setCurrentIndex(table.proxy.index(3, 0))
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(self.theme.seed, "#1E6FD9")
        self.appearance.set(accent="#9141AC")
        self.app.processEvents()
        time.sleep(0.3)
        self.app.processEvents()
        self.assertEqual(self.theme.seed, "#1E6FD9")
        settings.color_source.set_current("system", emit=True)
        wait_until(self.app, lambda: self.theme.seed == "#9141AC")
        self.assertFalse(any(s.isChecked() for s in settings.swatches.values()))

    def test_get_info_shows_tags_and_device_detail(self) -> None:
        self.load_library()
        target = self.services.library.tracks_value[0]
        self.window.show_info([target, self.services.library.tracks_value[1]])
        dialog = self.window.info_dialog
        self.assertEqual(dialog.fields["title"].text(), target.title)
        self.assertEqual(dialog.fields["path"].text(), target.path)
        wait_until(self.app, lambda: dialog.fields["size"].text() not in ("", "正在从 iPod 读取…"))
        self.assertIn("MB", dialog.fields["size"].text())
        self.assertEqual(dialog.tabs.count(), 5)
        dialog.copy_value("名称", dialog.fields["title"].text())
        self.assertEqual(QtWidgets.QApplication.clipboard().text(), target.title)
        self.assertIn(f"名称：{target.title}", dialog.all_text())
        dialog.step(1)
        self.assertEqual(dialog.fields["title"].text(), self.services.library.tracks_value[1].title)
        dialog.close()

    def test_delete_removes_only_managed_tracks(self) -> None:
        self.load_library()
        managed = next(t for t in self.services.library.tracks_value if t.managed)
        other = next(t for t in self.services.library.tracks_value if not t.managed)
        self.window.confirm_delete([other])
        self.assertEqual(self.window.toast.text,
                         "只能删除用 PodLink 导入或 Jellyfin 同步到 iPod 的歌曲。")
        before = self.window.songs_model.rowCount()
        self.window.delete_tracks([managed])
        wait_until(self.app, lambda: self.window.songs_model.rowCount() == before - 1)
        self.assertNotIn(managed.index, [t.index for t in self.window.songs_model.tracks])


class ImportTests(GuiCase):
    def make_files(self, names) -> Path:
        folder = self.root / "music" / "nested"
        folder.mkdir(parents=True)
        for name in names:
            (folder / name).write_bytes(b"x")
        (folder / "cover.jpg").write_bytes(b"x")
        return folder.parent

    def test_folder_review_marks_duplicates_and_conversion(self) -> None:
        self.load_library()
        existing = self.services.library.tracks_value[0]

        def probe(path):
            path = Path(path)
            if path.stem == "dup":
                return ProbedFile(path, title=existing.title, artist=existing.artist,
                                  album=existing.album, length_ms=existing.length_ms,
                                  codec="flac", sample_rate=44100, size_bytes=10)
            return ProbedFile(path, title=path.stem, artist="New", album="Fresh",
                              length_ms=100_000, codec="flac", sample_rate=96000, size_bytes=10)

        self.services.prober.probe = probe
        page = self.window.pages["import"]
        page.add_paths([str(self.make_files(["dup.flac", "hires.flac", "c.mp3"]))])
        self.assertEqual(len(page.model.items), 3)
        wait_until(self.app, lambda: all(i.state != "probing" for i in page.model.items))
        states = {i.path.stem: i.state for i in page.model.items}
        self.assertEqual(states, {"dup": "duplicate", "hires": "ready", "c": "ready"})
        self.assertFalse(page.model.item(page.model.items[0].path).include
                         if page.model.items[0].path.stem == "dup" else False)
        self.assertIn("48 kHz", page.summary.text())
        self.assertEqual(len(page.selected()), 2)
        page.skip_check.setChecked(False)
        self.assertEqual(len(page.selected()), 3)

    def test_import_runs_per_file_and_can_cancel_between_files(self) -> None:
        self.load_library()
        page = self.window.pages["import"]
        page.add_paths([str(self.make_files(["a.flac", "b.flac", "c.flac"]))])
        wait_until(self.app, lambda: all(i.state == "ready" for i in page.model.items))
        self.services.library.delay = True
        page.start()
        self.assertTrue(self.window.busy)
        wait_until(self.app, lambda: any(i.state == "uploading" for i in page.model.items))
        page.cancel()
        wait_until(self.app, lambda: page.job is None, 10)
        states = [i.state for i in page.model.items]
        self.assertIn("cancelled", states)
        self.assertIn("done", states)
        self.assertIn("已取消", page.summary.text())

    def test_import_completes_with_summary(self) -> None:
        self.load_library()
        page = self.window.pages["import"]
        page.add_paths([str(self.make_files(["a.flac", "b.m4a"]))])
        wait_until(self.app, lambda: all(i.state == "ready" for i in page.model.items))
        page.start()
        wait_until(self.app, lambda: page.job is None, 10)
        self.assertEqual([i.state for i in page.model.items], ["done", "done"])
        self.assertIn("已导入 2 首", page.summary.text())

    def test_unsupported_and_dropped_files(self) -> None:
        folder = self.root / "drop"
        folder.mkdir()
        (folder / "a.wav").write_bytes(b"x")
        self.window.import_paths([str(folder / "a.wav")])
        self.assertIs(self.window.stack.currentWidget(), self.window.pages["import"])
        page = self.window.pages["import"]
        wait_until(self.app, lambda: page.model.items and page.model.items[0].state == "error")
        self.assertEqual(page.selected(), [])


class SyncTests(GuiCase):
    def page(self):
        page = self.window.pages["sync"]
        page.url.setText("https://jellyfin.example.com")
        return page

    def test_password_login_is_default_masked_and_cleared(self) -> None:
        page = self.page()
        self.assertEqual(page.auth_mode, "password")
        self.assertEqual(page.password.echoMode(), QtWidgets.QLineEdit.EchoMode.Password)
        page.username.setText("alice")
        page.password.setText("secret")
        page.connect_source()
        self.assertEqual(page.password.text(), "")
        wait_until(self.app, lambda: page.source is not None)
        self.assertEqual(self.services.sync.calls[0], ("password", "https://jellyfin.example.com",
                                                       "alice", "secret"))
        self.assertEqual(page.scope.count(), 4)
        saved = podlink_settings.settings_path()
        self.assertFalse(saved.exists() and "secret" in saved.read_text())

    def test_sign_in_does_not_wait_for_the_ipod(self) -> None:
        page = self.page()
        gate = threading.Event()
        self.window.runner.run("iPod busy", lambda _job: gate.wait(5), device=True)
        page.username.setText("alice")
        page.password.setText("secret")
        page.connect_source()
        try:
            wait_until(self.app, lambda: page.source is not None)
        finally:
            gate.set()

    def test_preview_confirm_and_apply(self) -> None:
        page = self.page()
        page.auth_switch.set_current("token", emit=True)
        page.token.setText("tok")
        page.connect_source()
        wait_until(self.app, lambda: page.source is not None)
        page.preview()
        wait_until(self.app, lambda: page.plan is not None)
        rows = page.preview_rows()
        self.assertEqual([kind for kind, _ in rows].count("add"), 5)
        self.assertEqual([kind for kind, _ in rows].count("remove"), 2)
        self.assertEqual(page.tile_remove.value.text(), "2")
        page.filter.set_current("remove", emit=True)
        self.assertEqual(len(page.preview_rows()), 2)
        with patch.object(page, "confirm_removals", return_value=False):
            page.confirm()
        self.assertFalse(any(c[0] == "apply" for c in self.services.sync.calls))
        with patch.object(page, "confirm_removals", return_value=True):
            page.confirm()
        wait_until(self.app, lambda: page.plan is None and not page.busy)
        self.assertTrue(any(c[0] == "apply" for c in self.services.sync.calls))
        self.assertIn("写入 5 首，删除 2 首", page.status.text())

    def test_credential_change_invalidates_and_failure_recovers(self) -> None:
        page = self.page()
        page.auth_switch.set_current("token", emit=True)
        page.username.setText("alice")
        self.assertIsNone(page.source)
        page.connect_source()
        wait_until(self.app, lambda: page.source is not None)
        page.sign_out()
        self.assertIsNone(page.source)
        page.url.setText("https://other.example.com")
        self.assertFalse(page.preview_button.isEnabled())
        self.services.sync.fail = RuntimeError("boom")
        page.connect_source()
        wait_until(self.app, lambda: self.errors)
        self.assertFalse(page.busy)
        self.assertEqual(page.status.text(), "操作失败。")
        self.assertIsNone(self.services.sync.stored_session)

    def test_sign_in_is_kept_without_the_password(self) -> None:
        page = self.page()
        page.username.setText("alice")
        page.password.setText("secret")
        page.connect_source()
        wait_until(self.app, lambda: page.source is not None)
        stored = self.services.sync.stored_session
        self.assertEqual(stored["user_name"], "alice")
        self.assertEqual(stored["url"], "https://jellyfin.example.com")
        self.assertNotIn("secret", json.dumps(stored))
        self.assertFalse(page.account.isHidden())
        self.assertIn("alice", page.account_label.text())
        self.assertTrue(page.auth_stack.isHidden())
        page.scope.setCurrentIndex(2)
        self.assertEqual(self.services.sync.stored_session["source"],
                         {"kind": "playlist", "id": "pl1"})
        page.sign_out_button.click()
        self.assertIsNone(self.services.sync.stored_session)
        self.assertIsNone(page.source)
        self.assertTrue(page.account.isHidden())
        self.assertFalse(page.auth_stack.isHidden())
        self.assertEqual(page.connection_badge.text(), "未连接")

    def restored_page(self):
        from astrapod_desktop.pages.sync import SyncPage
        self.services.sync.stored_session = {
            "url": "https://jf.example.com", "user_id": "u1", "user_name": "bob",
            "token": "tok", "device_id": "d1", "source": {"kind": "playlist", "id": "pl2"}}
        page = SyncPage(self.window)
        self.addCleanup(page.deleteLater)
        return page

    def test_saved_session_restores_as_connected(self) -> None:
        page = self.restored_page()
        self.assertEqual(page.url.text(), "https://jf.example.com")
        self.assertFalse(page.url.isEnabled())
        self.assertIn("bob", page.account_label.text())
        wait_until(self.app, lambda: page.source is not None and not page.restoring)
        self.assertIn(("restore", "https://jf.example.com", "tok"), self.services.sync.calls)
        self.assertEqual(page.connection_badge.text(), "已连接")
        self.assertEqual(page.scope.currentIndex(), 3)
        self.assertTrue(page.preview_button.isEnabled())

    def test_rejected_token_signs_out(self) -> None:
        self.services.sync.reject_token = True
        page = self.restored_page()
        wait_until(self.app, lambda: not page.restoring)
        self.assertIsNone(page.session)
        self.assertIsNone(self.services.sync.stored_session)
        self.assertEqual(page.connection_badge.text(), "未连接")
        self.assertIn("登录已失效", page.status.text())
        self.assertFalse(page.auth_stack.isHidden())

    def test_unreachable_server_keeps_the_session(self) -> None:
        self.services.sync.fail = OSError("server unavailable")
        page = self.restored_page()
        wait_until(self.app, lambda: not page.restoring)
        self.assertIsNotNone(self.services.sync.stored_session)
        self.assertEqual(page.connection_badge.text(), "已登录，未连接")
        self.assertEqual(page.connect_button.text(), "重新连接")
        self.services.sync.fail = None
        page.connect_button.click()
        wait_until(self.app, lambda: page.source is not None and not page.restoring)

    def test_confirm_dialog_defaults_to_cancel(self) -> None:
        plan = fakes.FakePlan(fakes.FakeScope("all", "x"))
        box, cancel, apply = self.window.pages["sync"].build_confirm_dialog(plan)
        self.assertIs(box.defaultButton(), cancel)
        self.assertEqual(apply.property("variant"), "danger")


class JellyfinLibraryTests(GuiCase):
    def sign_in(self):
        page = self.window.pages["sync"]
        page.url.setText("https://jellyfin.example.com")
        page.auth_switch.set_current("token", emit=True)
        page.token.setText("tok")
        page.connect_source()
        wait_until(self.app, lambda: page.source is not None and not page.busy)
        return page

    def library_calls(self) -> int:
        return sum(1 for call in self.services.sync.calls if call[0] == "library")

    def test_signed_out_pages_point_to_the_sync_page(self) -> None:
        for key in ("songs", "albums", "artists", "genres", "composers"):
            self.window.navigate(key)
            page = self.window.pages[key]
            self.assertEqual(page.empty.title.text(), "没有登录 Jellyfin")
            self.assertFalse(page.empty.action.isHidden())
        self.assertEqual(self.window.pages["songs"].header.subtitle.text(), "没有登录 Jellyfin")
        self.assertEqual(self.library_calls(), 0)
        self.window.pages["songs"].empty.action.click()
        self.assertIs(self.window.stack.currentWidget(), self.window.pages["sync"])

    def test_library_pages_browse_jellyfin_not_the_ipod(self) -> None:
        self.sign_in()
        self.window.navigate("songs")
        page = self.window.pages["songs"]
        wait_until(self.app, lambda: self.window.jellyfin.loaded)
        expected = self.services.sync.library_value
        self.assertEqual(page.table.proxy.rowCount(), len(expected))
        self.assertEqual(page.header.subtitle.text(), "来自 Jellyfin，只能浏览")
        self.assertIn(f"{len(expected)} 首歌曲", page.status.text())
        self.assertEqual(self.services.library.sessions, 0)
        self.assertEqual(self.window.pages["device.songs"].table.proxy.rowCount(), 0)
        artists = self.window.pages["artists"]
        self.assertIn("Aurora Field", [label for _v, label, _c in artists.values.values])
        self.window.navigate("albums")
        self.assertGreater(self.window.pages["albums"].model.rowCount(), 5)
        self.window.search.setText("Aurora")
        self.assertEqual([a.artist for a in self.window.pages["albums"].model.albums],
                         ["Aurora Field"])
        self.window.navigate("songs")
        self.assertEqual(self.window.search.text(), "")
        self.assertEqual(self.library_calls(), 1)

    def test_jellyfin_rows_are_read_only(self) -> None:
        self.sign_in()
        self.window.navigate("songs")
        wait_until(self.app, lambda: self.window.jellyfin.loaded)
        page = self.window.pages["songs"]
        self.assertFalse(hasattr(page, "info_button"))
        table = page.table
        requested = []
        table.info_requested.connect(requested.append)
        table.delete_requested.connect(requested.append)
        table.selectRow(0)
        for key in (Qt.Key.Key_Delete, Qt.Key.Key_Return):
            table.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key,
                                                Qt.KeyboardModifier.NoModifier))
        table.doubleClicked.emit(table.proxy.index(0, 0))
        self.assertEqual(requested, [])
        self.assertFalse(self.window.pages["albums"].detail.info_button.isVisibleTo(
            self.window.pages["albums"].detail))

    def test_covers_come_from_jellyfin(self) -> None:
        self.sign_in()
        self.window.navigate("albums")
        wait_until(self.app, lambda: self.window.jellyfin.loaded)
        lead = self.window.pages["albums"].model.albums[0].lead
        art = self.window.jellyfin.art
        wait_until(self.app, lambda: art.get(lead, "coverflow") is not None)
        self.assertIsInstance(art.get(lead, "coverflow"), QtGui.QImage)
        self.assertEqual(self.services.library.sessions, 0)

    def test_reading_does_not_wait_for_the_ipod(self) -> None:
        self.sign_in()
        gate = threading.Event()
        self.window.runner.run("iPod busy", lambda _job: gate.wait(5), device=True)
        try:
            self.window.navigate("genres")
            wait_until(self.app, lambda: self.window.jellyfin.loaded)
        finally:
            gate.set()

    def test_sign_out_and_failure(self) -> None:
        sync = self.sign_in()
        self.window.navigate("composers")
        wait_until(self.app, lambda: self.window.jellyfin.loaded)
        self.assertEqual(self.window.pages["composers"].body.currentIndex(), 0)
        sync.sign_out()
        page = self.window.pages["composers"]
        self.assertEqual(page.body.currentIndex(), 1)
        self.assertEqual(page.empty.title.text(), "没有登录 Jellyfin")
        self.services.sync.fail = OSError("server unavailable")
        sync.auth_switch.set_current("token", emit=True)
        self.services.sync.fail = None
        self.sign_in()
        self.services.sync.fail = OSError("server unavailable")
        self.window.navigate("songs")
        wait_until(self.app, lambda: self.window.jellyfin.error)
        songs = self.window.pages["songs"]
        self.assertEqual(songs.empty.title.text(), "读取 Jellyfin 资料库失败")
        self.assertEqual(songs.empty.action.text(), "重试")
        self.services.sync.fail = None
        songs.empty.action.click()
        wait_until(self.app, lambda: self.window.jellyfin.loaded)
        self.assertIs(songs.stack.currentWidget(), songs.table)

    def test_saved_session_fills_the_library_at_startup(self) -> None:
        self.services.sync.stored_session = {
            "url": "https://jf.example.com", "user_id": "u1", "user_name": "bob",
            "token": "tok", "device_id": "d1"}
        podlink_settings.write_private(podlink_ui_theme.ui_prefs_path(),
                                       json.dumps({"page": "artists"}))
        _app, _theme, window = create_application(["test"], self.services)
        self.addCleanup(window.deleteLater)
        window.set_motion(False)
        self.assertIs(window.stack.currentWidget(), window.pages["artists"])
        self.assertEqual(window.pages["artists"].empty.title.text(), "正在连接 Jellyfin…")
        wait_until(self.app, lambda: window.jellyfin.loaded)
        self.assertEqual(window.pages["artists"].body.currentIndex(), 0)


class JellyfinBackendTests(unittest.TestCase):
    class Client:
        def __init__(self, rows, missing=False):
            self.rows, self.missing, self.calls = rows, missing, []

        def page(self, session, route, offset, limit, **params):
            self.calls.append((route, offset, params))
            return self.rows[offset:offset + 2], len(self.rows)

        def request(self, session, path, image=False, **params):
            self.calls.append((path, image, params))
            if self.missing:
                raise backends_error(404)
            return b"jpeg"

    def source(self, client):
        return type("Source", (), {"client": client, "session": object()})()

    def test_items_become_read_only_tracks(self) -> None:
        rows = [{"Id": "a1", "Type": "Audio", "Name": "Song", "Artists": ["X", "Y"],
                 "AlbumArtist": "X", "Album": "LP", "AlbumId": "al1", "Genres": ["Pop"],
                 "People": [{"Name": "Z", "Type": "Composer"}, {"Name": "P", "Type": "Producer"}],
                 "ProductionYear": 2001, "ParentIndexNumber": 1, "IndexNumber": 3,
                 "RunTimeTicks": 2_000_000_000, "MediaSources": [{"Bitrate": 320_000}],
                 "UserData": {"PlayCount": 4, "LastPlayedDate": "2024-01-02T03:04:05.1234567Z"},
                 "DateCreated": "2023-05-06T07:08:09Z", "Path": "/srv/music/song.flac"},
                {"Id": "a2", "Type": "Audio", "Name": "Two"},
                {"Id": "v1", "Type": "Video", "Name": "Not music"}]
        client = self.Client(rows)
        progress = []
        tracks = backends.SyncBackend().library_tracks(self.source(client),
                                                       lambda d, t: progress.append((d, t)))
        self.assertEqual([t.title for t in tracks], ["Song", "Two"])
        first = tracks[0]
        self.assertEqual((first.artist, first.album_artist, first.composer, first.genre),
                         ("X, Y", "X", "Z", "Pop"))
        self.assertEqual((first.length_ms, first.bitrate_kbps, first.play_count, first.track),
                         (200_000, 320, 4, 3))
        self.assertEqual(first.last_played, 1704164645)
        self.assertFalse(first.managed)
        self.assertEqual(progress, [(2, 3), (3, 3)])
        self.assertEqual(client.calls[0][2]["IncludeItemTypes"], "Audio")
        self.assertEqual(tracks[1].album_artist, "")

    def test_album_art_uses_the_album_and_404_is_no_art(self) -> None:
        from astrapod_desktop.jellyfin import track_from_item
        track = track_from_item({"Id": "a1", "AlbumId": "al1", "Name": "x"}, 0)
        client = self.Client([])
        self.assertEqual(backends.SyncBackend().album_art(self.source(client), track, 128),
                         b"jpeg")
        self.assertEqual(client.calls[0][0], "Items/al1/Images/Primary")
        self.assertIsNone(backends.SyncBackend().album_art(self.source(self.Client([], True)),
                                                           track, 128))


def backends_error(status):
    return fakes.FakeJellyfinError(f"HTTP {status}", status)


class DevicePortTests(GuiCase):
    def test_the_port_is_chosen_on_the_device_page(self) -> None:
        device = self.window.pages["device"]
        sync = self.window.pages["sync"]
        settings = self.window.pages["settings"]
        self.assertFalse([w for w in sync.findChildren(QtWidgets.QLineEdit)
                          if w.accessibleName() == "iPod 串口"])
        self.assertEqual(device.port.text(), fakes.FAKE_PORT)
        self.assertEqual(device.port_picker.badge.text(), "自动识别")
        self.assertEqual(sync.port_label.text(), "iPod 串口：自动识别")
        device.port.setText("/dev/ttyACM5")
        device.port.editingFinished.emit()
        self.assertEqual(self.window.prefs["port"], "/dev/ttyACM5")
        self.assertEqual(self.services.library.port, "/dev/ttyACM5")
        self.assertEqual(json.loads(podlink_ui_theme.ui_prefs_path().read_text())["port"],
                         "/dev/ttyACM5")
        self.assertEqual(settings.port.text(), "/dev/ttyACM5")
        self.assertEqual(settings.port_badge.text(), "手动指定")
        self.assertEqual(sync.port_label.text(), "iPod 串口：/dev/ttyACM5")
        sync.url.setText("https://jellyfin.example.com")
        sync.auth_switch.set_current("token", emit=True)
        sync.token.setText("tok")
        sync.connect_source()
        wait_until(self.app, lambda: sync.source is not None and not sync.busy)
        sync.preview()
        wait_until(self.app, lambda: sync.plan is not None)
        self.assertEqual(self.services.sync.calls[-1][2], "/dev/ttyACM5")
        device.port_picker.auto_button.click()
        self.assertEqual(self.window.prefs["port"], "")
        self.assertIsNone(sync.plan)
        self.assertEqual(device.port.text(), fakes.FAKE_PORT)
        self.assertEqual(settings.port_badge.text(), "自动识别")
        sync.port_button.click()
        self.assertIs(self.window.stack.currentWidget(), device)


class SettingsTests(GuiCase):
    def test_validation_and_save(self) -> None:
        page = self.window.pages["settings"]
        page.entries["JELLYFIN_URL"].setText("http://insecure.example.com")
        page.save()
        self.assertTrue(self.errors)
        self.errors.clear()
        page.entries["JELLYFIN_URL"].setText("https://jf.example.com")
        page.entries["CIDER_BACKEND"].setCurrentText("rpc")
        page.port.setText("/dev/ttyACM9")
        page.player_tokens[0].setText("cider-token")
        page.save()
        self.assertEqual(self.errors, [])
        saved = json.loads(podlink_settings.settings_path().read_text())
        self.assertEqual(saved["JELLYFIN_URL"], "https://jf.example.com")
        self.assertEqual(saved["CIDER_BACKEND"], "rpc")
        self.assertEqual(self.window.services.library.port, "/dev/ttyACM9")
        self.assertEqual(page.player_tokens[0].text(), "")
        self.assertEqual(self.window.pages["sync"].url.text(), "https://jf.example.com")
        self.assertIn(("reload", {}), self.services.daemon.requests)

    def test_port_is_detected_and_an_override_is_marked(self) -> None:
        page = self.window.pages["settings"]
        detected = fakes.FAKE_PORT
        self.assertEqual(page.port.text(), detected)
        self.assertEqual(page.port_badge.text(), "自动识别")
        self.assertTrue(page.port_auto_button.isHidden())
        page.save()
        self.assertEqual(self.window.prefs["port"], "")
        page.port.setText("/dev/ttyACM3")
        self.assertEqual(page.port_badge.text(), "手动指定")
        self.assertFalse(page.port_auto_button.isHidden())
        self.assertIn(detected, page.port_help.text())
        page.save()
        self.assertEqual(self.window.services.library.port, "/dev/ttyACM3")
        self.services.library.detected_port = None
        page.port_detect_button.click()
        self.assertEqual(page.port.text(), "/dev/ttyACM3")
        page.port_auto_button.click()
        self.assertEqual(page.port.text(), "")
        self.assertEqual(page.port_badge.text(), "未检测到")
        self.assertIn("没有检测到 iPod", page.port_help.text())
        page.save()
        self.assertEqual(self.window.services.library.port, "")
        self.services.library.detected_port = detected
        state = fakes.fake_state()
        state["link"]["port"] = "/dev/ttyACM1"
        self.services.daemon.push(state)
        self.assertEqual(page.port.text(), detected)
        self.assertEqual(page.port_badge.text(), "自动识别")

    def test_saved_override_survives_a_new_window(self) -> None:
        from astrapod_desktop.pages.settings import SettingsPage
        self.window.save_pref("port", "/dev/ttyACM7")
        page = SettingsPage(self.window)
        self.addCleanup(page.deleteLater)
        self.assertEqual(page.port.text(), "/dev/ttyACM7")
        self.assertEqual(page.port_badge.text(), "手动指定")


def fields_with(extra: dict, without=()) -> dict:
    fields = {k: v for k, v in podlink_settings.FIELDS.items()
              if k != "AGENT_CONTROL" and k not in without}
    fields.update(extra)
    return fields


class AgentControlTests(GuiCase):
    def page(self):
        from astrapod_desktop.pages.settings import SettingsPage
        patcher = patch.dict(podlink_settings.FIELDS,
                             fields_with({"AGENT_CONTROL": ("Agent 控制（herdr 或 t3）", "herdr")}),
                             clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        page = SettingsPage(self.window)
        self.addCleanup(page.deleteLater)
        return page

    def test_one_source_is_chosen_and_saved_with_reload(self) -> None:
        page = self.page()
        self.assertEqual(page.agent_control.current(), "herdr")
        self.assertIsNone(page.t3_switch)
        self.assertIn("herdr", page.agent_control_help.text())
        self.assertTrue(page.t3_card.isHidden())
        self.assertTrue(page.agent_form.isRowVisible(page.entries["HERDR_SOCKET_PATH"]))
        page.agent_control.set_current("t3", emit=True)
        self.assertIn("T3 Code 的项目和线程", page.agent_control_help.text())
        self.assertFalse(page.t3_card.isHidden())
        self.assertFalse(page.agent_form.isRowVisible(page.entries["HERDR_SOCKET_PATH"]))
        self.assertFalse(page.agent_form.isRowVisible(page.entries["HERDREMOTE_VOICE_FOCUS"]))
        saved = json.loads(podlink_settings.settings_path().read_text())
        self.assertEqual(saved["AGENT_CONTROL"], "t3")
        self.assertEqual(self.services.daemon.requests[-1], ("reload", {}))
        page.save()
        self.assertEqual(self.errors, [])
        saved = json.loads(podlink_settings.settings_path().read_text())
        self.assertEqual(saved["AGENT_CONTROL"], "t3")
        self.assertNotIn("T3_CONTROL", saved)
        self.assertEqual(self.services.daemon.requests[-1], ("reload", {}))
        wait_until(self.app, lambda: self.window.pages["agents"].control == "t3")

    def test_agents_page_shows_only_the_controlled_source(self) -> None:
        page = self.window.pages["agents"]
        wait_until(self.app, lambda: page.question_cards)
        self.assertEqual(page.control, "herdr")
        self.assertEqual(page.body.currentIndex(), 0)
        self.assertEqual(page.source_badge.text(), "控制：herdr")
        state = fakes.fake_state()
        del state["agents"]
        state["questions"].append({"id": "t3q1", "title": "T3 需要确认", "text": "继续？",
                                   "options": [{"number": 1, "label": "好"}], "dismissed": False})
        self.services.daemon.push(state)
        self.assertEqual(page.control, "herdr")
        self.assertEqual(sorted(page.question_cards), ["q1"])
        state["agents"] = {"control": "t3"}
        self.services.daemon.push(state)
        self.assertEqual(page.body.currentIndex(), 2)
        self.assertEqual(page.source_badge.text(), "控制：T3 Code")
        self.assertEqual(page.herdr_badge.text(), "T3 未连接")
        self.assertEqual(page.t3_list.count(), 2)
        self.assertIn("等待确认", page.t3_list.item(1).text())
        self.assertEqual(sorted(page.question_cards), ["t3q1"])
        page.t3_list.setCurrentRow(0)
        self.assertTrue(page.t3_open_button.isEnabled())
        state["t3"] = {"enabled": True, "connected": False, "error": None}
        self.services.daemon.push(state)
        self.assertIs(page.t3_stack.currentWidget(), page.t3_empty)
        self.assertIn("连接 T3", page.t3_empty.text.text())
        self.assertFalse(page.t3_open_button.isEnabled())


class T3SettingsTests(GuiCase):
    def test_connect_and_disconnect_on_click_only(self) -> None:
        page = self.window.pages["settings"]
        self.assertFalse([r for r in self.services.daemon.requests if r[0].startswith("t3_")])
        self.assertEqual(page.t3_badge.text(), "未连接")
        self.assertTrue(page.t3_connect_button.isEnabled())
        self.assertFalse(page.t3_disconnect_button.isEnabled())
        page.t3_connect_button.click()
        self.assertFalse(page.t3_connect_button.isEnabled())
        self.assertFalse(page.t3_disconnect_button.isEnabled())
        self.assertEqual(self.services.daemon.requests[-1], ("t3_connect", {}))
        wait_until(self.app, lambda: page.t3_badge.text() == "已连接" and not page.t3_busy)
        self.assertIn("T3 0.4.2", page.t3_status.text())
        self.assertIn("AstraPod 桌面", page.t3_status.text())
        self.assertTrue(page.t3_disconnect_button.isEnabled())
        self.assertFalse(page.t3_connect_button.isEnabled())
        page.t3_disconnect_button.click()
        self.assertEqual(self.services.daemon.requests[-1], ("t3_disconnect", {}))
        wait_until(self.app, lambda: page.t3_badge.text() == "未连接" and not page.t3_busy)
        self.assertTrue(page.t3_connect_button.isEnabled())

    def test_failure_is_shown_inline(self) -> None:
        page = self.window.pages["settings"]
        with patch.object(self.services.daemon, "request",
                          side_effect=lambda op, cb=None, **a: cb(False, "T3 没有运行")):
            page.t3_connect()
        self.assertFalse(page.t3_error.isHidden())
        self.assertIn("T3 没有运行", page.t3_error.text())
        self.assertFalse(page.t3_busy)
        self.assertTrue(page.t3_connect_button.isEnabled())

    def test_state_wording(self) -> None:
        page = self.window.pages["settings"]
        state = fakes.fake_state()
        state["t3"] = {}
        self.services.daemon.push(state)
        self.assertEqual(page.t3_badge.text(), "未连接")
        state["t3"] = {"enabled": True, "connected": False, "error": "令牌已失效"}
        self.services.daemon.push(state)
        self.assertEqual(page.t3_badge.text(), "连接出错")
        self.assertEqual(page.t3_status.text(), "令牌已失效")

    def test_control_switch_saves_when_the_setting_exists(self) -> None:
        from astrapod_desktop.pages.settings import SettingsPage
        fields = fields_with({"T3_CONTROL": ("在 iPod 上控制 T3（on 或 off）", "on")})
        with patch.dict(podlink_settings.FIELDS, fields, clear=True):
            page = SettingsPage(self.window)
            self.assertTrue(page.t3_switch.isChecked())
            page.t3_switch.setChecked(False)
            self.assertFalse(page.t3_connect_button.isEnabled())
            page.save()
            self.assertEqual(self.errors, [])
            saved = json.loads(podlink_settings.settings_path().read_text())
            self.assertEqual(saved["T3_CONTROL"], "off")
        page.deleteLater()


    def test_follow_switch_saves_and_follows_the_control_switch(self) -> None:
        from astrapod_desktop.pages.settings import SettingsPage
        fields = {k: v for k, v in podlink_settings.FIELDS.items() if k != "T3_FOLLOW_IPOD"}
        with patch.dict(podlink_settings.FIELDS, fields, clear=True):
            hidden = SettingsPage(self.window)
            self.assertIsNone(hidden.t3_follow_switch)
        hidden.deleteLater()
        fields = fields_with({"T3_CONTROL": ("T3 控制", "on"),
                              "T3_FOLLOW_IPOD": ("T3 跟随 iPod", "on")})
        with patch.dict(podlink_settings.FIELDS, fields, clear=True):
            page = SettingsPage(self.window)
            self.assertTrue(page.t3_follow_switch.isChecked())
            self.assertTrue(page.t3_follow_switch.isEnabled())
            page.t3_switch.setChecked(False)
            self.assertFalse(page.t3_follow_switch.isEnabled())
            self.assertTrue(page.t3_follow_switch.isChecked())
            page.t3_switch.setChecked(True)
            page.t3_follow_switch.setChecked(False)
            page.save()
            self.assertEqual(self.errors, [])
            saved = json.loads(podlink_settings.settings_path().read_text())
            self.assertEqual(saved["T3_FOLLOW_IPOD"], "off")
            self.assertEqual(saved["T3_CONTROL"], "on")
        page.deleteLater()


    def test_command_path_sits_in_the_t3_card(self) -> None:
        from astrapod_desktop.pages.settings import SettingsPage
        with patch.dict(podlink_settings.FIELDS, {"T3_BIN": ("T3 命令路径", "")}):
            page = SettingsPage(self.window)
            field = page.entries["T3_BIN"]
            self.assertEqual(field.placeholderText(), "留空自动查找 t3")
            self.assertTrue(page.t3_badge.parentWidget().isAncestorOf(field))
            field.setText("/opt/t3/bin/t3")
            page.save()
            self.assertEqual(self.errors, [])
            saved = json.loads(podlink_settings.settings_path().read_text())
            self.assertEqual(saved["T3_BIN"], "/opt/t3/bin/t3")
        page.deleteLater()


class QuickReplyTests(GuiCase):
    def page(self):
        from astrapod_desktop.pages.settings import SettingsPage
        fields = {"AGENT_QUICK_REPLIES": ("Agent 快捷回复", "")}
        self._fields = patch.dict(podlink_settings.FIELDS, fields)
        self._fields.start()
        self.addCleanup(self._fields.stop)
        return SettingsPage(self.window)

    def test_hidden_until_the_setting_exists(self) -> None:
        from astrapod_desktop.pages.settings import SettingsPage
        fields = {k: v for k, v in podlink_settings.FIELDS.items() if k != "AGENT_QUICK_REPLIES"}
        with patch.dict(podlink_settings.FIELDS, fields, clear=True):
            page = SettingsPage(self.window)
            self.assertFalse(hasattr(page, "quick_replies"))
        page.deleteLater()

    def test_defaults_validation_and_save(self) -> None:
        page = self.page()
        self.assertEqual(page.quick_replies.toPlainText().splitlines(),
                         ["Continue", "Yes", "No", "/compact"])
        page.quick_replies.setPlainText("\n".join(f"r{i}" for i in range(9)))
        self.assertFalse(page.quick_reply_error.isHidden())
        self.assertIn("最多 8 条", page.quick_reply_error.text())
        self.assertFalse(page.save_button.isEnabled())
        page.quick_replies.setPlainText("继续" * 11)
        self.assertIn("超过 60 字节", page.quick_reply_error.text())
        page.save()
        self.assertTrue(self.errors)
        self.errors.clear()
        page.quick_replies.setPlainText("  Continue \n\n好的，继续\n/compact\n")
        self.assertTrue(page.quick_reply_error.isHidden())
        self.assertTrue(page.save_button.isEnabled())
        page.save()
        self.assertEqual(self.errors, [])
        saved = json.loads(podlink_settings.settings_path().read_text())
        self.assertEqual(saved["AGENT_QUICK_REPLIES"], "Continue\n好的，继续\n/compact")


class RemoteTests(GuiCase):
    def test_agents_page_follows_state_events_and_answers(self) -> None:
        page = self.window.pages["agents"]
        wait_until(self.app, lambda: page.question_cards)
        self.assertEqual(page.ws_model.rowCount(), 2)
        self.assertEqual(page.tab_bar.count(), 2)
        self.assertEqual(page.agent_model.rowCount(), 2)
        card = next(iter(page.question_cards.values()))
        card.option_buttons[1].click()
        wait_until(self.app, lambda: not page.question_cards)
        self.assertIn(("answer", {"question": "q1", "number": 2}), self.services.daemon.requests)
        state = fakes.fake_state()
        state["questions"][0]["id"] = "q9"
        state["herdr"]["workspaces"] = state["herdr"]["workspaces"][:1]
        self.services.daemon.push(state)
        self.app.processEvents()
        self.assertIn("q9", page.question_cards)
        self.assertEqual(page.ws_model.rowCount(), 1)
        self.assertEqual(self.window.sidebar.model_.item(self.window.sidebar.rows["agents"])
                         .data(Qt.ItemDataRole.UserRole + 13), 1)
        page.focus_pane()
        self.assertEqual(self.services.daemon.requests[-1][0], "focus_pane")
        state["herdr"] = {"ok": False, "workspaces": []}
        self.services.daemon.push(state)
        self.assertEqual(page.body.currentIndex(), 1)

    def test_usage_without_playback_or_voice(self) -> None:
        usage = self.window.pages["usage"]
        self.assertEqual(sum(1 for c in usage.cards if not c.isHidden()), 3)
        self.assertEqual(usage.cards[0].five.value, 62)
        self.assertFalse(hasattr(usage, "refresh_button"))
        self.assertFalse(any(b.text() == "刷新" for b in usage.findChildren(QtWidgets.QPushButton)))
        self.assertFalse(hasattr(self.window, "voice"))
        for key in self.window.pages:
            self.window.navigate(key)
        self.assertFalse([r for r in self.services.daemon.requests
                          if r[0] in ("player", "lyrics", "voice")])

    def test_usage_says_when_t3_is_unavailable(self) -> None:
        usage = self.window.pages["usage"]
        state = fakes.fake_state()
        state["usage"][0]["status"] = "unavailable"
        state["usage"][0]["refreshing"] = True
        self.services.daemon.push(state)
        card = usage.cards[0]
        self.assertEqual(card.badge.text(), "T3 不可用")
        self.assertFalse(card.note.isHidden())
        self.assertIn("T3 Code", card.note.text())
        self.assertEqual(usage.cards[1].badge.text(), "最新")
        self.assertTrue(usage.cards[1].note.isHidden())

    def test_extensions_page_lists_toggles_and_saves(self) -> None:
        page = self.window.pages["extensions"]
        host = self.services.plugins.host
        self.assertEqual(page.list.count(), 2)
        self.assertIn("target", page.fields)
        page.list.setCurrentRow(1)
        page.toggle.click()
        self.assertTrue(host.enabled["gamepad"])
        page.list.setCurrentRow(0)
        page.fields["repeat_delay_ms"].setValue(500)
        page.save_settings()
        self.assertEqual(host.values["apple-ir"]["repeat_delay_ms"], 500)
        host.emit_input("menu", "down")
        self.app.processEvents()
        self.assertEqual(page.events.count(), 1)
        self.assertIn("菜单", page.events.item(0).text())

    def test_schema_normalisation(self) -> None:
        fields = normalize_schema({"properties": {"a": {"type": "integer", "default": 1},
                                                  "b": {"enum": ["x", "y"]}}})
        self.assertEqual([(f["key"], f["type"]) for f in fields], [("a", "int"), ("b", "enum")])


class RuntimeTests(GuiCase):
    def test_close_hides_to_tray_and_busy_blocks_quit(self) -> None:
        from astrapod_desktop.runtime import DesktopRuntime
        with patch.object(QtCore.QProcess, "start"), \
                patch.object(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            runtime = DesktopRuntime(self.app, self.window)
        self.assertIsNotNone(runtime.tray)
        self.assertTrue(self.services.library.borrow)
        self.assertTrue(self.services.plugins.running)
        self.window.show()
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.window.show()
        job = self.window.runner.run("slow", lambda j: time.sleep(0.3))
        self.assertTrue(self.window.busy)
        with patch.object(self.app, "quit") as quit_app:
            runtime.quit()
            quit_app.assert_not_called()
        self.window.close()
        self.assertTrue(self.window.isVisible())
        wait_until(self.app, lambda: not self.window.busy)
        with patch.object(self.app, "quit") as quit_app:
            runtime.quit()
            quit_app.assert_called_once()
        runtime.stop()
        self.assertFalse(self.services.plugins.running)
        runtime.tray.hide()
        self.app.setQuitOnLastWindowClosed(True)
        del job

    def test_job_runner_serialises_device_jobs(self) -> None:
        runner = JobRunner()
        order = []
        for name in ("a", "b", "c"):
            runner.run(name, lambda j, n=name: (order.append(n + "+"), time.sleep(0.02),
                                               order.append(n + "-")))
        wait_until(self.app, lambda: not runner.busy)
        self.assertEqual(order, ["a+", "a-", "b+", "b-", "c+", "c-"])


class DaemonClientTests(GuiCase):
    def test_client_subscribes_and_receives_state(self) -> None:
        path = self.root / "run" / "desktop.sock"
        server = QtNetwork.QLocalServer()
        self.assertTrue(server.listen(str(path)))
        received = []
        peers = []

        def accept():
            peer = server.nextPendingConnection()
            peers.append(peer)
            peer.readyRead.connect(lambda: received.extend(
                json.loads(line) for line in bytes(peer.readAll()).splitlines() if line))

        server.newConnection.connect(accept)
        client = backends.DaemonClient(path)
        states = []
        client.state_changed.connect(states.append)
        client.start()
        wait_until(self.app, lambda: len(received) >= 2)
        self.assertEqual([m["op"] for m in received[:2]], ["hello", "subscribe"])
        self.assertTrue(received[1]["logs"])
        state = fakes.fake_state()
        peers[0].write(json.dumps({"event": "state", "state": state}).encode() + b"\n")
        wait_until(self.app, lambda: states)
        self.assertEqual(states[-1]["link"]["serial"], state["link"]["serial"])
        answers = []
        client.request("detail", lambda ok, r: answers.append((ok, r)), pane="p1")
        wait_until(self.app, lambda: len(received) >= 3)
        peers[0].write(json.dumps({"id": received[2]["id"], "ok": False,
                                   "error": "no herdr"}).encode() + b"\n")
        wait_until(self.app, lambda: answers)
        self.assertEqual(answers, [(False, "no herdr")])
        client.stop()
        server.close()


if __name__ == "__main__":
    unittest.main()
