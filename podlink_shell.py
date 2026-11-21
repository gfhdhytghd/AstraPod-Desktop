#!/usr/bin/env python3
"""Open the PodLink shell on a connected iPod Classic."""
import argparse
import shlex
import struct
import time

import usb.core


COMMANDS = {"reboot": 1, "play": 2, "seek": 3, "stop": 4, "open-bench": 5}
REQUEST, INDEX = 0x5D, 0x4951


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


def debug_names(player):
    for index in range(65536):
        data = player.ctrl_transfer(0xC0, REQUEST, index, 0x4952, 128, timeout=1000)
        name = bytes(data).split(b"\0", 1)[0]
        if not name:
            return
        yield index, name.decode("utf-8", errors="replace")


def execute(serial, action, argument=None):
    player = find_player(serial)
    if action == "debug-list":
        for _, name in debug_names(player):
            print(name)
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
            print(f"{action}: {'re-enumerated' if action == 'reboot' else 'processed'}")
            return
    raise RuntimeError("Timed out waiting for the shell command")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="USB serial number of the iPod")
    actions = ("status", "debug-list", "debug", *COMMANDS)
    parser.add_argument("command", nargs="?", choices=actions)
    parser.add_argument("name", nargs="?", help="Debug menu label")
    args = parser.parse_args()
    failures = (RuntimeError, usb.core.USBError, ValueError, struct.error)
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
