#!/usr/bin/env python3
"""Open the PodLink shell on a connected iPod Classic."""
import argparse
from dataclasses import dataclass
import errno
import json
from pathlib import Path
import re
import secrets
import shlex
import struct
import sys
import time
import zlib

import usb.core


COMMANDS = {"reboot": 1, "play": 2, "seek": 3, "stop": 4, "open-bench": 5}
REQUEST, INDEX = 0x5D, 0x4951
PLAYBACK_INDEX = 0x4954
PLAYBACK_COMMANDS = {"play": 1, "seek": 2, "next": 3, "prev": 4, "open-lyrics": 5}
PLAYBACK_STATUS = struct.Struct("<IIIi6Ii24s260s")
PLAYBACK_COMMAND_ERRORS = {2: "file not found", 5: "cannot create playback playlist",
                           34: "seek outside track", 61: "no active playback"}
LCD_INDEX = 0x4955
LCD_COMMANDS = {"lcd-timing": 1, "lcd-te-sync": 2, "lcd-pacing": 3, "lcd-diag": 4,
                "lcd-dma-burst": 5, "lcd-dma-width": 6, "lcd-boost": 7,
                "lcd-bench": 8, "lcd-pio": 9, "lcd-async": 11}
LCD_TIMINGS = {f"0x{timing:02x}": timing
               for timing in (0x33, 0x22, 0x21, 0x12, 0x11, 0x10, 0x01)}
LCD_TIMINGS["0x00"] = 0xFF  # Wire value 0 stays the query opcode's argument.
LCD_DMA_OPTIONS = {"lcd-dma-burst": {"get": 0, "1": 1, "4": 4, "8": 8, "16": 16},
                   "lcd-dma-width": {"get": 0, "16": 16, "32": 32}}
LCD_BENCH_OPTIONS = {"full": 0, "band": 1}
LCD_SWITCH = {"off": 0, "on": 1, "get": 2}
LCD_PACE_STATUS = 10  # Argument selects one LCD_PACE_FIELDS entry.
LCD_PACE_FIELDS = ("period", "divisor", "shown", "missed", "offset", "cost", "dma", "flags")
LCD_PACE_ENABLED, LCD_PACE_MEASURED, LCD_PACE_PHASED = 1, 2, 4
LCD_REPLY = struct.Struct("<4I")
LCD_ERRORS = {5: "could not save lcd-te-diag.txt",
              16: "LCD asleep or busy; retry with the LCD awake",
              61: "no usable LCD sync source", 128: "PodLink disconnected",
              134: "requires LCD type 1"}
LCD_DIAG_PATH = "/.rockbox/lcd-te-diag.txt"
UI_PERF_PATH = "/.rockbox/ui-perf.txt"
SETTINGS_INDEX = 0x4953
SETTINGS_REQUESTS = {"settings-tree": 1, "settings-get": 2,
                     "settings-set": 3, "settings-action": 4}
SETTINGS_OUT_SIZE = 704
SETTINGS_PAGE_SIZE = 128
SETTINGS_HEADER = struct.Struct("<IH")
IPOD_VENDOR, IPOD_PRODUCT = 0x05AC, 0x1261
EP0_STALL_DELAYS = (.05, .10, .20, .25, .25, .25, .25, .25)
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
    players = []
    for device in usb.core.find(find_all=True, idVendor=IPOD_VENDOR, idProduct=IPOD_PRODUCT):
        if (device.idVendor, device.idProduct) != (IPOD_VENDOR, IPOD_PRODUCT):
            continue
        try:
            if device.serial_number == serial:
                players.append(device)
        except (ValueError, usb.core.USBError):
            continue
    if len(players) != 1:
        raise RuntimeError("Expected one connected iPod with the specified USB serial")
    return players[0]


def status(device):
    return struct.unpack("<4I", read_control(device, 0, 16))


def ep0_transfer(player, direction, request, value, index, payload):
    """Retry a stalled transfer without repeating the surrounding command."""
    for delay in EP0_STALL_DELAYS:
        try:
            return player.ctrl_transfer(direction, request, value, index, payload, timeout=1000)
        except usb.core.USBError as error:
            if error.errno != errno.EPIPE:
                raise
            time.sleep(delay)
    return player.ctrl_transfer(direction, request, value, index, payload, timeout=1000)


