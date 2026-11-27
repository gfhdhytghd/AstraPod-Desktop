from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from threading import Thread
from xml.sax.saxutils import escape

import pytest

from astrapod_desktop import calendar_format as ics, calendar_sync, contact_format as vcf, contact_sync
from astrapod_desktop.caldav import Client
from astrapod_desktop.extras_storage import PlayerDisk, Store
from astrapod_desktop import backends  # Load the desktop's HerdRemote module path.
from hrdisksync import MediaDisk


def entry(uid, summary, day="27", kind="VEVENT"):
    end = ("DTSTART:20261128T120000Z" if kind == "VEVENT" else "DUE:20261128T120000Z")
    return "\r\n".join((
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Fixture//EN",
        f"BEGIN:{kind}", f"UID:{uid}", f"SUMMARY:{summary}", end,
        "DTSTAMP:20261101T120000Z", f"LAST-MODIFIED:202611{day}T120000Z",
        f"SEQUENCE:{int(day)}", f"END:{kind}", "END:VCALENDAR", "",
    ))


class DavFixture:
    collection = "/home/combined/"
    original = collection + "event.ics"

    def __init__(self):
        self.resources = {}
        self.changes = []
        self.calls = []
        self.tokens = True
        self.ctags = True
        self.invalid_token = False
        self.before_put = None
        self.after_put = None
        self.save(self.original, entry("one", "initial", "22"))

    def save(self, path, body):
        self.changes.append(path)
        if body is None:
            del self.resources[path]
        else:
            self.resources[path] = (f'"{len(self.changes)}"', body)

    @staticmethod
    def response(path, properties=""):
        if properties:
            result = (f"<d:propstat><d:prop>{properties}</d:prop>"
                      "<d:status>HTTP/1.1 200 OK</d:status></d:propstat>")
        else:
            result = "<d:status>HTTP/1.1 404 Not Found</d:status>"
        return f"<d:response><d:href>{escape(path)}</d:href>{result}</d:response>"

    def etag(self, path):
        return f"<d:getetag>{self.resources[path][0]}</d:getetag>" if path in self.resources else ""

    def multistatus(self, *rows):
        return (207, ('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
                      'xmlns:s="http://calendarserver.org/ns/">' + "".join(rows)
                      + "</d:multistatus>"), {})

    def handle(self, method, path, headers, body):
        assert headers.get("Authorization") == "Basic dGVzdDpmaXh0dXJl"
        self.calls.append((method, path, headers.get("If-Match"), headers.get("If-None-Match")))
        token = f"<d:sync-token>{len(self.changes)}</d:sync-token>"
        if method == "PROPFIND":
            if path == "/":
                return 404, "", {}
            if path == "/.well-known/caldav":
                return 301, "", {"Location": "/dav/"}
            if path == "/dav/":
                return self.multistatus(self.response(path, """
                    <d:current-user-principal><d:href>/principal/</d:href></d:current-user-principal>
                """))
            if path == "/principal/":
                return self.multistatus(self.response(path, """
                    <c:calendar-home-set><d:href>/home/</d:href></c:calendar-home-set>
                """))
            if path == "/home/":
                return self.multistatus(self.response(self.collection, """
                    <d:displayname>Fixture</d:displayname>
                    <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
                    <c:supported-calendar-component-set><c:comp name="VEVENT"/><c:comp name="VTODO"/>
                    </c:supported-calendar-component-set>
                """))
            if headers["Depth"] == "0":
                ctag = f"<s:getctag>{len(self.changes)}</s:getctag>" if self.ctags else ""
                metadata = (token if self.tokens else "") + ctag
                return self.multistatus(self.response(path, metadata))
            return self.multistatus(self.response(path, "<d:resourcetype><d:collection/></d:resourcetype>"),
                                    *(self.response(href, self.etag(href)) for href in self.resources))
        if method == "REPORT":
            if self.invalid_token:
                return 403, "", {}
            from xml.etree import ElementTree
            previous = int(ElementTree.fromstring(body).findtext("{DAV:}sync-token"))
            return self.multistatus(token, *(self.response(href, self.etag(href))
                                            for href in dict.fromkeys(self.changes[previous:])))
        if method == "GET":
            if path not in self.resources:
                return 404, "", {}
            etag, data = self.resources[path]
            return 200, data, {"ETag": etag}
        if method == "PUT" and self.before_put:
            update, self.before_put = self.before_put, None
            self.save(path, update)
        existing = self.resources.get(path)
        if existing and headers.get("If-Match") != existing[0]:
            return 412, "", {}
        if existing is None and headers.get("If-None-Match") != "*":
            return 412, "", {}
        self.save(path, body if method == "PUT" else None)
        reply_headers = {"ETag": self.resources[path][0]} if method == "PUT" else {}
        if method == "PUT" and self.after_put:
            update, self.after_put = self.after_put, None
            self.save(path, update)
        return (204 if existing else 201), "", reply_headers


