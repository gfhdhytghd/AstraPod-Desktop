"""In-memory stand-ins for interfaces 1-3, Jellyfin and ffprobe.

Used by `--demo`, by the screenshot script and by the tests. Every fake has
the surface of its adapter in backends.py.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import math
from pathlib import Path
import random
import tempfile
import threading
import time

from . import contracts
from .backends import FallbackLibraryCache, ProbedFile, empty_state
from .qt import QtCore, QtGui, Signal

ARTISTS = [
    ("Harbor Lights", "Indie Pop", ["Salt & Signal", "Paper Lanterns"]),
    ("陈默", "华语流行", ["夜航", "城市的另一面", "未寄出的信"]),
    ("Neon Orchard", "Synthwave", ["Afterglow District", "Chrome Bloom"]),
    ("Mira Sol", "R&B", ["Velvet Hours"]),
    ("北岸乐队", "摇滚", ["潮汐", "无人区"]),
    ("Glass Cathedral", "Ambient", ["Choir of Rooms", "Low Light Hymns"]),
    ("Okara Quartet", "Classical", ["Nocturnes for Strings"]),
    ("苏晓", "民谣", ["山风与你", "旧车站"]),
    ("Velvet Static", "Shoegaze", ["Static Bloom"]),
    ("Lumen Drive", "Electronic", ["Night Grid", "Photon"]),
    ("夏日信号", "City Pop", ["海岸线 1986", "霓虹快递"]),
    ("Ambrose Field", "Jazz", ["Blue Hour Sessions", "Late Set"]),
]
COMPOSERS = ["A. Field", "陈默", "M. Sol", "K. Okara", "苏晓", "J. Reyes", ""]
WORDS = ["Tide", "Glass", "Ember", "Signal", "Harbor", "Orbit", "Velvet", "Lantern",
         "Morning", "Static", "Paper", "River", "Echo", "Neon", "Quiet", "Golden"]
CN_WORDS = ["夜", "海", "风", "光", "雨", "梦", "城", "路", "月", "星", "雾", "桥"]


def _album_colors(key: str) -> tuple[QtGui.QColor, QtGui.QColor, QtGui.QColor]:
    digest = hashlib.sha1(key.encode()).digest()
    hue = digest[0] / 255.0
    first = QtGui.QColor.fromHslF(hue, 0.38 + digest[1] / 1400, 0.36 + digest[2] / 1500)
    second = QtGui.QColor.fromHslF((hue + 0.06 + digest[3] / 1200) % 1.0, 0.5, 0.6)
    third = QtGui.QColor.fromHslF((hue + 0.5) % 1.0, 0.22, 0.88)
    return first, second, third


def generated_cover(key: str, size: int) -> QtGui.QImage:
    """A deterministic abstract cover; safe to call off the GUI thread."""
    first, second, third = _album_colors(key)
    digest = hashlib.sha1(key.encode()).digest()
    image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_RGB32)
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    gradient = QtGui.QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0, first)
    gradient.setColorAt(1, first.darker(170))
    painter.fillRect(0, 0, size, size, gradient)
    style = digest[4] % 4
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    if style == 0:
        painter.setBrush(second)
        r = size * (0.22 + digest[5] / 900)
        painter.drawEllipse(QtCore.QPointF(size * 0.62, size * 0.42), r, r)
        painter.setBrush(third)
        painter.drawRect(QtCore.QRectF(0, size * 0.72, size, size * 0.06))
    elif style == 1:
        for i in range(7):
            color = QtGui.QColor(second if i % 2 else third)
            color.setAlphaF(0.85 - i * 0.1)
            painter.setBrush(color)
            painter.drawRect(QtCore.QRectF(0, size * (0.18 + i * 0.1), size, size * 0.045))
    elif style == 2:
        pen = QtGui.QPen(third, size * 0.018)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        for i in range(9):
            r = size * (0.06 + i * 0.05)
            painter.drawEllipse(QtCore.QPointF(size * 0.5, size * 0.55), r, r)
    else:
        painter.setBrush(second)
        path = QtGui.QPainterPath()
        path.moveTo(0, size)
        for i in range(9):
            x = size * i / 8
            y = size * (0.55 + 0.18 * math.sin(i * 1.3 + digest[6]))
            path.lineTo(x, y)
        path.lineTo(size, size)
        path.closeSubpath()
        painter.drawPath(path)
        painter.setBrush(third)
        painter.drawEllipse(QtCore.QPointF(size * 0.28, size * 0.28), size * 0.08, size * 0.08)
    painter.end()
    return image


def image_to_artwork(image: QtGui.QImage) -> contracts.Artwork:
    rgb = image.convertToFormat(QtGui.QImage.Format.Format_RGB888)
    width, height = rgb.width(), rgb.height()
    stride = rgb.bytesPerLine()
    raw = bytes(rgb.constBits())[: stride * height]
    rows = b"".join(raw[y * stride: y * stride + width * 3] for y in range(height))
    return contracts.Artwork(width, height, rows)


def fake_tracks(count: int = 0, seed: int = 7) -> list[contracts.DeviceTrack]:
    rng = random.Random(seed)
    tracks: list[contracts.DeviceTrack] = []
    serial = 0

    def add(artist, genre, album, year, number, disc, title, managed):
        nonlocal serial
        length = rng.randint(150_000, 330_000)
        bitrate = rng.choice([256, 320, 320, 900, 1011, 1411])
        ext = "flac" if bitrate > 400 else rng.choice(["mp3", "m4a"])
        played = rng.random() < 0.7
        serial += 1
        folder = f"/Music/PodLink/{album}" if managed else f"/Music/{artist}/{album}"
        tracks.append(contracts.DeviceTrack(
            index=len(tracks), path=f"{folder}/{number:02d} {title}.{ext}",
            title=title, artist=artist, album=album, album_artist=artist,
            genre=genre, composer=rng.choice(COMPOSERS), grouping="", comment="",
            year=year, disc=disc, track=number, length_ms=length,
            bitrate_kbps=bitrate, play_count=rng.randint(1, 60) if played else 0,
            rating=rng.choice([0, 0, 4, 6, 8, 10]),
            play_time_ms=length * rng.randint(0, 40) if played else 0,
            last_played=rng.randint(1, 4000) if played else 0,
            mtime=1_700_000_000 + rng.randint(0, 60_000_000), managed=managed))

    for a, (artist, genre, albums) in enumerate(ARTISTS):
        for b, album in enumerate(albums):
            year = 1986 + (a * 3 + b * 5) % 38
            managed = (a + b) % 3 == 0
            for number in range(1, rng.randint(7, 13)):
                if any("一" <= ch <= "鿿" for ch in artist):
                    title = "".join(rng.sample(CN_WORDS, rng.randint(2, 4)))
                else:
                    title = " ".join(rng.sample(WORDS, rng.randint(1, 3)))
                add(artist, genre, album, year, number, 1, title, managed)
    while len(tracks) < count:
        artist = f"Artist {len(tracks) // 120:03d}"
        album = f"Album {len(tracks) // 12:04d}"
        add(artist, "Bulk", album, 2000 + len(tracks) % 25, len(tracks) % 12 + 1, 1,
            " ".join(rng.sample(WORDS, 2)), False)
    return tracks


def _no_art(album: str) -> bool:
    return hashlib.sha1(album.encode()).digest()[0] % 7 == 0


class FakeSession:
    def __init__(self, library: "FakeLibrary"):
        self.library = library
        self.identity = library.serial

    def summary(self):
        self._delay()
        return self.library.summary_value

    def tracks(self, progress=None, *, byte_progress=None):
        total = len(self.library.tracks_value)
        for done in range(0, total, max(1, total // 8)):
            self._delay(0.02)
            if progress:
                progress(done)
        if progress:
            progress(total)
        return list(self.library.tracks_value)

    def detail(self, track):
        self._delay()
        codec = track.path.rsplit(".", 1)[-1]
        return contracts.TrackDetail(
            size_bytes=track.length_ms * track.bitrate_kbps // 8,
            codec={"m4a": "aac", "mp3": "mp3", "flac": "flac"}.get(codec, codec),
            sample_rate=96000 if track.bitrate_kbps > 1200 else 44100,
            channels=2, has_art=not _no_art(track.album))

    def artwork(self, track, size="coverflow"):
        self._delay(0.01)
        if _no_art(track.album) and not self.library.all_art:
            return None
        pixels = {"list": 44, "coverflow": 128, "wps": 300}.get(size, 128)
        key = "\0".join(contracts.album_key(track))
        return image_to_artwork(generated_cover(key, pixels))

    def delete(self, track):
        self._delay()
        if not track.managed:
            raise RuntimeError("只能删除 /Music/PodLink/ 下的曲目")
        self.library.tracks_value = [t for t in self.library.tracks_value
                                     if t.index != track.index]
        self.library._bump()

    def import_files(self, paths, progress, skip_duplicates=True):
        stored = 0
        for path in paths:
            progress({"file": str(path), "state": "probe", "sent": 0, "total": 0, "error": None})
            total = 4_000_000
            for sent in range(0, total + 1, total // 5):
                self._delay(0.03)
                progress({"file": str(path), "state": "upload", "sent": sent,
                          "total": total, "error": None})
            progress({"file": str(path), "state": "done", "sent": total,
                      "total": total, "error": None})
            stored += 1
        return {"stored": stored}

    def _delay(self, seconds=0.05):
        if self.library.delay:
            time.sleep(seconds)


FAKE_PORT = "/dev/serial/by-id/usb-PodBox_AstraPod_iPod_000A27001F2C9D10-if00"


class FakeLibrary:
    demo = True

    def __init__(self, count: int = 0, delay: bool = True, all_art: bool = False):
        self.serial = "000A27001F2C9D10"
        self.tracks_value = fake_tracks(count)
        self.commit = 41
        self.delay = delay
        self.all_art = all_art
        self.borrow = False
        self.port = ""
        self.available = True
        self.cache_dir = Path(tempfile.mkdtemp(prefix="astrapod-demo-"))
        self.cache = FallbackLibraryCache(self.cache_dir)
        self.sessions = 0
        self.detected_port = FAKE_PORT

    def detect_port(self):
        return self.detected_port

    def _bump(self):
        self.commit += 1

    @property
    def summary_value(self):
        used = sum(t.length_ms * t.bitrate_kbps // 8 for t in self.tracks_value)
        total = 160_041_885_696
        return contracts.DeviceSummary(
            model="ipod6g", firmware="PodBox 2026.11 (62a9ad5)",
            total_bytes=total, free_bytes=total - used - 9_800_000_000,
            track_count=len(self.tracks_value), commit_id=self.commit,
            database_ready=True)

    @contextmanager
    def session(self):
        self.sessions += 1
        yield FakeSession(self)

    def remember(self, serial, summary):
        pass

    def last(self):
        return None

    def art_path(self, serial, track, size) -> Path:
        return Path(self.cache.art_path(serial, track, size))


@dataclass(frozen=True)
class FakeScope:
    kind: str
    label: str
    item_id: str | None = None


@dataclass(frozen=True)
class FakeSyncTrack:
    item_id: str
    label: str
    name: str


class FakePlan:
    def __init__(self, scope):
        self.scope = scope
        self.tracks = tuple(FakeSyncTrack(str(i), label, f"{i:064x}.flac")
                            for i, label in enumerate([
                                "Harbor Lights — Lantern Tide", "陈默 — 夜航",
                                "Neon Orchard — Afterglow", "Mira Sol — Velvet Hours",
                                "北岸乐队 — 潮汐", "Glass Cathedral — Room One",
                                "苏晓 — 山风与你", "Lumen Drive — Photon"]))
        self.additions = self.tracks[:5]
        self.originals = self.tracks[5:]
        self.removals = frozenset({"Velvet Static — Static Bloom.flac",
                                   "Okara Quartet — Nocturne No. 2.flac"})


class FakeJellyfinError(OSError):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.status = status


class FakeSync:
    demo = True

    def __init__(self, delay: float = 0.2, fail: Exception | None = None, session=None):
        self.delay = delay
        self.fail = fail
        self.borrow = False
        self.calls: list[tuple] = []
        self.source = object()
        self.stored_session = session
        self.reject_token = False

    def load_session(self):
        return dict(self.stored_session) if self.stored_session else None

    def save_session(self, record):
        self.stored_session = dict(record)

    def clear_session(self):
        self.stored_session = None

    def session_record(self, source, url, user_name=""):
        return {"url": url, "user_id": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
                "user_name": user_name or "demo", "token": "fake-session-token",
                "device_id": "fake-device"}

    def restore(self, record):
        self.calls.append(("restore", record["url"], record["token"]))
        time.sleep(self.delay)
        if self.reject_token:
            raise FakeJellyfinError("HTTP 401", 401)
        if self.fail is not None:
            raise self.fail
        return self.source, self.scopes()

    def _wait(self):
        time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail

    def scopes(self):
        return [FakeScope("all", "全部音乐"), FakeScope("library", "音乐", "lib1"),
                FakeScope("playlist", "通勤", "pl1"), FakeScope("playlist", "深夜驾驶", "pl2")]

    def connect_password(self, url, username, password):
        self.calls.append(("password", url, username, password))
        self._wait()
        return self.source, self.scopes()

    def connect_token(self, url, token):
        self.calls.append(("token", url, token))
        self._wait()
        return self.source, self.scopes()

    def preview(self, source, scope, port):
        self.calls.append(("preview", scope, port))
        self._wait()
        return FakePlan(scope)

    def apply(self, source, plan, port):
        self.calls.append(("apply", plan, port))
        self._wait()
        return len(plan.additions), len(plan.removals)


class FakeProber:
    demo = True

    def __init__(self):
        self.thumbs = Path(tempfile.mkdtemp(prefix="astrapod-thumbs-"))

    def probe(self, path):
        path = Path(path)
        time.sleep(0.01)
        digest = hashlib.sha1(str(path).encode()).digest()
        stem = path.stem
        artist, _, title = stem.partition(" - ")
        result = ProbedFile(path, title=title or stem, artist=artist if title else "",
                            album="Demo Imports", length_ms=180_000 + digest[0] * 600,
                            codec={".flac": "flac", ".mp3": "mp3", ".m4a": "aac"}.get(
                                path.suffix.lower(), ""),
                            sample_rate=96000 if digest[1] % 3 == 0 else 44100,
                            bits=24 if digest[1] % 3 == 0 else 16,
                            bitrate_kbps=900, size_bytes=20_000_000 + digest[2] * 10000,
                            track=digest[3] % 12 + 1)
        if not result.supported:
            result.error = "只能导入 FLAC、M4A 和 MP3 文件"
            return result
        target = self.thumbs / f"{digest.hex()[:16]}.png"
        generated_cover(result.artist + result.album + stem, 96).save(str(target))
        result.cover = target
        return result


def fake_state() -> dict:
    state = empty_state()
    now = time.time()
    state["link"] = {"connected": True, "port": "/dev/ttyACM0", "mode": "podlink",
                     "serial": "000A27001F2C9D10", "borrowed": False,
                     "firmware": "PodBox 2026.11"}
    state["herdr"] = {"ok": True, "workspaces": [
        {"id": "w1", "name": "podbox", "counts": {"working": 2, "waiting": 1, "done": 1, "idle": 0},
         "tabs": [
             {"id": "t1", "name": "frontend", "agents": [
                 {"pane": "p1", "name": "stream-d", "kind": "claude", "status": "working",
                  "activity": "正在渲染专辑网格截图", "since": now - 340},
                 {"pane": "p2", "name": "review", "kind": "codex", "status": "waiting",
                  "activity": "等待确认：是否删除旧测试？", "since": now - 75}]},
             {"id": "t2", "name": "firmware", "agents": [
                 {"pane": "p3", "name": "stream-a", "kind": "claude", "status": "working",
                  "activity": "编写 MS3 曲目遍历", "since": now - 1260},
                 {"pane": "p4", "name": "build", "kind": "shell", "status": "done",
                  "activity": "make -j16 完成", "since": now - 3000}]}]},
        {"id": "w2", "name": "jellyfin-sync", "counts": {"working": 0, "waiting": 0, "done": 2, "idle": 1},
         "tabs": [{"id": "t3", "name": "main", "agents": [
             {"pane": "p5", "name": "sync", "kind": "claude", "status": "done",
              "activity": "已同步 128 首", "since": now - 7200},
             {"pane": "p6", "name": "notes", "kind": "claude", "status": "idle",
              "activity": "", "since": now - 86000}]}]},
    ]}
    state["questions"] = [{
        "id": "q1", "pane": "p2", "workspace": "w1", "title": "review 需要确认",
        "text": "旧的 test_podlink_gui.py 覆盖的是单页窗口。要用新的多页测试替换它吗？",
        "options": [{"number": 1, "label": "替换"}, {"number": 2, "label": "保留两份"},
                    {"number": 3, "label": "先跳过"}],
        "multi": False, "dismissed": False}]
    state["usage"] = [
        {"provider": "Claude", "status": "ok",
         "five_hour": {"percent": 62, "resets_at": now + 2 * 3600 + 900},
         "weekly": {"percent": 38, "resets_at": now + 3 * 86400},
         "tokens_week": 18_400_000, "cost_usd": 41.27, "refreshing": False},
        {"provider": "Codex", "status": "ok",
         "five_hour": {"percent": 18, "resets_at": now + 4 * 3600},
         "weekly": {"percent": 71, "resets_at": now + 86400 + 7200},
         "tokens_week": 9_100_000, "cost_usd": 12.08, "refreshing": False},
        {"provider": "Gemini", "status": "stale",
         "five_hour": {"percent": None, "resets_at": None},
         "weekly": {"percent": 9, "resets_at": now + 5 * 86400},
         "tokens_week": 600_000, "cost_usd": None, "refreshing": False},
    ]
    state["voice"] = {"state": "idle", "method": "whisper", "focus": True,
                      "last_result": "把专辑网格的阴影再柔和一点"}
    state["t3"] = {"enabled": True, "connected": False, "server_version": None,
                   "error": None, "session_label": None,
                   "threads": [
                       {"id": "t3t1", "project": "t3p1", "title": "AstraPod 设置页",
                        "status": "running", "waiting": False, "model": "claude-opus-5-5",
                        "branch": "master", "link": "t3code://threads/demo/1"},
                       {"id": "t3t2", "project": "t3p1", "title": "固件 T3 列表",
                        "status": "idle", "waiting": True, "model": "gpt-5.5",
                        "branch": "agents-t3", "link": "t3code://threads/demo/2"}]}
    state["agents"] = {"control": "herdr"}
    state["input"] = {"ir_fallback": "ipod", "plugins": []}
    state["settings_revision"] = 3
    return state


class FakeAppearance(QtCore.QObject):
    """SystemAppearance without D-Bus or processes."""

    changed = Signal()

    def __init__(self, scheme: str | None = "light", accent: str | None = "#3584E4",
                 origin: str = "gsettings", parent=None):
        super().__init__(parent)
        self.scheme_value = scheme
        self.accent_value = accent
        self.origin = origin

    def scheme(self, _app):
        return self.scheme_value

    def accent(self, _palette=None):
        return (self.accent_value, self.origin) if self.accent_value else (None, "")

    def set(self, scheme=None, accent=None):
        """Change the desktop's values and announce it, as the portal would."""
        if scheme is not None:
            self.scheme_value = scheme
        if accent is not None:
            self.accent_value = accent
        self.changed.emit()

    def stop(self):
        pass


