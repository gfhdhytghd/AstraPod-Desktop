"""Desktop JSON lines over private sockets with fake host services."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import socket
import stat
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import herdremote
import hrdisk
import hrdesktop
import hrherdr
import hrproto
from hrstockdevice import DeviceState
import hrusage


class DesktopAPI(unittest.TestCase):
    def setUp(self):
        runtime = TemporaryDirectory(dir=os.environ.get("XDG_RUNTIME_DIR"))
        self.addCleanup(runtime.cleanup)
        self.enterContext(patch.dict(os.environ, XDG_RUNTIME_DIR=runtime.name))
        self.device = self.enterContext(patch.object(hrdesktop, "current_device",
                                                     return_value=DeviceState("none")))
        self.make_bridge = self.enterContext(patch.object(hrdesktop, "make_bridge",
            side_effect=AssertionError("unexpected iPod bridge startup")))
        self.daemon = herdremote.Daemon(SimpleNamespace(socket=runtime.name + "/fake-herdr",
                                                       state=None))
        self.daemon.usage.close()
        self.daemon.usage = Mock(snapshot=Mock(return_value=[hrusage.row("codex", 10)]))
        self.daemon.voice_worker = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.daemon.voice_worker.shutdown)
        self.daemon.voice.bus = Mock()
        self.server = self.daemon.desktop
        self.server.start()
        self.addCleanup(self.server.close)
        self.first = self.connect()
        self.daemon.herdr_ok = True
        self.daemon.model.update(
            workspaces=[dict(id="w1", label="Work", tab="t1", working=1,
                             waiting=0, done=0, idle=0)],
            tabs={"w1": [dict(id="t1", label="First"), dict(id="t2", label="Second")]},
            tab_agents={"t1": [dict(pane="p1", name="Ada", kind="codex", status="working", last="Task")]},
            panes={"p1"},
            questions={"q1": dict(id="q1", pane="p1", workspace="w1", agent="Ada",
                                  lines=["Continue?"], options=[(1, "Yes")], multi=False)})

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(self.server.path))
        client.setblocking(False)
        self.addCleanup(client.close)
        return SimpleNamespace(socket=client, buffer=b"")

    def step(self):
        self.daemon.flush_replies()
        self.daemon.poll_desktop()

    def receive(self, client=None, timeout=1):
        client = client or self.first
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b"\n" in client.buffer:
                line, client.buffer = client.buffer.split(b"\n", 1)
                return json.loads(line)
            self.step()
            try:
                client.buffer += client.socket.recv(65536)
            except BlockingIOError:
                time.sleep(.001)
        self.fail("No desktop response")

    def send(self, op, client=None, request_id=7, **args):
        (client or self.first).socket.sendall(hrdesktop.encode(dict(id=request_id, op=op, **args)))

    def call(self, op, client=None, **args):
        self.send(op, client, **args)
        reply = self.receive(client)
        self.assertEqual(reply["id"], 7)
        return reply

    def subscribe(self, client=None, **args):
        self.assertEqual(self.call("subscribe", client, **args),
                         dict(id=7, ok=True, result=None))
        return self.receive(client)["state"]

    def test_stock_link_state_has_no_podlink_port(self):
        self.device.return_value = DeviceState("stock", serial="APPLE")
        self.assertEqual(self.subscribe()["link"], dict(connected=True, mode="stock",
                         port=None, serial="APPLE", borrowed=False, firmware=None))

    def test_hello_multiple_clients_unknown_and_utf8_lines(self):
        second = self.connect()
        self.first.socket.sendall(b'{"id":12,"op":"hel')
        for _ in range(3):
            self.step()
        self.first.socket.sendall(b'lo"}\n{"id":13,"op":"unknown"}\n')
        self.send("hello", second, request_id=19)
        reply = self.receive()
        self.assertEqual(reply, dict(id=12, ok=True, result=dict(version=1, ops=list(hrdesktop.OPS))))
        self.assertEqual(self.receive(), dict(id=13, ok=False, error="Unknown op: unknown"))
        self.assertEqual(self.receive(second)["id"], 19)
        self.first.socket.sendall(b'{"id":true,"op":"hello"}\n')
        self.assertFalse(self.receive()["ok"])

    def test_state_subscription_change_coalescing_and_logs(self):
        clock = time.monotonic()
        with patch.object(time, "monotonic", side_effect=lambda: clock):
            original = self.subscribe()
            self.assertEqual(set(original), {"link", "agents", "herdr", "questions", "usage",
                                             "voice", "input", "settings_revision", "t3"})
            self.assertEqual(original["agents"], {"control": "herdr"})
            self.assertEqual(original["t3"], dict(enabled=False, connected=False,
                                                  server_version=None, error=None,
                                                  session_label=None, threads=[]))
            self.assertIsNone(original["link"]["firmware"])
            self.make_bridge.assert_not_called()
            self.assertEqual(original["herdr"]["workspaces"][0]["tabs"][0]["agents"][0]["activity"], "Task")
            self.assertEqual(original["questions"][0]["options"], [dict(number=1, label="Yes")])
            self.assertEqual(original["input"], dict(ir_fallback="pending", plugins=[]))

            second = self.connect()
            self.subscribe(second, logs=True)
            self.daemon.set_input_plugins([dict(id="one")])
            self.daemon.set_input_plugins([dict(id="two")])
            self.daemon.ir_fallback.mode = "ipod"
            logging.getLogger("herdremote.desktop").warning("test desktop log")
            self.assertIn("test desktop log", self.receive(second)["line"])
            for _ in range(5):
                self.step()
            with self.assertRaises(BlockingIOError):
                self.first.socket.recv(4096)
            clock += .25
            for client in (self.first, second):
                state = self.receive(client)["state"]
                self.assertEqual(state["input"], dict(ir_fallback="ipod", plugins=[dict(id="two")]))
            clock += .25
            for _ in range(5):
                self.step()
            with self.assertRaises(BlockingIOError):
                self.first.socket.recv(4096)

    def test_T3_session_operations_use_the_client(self):
        client = self.daemon.agents.client
        self.daemon.herdr_ok = False
        for op, label in (("t3_connect", "AstraPod"), ("t3_disconnect", None)):
            state = {**client.state(), "session_label": label}
            with patch.object(client, op, return_value=state) as action:
                self.assertEqual(self.call(op)["result"], state)
                action.assert_called_once_with()
        self.assertFalse(self.call("t3_send")["ok"])

    def test_permissions_stale_socket_and_owner(self):
        self.assertEqual(stat.S_IMODE(self.server.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.server.path.parent.stat().st_mode), 0o700)
        other = hrdesktop.Server(self.daemon, self.server.path)
        with self.assertRaisesRegex(OSError, "in use"):
            other.start()
        self.call("hello")
        with socket.socket(socket.AF_UNIX) as stale:
            path = self.server.path.with_name("stale.sock")
            stale.bind(str(path))
        replacement = hrdesktop.Server(self.daemon, path)
        replacement.start()
        replacement.close()
        self.assertFalse(path.exists())

    def test_focus_ops_share_focus_checks_and_use_worker(self):
        with patch.object(hrherdr.Herdr, "call") as call:
            call.side_effect = lambda method, **kw: (
                {"capabilities": {"client_targeted_focus": True}} if method == "ping" else {"moved": ["local"]})
            for op, args, target in (
                ("focus_workspace", dict(workspace="w1"), dict(workspace_id="w1", tab_id="t1")),
                ("focus_tab", dict(workspace="w1", tab="t2"), dict(workspace_id="w1", tab_id="t2")),
                ("focus_pane", dict(pane="p1"), dict(pane_id="p1")),
            ):
                with self.subTest(op=op):
                    self.assertTrue(self.call(op, **args)["ok"])
                    call.assert_called_with("client.focus", clients="local", **target)
            self.assertFalse(self.call("focus_tab", workspace="w1", tab="wrong")["ok"])
        self.daemon.herdr_ok = False
        self.assertEqual(self.call("focus_pane", pane="p1")["error"], "herdr unavailable")

    def test_slow_focus_does_not_block_another_client(self):
        started, finish = threading.Event(), threading.Event()

        def focus(*_):
            started.set()
            finish.wait(2)

        with patch.object(hrherdr.Herdr, "focus_workspace", focus):
            self.send("focus_workspace", workspace="w1")
            self.step()
            self.step()
            self.assertTrue(started.wait(1))
            try:
                self.assertTrue(self.call("hello", self.connect())["ok"])
                self.assertFalse(self.server.focus_job.done())
            finally:
                finish.set()
            self.assertTrue(self.receive()["ok"])
            self.assertEqual(self.server.focused_tab, ("w1", None))

    def test_answer_worker_results_and_dismissal(self):
        self.send("answer", question="q1", number=1)
        self.step()
        self.step()
        token, question, number = self.daemon.answerer.jobs.get_nowait()
        self.assertEqual((question["id"], number), ("q1", 1))
        self.daemon.desktop_results.put((token, True, "Answered"))
        self.assertEqual(self.receive()["result"], "Answered")
        self.assertFalse(self.call("answer", question="q1", number=-1)["ok"])
        self.assertFalse(self.call("answer", question="stale", number=1)["ok"])
        self.assertTrue(self.call("dismiss", question="q1")["ok"])
        self.assertTrue(self.server.state()["questions"][0]["dismissed"])
        self.daemon.model["questions"].clear()
        self.assertFalse(self.call("dismiss", question="q1")["ok"])
        self.server.state()
        self.assertEqual(self.server.dismissed, set())

    def test_detail_shares_reader_and_never_reads_unknown_pane(self):
        self.daemon.sessions.read = Mock(return_value=dict(task="Review", steps=["One"], cursor=1, input="x"))
        reply = self.call("detail", pane="p1")
        self.assertEqual(reply["result"]["task"], "Review")
        self.assertEqual(reply["result"]["steps"], ["One", "", ""])
        self.assertEqual(reply["result"]["cursor"], 1)
        self.assertFalse(self.call("detail", pane="stale")["ok"])
        self.daemon.sessions.read.assert_called_once()

    def test_voice_hold_release_cancel_and_disconnect(self):
        bus = self.daemon.voice.bus
        bus.status.side_effect = [(0, "idle"), (42, "recording"), (0, "idle"),
                                  (43, "recording"), (43, "recording"),
                                  (0, "idle"), (44, "recording"), (44, "recording")]
        self.assertEqual(self.call("voice", action="press")["result"], "Talking")
        self.assertEqual(self.server.state()["voice"]["state"], "recording")
        self.assertEqual(self.call("voice", action="release")["result"], "Sent")
        self.assertEqual(self.call("voice", action="press")["result"], "Talking")
        self.assertEqual(self.call("voice", action="cancel")["result"], "Discarded")
        self.assertEqual(self.call("voice", action="press")["result"], "Talking")
        self.first.socket.close()
        deadline = time.monotonic() + 1
        while self.daemon.voice.engaged and time.monotonic() < deadline:
            self.step()
            time.sleep(.001)
        self.assertEqual([call.args[0] for call in bus.call.call_args_list],
                         ["HoldStart", "HoldStop", "HoldStart", "Cancel", "HoldStop",
                          "HoldStart", "Cancel", "HoldStop"])
        self.assertFalse(self.daemon.voice.engaged)
        self.assertTrue(self.call("hello", self.connect())["ok"])

    def test_ir_guards_apply_to_desktop_commands(self):
        self.daemon.ir_fallback.mode = "host-fallback"
        self.daemon.ir_fallback.expires = time.monotonic() + 5
        for op, args in (
            ("focus_pane", dict(pane="p1")),
            ("answer", dict(question="q1", number=1)),
            ("voice", dict(action="press")),
        ):
            self.assertEqual(self.call(op, **args)["error"], "IR host fallback active")

    def test_usage_reload_and_real_borrow_lease_in_state(self):
        self.assertTrue(self.call("usage_refresh")["ok"])
        self.daemon.usage.request.assert_called_once()
        link = self.daemon.link = SimpleNamespace(opened=True)
        with patch.object(hrdesktop, "read_settings", return_value=(
                dict(HERDREMOTE_VOICE_FOCUS="on", HERDREMOTE_KEY_TOOL="wtype"), None)):
            self.assertEqual(self.call("reload")["result"], dict(settings_revision=1))
        self.assertIs(self.daemon.link, link)
        self.assertTrue(self.daemon.voice.voice_focus)
        self.assertEqual(self.daemon.keys.tool.tool, "wtype")
        control = self.daemon.disk_control
        control.release_serial = lambda: setattr(self.daemon, "link", None)
        control.start()
        self.addCleanup(control.close)
        with socket.socket(socket.AF_UNIX) as borrowed:
            borrowed.connect(control.path)
            borrowed.sendall(hrdisk.BORROW_SERIAL)
            control.poll(link)
            self.assertEqual(borrowed.recv(64), b"0 serial released\n")
            self.assertTrue(self.server.state()["link"]["borrowed"])
        control.poll(None)
        self.assertFalse(self.server.state()["link"]["borrowed"])

    def test_agent_control_reload_preserves_link_and_player_capabilities(self):
        link = self.daemon.link = SimpleNamespace(opened=True)
        self.daemon.sent_ws = b"existing picture"
        self.daemon.t3_player = self.daemon.question_ext = True
        for revision, mode in enumerate(("t3", "herdr"), 1):
            with patch.object(hrdesktop, "read_settings",
                              return_value=({"AGENT_CONTROL": mode, "T3_CONTROL": "on"}, None)):
                self.assertEqual(self.call("reload")["result"], {"settings_revision": revision})
            self.assertEqual(self.server.state()["agents"], {"control": mode})
            self.assertEqual(self.server.state()["t3"]["enabled"], mode == "t3")
            self.assertIs(self.daemon.link, link)
            self.assertEqual(self.daemon.sent_ws, b"existing picture")
            self.assertTrue(self.daemon.question_ext)
            self.assertEqual(self.daemon.player_t3, mode == "t3")

    def test_playback_ops_are_unavailable(self):
        ops = self.call("hello")["result"]["ops"]
        for op in ("player", "lyrics"):
            with self.subTest(op=op):
                self.assertNotIn(op, ops)
                self.assertEqual(self.call(op, name="cider", command="pause"),
                                 dict(id=7, ok=False, error=f"Unknown op: {op}"))
        self.make_bridge.assert_not_called()

    def test_ipod_commands_still_reach_the_bridge(self):
        bridge = Mock(command=Mock(return_value=True))
        self.make_bridge.side_effect = None
        self.make_bridge.return_value = bridge
        hello = b"ci\t1\t131072"
        control = b"cc\t4\t19\tpause\t0"
        self.daemon.command(hello)
        self.daemon.command(control)
        self.make_bridge.assert_called_once_with()
        self.assertEqual([call.args[0] for call in bridge.command.call_args_list],
                         [hello, control])


if __name__ == "__main__":
    unittest.main()


class T3StateTest(unittest.TestCase):
    def test_threads_for_agents_page(self):
        with TemporaryDirectory() as directory:
            runtime = Path(directory) / "server-runtime.json"
            runtime.with_name("environment-id").write_text("env-1", encoding="ascii")
            row = dict(id="t-1", projectId="p-1", title="Fix", status="idle",
                       pendingRuntimeRequest={"id": "r"}, modelSelection={"model": "opus"},
                       branch="main", updatedAt="2026-11-21T08:00:00Z")
            archived = dict(row, id="t-2", archivedAt="2026-11-20T00:00:00Z")
            client = SimpleNamespace(
                runtime=runtime, state=lambda: dict(enabled=True, connected=True),
                snapshot=lambda: ({}, {"t-1": row, "t-2": archived}, {}))
            state = hrdesktop.t3_state(client)
        self.assertTrue(state["connected"])
        self.assertEqual([t["id"] for t in state["threads"]], ["t-1"])
        thread = state["threads"][0]
        self.assertEqual((thread["status"], thread["waiting"], thread["model"]), ("running", True, "opus"))
        self.assertEqual(thread["link"], "t3code://threads/env-1/t-1")
