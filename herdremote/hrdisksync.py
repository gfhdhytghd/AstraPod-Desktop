"""Linux USB mass-storage transport for PodLink-managed media."""
from contextlib import ExitStack, contextmanager, suppress
import ctypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from tempfile import TemporaryDirectory
from uuid import uuid4

import hrdisk
import hrmediasync
import podlink
from podlink_disk import request_disk
from usb_remote_key.disk_mode import eject_drive

UUIDS = Path("/dev/disk/by-uuid")
SYS_TTY = Path("/sys/class/tty")
SYS_BLOCK = Path("/sys/class/block")
MOUNTINFO = Path("/proc/self/mountinfo")
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
IO_SIZE = 1024 * 1024
WAIT_SECONDS = 25


def usb_identity(sys_device):
    """Return the USB device's sysfs path and profile serial."""
    try:
        parent = sys_device.resolve(strict=True)
        for usb in parent.parents:
            if (usb / "idVendor").exists():
                vid = int((usb / "idVendor").read_text().strip(), 16)
                pid = int((usb / "idProduct").read_text().strip(), 16)
                if (vid, pid) == (podlink.VID, podlink.PID):
                    return usb, (usb / "serial").read_text().strip()
                break
    except (OSError, ValueError):
        pass
    return None


def selected_player(port):
    from serial.tools import list_ports

    tty = Path(port or podlink.find_port(list_ports.comports())).resolve(strict=True)
    device = usb_identity(SYS_TTY / tty.name)
    if device is None or not re.fullmatch(r"2[0-9A-Fa-f]+", device[1]):
        raise RuntimeError("磁盘模式要求所选 iPod 已连接 PodLink USB。")
    return tty, device


def run_host(*args, timeout=10):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{args[0]}: timed out") from error
    if result.returncode:
        raise RuntimeError(f"{args[0]}: {result.stderr.strip() or result.returncode}")
    return result.stdout


@dataclass(frozen=True)
class Volume:
    uuid_path: Path
    block: Path
    number: str
    usb: tuple

    def present(self):
        try:
            return (self.uuid_path.resolve(strict=True) == self.block
                    and usb_identity(SYS_BLOCK / self.block.name) == self.usb
                    and (SYS_BLOCK / self.block.name / "dev").read_text().strip() == self.number)
        except OSError:
            return False

    def mounts(self):
        mounts = []
        for line in MOUNTINFO.read_text().splitlines():
            before, _, after = line.partition(" - ")
            fields = before.split()
            if fields[2] == self.number:
                path = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
                mounts.append((fields[0], Path(path), fields[3], after.split()[0]))
        return mounts

    def mount(self):
        if not self.present():
            raise RuntimeError("iPod 磁盘已断开。")
        if not self.mounts():
            try:
                run_host("udisksctl", "mount", "--no-user-interaction", "-b", str(self.block))
            except RuntimeError:
                # The desktop automounter may have mounted it first.
                if not self.mounts():
                    raise
        mounts = self.mounts()
        if len(mounts) != 1 or mounts[0][2:] != ("/", "vfat"):
            raise RuntimeError("找不到唯一的 iPod FAT 根挂载点。")
        return mounts[0]

    def eject(self):
        if not self.present():
            if self.mounts():
                raise RuntimeError("iPod 磁盘已断开，但系统仍报告挂载。")
            return
        if self.mounts():
            try:
                run_host("udisksctl", "unmount", "--no-user-interaction", "-b", str(self.block))
            except RuntimeError:
                if self.mounts():
                    raise
        if self.mounts():
            raise RuntimeError("iPod 磁盘仍有挂载点。")
        if self.present() and not eject_drive(str(self.block), subprocess.run):
            raise RuntimeError("UDisks 无法弹出 iPod。")


def find_volume(device):
    usb_path, serial = device
    expected = usb_path, "1" + serial[1:]
    found = {}
    for uuid_path in UUIDS.glob("*"):
        try:
            block = uuid_path.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            continue
        sys_block = SYS_BLOCK / block.name
        if usb_identity(sys_block) != expected:
            continue
        try:
            fs = run_host("lsblk", "--nodeps", "--noheadings", "--output", "FSTYPE",
                          str(block), timeout=2).strip()
            if fs == "vfat":
                number = (sys_block / "dev").read_text().strip()
                found[block] = Volume(uuid_path, block, number, expected)
        except (OSError, RuntimeError):
            if usb_identity(sys_block) == expected:
                raise
    if len(found) > 1:
        raise RuntimeError("所选 iPod 有多个 FAT 卷，无法确定音乐资料库的位置。")
    return next(iter(found.values()), None)


def wait_for(probe, timeout, message):
    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError(message)
        time.sleep(0.15)


