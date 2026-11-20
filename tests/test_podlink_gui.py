"""Offscreen Qt unit and workflow tests for AstraPod desktop GUI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TOOLS = Path(__file__).resolve().parents[1]
for _p in (str(TOOLS), str(TOOLS / "herdremote")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hrmediasync
import podlink_gui
import podlink_settings
import podlink_ui_theme
from hrjellyfin import JellyfinError
from podlink_ui_theme import QtCore, QtGui, QtWidgets


def wait_until(app: QtWidgets.QApplication, predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents()
    if not predicate():
        raise AssertionError("timed out waiting for Qt UI condition")


class PodlinkGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["test_podlink_gui"])

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="podlink-gui-test-")
        self.config_root = Path(self._tmp.name)
        self._env_patch = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.config_root),
                "JELLYFIN_URL": "",
                "JELLYFIN_API_KEY": "",
                "CIDER_API_URL": "",
                "CIDER_BACKEND": "",
                "RUFIN_API_URL": "",
                "HERDR_SOCKET_PATH": "",
                "HERDREMOTE_VOICE_FOCUS": "",
                "HERDREMOTE_USAGE_CACHE_DIR": "",
                "HERDREMOTE_KEY_TOOL": "",
            },
            clear=False,
        )
        self._env_patch.start()
        self.theme_manager = podlink_ui_theme.ThemeManager(self.app)
        self.theme_manager.apply(self.theme_manager.mode, persist=False)
        self.window = podlink_gui.SyncWindow(self.theme_manager)
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.busy = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self._env_patch.stop()
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # Theme & Persistence
    # ------------------------------------------------------------------
    def test_theme_persistence_and_live_switching(self) -> None:
        prefs_file = podlink_ui_theme.ui_prefs_path()
        self.assertFalse(prefs_file.exists())
        self.assertEqual(self.theme_manager.mode, "system")

        # Switch to dark via header segmented control
        self.window.header_theme_switch.button("dark").click()
        self.app.processEvents()
        self.assertEqual(self.theme_manager.mode, "dark")
        self.assertEqual(self.theme_manager.resolved, "dark")
        self.assertEqual(self.window.settings_theme_switch.current(), "dark")
        self.assertEqual(json.loads(prefs_file.read_text(encoding="utf-8")), {"theme": "dark"})
        self.assertFalse(podlink_settings.settings_path().exists())

        # Reload ThemeManager to verify persistence across restarts
        reloaded = podlink_ui_theme.ThemeManager(self.app)
        self.assertEqual(reloaded.mode, "dark")

        # Switch to light via settings segmented control
        self.window.settings_theme_switch.button("light").click()
        self.app.processEvents()
        self.assertEqual(self.theme_manager.mode, "light")
        self.assertEqual(self.theme_manager.resolved, "light")
        self.assertEqual(self.window.header_theme_switch.current(), "light")
        self.assertEqual(json.loads(prefs_file.read_text(encoding="utf-8")), {"theme": "light"})

        # Switch back to system and verify dynamic system colorScheme following
        with patch.object(podlink_ui_theme, "detect_system_theme", return_value="dark"):
            self.window.header_theme_switch.button("system").click()
            self.app.processEvents()
            self.assertEqual(self.theme_manager.mode, "system")
            self.assertEqual(self.theme_manager.resolved, "dark")

        with patch.object(podlink_ui_theme, "detect_system_theme", return_value="light"):
            self.theme_manager._on_system_scheme_changed()
            self.app.processEvents()
            self.assertEqual(self.theme_manager.resolved, "light")

        # Malformed desktop-ui.json safely falls back to system
        prefs_file.write_text("{broken json", encoding="utf-8")
        self.assertEqual(podlink_ui_theme.load_theme_preference(prefs_file), "system")

    # ------------------------------------------------------------------
    # Jellyfin Password Login (Default) & Token Login (Alternative)
    # ------------------------------------------------------------------
    def test_password_login_is_default_masked_clears_on_submit_and_not_persisted(self) -> None:
        self.assertEqual(self.window.auth_mode, "password")
        self.assertEqual(self.window.auth_stack.currentIndex(), 0)
        self.assertEqual(self.window.password.echoMode(), QtWidgets.QLineEdit.EchoMode.Password)

        # Peek toggle reveals and hides password
        self.window.password.toggle_revealed()
        self.assertEqual(self.window.password.echoMode(), QtWidgets.QLineEdit.EchoMode.Normal)

        scopes = [
            hrmediasync.Scope("all", "账号可见的全部音乐"),
            hrmediasync.Scope("library", "FLAC 曲库", "1" * 32),
            hrmediasync.Scope("playlist", "车载歌单", "2" * 32),
        ]
        fake_source = MagicMock(identity="a" * 24)
        fake_source.scopes.return_value = scopes

        gate = threading.Event()
        captured = {}

        def fake_with_password(url, username, password):
            captured["args"] = (url, username, password)
            gate.wait(2.0)
            return fake_source

        self.window.url.setText("https://jf.example.com")
        self.window.username.setText("alice")
        self.window.password.setText("super-secret-pass")

        with patch.object(hrmediasync.JellyfinSource, "with_password", side_effect=fake_with_password):
            self.window.connect_button.click()
            self.app.processEvents()

            # Password MUST be cleared and re-masked immediately on submit (before worker finishes)
            self.assertEqual(self.window.password.text(), "")
            self.assertEqual(self.window.password.echoMode(), QtWidgets.QLineEdit.EchoMode.Password)
            self.assertTrue(self.window.busy)
            self.assertFalse(self.window.connect_button.isEnabled())
            self.assertFalse(self.window.auth_switch.button("token").isEnabled())

            # Close guard blocks closing while background task is active
            close_ev = QtGui.QCloseEvent()
            self.window.closeEvent(close_ev)
            self.assertFalse(close_ev.isAccepted())

            gate.set()
            wait_until(self.app, lambda: not self.window.busy)

        self.assertEqual(captured["args"], ("https://jf.example.com", "alice", "super-secret-pass"))
        self.assertIs(self.window.source, fake_source)
        self.assertEqual(
            [self.window.scope.itemText(i) for i in range(self.window.scope.count())],
            ["账号可见的全部音乐", "音乐库 · FLAC 曲库", "播放列表 · 车载歌单"],
        )
        self.assertTrue(self.window.preview_button.isEnabled())
        self.assertIn("Jellyfin 已连接", self.window.status.text())

        # Ensure password/username were never written anywhere under XDG_CONFIG_HOME
        for path in self.config_root.rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn("super-secret-pass", content)
                self.assertNotIn("alice", content)

        # Now idle: closeEvent accepts
        close_ev2 = QtGui.QCloseEvent()
        self.window.closeEvent(close_ev2)
        self.assertTrue(close_ev2.isAccepted())

    def test_token_login_alternative_mode(self) -> None:
        self.window.auth_switch.button("token").click()
        self.app.processEvents()
        self.assertEqual(self.window.auth_mode, "token")
        self.assertEqual(self.window.auth_stack.currentIndex(), 1)
        self.assertEqual(self.window.token.echoMode(), QtWidgets.QLineEdit.EchoMode.Password)

        scopes = [hrmediasync.Scope("all", "账号可见的全部音乐")]
        fake_source = MagicMock(identity="b" * 24)
        fake_source.scopes.return_value = scopes

        self.window.url.setText("https://jf.example.com")
        self.window.token.setText("user-token-123")

        with patch.object(hrmediasync, "JellyfinSource", return_value=fake_source) as ctor:
            self.window.connect_button.click()
            wait_until(self.app, lambda: not self.window.busy)

        ctor.assert_called_once_with("https://jf.example.com", "user-token-123")
        self.assertIs(self.window.source, fake_source)
        self.assertTrue(self.window.preview_button.isEnabled())

    # ------------------------------------------------------------------
    # Preview, Secondary Confirmation, Apply & Failure Handling
    # ------------------------------------------------------------------
    def test_preview_confirm_deletion_and_apply_flow(self) -> None:
        scope_all = hrmediasync.Scope("all", "账号可见的全部音乐")
        scope_pl = hrmediasync.Scope("playlist", "最爱", "3" * 32)
        fake_source = MagicMock(identity="e" * 24)
        self.window.connected((fake_source, [scope_all, scope_pl]))
        self.window.update_buttons()

        add_track = hrmediasync.Track("a" * 32, "Track Alpha", ".flac", "e" * 24 + "1" * 40 + ".flac")
        rem_name = "e" * 24 + "9" * 40 + ".flac"
        plan = hrmediasync.Plan("e" * 24, scope_all, "usb-1", (add_track,), frozenset([rem_name]))

        with patch.object(hrmediasync, "preview", return_value=plan) as preview_fn:
            self.window.port.setText("/dev/ttyACM9")
            self.window.preview_button.click()
            wait_until(self.app, lambda: not self.window.busy)

        preview_fn.assert_called_once_with(fake_source, scope_all, "/dev/ttyACM9")
        self.assertIn("＋ Track Alpha", self.window.details.toPlainText())
        self.assertIn(f"－ {rem_name}", self.window.details.toPlainText())
        self.assertTrue(self.window.apply_button.isEnabled())
        self.assertIn("新增 1 首，匹配 iPod 原有曲目 0 首，删除 1 首", self.window.status.text())

        # Verify confirmation dialog safety properties: default & escape button is "取消"
        box, cancel_btn, apply_btn = self.window.build_confirm_dialog(plan)
        self.assertEqual(cancel_btn.text(), "取消")
        self.assertEqual(apply_btn.text(), "应用同步")
        self.assertIs(box.defaultButton(), cancel_btn)
        self.assertIs(box.escapeButton(), cancel_btn)
        self.assertIn("删除 iPod 上的 1 首同步副本？", box.text())
        self.assertEqual(box.informativeText(), "只删除预览列出的同步副本。")
        box.deleteLater()

        # Declining confirmation must NOT call apply
        with patch.object(self.window, "confirm_removals", return_value=False) as confirm_fn, \
                patch.object(hrmediasync, "apply") as apply_fn:
            self.window.apply_button.click()
            self.app.processEvents()
        confirm_fn.assert_called_once_with(plan)
        apply_fn.assert_not_called()

        # Approving confirmation runs apply in background and clears plan
        with patch.object(self.window, "confirm_removals", return_value=True), \
                patch.object(hrmediasync, "apply", return_value=(1, 1)) as apply_fn:
            self.window.apply_button.click()
            wait_until(self.app, lambda: not self.window.busy)

        apply_fn.assert_called_once_with(fake_source, plan, "/dev/ttyACM9")
        self.assertIsNone(self.window.plan)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.assertEqual(self.window.status.text(), "已同步。写入 1 首，删除 1 首。")

    def test_apply_without_removals_skips_deletion_confirmation(self) -> None:
        scope_all = hrmediasync.Scope("all", "账号可见的全部音乐")
        fake_source = MagicMock(identity="e" * 24)
        self.window.connected((fake_source, [scope_all]))
        add_track = hrmediasync.Track("a" * 32, "Only Add", ".mp3", "e" * 24 + "2" * 40 + ".mp3")
        plan = hrmediasync.Plan("e" * 24, scope_all, "usb-1", (add_track,), frozenset())
        self.window.show_plan(plan)
        self.window.update_buttons()

        with patch.object(self.window, "confirm_removals") as confirm_fn, \
                patch.object(hrmediasync, "apply", return_value=(1, 0)) as apply_fn:
            self.window.apply_button.click()
            wait_until(self.app, lambda: not self.window.busy)

        confirm_fn.assert_not_called()
        apply_fn.assert_called_once_with(fake_source, plan, "")
        self.assertEqual(self.window.status.text(), "已同步。写入 1 首，删除 0 首。")

    def test_background_failure_reports_error_and_recovers(self) -> None:
        self.window.url.setText("https://jf.example.com")
        self.window.username.setText("bob")
        self.window.password.setText("wrong")

        with patch.object(
            hrmediasync.JellyfinSource,
            "with_password",
            side_effect=JellyfinError("invalid credentials"),
        ), patch.object(self.window, "show_error") as show_error:
            self.window.connect_button.click()
            wait_until(self.app, lambda: not self.window.busy)

        self.assertEqual(self.window.password.text(), "")
        self.assertEqual(self.window.status.text(), "操作失败。")
        show_error.assert_called_once()
        self.assertIn("invalid credentials", str(show_error.call_args.args[0]))
        self.assertTrue(self.window.connect_button.isEnabled())

    def test_scope_or_credential_changes_invalidate_state(self) -> None:
        scopes = [
            hrmediasync.Scope("all", "账号可见的全部音乐"),
            hrmediasync.Scope("library", "摇滚", "4" * 32),
        ]
        fake_source = MagicMock(identity="e" * 24)
        self.window.connected((fake_source, scopes))
        add_track = hrmediasync.Track("a" * 32, "Song", ".flac", "e" * 24 + "3" * 40 + ".flac")
        self.window.show_plan(
            hrmediasync.Plan("e" * 24, scopes[0], "usb-1", (add_track,), frozenset())
        )
        self.window.update_buttons()
        self.assertTrue(self.window.apply_button.isEnabled())

        # Changing scope clears plan only
        self.window.scope.setCurrentIndex(1)
        self.app.processEvents()
        self.assertIsNone(self.window.plan)
        self.assertIs(self.window.source, fake_source)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.assertTrue(self.window.preview_button.isEnabled())

        # Editing URL clears source too
        self.window.url.setText("https://other.example.com")
        self.app.processEvents()
        self.assertIsNone(self.window.source)
        self.assertFalse(self.window.preview_button.isEnabled())

    # ------------------------------------------------------------------
    # Settings Validation & Persistence
    # ------------------------------------------------------------------
    def test_settings_validation_and_save(self) -> None:
        for key in podlink_settings.FIELDS:
            self.assertIn(key, self.window.entries)

        # Invalid HTTP Jellyfin URL rejected without saving
        self.window.entries["JELLYFIN_URL"].setText("http://insecure.example.com")
        with patch.object(self.window, "show_error") as show_error:
            self.window.save()
        show_error.assert_called_once()
        self.assertFalse(podlink_settings.settings_path().exists())

        # Invalid choice rejected
        self.window.entries["JELLYFIN_URL"].setText("https://media.example.com")
        self.window.entries["CIDER_BACKEND"].setCurrentText("bogus")
        with patch.object(self.window, "show_error") as show_error:
            self.window.save()
        show_error.assert_called_once()
        self.assertFalse(podlink_settings.settings_path().exists())

        # Valid settings + Cider/Rufin tokens save properly and clear token fields
        self.window.entries["CIDER_API_URL"].setText("http://127.0.0.1:10767")
        self.window.entries["CIDER_BACKEND"].setCurrentText("rpc")
        self.window.entries["RUFIN_API_URL"].setText("http://127.0.0.1:1717")
        self.window.entries["HERDR_SOCKET_PATH"].setText("/tmp/herdr.sock")
        self.window.entries["HERDREMOTE_VOICE_FOCUS"].setCurrentText("on")
        self.window.entries["HERDREMOTE_USAGE_CACHE_DIR"].setText("/tmp/usage-cache")
        self.window.entries["HERDREMOTE_KEY_TOOL"].setCurrentText("wtype")
        self.window.player_tokens[0].setText("cider-secret-tok")
        self.window.player_tokens[1].setText("rufin-secret-tok")

        with patch.object(self.window, "show_error") as show_error:
            self.window.save()
        show_error.assert_not_called()

        # Token fields cleared & re-masked; URL synced to Sync tab
        self.assertEqual(self.window.player_tokens[0].text(), "")
        self.assertEqual(self.window.player_tokens[1].text(), "")
        self.assertEqual(self.window.url.text(), "https://media.example.com")
        self.assertIn("设置已保存", self.window.settings_status.text())

        saved = podlink_settings.load()
        self.assertEqual(saved["JELLYFIN_URL"], "https://media.example.com")
        self.assertEqual(saved["CIDER_BACKEND"], "rpc")
        self.assertEqual(saved["RUFIN_API_URL"], "http://127.0.0.1:1717")
        self.assertEqual(saved["HERDR_SOCKET_PATH"], "/tmp/herdr.sock")
        self.assertEqual(saved["HERDREMOTE_VOICE_FOCUS"], "on")
        self.assertEqual(saved["HERDREMOTE_USAGE_CACHE_DIR"], "/tmp/usage-cache")
        self.assertEqual(saved["HERDREMOTE_KEY_TOOL"], "wtype")
        self.assertEqual(podlink_settings.cider_token(), "cider-secret-tok")
        rufin_token_path = podlink_settings.settings_path().with_name("rufin-token")
        self.assertEqual(rufin_token_path.read_text(encoding="utf-8").strip(), "rufin-secret-tok")


if __name__ == "__main__":
    unittest.main()
