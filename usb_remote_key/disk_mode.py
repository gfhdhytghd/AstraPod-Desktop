"""Bounded, unprivileged Disk Mode flush/eject for the one PodBox FAT UUID."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import logging
import re
import subprocess

LOG = logging.getLogger('podbox-usbkey')
UUID = Path('/dev/disk/by-uuid/8DD6-4F07')
UDISKS = 'org.freedesktop.UDisks2'


def eject_drive(dev, run):
    """Ask udisksd to eject as its service user; never open the disk here."""
    name = Path(dev).name
    # UDisks object components escape every non-alphanumeric byte as _xx.
    component = ''.join(c if c.isascii() and c.isalnum() else
                        f'_{ord(c):02x}' for c in name)
    command = ['gdbus', 'call', '--system', '--dest', UDISKS,
               '--object-path', f'/org/freedesktop/UDisks2/block_devices/{component}',
               '--method', 'org.freedesktop.DBus.Properties.Get', '--timeout', '2',
               UDISKS + '.Block', 'Drive']
    result = run(command, capture_output=True, text=True, timeout=3, check=False)
    if result.returncode:
        LOG.warning('Disk Mode drive lookup failed: %s; no force', result.stderr.strip())
        return False
    match = re.fullmatch(r"\(<objectpath '(/org/freedesktop/UDisks2/drives/[A-Za-z0-9_]+)'>,\)\s*",
                         result.stdout)
    if not match:
        LOG.warning('Disk Mode drive lookup returned no valid Drive; no force')
        return False
    # No polkit password prompt. A denied/inactive session fails closed.
    command = ['gdbus', 'call', '--system', '--dest', UDISKS,
               '--object-path', match[1], '--method', UDISKS + '.Drive.Eject',
               '--timeout', '12', "{'auth.no_user_interaction': <true>}"]
    result = run(command, capture_output=True, text=True, timeout=13, check=False)
    if result.returncode:
        LOG.warning('Disk Mode Drive.Eject failed: %s; no force', result.stderr.strip())
        return False
    return True


def volume():
    """Verify the UUID belongs to this USB player, never an arbitrary disk."""
    try:
        block = UUID.resolve(strict=True)
        sysdev = (Path('/sys/class/block') / block.name).resolve(strict=True)
        for parent in sysdev.parents:
            if (parent / 'idVendor').exists():
                if ((parent / 'idVendor').read_text().strip() == '05ac' and
                    (parent / 'idProduct').read_text().strip() == '1261' and
                    (parent / 'serial').read_text().strip() == '1AA0000000000360'):
                    return str(block)
                break
    except OSError:
        pass
    return None


def clean(eject, find=volume, run=subprocess.run):
    dev = find()
    if dev is None:
        LOG.warning('Disk Mode volume unavailable or identity mismatch')
        return False
    for command in [['udisksctl', 'unmount', '-b', dev]]:
        try:
            result = run(command, capture_output=True, text=True, timeout=2, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            LOG.warning('Disk Mode %s failed: %s; no force', ' '.join(command[:2]), error)
            return False
        if result.returncode:
            # Already unmounted is safe, but a busy mount is never forced.
            if command[1] == 'unmount' and 'NotMounted' in result.stderr:
                continue
            LOG.warning('Disk Mode %s failed: %s; no force', ' '.join(command[:2]), result.stderr.strip())
            return False
    if eject:
        try:
            if not eject_drive(dev, run):
                return False
        except (OSError, subprocess.TimeoutExpired) as error:
            LOG.warning('Disk Mode Drive.Eject failed: %s; no force', error)
            return False
    LOG.info('Disk Mode %s complete', 'eject' if eject else 'unmount')
    return True


class DiskMode:
    def __init__(self, remote, *, find=None):
        self.remote = remote
        self.find_volume = find if find is not None else volume
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='disk-eject')
        self.job = None
        self.request = None
        self.seen = None
        self.next_poll = 0.0
        self.down = self.fired = False
        self.first = self.last = 0.0
        self.active = False

    def frame(self, frame, now, span_seconds=0.0):
        if self.down and now - self.last >= .150:
            self.down = self.fired = False
        if frame is None:
            return
        menu = (len(frame) == 4 and frame[:2] == b'\xee\x87' and frame[2] in (2, 3))
        if menu or (frame == b'' and self.down):
            if not self.down:
                self.first = now - span_seconds
                self.down = True
            self.last = now
            if not self.fired and now - self.first >= 1.0 and self.active:
                self.fired = True
                self.submit(True, None)
        elif frame:
            self.down = self.fired = False

    def submit(self, eject, request):
        if self.job is None:
            self.request = request
            self.job = self.pool.submit(clean, eject)
            return True
        return False

    def poll(self, now):
        if self.job is not None and self.job.done():
            ok = self.job.result()
            if ok and self.request is not None:
                self.remote.disk_ack(self.request)
            self.job = self.request = None
        if now < self.next_poll:
            return
        # EP0 status only while the Disk Mode volume exists: outside Disk Mode
        # a 10 Hz vendor request competes with PodLink bulk traffic for nothing.
        if not self.find_volume():
            self.next_poll = now + .5
            self.active = False
            self.seen = None
            return
        self.next_poll = now + .1
        status = self.remote.disk_status(now)
        self.active = bool(status[1]) if status else True
        if status and status[2]:
            request = int.from_bytes(status[4:8], 'little')
            if request != self.seen:
                if self.submit(False, request):
                    self.seen = request
        elif not self.active:
            self.seen = None

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
