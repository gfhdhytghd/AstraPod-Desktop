#!/usr/bin/env python3
"""Run the AstraPod plugin smoke suite on an explicitly selected iPod."""
import argparse
from contextlib import suppress
from dataclasses import asdict
import json
from pathlib import Path
import struct
import sys
import time

import usb.core

import podlink_shell as shell
from usb_remote_key import usb_remote_key as remote


CASES = (
    ("snow", (remote.LEFT, remote.RIGHT), False),
    ("stopwatch", (remote.SELECT, remote.RIGHT), True),
    ("dice", (remote.SELECT, remote.SELECT), True),
    ("rb_info", (remote.SELECT,), True),
    ("maze", (remote.SELECT, remote.RIGHT), True),
    ("rockblox1d", (remote.PLAY, remote.PLAY), False),
    ("oscilloscope", (remote.RIGHT, remote.RIGHT), False),
)


def tap(player, key):
    player.ctrl_transfer(0x40, remote.REQUEST, key, 1, b"", timeout=1000)
    try:
        time.sleep(.12)
    finally:
        player.ctrl_transfer(0x40, remote.REQUEST, key, 0, b"", timeout=1000)
    time.sleep(.18)


def wait_for(probe, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result is not None:
            return result
        time.sleep(.10)
    raise RuntimeError(message)


def status_for(player, launch):
    current = shell.plugin_status(player)
    if (current.generation, current.name) != (launch.generation, launch.name):
        raise RuntimeError(f"Launch changed: expected {launch}, received {current}")
    return current


def expect_exit(current, reason):
    if current.state != "idle" or current.reason != reason:
        raise RuntimeError(f"Expected {reason} exit, received {current}")
    if reason == "normal" and current.result != 0:
        raise RuntimeError(f"Expected zero status, received {current}")
    return current


def back_out(player, launch):
    # One Menu closes one level. Rockbox Info returns from a detail list first.
    for _ in range(3):
        current = status_for(player, launch)
        if current.state == "idle":
            return expect_exit(current, "normal")
        shell.plugin_exit(player)
        deadline = time.monotonic() + .65
        while time.monotonic() < deadline:
            current = status_for(player, launch)
            if current.state == "idle":
                return expect_exit(current, "normal")
            time.sleep(.08)
    return expect_exit(status_for(player, launch), "normal")


def capture(player, output, label):
    path = output / (label + ".png")
    shell.screenshot(player, path)
    return path


def playing(player):
    flags, = struct.unpack("<I", shell.read_control(player, 5, 4))
    return (flags & 3) == 1


def exercise(player, output, case, require_playback=False):
    name, keys, expect_change = case
    launch = shell.plugin_launch(player, name)
    time.sleep(.35)  # Allow the plugin to draw its first frame.
    try:
        before = capture(player, output, name + "-before")
        for key in keys:
            tap(player, key)
        after = capture(player, output, name + "-after")
        if status_for(player, launch).state != "running":
            raise RuntimeError(f"{name} exited before plugin-exit")
        changed = before.read_bytes() != after.read_bytes()
        if expect_change and not changed:
            raise RuntimeError(f"{name} did not redraw after remote keys")
        if require_playback and not playing(player):
            raise RuntimeError("Test-track playback did not continue in Oscilloscope")
    except BaseException:
        with suppress(RuntimeError, OSError):
            back_out(player, launch)
        raise
    result = back_out(player, launch)
    return {"exit": asdict(result), "screen_changed": changed,
            "before": before.name, "after": after.name, "playback": require_playback}


def usb_takeover(serial, player, output, control_socket):
    # Reuse the serial-targeted PodLink Disk Mode request, block-device identity
    # check, and UDisks unmount + Drive.Eject from the Desktop media sync.
    import podlink
    from serial.tools import list_ports

    herdremote = str(Path(__file__).resolve().parent / "herdremote")
    if herdremote not in sys.path:
        sys.path.insert(0, herdremote)
    import hrdisk
    import hrdisksync

    port = podlink.find_port(p for p in list_ports.comports() if p.serial_number == serial)
    tty, device = hrdisksync.selected_player(port)
    if device[1] != serial:
        raise RuntimeError("PodLink player identity changed")
    launch = shell.plugin_launch(player, "snow")
    volume = None
    sent_disk_request = False
    try:
        time.sleep(.35)
        capture(player, output, "usb-before")
        sent_disk_request = True
        volume = hrdisksync.enter_disk(tty, device, control_socket or hrdisk.default_socket())
    finally:
        if sent_disk_request:
            if volume is None:
                volume = hrdisksync.find_volume(device)
            if volume is not None:
                volume.eject()
        if volume is None:
            with suppress(RuntimeError, OSError):
                back_out(player, launch)

    def returned():
        try:
            current = shell.find_player(serial)
        except (RuntimeError, usb.core.USBError):
            return None
        try:
            state = status_for(current, launch)
        except usb.core.USBError:
            return None
        return (current, state) if state.state == "idle" else None

    current, state = wait_for(returned, 30, "iPod did not return to PodLink after eject")
    result = expect_exit(state, "usb")
    capture(current, output, "usb-returned")
    return asdict(result)


def run(serial, output, control_socket):
    output.mkdir(parents=True, exist_ok=False)
    results = {}
    try:
        player = shell.find_player(serial)
        capture(player, output, "native-before")
        for case in CASES:
            name = case[0]
            if name == "oscilloscope":
                shell.execute(serial, "play")
                try:
                    wait_for(lambda: True if playing(player) else None, 5,
                             "Test track is not playing; check /.rockbox/sm1-test/irq-dma44.flac")
                    results[name] = exercise(player, output, case, require_playback=True)
                    if not playing(player):
                        raise RuntimeError("Playback stopped when Oscilloscope exited")
                finally:
                    shell.execute(serial, "stop")
            else:
                results[name] = exercise(player, output, case)
            print(f"{name}: normal exit", flush=True)
        capture(player, output, "native-after")
        results["usb-takeover"] = usb_takeover(serial, player, output, control_socket)
        print("USB takeover: USB exit and native screen restored", flush=True)
    except (RuntimeError, OSError, ValueError, struct.error) as error:
        results["error"] = str(error)
        raise
    finally:
        (output / "result.json").write_text(json.dumps(results, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="USB serial of the selected PodLink iPod")
    parser.add_argument("--out", type=Path, required=True, help="New evidence directory")
    parser.add_argument("--control-socket", help="HerdRemote disk control socket")
    args = parser.parse_args()
    try:
        run(args.serial, args.out, args.control_socket)
    except (RuntimeError, OSError, ValueError, struct.error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
