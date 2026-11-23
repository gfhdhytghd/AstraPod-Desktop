"""Offscreen coverage of the shared Disk Mode state and transfer routing."""
from unittest.mock import patch

from test_podlink_gui import GuiCase, wait_until


class DiskSyncTests(GuiCase):
    def connect_source(self):
        page = self.window.pages["sync"]
        page.url.setText("https://jellyfin.example.com")
        page.username.setText("alice")
        page.password.setText("secret")
        page.connect_source()
        wait_until(self.app, lambda: page.source is not None and not page.busy)
        return page

    def test_disk_preview_and_apply_keep_session_until_eject(self):
        page = self.window.pages["sync"]
        other = self.window.pages["import"]
        self.assertFalse(page.disk_mode.isChecked())
        page.disk_mode.setChecked(True)
        self.assertFalse(page.disk_mode.isEnabled())
        self.assertFalse(other.disk_mode.isEnabled())
        self.assertIn("挂载", page.status.text())
        wait_until(self.app, lambda: not self.window.disk.transition and other.disk_mode.isChecked())
        self.assertTrue(page.disk_mode.isChecked())
        self.assertNotIn("sync_disk_mode", self.window._load_prefs())

        self.connect_source()
        page.preview()
        self.assertFalse(other.disk_mode.isEnabled())
        wait_until(self.app, lambda: page.plan is not None)
        plan = page.plan
        self.assertTrue(plan.removals)
        self.assertIn(("preview_disk", plan.scope, "fake-volume"), self.services.sync.calls)
        with patch.object(page, "confirm_removals", return_value=False):
            page.confirm()
        self.assertNotIn("apply_disk", [call[0] for call in self.services.sync.calls])
        with patch.object(page, "confirm_removals", return_value=True):
            page.confirm()
        wait_until(self.app, lambda: page.plan is None and not page.busy)
        self.assertIn(("apply_disk", plan, "fake-volume"), self.services.sync.calls)
        self.assertTrue(other.disk_mode.isChecked())
        self.assertNotIn("refresh_disk", [call[0] for call in self.services.sync.calls])

        other.disk_mode.setChecked(False)
        wait_until(self.app, lambda: not self.window.disk.transition and not self.window.busy)
        self.assertFalse(page.disk_mode.isChecked())
        self.assertFalse(other.disk_mode.isChecked())
        self.assertEqual([c[0] for c in self.services.sync.calls if "disk" in c[0]],
                         ["enter_disk", "preview_disk", "apply_disk", "eject_disk", "refresh_disk"])

    def test_disk_import_uses_volume_and_external_eject_refreshes(self):
        page = self.window.pages["import"]
        daemon = self.window.daemon
        initial = daemon.state
        daemon.push({**initial, "link": {**initial["link"], "mode": "disk", "serial": None}})
        self.assertTrue(page.disk_mode.isChecked())
        self.assertTrue(self.window.pages["sync"].disk_mode.isChecked())
        page.skip_check.setChecked(False)
        self.assertNotIn("import_disk_mode", self.window._load_prefs())
        source = self.root / "song.flac"
        source.write_bytes(b"music")
        page.add_paths([str(source)])
        wait_until(self.app, lambda: page.selected() and not self.window.busy)
        sessions = self.services.library.sessions
        with patch.object(self.services.library, "import_disk",
                          wraps=self.services.library.import_disk) as applied:
            page.start()
            wait_until(self.app, lambda: page.job is None)
        self.assertEqual(applied.call_args.args[0], [source])
        self.assertFalse(applied.call_args.kwargs["skip_duplicates"])
        self.assertEqual(applied.call_args.kwargs["volume"], "fake-volume")
        self.assertEqual(self.services.library.sessions, sessions)
        self.assertIn("已导入 1 首", page.summary.text())
        self.assertIn("关闭磁盘模式", page.summary.text())

        daemon.push(initial)
        wait_until(self.app, lambda: not self.window.disk.transition and not self.window.busy)
        self.assertFalse(page.disk_mode.isChecked())
        self.assertEqual([c[0] for c in self.services.sync.calls if "disk" in c[0]],
                         ["refresh_disk"])

    def test_failed_transition_restores_both_switches(self):
        sync, importer = self.window.pages["sync"], self.window.pages["import"]
        self.services.sync.fail = RuntimeError("授权失败")
        sync.disk_mode.setChecked(True)
        wait_until(self.app, lambda: not self.window.disk.transition)
        self.assertFalse(sync.disk_mode.isChecked())
        self.assertFalse(importer.disk_mode.isChecked())
        self.assertTrue(sync.disk_mode.isEnabled())
        self.assertIn("授权失败", sync.status.text())
        self.assertIn("授权失败", importer.summary.text())

    def test_warm_library_snapshot_reaches_podlink_preview(self):
        self.load_library()
        page = self.connect_source()
        with patch.object(self.services.sync, "preview", wraps=self.services.sync.preview) as preview:
            page.preview()
            wait_until(self.app, lambda: page.plan is not None)
        self.assertEqual(preview.call_args.kwargs["cached_library"], self.window.library_state.snapshot())

    def test_stock_mass_storage_has_clear_status_without_podlink(self):
        sync, importer = self.window.pages["sync"], self.window.pages["import"]
        daemon = self.window.daemon
        backend = self.services.sync
        with patch.object(backend, "disk_volume", return_value="stock-volume"), \
                patch.object(backend, "disk_player", return_value=None):
            daemon.push({**daemon.state, "link": {"mode": "none"}})
            self.assertTrue(sync.disk_mode.isChecked())
            self.assertTrue(importer.disk_mode.isChecked())
            self.assertTrue(sync.disk_mode.isEnabled())
            self.assertFalse(sync.preview_button.isEnabled())
            self.assertFalse(importer.import_button.isEnabled())
            self.assertIn("Apple 原厂固件", sync.status.text())
            self.assertIn("Apple 原厂固件", importer.summary.text())
        self.assertFalse(self.errors)
