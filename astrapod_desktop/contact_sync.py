"""Two-way vCard file sync. Legacy multi-card files stay in their original paths."""
from __future__ import annotations

import re
from pathlib import Path

from . import contact_format as vcf
from .extras_storage import MAX_FILE, digest, fingerprint

ROOT = ("Contacts",)
MANAGED = (*ROOT, "AstraPod")
LIBRARY = "contacts.json"
SAFE_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,119}")


def load(store):
    return {uid: [tuple(entry) for entry in entries]
            for uid, entries in store.read(LIBRARY, {}).items()}


def save(store, entry, previous=None):
    with store.locked():
        cards = load(store)
        uid = vcf.uid(entry)
        if cards.get(uid) != previous:
            raise RuntimeError("联系人已发生变化。请重新打开编辑器。")
        cards[uid] = entry
        store.write(LIBRARY, cards)
    return cards


def import_files(store, files):
    imported = []
    for filename in files:
        with Path(filename).open("rb") as handle:
            data = handle.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError("联系人文件超过 32 MiB。")
        imported.extend(vcf.parse(data))
    with store.locked():
        cards = load(store)
        conflicts = []
        for card in imported:
            uid = vcf.uid(card)
            original = cards.get(uid)
            if original is not None and vcf.canonical(original) != vcf.canonical(card):
                if vcf.revision(original) > vcf.revision(card):
                    card, original = original, card
                name = store.conflict(("contact-import", uid), vcf.serialize([original]), "vcf")
                conflicts.append(f"{vcf.title(card)} · {name}")
            cards[uid] = card
        store.write(LIBRARY, cards)
    return cards, conflicts


def _file(uid):
    # URI-style UIDs can contain characters FAT rejects. Keep the UID in the
    # card and use its digest as the filename in that case.
    stem = uid if SAFE_UID.fullmatch(uid) and not uid.endswith(".") else digest(uid)
    device_name = stem.split(".")[0].upper()
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", device_name):
        stem = digest(uid)
    return (*MANAGED, stem + ".vcf")


def sync(store, disk, serial):
    cards = load(store)
    key = f"contacts-{fingerprint(serial)}.json"
    base = store.read(key, {})
    device, files, conflicts = {}, {}, []
    for folder in [ROOT, *((*ROOT, name) for name in disk.subfolders(ROOT))]:
        for name in disk.names(folder, ".vcf"):
            path = (*folder, name)
            data = disk.read(path)
            if data is None:
                raise RuntimeError("iPod 联系人文件在同步期间消失。")
            entries = vcf.parse(data, source=(serial, path))
            spans = list(vcf.records(data))
            if len(spans) != len(entries):
                raise ValueError("联系人文件边界与联系人数量不一致。")
            files[path] = data, entries, spans
            for index, entry in enumerate(entries):
                uid = vcf.uid(entry)
                if uid in device:
                    raise ValueError("iPod 的多个联系人包含相同 UID。")
                device[uid] = path, index, entry

    updated = {}
    occupied = {tuple(part.casefold() for part in path) for path in files}
    for uid in sorted(cards.keys() | device.keys()):
        path, index, remote = device.get(uid, (_file(uid), None, None))
        local = cards.get(uid)
        if local is None or (remote is not None and vcf.canonical(local) == vcf.canonical(remote)):
            winner = remote
        elif remote is None:
            winner = local
        elif fingerprint(vcf.canonical(local)) == base.get(uid):
            winner = remote
        elif fingerprint(vcf.canonical(remote)) == base.get(uid):
            winner = local
        else:
            winner, loser = ((local, remote) if vcf.revision(local) > vcf.revision(remote)
                             else (remote, local))
            name = store.conflict((serial, uid), vcf.serialize([loser]), "vcf")
            conflicts.append(f"{vcf.title(winner)} · {name}")
        cards[uid] = winner
        if winner != remote:
            if index is None:
                folded = tuple(part.casefold() for part in path)
                if folded in occupied:
                    raise ValueError("iPod 联系人文件名发生冲突。")
                occupied.add(folded)
                updated[path] = [winner]
            else:
                changed = updated.setdefault(path, list(files[path][1]))
                changed[index] = winner
    for path, (before, originals, spans) in files.items():
        entries = updated.pop(path, originals)
        after = bytearray()
        offset = 0
        for (start, end), original, entry in zip(spans, originals, entries, strict=True):
            after.extend(before[offset:start])
            after.extend(vcf.rewrite(before[start:end], original, entry))
            offset = end
        after.extend(before[offset:])
        disk.write(path, before, bytes(after))
    for path, entries in updated.items():
        disk.write(path, None, vcf.serialize(entries))

    store.write(LIBRARY, cards)
    store.write(key, {uid: fingerprint(vcf.canonical(card)) for uid, card in cards.items()})
    return cards, conflicts
