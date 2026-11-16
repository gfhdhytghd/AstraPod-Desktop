"""Bounded, unprivileged Disk Mode flush/eject for the one PodBox FAT UUID."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import logging
import subprocess

LOG = logging.getLogger('podbox-usbkey')
UUID = Path('/dev/disk/by-uuid/8DD6-4F07')


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
    for command in ([['udisksctl', 'unmount', '-b', dev]] +
                    ([['eject', '-s', dev]] if eject else [])):
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
        self.next_poll = now + .1
        status = self.remote.disk_status(now)
        self.active = bool(status[1]) if status else bool(self.find_volume())
        if status and status[2]:
            request = int.from_bytes(status[4:8], 'little')
            if request != self.seen:
                if self.submit(False, request):
                    self.seen = request
        elif not self.active:
            self.seen = None

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