def read_control(player, value, length):
    data = bytes(ep0_transfer(player, 0xC0, REQUEST, value, INDEX, length))
    if len(data) != length:
        raise RuntimeError(f"Short PodLink Shell response for value {value:#x}")
    return data


def write_control(player, value, data=b""):
    if ep0_transfer(player, 0x40, REQUEST, value, INDEX, data) != len(data):
        raise RuntimeError(f"Short PodLink Shell write for value {value:#x}")


def playback_status(player):
    data = bytes(ep0_transfer(player, 0xC0, REQUEST, 0, PLAYBACK_INDEX, PLAYBACK_STATUS.size))
    version, sequence, operation, command_error, flags, elapsed, length, codec, rate, bits, error, label, path = (
        PLAYBACK_STATUS.unpack(data)
    )
    if version != 1:
        raise RuntimeError(f"Unsupported playback status version {version}")
    state = "paused" if flags & 2 else "playing" if flags & 1 else "stopped"
    if flags & 4:
        state = "recording"
    return dict(state=state, flags=flags, path=path.split(b"\0", 1)[0].decode("utf-8", "replace"),
                elapsed_ms=elapsed, length_ms=length, codec_id=codec,
                codec=label.split(b"\0", 1)[0].decode("utf-8", "replace"),
                frequency_hz=rate, bits_per_sample=bits, playback_error=error,
                sequence=sequence, operation=operation, command_error=command_error)


def playback_payload(action, argument, start_ms):
    if action == "play":
        path = argument.encode("utf-8")
        if not path.startswith(b"/") or not 2 <= len(path) <= 259 or any(
            ch in path for ch in (b"\0", b"\r", b"\n")
        ):
            raise ValueError("play requires an absolute iPod path of 2 to 259 UTF-8 bytes")
        return struct.pack("<I", playback_ms(start_ms if start_ms is not None else 0)) + path
    if action == "seek":
        return struct.pack("<I", playback_ms(argument))
    if argument is not None:
        raise ValueError(f"{action} takes no argument")
    return b""


def playback_ms(value):
    try:
        position = int(value)
        if 0 <= position <= 0x7fffffff:
            return position
    except (ValueError, TypeError):
        pass
    raise ValueError("Playback position must be an integer from 0 to 2147483647 ms")


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


def settings_exchange(player, command, body):
    cookie = secrets.randbelow(0xFFFFFFFF) + 1
    payload = struct.pack("<I", cookie) + body
    if len(payload) > SETTINGS_OUT_SIZE:
        raise ValueError(f"Settings request exceeds {SETTINGS_OUT_SIZE} bytes including the header")
    try:
        written = ep0_transfer(player, 0x40, REQUEST,
                               SETTINGS_REQUESTS[command], SETTINGS_INDEX, payload)
    except usb.core.USBError as error:
        if error.errno == errno.EPIPE:
            raise RuntimeError("Player rejected the Settings request (busy or unsupported)") from error
        raise
    if written != len(payload):
        raise RuntimeError("Short PodLink Shell Settings write")

    def read_page(page):
        data = bytes(ep0_transfer(player, 0xC0, REQUEST, page,
                                  SETTINGS_INDEX, SETTINGS_PAGE_SIZE))
        if len(data) != SETTINGS_PAGE_SIZE:
            raise RuntimeError("Short PodLink Shell Settings page")
        return SETTINGS_HEADER.unpack_from(data), data[SETTINGS_HEADER.size:]

    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        header, chunk = read_page(0)
        if header[0] == cookie:
            break
        time.sleep(.01)
    else:
        raise RuntimeError("Timed out waiting for Settings reply")

    length = header[1]
    if not length:
        raise RuntimeError("Empty PodLink Shell Settings reply")
    page_length = len(chunk)
    reply = bytearray(chunk)
    for page, _ in enumerate(range(page_length, length, page_length), 1):
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out reading Settings reply")
        next_header, chunk = read_page(page)
        if next_header != header:
            raise RuntimeError("Settings reply changed during transfer")
        reply.extend(chunk)
    try:
        result = json.loads(reply[:length].decode("utf-8"))
    except ValueError as error:
        raise RuntimeError("Invalid PodLink Shell Settings JSON") from error
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"{command}: {result['error']}")
    return result


