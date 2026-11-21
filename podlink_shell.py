#!/usr/bin/env python3
"""Open the PodLink shell on a connected iPod Classic."""
import argparse
from dataclasses import dataclass
import errno
from pathlib import Path
import re
import shlex
import struct
import sys
import time
import zlib

import usb.core


COMMANDS = {"reboot": 1, "play": 2, "seek": 3, "stop": 4, "open-bench": 5}
REQUEST, INDEX = 0x5D, 0x4951
RECORD_START = {"line-in": 6, "mic": 7}
RECORD_STOP = 8
RECORD_ACTIVE, RECORD_READY, RECORD_ERROR = 1, 2, 3
RECORD_STATES = (
    "idle", "recording", "saved", "capture failed", "input unsupported",
    "recorder busy",
)
PLUGIN_LAUNCH, PLUGIN_EXIT, SCREENSHOT_BEGIN = 9, 10, 11
PLUGIN_STATUS = struct.Struct("<IIIIi32s")
SCREENSHOT_META = struct.Struct("<IIHHHH")
PLUGIN_STATES = ("idle", "queued", "running")
PLUGIN_REASONS = ("none", "normal", "usb", "error")
PLUGIN_NAME = re.compile(rb"[a-zA-Z0-9][a-zA-Z0-9_-]{0,30}")
SCREENSHOT_DATA = 0x100
LCD_WIDTH, LCD_HEIGHT = 320, 240


@dataclass(frozen=True)
class PluginStatus:
    generation: int
    state: str
    reason: str
    result: int
    name: str

    def __str__(self):
        return (f"plugin={self.name or '-'} state={self.state} "
                f"reason={self.reason} result={self.result} generation={self.generation}")


def find_player(serial):
    players = [
        device for device in usb.core.find(
            find_all=True, idVendor=0x05AC, idProduct=0x1261
        )
        if device.serial_number == serial
    ]
    if len(players) != 1:
        raise RuntimeError("Expected one connected iPod with the specified USB serial")
    return players[0]


def status(device):
    return struct.unpack("<4I", device.ctrl_transfer(
        0xC0, REQUEST, 0, INDEX, 16, timeout=1000
    ))


def read_control(player, value, length):
    data = bytes(player.ctrl_transfer(0xC0, REQUEST, value, INDEX, length, timeout=1000))
    if len(data) != length:
        raise RuntimeError(f"Short PodLink Shell response for value {value:#x}")
    return data


def write_control(player, value, data=b""):
    if player.ctrl_transfer(0x40, REQUEST, value, INDEX, data, timeout=1000) != len(data):
        raise RuntimeError(f"Short PodLink Shell write for value {value:#x}")


def plugin_status(player):
    version, generation, state, reason, result, raw = PLUGIN_STATUS.unpack(
        read_control(player, 3, PLUGIN_STATUS.size)
    )
    name, end, padding = raw.partition(b"\0")
    if (version != 1 or state >= len(PLUGIN_STATES) or reason >= len(PLUGIN_REASONS)
            or not end or any(padding) or (name and not PLUGIN_NAME.fullmatch(name))):
        raise RuntimeError("Invalid plugin status")
    return PluginStatus(generation, PLUGIN_STATES[state], PLUGIN_REASONS[reason],
                        result, name.decode("ascii"))


def plugin_launch(player, name):
    if not name or not PLUGIN_NAME.fullmatch(name.encode("utf-8")):
        raise ValueError("plugin-launch requires a plugin basename of 1 to 31 ASCII characters")
    initial = plugin_status(player)
    if initial.state != "idle":
        raise RuntimeError(f"A plugin is already active: {initial}")
    write_control(player, PLUGIN_LAUNCH, name.encode("ascii"))
    expected = (initial.generation + 1) & 0xFFFFFFFF
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        current = plugin_status(player)
        if current.generation == expected and current.name == name:
            if current.state == "running":
                return current
            if current.state == "idle":
                raise RuntimeError(f"Plugin returned during launch: {current}")
        elif current.generation != initial.generation:
            raise RuntimeError(f"Another launch replaced {name}: {current}")
        time.sleep(.05)
    raise RuntimeError(f"Timed out launching {name}")


def plugin_exit(player):
    write_control(player, PLUGIN_EXIT)


