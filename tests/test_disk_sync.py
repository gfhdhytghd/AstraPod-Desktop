"""Offscreen coverage of both Disk Mode switches and their job routing."""
import json
from unittest.mock import patch

from test_podlink_gui import GuiCase, podlink_ui_theme, wait_until


class DiskSyncTests(GuiCase):
    def test_disk_sync_persists_and_uses_existing_confirmation(self):
        page = self.window.pages["sync"]
        self.assertFalse(page.disk_mode.isChecked())
        self.assertIn("USB", page.disk_mode.accessibleDescription())
        page.disk_mode.setChecked(True)
        prefs = json.loads(podlink_ui_theme.ui_prefs_path().read_text())
        self.assertIs(prefs["sync_disk_mode"], True)
        self.assertFalse(self.window.pages["import"].disk_mode.isChecked())

        page.url.setText("https://jellyfin.example.com")
        page.username.setText("alice")
        page.password.setText("secret")
        page.connect_source()
        wait_until(self.app, lambda: page.source is not None)
        page.preview()
        wait_until(self.app, lambda: page.plan is not None)
        plan = page.plan
        self.assertTrue(plan.removals)
        with patch.object(page, "confirm_removals", return_value=False):
            page.confirm()
        self.assertNotIn("apply_disk", [call[0] for call in self.services.sync.calls])
        with patch.object(page, "confirm_removals", return_value=True):
            page.confirm()
        wait_until(self.app, lambda: page.plan is None and not page.busy)
        self.assertIn(("apply_disk", plan, page.port()), self.services.sync.calls)
        self.assertIn("同步完成", page.status.text())

    def test_disk_import_passes_selected_files_and_skip_preference(self):
        page = self.window.pages["import"]
        self.assertFalse(page.disk_mode.isChecked())
        page.disk_mode.setChecked(True)
        page.skip_check.setChecked(False)
        self.assertIs(self.window._load_prefs()["import_disk_mode"], True)
        source = self.root / "song.flac"
        source.write_bytes(b"music")
        page.add_paths([str(source)])
        wait_until(self.app, lambda: page.selected())
        with patch.object(self.services.library, "import_disk",
                          wraps=self.services.library.import_disk) as applied:
            page.start()
            wait_until(self.app, lambda: page.job is None)
        self.assertEqual(applied.call_args.args[0], [source])
        self.assertEqual(applied.call_args.kwargs, {"skip_duplicates": False})
        self.assertIn("已导入 1 首", page.summary.text())
