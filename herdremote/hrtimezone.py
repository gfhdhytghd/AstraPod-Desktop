"""Discover the host's IANA zone and TZif POSIX footer."""
import datetime as dt
from functools import lru_cache
import os
from pathlib import Path
import re
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError, available_timezones

CAPABILITY = b"tz\t1"
QUERY = b"tz\t?"
NAME = re.compile(r"[A-Za-z0-9_+/-]{1,63}\Z")
RULE = re.compile(r"[A-Za-z0-9<>+,:./-]{1,95}\Z")
_TZ_NAME = r"(?:[A-Za-z]{3,}|<[A-Za-z0-9+-]{3,}>)"
_TZ_TIME = r"[+-]?\d{1,3}(?::\d{1,2}(?::\d{1,2})?)?"
_TZ_DAY = rf"(?:M\d{{1,2}}\.\d\.\d|J?\d{{1,3}})(?:/{_TZ_TIME})?"
POSIX = re.compile(
    rf"{_TZ_NAME}{_TZ_TIME}(?:{_TZ_NAME}(?:{_TZ_TIME})?,{_TZ_DAY},{_TZ_DAY})?\Z",
    re.ASCII,
)
ROOTS = tuple(map(Path, TZPATH)) + (Path("/var/db/timezone/zoneinfo"),)


def _name(value):
    return (NAME.fullmatch(value) and not value.startswith("/") and
            all(value.split("/")) and not value.startswith(("posix/", "right/")))


def _file_zone(path):
    for root in ROOTS:
        try:
            name = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if _name(name):
            return name
    return None


def _footer(data):
    if data.startswith((b"TZif2", b"TZif3", b"TZif4")) and data.endswith(b"\n"):
        rule = data.rsplit(b"\n", 2)[-2].decode("ascii", errors="replace")
        if len(rule) <= 95 and POSIX.fullmatch(rule):
            return rule
    return "-"


@lru_cache(maxsize=8)
def _named(name):
    for root in ROOTS:
        try:
            data = (root / name).read_bytes()
        except OSError:
            continue
        if data.startswith(b"TZif"):
            return name, _footer(data)
    try:
        from importlib.resources import files
        data = files("tzdata.zoneinfo").joinpath(*name.split("/")).read_bytes()
    except (ModuleNotFoundError, OSError):
        return None
    return name, _footer(data)


@lru_cache(maxsize=4)
def _copied_localtime(path, identity):
    del identity  # The stat tuple invalidates the cache if the file changes.
    try:
        data = path.read_bytes()
    except OSError:
        return "Local", "-"
    if not data.startswith(b"TZif"):
        return "Local", "-"
    try:
        name = Path("/etc/timezone").read_text().strip()
    except OSError:
        name = ""
    for root in ROOTS:
        candidates = ([name] if _name(name) else []) + sorted(available_timezones())
        for candidate in candidates:
            if not _name(candidate):
                continue
            try:
                if (root / candidate).read_bytes() == data:
                    return candidate, _footer(data)
            except OSError:
                continue
    return "Local", _footer(data)


def local_zone():
    """Return (zone name, POSIX rule), using '-' if the host exposes no rule."""
    override = os.environ.get("TZ")
    if override is not None:
        value = override.removeprefix(":")
        if not value:
            return "Etc/UTC", "UTC0"
        if _name(value):
            found = _named(value)
            if found:
                return found
        if value.startswith("/"):
            path = Path(value).resolve()
            name = _file_zone(path)
            if name:
                found = _named(name)
                if found:
                    return found
            try:
                stat = path.stat()
            except OSError:
                return "Local", "-"
            return _copied_localtime(path, (stat.st_dev, stat.st_ino,
                                           stat.st_mtime_ns, stat.st_size))
        return "Local", value if len(value) <= 95 and POSIX.fullmatch(value) else "-"

    path = Path("/etc/localtime").resolve()
    name = _file_zone(path)
    if name:
        found = _named(name)
        if found:
            return found
    try:
        stat = path.stat()
    except OSError:
        return "Local", "-"
    return _copied_localtime(path, (stat.st_dev, stat.st_ino,
                                   stat.st_mtime_ns, stat.st_size))


def local_time(wall, zone):
    if zone and zone[0] != "Local":
        try:
            return dt.datetime.fromtimestamp(wall, ZoneInfo(zone[0]))
        except ZoneInfoNotFoundError:
            pass
    return dt.datetime.fromtimestamp(wall).astimezone()
