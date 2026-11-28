"""Imported iCalendar items kept in a private desktop calendar."""
from __future__ import annotations

from datetime import date, datetime, timezone
import os
from pathlib import Path

from . import calendar_format as ics
from .caldav import Collection, KINDS
from .extras_storage import MAX_FILE, _publish

FILE = "local-calendar.json"
LOCAL = Collection("local:ics", "本地日历", KINDS)


def load(store):
    return store.read(FILE, {})


def _instances(calendar):
    return {str(item.get("RECURRENCE-ID", "")): item
            for item in calendar.subcomponents if item.name in KINDS}


def _combine(original, incoming):
    if ics.kind(original) != ics.kind(incoming):
        raise ValueError("同一个 UID 同时用于日程和待办。")
    left, right = ics.parse(original), ics.parse(incoming)
    instances = _instances(left)
    zones = {}
    for zone in [*left.walk("VTIMEZONE"), *right.walk("VTIMEZONE")]:
        tzid = str(zone["TZID"])
        if tzid in zones and zones[tzid].to_ical() != zone.to_ical():
            raise ValueError("导入的日历包含同名但定义不同的时区。")
        zones[tzid] = zone
    conflicts = []
    for key, item in _instances(right).items():
        previous = instances.get(key)
        if previous is not None and previous.to_ical() != item.to_ical():
            old = ics.pack([previous], ics.timezones_for(left, [previous]))
            new = ics.pack([item], ics.timezones_for(right, [item]))
            if ics.revision(old) >= ics.revision(new):
                conflicts.append(new)
                continue
            conflicts.append(old)
        instances[key] = item
    body = ics.pack([instances[key] for key in sorted(instances)], zones.values())
    return next(iter(ics.split(body).values())), conflicts


def import_files(store, paths):
    with store.locked():
        result = load(store)
        conflicts = []
        for path in paths:
            with Path(path).open("rb") as stream:
                content = stream.read(MAX_FILE + 1)
            if len(content) > MAX_FILE:
                raise ValueError("ICS 文件超过 32 MiB。")
            for calendar in ics.split_many(content):
                for uid, incoming in calendar.items():
                    if uid in result and result[uid] != incoming:
                        incoming, copies = _combine(result[uid], incoming)
                        conflicts.extend((uid, body) for body in copies)
                    result[uid] = incoming
        ics.merge(result)
        names = [store.conflict((LOCAL.url, uid), body, "ics") for uid, body in conflicts]
        store.write(FILE, result)
    return result, names


def entries(items):
    for uid, body in items.items():
        for recurrence, component in _instances(ics.parse(body)).items():
            yield uid, recurrence, component


def _set_date(item, field, value):
    previous = item.pop(field, None)
    if not value:
        return
    try:
        parsed = (date.fromisoformat(value) if len(value) == 10 else
                  datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")))
    except ValueError:
        raise ValueError("日期使用 YYYY-MM-DD，时间使用 YYYY-MM-DDTHH:MM。") from None
    if isinstance(parsed, datetime):
        reference = previous
        if reference is None:
            companion = "DTSTART" if field != "DTSTART" else ("DTEND" if item.name == "VEVENT" else "DUE")
            reference = item.get(companion)
        old = getattr(reference, "dt", None)
        tzid = reference.params.get("TZID") if reference is not None else None
        if isinstance(old, datetime) and old.tzinfo:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=old.tzinfo)
            elif tzid:
                parsed = parsed.astimezone(old.tzinfo)
        if parsed.tzinfo is not None and not tzid:
            parsed = parsed.astimezone(timezone.utc)
        item.add(field, parsed)
        if tzid:
            item[field].params["TZID"] = tzid
    else:
        item.add(field, parsed)


def edit(body, recurrence, changes):
    calendar = ics.parse(body)
    item = _instances(calendar)[recurrence]
    if item.name == "VTODO":
        fields = {"SUMMARY", "DESCRIPTION", "DTSTART", "DUE", "STATUS"}
        dates = {"DTSTART", "DUE"}
    else:
        fields = {"SUMMARY", "DESCRIPTION", "LOCATION", "DTSTART", "DTEND"}
        dates = {"DTSTART", "DTEND"}
    if not changes.keys() <= fields:
        raise ValueError("条目包含此类型不支持的编辑字段。")
    if "SUMMARY" in changes and not changes["SUMMARY"].strip():
        raise ValueError("请输入标题。")
    before = item.to_ical()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for field, value in changes.items():
        if field in dates:
            _set_date(item, field, value.strip())
        else:
            item.pop(field, None)
            if value:
                item.add(field, value)
    if "DTEND" in changes and "DTEND" in item:
        item.pop("DURATION", None)
    if "DUE" in changes and "DUE" in item:
        item.pop("DURATION", None)
    if "STATUS" in changes and item.name == "VTODO":
        if changes["STATUS"] not in ("", "NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED"):
            raise ValueError("待办状态无效。")
        item.pop("COMPLETED", None)
        item.pop("PERCENT-COMPLETE", None)
        if changes["STATUS"] == "COMPLETED":
            item.add("COMPLETED", now)
            item.add("PERCENT-COMPLETE", 100)
    if dates & changes.keys():
        start = getattr(item.get("DTSTART"), "dt", None)
        end = getattr(item.get("DTEND" if item.name == "VEVENT" else "DUE"), "dt", None)
        if (item.name == "VEVENT" and start is None) or (end is not None and start is not None
                and (type(start) is not type(end) or
                     (isinstance(start, datetime) and (start.tzinfo is None) != (end.tzinfo is None))
                     or end <= start)):
            raise ValueError("结束时间必须晚于开始时间，且两者使用同一种日期或时间格式。")
    if item.to_ical() == before:
        return body
    for field, value in (("SEQUENCE", int(item.get("SEQUENCE", 0)) + 1),
                         ("DTSTAMP", now), ("LAST-MODIFIED", now)):
        item.pop(field, None)
        item.add(field, value)
    return next(iter(ics.split(calendar.to_ical()).values()))


def save(store, uid, before, after):
    with store.locked():
        items = load(store)
        if items.get(uid) != before:
            raise RuntimeError("条目已发生变化。请重新打开编辑器。")
        items[uid] = after
        store.write(FILE, items)
    return items


def export_file(path, data):
    path = Path(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _publish(directory, path.name, data)
    finally:
        os.close(directory)