def settings_row(result):
    if (not isinstance(result, dict)
            or result.get("kind") not in ("menu", "setting", "action")
            or not isinstance(result.get("id"), str) or not result["id"]
            or not isinstance(result.get("label"), str)
            or not isinstance(result.get("path"), list)
            or not result["path"]
            or not all(isinstance(part, str) for part in result["path"])):
        raise RuntimeError("Invalid PodLink Shell Settings row")
    return result


def settings_rows(player):
    for ordinal in range(65536):
        result = settings_exchange(player, "settings-tree", struct.pack("<H", ordinal))
        if result is None:
            return
        yield settings_row(result)
    raise RuntimeError("Settings tree did not terminate")


def settings_action_id(rows, selector):
    actions = [row for row in rows if row["kind"] == "action"]
    folded = selector.casefold()
    matches = [row for row in actions if row["id"] == selector]
    if not matches:
        matches = [row for row in actions if " / ".join(row["path"]).casefold() == folded]
    if not matches:
        matches = [row for row in actions if row["label"].casefold() == folded]
    if not matches:
        raise RuntimeError("Unknown Settings action")
    if len(matches) != 1:
        paths = ", ".join(f"{' / '.join(row['path'])} [{row['id']}]" for row in matches)
        raise RuntimeError(f"Ambiguous Settings action. Use a full path or ID. Matches: {paths}")
    return matches[0]["id"]


def print_settings_row(row, indent=0):
    name = row["label"]
    if row.get("cfg") is not None:
        value = json.dumps(row.get("value"), ensure_ascii=False)
        name += f" ({row['cfg']}) = {value}"
        fields = ("type", "allowed", "values", "min", "max", "step", "unit",
                  "decimals", "max_bytes", "format", "writable")
        details = [f"{field}={row[field]}" for field in fields if row.get(field) is not None]
        if details:
            name += f" [{', '.join(details)}]"
    if row["kind"] == "action":
        name += f" [action ID: {row['id']}]"
    if row.get("visible") is False:
        name += " [hidden]"
    if row.get("available") is False:
        name += " [unavailable]"
    print("  " * indent + name)


def execute_settings(serial, command, argument, value, yes, json_output):
    if command == "settings-action" and not yes:
        raise ValueError("settings-action requires --yes")
    if command != "settings-action" and yes:
        raise ValueError("--yes applies to settings-action")
    if command == "settings-tree":
        if argument is not None or value is not None:
            raise ValueError("settings-tree takes no arguments")
    elif not argument:
        raise ValueError(f"{command} requires a Settings selector")
    if command == "settings-set":
        if value is None:
            raise ValueError("settings-set requires a cfg name and value")
        if ":" in argument or any(char in argument + value for char in "\0\r\n"):
            raise ValueError("Provide one cfg name and one single-line value")
    elif value is not None:
        raise ValueError(f"{command} takes at most one argument")

    player = find_player(serial)
    if command == "settings-tree":
        rows = list(settings_rows(player))
        if json_output:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for row in rows:
                print_settings_row(row, len(row["path"]) - 1)
        return
    if command == "settings-action":
        action_id = settings_action_id(settings_rows(player), argument)
        result = settings_exchange(player, command, b"\1" + action_id.encode("utf-8"))
        if result != {"status": "opened"}:
            raise RuntimeError("Invalid PodLink Shell Settings action reply")
        print(json.dumps(result) if json_output else "Settings action opened on the player")
        return

    body = f"{argument}: {value}" if command == "settings-set" else argument
    row = settings_row(settings_exchange(player, command, body.encode("utf-8")))
    if not isinstance(row.get("cfg"), str) or not row["cfg"]:
        raise RuntimeError("Unexpected PodLink Shell setting result")
    if json_output:
        print(json.dumps(row, ensure_ascii=False))
    else:
        print_settings_row(row)


