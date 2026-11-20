#!/usr/bin/env python3
"""Wayland-compatible desktop window for PodLink media import."""
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk


class ImportWindow(Gtk.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="AstraPod · 媒体同步")
        self.set_default_size(760, 490)
        self.paths = {}
        self.busy = False
        self.connect("close-request", lambda *_: self.busy)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ("top", "bottom", "start", "end"):
            getattr(body, f"set_margin_{side}")(24)
        self.set_child(body)

        heading = Gtk.Label(label="媒体同步", xalign=0)
        heading.add_css_class("title-1")
        body.append(heading)
        body.append(Gtk.Label(
            label="在 iPod 上打开 System > PodLink。导入不需要 Disk Mode。",
            xalign=0, wrap=True))

        toolbar = Gtk.Box(spacing=8)
        body.append(toolbar)
        self.add_button = Gtk.Button(label="添加音频…")
        self.add_button.connect("clicked", self.add)
        toolbar.append(self.add_button)
        self.remove_button = Gtk.Button(label="移除所选")
        self.remove_button.connect("clicked", self.remove)
        toolbar.append(self.remove_button)
        toolbar.append(Gtk.Box(hexpand=True))
        self.port = Gtk.Entry(placeholder_text="串口（留空自动识别）", width_chars=23)
        toolbar.append(self.port)

        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=160)
        self.files = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE)
        self.files.add_css_class("boxed-list")
        self.files.connect("selected-rows-changed", self.update_buttons)
        scroll.set_child(self.files)
        body.append(scroll)

        footer = Gtk.Box(spacing=12)
        self.status = Gtk.Label(label="超过 48 kHz 的音频会自动降采样。",
                                xalign=0, hexpand=True, wrap=True)
        footer.append(self.status)
        self.import_button = Gtk.Button(label="导入到 iPod", valign=Gtk.Align.CENTER)
        self.import_button.add_css_class("suggested-action")
        self.import_button.connect("clicked", self.start)
        footer.append(self.import_button)
        body.append(footer)
        self.update_buttons()

    def update_buttons(self, *_):
        self.add_button.set_sensitive(not self.busy)
        self.port.set_sensitive(not self.busy)
        self.remove_button.set_sensitive(not self.busy and bool(self.files.get_selected_rows()))
        self.import_button.set_sensitive(not self.busy and bool(self.paths))

    def add(self, _button):
        dialog = Gtk.FileChooserNative(
            title="选择音频文件", transient_for=self,
            action=Gtk.FileChooserAction.OPEN, accept_label="添加", cancel_label="取消")
        dialog.set_select_multiple(True)
        audio_filter = Gtk.FileFilter(name="音频")
        for suffix in ("mp3", "m4a", "flac"):
            audio_filter.add_suffix(suffix)
        dialog.add_filter(audio_filter)
        dialog.connect("response", self.add_response)
        dialog.show()

    def add_response(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            for file in dialog.get_files():
                path = file.get_path()
                if not path or any(item[0] == path for item in self.paths.values()):
                    continue
                fields = Gtk.Box(spacing=12, margin_top=9, margin_bottom=9,
                                 margin_start=12, margin_end=12)
                name = Gtk.Label(label=Path(path).name, xalign=0, hexpand=True, wrap=True)
                name.set_tooltip_text(path)
                fields.append(name)
                rate = Gtk.Label(label="待检测")
                fields.append(rate)
                row = Gtk.ListBoxRow(child=fields)
                self.paths[row] = (path, rate)
                self.files.append(row)
            self.update_buttons()
        dialog.destroy()

    def remove(self, _button):
        for row in self.files.get_selected_rows():
            del self.paths[row]
            self.files.remove(row)
        self.update_buttons()

    def start(self, _button):
        command = [sys.executable, str(Path(__file__).with_name("podlink.py"))]
        port = self.port.get_text().strip()
        if port:
            command.extend(("--port", port))
        command.extend(("import-media", "--", *(path for path, _ in self.paths.values())))
        self.busy = True
        self.status.set_text("正在检查采样率、转换并导入音频…")
        self.update_buttons()
        Thread(target=self.run_import, args=(command,), daemon=True).start()

    def run_import(self, command):
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            GLib.idle_add(self.finish, json.loads(result.stdout)["result"]["tracks"], None)
        except subprocess.CalledProcessError as error:
            GLib.idle_add(self.finish, None, error.stderr.strip() or str(error))
        except (OSError, ValueError, KeyError) as error:
            GLib.idle_add(self.finish, None, str(error))

    def finish(self, tracks, error):
        self.busy = False
        self.update_buttons()
        if error:
            self.status.set_text("导入失败。")
            Gtk.AlertDialog(message="导入失败", detail=error).show(self)
            return
        for (_, label), track in zip(self.paths.values(), tracks):
            source = track["source_sample_rate_hz"] / 1000
            target = track["sample_rate_hz"] / 1000
            label.set_text(f"{source:g} kHz" if source == target else f"{source:g} → {target:g} kHz")
        self.status.set_text(f"已导入 {len(tracks)} 首。设备已接收曲库更新请求。")


def activate(application):
    window = application.get_active_window() or ImportWindow(application)
    window.present()


def main():
    application = Gtk.Application(application_id="org.podbox.AstraPodMedia",
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
    application.connect("activate", activate)
    return application.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
