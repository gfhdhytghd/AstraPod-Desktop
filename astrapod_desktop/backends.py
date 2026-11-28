"""Adapters from the GUI onto interfaces 1-3 and the Jellyfin sync module.

Each adapter has the same surface as its stand-in in fakes.py, so pages
never test which one they hold.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from functools import partial
import hashlib
import json
import os
import re
from pathlib import Path

from . import contracts
from .qt import QtCore, QtNetwork, Signal


def cache_root() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "astrapod"


def _optional(name: str):
    try:
        return __import__(name)
    except ImportError:
        return None


# ----------------------------------------------------------------------
# Interface 1: the iPod library over PodLink
# ----------------------------------------------------------------------
class FallbackLibraryCache:
    """JSON track cache and PNG art paths when hrlibrary is not present."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _tracks_path(self, serial, commit_id) -> Path:
        return self.directory / f"tracks-{serial}-{commit_id}.json"

    def load(self, serial, commit_id):
        try:
            rows = json.loads(self._tracks_path(serial, commit_id).read_text("utf-8"))
            return [contracts.DeviceTrack(**row) for row in rows]
        except (OSError, ValueError, TypeError):
            return None

    def store(self, serial, commit_id, tracks) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        rows = [contracts.track_record(track) for track in tracks]
        self._tracks_path(serial, commit_id).write_text(
            json.dumps(rows, ensure_ascii=False), "utf-8")

    def art_path(self, serial, track, size) -> Path:
        key = "\0".join(contracts.album_key(track))
        digest = hashlib.sha1(key.encode()).hexdigest()[:20]
        return self.directory / "art" / str(serial) / f"{digest}-{size}.png"


class LibrarySession:
    """One open PodLink link; every call is a request/reply round."""

    def __init__(self, module, link, identity):
        self.module = module
        self.link = link
        self.identity = identity
        self.commit_id = None

    def summary(self):
        summary = self.module.summary(self.link)
        self.commit_id = summary.commit_id
        return summary

    def tracks(self, progress=None, *, byte_progress=None):
        return self.module.tracks(self.link, progress, byte_progress=byte_progress,
                                  expected_commit=self.commit_id)

    def detail(self, track):
        return self.module.detail(self.link, track)

    def artwork(self, track, size="coverflow"):
        return self.module.artwork(self.link, track, size)

    def delete(self, track):
        return self.module.delete(self.link, track)

    def import_files(self, paths, progress, skip_duplicates=True, *, cached_library=None):
        snapshot = None
        if cached_library is not None and cached_library[0] == self.identity:
            snapshot = cached_library[1:]
        return self.module.import_files(self.link, paths, progress,
                                        skip_duplicates=skip_duplicates,
                                        cached_snapshot=snapshot)