def enter_disk(tty, device, socket_path):
    if find_volume(device) is not None:
        raise RuntimeError("所选 iPod 已进入磁盘模式。请先安全弹出。")

    hello_deadline = None

    def send():
        nonlocal hello_deadline
        if usb_identity(SYS_TTY / tty.name) != device:
            raise RuntimeError("所选 iPod 的 PodLink 连接已更改。")
        request = hrdisk.ENTER_DISK + b"\t" + os.fsencode(tty) + b"\n"
        reply = request_disk(socket_path, 2, request)
        if reply == b"3 no PodLink session\n":
            return None  # The preflight serial lease has just closed.
        if reply == b"4 firmware lacks disk-mode-v1 hello\n":
            # The daemon accepts socket requests before its new link has
            # received DH. This reply means it has not sent DK.
            if hello_deadline is None:
                hello_deadline = time.monotonic() + 2
            if time.monotonic() < hello_deadline:
                return None
        if not reply.endswith(b"\n"):
            raise ConnectionResetError("磁盘模式请求未确认。")
        if reply != b"0 disk request sent\n":
            raise RuntimeError(reply.decode("utf-8", "replace").strip())
        return True

    try:
        wait_for(send, WAIT_SECONDS, "HerdRemote 未恢复 PodLink 连接。")
    except (TimeoutError, ConnectionResetError):
        # The daemon may have sent DK before its socket reply was lost.
        # Wait for the same player's volume below without sending DK again.
        pass
    return wait_for(lambda: find_volume(device), WAIT_SECONDS,
                    "等待 iPod FAT 卷超时。如果 iPod 显示 USB 画面，请安全弹出后重试。")


def refresh_after_eject(device, serial_access):
    usb_path, serial = device

    def refresh():
        from serial.tools import list_ports

        for port in list_ports.comports():
            tty = Path(port.device).resolve()
            if usb_identity(SYS_TTY / tty.name) != (usb_path, serial):
                continue
            try:
                with serial_access(), hrmediasync.connect_device(str(tty)) as (link, identity):
                    if identity != serial or usb_identity(SYS_TTY / tty.name) != device:
                        raise RuntimeError("返回 PodLink 的 iPod 与同步目标不一致。")
                    podlink.refresh_media(link)
                    return True
            except (OSError, RuntimeError):
                return None
        return None

    wait_for(refresh, WAIT_SECONDS, "iPod 未确认资料库更新。请在 iPod 上手动更新资料库。")


def rename_new(directory, source, name):
    # Linux vfat supports RENAME_NOREPLACE. A conflict must never overwrite
    # another copy (including a case-insensitive FAT name collision).
    renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                          ctypes.c_uint)
    if renameat2(directory, os.fsencode(source), directory, os.fsencode(name), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


class MediaDisk:
    def __init__(self, root, check):
        self.root = root
        self.check = check
        self.dirty = False

    @contextmanager
    def folder(self, *, jellyfin=False, create=False):
        self.check()
        parts = ("Music", "PodLink", "Jellyfin") if jellyfin else ("Music", "PodLink")
        with ExitStack() as stack:
            directory = self.root
            for part in parts:
                try:
                    child = os.open(part, DIR_FLAGS, dir_fd=directory)
                except FileNotFoundError:
                    if not create:
                        yield None
                        return
                    os.mkdir(part, dir_fd=directory)
                    os.fsync(directory)
                    child = os.open(part, DIR_FLAGS, dir_fd=directory)
                stack.callback(os.close, child)
                if os.fstat(child).st_dev != os.fstat(self.root).st_dev:
                    raise RuntimeError("iPod 音乐目录跨越文件系统。")
                directory = child
            yield directory

    def inventory(self, identity):
        with self.folder(jellyfin=True) as directory:
            if directory is None:
                return frozenset()
            return frozenset(name for name in os.listdir(directory)
                             if hrmediasync.owned_name(name, identity)
                             and stat.S_ISREG(os.stat(name, dir_fd=directory,
                                                     follow_symlinks=False).st_mode))

    def publish(self, prepared, name=None, report=lambda _sent: None):
        jellyfin = name is not None
        suffix = prepared.upload.suffix.lower()
        if suffix not in hrmediasync.AUDIO_SUFFIXES or (
                jellyfin and (not hrmediasync.SYNC_NAME.fullmatch(name)
                             or not name.endswith(suffix))):
            raise ValueError("无效的 iPod 音乐文件名。")

        total = prepared.upload.stat().st_size
        if not 0 < total <= podlink.MEDIA_MAX_BYTES:
            raise ValueError("音频文件大小必须在 1 字节到 128 MiB 之间。")
        with self.folder(jellyfin=jellyfin, create=True) as directory:
            temp = f".astrapod-{uuid4().hex}.part"
            fd = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "w+b") as dest, prepared.upload.open("rb") as source:
                    digest = hashlib.sha256()
                    sent = 0
                    while chunk := source.read(IO_SIZE):
                        sent += len(chunk)
                        if sent > total:
                            raise ValueError("复制期间，源文件大小发生变化。")
                        digest.update(chunk)
                        dest.write(chunk)
                        report(sent)
                    dest.flush()
                    os.fsync(dest.fileno())
                    dest.seek(0)
                    received = hashlib.sha256()
                    while chunk := dest.read(IO_SIZE):
                        received.update(chunk)
                    if (sent != total or os.fstat(dest.fileno()).st_size != total
                            or received.digest() != digest.digest()):
                        raise RuntimeError("iPod 文件校验失败。")
                self.check()
                name = name or digest.hexdigest() + suffix
                rename_new(directory, temp, name)
                self.dirty = True
                os.fsync(directory)
                self.check()
            except BaseException:
                # If the device vanished, its mount can reject cleanup too.
                # Preserve the original write or disconnect error.
                with suppress(OSError):
                    os.unlink(temp, dir_fd=directory)
                raise
        folder = "/Music/PodLink/Jellyfin" if jellyfin else "/Music/PodLink"
        return f"{folder}/{name}"

    def remove(self, names, identity):
        with self.folder(jellyfin=True) as directory:
            for name in sorted(names):
                self.check()
                if (not hrmediasync.owned_name(name, identity) or directory is None
                        or not stat.S_ISREG(os.stat(name, dir_fd=directory,
                                                  follow_symlinks=False).st_mode)):
                    raise RuntimeError("仅可删除此 Jellyfin 账号的同步副本。")
                os.unlink(name, dir_fd=directory)
                self.dirty = True
                os.fsync(directory)
                self.check()