def screenshot_meta(player):
    return SCREENSHOT_META.unpack(read_control(player, 4, SCREENSHOT_META.size))


def lcd_snapshot(player):
    initial = screenshot_meta(player)[0]
    write_control(player, SCREENSHOT_BEGIN)
    expected = (initial + 1) & 0xFFFFFFFF
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        meta = screenshot_meta(player)
        generation, length, width, height, chunk, state = meta
        if generation == expected and state == 2:
            break
        if generation not in (initial, expected) or state > 2:
            raise RuntimeError("LCD snapshot was replaced")
        time.sleep(.05)
    else:
        raise RuntimeError("Timed out waiting for LCD snapshot")
    if ((width, height) != (LCD_WIDTH, LCD_HEIGHT) or length != width * height * 2
            or not 0 < chunk <= 4096 or (length + chunk - 1) // chunk > 65536 - SCREENSHOT_DATA):
        raise RuntimeError("Invalid LCD snapshot format")
    pixels = bytearray()
    for index, offset in enumerate(range(0, length, chunk)):
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out reading LCD snapshot")
        pixels.extend(read_control(player, SCREENSHOT_DATA + index, min(chunk, length - offset)))
    if screenshot_meta(player) != meta:
        raise RuntimeError("LCD snapshot changed during transfer")
    return bytes(pixels)


def png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data)))


