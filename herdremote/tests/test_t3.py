"""T3 tests use a loopback fake server, private files and an in-memory player."""
import base64
from http import HTTPStatus
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import herdremote
import hragent
import hrproto
import hrt3


UTC = "2026-11-20T10:01:00.000Z"
SID = "4071c2ab-e1ba-4d82-aef3-2f6bbf68b699"
CLAIMS = {"kind": "session", "method": "bearer-access-token", "sid": SID,
          "scopes": ["orchestration:read", "orchestration:operate", "diagnostics:read"]}
TOKEN = base64.urlsafe_b64encode(json.dumps(CLAIMS).encode()).decode().rstrip("=") + ".fake-signature"


def project(identity="project", **fields):
    return dict(id=identity, title="Project " + identity, workspaceRoot="/fake/project",
                updatedAt=UTC, **fields)


def thread(identity="thread", **fields):
    return dict({"id": identity, "projectId": "project", "title": "Thread " + identity,
                 "status": "idle", "updatedAt": UTC, "providerInstanceId": "codex",
                 "modelSelection": {"instanceId": "codex", "model": "gpt-6.2-sol"},
                 "activeRunId": None, "branch": "main", "pendingRuntimeRequest": None,
                 "latestVisibleMessage": None, "archivedAt": None,
                 "settledOverride": None, "deletedAt": None}, **fields)


def request(identity, kind="user_input", **fields):
    return dict({"id": identity, "kind": kind, "createdAt": UTC, "status": "pending"}, **fields)


def user_input(identity, **fields):
    return dict(id="turn-" + identity, type="user_input_request", requestId=identity,
                questions=[dict({"id": "language", "question": "Pick a language",
                                 "options": [{"label": "English", "value": "en"}]}, **fields)])


def message(identity, role, content, timestamp=UTC):
    return dict(id=identity, role=role, text=content, createdAt=timestamp)


def detail(messages=(), requests=(), items=()):
    def index(rows):
        return {row["id"]: row for row in rows}
    return dict(messages=index(messages), runtimeRequests=index(requests), turnItems=index(items))


class FakeServer:
    def __init__(self, directory, version=hrt3.VERSION, malformed=False, deny=False):
        self.directory = Path(directory)
        self.runtime = self.directory / "runtime.json"
        self.requests = queue.Queue()
        self.hello = queue.Queue()
        self.connections = queue.Queue()
        self.malformed = malformed
        self.server = serve(self.handle, "127.0.0.1", 0, process_request=(
            (lambda connection, req: connection.respond(HTTPStatus.UNAUTHORIZED, "Denied"))
            if deny else None))
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.set_version(version)

    def set_version(self, version):
        port = self.server.socket.getsockname()[1]
        self.runtime.write_text(json.dumps(dict(origin=f"http://127.0.0.1:{port}",
                                                port=port, serverVersion=version)))

    @staticmethod
    def chunk(socket, rid, *values):
        socket.send(json.dumps({"_tag": "Chunk", "requestId": rid, "values": values}))

    @staticmethod
    def success(socket, rid, value):
        socket.send(json.dumps(dict(_tag="Exit", requestId=rid,
                                    exit=dict(_tag="Success", value=value))))

    def handle(self, socket):
        self.hello.put((socket.request.path, socket.request.headers.get("Authorization")))
        self.connections.put(socket)
        try:
            for data in socket:
                frame = json.loads(data)
                self.requests.put(frame)
                if frame["_tag"] != "Request":
                    continue
                rid = frame["id"]
                method = frame["tag"]
                if method == "orchestration.subscribeShell":
                    snapshot = dict(schemaVersion=2, snapshotSequence=1, projects=[project()],
                                    threads=[thread()], archivedThreads=[])
                    if self.malformed:
                        snapshot["threads"] = "invalid"
                    self.chunk(socket, rid, dict(kind="snapshot", snapshot=snapshot),
                               dict(kind="synchronized"))
                elif method == "orchestration.subscribeThread":
                    self.chunk(socket, rid, dict(kind="snapshot", snapshotSequence=1,
                        projection=dict(thread={"id": frame["payload"]["threadId"]},
                                        messages=[message("m1", "assistant", "Initial")],
                                        turnItems=[], runtimeRequests=[])))
                elif method == "orchestration.dispatchCommand":
                    assert set(frame["payload"]) == {"commandId", "threadId", "type"}
                    self.success(socket, rid, {"sequence": 2})
                elif method == "orchestration.getArchivedShellSnapshot":
                    self.success(socket, rid, dict(schemaVersion=2, projects=[], snapshotSequence=1,
                                                   threads=[thread("archived", archivedAt=UTC)]))
        except ConnectionClosed:
            pass

    def close(self):
        while not self.connections.empty():
            self.connections.get_nowait().close()
        self.server.shutdown()
        self.worker.join(3)

    def received(self, predicate):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            frame = self.requests.get(timeout=max(.01, end - time.monotonic()))
            if predicate(frame):
                return frame
        raise AssertionError("missing T3 request")


