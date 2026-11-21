"""The input monitor's Apple Remote: model choice, detection and highlights."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_podlink_gui import GuiCase  # noqa: E402

from astrapod_desktop.pages import extensions  # noqa: E402
from astrapod_desktop.pages.extensions import normalize_schema  # noqa: E402
from astrapod_desktop.qt import QtCore, QtGui  # noqa: E402

MANIFEST = Path(__file__).resolve().parents[1] / "astrapod_plugins/apple-ir/plugin.json"


class RemoteMonitorTests(GuiCase):
    def setUp(self) -> None:
        super().setUp()
        self.page = self.window.pages["extensions"]
        self.host = self.services.plugins.host
        self.pad = self.page.pad

    def save_remote(self, choice: str) -> None:
        self.page.list.setCurrentRow(0)
        combo = self.page.fields["remote"]
        combo.setCurrentIndex(combo.findData(choice))
        self.page.save_settings()
        self.app.processEvents()

    def test_both_drawings_name_every_button(self) -> None:
        keys = {"plastic": ["menu", "play", "left", "right", "vol_up", "vol_down"],
                "aluminium": ["menu", "play", "select", "left", "right", "vol_up", "vol_down"]}
        for model, wanted in keys.items():
            with self.subTest(model=model):
                self.pad.set_model(model)
                renderer = self.pad.renderer()
                self.assertTrue(renderer.isValid())
                self.assertEqual([k for k in wanted if self.pad.element(k)], wanted)
                self.assertFalse(renderer.elementExists("key-select") and model == "plastic")
                view = renderer.viewBoxF()
                body = renderer.boundsOnElement("body")
                ratio = body.height() / body.width()
                self.assertAlmostEqual(ratio, 83 / 32 if model == "plastic" else 3.93, delta=0.05)
                self.assertTrue(view.contains(body))

    def test_choices_show_chinese_labels_and_default_to_aluminium(self) -> None:
        combo = self.page.fields["remote"]
        self.assertEqual([combo.itemText(i) for i in range(combo.count())],
                         ["自动识别", "铝合金（第二代）", "塑料（第一代）"])
        self.assertEqual(self.pad.model, "aluminium")
        self.assertIn("未识别", self.pad.toolTip())

    def test_aluminium_centre_is_select_and_play_is_its_own_button(self) -> None:
        self.host.emit_input("select", "down")
        self.host.emit_input("play", "down")
        self.app.processEvents()
        self.assertEqual(set(self.pad.lit), {"key-select", "key-play"})

    def test_plastic_centre_is_play(self) -> None:
        self.save_remote("plastic")
        self.assertEqual(self.pad.model, "plastic")
        self.assertEqual(self.host.values["apple-ir"]["remote"], "plastic")
        self.host.emit_input("select", "down")
        self.app.processEvents()
        self.assertEqual(set(self.pad.lit), {"key-play"})

    def test_auto_follows_the_remote_seen_and_a_choice_overrides_it(self) -> None:
        self.host.emit_input("play", "down", remote="plastic")
        self.app.processEvents()
        self.assertEqual(self.pad.model, "plastic")
        self.assertEqual(set(self.pad.lit), {"key-play"})
        self.assertIn("自动识别", self.pad.toolTip())
        self.save_remote("aluminium")
        self.assertEqual(self.pad.model, "aluminium")
        self.host.emit_input("play", "down", remote="plastic")
        self.app.processEvents()
        self.assertEqual(self.pad.model, "aluminium")
        self.save_remote("auto")
        self.assertEqual(self.pad.model, "plastic")

    def test_previewing_a_choice_redraws_before_saving(self) -> None:
        combo = self.page.fields["remote"]
        combo.setCurrentIndex(combo.findData("plastic"))
        self.assertEqual(self.pad.model, "plastic")
        self.assertEqual(self.host.values["apple-ir"]["remote"], "auto")

    def test_ring_highlight_covers_only_its_quarter(self) -> None:
        for model in ("plastic", "aluminium"):
            self.pad.set_model(model)
            renderer = self.pad.renderer()
            ring = renderer.boundsOnElement("ring")
            centre = ring.center()
            for key, (dx, dy) in {"vol_up": (0, -1), "vol_down": (0, 1),
                                  "left": (-1, 0), "right": (1, 0)}.items():
                with self.subTest(model=model, key=key):
                    shape = self.pad._shape(renderer, self.pad.element(key))
                    middle = (ring.width() / 2 + renderer.boundsOnElement(
                        self.pad.element("play" if model == "plastic" else "select")).width() / 2) / 2
                    self.assertTrue(shape.contains(centre + QtCore.QPointF(dx, dy) * middle))
                    self.assertFalse(shape.contains(centre - QtCore.QPointF(dx, dy) * middle))
                    self.assertFalse(shape.contains(centre))

    def test_long_press_stays_lit_while_repeats_arrive(self) -> None:
        clock = [100.0]
        with patch.object(extensions.time, "monotonic", lambda: clock[0]):
            self.pad.flash("vol_up", "down")
            self.pad.flash("vol_up", "repeat")
            for _ in range(10):
                clock[0] += 0.1
                self.pad.flash("vol_up", "repeat")
                self.pad._fade()
            self.assertEqual(self.pad.lit["key-vol_up"], 1.0)
            self.assertIn("key-vol_up", self.pad.long)
            self.pad.flash("vol_up", "up")
            for _ in range(20):
                self.pad._fade()
            self.assertNotIn("key-vol_up", self.pad.lit)
            self.assertNotIn("key-vol_up", self.pad.long)
            self.pad.flash("menu", "down")
            clock[0] += extensions.HOLD_SECONDS + 0.01
            for _ in range(20):
                self.pad._fade()
            self.assertNotIn("key-menu", self.pad.lit)

    def test_highlight_paints_the_pressed_button(self) -> None:
        self.pad.resize(640, 170)
        idle = self.pad.grab().toImage()
        self.pad.flash("menu", "down")
        lit = self.pad.grab().toImage()
        renderer = self.pad.renderer()
        view = renderer.viewBoxF()
        transform = self.pad.view_transform(view, self.pad.scale_for(view))
        menu = renderer.boundsOnElement("key-menu")
        edge = transform.map(QtCore.QPointF(menu.center().x() - menu.width() * 0.4,
                                            menu.center().y()))
        ratio = idle.devicePixelRatio()
        point = QtCore.QPoint(round(edge.x() * ratio), round(edge.y() * ratio))
        self.assertNotEqual(idle.pixelColor(point), lit.pixelColor(point))
        accent = QtGui.QColor(self.page.tokens.accent)
        self.assertLess(abs(lit.pixelColor(point).hue() - accent.hue()) % 360, 25)

    def test_drawing_lies_on_its_side_with_the_ir_window_right(self) -> None:
        self.pad.resize(640, 170)
        renderer = self.pad.renderer()
        view = renderer.viewBoxF()
        transform = self.pad.view_transform(view, self.pad.scale_for(view))
        body = transform.mapRect(renderer.boundsOnElement("body"))
        ring = transform.map(renderer.boundsOnElement("ring").center())
        window = transform.map(renderer.boundsOnElement("ir-window").center())
        self.assertGreater(body.width(), 3 * body.height())
        self.assertLessEqual(body.height(), 170)
        self.assertLessEqual(body.width(), 640)
        self.assertGreater(window.x(), ring.x())


class SchemaTests(GuiCase):
    def test_manifest_remote_field_normalises_to_a_labelled_enum(self) -> None:
        schema = json.loads(MANIFEST.read_text(encoding="utf-8"))["settings"]
        field = next(f for f in normalize_schema(schema) if f["key"] == "remote")
        self.assertEqual(field["type"], "enum")
        self.assertEqual(field["choices"], ["auto", "aluminium", "plastic"])
        self.assertEqual(field["labels"]["plastic"], "塑料（第一代）")
        self.assertNotIn("labels", normalize_schema({"a": {"enum": [1], "labels": "x"}})[0])
