"""Desktop settings shared by the GUI and the HerdRemote daemon."""
import json
import os
import re
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

FIELDS = {
    "JELLYFIN_URL": ("Jellyfin HTTPS 地址", ""),
    "CIDER_API_URL": ("Cider RPC 地址", "http://127.0.0.1:10767"),
    "CIDER_BACKEND": ("Cider 后端（plugin、auto、rpc）", "plugin"),
    "RUFIN_API_URL": ("Rufin 地址（可留空自动识别）", ""),
    "HERDR_SOCKET_PATH": ("Agent 的 herdr 套接字", ""),
    "HERDREMOTE_VOICE_FOCUS": ("语音输入聚焦（on 或 off）", "off"),
    "HERDREMOTE_USAGE_CACHE_DIR": ("Usage 缓存目录", ""),
    "HERDREMOTE_KEY_TOOL": ("Usage 按键工具（auto、ydotool、wtype）", "auto"),
    "AGENT_CONTROL": ("iPod Agent 控制（herdr 或 t3）", "herdr"),
    "T3_FOLLOW_IPOD": ("T3 跟随 iPod（on 或 off）", "on"),
    "T3_BIN": ("T3 命令路径", ""),
    "AGENT_QUICK_REPLIES": ("Agent 快捷回复（每行一条）", "Continue\nYes\nNo\n/compact"),
}


def settings_path():
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "podbox-herdremote" / "desktop.json"


def load():
    try:
        values = json.loads(settings_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(values, dict):
        raise ValueError("desktop settings must contain a JSON object")
    return {key: values[key] for key in FIELDS if isinstance(values.get(key), str)
            and values[key]}


def write_private(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(value)
            file.write("\n")
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def save(values):
    payload = {key: values[key] for key in FIELDS if values.get(key)}
    write_private(settings_path(), json.dumps(payload, ensure_ascii=False, indent=2))


def save_player_tokens(cider, rufin):
    tokens = {"cider-rpc-token": cider, "rufin-token": rufin}
    if any(len(value) > 512 or any(ord(char) < 33 or ord(char) > 126 for char in value)
           for value in tokens.values()):
        raise ValueError("invalid player token")
    for filename, token in tokens.items():
        if token:
            write_private(settings_path().with_name(filename), token)


def cider_token():
    return read_private_token("cider-rpc-token")


def read_private_token(filename):
    path = settings_path().with_name(filename)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 513):
            raise ValueError("invalid private token file")
        token = file.read(514).decode("ascii").strip()
        return token if 0 < len(token) <= 512 else None


JELLYFIN_SESSION = "jellyfin-session.json"


def _jellyfin_session(value):
    keys = ("url", "user_id", "user_name", "token", "device_id")
    if not isinstance(value, dict) or any(
            not isinstance(value.get(key), str) or not value[key].strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value[key])
            for key in keys):
        raise ValueError("invalid Jellyfin session")
    session = {key: value[key] for key in keys}
    if not re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", session["token"]):
        raise ValueError("invalid Jellyfin access token")
    url = session["url"]
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.query or parts.fragment or "\\" in url or any(c.isspace() for c in url)):
        raise ValueError("invalid Jellyfin session URL")
    _ = parts.port
    if "source" in value:
        source = value["source"]
        if not isinstance(source, dict) or "id" not in source:
            raise ValueError("invalid Jellyfin source")
        kind, identity = source.get("kind"), source["id"]
        if not ((kind == "all" and identity is None) or
                (kind in ("library", "playlist") and isinstance(identity, str)
                 and identity.strip())):
            raise ValueError("invalid Jellyfin source")
        session["source"] = {"kind": kind, "id": identity}
    return session


def load_jellyfin_session() -> dict | None:
    path = settings_path().with_name(JELLYFIN_SESSION)
    try:
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                return None
            return _jellyfin_session(json.load(file))
    except (OSError, ValueError):
        return None


def save_jellyfin_session(session: dict) -> None:
    value = json.dumps(_jellyfin_session(session), ensure_ascii=False)
    path = settings_path().with_name(JELLYFIN_SESSION)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    write_private(path, value)


def clear_jellyfin_session() -> None:
    settings_path().with_name(JELLYFIN_SESSION).unlink(missing_ok=True)


def ipod_ports(comports=None, by_id: Path = Path("/dev/serial/by-id")) -> list[dict]:
    """Every attached PodLink tty, sorted by device: {"port", "serial",
    "product"}, where port is the /dev/serial/by-id path when one exists."""
    import podlink

    if comports is None:
        from serial.tools import list_ports
        try:
            comports = list_ports.comports()
        except OSError:
            return []
    links = {}
    try:
        for candidate in sorted(by_id.glob("*")):
            if candidate.is_symlink():
                links.setdefault(os.path.realpath(candidate), str(candidate))
    except OSError:
        pass
    found = []
    for info in sorted(comports, key=lambda info: info.device):
        if info.vid == podlink.VID and info.pid == podlink.PID:
            found.append({"port": links.get(os.path.realpath(info.device), info.device),
                          "serial": getattr(info, "serial_number", None) or "",
                          "product": getattr(info, "product", None) or ""})
    return found


def detect_ipod_port() -> str | None:
    """The one attached iPod's port, or None when there is none or several."""
    ports = ipod_ports()
    return ports[0]["port"] if len(ports) == 1 else None