@contextmanager
def disk_session(player, serial_access, *, socket_path=None):
    tty, device = player
    for tool in ("udisksctl", "gdbus", "lsblk"):
        if not shutil.which(tool):
            raise RuntimeError(f"磁盘模式需要 {tool}。")
    volume = enter_disk(tty, device, socket_path or hrdisk.default_socket())
    disk = None
    failure = None
    try:
        mount = volume.mount()

        def check():
            if not volume.present() or volume.mounts() != [mount]:
                raise RuntimeError("iPod 失去挂载，磁盘操作已停止。")

        root = os.open(mount[1], DIR_FLAGS)
        try:
            check()
            st_dev = os.fstat(root).st_dev
            if f"{os.major(st_dev)}:{os.minor(st_dev)}" != volume.number:
                raise RuntimeError("iPod FAT 挂载点已更改。")
            disk = MediaDisk(root, check)
            yield disk
        finally:
            os.close(root)
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            volume.eject()
            if disk is not None and disk.dirty:
                refresh_after_eject(device, serial_access)
            elif volume.present():
                wait_for(lambda: True if not volume.present() else None, WAIT_SECONDS,
                         "iPod 弹出后未退出磁盘模式。")
        except (OSError, RuntimeError, subprocess.SubprocessError) as cleanup:
            detail = f"磁盘模式收尾失败：{cleanup}"
            if failure is not None:
                detail = f"{failure}。{detail}"
            raise RuntimeError(detail) from cleanup


def apply(source, plan, port, *, serial_access=hrmediasync.borrow_serial):
    hrmediasync.validate_source(source, plan)
    player = selected_player(port)
    if player[1][1] != plan.device:
        raise RuntimeError("iPod changed after preview; build a new preview")
    with serial_access(), hrmediasync.connect_device(str(player[0])) as (link, serial):
        hrmediasync.validate_device(link, serial, plan)

    added = set()
    with disk_session(player, serial_access) as disk:
        if disk.inventory(plan.identity) != plan.owned:
            raise RuntimeError("iPod changed after preview; build a new preview")
        for track in plan.additions:
            with TemporaryDirectory(prefix="podlink-jellyfin-") as tmp:
                prepared = hrmediasync.prepare_track(source, track, Path(tmp))
                disk.publish(prepared, track.name)
                added.add(track.name)
        if plan.removals:
            if source.tracks(plan.scope) != plan.tracks:
                raise RuntimeError("Jellyfin changed during sync; deletion was skipped")
            if disk.inventory(plan.identity) != plan.owned | added:
                raise RuntimeError("iPod changed during sync; deletion was skipped")
            disk.remove(plan.removals, plan.identity)
    return len(added), len(plan.removals)


def import_files(port, paths, progress, skip_duplicates, *, serial_access=hrmediasync.borrow_serial):
    import hrlibrary

    player = selected_player(port)
    index = None
    if skip_duplicates:
        with serial_access(), hrmediasync.connect_device(str(player[0])) as (link, serial):
            if serial != player[1][1]:
                raise RuntimeError("所选 iPod 的 PodLink 连接已更改。")
            index = hrlibrary.import_tag_index(link)

    with disk_session(player, serial_access) as disk:
        def publish(prepared, report):
            return dict(source=str(prepared.source), device=disk.publish(prepared, report=report),
                        source_sample_rate_hz=prepared.source_rate,
                        sample_rate_hz=prepared.upload_rate)

        return hrlibrary.import_batch(paths, progress, skip_duplicates, lambda: index, publish)