class RemoteClient(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, XDG_CONFIG_HOME=str(self.root)))

    def fake(self, **kwargs):
        fake = FakeServer(self.root, **kwargs)
        self.addCleanup(fake.close)
        return fake

    def client(self, fake):
        client = hrt3.Client(fake.runtime)
        client.set_config(True, None, TOKEN, None)
        client.start()
        self.addCleanup(client.close)
        return client

    def wait_for(self, predicate):
        end = time.monotonic() + 4
        while not predicate():
            if time.monotonic() >= end:
                self.fail("T3 state did not arrive")
            time.sleep(.01)

    def test_stream_handshake_acks_dispatch_updates_and_reconnect(self):
        fake = self.fake()
        client = self.client(fake)
        agents = hragent.Agents(client)
        self.assertTrue(fake.hello.empty())
        client.watch(True, ("project", "all", "thread"))
        self.assertEqual(fake.hello.get(timeout=3), ("/ws?orchestrationProtocol=2", "Bearer " + TOKEN))
        connection = fake.connections.get(timeout=3)
        shell = fake.received(lambda f: f.get("tag") == "orchestration.subscribeShell")
        self.assertEqual(shell["payload"], {"requestCompletionMarker": True})
        self.assertEqual(shell["headers"], [])
        fake.received(lambda f: f["_tag"] == "Ack" and f["requestId"] == shell["id"])
        stream = fake.received(lambda f: f.get("tag") == "orchestration.subscribeThread"
                               and f["payload"]["threadId"] == "thread")
        self.wait_for(lambda: "thread" in client.snapshot()[2] and "archived" in client.snapshot()[1])
        fake.chunk(connection, stream["id"], dict(kind="event", sequence=2,
            event=dict(type="message.updated", payload=message("m1", "assistant", "Changed"))))
        self.wait_for(lambda: client.snapshot()[2]["thread"]["messages"]["m1"]["text"] == "Changed")
        ask = request("live-question")
        fake.chunk(connection, shell["id"], dict(kind="thread.updated", sequence=3,
                  location="active", thread=thread(pendingRuntimeRequest=ask)))
        fake.chunk(connection, stream["id"],
                   dict(kind="event", sequence=3, event=dict(type="runtime-request.updated", payload=ask)),
                   dict(kind="event", sequence=4, event=dict(type="turn-item.updated",
                                                            payload=user_input(ask["id"]))))
        self.wait_for(lambda: bool(agents.questions(client.snapshot(), True)))
        self.assertEqual(agents.questions(client.snapshot(), True)[0]["options"],
                         [(1, "English"), (2, "Type an answer")])
        fake.chunk(connection, stream["id"], dict(kind="event", sequence=5, event=dict(
            type="runtime-request.updated", payload={**ask, "status": "resolved"})))
        fake.chunk(connection, shell["id"], dict(kind="thread.updated", sequence=5,
                  location="active", thread=thread()))
        self.wait_for(lambda: not agents.questions(client.snapshot(), True))
        alias = agents.threads.alias("thread")
        client.dispatch("thread", {"type": "thread.pin"}).result(3)
        sent = fake.received(lambda f: f.get("tag") == "orchestration.dispatchCommand")
        self.assertEqual(sent["payload"]["threadId"], "thread")
        self.assertEqual(sent["payload"]["type"], "thread.pin")
        self.assertTrue(sent["payload"]["commandId"])
        client.watch(True, (None, None, None))
        fake.received(lambda f: f["_tag"] == "Interrupt" and f["requestId"] == stream["id"])
        connection.close()
        self.assertEqual(fake.hello.get(timeout=3)[0], "/ws?orchestrationProtocol=2")
        self.wait_for(lambda: client.state()["connected"])
        self.assertEqual(agents.threads.alias("thread"), alias)
        client.watch(False, (None, None, None))
        self.assertFalse(client.state()["connected"])

    def test_future_version_decodes_or_disables_until_runtime_changes(self):
        fake = self.fake(version="9.8.7", malformed=True)
        client = self.client(fake)
        client.watch(True, (None, None, None))
        self.wait_for(lambda: client.state()["error"] == "T3 9.8.7 的接口与 AstraPod 不兼容")
        self.assertFalse(client.state()["connected"])
        self.assertEqual(client.snapshot(), ({}, {}, {}))
        fake.malformed = False
        fake.set_version("9.8.8")
        self.wait_for(lambda: client.state()["connected"])
        self.assertEqual(client.state()["server_version"], "9.8.8")

    def test_auth_argv_private_storage_and_revoke(self):
        binary = self.root / "fake-t3"
        argv_log = self.root / "calls"
        binary.write_text(f"#!{sys.executable}\n"
                          "import json, sys\n"
                          "from pathlib import Path\n"
                          f"with Path({str(argv_log)!r}).open('a') as sink:\n"
                          "    sink.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                          f"if 'issue' in sys.argv: print({TOKEN!r})\n")
        binary.chmod(0o700)
        client = hrt3.Client(self.root / "unused")
        client.configure(False, str(binary))
        self.assertEqual(client.t3_connect()["session_label"], "AstraPod")
        self.assertFalse(client.state()["enabled"])
        token_file = self.root / "podbox-herdremote" / hrt3.TOKEN_FILE
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
        self.assertEqual(token_file.read_text().strip(), TOKEN)
        client.t3_connect()
        reloaded = hrt3.Client(self.root / "unused")
        reloaded.configure(True, str(binary))
        self.assertEqual(reloaded.t3_disconnect()["session_label"], None)
        self.assertFalse(token_file.exists())
        self.assertEqual([json.loads(line) for line in argv_log.read_text().splitlines()], [
            ["auth", "session", "issue", "--scope", "orchestration:read", "--scope",
             "orchestration:operate", "--scope", "diagnostics:read", "--label", "AstraPod", "--ttl", "90d", "--token-only"],
            ["auth", "session", "revoke", SID]])

    def test_nonloopback_runtime_never_returns_bearer_endpoint(self):
        runtime = self.root / "runtime.json"
        for origin in ("http://0.0.0.0:1234", "http://example.org:1234",
                       "http://127.0.0.1:1234/other"):
            runtime.write_text(json.dumps(dict(origin=origin, port=1234, serverVersion=hrt3.VERSION)))
            with self.assertRaises(ValueError):
                hrt3.runtime_endpoint(runtime)

    def test_auth_rejection_exposes_no_session_secret(self):
        client = self.client(self.fake(deny=True))
        client.watch(True, (None, None, None))
        self.wait_for(lambda: client.state()["error"] == "T3 authentication failed")
        self.assertFalse(client.state()["connected"])
        self.assertNotIn(TOKEN, repr(client.state()))


class PlayerProtocol(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, XDG_RUNTIME_DIR=str(root), XDG_CONFIG_HOME=str(root)))
        self.daemon = herdremote.Daemon(SimpleNamespace(socket=str(root / "fake-herdr"), state=None))
        self.addCleanup(self.daemon.usage.close)
        self.daemon.herdr = Mock()
        self.daemon.voice = Mock()
        self.daemon.link = SimpleNamespace(max_message=16384)
        self.daemon.send = Mock()
        self.daemon.herdr_ok = True
        self.daemon.model_time = time.monotonic()
        self.daemon.command(b"hr\t1")
        self.daemon.command(b"qx\t1")

    def setup_focus(self, *rows):
        self.enable_t3()
        client = self.daemon.agents.client
        client.runtime = Path(os.environ["XDG_CONFIG_HOME"]) / "server-runtime.json"
        client.runtime.with_name("environment-id").write_text(SID + "\n", encoding="ascii")
        opener = self.enterContext(patch.object(hrt3.subprocess, "run"))
        self.daemon.t3_focus_worker = self.enterContext(ThreadPoolExecutor(max_workers=1))
        self.install(rows)
        return opener, [self.daemon.agents.threads.alias(row["id"]) for row in rows]

    def rows(self, kind):
        return [hrproto.decode(call.args[0]) for call in self.daemon.send.call_args_list
                if call.args[0].startswith(kind.encode() + b"\t")]

    def install(self, rows=(), details=None, projects=(None,)):
        picture = ({p["id"]: p for p in (project() if p is None else p for p in projects)},
                   {row["id"]: row for row in rows}, details or {})
        client = self.daemon.agents.client
        client.picture = picture
        client.connected = True
        return picture

    def command(self, text):
        self.daemon.command(text.encode())

    def enable_t3(self):
        self.daemon.configure_agents(hragent.config({"AGENT_CONTROL": "t3"}))
        self.command("tc\t1")

    def start_player_handshake(self):
        self.daemon.send.reset_mock()
        self.command("hr\t1")
        self.assertEqual(self.rows("M"), [[["M", "t3"]]])

    def assert_waiting_for_tc(self):
        self.daemon.send.reset_mock()
        for command in ("sub\tw1\ttab1", "det\tpane1", "ks\tpane1", "us"):
            self.command(command)
        self.daemon.push()
        self.daemon.send.assert_not_called()

    def test_player_waits_for_delayed_t3_capability(self):
        self.daemon.configure_agents(hragent.config({"AGENT_CONTROL": "t3"}))
        self.start_player_handshake()
        self.assert_waiting_for_tc()
        self.command("tc\t1")
        ask = request("ask")
        self.command("qx\t1")
        self.install((thread("waiting", pendingRuntimeRequest=ask),),
                     {"waiting": detail(requests=[ask], items=[user_input("ask")])})
        self.daemon.push()
        self.assertEqual(self.rows("W")[-1][:2], [["W", "1"], ["#", "T3"]])
        self.assertTrue(self.rows("Q")[-1][0][1].startswith("t3q"))

    def test_player_without_t3_capability_receives_no_snapshot(self):
        self.daemon.configure_agents(hragent.config({"AGENT_CONTROL": "t3"}))
        with patch.object(herdremote.time, "monotonic", return_value=100):
            self.start_player_handshake()
        with patch.object(herdremote.time, "monotonic", return_value=110):
            with self.assertLogs(herdremote.log, "INFO") as events:
                self.assert_waiting_for_tc()
            self.assertIn("T3 needs newer firmware", events.output[0])
            self.assert_waiting_for_tc()

    def test_reconnect_requires_fresh_t3_capability(self):
        self.enable_t3()
        self.install((thread("running"),))
        self.daemon.push()
        self.assertEqual(self.rows("W")[-1][1], ["#", "T3"])
        self.daemon.reset_session()
        self.command("tc\t1")  # an unsolicited capability does not open a session
        self.start_player_handshake()
        self.assert_waiting_for_tc()
        self.command("tc\t1")
        self.install()
        self.daemon.push()
        self.assertEqual(self.rows("W")[-1][1], ["#", "T3"])

    def test_mode_reload_clears_old_source_before_snapshot(self):
        self.daemon.sent_questions["legacy-question"] = b"legacy"
        self.daemon.results.put(("7", True, "Legacy answer"))
        self.daemon.subscribed = "w1"
        self.daemon.detail_pane = "pane1"
        self.daemon.send.reset_mock()
        self.daemon.configure_agents(hragent.config({"AGENT_CONTROL": "t3"}))
        self.assertEqual(self.rows("M"), [[["M", "t3"]]])
        self.assert_waiting_for_tc()
        self.command("tc\t1")
        self.install()
        self.daemon.push()
        self.assertFalse(self.rows("A") or self.rows("D") or self.rows("X") or self.rows("R"))
        self.assertEqual(self.rows("W")[-1][1], ["#", "T3"])

    def test_exclusive_sections_filters_focus_and_detail(self):
        self.daemon.model["workspaces"] = [
            dict(id=f"w{i}", label="界" * 30, focused=False, working=0, waiting=0,
                 done=0, idle=0, tab1="-", other="-") for i in range(32)]
        rows = [thread("running", status="running"), thread("idle"),
                thread("waiting", pendingRuntimeRequest=request("ask")),
                thread("done", settledOverride="settled", status="completed")]
        rows += [thread(f"old{i}", updatedAt="2020-01-01T00:00:00Z") for i in range(20)]
        projects = [None] + [project(f"p{i}") for i in range(12)]
        self.install(rows, {"running": detail(messages=[message("a", "assistant", "hello\nworld"),
                                                       message("u", "user", "continue")])}, projects)
        self.daemon.push()
        self.assertEqual(len(self.rows("W")[-1]), 33)
        self.assertNotIn(["#", "T3"], self.rows("W")[-1])
        self.enable_t3()
        self.install(rows, {"running": detail(messages=[message("a", "assistant", "hello\nworld")])}, projects)
        self.daemon.push()
        spaces = self.rows("W")[-1]
        self.assertEqual(spaces[:2], [["W", "13"], ["#", "T3"]])
        self.assertTrue(all(row[0].startswith("t3p") for row in spaces[2:]))
        self.assertEqual(len(self.rows("S")[-1]), 14)
        wid = self.daemon.agents.projects.alias("project")
        self.command(f"sub\t{wid}\tactive")
        self.daemon.push()
        self.assertEqual([r[4] for r in self.rows("T")[-1]], ["active", "all"])
        agents = self.rows("A")[-1]
        self.assertEqual(agents[0], ["A", wid, "16"])
        self.assertEqual([row[2] for row in agents[1:4]], ["b", "w", "i"])
        self.assertEqual(agents[2][-1], "hello")
        self.command(f"sub\t{wid}\tall")
        self.daemon.push()
        self.assertIn("d", [row[2] for row in self.rows("A")[-1][1:]])
        pane = self.daemon.agents.threads.alias("running")
        self.command(f"det\t{pane}")
        self.command(f"ft\t2\t{wid}\tall")
        self.command(f"vd\t3\t{pane}")
        self.daemon.link.max_message = 256
        self.daemon.send.reset_mock()
        self.daemon.push()
        self.assertEqual(self.rows("D")[-1][0][1:6],
                         [pane, "-1", "Thread running", "Running · gpt-6.2-sol", "hello world"])
        self.daemon.herdr.assert_not_called()
        self.assertFalse(self.daemon.voice.press.called)
        self.assertTrue(all(len(c.args[0]) <= 256 for c in self.daemon.send.call_args_list))
        self.daemon.configure_agents(hragent.config({"T3_CONTROL": "on"}))
        self.daemon.push()
        self.assertEqual(self.rows("W")[-1], [["W", "0"]])
        self.assertFalse(self.daemon.agents.client.state()["connected"])
        self.assertTrue(self.daemon.t3_player)

    def test_v2_reachability_preserves_picture_until_t3_returns(self):
        self.enable_t3()
        self.install([thread("running", status="running")])
        self.daemon.push()
        self.assertEqual(self.rows("T")[-1], [["T", "0", "1"]])
        self.command("tc\t2")
        self.daemon.push()
        self.assertEqual(self.rows("T")[-1], [["T", "0", "0", "1"]])
        project_id = self.daemon.agents.projects.alias("project")
        self.command(f"sub\t{project_id}\tactive")
        self.daemon.push()
        self.daemon.send.reset_mock()

        self.daemon.agents.client.set_status(False, "T3 server unavailable")
        self.daemon.push()
        self.assertEqual(self.rows("T"), [[["T", "0", "0", "0"]]])
        self.assertFalse(self.rows("W"))
        self.assertFalse(self.rows("A"))

        self.install([thread("new", status="running")])
        self.daemon.push()
        self.assertEqual(self.rows("T")[-1], [["T", "0", "0", "1"]])
        self.assertEqual(self.rows("A")[-1][0], ["A", project_id, "1"])
        self.assertEqual(self.rows("A")[-1][1][4], "Thread new")

        self.command("hr\t1")
        self.command("tc\t1")
        self.daemon.push()
        self.assertEqual(self.rows("T")[-1], [["T", "0", "1"]])

    def test_hello_announces_the_thread_list(self):
        for mode in ("herdr", "t3"):
            self.daemon.configure_agents(hragent.config({"AGENT_CONTROL": mode}))
            self.daemon.send.reset_mock()
            self.command("hr\t1")
            frames = [call.args[0] for call in self.daemon.send.call_args_list]
            self.assertEqual(frames[1:3], [hrproto.agent_mode(mode), b"TL\t1"])

    def test_thread_list_spans_projects_in_attention_order(self):
        self.enable_t3()
        ask = request("ask", createdAt="2026-11-20T09:00:00.000Z")
        rows = [thread("idle-new", updatedAt="2026-11-20T10:05:00.000Z"),
                thread("running", projectId="other", status="running",
                       updatedAt="2026-11-20T10:02:00.000Z"),
                thread("waiting", projectId="other", pendingRuntimeRequest=ask),
                thread("idle-old", updatedAt="2026-11-20T08:00:00.000Z"),
                thread("settled", settledOverride="settled", status="completed",
                       updatedAt="2026-11-20T10:09:00.000Z"),
                thread("archived-project", projectId="gone"),
                thread("deleted", deletedAt=UTC)]
        rows += [thread(f"old{i}", updatedAt="2020-01-01T00:00:00Z") for i in range(20)]
        self.install(rows, {"running": detail(messages=[message("a", "assistant", "hi\nthere")])},
                     (None, project("other"), project("gone", archivedAt=UTC)))
        alias = self.daemon.agents.threads.alias
        self.command("sub\tt3\tactive")
        self.daemon.push()
        agents = self.rows("A")[-1]
        self.assertEqual(agents[0], ["A", "t3", "16"])
        self.assertEqual([row[0] for row in agents[1:5]],
                         [alias("waiting"), alias("running"), alias("idle-new"), alias("idle-old")])
        self.assertEqual([row[5] for row in agents[1:4]],
                         ["Project other", "Project other", "Project project"])
        self.assertEqual(agents[2][6], "hi")
        names = [row[4] for row in agents[1:]]
        for gone in ("settled", "archived-project", "deleted"):
            self.assertNotIn("Thread " + gone, names)
        self.command("sub\tt3\tall")
        self.daemon.push()
        agents = self.rows("A")[-1]
        self.assertEqual(agents[1][0], alias("settled"))
        self.assertNotIn("Thread archived-project", [row[4] for row in agents[1:]])
        self.assertFalse([tabs for tabs in self.rows("T") if len(tabs[0]) == 7])

    def test_thread_list_needs_a_t3_player(self):
        self.install((thread("running", status="running"),))
        self.command("sub\tt3\tactive")
        self.daemon.push()
        self.assertFalse(self.rows("A"))
        self.enable_t3()
        self.command("sub\tt3\tlatest")
        self.daemon.push()
        self.assertFalse(self.rows("A"))

    def test_thread_focus_opens_local_link_and_project_stays_focus_free(self):
        opener, (pane,) = self.setup_focus(thread(SID, title="Build"))
        project_id = self.daemon.agents.projects.alias("project")
        self.command(f"fw\t1\t{project_id}")
        self.command(f"fp\t2\t{pane}")
        self.daemon.flush_replies()
        self.daemon.t3_focus_job[0].result(timeout=3)
        self.daemon.flush_replies()
        self.assertEqual(self.rows("R"), [[["R", "1", "ok", "Focused"]],
                                          [["R", "2", "ok", "Focused Build"]]])
        opener.assert_called_once_with(["xdg-open", f"t3code://threads/{SID}/{SID}"],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=2, check=True)
        self.daemon.herdr.focus_pane.assert_not_called()
        self.assertIsNone(self.daemon.detail_keys_pane)

    def test_thread_focus_coalesces_pending_and_discards_stale_session_reply(self):
        tids = [f"4071c2ab-e1ba-4d82-aef3-2f6bbf68b69{i}" for i in range(3)]
        opener, panes = self.setup_focus(*(thread(tid) for tid in tids))
        started, release = threading.Event(), threading.Event()

        def open_first(*args, **kwargs):
            started.set()
            release.wait(3)

        opener.side_effect = open_first
        self.command(f"fp\t1\t{panes[0]}")
        self.daemon.flush_replies()
        try:
            self.assertTrue(started.wait(2))
            first_job = self.daemon.t3_focus_job
            self.command(f"fp\t2\t{panes[1]}")
            self.command(f"fp\t3\t{panes[2]}")
            self.daemon.flush_replies()
            self.assertIs(self.daemon.t3_focus_job, first_job)
            self.assertEqual(opener.call_count, 1)
        finally:
            release.set()
        first_job[0].result(timeout=3)
        self.daemon.flush_replies()
        self.daemon.t3_focus_job[0].result(timeout=3)
        self.daemon.flush_replies()
        self.assertEqual([call.args[0][1] for call in opener.call_args_list],
                         [f"t3code://threads/{SID}/{tids[i]}" for i in (0, 2)])
        self.assertEqual([r[0][1:3] for r in self.rows("R")],
                         [["2", "err"], ["1", "ok"], ["3", "ok"]])

        self.command(f"fp\t4\t{panes[0]}")
        self.daemon.flush_replies()
        self.command(f"fp\t5\t{panes[1]}")
        self.command("hr\t1")
        self.daemon.t3_focus_job[0].result(timeout=3)
        self.daemon.flush_replies()
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(len(self.rows("R")), 3)

    def test_thread_focus_off_and_invalid_ids(self):
        opener, (pane, invalid) = self.setup_focus(thread(SID), thread("../other"))
        client = self.daemon.agents.client
        client.runtime.with_name("environment-id").write_text("not-a-uuid", encoding="ascii")
        self.daemon.agents.configure(hragent.config({"AGENT_CONTROL": "t3", "T3_FOLLOW_IPOD": "off"}))
        self.install((thread(SID), thread("../other")))
        self.command(f"fw\t1\t{pane}")
        self.daemon.flush_replies()
        self.assertEqual(self.rows("R")[-1][0][1:3], ["1", "ok"])

        self.daemon.agents.configure(hragent.config({"AGENT_CONTROL": "t3"}))
        self.install((thread(SID), thread("../other")))
        for req, target, error in ((2, invalid, "Invalid T3 thread ID"),
                                   (3, pane, "Invalid T3 environment ID")):
            self.command(f"fp\t{req}\t{target}")
            self.daemon.flush_replies()
            with self.assertRaisesRegex(ValueError, error):
                self.daemon.t3_focus_job[0].result(timeout=3)
            self.daemon.flush_replies()
            self.assertEqual(self.rows("R")[-1][0][1:], [str(req), "err", error])
        opener.assert_not_called()

    def test_thread_focus_reports_opener_failure(self):
        opener, (pane,) = self.setup_focus(thread(SID))
        for req, failure, error in (
                (1, subprocess.TimeoutExpired("xdg-open", 2), "T3 desktop open timed out"),
                (2, subprocess.CalledProcessError(1, "xdg-open"), "Could not open T3 desktop")):
            opener.side_effect = failure
            self.command(f"fp\t{req}\t{pane}")
            self.daemon.flush_replies()
            with self.assertRaisesRegex(ValueError, error):
                self.daemon.t3_focus_job[0].result(timeout=3)
            self.daemon.flush_replies()
            self.assertEqual(self.rows("R")[-1][0][1:], [str(req), "err", error])

    def test_action_sheet_executes_only_bounded_advertised_actions(self):
        self.daemon.model["panes"] = {"p1"}
        self.daemon.model["pane_meta"] = {"p1": {"zoomed": True}}
        for req, action in enumerate(("interrupt", "unzoom", "reply1"), 1):
            self.command("ks\tp1")
            self.assertEqual(self.rows("K")[-1][:3],
                             [["K", "p1", "6"], ["interrupt", "Interrupt", "1"], ["unzoom", "Unzoom", "0"]])
            self.command(f"act\t{req}\tp1\t{action}")
        self.daemon.herdr.send_keys.assert_called_once_with("p1", ["esc"])
        self.daemon.herdr.call.assert_any_call("pane.zoom", pane_id="p1", mode="off")
        self.daemon.herdr.call.assert_any_call("pane.send_input", pane_id="p1", text="Continue", keys=["enter"])
        self.command("act\t4\tp1\treply1")
        self.assertEqual(self.rows("R")[-1][0][2], "err")
        for invalid in (b"ks\t", b"ks\tp\nextra", b"act\t1\tp1\tx\ny", b"act\t12345678\tp1\tzoom"):
            self.assertIsNone(hrproto.parse_command(invalid))
        actions = [("reply1", "界" * 30, False)] * 15
        sheet = hrproto.actions("p1", actions)
        self.assertEqual(hrproto.decode(sheet)[0][-1], "12")
        self.assertLessEqual(len(hrproto.decode(sheet)[1][1].encode()), 40)

    def test_T3_actions_and_run_identity(self):
        self.enable_t3()
        row = thread(activeRunId="run-1", status="running", pinnedAt=UTC)
        picture = self.install([row])
        pane = self.daemon.agents.threads.alias("thread")
        client = self.daemon.agents.client
        client.dispatch = Mock(side_effect=lambda *args: finished())
        for i, action in enumerate(("interrupt", "settle", "snooze1h", "tomorrow",
                                    "unpin", "reply1"), 1):
            self.command(f"ks\t{pane}")
            self.command(f"act\t{i}\t{pane}\t{action}")
        self.daemon.push()
        commands = [c.args[1] for c in client.dispatch.call_args_list]
        self.assertEqual([c["type"] for c in commands], [
            "run.interrupt", "thread.settle", "thread.snooze", "thread.snooze", "thread.unpin",
            "message.dispatch"])
        self.assertEqual(commands[0]["runId"], "run-1")
        self.assertTrue(commands[0]["holdQueue"])
        self.assertEqual(commands[-1]["dispatchMode"], {"type": "queue_after_active"})
        self.assertIn([["R", "6", "ok", "Queued"]], self.rows("R"))
        self.assertEqual(hragent.action_command(row, "snooze1h", now=datetime(
            2026, 11, 20, 12, tzinfo=timezone.utc))["snoozedUntil"], "2026-11-20T13:00:00Z")
        self.assertEqual(hragent.action_command(row, "reopen"), {"type": "thread.unsettle", "reason": "user"})
        self.command(f"ks\t{pane}")
        picture[1]["thread"] = thread(activeRunId="run-2")
        self.command(f"act\t7\t{pane}\tinterrupt")
        self.assertEqual(self.rows("R")[-1][0][2:], ["err", "The active T3 turn has changed"])
        self.assertEqual(client.dispatch.call_count, 6)

    def test_question_order_approval_custom_multi_and_dismiss(self):
        daemon = self.daemon
        self.daemon.model["questions"] = {"herdr-q": dict(id="herdr-q", pane="p1", workspace="w1",
            multi=False, agent="herdr", lines=["Newer"], options=[(1, "OK")])}
        daemon.order.panes["p1"] = {"asked": hragent.instant(UTC) + 10}
        daemon.push()
        self.enable_t3()
        asks = [request("approval", kind="permission"), request("question"), request("multi")]
        approval = dict(type="approval_request", id="approval-item", requestId="approval",
                        prompt="Run a command?", options=[{"decision": "decline", "label": "Deny"},
                                                          {"decision": "acceptAlways", "label": "Always"}])
        data = detail(requests=asks, items=[approval, user_input("question"),
                                          user_input("multi", multiSelect=True)])
        self.install([thread(pendingRuntimeRequest=asks[0])], {"thread": data})
        client = daemon.agents.client
        client.dispatch = Mock(side_effect=lambda *args: finished())
        daemon.push()
        qids = {frame[1][1]: frame[0][1] for frame in self.rows("Q")[1:]}
        self.assertNotIn("herdr-q", [frame[0][1] for frame in self.rows("Q")[1:]])
        self.assertIn([["M", "t3"]], self.rows("M"))
        self.assertNotIn([["X", "herdr-q"]], self.rows("X"))
        aid = qids["Run a command?"]
        approve = next(q for q in self.rows("Q") if q[0][1] == aid)
        self.assertEqual(approve[2:], [["o", "1", "Deny", "-"], ["o", "2", "Always", "-"]])
        self.command(f"a\t1\t{aid}\t2")
        pane = daemon.agents.threads.alias("thread")
        self.command(f"det\t{pane}")
        qid = daemon.agents.qids.alias(("thread", "question", "language"))
        self.command(f"a\t2\t{qid}\t2")
        self.assertEqual(self.rows("R")[-1][0][3], "Input ready")
        self.command(f"qi\t3\t{qid}\tenter\t日本語")
        multi_id = daemon.agents.qids.alias(("thread", "multi", "language"))
        self.command(f"a\t4\t{multi_id}\t1")
        self.command(f"a\t5\t{multi_id}\t0")
        # Only one in-flight answer per request. A newly delivered pending
        # question remains dismissible independently of the existing ones.
        asks.append(request("dismiss"))
        data["runtimeRequests"]["dismiss"] = asks[-1]
        data["turnItems"]["turn-dismiss"] = user_input("dismiss")
        daemon.push()
        dismiss_id = daemon.agents.qids.alias(("thread", "dismiss", "language"))
        self.command(f"a\t6\t{dismiss_id}\t-2")
        daemon.push()
        self.assertEqual([c.args[1] for c in client.dispatch.call_args_list], [
            dict(type="runtime-request.respond", requestId="approval", decision="acceptAlways"),
            dict(type="runtime-request.respond", requestId="question", answers={"language": "日本語"}),
            dict(type="runtime-request.respond", requestId="multi", answers={"language": ["en"]}),
            dict(type="thread.user-input.dismiss", requestId="dismiss")])
        self.assertIn([["X", dismiss_id]], self.rows("X"))
        daemon.herdr.assert_not_called()


def finished():
    future = Future()
    future.set_result(None)
    return future