def screenshot(player, destination):
    pixels = lcd_snapshot(player)
    rows = bytearray()
    for offset in range(0, len(pixels), LCD_WIDTH * 2):
        rows.append(0)  # PNG filter: None.
        for pixel, in struct.iter_unpack("<H", pixels[offset:offset + LCD_WIDTH * 2]):
            rows.extend(((pixel >> 11) * 255 // 31, ((pixel >> 5) & 63) * 255 // 63,
                         (pixel & 31) * 255 // 31))
    header = struct.pack(">IIBBBBB", LCD_WIDTH, LCD_HEIGHT, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
           + png_chunk(b"IDAT", zlib.compress(rows)) + png_chunk(b"IEND", b""))
    Path(destination).write_bytes(png)


def record_status(player):
    revision, state, path = struct.unpack("<II88s", player.ctrl_transfer(
        0xC0, REQUEST, 1, INDEX, 96, timeout=1000
    ))
    if revision not in (0, 1) or state >= len(RECORD_STATES):
        raise RuntimeError("Invalid recording status")
    return revision, state, path.split(b"\0", 1)[0].decode("utf-8")


def record_error(player):
    try:
        stage, status, pcm_error, fs_error, dma_blocks = struct.unpack(
            "<5I", player.ctrl_transfer(0xC0, REQUEST, 2, INDEX, 20, timeout=1000)
        )
    except usb.core.USBError as error:
        if error.errno != errno.EPIPE:
            raise
        return " diagnostics=unavailable"
    stages = ("none", "dir", "filename", "init", "start", "capture", "save")
    name = stages[stage] if stage < len(stages) else str(stage)
    return (f" stage={name} audio_status=0x{status:08x} "
            f"pcm_error=0x{pcm_error:08x} fs_errno={fs_error} dma_blocks={dma_blocks}")


def record_fetch(serial, path, destination):
    herdremote = str(Path(__file__).resolve().parent / "herdremote")
    if herdremote not in sys.path:
        sys.path.insert(0, herdremote)
    import hrlibrary
    import hrmediasync
    import podlink
    from serial.tools import list_ports

    with hrmediasync.borrow_serial():
        ports = [p for p in list_ports.comports() if p.serial_number == serial]
        with hrmediasync.connect_device(podlink.find_port(ports)) as (link, identity):
            if identity != serial:
                raise RuntimeError("The PodLink tty changed USB serial")
            hrlibrary.download_sample(link, path, destination)


def debug_names(player):
    for index in range(65536):
        data = player.ctrl_transfer(0xC0, REQUEST, index, 0x4952, 128, timeout=1000)
        name = bytes(data).split(b"\0", 1)[0]
        if not name:
            return
        yield index, name.decode("utf-8", errors="replace")


def execute(serial, action, argument=None):
    player = find_player(serial)
    if action == "plugin-status":
        print(plugin_status(player))
        return
    if action == "plugin-launch":
        print(plugin_launch(player, argument))
        return
    if action == "plugin-exit":
        plugin_exit(player)
        print("plugin exit requested")
        return
    if action == "screenshot":
        if not argument:
            raise ValueError("screenshot requires an output PNG path")
        screenshot(player, argument)
        print(f"Saved {LCD_WIDTH}x{LCD_HEIGHT} PNG to {argument}")
        return
    if action == "debug-list":
        for _, name in debug_names(player):
            print(name)
        return
    if action in ("record-info", "record-fetch"):
        revision, state, path = record_status(player)
        if action == "record-info":
            inputs = "line-in,mic" if revision else "line-in"
            details = record_error(player) if state == RECORD_ERROR else ""
            print(f"hw_revision={revision} inputs={inputs} "
                  f"state={RECORD_STATES[state]} path={path}{details}")
            return
        if not argument:
            raise ValueError("record-fetch requires an output WAV path")
        if not path or state == RECORD_ACTIVE:
            raise RuntimeError("Stop recording before fetching a completed WAV")
        record_fetch(serial, path, argument)
        print(f"record-fetch: {path} -> {argument}")
        return
    initial = status(player)
    if action == "status":
        print("ticks=%d submitted=%d completed=%d dispatch_tick=%d" % initial)
        return
    if initial[1] != initial[2]:
        raise RuntimeError("A shell command is still pending")
    if action == "debug":
        index = next((index for index, name in debug_names(player)
                      if name.casefold() == (argument or "").casefold()), None)
        if index is None:
            raise RuntimeError("Unknown Debug menu item")
        player.ctrl_transfer(0x40, REQUEST, index, 0x4952, b"", timeout=1000)
        print(f"Debug menu item queued: {argument}")
        return
    expected_record_state = None
    if action == "record-start":
        if argument not in RECORD_START:
            raise ValueError("record-start requires line-in or mic")
        command, expected_record_state = RECORD_START[argument], RECORD_ACTIVE
    elif action == "record-stop":
        if record_status(player)[1] != RECORD_ACTIVE:
            raise RuntimeError("No shell recording is active")
        command, expected_record_state = RECORD_STOP, RECORD_READY
    else:
        command = COMMANDS[action]
    address = (player.bus, player.address)
    player.ctrl_transfer(0x40, REQUEST, command, INDEX, b"", timeout=1000)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            current = find_player(serial) if action == "reboot" else player
            if action == "reboot" and (current.bus, current.address) == address:
                continue
            reply = status(current)
        except (RuntimeError, usb.core.USBError):
            continue
        if action == "reboot" or reply[2] == ((initial[2] + 1) & 0xFFFFFFFF):
            if expected_record_state is not None:
                _, state, path = record_status(current)
                if state != expected_record_state:
                    details = record_error(current) if state == RECORD_ERROR else ""
                    raise RuntimeError(f"{action}: {RECORD_STATES[state]}{details}")
                print(f"{action}: {path if state == RECORD_READY else 'recording'}")
                return
            print(f"{action}: {'re-enumerated' if action == 'reboot' else 'processed'}")
            return
    raise RuntimeError("Timed out waiting for the shell command")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="USB serial number of the iPod")
    actions = ("status", "debug-list", "debug", *COMMANDS,
               "record-info", "record-start", "record-stop", "record-fetch",
               "plugin-launch", "plugin-status", "plugin-exit", "screenshot")
    parser.add_argument("command", nargs="?", choices=actions)
    parser.add_argument("name", nargs="?",
                        help="Debug label, plugin name, recording source or output file")
    parser.add_argument("--format", choices=("wav",), default="wav",
                        help="record-start format (default: wav)")
    args = parser.parse_args()
    failures = (RuntimeError, OSError, ValueError, struct.error)
    if args.command:
        try:
            execute(args.serial, args.command, args.name)
        except failures as error:
            parser.exit(1, f"{error}\n")
        return
    print("Commands: " + ", ".join((*actions, "exit")))
    while True:
        try:
            words = shlex.split(input("podlink> "))
            if not words:
                continue
            if words[0] == "exit":
                return
            if words[0] not in actions:
                print("Unknown command")
                continue
            execute(args.serial, words[0], " ".join(words[1:]) or None)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        except failures as error:
            print(error)


if __name__ == "__main__":
    main()
