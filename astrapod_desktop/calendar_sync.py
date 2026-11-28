"""Reconcile selected calendars against their last device exports."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from . import calendar_format as ics
from .caldav import DavError
from .local_calendar import FILE as LOCAL_FILE, LOCAL, load as load_local
from .extras_storage import digest, fingerprint

INBOX = ("Calendars", "AstraPod.ics")


def _resource_url(collection, uid):
    parts = urlsplit(collection)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/" + digest(uid) + ".ics"))


def _contents(snapshot):
    result = {}
    for href, resource in snapshot["resources"].items():
        items = ics.split(resource["body"])
        if not items:
            continue
        if len(items) != 1:
            raise ValueError("一个 CalDAV 资源包含多个不同的 UID。")
        uid, body = next(iter(items.items()))
        if uid in result:
            raise ValueError("CalDAV 日历包含重复 UID。")
        result[uid] = href, body
    return result


def export_dav(client, collection):
    items = _contents(client.snapshot(collection.url, {}))
    return ics.merge({uid: body for uid, (_, body) in items.items()}, name=collection.name)


def _save_conflict(store, conflicts, collection, uid, body, source):
    name = store.conflict((collection.url, uid), body, "ics")
    conflicts.append(f"{collection.name} · {source} · {name}")


def _decide(base, local, remote):
    """Return the winner and any losing edit. Ties prefer the desktop source."""
    if local == remote or local == base:
        return remote, None
    if remote == base:
        return local, None
    if local is None or remote is None:
        # An absence carries no modification timestamp. Preserve the edited item.
        return local or remote, None
    if ics.revision(local, base) > ics.revision(remote, base):
        return local, remote
    return remote, local


def _reconcile(store, collection, base, device, source, source_name, commit):
    result, conflicts = {}, []
    for uid in sorted(base.keys() | device.keys() | source.keys()):
        device_body, source_body = device.get(uid), source.get(uid)
        previous_body = base.get(uid)
        winner, losing = _decide(previous_body, device_body, source_body)
        if (previous_body is not None and device_body != source_body
                and device_body != previous_body and source_body != previous_body
                and (device_body is None or source_body is None)):
            conflicts.append(f"{collection.name} · {uid}：删除与编辑冲突，保留编辑。")
        if losing is not None:
            side = "iPod" if losing == device_body else source_name
            _save_conflict(store, conflicts, collection, uid, losing, side)
        if winner != source_body:
            winner = commit(uid, winner, conflicts)
        if winner is not None:
            result[uid] = winner
    return result, conflicts


def _sync_collection(client, store, disk, collection, key, snapshot, state, before, device, source):
    is_local = collection.url == LOCAL.url
    if is_local:
        result, conflicts = _reconcile(store, collection, state.get("device", {}), device,
                                       source, "本地日历", lambda _uid, winner, _conflicts: winner)
    else:
        result, conflicts = _sync_dav(client, store, collection, snapshot, state, device, source)
    encoded = ics.merge(result, name=collection.name)
    if is_local:
        store.write(LOCAL_FILE, result)
    path = ("Calendars", state["filename"])
    # Record the expected export before replacing the device file.
    pending = dict(device=result, hash=digest(encoded))
    store.write(key, dict(state, pending=pending))
    disk.write(path, before, encoded)
    store.write(key, dict(filename=path[-1], device=result, snapshot=snapshot))
    return conflicts


def _sync_dav(client, store, collection, snapshot, state, device, resources):
    source = {uid: body for uid, (_, body) in resources.items()}

    def commit(uid, winner, conflicts):
        href = resources[uid][0] if uid in resources else _resource_url(collection.url, uid)
        previous = snapshot["resources"].get(href)
        etag = previous["etag"] if previous else None
        snapshot.update(token=None, ctag=None)
        try:
            if winner is None:
                client.delete(href, etag)
            else:
                body = ics.as_resource(winner, previous["body"] if previous else None)
                written_etag = client.put(href, body, etag=etag, new=previous is None)
        except DavError as error:
            if error.status in (404, 409, 412):
                raise RuntimeError("CalDAV 日历在同步期间发生变化。请重新同步。") from None
            raise
        if winner is None:
            del snapshot["resources"][href]
        else:
            saved = client.get(href)
            if written_etag and saved["etag"] and written_etag != saved["etag"]:
                raise RuntimeError("CalDAV 在写入后收到另一处编辑。请重新同步。")
            stored = ics.split(saved["body"])
            if set(stored) != {uid}:
                raise ValueError("CalDAV 服务器返回的 UID 与写入的条目不符。")
            if (not written_etag or not saved["etag"]) and stored[uid] != winner:
                _save_conflict(store, conflicts, collection, uid, winner, "iPod")
            snapshot["resources"][href] = saved
            winner = stored[uid]
        return winner

    return _reconcile(store, collection, state.get("device", {}), device, source, "CalDAV", commit)


def sync(client, store, disk, serial, account, calendars, destinations):
    inbox_data = disk.read(INBOX)
    inbox = ics.split(inbox_data) if inbox_data is not None else {}
    inputs, owners = {}, {}
    for collection in calendars:
        is_local = collection.url == LOCAL.url
        identity = fingerprint("local" if is_local else account, collection.url)
        key = f"calendar-{fingerprint(serial, identity)}.json"
        filename = "AstraPod-Local.ics" if is_local else f"AstraPod-DAV-{identity[:24]}.ics"
        state = store.read(key, {"filename": filename})
        path = ("Calendars", filename)
        before = disk.read(path)
        if before is not None and "device" not in state and "pending" not in state:
            raise ValueError("iPod 已有同名日历但缺少同步记录。请先备份并移走该文件。")
        pending = state.get("pending")
        if before is not None and pending and pending["hash"] == digest(before):
            state["device"] = pending["device"]
        local = ics.split(before) if before is not None else state.get("device", {}).copy()
        if is_local:
            snapshot, source = {}, load_local(store)
        else:
            snapshot = client.snapshot(collection.url, state.get("snapshot", {}))
            snapshot = dict(snapshot, resources=snapshot["resources"].copy())
            source = _contents(snapshot)
        for uid in local.keys() | state.get("device", {}).keys() | source.keys():
            owners.setdefault(uid, []).append(collection.url)
        inputs[collection.url] = (key, snapshot, state, before, local, source)

    moved, conflicts = set(), []
    route_key = f"inbox-{fingerprint(serial)}.json"
    saved_routes = store.read(route_key, {})
    routes = {uid: url for uid, url in saved_routes.items() if uid in inbox}
    collections = {calendar.url: calendar for calendar in calendars}
    for uid, body in inbox.items():
        matches = owners.get(uid, [])
        target = routes.get(uid)
        if not target:
            if len(matches) > 1:
                raise ValueError("设备新条目的 UID 对应多个日历，无法确定目标。")
            target = matches[0] if matches else destinations.get(ics.kind(body))
        if target not in inputs:
            continue
        collection = collections[target]
        if ics.kind(body) not in collection.kinds:
            raise ValueError("选择的日历不支持此设备条目类型。")
        _, _, state, _, local, _ = inputs[target]
        if uid in local:
            body, losing = _decide(state.get("device", {}).get(uid), body, local[uid])
            if losing is not None:
                _save_conflict(store, conflicts, collection, uid, losing, "iPod")
        local[uid] = body
        routes[uid] = target
        moved.add(uid)

    if routes != saved_routes:
        store.write(route_key, routes)
    for collection in calendars:
        conflicts.extend(_sync_collection(client, store, disk, collection, *inputs[collection.url]))
    if moved:
        disk.write(INBOX, inbox_data, ics.remove_inbox_items(inbox_data, moved))
        store.write(route_key, {uid: url for uid, url in routes.items() if uid not in moved})
    return len(calendars), len(inbox) - len(moved), conflicts
