"""Private desktop state and guarded writes to an already mounted iPod."""
from __future__ import annotations

from contextlib import contextmanager, suppress
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
from uuid import uuid4

MAX_FILE = 32 * 1024 * 1024


def digest(data):
    return sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def fingerprint(*parts):
    return digest(json.dumps(parts, ensure_ascii=False))


def secret(account, value=None):
    """Use the same freedesktop Secret Service tool as HerdRemote."""
    args = ["secret-tool", "lookup" if value is None else "store"]
    if value is not None:
        args += ["--label=AstraPod CalDAV"]
    try:
        result = subprocess.run(
            [*args, "service", "astrapod-caldav", "account", account],
            input=value if value is not None else "", text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("无法访问系统密钥环。请确认 Secret Service 和 secret-tool 可用。") from None
    if result.returncode:
        raise RuntimeError("无法读取或保存 CalDAV 密钥。请解锁系统密钥环。")
    return result.stdout.removesuffix("\n") if value is None else None


def _publish(directory, name, data, check=lambda: None, existing=True):
    temp = ".astrapod-" + uuid4().hex + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        check()
        if existing:
            os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            from hrdisksync import rename_new
            rename_new(directory, temp, name)
        os.fsync(directory)
    finally:
        with suppress(OSError):
            os.unlink(temp, dir_fd=directory)


class Store:
    def __init__(self, root=None):
        data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        self.root = Path(root) if root is not None else data / "astrapod" / "extras"

    def read(self, name, default):
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default

    def write(self, name, value):
        self.save(name, json.dumps(value, ensure_ascii=False).encode())

    def save(self, name, data):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            _publish(fd, name, data)
        finally:
            os.close(fd)

    @contextmanager
    def locked(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("另一个 AstraPod 窗口正在同步或编辑日程与联系人。") from None
            yield
        finally:
            os.close(fd)

    def conflict(self, scope, body, ext):
        data = body.encode() if isinstance(body, str) else body
        name = f"conflict-{fingerprint(scope, digest(data))[:24]}.{ext}"
        self.save(name, data)
        return name


class PlayerDisk:
    """Use MediaDisk's pinned root and mount identity checks for Extras files."""

    def __init__(self, disk):
        self.disk = disk

    def subfolders(self, parts):
        with self.disk.directory(parts) as directory:
            return [] if directory is None else sorted(
                name for name in os.listdir(directory) if not name.startswith(".")
                and stat.S_ISDIR(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode)
            )

    def names(self, parts, suffix):
        with self.disk.directory(parts) as directory:
            return [] if directory is None else sorted(
                name for name in os.listdir(directory)
                if not name.startswith(".") and name.lower().endswith(suffix)
                and not stat.S_ISDIR(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode)
            )

    def _read(self, directory, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_dev != os.fstat(self.disk.root).st_dev:
                raise ValueError("iPod 文件类型无效。")
            data = stream.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise ValueError("iPod 文件超过 32 MiB。")
            return data

    def read(self, path):
        with self.disk.directory(path[:-1]) as directory:
            data = None if directory is None else self._read(directory, path[-1])
            self.disk.check()
            return data

    def write(self, path, before, after):
        if before == after:
            return
        if len(after) > MAX_FILE:
            raise ValueError("iPod 文件超过 32 MiB。")
        with self.disk.directory(path[:-1], create=True) as directory:
            def check():
                self.disk.check()
                if self._read(directory, path[-1]) != before:
                    raise RuntimeError("iPod 文件在同步期间发生变化。请重试。")

            _publish(directory, path[-1], after, check, existing=before is not None)
            self.disk.check()