def execute_lcd(serial, action, argument):
    if action == "lcd-timing":
        if argument is not None and argument not in LCD_TIMINGS:
            raise ValueError("lcd-timing accepts " + ", ".join(LCD_TIMINGS))
        option = LCD_TIMINGS.get(argument, 0)
    elif action in LCD_DMA_OPTIONS:
        options = LCD_DMA_OPTIONS[action]
        if argument not in options:
            raise ValueError(f"{action} accepts " + ", ".join(options))
        option = options[argument]
    elif action == "lcd-bench":
        if argument not in LCD_BENCH_OPTIONS:
            raise ValueError("lcd-bench requires full or band")
        option = LCD_BENCH_OPTIONS[argument]
    elif action == "lcd-diag":
        if argument not in (None, "survey"):
            raise ValueError("lcd-diag takes no argument or survey")
        option = 1 if argument == "survey" else 0
    elif action == "lcd-pacing" and argument == "status":
        option = None
    else:
        if argument not in LCD_SWITCH:
            choices = "on, off, get or status" if action == "lcd-pacing" else "on, off or get"
            raise ValueError(f"{action} requires {choices}")
        option = LCD_SWITCH[argument]

    player = find_player(serial)
    if action == "lcd-pacing" and argument == "status":
        print_pace_status({field: lcd_exchange(player, action, LCD_PACE_STATUS, number)
                           for number, field in enumerate(LCD_PACE_FIELDS)})
        return
    value = lcd_exchange(player, action, LCD_COMMANDS[action], option)
    if action == "lcd-diag":
        print(f"lcd-diag: saved {LCD_DIAG_PATH}")
    elif action == "lcd-timing":
        print(f"LCD_PHTIME=0x{value:02x}")
    elif action in LCD_DMA_OPTIONS:
        print(f"{action}: {value}")
    elif action == "lcd-bench":
        print(f"lcd-bench: {argument} measured")
    else:
        print(f"{action}: {'on' if value else 'off'}")


def lcd_exchange(player, action, opcode, option):
    cookie = secrets.randbelow(0xFFFFFFFF) + 1
    payload = struct.pack("<IB", cookie, option)
    if ep0_transfer(player, 0x40, REQUEST, opcode, LCD_INDEX, payload) != len(payload):
        raise RuntimeError("Short PodLink Shell LCD command write")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        data = bytes(ep0_transfer(player, 0xC0, REQUEST, 0, LCD_INDEX, LCD_REPLY.size))
        if len(data) != LCD_REPLY.size:
            raise RuntimeError("Short PodLink Shell LCD reply")
        version, completed_cookie, error, value = LCD_REPLY.unpack(data)
        if version != 1:
            raise RuntimeError(f"Unsupported PodLink Shell LCD reply version {version}")
        if completed_cookie != cookie:
            time.sleep(0.05)
            continue
        if error:
            detail = LCD_ERRORS.get(error, f"error {error}")
            raise RuntimeError(f"{action}: {detail}")
        return value
    raise RuntimeError(f"{action}: timed out waiting for the firmware result")


def print_pace_status(status):
    flags = status["flags"]
    enabled = bool(flags & LCD_PACE_ENABLED)
    period = status["period"]
    source = ("authored 60 fps" if not enabled else
              "measured TE" if flags & LCD_PACE_MEASURED else "nominal TE")
    print(f"lcd-pacing: {'on' if enabled else 'off'}")
    print(f"period: {period} us ({1e6 / period:.3f} Hz, {source})" if period
          else "period: unknown")
    print(f"TE phase: {'known' if flags & LCD_PACE_PHASED else 'unknown'}")
    divisor = status["divisor"]
    if not divisor:
        print("last animation: none")
        return
    rate = {1: "full", 2: "half"}.get(divisor, f"1/{divisor}")
    print(f"last animation: {rate} rate")
    print(f"shown: {status['shown']} frames")
    print(f"missed: {status['missed']} frames")
    print(f"wake offset: {status['offset']} us after TE")
    print(f"cost: {status['cost']} us per frame (average)")
    print(f"dma: {status['dma']} us (longest)")


