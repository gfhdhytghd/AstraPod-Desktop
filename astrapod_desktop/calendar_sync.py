"""Reconcile each selected CalDAV collection against its last device export."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from . import calendar_format as ics
from .caldav import DavError
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


def _save_conflict(store, conflicts, collection, uid, body, source):
    name = store.conflict((collection.url, uid), body, "ics")
    conflicts.append(f"{collection.name} · {source} · {name}")


def _decide(base, local, remote):
    """Return the winner and any losing edit. Ties prefer the server."""
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


def _sync_collection(client, store, disk, collection, key, snapshot, state, before, local):
    remote = _contents(snapshot)
    base = state.get("device", {})
    result, conflicts = {}, []
    modified = False
    for uid in sorted(base.keys() | local.keys() | remote.keys()):
        href, remote_body = remote.get(uid, (_resource_url(collection.url, uid), None))
        local_body = local.get(uid)
        previous_body = base.get(uid)
        winner, losing = _decide(previous_body, local_body, remote_body)
        if (previous_body is not None and local_body != remote_body
                and local_body != previous_body and remote_body != previous_body
                and (local_body is None or remote_body is None)):
            conflicts.append(f"{collection.name} · {uid}：删除与编辑冲突，保留编辑。")
        if losing is not None:
            source = "iPod" if losing == local_body else "CalDAV"
            _save_conflict(store, conflicts, collection, uid, losing, source)
        if winner != remote_body:
            modified = True
            previous = snapshot["resources"].get(href)
            etag = previous["etag"] if previous else None
            try:
                if winner is None:
                    client.delete(href, etag)
                else:
                    body = ics.as_resource(winner, previous["body"] if previous else None)
                    client.put(href, body, etag=etag, new=previous is None)
            except DavError as error:
                if error.status in (404, 409, 412):
                    raise RuntimeError("CalDAV 日历在同步期间发生变化。请重新同步。") from None
                raise
            if winner is None:
                del snapshot["resources"][href]
            else:
                saved = client.get(href)
                stored = ics.split(saved["body"])
                if set(stored) != {uid}:
                    raise ValueError("CalDAV 服务器返回的 UID 与写入的条目不符。")
                snapshot["resources"][href] = saved
                winner = stored[uid]
        if winner is not None:
            result[uid] = winner

    if modified:
        snapshot.update(token=None, ctag=None)
    encoded = ics.merge(result, name=collection.name)
    path = ("Calendars", state["filename"])
    # Write the expected output to the journal before replacing the device file.
    # A restart can distinguish the committed write from an unsent device edit.
    pending = dict(device=ics.split(encoded), hash=digest(encoded))
    store.write(key, dict(state, pending=pending))
    disk.write(path, before, encoded)
    store.write(key, dict(filename=path[-1], device=pending["device"], snapshot=snapshot))
    return conflicts


def sync(client, store, disk, serial, account, calendars, destinations):
    inbox_data = disk.read(INBOX)
    inbox = ics.split(inbox_data) if inbox_data is not None else {}
    inputs, owners = {}, {}
    for collection in calendars:
        identity = fingerprint(account, collection.url)
        key = f"calendar-{fingerprint(serial, identity)}.json"
        filename = f"AstraPod-DAV-{identity[:24]}.ics"
        state = store.read(key, {"filename": filename})
        path = ("Calendars", filename)
        before = disk.read(path)
        if before is not None and "device" not in state and "pending" not in state:
            raise ValueError("iPod 已有同名日历但缺少同步记录。请先备份并移走该文件。")
        pending = state.get("pending")
        if before is not None and pending and pending["hash"] == digest(before):
            state["device"] = pending["device"]
        local = ics.split(before) if before is not None else state.get("device", {}).copy()
        old = state.get("snapshot", {})
        # The client may return an unchanged cache. Keep the prior state immutable.
        snapshot = client.snapshot(collection.url, old)
        snapshot = dict(snapshot, resources=snapshot["resources"].copy())
        for uid in local.keys() | state.get("device", {}).keys() | _contents(snapshot).keys():
            owners.setdefault(uid, []).append(collection.url)
        inputs[collection.url] = (key, snapshot, state, before, local)

    moved, conflicts = set(), []
    route_key = f"inbox-{fingerprint(serial)}.json"
    saved_routes = store.read(route_key, {})
    routes = {uid: url for uid, url in saved_routes.items() if uid in inbox}
    collections = {calendar.url: calendar for calendar in calendars}
    for uid, body in inbox.items():
        matches = owners.get(uid, [])
        if len(matches) > 1:
            raise ValueError("设备新条目的 UID 对应多个日历，无法确定目标。")
        target = routes.get(uid) or (matches[0] if matches else destinations.get(ics.kind(body)))
        if target not in inputs:
            continue
        collection = collections[target]
        if ics.kind(body) not in collection.kinds:
            raise ValueError("选择的日历不支持此设备条目类型。")
        _, _, state, _, local = inputs[target]
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
