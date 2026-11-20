"""Desktop settings shared by the GUI and the HerdRemote daemon."""
import json
import os
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile

FIELDS = {
    "JELLYFIN_URL": ("Jellyfin HTTPS 地址", ""),
    "CIDER_API_URL": ("Cider RPC 地址", "http://127.0.0.1:10767"),
    "CIDER_BACKEND": ("Cider 后端（auto、plugin、rpc）", "auto"),
    "RUFIN_API_URL": ("Rufin 地址（可留空自动识别）", ""),
    "HERDR_SOCKET_PATH": ("Agent 的 herdr 套接字", ""),
    "HERDREMOTE_VOICE_FOCUS": ("语音输入聚焦（on 或 off）", "off"),
    "HERDREMOTE_USAGE_CACHE_DIR": ("Usage 缓存目录", ""),
    "HERDREMOTE_KEY_TOOL": ("Usage 按键工具（auto、ydotool、wtype）", "auto"),
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
    path = settings_path().with_name("cider-rpc-token")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 513):
            raise ValueError("invalid Cider token file")
        token = file.read(514).decode("ascii").strip()
        return token if 0 < len(token) <= 512 else None
