#!/usr/bin/env python3
"""Desktop window for the PodLink media importer."""
import json
from pathlib import Path
from queue import Empty, SimpleQueue
import subprocess
import sys
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class ImportWindow:
    def __init__(self, root):
        self.root = root
        self.paths = []
        self.results = SimpleQueue()
        self.busy = False
        root.title("AstraPod · 媒体同步")
        root.geometry("760x490")
        root.minsize(560, 390)
        root.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(root, padding=24)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="媒体同步", font=("TkDefaultFont", 20, "bold")).pack(anchor="w")
        ttk.Label(body, text="在 iPod 上打开 System > PodLink。导入不需要 Disk Mode。").pack(
            anchor="w", pady=(8, 16))

        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x", pady=(0, 8))
        self.add_button = ttk.Button(toolbar, text="添加音频…", command=self.add)
        self.add_button.pack(side="left")
        self.remove_button = ttk.Button(toolbar, text="移除所选", command=self.remove)
        self.remove_button.pack(side="left", padx=(8, 0))
        self.port = tk.StringVar()
        self.port_entry = ttk.Entry(toolbar, textvariable=self.port, width=18)
        self.port_entry.pack(side="right")
        ttk.Label(toolbar, text="串口（留空自动识别）").pack(side="right", padx=(0, 8))

        list_frame = ttk.Frame(body)
        list_frame.pack(fill="both", expand=True)
        self.files = ttk.Treeview(list_frame, columns=("file", "rate"), show="headings",
                                  selectmode="extended")
        self.files.heading("file", text="音频文件")
        self.files.heading("rate", text="采样率")
        self.files.column("file", width=410)
        self.files.column("rate", width=150, stretch=False)
        scroll = ttk.Scrollbar(list_frame, command=self.files.yview)
        self.files.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.files.pack(side="left", fill="both", expand=True)

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(16, 0))
        self.status = tk.StringVar(value="超过 48 kHz 的音频会自动降采样。")
        ttk.Label(footer, textvariable=self.status).pack(side="left")
        self.import_button = ttk.Button(footer, text="导入到 iPod", command=self.start)
        self.import_button.pack(side="right")
        self.update_buttons()
        self.files.bind("<<TreeviewSelect>>", self.update_buttons)

    def update_buttons(self, _event=None):
        self.add_button.state(["disabled" if self.busy else "!disabled"])
        self.port_entry.state(["disabled" if self.busy else "!disabled"])
        self.remove_button.state(["!disabled" if not self.busy and self.files.selection()
                                  else "disabled"])
        self.import_button.state(["!disabled" if self.paths and not self.busy else "disabled"])

    def redraw(self):
        self.files.delete(*self.files.get_children())
        for index, path in enumerate(self.paths):
            self.files.insert("", "end", iid=str(index), values=(str(path), "待检测"))
        self.update_buttons()

    def add(self):
        paths = filedialog.askopenfilenames(
            parent=self.root, title="选择音频文件",
            filetypes=[("音频", "*.flac *.m4a *.mp3 *.FLAC *.M4A *.MP3"), ("所有文件", "*")])
        for path in paths:
            if path not in self.paths:
                self.paths.append(path)
        self.redraw()

    def remove(self):
        selected = {int(index) for index in self.files.selection()}
        self.paths = [path for index, path in enumerate(self.paths) if index not in selected]
        self.redraw()

    def start(self):
        command = [sys.executable, str(Path(__file__).with_name("podlink.py"))]
        port = self.port.get().strip()
        if port:
            command.extend(("--port", port))
        command.extend(("import-media", "--", *self.paths))
        self.busy = True
        self.status.set("正在检查采样率、转换并导入音频…")
        self.update_buttons()
        Thread(target=self.run_import, args=(command,), daemon=True).start()
        self.root.after(100, self.poll)

    def run_import(self, command):
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.results.put((json.loads(result.stdout)["result"]["tracks"], None))
        except subprocess.CalledProcessError as error:
            self.results.put((None, error.stderr.strip() or str(error)))
        except (OSError, ValueError, KeyError) as error:
            self.results.put((None, str(error)))

    def poll(self):
        try:
            tracks, error = self.results.get_nowait()
        except Empty:
            self.root.after(100, self.poll)
            return
        self.busy = False
        self.update_buttons()
        if error:
            self.status.set("导入失败。")
            messagebox.showerror("导入失败", error, parent=self.root)
            return
        for index, track in enumerate(tracks):
            source = track["source_sample_rate_hz"] / 1000
            target = track["sample_rate_hz"] / 1000
            rate = f"{source:g} kHz" if source == target else f"{source:g} → {target:g} kHz"
            self.files.set(str(index), "rate", rate)
        self.status.set(f"已导入 {len(tracks)} 首。设备已接收曲库更新请求。")

    def close(self):
        if self.busy:
            self.root.bell()
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    ImportWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
