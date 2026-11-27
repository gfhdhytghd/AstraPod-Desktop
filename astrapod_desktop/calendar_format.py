"""Group iCalendar objects by UID, preserving recurrence and timezone data."""
from __future__ import annotations

from datetime import datetime, timezone

from icalendar import Calendar, Event, Todo

from .caldav import KINDS


def parse(data):
    try:
        calendar = Calendar.from_ical(data)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("无法读取 iCalendar 文件。") from None
    if not isinstance(calendar, Calendar):
        raise ValueError("iCalendar 文件缺少 VCALENDAR。")
    if any(component.errors for component in calendar.walk()):
        raise ValueError("iCalendar 文件包含无效的属性。")
    return calendar


def pack(components, timezones=(), template=None):
    calendar = Calendar()
    if template is not None:
        calendar.update(template)
    else:
        calendar.add("version", "2.0")
        calendar.add("prodid", "-//AstraPod//Desktop Extras//EN")
    for item in (*timezones, *components):
        calendar.add_component(item)
    return calendar.to_ical().decode("utf-8")


def timezones_for(calendar, components):
    used = set()
    for component in components:
        for child in component.walk():
            for _, value in child.property_items(recursive=False):
                tzid = getattr(value, "params", {}).get("TZID")
                if tzid:
                    used.add(str(tzid))
    return [zone for zone in calendar.walk("VTIMEZONE") if str(zone.get("TZID")) in used]


def split(data):
    calendar = parse(data)
    groups = {}
    for component in calendar.subcomponents:
        if component.name not in KINDS:
            continue
        uid = str(component.get("UID", ""))
        if not uid:
            raise ValueError("日历条目缺少 UID。")
        group = groups.setdefault(uid, {})
        key = (component.name, str(component.get("RECURRENCE-ID", "")))
        if key in group or (group and next(iter(group))[0] != component.name):
            raise ValueError("日历包含重复的 UID 或重复实例。")
        group[key] = component
    result = {}
    for uid, group in groups.items():
        components = [group[key] for key in sorted(group)]
        result[uid] = pack(components, timezones_for(calendar, components))
    return result


def merge(items, *, name=None):
    components, zones = [], {}
    for body in items.values():
        calendar = parse(body)
        for item in calendar.subcomponents:
            if item.name != "VTIMEZONE":
                components.append(item)
                continue
            tzid = str(item.get("TZID", ""))
            if tzid in zones and zones[tzid].to_ical() != item.to_ical():
                raise ValueError("日历包含同名但定义不同的时区。")
            zones[tzid] = item
    header = Calendar()
    header.add("VERSION", "2.0")
    header.add("PRODID", "-//AstraPod//Desktop Extras//EN")
    header.add("CALSCALE", "GREGORIAN")
    if name:
        header.add("X-WR-CALNAME", name)
    return pack(components, zones.values(), header).encode()


def remove_inbox_items(data, uids):
    """Remove only the component byte ranges copied to confirmed calendars."""
    starts = {b"BEGIN:VEVENT": (Event, b"END:VEVENT"), b"BEGIN:VTODO": (Todo, b"END:VTODO")}
    result, component = [], []
    parser = end = None
    removed = set()
    for line in data.splitlines(keepends=True):
        tag = line.rstrip(b"\r\n").upper()
        if parser is None and tag in starts:
            parser, end = starts[tag]
        if parser is None:
            result.append(line)
            continue
        component.append(line)
        if tag == end:
            uid = str(parser.from_ical(b"".join(component)).get("UID", ""))
            if uid in uids:
                removed.add(uid)
            else:
                result.extend(component)
            component.clear()
            parser = end = None
    if component or removed != uids:
        raise ValueError("无法在设备收件箱中定位已同步的日历条目。")
    return b"".join(result)


def kind(body):
    return next(item.name for item in parse(body).subcomponents if item.name in KINDS)


def as_resource(body, previous):
    if previous is None:
        return body
    calendar = parse(body)
    return pack(calendar.subcomponents, template=parse(previous))


def revision(body, base=None):
    old = {component.to_ical() for component in parse(base).subcomponents} if base else set()
    stamps = []
    for component in parse(body).subcomponents:
        if component.name not in KINDS or component.to_ical() in old:
            continue
        for name in ("LAST-MODIFIED", "DTSTAMP"):
            value = getattr(component.get(name), "dt", None)
            if isinstance(value, datetime):
                stamps.append(value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp())
                break
    return max(stamps, default=float("-inf"))