def execute(serial, action, argument=None, *, value=None, yes=False, json_output=False,
            start_ms=None):
    if start_ms is not None and (action != "play" or argument is None):
        raise ValueError("--start-ms requires play with an iPod path")
    if action in SETTINGS_REQUESTS:
        execute_settings(serial, action, argument, value, yes, json_output)
        return
    if action == "log-fetch":
        if not argument or not argument.startswith("/") or not value or yes or json_output:
            raise ValueError("log-fetch requires an absolute iPod log path and a local output path")
        record_fetch(serial, argument, value)
        print(f"log-fetch: {argument} -> {value}")
        return
    if value is not None or yes or (json_output and action != "playback-status"):
        raise ValueError("Unexpected argument, --yes or --json")
    if action in LCD_COMMANDS:
        execute_lcd(serial, action, argument)
        return
    if action == "ui-perf":
        if not argument:
            raise ValueError("ui-perf requires a local output path")
        record_fetch(serial, UI_PERF_PATH, argument)
        print(f"ui-perf: {UI_PERF_PATH} -> {argument}")
        return
    if action == "playback-status":
        if argument is not None:
            raise ValueError("playback-status takes no argument")
        result = playback_status(find_player(serial))
        print(json.dumps(result, ensure_ascii=False) if json_output else
              " ".join(f"{key}={value!r}" for key, value in result.items()))
        return
    parameterized = action in PLAYBACK_COMMANDS and (action not in COMMANDS or argument is not None)
    payload = playback_payload(action, argument, start_ms) if parameterized else b""
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
    elif parameterized:
        command = PLAYBACK_COMMANDS[action]
    else:
        command = COMMANDS[action]
    address = (player.bus, player.address)
    index = PLAYBACK_INDEX if parameterized else INDEX
    transferred = ep0_transfer(player, 0x40, REQUEST, command, index, payload)
    if parameterized and transferred != len(payload):
        raise RuntimeError("Short PodLink Shell command write")
    expected_sequence = (initial[2] + 1) & 0xFFFFFFFF
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
        if parameterized:
            result = playback_status(current)
            if result["sequence"] == expected_sequence and result["operation"] == command:
                error = result["command_error"]
                if error:
                    detail = PLAYBACK_COMMAND_ERRORS.get(error, f"error {error}")
                    raise RuntimeError(f"{action}: {detail}")
                print(f"{action}: processed")
                return
            if reply[2] != initial[2] and reply[2] != expected_sequence:
                raise RuntimeError("Playback result superseded by another shell command")
            continue
        if action == "reboot" or reply[2] == expected_sequence:
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
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--serial", required=True, help="USB serial number of the iPod")
    actions = ("status", "debug-list", "debug", *COMMANDS,
               "next", "prev", "open-lyrics", "playback-status", "log-fetch",
               "record-info", "record-start", "record-stop", "record-fetch",
               "plugin-launch", "plugin-status", "plugin-exit", "screenshot",
               *SETTINGS_REQUESTS, *LCD_COMMANDS, "ui-perf")
    parser.add_argument("command", nargs="?", choices=actions)
    parser.add_argument("name", nargs="?",
                        help="Debug label, Settings selector, plugin name, path, seek position or LCD option")
    parser.add_argument("value", nargs="?", help="settings-set value or log-fetch output path")
    parser.add_argument("--start-ms", type=int, help="Starting position for play with a path")
    parser.add_argument("--format", choices=("wav",), default="wav",
                        help="record-start format (default: wav)")
    parser.add_argument("--yes", action="store_true", help="Confirm a Settings action")
    parser.add_argument("--json", action="store_true", help="Print Settings or playback status as JSON")
    args = parser.parse_intermixed_args()
    failures = (RuntimeError, OSError, ValueError, struct.error)

    def execute_args(parsed):
        execute(parsed.serial, parsed.command, parsed.name, value=parsed.value,
                yes=parsed.yes, json_output=parsed.json, start_ms=parsed.start_ms)

    if args.command:
        try:
            execute_args(args)
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
            if words[0] in SETTINGS_REQUESTS or words[0] in PLAYBACK_COMMANDS or words[0] in (
                "playback-status", "log-fetch", *LCD_COMMANDS, "ui-perf"
            ):
                try:
                    execute_args(parser.parse_intermixed_args(["--serial", args.serial, *words]))
                except SystemExit:
                    continue
            else:
                execute(args.serial, words[0], " ".join(words[1:]) or None)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        except failures as error:
            print(error)


if __name__ == "__main__":
    main()
