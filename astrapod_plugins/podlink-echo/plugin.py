"""Companion for the AstraPod PodLink echo and file example."""
import os
from pathlib import Path
import signal

from astrapod_plugins.podlink_files import PodLinkFiles
from astrapod_plugins.podlink_wire import Op, Packet
from astrapod_plugins.sdk import PodLinkPlugin


def stop(_signum, _frame):
    raise SystemExit(0)


def main():
    plugin = PodLinkPlugin()
    default = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    directory = Path(plugin.settings["directory"] or default / "astrapod/podlink-echo")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    files = PodLinkFiles(directory)
    signal.signal(signal.SIGTERM, stop)
    plugin.status("running", detail=str(directory))
    active = None
    try:
        while True:
            expired = files.expire()
            if expired is not None:
                plugin.send_podlink(*expired)
            event = plugin.receive_podlink(.25)
            if event is None:
                continue
            token, packet = event
            if packet.op == Op.OPEN:
                files.close()
                active = token, packet.channel
                plugin.send_podlink(token, Packet(Op.OPEN_OK, packet.channel))
            elif (token, packet.channel) == active:
                if packet.op in (Op.TEXT, Op.BINARY):
                    plugin.send_podlink(token, packet)
                else:
                    for reply in files.handle(token, packet):
                        plugin.send_podlink(token, reply)
                    if packet.op == Op.CLOSE:
                        active = None
    except (BrokenPipeError, EOFError):
        pass
    finally:
        files.close()


if __name__ == "__main__":
    main()