class FakeDaemon(QtCore.QObject):
    state_changed = Signal(dict)
    log_line = Signal(str)
    connection_changed = Signal(bool)

    demo = True

    def __init__(self, parent=None, tick: bool = True):
        super().__init__(parent)
        self.state = fake_state()
        self.connected = True
        self.requests: list[tuple[str, dict]] = []
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.tick = tick

    def start(self):
        if self.tick:
            self.timer.start()
        QtCore.QTimer.singleShot(0, self._emit)
        for line in ("herdremote: PodLink 已连接 /dev/ttyACM0",
                     "herdremote: herdr socket ok, 2 workspaces",
                     "herdremote: usage read from T3"):
            self.log_line.emit(line)

    def stop(self):
        self.timer.stop()

    def push(self, state: dict):
        self.state = state
        self._emit()

    def _emit(self):
        self.state_changed.emit(self.state)

    def _tick(self):
        self._emit()

    def request(self, op, callback=None, **args):
        self.requests.append((op, args))
        ok, result = True, None
        state = self.state
        if op == "answer" or op == "dismiss":
            state["questions"] = [q for q in state["questions"] if q["id"] != args.get("question")]
        elif op == "voice":
            action = args.get("action")
            state["voice"]["state"] = {"press": "recording", "release": "submitting",
                                       "cancel": "idle"}.get(action, "idle")
            if action == "release":
                QtCore.QTimer.singleShot(900, self._voice_done)
        elif op == "detail":
            result = {"pane": args.get("pane"), "age": 42, "task": "重写 AstraPod 桌面前端",
                      "now": "正在渲染专辑网格截图", "say": "深色主题的阴影还要再柔和一点。",
                      "steps": ["读取规范", "写页面", "截图检查"],
                      "input": "", "cursor": 0, "revision": "r12"}
        elif op == "t3_connect":
            state["t3"] = {"enabled": True, "connected": True, "server_version": "0.4.2",
                           "error": None, "session_label": "AstraPod 桌面（本机）"}
        elif op == "t3_disconnect":
            state["t3"] = {"enabled": True, "connected": False, "server_version": None,
                           "error": None, "session_label": None}
        elif op == "reload":
            import podlink_settings
            control = podlink_settings.load().get("AGENT_CONTROL")
            state["agents"] = {"control": control if control in ("herdr", "t3") else "herdr"}
        if callback is not None:
            QtCore.QTimer.singleShot(0, lambda: callback(ok, result))
        QtCore.QTimer.singleShot(0, self._emit)
        return len(self.requests)

    def _voice_done(self):
        self.state["voice"]["state"] = "idle"
        self.state["voice"]["last_result"] = "已提交语音输入"
        self._emit()