@pytest.fixture
def dav():
    model = DavFixture()

    class Handler(BaseHTTPRequestHandler):
        def dispatch(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            status, reply, headers = model.handle(self.command, self.path, self.headers, body)
            data = reply.encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        do_PROPFIND = do_REPORT = do_GET = do_PUT = do_DELETE = dispatch

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield model, Client(f"http://127.0.0.1:{server.server_port}", "test", "fixture")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def player(tmp_path):
    root = tmp_path / "player"
    root.mkdir()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield root, PlayerDisk(MediaDisk(fd, lambda: None)), Store(tmp_path / "private")
    finally:
        os.close(fd)


def test_calendar_two_way_incremental_and_conflicts(dav, player):
    model, client = dav
    root, disk, store = player
    (collection,) = client.discover()
    assert collection.kinds == ("VEVENT", "VTODO")
    assert ("/.well-known/caldav" in {call[1] for call in model.calls})

    def sync(targets=None):
        return calendar_sync.sync(client, store, disk, "player", "account", [collection], targets or {})

    assert sync() == (1, 0, [])
    (export,) = (root / "Calendars").glob("AstraPod-DAV-*.ics")
    assert "SUMMARY:initial" in export.read_text()
    start = len(model.calls)
    sync()
    assert [call[0] for call in model.calls[start:]] == ["PROPFIND"]

    model.save(model.original, entry("one", "server edited", "23"))
    sync()
    assert "REPORT" in [call[0] for call in model.calls[start:]]
    assert "SUMMARY:server edited" in export.read_text()

    export.write_text(entry("one", "device edited", "25"))
    model.save(model.original, entry("one", "losing server edit", "24"))
    inbox = export.with_name("AstraPod.ics")
    inbox.write_text(entry("task-one", "from iPod", kind="VTODO"))
    current_etag = model.resources[model.original][0]
    result = sync({"VTODO": collection.url})
    assert result[:2] == (1, 0)
    assert len(result[2]) == 1
    assert ("PUT", model.original, current_etag, None) in model.calls
    assert any(call[0] == "PUT" and call[3] == "*" for call in model.calls)
    assert "losing server edit" in next(store.root.glob("conflict-*.ics")).read_text()
    assert "SUMMARY:device edited" in model.resources[model.original][1]
    assert set(ics.split(export.read_bytes())) == {"one", "task-one"}
    assert ics.split(inbox.read_bytes()) == {}
    start = len(model.calls)
    assert sync() == (1, 0, [])
    assert not any(call[0] == "PUT" for call in model.calls[start:])

    exported = ics.split(export.read_bytes())
    exported["one"] = entry("one", "losing device edit", "26")
    export.write_bytes(ics.merge(exported))
    model.save(model.original, entry("one", "server wins", "27"))
    assert len(sync()[2]) == 1
    assert any("losing device edit" in copy.read_text()
               for copy in store.root.glob("conflict-*.ics"))
    assert "SUMMARY:server wins" in export.read_text()

    exported = ics.split(export.read_bytes())
    del exported["one"]
    export.write_bytes(ics.merge(exported))
    current_etag = model.resources[model.original][0]
    sync()
    assert ("DELETE", model.original, current_etag, None) in model.calls
    assert model.original not in model.resources


def test_ctag_etag_and_invalid_token_fallback(dav):
    model, client = dav
    (collection,) = client.discover()
    old = client.snapshot(collection.url, {})
    model.invalid_token = True
    task = model.collection + "task.ics"
    model.save(task, entry("two", "task", kind="VTODO"))
    model.calls.clear()
    snapshot = client.snapshot(collection.url, old)
    assert {call[0] for call in model.calls} == {"PROPFIND", "GET", "REPORT"}
    assert [call[1] for call in model.calls if call[0] == "GET"] == [task]
    model.tokens = False
    model.save(task, entry("two", "changed", kind="VTODO"))
    snapshot = client.snapshot(collection.url, snapshot)
    assert snapshot["token"] is None and snapshot["ctag"]
    model.calls.clear()
    assert client.snapshot(collection.url, snapshot) == snapshot
    assert [call[0] for call in model.calls] == ["PROPFIND"]
    model.ctags = False
    model.calls.clear()
    assert set(client.snapshot(collection.url, snapshot)["resources"]) == {client.url + task, client.url + model.original}
    assert [call[0] for call in model.calls] == ["PROPFIND", "PROPFIND"]


@pytest.mark.parametrize(("race", "error"), [
    ("before_put", "同步期间发生变化"),
    ("after_put", "写入后收到另一处编辑"),
])
def test_conditional_race_keeps_device_edit_for_retry(dav, player, race, error):
    model, client = dav
    root, disk, store = player
    (collection,) = client.discover()

    def sync():
        return calendar_sync.sync(client, store, disk, "player", "account", [collection], {})

    sync()
    (export,) = (root / "Calendars").glob("*.ics")
    export.write_text(entry("one", "my edit", "24"))
    setattr(model, race, entry("one", "concurrent edit", "25"))
    with pytest.raises(RuntimeError, match=error):
        sync()
    assert "my edit" in export.read_text()
    assert "concurrent edit" in model.resources[model.original][1]
    assert len(sync()[2]) == 1
    assert "concurrent edit" in export.read_text()


def test_ical_round_trip_with_recurrence_timezone_and_alarm():
    data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VTIMEZONE
TZID:Fixture/Zone
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0100
TZOFFSETTO:+0100
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:weekly
DTSTAMP:20261101T000000Z
DTSTART;TZID=Fixture/Zone:20261125T103000
RRULE:FREQ=WEEKLY;COUNT=2
SUMMARY:日程\\,会议
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT10M
DESCRIPTION:提醒
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:weekly
RECURRENCE-ID;TZID=Fixture/Zone:20261202T103000
DTSTAMP:20261101T000000Z
DTSTART;TZID=Fixture/Zone:20261202T113000
END:VEVENT
END:VCALENDAR
"""
    items = ics.split(data)
    packed = ics.merge(items)
    assert set(items) == {"weekly"}
    assert ics.split(packed) == items
    for marker in (b"RRULE", b"RECURRENCE-ID", b"Fixture/Zone", b"VALARM"):
        assert marker in packed


CARDS = (
    b"BEGIN:VCARD\r\nVERSION:2.1\r\nN:Doe;Jane;;;\r\n"
    b"FN;CHARSET=ISO-8859-1;ENCODING=QUOTED-PRINTABLE:J=E4=\r\nne Doe\r\n"
    b"TEL;HOME;VOICE:+1-555-0100\r\nUID:one\r\nPHOTO;TYPE=JPEG;ENCODING=BASE64:\r\n"
    b"/9j/\r\n2Q==\r\n\r\nEND:VCARD\r\n"
    b"BEGIN:VCARD\r\nVERSION:4.0\r\nFN:Joe Doe\r\nTEL;VALUE=uri:tel:+1-555-0101\r\n"
    b"PHOTO:data:image/png;base64,YWJj\r\nUID:two\r\nEND:VCARD\r\n"
)


def test_vcard_versions_photos_and_desktop_edit():
    jane, joe = vcf.parse(CARDS)
    assert vcf.title(jane) == "Jäne Doe"
    assert vcf.values(jane, "PHOTO") == ["/9j/2Q=="]
    assert vcf.values(joe, "PHOTO") == ["YWJj"]
    assert vcf.values(joe, "TEL") == ["+1-555-0101"]
    changes = {"FN": ["小明" * 40], "N": ["Doe;Jane;;;"], "TEL": ["+1-555-0200", "+1-555-0300"],
               "EMAIL": ["demo@example.invalid"], "ADR": [";;1 Main;City;State;000;US"],
               "ORG": ["School;Lab"], "BDAY": ["2000-01-02"], "NOTE": ["line 1\nline 2;test"]}
    edited = vcf.edit(jane, changes)
    output = vcf.serialize([edited, joe])
    assert all(len(line) <= 75 for line in output.splitlines())
    assert vcf.parse(output) == [edited, joe]
    for field, values in changes.items():
        assert vcf.values(edited, field, visible=True) == values
    assert ("TEL;TYPE=HOME;TYPE=VOICE", "+1-555-0200") in edited
    assert vcf.values(edited, "PHOTO") == vcf.values(jane, "PHOTO")


def test_contact_sync_preserves_legacy_files_and_picks_up_device_edits(player, tmp_path):
    root, disk, store = player
    legacy = root / "Contacts" / "Legacy.vcf"
    legacy.parent.mkdir()
    legacy.write_bytes(CARDS)
    local_card = vcf.edit(vcf.new(), {"FN": ["New desktop contact"]})
    filename = tmp_path / "import.vcf"
    filename.write_bytes(vcf.serialize([local_card]))
    cards, conflicts = contact_sync.import_files(store, [filename])
    assert len(cards) == 1 and not conflicts
    cards, conflicts = contact_sync.sync(store, disk, "player")
    assert len(cards) == 3 and not conflicts and legacy.read_bytes() == CARDS
    target = root / "Contacts" / "AstraPod" / (vcf.uid(local_card) + ".vcf")
    assert vcf.parse(target.read_bytes()) == [local_card]

    updated = vcf.edit(local_card, {"NOTE": ["changed on device"]})
    target.write_bytes(vcf.serialize([updated]))
    cards, conflicts = contact_sync.sync(store, disk, "player")
    assert cards[vcf.uid(local_card)] == updated and not conflicts
    edited = vcf.edit(cards["one"], {"EMAIL": ["changed@example.invalid"]})
    contact_sync.save(store, edited, cards["one"])
    contact_sync.sync(store, disk, "player")
    assert vcf.parse(legacy.read_bytes()) == [edited, cards["two"]]


def test_contact_subfolder_and_uidless_record_keeps_siblings_byte_exact(player):
    root, disk, store = player
    path = root / "Contacts" / "Work" / "Legacy.VCF"
    path.parent.mkdir(parents=True)
    legacy = b"BEGIN:VCARD\nVERSION:3.0\nFN:Fixture\nN:Fixture;;;;\nX-KEEP: raw\nEND:VCARD\n"
    sibling = CARDS[CARDS.index(b"BEGIN:VCARD\r\nVERSION:4.0"):]
    path.write_bytes(legacy + b"\n# comment\n" + sibling)
    cards, _ = contact_sync.sync(store, disk, "player")
    (uid,) = set(cards) - {"two"}
    assert b"UID:" + uid.encode() in path.read_bytes()
    assert b"X-KEEP: raw\n" in path.read_bytes()
    assert path.read_bytes().endswith(sibling)

    updated = vcf.edit(cards["two"], {"TEL": ["+1-555-0111"], "BDAY": ["2000-01-02"]})
    contact_sync.save(store, updated, cards["two"])
    _, conflicts = contact_sync.sync(store, disk, "player")
    assert not conflicts
    raw = path.read_bytes()
    assert b"VERSION:4.0\r\n" in raw and b"BDAY:20000102\r\n" in raw
    assert b"PHOTO:data:image/png;base64,YWJj\r\n" in raw
    assert b"X-KEEP: raw\n" in raw
    assert contact_sync.sync(store, disk, "player")[1] == []
    assert path.read_bytes() == raw


def test_calendar_inbox_preserves_unassigned_bytes():
    day = entry("day", "export").encode()
    task = (
        b"BEGIN:VTODO\nUID:task\nSUMMARY:keep me\n"
        b"DESCRIPTION:first\n continuation\nEND:VTODO\n"
    )
    inbox = day.replace(b"END:VCALENDAR", task + b"END:VCALENDAR")
    result = ics.remove_inbox_items(inbox, {"day"})
    assert set(ics.split(result)) == {"task"}
    assert task in result


def test_calendar_recovers_after_device_publish_before_state_commit(dav, player, monkeypatch):
    model, client = dav
    root, disk, store = player
    (collection,) = client.discover()
    model.save(model.collection + "second.ics", entry("second", "other event"))

    def sync():
        return calendar_sync.sync(client, store, disk, "player", "account", [collection], {})

    sync()
    (export,) = (root / "Calendars").glob("*.ics")
    model.save(model.original, None)
    save = store.write

    def interrupt(name, value):
        if name.startswith("calendar-") and "pending" not in value:
            raise OSError("interrupted after publish")
        save(name, value)

    with monkeypatch.context() as patch:
        patch.setattr(store, "write", interrupt)
        with pytest.raises(OSError, match="interrupted after publish"):
            sync()
    assert set(ics.split(export.read_bytes())) == {"second"}

    model.save(model.original, entry("one", "recreated on server"))
    sync()
    assert set(ics.split(export.read_bytes())) == {"second", "one"}
    assert "recreated on server" in export.read_text()


def test_keyring_uses_stdin_for_password(monkeypatch):
    from types import SimpleNamespace
    from astrapod_desktop import extras_storage

    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="fixture-password\n" if args[1] == "lookup" else "")

    monkeypatch.setattr(extras_storage.subprocess, "run", run)
    extras_storage.secret("account-id", "fixture-password")
    assert extras_storage.secret("account-id") == "fixture-password"
    assert "fixture-password" not in calls[0][0]
    assert calls[0][1]["input"] == "fixture-password"
