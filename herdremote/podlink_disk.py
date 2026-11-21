#!/usr/bin/env python3
"""Ask running HerdRemote to enter Disk Mode, then wait for the FAT UUID.

Exit: 0 volume present, 2 timeout, 3 daemon/session unavailable,
4 unsupported firmware, 5 rejected/invalid response. Never opens a serial port.
"""
import argparse
import math
import os
import socket
import sys
import time

import hrdisk

UUID_PATH = '/dev/disk/by-uuid/8DD6-4F07'


def request_disk(path, timeout, request=hrdisk.ENTER_DISK + b'\n'):
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(timeout)
        channel.connect(path)
        channel.sendall(request)
        reply = b''
        while b'\n' not in reply and len(reply) < 256:
            part = channel.recv(256 - len(reply))
            if not part:
                break
            reply += part
    return reply


def enter(path, timeout, present=lambda: os.path.exists(UUID_PATH)):
    if present():
        print('Disk Mode volume already present: ' + UUID_PATH)
        return 0
    deadline = time.monotonic() + timeout
    try:
        reply = request_disk(path, min(timeout, 2.0))
    except OSError as error:
        print('HerdRemote unavailable: ' + str(error), file=sys.stderr)
        return 3
    if reply != b'0 disk request sent\n':
        print(reply.decode('utf-8', errors='replace').strip(), file=sys.stderr)
        return int(reply[:1]) if reply[:1] in (b'3', b'4', b'5') else 5
    while time.monotonic() < deadline:
        if present():
            print('Disk Mode volume present: ' + UUID_PATH)
            return 0
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    print('Timed out waiting for FAT UUID 8DD6-4F07', file=sys.stderr)
    return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-socket', default=hrdisk.default_socket())
    parser.add_argument('--timeout', type=float, default=20.0)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be finite and positive')
    return enter(args.control_socket, args.timeout)


if __name__ == '__main__':
    sys.exit(main())