class FakePluginHost:
    """Pure-Python PluginHost with two plugins; no processes."""

    def __init__(self):
        self.callbacks = []
        self.running = False
        self.values = {
            "apple-ir": {"target": "ipod", "repeat_delay_ms": 350, "voice_on_hold": True,
                         "receiver": ""},
            "gamepad": {"deadzone": 0.25, "device": ""},
        }
        self.enabled = {"apple-ir": True, "gamepad": False}
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def _info(self, plugin_id):
        schema = {
            "apple-ir": [
                {"key": "target", "type": "enum", "label": "按键发送到",
                 "choices": ["ipod", "host"], "default": "ipod",
                 "description": "ipod 把遥控器按键转给 iPod；host 直接控制电脑。"},
                {"key": "repeat_delay_ms", "type": "int", "label": "长按重复延迟（毫秒）",
                 "default": 350, "min": 100, "max": 2000},
                {"key": "voice_on_hold", "type": "bool", "label": "长按菜单键开始语音输入",
                 "default": True},
                {"key": "receiver", "type": "string", "label": "接收器 USB ID",
                 "default": "", "placeholder": "留空自动识别，例如 05ac:8242"},
            ],
            "gamepad": [
                {"key": "deadzone", "type": "float", "label": "摇杆死区", "default": 0.25,
                 "min": 0.0, "max": 0.9},
                {"key": "device", "type": "string", "label": "设备路径", "default": ""},
            ],
        }[plugin_id]
        meta = {
            "apple-ir": ("Apple IR 遥控器", "1.0.0",
                         "把 Apple Remote 的红外按键转成 iPod 按键、电脑快捷键和语音输入。",
                         ["usb", "ep0", "keys", "voice"]),
            "gamepad": ("游戏手柄", "0.3.0", "用游戏手柄的方向键和按钮控制播放。",
                        ["usb", "keys"]),
        }[plugin_id]
        enabled = self.enabled[plugin_id]
        running = enabled and self.running
        last = next((e for e in reversed(self.events) if e.get("source") == plugin_id), None)
        return contracts.PluginInfo(
            id=plugin_id, name=meta[0], version=meta[1], description=meta[2],
            enabled=enabled, state="running" if running else ("waiting" if enabled else "disabled"),
            device="Apple IR Receiver (05ac:8242)" if plugin_id == "apple-ir" and running else None,
            detail="接收器就绪" if running else ("等待设备" if enabled else "已停用"),
            permissions=meta[3], settings_schema=schema, last_event=last)

    def list(self):
        return [self._info(pid) for pid in ("apple-ir", "gamepad")]

    def enable(self, plugin_id, on):
        self.enabled[plugin_id] = bool(on)
        self._status(plugin_id)

    def settings(self, plugin_id):
        return dict(self.values[plugin_id])

    def set_settings(self, plugin_id, values):
        self.values[plugin_id].update(values)

    def subscribe(self, callback):
        self.callbacks.append(callback)

    def start(self):
        self.running = True
        for pid in self.enabled:
            self._status(pid)

    def stop(self):
        self.running = False

    def _status(self, plugin_id):
        info = self._info(plugin_id)
        self._send({"type": "status", "source": plugin_id, "state": info.state,
                    "device": info.device, "detail": info.detail})

    def emit_input(self, key, phase="tap", source="apple-ir"):
        event = {"type": "input", "key": key, "phase": phase, "source": source,
                 "t": time.monotonic()}
        with self._lock:
            self.events.append(event)
        self._send(event)

    def _send(self, message):
        for callback in list(self.callbacks):
            callback(message)