class DeviceLibrary:
    """Open a library session for the selected firmware."""

    demo = False

    def __init__(self, cache_dir: Path | None = None):
        self.module = _optional("hrlibrary")
        self.cache_dir = Path(cache_dir) if cache_dir else cache_root() / "library"
        if self.module is not None and hasattr(self.module, "LibraryCache"):
            self.cache = self.module.LibraryCache(self.cache_dir)
        else:
            self.cache = FallbackLibraryCache(self.cache_dir)
        self.borrow = False
        self.port = ""
        self.device = None
        self._selected_port = None
        self._selected_serial = None

    @property
    def available(self) -> bool:
        return self.module is not None

    def detect_device(self):
        import hrstockdevice
        if self._selected_port != self.port:
            self._selected_port = self.port
            self._selected_serial = None
        device = hrstockdevice.current_device(self.port, podlink_serial=self._selected_serial)
        if self.port and device.kind == "podbox-podlink":
            self._selected_serial = device.serial
        return device

    def session_for_device(self, *, authorize=False):
        return partial(self.session, device=self.device, authorize=authorize)

    @contextmanager
    def session(self, *, device=None, authorize=False):
        if device is None:
            device = self.device or self.detect_device()
        if device != self.device and self.device is not None:
            raise RuntimeError("所选 iPod 已更改。")
        if device.kind == "stock":
            from hrstockdevice import stock_mount
            from .stock_library import StockLibrarySession

            with ExitStack() as stack:
                root = stack.enter_context(stock_mount(device, authorize=authorize))
                yield StockLibrarySession(root, device.serial, stack)
            return
        if device.kind != "podbox-podlink":
            raise RuntimeError("请用 USB 连接 iPod Classic。PodBox 须进入 PodLink 模式。")
        if self.module is None:
            raise RuntimeError("这个版本缺少 iPod 资料库模块（hrlibrary）。")
        import hrmediasync
        serial_access = hrmediasync.borrow_serial if self.borrow else nullcontext
        with serial_access(), hrmediasync.connect_device(device.port or self.port) as (link, identity):
            if device.serial and device.serial != identity:
                raise RuntimeError("所选 iPod 已更改。")
            yield LibrarySession(self.module, link, identity)

    def import_disk(self, paths, progress, skip_duplicates=True, *, volume, on_write):
        import hrdisksync
        volume.mount()
        return hrdisksync.import_mounted(volume, paths, progress, skip_duplicates,
                                         on_write=on_write)

    # The last library seen, so the window can show it before the iPod is
    # plugged in again.
    def _last_path(self) -> Path:
        return self.cache_dir / "last.json"

    def remember(self, serial, summary, *, stock=False) -> None:
        from podlink_settings import write_private

        payload = {"serial": serial, "stock": stock,
                   "summary": {name: getattr(summary, name)
                               for name in contracts.DeviceSummary.__dataclass_fields__}}
        write_private(self._last_path(), json.dumps(payload))

    def last(self):
        """The last cached library, with playlists for an Apple database."""
        try:
            payload = json.loads(self._last_path().read_text("utf-8"))
            summary = contracts.DeviceSummary(**payload["summary"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        tracks = self.cache.load(payload["serial"], summary.commit_id)
        if tracks is None:
            return None
        if payload.get("stock"):
            from .stock_library import cached_playlists
            playlists = cached_playlists(self.cache_dir, payload["serial"], summary.commit_id)
            return payload["serial"], summary, tracks, playlists or ()
        return payload["serial"], summary, tracks

    def art_path(self, serial, track, size) -> Path:
        return Path(self.cache.art_path(serial, track, size))

    @staticmethod
    def detect_ports() -> list[dict]:
        """Every attached iPod: {"port", "serial", "product"}."""
        import podlink_settings
        return podlink_settings.ipod_ports()

    @staticmethod
    def detect_port() -> str | None:
        """The iPod's /dev/serial/by-id path, or None when none is plugged in."""
        import podlink_settings
        detect = getattr(podlink_settings, "detect_ipod_port", None)
        if detect is None:
            return None
        try:
            return detect() or None
        except OSError:
            return None


# ----------------------------------------------------------------------
# Jellyfin sync
# ----------------------------------------------------------------------
class SyncBackend:
    """hrmediasync, with the daemon's serial borrowed when it is running."""

    demo = False

    def __init__(self):
        self.borrow = False

    def _serial(self) -> dict:
        if not self.borrow:
            return {}
        import hrmediasync
        return {"serial_access": hrmediasync.borrow_serial}

    def connect_password(self, url, username, password):
        import hrmediasync
        factory = getattr(hrmediasync.JellyfinSource, "with_password", None)
        if factory is None:
            raise RuntimeError("当前后端版本不支持 Jellyfin 账号密码登录，请改用访问令牌或更新后端。")
        source = factory(url, username, password)
        return source, source.scopes()

    def connect_token(self, url, token):
        import hrmediasync
        source = hrmediasync.JellyfinSource(url, token)
        return source, source.scopes()

    # The signed-in session, kept across restarts. Never holds a password.
    @staticmethod
    def _store(name):
        import podlink_settings
        return getattr(podlink_settings, name, None)

    def load_session(self):
        load = self._store("load_jellyfin_session")
        return load() if load is not None else None

    def save_session(self, record):
        save = self._store("save_jellyfin_session")
        if save is not None:
            save(record)

    def clear_session(self):
        clear = self._store("clear_jellyfin_session")
        if clear is not None:
            clear()

    def session_record(self, source, url, user_name=""):
        """What save_session stores for this source. Runs off the GUI thread:
        without a user name it asks Jellyfin for one."""
        import socket
        session = source.session
        if not user_name:
            reply = source.client.request(session, "Users/Me")
            user_name = reply.get("Name") if isinstance(reply.get("Name"), str) else ""
        device = hashlib.sha256(("AstraPod:" + socket.gethostname()).encode()).hexdigest()
        return {"url": session.base or url, "user_id": session.user, "user_name": user_name,
                "token": session.token, "device_id": device}

    def restore(self, record):
        """Reopen a saved session; the Users/Me call inside checks the token.
        A rejected token raises JellyfinError with status 401."""
        import hrmediasync
        source = hrmediasync.JellyfinSource(record["url"], record["token"])
        return source, source.scopes()

    # Read-only browsing for the 资料库 pages. Network only; never the iPod.
    LIBRARY_PAGE = 200

    def library_tracks(self, source, progress=None):
        """Every audio item the signed-in account sees, as JellyfinTrack rows."""
        from .jellyfin import track_from_item
        import hrmediasync

        build_snapshot = getattr(source, "tracks_from_items", None)
        if build_snapshot is not None:
            source.browsed_tracks = None
        rows, offset, total = [], 0, None
        consistent = True
        while total is None or offset < total:
            page, page_total = source.client.page(
                source.session, "Items", offset, self.LIBRARY_PAGE, IncludeItemTypes="Audio",
                Recursive="true", SortBy="SortName", EnableImages="false",
                EnableUserData="true", Fields="Genres,People,MediaSources,DateCreated,Path")
            if total is not None and page_total != total:
                consistent = False
            total = page_total
            if not page:
                break
            rows.extend(page)
            offset += len(page)
            if progress is not None:
                progress(offset, total)
        tracks = []
        for row in rows:
            track = track_from_item(row, len(tracks))
            if track is not None:
                tracks.append(track)
        if build_snapshot is not None and consistent and offset == total:
            try:
                source.browsed_tracks = build_snapshot(hrmediasync.Scope("all", ""), rows)
            except hrmediasync.JellyfinError:
                # Browsing can still show items if the sync snapshot is invalid.
                pass
        return tracks

    def album_art(self, source, track, pixels):
        """The album's (or else the track's) primary image as JPEG bytes;
        None when Jellyfin has none."""
        item = track.album_id or track.item_id
        try:
            return source.client.request(source.session, f"Items/{item}/Images/Primary",
                                         image=True, MaxWidth=pixels, MaxHeight=pixels,
                                         Format="Jpg")
        except OSError as error:
            if getattr(error, "status", None) == 404:
                return None
            raise

    @staticmethod
    def _source_tracks(source, scope):
        return getattr(source, "browsed_tracks", None) if scope.kind == "all" else None

    def preview(self, source, scope, port, *, cached_library=None):
        import hrmediasync
        return hrmediasync.preview(source, scope, port, cached_library=cached_library,
                                    source_tracks=self._source_tracks(source, scope), **self._serial())

    def apply(self, source, plan, port):
        import hrmediasync
        return hrmediasync.apply(source, plan, port, **self._serial())

    @staticmethod
    def disk_serial(port, live_port, live_serial):
        if not port or (live_port and Path(port).resolve() == Path(live_port).resolve()):
            return live_serial
        import hrdisksync

        identity = hrdisksync.usb_identity(hrdisksync.SYS_TTY / Path(port).resolve().name)
        if identity is not None:
            return identity[1]
        if Path(port).parent == Path("/dev/serial/by-id"):
            stable = re.search(r"_(2[0-9A-Fa-f]+)-if[0-9]+$", Path(port).name)
            if stable is not None:
                return stable[1]
        return None

    @staticmethod
    def disk_volume(link_mode, port, selected_serial):
        import hrdisksync

        if (link_mode == "podlink" or port) and selected_serial is None:
            return None
        return hrdisksync.current_volume(selected_serial)

    @staticmethod
    def disk_player(volume):
        import hrdisksync
        return hrdisksync.disk_player(volume)

    @staticmethod
    def enter_disk(port):
        import hrdisksync
        return hrdisksync.mount_disk(port)

    @staticmethod
    def eject_disk(volume):
        import hrdisksync
        device = hrdisksync.disk_player(volume)
        hrdisksync.eject_volume(volume)
        if device is not None:
            from serial.tools import list_ports

            def returned():
                for info in list_ports.comports():
                    tty = hrdisksync.SYS_TTY / Path(info.device).name
                    if hrdisksync.usb_identity(tty) == device:
                        return True

            hrdisksync.wait_for(returned, hrdisksync.WAIT_SECONDS, "等待 iPod 返回 PodLink 超时。")

    @staticmethod
    def refresh_disk(device):
        import hrdisksync
        import hrmediasync
        hrdisksync.refresh_after_eject(device, hrmediasync.borrow_serial)

    def preview_disk(self, source, scope, volume):
        import hrdisksync
        volume.mount()
        return hrdisksync.preview_mounted(source, scope, volume,
                                          source_tracks=self._source_tracks(source, scope))

    def apply_disk(self, source, plan, volume, *, on_write):
        import hrdisksync
        volume.mount()
        return hrdisksync.apply_mounted(source, plan, volume, on_write=on_write)


# ----------------------------------------------------------------------
# Interface 2: the daemon's desktop socket
# ----------------------------------------------------------------------
def empty_state() -> dict:
    return {
        "link": {"connected": False, "port": None, "mode": "none",
                 "serial": None, "borrowed": False, "firmware": None},
        "herdr": {"ok": False, "workspaces": []},
        "questions": [],
        "usage": [],
        "voice": {"state": "idle", "method": "", "focus": False, "last_result": None},
        "t3": {},
        "input": {"ir_fallback": "pending", "plugins": []},
        "settings_revision": 0,
    }


def desktop_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "podbox-herdremote" / "desktop.sock"


class DaemonClient(QtCore.QObject):
    """Newline-delimited JSON client; reconnects every two seconds."""

    state_changed = Signal(dict)
    log_line = Signal(str)
    connection_changed = Signal(bool)

    demo = False

    def __init__(self, path: Path | None = None, parent=None):
        super().__init__(parent)
        self.path = Path(path) if path else desktop_socket_path()
        self.state = empty_state()
        self.connected = False
        self._ids = 0
        self._pending: dict[int, object] = {}
        self._buffer = b""
        self.socket = QtNetwork.QLocalSocket(self)
        self.socket.connected.connect(self._on_connected)
        self.socket.disconnected.connect(self._on_disconnected)
        self.socket.readyRead.connect(self._on_ready)
        self.retry = QtCore.QTimer(self)
        self.retry.setInterval(2000)
        self.retry.timeout.connect(self.connect_to_daemon)

    def start(self) -> None:
        self.connect_to_daemon()
        self.retry.start()

    def stop(self) -> None:
        self.retry.stop()
        self.socket.abort()

    def connect_to_daemon(self) -> None:
        if self.socket.state() == QtNetwork.QLocalSocket.LocalSocketState.UnconnectedState:
            if self.path.exists():
                self.socket.connectToServer(str(self.path))

    def request(self, op: str, callback=None, **args) -> int:
        """callback(ok: bool, result_or_error) runs on the GUI thread."""
        if not self.connected:
            if callback is not None:
                QtCore.QTimer.singleShot(0, lambda: callback(False, "后台远程服务未运行。"))
            return 0
        self._ids += 1
        self._pending[self._ids] = callback
        message = {"id": self._ids, "op": op, **args}
        self.socket.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        self.socket.flush()
        return self._ids

    def _on_connected(self) -> None:
        self.connected = True
        self.retry.stop()
        self.connection_changed.emit(True)
        self.request("hello")
        self.request("subscribe", logs=True)

    def _on_disconnected(self) -> None:
        was = self.connected
        self.connected = False
        self._buffer = b""
        for callback in self._pending.values():
            if callback is not None:
                callback(False, "与后台远程服务的连接已断开。")
        self._pending.clear()
        if was:
            self.state = empty_state()
            self.connection_changed.emit(False)
            self.state_changed.emit(self.state)
        self.retry.start()

    def _on_ready(self) -> None:
        self._buffer += bytes(self.socket.readAll())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if line.strip():
                with suppress(ValueError, UnicodeDecodeError):
                    self.handle(json.loads(line.decode("utf-8")))

    def handle(self, message: dict) -> None:
        if not isinstance(message, dict):
            return
        event = message.get("event")
        if event == "state" and isinstance(message.get("state"), dict):
            merged = empty_state()
            merged.update(message["state"])
            self.state = merged
            self.state_changed.emit(merged)
        elif event == "log":
            self.log_line.emit(str(message.get("line", "")))
        elif "id" in message:
            callback = self._pending.pop(message.get("id"), None)
            if callback is not None:
                if message.get("ok"):
                    callback(True, message.get("result"))
                else:
                    callback(False, message.get("error") or "后台远程服务拒绝了这个操作。")


# ----------------------------------------------------------------------
# Interface 3: the input plugin host
# ----------------------------------------------------------------------
def plugins_config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "astrapod" / "plugins.json"


class PluginBridge(QtCore.QObject):
    """Hands PluginHost callbacks, from any thread, to the GUI thread."""

    plugins_changed = Signal()
    event = Signal(dict)

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.running = False
        self.error = None
        if host is not None:
            host.subscribe(self._forward)

    @classmethod
    def create(cls, parent=None):
        try:
            from astrapod_plugins.host import PluginHost
        except ImportError as error:
            bridge = cls(None, parent)
            bridge.error = f"插件宿主不可用：{error}"
            return bridge
        return cls(PluginHost(plugins_config_path()), parent)

    @property
    def available(self) -> bool:
        return self.host is not None

    def _forward(self, message) -> None:
        if isinstance(message, dict):
            self.event.emit(message)

    def list(self) -> list:
        if self.host is None:
            return []
        return list(self.host.list())

    def enable(self, plugin_id: str, on: bool) -> None:
        self.host.enable(plugin_id, on)
        self.plugins_changed.emit()

    def settings(self, plugin_id: str) -> dict:
        return dict(self.host.settings(plugin_id) or {})

    def set_settings(self, plugin_id: str, values: dict) -> None:
        self.host.set_settings(plugin_id, values)
        self.plugins_changed.emit()

    def start(self) -> None:
        if self.host is not None and not self.running:
            self.host.start()
            self.running = True
            self.plugins_changed.emit()

    def stop(self) -> None:
        if self.host is not None and self.running:
            self.running = False
            self.host.stop()


# ----------------------------------------------------------------------
# Import probing (ffprobe / ffmpeg)
# ----------------------------------------------------------------------
@dataclass
class ProbedFile:
    path: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    year: int = 0
    disc: int = 0
    track: int = 0
    length_ms: int = 0
    codec: str = ""
    sample_rate: int = 0
    bits: int = 0
    bitrate_kbps: int = 0
    size_bytes: int = 0
    cover: Path | None = None
    error: str = ""

    @property
    def supported(self) -> bool:
        return self.path.suffix.lower() in IMPORT_SUFFIXES

    @property
    def converts(self) -> bool:
        return self.sample_rate > 48000


IMPORT_SUFFIXES = {".flac", ".m4a", ".mp3"}
STOCK_VIDEO_SUFFIXES = {".m4v", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".mpg", ".mpeg"}
STOCK_PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
                        ".bmp", ".webp", ".gif"}
STOCK_ONLY_SUFFIXES = STOCK_VIDEO_SUFFIXES | STOCK_PHOTO_SUFFIXES | {".wav"}
STOCK_IMPORT_SUFFIXES = IMPORT_SUFFIXES | STOCK_ONLY_SUFFIXES


def _leading_int(value) -> int:
    text = str(value or "").strip()
    digits = ""
    for char in text:
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else 0


class FileProber:
    """Reads tags and a cover thumbnail with ffprobe and ffmpeg."""

    demo = False

    def __init__(self, thumbs: Path | None = None):
        self.thumbs = Path(thumbs) if thumbs else cache_root() / "import-thumbs"

    def probe(self, path: Path, *, stock=False) -> ProbedFile:
        import subprocess
        result = ProbedFile(Path(path))
        try:
            result.size_bytes = result.path.stat().st_size
        except OSError as error:
            result.error = str(error)
            return result
        suffixes = STOCK_IMPORT_SUFFIXES if stock else IMPORT_SUFFIXES
        if result.path.suffix.lower() not in suffixes:
            result.error = ("不支持此文件格式" if stock else
                            "只能导入 FLAC、M4A 和 MP3 文件")
            return result
        if result.path.suffix.lower() in STOCK_PHOTO_SUFFIXES:
            result.title, result.album = result.path.stem, result.path.parent.name
            return result
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_format", "-show_streams",
                 "-of", "json", str(result.path)],
                check=True, capture_output=True, text=True, timeout=20).stdout
            info = json.loads(out)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            result.error = f"无法读取标签：{error}"
            return result
        fmt = info.get("format", {})
        tags = {key.lower(): value for key, value in (fmt.get("tags") or {}).items()}
        if result.path.suffix.lower() in STOCK_VIDEO_SUFFIXES:
            video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
                          and not s.get("disposition", {}).get("attached_pic")), None)
            if video is None:
                result.error = "文件不含视频画面"
                return result
            result.title = tags.get("title") or result.path.stem
            result.album = tags.get("show", "")
            result.codec = video.get("codec_name", "")
            return result
        audio = next((s for s in info.get("streams", [])
                      if s.get("codec_type") == "audio"), {})
        for key, value in (audio.get("tags") or {}).items():
            tags.setdefault(key.lower(), value)
        result.title = tags.get("title", "") or result.path.stem
        result.artist = tags.get("artist", "")
        result.album = tags.get("album", "")
        result.album_artist = tags.get("album_artist", "") or tags.get("albumartist", "")
        result.genre = tags.get("genre", "")
        result.year = _leading_int(tags.get("date") or tags.get("year"))
        result.disc = _leading_int(tags.get("disc"))
        result.track = _leading_int(tags.get("track"))
        result.length_ms = int(float(fmt.get("duration") or 0) * 1000)
        result.codec = audio.get("codec_name", "")
        result.sample_rate = int(audio.get("sample_rate") or 0)
        result.bits = int(audio.get("bits_per_raw_sample") or 0)
        result.bitrate_kbps = int(int(fmt.get("bit_rate") or 0) / 1000)
        has_picture = any(s.get("disposition", {}).get("attached_pic")
                          for s in info.get("streams", []))
        if has_picture:
            result.cover = self._thumbnail(result.path)
        return result

    def _thumbnail(self, path: Path) -> Path | None:
        import subprocess
        stat = path.stat()
        digest = hashlib.sha1(f"{path}\0{stat.st_mtime_ns}\0{stat.st_size}".encode()).hexdigest()
        target = self.thumbs / f"{digest[:24]}.png"
        if target.exists():
            return target
        self.thumbs.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
                 "-map", "0:v:0", "-frames:v", "1",
                 "-vf", "scale=128:128:force_original_aspect_ratio=increase,crop=128:128",
                 str(target)], check=True, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        return target if target.exists() else None


def expand_paths(paths, *, stock=False) -> list[Path]:
    """Files as given, folders recursively, in a stable order."""
    found: list[Path] = []
    suffixes = STOCK_IMPORT_SUFFIXES if stock else IMPORT_SUFFIXES
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    candidate = Path(root) / name
                    if candidate.suffix.lower() in suffixes:
                        found.append(candidate)
        elif path.is_file():
            found.append(path)
    return found


def music_tags_for(title, artist, album, length_ms, disc, number):
    """hrmediasync.music_tags, the duplicate rule shared with sync."""
    import hrmediasync
    artists = [part for part in str(artist or "").replace("；", ";").split(";")]
    return hrmediasync.music_tags(title, artists, album, int(length_ms or 0),
                                  int(disc or 0), int(number or 0))
