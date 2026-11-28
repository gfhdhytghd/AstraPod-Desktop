"""The HerdRemote formatter and player TS parser agree on framed records."""
import datetime as dt
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from _support import require_firmware
import hrproto
import podlink


def main(out):
    root = require_firmware()
    sys.path.insert(0, str(root / "tools/tests"))
    from podlink_time_test import build

    parser = build(out)
    for name, rule, change in [
        ("America/New_York", "EST5EDT,M3.2.0,M11.1.0",
         dt.datetime(2026, 11, 1, 1, 30)),
        ("Australia/Lord_Howe", "<+1030>-10:30<+11>-11,M10.1.0,M4.1.0",
         dt.datetime(2026, 4, 5, 1, 45)),
        ("Asia/Kathmandu", "<+0545>-5:45", dt.datetime(2026, 1, 1, 9, 0)),
    ]:
        for fold in (0, 1):
            local = change.replace(tzinfo=ZoneInfo(name), fold=fold)
            for zone in (None, (name, rule)):
                wire = podlink.encode(podlink.TEXT, 9, hrproto.clock(local, zone))
                frame = podlink.Decoder().feed(wire)[0]
                response = subprocess.check_output(
                    [str(parser), frame.payload.decode()], text=True).split()
                assert response == [
                    str(int(local.timestamp())),
                    str(int(local.utcoffset().total_seconds())),
                    name if zone else "-", rule if zone else "-",
                ], response
    print("TS legacy and extended host/firmware contract PASS")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
