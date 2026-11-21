#!/usr/bin/env python3
"""HerdRemote: the herdr agent dashboard and remote for System > Agents.

Finds the iPod's PodLink tty, keeps a PodLink session open whenever the
player is attached with the Agents screen running, and keeps the screen's
picture of herdr current: workspaces, tabs, the selected tab's agents,
live pane detail, and choice questions. Commands from the player
focus local herdr clients (all clients on older servers) or answer the question.

    python3 tools/herdremote/herdremote.py            # run until stopped
    python3 tools/herdremote/herdremote.py --once     # print the model once

Dependencies: Python 3.9+, pyserial. Logs go to stdout.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from functools import lru_cache
import logging
import os
import queue
import re
import signal
import sys
import threading
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import podlink  # noqa: E402  tools/podlink.py: the PodLink frame codec

import hrdisk
import hragent
import hrdesktop
import hrfallback
import hrdetect  # noqa: E402
import hrdetail  # noqa: E402
import hrherdr  # noqa: E402
import hrorder  # noqa: E402
import hrproto  # noqa: E402
import hrt3  # noqa: E402
import hrvoice  # noqa: E402
import hrkeys  # noqa: E402
import hrusagekeys  # noqa: E402
import hrsession  # noqa: E402
import hrappserver  # noqa: E402
import hrremotelog  # noqa: E402
import hrcider  # noqa: E402
import hrhandoff  # noqa: E402

VERSION = "herdremote-1"
DEVICE = dict(vid=0x05AC, pid=0x1261, manufacturer="PodBox",
              serial="2AA0000000000360")
REFRESH_SECONDS = 1.0
TICK_SECONDS = 2.0
CONFIRM_SECONDS = 5.0
DETAIL_SECONDS = 1.0
DETAIL_VISIBLE_LINES = 5
DETAIL_KEY_SETTLE_SECONDS = 0.075
# A live timer or token count in the working line changes every second and
# must not redraw the player's view; the line still refreshes this often.
DETAIL_VOLATILE_SECONDS = 15.0
VOLATILE = re.compile(rb"\d+(?:\.\d+)?[kKmM]? tokens|\d+(?:\.\d+)?[hms]\b(?: \d+[ms]\b)*")

log = logging.getLogger("herdremote")


def find_device(ports, device=DEVICE):
    """The one port that is this iPod in PodLink mode, or None.

    Every field must match; any other tty, including other ACM devices on
    this machine, is never returned and so never opened.
    """
    found = [p.device for p in ports
             if p.vid == device["vid"] and p.pid == device["pid"]
             and p.manufacturer == device["manufacturer"]
             and p.serial_number == device["serial"]]
    return found[0] if len(found) == 1 else None


class Answerer(threading.Thread):
    """Answers questions off the link thread: keys, then re-reads to confirm."""

    def __init__(self, herdr, results, changed, desktop_results=None):
        super().__init__(daemon=True)
        self.herdr, self.results, self.changed = herdr, results, changed
        self.desktop_results = desktop_results
        self.jobs = queue.Queue()
        self.available = lambda: True

    def run(self):
        while True:
            req, question, number = self.jobs.get()
            try:
                ok, text = self.answer(question, number)
            except (hrherdr.HerdrError, ValueError) as error:
                ok, text = False, str(error)
            log.info("answer %s pane=%s option=%s: %s %s", question["id"],
                     question["pane"], number, "ok" if ok else "failed", text)
            desktop = isinstance(req, int) and req < 0
            results = self.desktop_results if desktop else self.results
            results.put((req, ok, text))
            self.changed.set()

    def current(self, pane):
        if not self.available():
            raise hrherdr.HerdrError("herdr control is off")
        text = self.herdr.read(pane)
        q = hrdetect.find_question(text)
        return q, hrdetect.question_id(pane, q) if q else None

    def answer(self, question, number):
        pane = question["pane"]
        q, qid = self.current(pane)
        if qid != question["id"]:
            return False, "The question has changed"
        if q["method"] == "codex-queued":
            if not self.available():
                return False, "herdr control is off"
            self.herdr.send_keys(pane, ["shift+left"])
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                time.sleep(0.2)
                opened, _ = self.current(pane)
                if opened and opened["method"] == "number":
                    q = opened
                    break
            else:
                return False, "Codex did not open the question"
        keys = hrdetect.answer_keys(q, number)
        before = hrdetect.question_id(pane, q)
        if not self.available():
            return False, "herdr control is off"
        self.herdr.send_keys(pane, keys)
        custom = number in q.get("custom", [])
        deadline = time.monotonic() + CONFIRM_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.25)
            after, after_id = self.current(pane)
            if custom and (after is None or after_id == before):
                # Selecting an option must expose an editor, not just change a pane.
                record = self.herdr.read_record(pane, source="visible")
                if hrdetail.extract_custom_input(record["text"], terminal_cursor=record.get("cursor"))["active"]:
                    return True, "Input ready"
            if after is None:
                if not custom:
                    return True, "Answered"
                continue
            if not custom and q["multi"] and after.get("options") != q.get("options"):
                return True, "Updated"
            if after_id != before:
                return (False, "The question has changed") if custom else (True, "Updated" if q["multi"] else "Answered")
        return False, "The pane did not change"


CLOCK_SYNC_SECONDS = 600.0
CLOCK_JUMP_SECONDS = 2.0


class ClockSync:
    """Monotonic schedule; wall/monotonic divergence also detects resume/steps."""
    def __init__(self):
        self.anchor = None
        self.offset = None
        self.next_sync = 0.0

    def poll(self, send, mono=None, wall=None, offset=None):
        mono = time.monotonic() if mono is None else mono
        wall = time.time() if wall is None else wall
        if offset is None:
            offset = int(datetime.datetime.fromtimestamp(wall).astimezone().utcoffset().total_seconds())
        anchor = wall - mono
        if (self.anchor is not None and mono < self.next_sync and
                abs(anchor - self.anchor) <= CLOCK_JUMP_SECONDS and offset == self.offset):
            return False
        local = datetime.datetime.fromtimestamp(wall).astimezone()
        send(hrproto.clock(local))
        self.anchor, self.offset = anchor, offset
        self.next_sync = mono + CLOCK_SYNC_SECONDS
        return True


class Daemon:
    def __init__(self, args):
        self.args = args
        self.herdr = hrherdr.Herdr(args.socket)
        self.order = hrorder.Order(args.state)
        self.changed = threading.Event()
        self.results = queue.Queue()
        self.desktop_results = queue.Queue()
        self.answerer = Answerer(self.herdr, self.results, self.changed, self.desktop_results)
        self.answerer.available = lambda: self.agents.control == "herdr"
        self.model = hrherdr.empty_model()
        self.state_seen = {}
        self.model_time = 0.0
        self.herdr_ok = False
        self.port = self.link = None
        self.link_serial = None
        self.link_device = None
        self.stopping = threading.Event()
        self.keys = hrkeys.KeyTaps(hrkeys.KeyTool(getattr(args, "key_tool", None)),
                                  key_scale=getattr(args, "key_scale", None))
        self.usage_keys = hrusagekeys.UsageKeys(self.keys, lambda:
            bool(not self._ir_owns_desktop() and self.link and self.link.opened and self.sent_ws is not None and
                 self.foreground in (None, 1) and
                 (self.usage_keys.view != 'detail' or
                  self.detail_keys_pane == self.usage_keys.pane == self.detail_pane)))
        self.voice = hrvoice.HoldToTalk(
            self.voice_focus_target,
            voice_focus=getattr(args, "voice_focus", False),
            voice_max_hold=getattr(args, "voice_max_hold", 120),
            voice_method=getattr(args, "voice_method", "HoldStart"))
        self.reader = None
        self.watcher = None
        self.model_job = self.detail_job = None
        self.model_key = None
        self.next_refresh = 0.0
        self.sessions = hrsession.Sessions()
        self.appserver = hrappserver.Reader()
        self.cider = None
        self.agents = hragent.Agents()
        self.usage = hrt3.Usage(self.agents.client)
        self.t3_focus_worker = None
        self.t3_focus_job = None
        self.desktop = hrdesktop.Server(self)
        self.voice_worker = None
        self.voice_jobs = {}
        self.remote_log = hrremotelog.Writer()
        self.ir_fallback = hrfallback.Executor(self.keys, self.voice)
        self.disk_control = hrdisk.Control(getattr(args, "disk_control_socket", None),
                                          fallback=self.ir_fallback,
                                          release_serial=self.close_link)
        self.reset_session()

    def reset_session(self):
        self.clock_sync = ClockSync()
        self.session_generation = getattr(self, "session_generation", 0) + 1
        self.keys.reset()
        if not getattr(self, "ir_fallback", None) or not self.ir_fallback.talking:
            self.stop_voice()
        self.sent_ws = None
        self.pending_usage = False
        self.sent_status = None
        self.sent_agents = None
        self.sent_tabs = None
        self.sent_questions = {}
        self.question_ext = False
        self.t3_player = False
        self.t3_focus_pending = None
        self.agents.reset()
        self.custom_pending = {}
        self.custom_input = None
        self.subscribed = None
        self.subscribed_tab = None
        self.detail_pane = None
        self.detail_keys_pane = None
        self.sent_detail = None
        self.sent_detail_key = None
        self.sent_detail_at = 0.0
        self.pending_detail = None
        self.detail_log_at = float("-inf")
        self.detail_log_suppressed = 0
        self.next_detail = 0.0
        self.detail_fast_due = None
        self.detail_edit_revision = getattr(self, "detail_edit_revision", 0) + 1
        self.key_details = {}
        self.next_tick = 0.0
        self.foreground = None
        self.usage_keys.update(False)

    # --- herdr ---------------------------------------------------------
    @property
    def player_t3(self):
        return self.agents.control == "t3" and self.t3_player

    def configure_agents(self, settings):
        previous = self.agents.control
        self.agents.configure(settings)
        if self.agents.control == previous:
            return
        self.session_generation += 1
        self.agents.reset()
        self.t3_focus_pending = None
        self.stop_voice(discard=True)
        self.desktop.voice_owner = None
        self.keys.reset()
        self.usage_keys.update(False)
        self.custom_pending.clear()
        self.custom_input = None
        self.key_details.clear()
        self.subscribed = self.subscribed_tab = self.detail_pane = self.detail_keys_pane = None
        self.sent_agents = self.sent_tabs = self.sent_detail = self.sent_detail_key = None
        self.pending_detail = None
        self.detail_fast_due = None
        self.next_detail = self.next_tick = self.next_refresh = 0
        self.model = hrherdr.empty_model()
        self.state_seen.clear()
        self.model_time = time.monotonic()
        self.herdr_ok = False
        self.desktop.focused_tab = None
        self.desktop.dismissed.clear()

    def update_herdr_watcher(self):
        if self.watcher is not None:
            self.watcher.stopping.set()
            self.watcher = None
        if self.reader is not None and self.agents.control == "herdr":
            self.watcher = hrherdr.Watcher(self.herdr, self.changed)
            self.watcher.start()

    def set_input_plugins(self, plugins):
        """Publish Interface 3 status dictionaries on the daemon thread."""
        self.desktop.plugins = copy.deepcopy(plugins)

    def refresh(self):
        """Synchronous refresh for --once and host callers; run uses a worker."""
        if self.agents.control != "herdr":
            return
        self.apply_model(self.read_model(self.herdr, self.subscribed,
                                         self.subscribed_tab, not self.herdr_ok))

    def read_model(self, client, workspace, tab, probe, desktop_tab=None):
        try:
            if probe:
                client.detect_capabilities()
            snapshot = client.snapshot()

            @lru_cache(maxsize=None)
            def read(pane):
                if self.stopping.is_set():
                    raise hrherdr.HerdrError("herdremote stopping")
                return client.read(pane)

            if self.stopping.is_set():
                return None, time.monotonic(), "herdremote stopping"
            model = hrherdr.build_model(snapshot, read, workspace, tab)
            if desktop_tab and desktop_tab != (workspace, tab):
                model["desktop"] = hrherdr.build_model(snapshot, read, *desktop_tab)
            if self.stopping.is_set():
                return None, time.monotonic(), "herdremote stopping"
            return self.order.apply(model), time.monotonic(), None
        except hrherdr.HerdrError as error:
            return None, time.monotonic(), str(error)

    def apply_model(self, update):
        model, sampled, error = update
        if error is None:
            self.model, self.model_time = model, sampled
            session = getattr(self, 'custom_input', None)
            if session and (session[1] not in model['panes'] or any(
                    q['pane'] == session[1] and q['id'] != session[0]
                    for q in model['questions'].values())):
                self.stop_voice()
                self.keys.reset()
                self.custom_input = None
                self.detail_keys_pane = None
            seen = {}
            for pane, meta in model.get("pane_meta", {}).items():
                status = meta["status"]
                old = self.state_seen.get(pane)
                since = sampled if old and old[0] != status else old[1] if old else None
                seen[pane] = (status, since)
            self.state_seen = seen
            if not self.herdr_ok:
                log.info("herdr reachable at %s", self.herdr.path)
            self.herdr_ok = True
        else:
            if self.herdr_ok or not self.model_time:
                log.warning("herdr unreachable: %s", error)
            self.herdr_ok = False
            self.state_seen = {}  # transitions during an outage are unknown
            self.model_time = self.model_time or sampled

    def detail_context(self, pane):
        meta = dict(self.model.get("pane_meta", {}).get(pane, {}))
        meta.setdefault("kind", "unknown")
        meta.setdefault("status", "unknown")
        meta["title"] = hrdetail.clean_title(meta.get("title", ""))
        meta["resident"] = self.foreground is not None
        since = self.state_seen.get(pane, (None, None))[1]
        meta["state_age_s"] = -1 if since is None else max(0, int(time.monotonic() - since))
        return meta

    def detail_key(self, pane):
        meta = self.detail_context(pane)
        session = meta.get("agent_session") or {}
        return (meta["kind"], meta["status"], meta["title"],
                session.get("kind"), session.get("value"), self.herdr_ok,
                pane in self.model["panes"], self.detail_edit_revision, getattr(self, "custom_input", None))

    def detail_limit(self):
        return getattr(self.link, "max_message", hrproto.MAX_MESSAGE)

    def read_detail(self, pane, readable, meta=None, client=None, max_message=None):
        meta = dict(meta or {})
        meta["custom_input"] = bool(getattr(self, "custom_input", None) and self.custom_input[1] == pane)
        fields = (self.appserver.read(meta or {}) if (meta or {}).get("kind") == "codex"
                  else self.sessions.read(meta or {})) if readable else None
        if fields is not None:
            fields = dict(fields)
            if (meta or {}).get('custom_input'):
                client = client or hrherdr.Herdr(self.herdr.path, self.herdr.timeout)
                try:
                    record = client.read_record(pane, source="visible")
                    fields.update(hrdetail.extract_custom_input(record['text'], (meta or {}).get('kind', 'unknown'), record.get('cursor')))
                except hrherdr.HerdrError:
                    fields.update(input='', cursor=0)
            if not (meta or {}).get("resident"):
                fields.pop("revision", None)
            return hrproto.detail(pane, fields, max_message=self.detail_limit() if max_message is None else max_message)
        fields = {}
        if readable and not self.stopping.is_set():
            client = client or hrherdr.Herdr(self.herdr.path, self.herdr.timeout)
            try:
                record = client.read_record(pane, source="visible")
                # source=visible with no lines cannot request scrollback.
                fields = hrdetail.parse(record["text"], terminal_cursor=record.get("cursor"),
                                        **{k: v for k, v in (meta or {}).items()
                                           if k in ("kind", "status", "title", "state_age_s")})
                if meta.get('custom_input'):
                    fields.update(hrdetail.extract_custom_input(record['text'], meta.get('kind', 'unknown'), record.get('cursor')))
                fields["say"] = "\n".join(fields.get("say", "").splitlines()[-DETAIL_VISIBLE_LINES:])
            except hrherdr.HerdrError:
                pass
        return hrproto.detail(pane, fields, max_message=self.detail_limit() if max_message is None else max_message)

    def send_detail(self, payload):
        fields = payload.split(b"\t")
        mode = b"known" if int(fields[2]) >= 0 else fields[2]
        # Revision and elapsed seconds are metadata, not visible changes.
        stable = fields[:2] + fields[3:11]
        stable[3] = VOLATILE.sub(b"#", stable[3])
        key = (tuple(stable), mode, self.detail_context(self.detail_pane)["status"])
        now = time.monotonic()
        legacy_due = (len(fields) != 12 and payload != self.sent_detail and
                      now - self.sent_detail_at >= DETAIL_VOLATILE_SECONDS)
        if key == self.sent_detail_key and not legacy_due:
            self.pending_detail = None
            return
        # Editing remains responsive. Streaming body/tool changes share a
        # one-second deadline, including changes caused by fast key reads.
        old = self.sent_detail.split(b"\t") if self.sent_detail else None
        same_body = old is not None and fields[1:2] + fields[3:9] == old[1:2] + old[3:9]
        if old is not None and not same_body and now - self.sent_detail_at < DETAIL_SECONDS:
            self.pending_detail = payload
            return
        self.pending_detail = None
        names = ("type", "pane", "age", "task", "now", "say", "step1",
                 "step2", "step3", "input", "cursor", "revision")
        changed = [names[i] for i, value in enumerate(fields)
                   if old is None or i >= len(old) or old[i] != value]
        args = (now, fields[1].decode("utf-8", "replace"),
                fields[11].decode() if len(fields) == 12 else "-",
                len(payload), ",".join(changed))
        log.debug("D time=%.3f pane=%s revision=%s bytes=%d changed=%s", *args)
        if now - self.detail_log_at >= DETAIL_SECONDS:
            log.info("D time=%.3f pane=%s revision=%s bytes=%d changed=%s suppressed=%d",
                     *args, self.detail_log_suppressed)
            self.detail_log_at, self.detail_log_suppressed = now, 0
        else:
            self.detail_log_suppressed += 1
        self.send(payload)
        self.sent_detail, self.sent_detail_key, self.sent_detail_at = payload, key, now

    def flush_detail(self):
        if self.pending_detail is not None and self.foreground in (None, 1):
            self.send_detail(self.pending_detail)

    def adopt_foreground(self):
        value = getattr(self.link, "foreground", None)
        if value == self.foreground:
            return
        previous, self.foreground = self.foreground, value
        self.detail_edit_revision += 1  # Reject a read begun in another visibility epoch.
        if value in (0, 2):
            self.stop_voice()
        if value == 1 and previous in (0, 2):
            # Keep subscriptions and the last sent picture; only changes go out.
            self.next_detail = 0.0
        if value != 1:
            self.usage_keys.update(False)
            self.detail_fast_due = None
            self.pending_detail = None

    def pump_reads(self):
        """Publish complete snapshots on the link thread, with bounded work.

        One model job and one detail job at most; never queue one per focus.
        Each worker owns its Herdr client, so probes cannot change the focus
        connection's capabilities. Stale subscription/session results are dropped.
        """
        if self.agents.control != "herdr":
            return
        self.flush_detail()
        desktop_tab = self.desktop.focused_tab if self.desktop.subscribed else None
        key = (self.session_generation, self.subscribed, self.subscribed_tab)
        extra = (desktop_tab,) if desktop_tab else ()
        key += extra
        if self.model_job and self.model_job[1].done():
            requested, future = self.model_job
            self.model_job = None
            if requested == key:
                self.apply_model(future.result())
        if self.detail_job and self.detail_job[1].done():
            requested, future = self.detail_job
            self.detail_job = None
            if requested[0] == self.session_generation:
                if (self.foreground in (None, 1) and
                        requested[1] == self.detail_pane and self.sent_ws is not None and
                        requested[2] == self.detail_key(self.detail_pane)):
                    self.send_detail(future.result())
                # Switching detail cannot bypass the one-second send limit.
                self.next_detail = time.monotonic() + DETAIL_SECONDS
        now = time.monotonic()
        if self.model_job is None and (now >= self.next_refresh or key != self.model_key):
            self.changed.clear()
            self.model_key = key
            self.next_refresh = now + REFRESH_SECONDS
            client = hrherdr.Herdr(self.herdr.path, self.herdr.timeout)
            self.model_job = (key, self.reader.submit(
                self.read_model, client, self.subscribed, self.subscribed_tab,
                not self.herdr_ok, *extra))
        due = self.detail_fast_due if self.detail_fast_due is not None else self.next_detail
        if (self.foreground in (None, 1) and
                self.sent_ws is not None and self.detail_pane and
                not hragent.THREAD.fullmatch(self.detail_pane) and
                self.detail_job is None and now >= due):
            pane = self.detail_pane
            self.detail_fast_due = None
            self.detail_job = ((self.session_generation, pane, self.detail_key(pane)),
                self.reader.submit(self.read_detail, pane,
                    self.herdr_ok and pane in self.model["panes"], self.detail_context(pane),
                    None, self.detail_limit()))

    # --- PodLink session -----------------------------------------------
    def open_link(self):
        if self.args.port:
            device = self.args.port
        else:
            from serial.tools import list_ports
            device = find_device(list_ports.comports())
        if not device:
            return False
        try:
            self.port = podlink.serial_port(device, 2)
            self.link = podlink.Link(self.port, 2)
            self.link.keepalive_interval = 1.0
            self.link.keepalive_timeout = 3.0
            self.link.nonce_keepalive = True
            self.link.open()
        except (OSError, RuntimeError, TimeoutError, podlink.LinkClosed) as e:
            log.info("PodLink at %s not ready: %s", device, e)
            self.close_link()
            return False
        log.info("PodLink session open on %s", device)
        self.link_device = device
        self.link_serial = DEVICE["serial"] if not self.args.port else None
        self.reset_session()
        self.send(hrproto.hello(VERSION))
        self.send(hrhandoff.CAPABILITY)
        self.clock_sync.poll(self.send)
        return True

    def close_link(self):
        self.disk_control.reset()
        if self.cider is not None:
            self.cider.disconnect()
        self.reset_session()
        if self.link and self.link.opened:
            try:
                self.link.close(1001, "herdremote stopping")
            except (OSError, RuntimeError, TimeoutError, podlink.LinkClosed):
                pass
        if self.port:
            try:
                self.port.close()
            except OSError:
                pass
        self.port = self.link = None

    def send(self, payload):
        self.link.send(podlink.TEXT, payload)

    def complete_answer(self, result, model):
        pending = self.custom_pending.pop(result[0], None)
        if pending and result[1]:
            valid = pending[2] == self.session_generation and not any(
                q['pane'] == pending[1] and q['id'] != pending[0]
                for q in model['questions'].values())
            if not valid:
                return result[0], False, "The question has changed"
            self.custom_input = pending[:2]
            self.sent_detail_key = None
            self.detail_edit_revision += 1
            self.next_detail = 0
        return result

    def flush_replies(self):
        """Publish replies before adopting reads that may predate a key batch."""
        self.agents.flush(self)
        self.pump_t3_focus()
        if self.sent_ws is None:
            return
        while not self.results.empty():
            self.send(hrproto.result(*self.complete_answer(self.results.get(), self.model)))
        for result in self.keys.replies():
            self.send(hrproto.result(*result))
            target = self.key_details.pop(result[0], None)
            if self.detail_pane and target == (self.session_generation, self.detail_pane):
                self.detail_edit_revision += 1
                if self.detail_fast_due is None:
                    self.detail_fast_due = time.monotonic() + DETAIL_KEY_SETTLE_SECONDS

    def push(self):
        """Send whatever the player's picture lacks."""
        if self.sent_ws is None or self.foreground == 2:
            return  # no Agents client has introduced itself
        self.flush_detail()
        self.flush_replies()
        t3_mode = self.agents.control == "t3"
        if not t3_mode and not self.model_time:
            return  # do not present an empty startup model while a read is pending
        picture = self.agents.client.snapshot() if self.player_t3 else ({}, {}, {})
        limit = self.detail_limit()
        spaces = self.agents.workspaces(picture) if t3_mode else self.model["workspaces"]
        ws = hrproto.workspaces(spaces, limit)
        # The legacy W encoder can fit fewer rows when the link cap is small.
        st = hrproto.status(spaces[:int(hrproto.decode(ws)[0][1])], limit)
        if ws != self.sent_ws:
            self.send(ws)
            self.sent_ws = ws
            self.sent_status = None
        if st != self.sent_status:
            self.send(st)
            self.sent_status = st
        if self.subscribed:
            if hragent.PROJECT.fullmatch(self.subscribed):
                tabs, rows = self.agents.tab_rows(picture, self.subscribed, self.subscribed_tab)
            else:
                tabs = self.model["tabs"].get(self.subscribed, [])
                tid = self.subscribed_tab or (tabs[0]["id"] if tabs else None)
                rows = self.model["tab_agents"].get(tid, []) if any(
                    t["id"] == tid for t in tabs) else []
            payload = hrproto.tabs(self.subscribed, tabs, limit)
            if payload != self.sent_tabs:
                self.send(payload)
                self.sent_tabs = payload
            payload = hrproto.agents(self.subscribed, rows, limit)
            if payload != self.sent_agents:
                self.send(payload)
                self.sent_agents = payload
        current = (self.agents.questions(picture, self.question_ext) if t3_mode else
                   list(self.model["questions"].values()))
        current.sort(key=lambda q: q.get("asked", self.order.panes.get(q["pane"], {}).get("asked"))
                     or float("inf"))
        current = {q["id"]: q for q in current[:hrproto.MAX_QUESTIONS]}
        for qid in list(self.sent_questions):
            if qid not in current:
                self.send(hrproto.question_gone(qid))
                del self.sent_questions[qid]
        if self.player_t3:
            # The player preserves Q arrival order when updating an existing ID.
            # Reinsert the suffix when a late subscription reveals an older Q.
            prefix = 0
            for old, new in zip(self.sent_questions, current):
                if old != new:
                    break
                prefix += 1
            for qid in list(self.sent_questions)[prefix:]:
                self.send(hrproto.question_gone(qid))
                del self.sent_questions[qid]
        self.agents.recent_questions = {}
        for qid, q in current.items():
            payload = hrproto.question(q, self.question_ext, limit)
            if "key" in q:
                numbers = (int(record[1]) for record in hrproto.decode(payload)[1:]
                           if record[0] == "o")
                self.agents.recent_questions[qid] = {number: q["values"][number]
                                                     for number in numbers}
            if self.sent_questions.get(qid) != payload:
                self.send(payload)
                self.sent_questions[qid] = payload
        now = time.monotonic()
        due = self.detail_fast_due if self.detail_fast_due is not None else self.next_detail
        if (self.foreground in (None, 1) and self.detail_pane and now >= due
                and hragent.THREAD.fullmatch(self.detail_pane)):
            self.send_detail(self.agents.detail(picture, self.detail_pane, limit))
            self.next_detail = now + DETAIL_SECONDS
        elif self.foreground in (None, 1) and self.reader is None and self.detail_pane and now >= due:
            # Share one deadline across switches: repeated det cannot bypass
            # the read/send limit. Never read a pane absent from the snapshot.
            self.detail_fast_due = None
            self.send_detail(self.read_detail(self.detail_pane,
                self.herdr_ok and self.detail_pane in self.model["panes"],
                self.detail_context(self.detail_pane), self.herdr))
            # Measure from completion: a slow read/send must not permit two
            # detail frames less than one second apart.
            now = time.monotonic()
            self.next_detail = now + DETAIL_SECONDS
        if now >= self.next_tick:
            # The firmware uses a false heartbeat to display a herdr-specific error.
            age, ok = (0, True) if t3_mode else (now - self.model_time, self.herdr_ok)
            self.send(hrproto.tick(age, ok))
            self.next_tick = now + TICK_SECONDS

    def command(self, payload):
        if payload == hrdisk.CAPABILITY and self.disk_control.hello(payload, self.send):
            return
        if self.remote_log.submit(payload):
            return
        if self._ir_owns_desktop() and payload.startswith(b"cc\t"):
            return  # Dock input must not also mutate Cider during IR ownership.
        if self.cider is None and payload.startswith((b"ci\t1\t", b"hf\t1\tq\t")):
            self.ensure_cider()
        if self.cider is not None and self.cider.command(payload):
            return
        parsed = hrproto.parse_command(payload)
        if not parsed:
            log.info("ignored message from player: %r", payload[:40])
            return
        name, args = parsed
        if (self._ir_owns_desktop() and name in ("fw", "fp", "ft", "a", "qi", "act")
                and not (name in ("fw", "fp", "ft") and (
                    hragent.THREAD.fullmatch(args[1]) or hragent.PROJECT.fullmatch(args[1])))):
            self.send(hrproto.result(args[0], False, "IR host fallback active"))
            return
        if name == "hr":
            self.reset_session()
            self.sent_ws = b""
        elif name == "qx":
            self.question_ext = True
            self.sent_questions.clear()
        elif name == "tc":
            self.t3_player = self.sent_ws is not None
        elif name == "qi":
            if args[1].startswith("t3q"):
                try:
                    self.agents.custom(self, *args)
                except ValueError as error:
                    self.send(hrproto.result(args[0], False, str(error)))
            elif len(args) == 3:
                self.custom_command(*args)
        elif name == "ks":
            target = args[0]
            if hragent.THREAD.fullmatch(target):
                row = self.agents.client.snapshot()[1].get(self.agents.threads.get(target))
                valid = self.player_t3 and row is not None
            else:
                row = None
                valid = self.agents.control == "herdr" and self.herdr_ok and target in self.model["panes"]
            zoomed = self.detail_context(target).get("zoomed", False)
            self.send(self.agents.sheet_payload(target, row, zoomed, self.detail_limit(), valid))
        elif name == "act":
            try:
                self.agents.action(self, *args)
            except (ValueError, hrherdr.HerdrError) as error:
                self.send(hrproto.result(args[0], False, str(error)))
        elif name == "sub":
            if args[0] != "-":
                t3_project = bool(hragent.PROJECT.fullmatch(args[0]))
                if t3_project:
                    if not self.player_t3 or len(args) != 2 or args[1] not in ("active", "all"):
                        return
                elif self.agents.control != "herdr":
                    return
            self.subscribed = None if args[0] == "-" else args[0]
            self.subscribed_tab = args[1] if len(args) == 2 else None
            self.sent_agents = None
            self.sent_tabs = None
            self.changed.set()
        elif name == "det":
            pane = None if args[0] == "-" else args[0]
            if pane and (bool(hragent.THREAD.fullmatch(pane)) != (self.agents.control == "t3")
                         or self.agents.control == "t3" and not self.player_t3):
                return
            if pane != self.detail_pane:
                self.agents.sheet = None
                self.agents.editing = None
                self.next_detail = 0.0 if pane and hragent.THREAD.fullmatch(pane) else self.next_detail
                self.detail_keys_pane = None
                self.usage_keys.update(False)
                self.sent_detail = None
                self.sent_detail_key = None
                self.pending_detail = None
                self.detail_fast_due = None
                self.key_details.clear()
            self.detail_pane = pane
        elif name == "us":
            rows = self.usage.snapshot()
            self.send(hrproto.usage(rows, time.time(), replace_errors=True))
            self.pending_usage = bool(self.usage.request())
        elif name == "vw":
            if args[0] != "1":
                self.pending_usage = False
            if args[0] == 'detail':
                pane = args[1]
                self.usage_keys.update(pane == self.detail_pane == self.detail_keys_pane,
                                       view='detail', pane=pane)
            else:
                self.usage_keys.update(args[0] == "1")
        elif name in ("fw", "fp", "ft"):
            self.focus(name, *args)
        elif name in ("vd", "vu", "vc"):
            self.voice_command(name, *args)
        elif name == "k":
            self.key_command(*args)
        elif name == "a":
            req, qid, number = args
            try:
                if qid.startswith(("t3q", "t3a")):
                    self.agents.answer(self, req, qid, int(number))
                else:
                    self.queue_answer(req, qid, number)
            except ValueError as error:
                self.send(hrproto.result(req, False, str(error)))

    def queue_answer(self, req, qid, number):
        if self.agents.control != "herdr":
            raise ValueError("herdr control is off")
        if self._ir_owns_desktop():
            raise ValueError("IR host fallback active")
        model = self.desktop.model if isinstance(req, int) and req < 0 else self.model
        question = model["questions"].get(qid)
        if not question or not (number.isdecimal() or (number == "-1" and question.get("no") == -1)):
            raise ValueError("The question has gone")
        number = int(number)
        if number in question.get("custom", []):
            self.custom_pending[req] = (qid, question["pane"], self.session_generation)
        self.answerer.jobs.put((req, question, number))

    def custom_command(self, req, qid, key):
        if self.desktop.voice_owner is not None:
            self.send(hrproto.result(req, False, "Voice input busy"))
            return
        session = self.custom_input
        if not session or session[0] != qid or session[1] != self.detail_keys_pane or session[1] != self.detail_pane:
            self.send(hrproto.result(req, False, "Input is not focused"))
            return
        pane = session[1]
        # Never send into a replacement question, even if refresh is pending.
        try:
            current, current_id = self.answerer.current(pane)
            if current and current_id != qid:
                self.custom_input = None
                self.send(hrproto.result(req, False, "The question has changed"))
                return
            self.voice.stop()
            self.keys.reset()
            self.herdr.send_keys(pane, [key])
            self.custom_input = None
            self.detail_edit_revision += 1
            self.send(hrproto.result(req, True, "Submitted" if key == "enter" else "Cancelled"))
        except hrherdr.HerdrError as error:
            self.send(hrproto.result(req, False, str(error)))

    def _ir_owns_desktop(self):
        fallback = getattr(self, "ir_fallback", None)
        return fallback is not None and fallback.active

    def key_command(self, req, key, count=1):
        if self._ir_owns_desktop():
            self.send(hrproto.result(req, False, "IR host fallback active"))
            return
        result = ((False, "Session not ready") if self.sent_ws is None else
                  self.keys.request(req, key, count))
        if result is None and self.detail_pane:
            self.key_details[req] = (self.session_generation, self.detail_pane)
        if result is not None:
            self.send(hrproto.result(req, *result))

    def voice_focus_target(self, target):
        if self.agents.control != "herdr":
            raise hrvoice.VoiceError("herdr control is off")
        params = dict(pane_id=target)
        if target.startswith("w:"):
            wid = target[2:]
            ws = next((w for w in self.model["workspaces"]
                       if w["id"] == wid), None)
            if not ws:
                raise hrvoice.VoiceError("no workspace %s" % wid)
            params = dict(workspace_id=wid)
            if ws.get("tab"):
                params["tab_id"] = ws["tab"]
        try:
            # Voice must never use the legacy all-client focus fallback.
            client = self.herdr
            if self.voice_worker is not None and isinstance(client, hrherdr.Herdr):
                client = hrherdr.Herdr(client.path, client.timeout)
            if not client._focus_local(**params):
                raise hrvoice.VoiceError("Local herdr focus unavailable")
        except hrherdr.HerdrError as error:
            raise hrvoice.VoiceError(str(error)) from error
        self.changed.set()

    def check_voice_target(self, name, target):
        if name != "vd" or not target or target == "-":
            return
        if (hragent.THREAD.fullmatch(target) or
                target.startswith("w:") and hragent.PROJECT.fullmatch(target[2:])):
            raise ValueError("Voice cannot reach T3")
        if self.agents.control != "herdr":
            raise ValueError("herdr control is off")

    def voice_action(self, name, target=None):
        try:
            self.check_voice_target(name, target)
        except ValueError as error:
            return False, str(error)
        if self._ir_owns_desktop():
            return False, "IR host fallback active"
        return (self.voice.press(target) if name == "vd" else
                self.voice.discard() if name == "vc" else self.voice.release())

    def queue_voice(self, name, target=None, req=None):
        self.check_voice_target(name, target)
        if self._ir_owns_desktop():
            raise ValueError("IR host fallback active")
        if len(self.voice_jobs) >= hrdesktop.MAX_JOBS:
            raise ValueError("Voice busy")
        future = self.voice_worker.submit(self.voice_action, name, target)
        self.voice_jobs[future] = (name, req, self.session_generation)
        return future

    @property
    def voice_pending(self):
        return next(iter(self.voice_jobs.values()), None)

    def stop_voice(self, discard=False):
        if not discard and self.desktop.voice_owner is not None:
            return
        if self.voice_worker is None:
            return self.voice.discard() if discard else self.voice.stop()
        if self.voice_jobs or self.voice.engaged:
            future = self.voice_worker.submit(self.voice.discard if discard else self.voice.stop)
            if discard:
                self.voice_jobs[future] = ("vc", None, self.session_generation)

    def flush_voice(self):
        for future, (name, req, session) in tuple(self.voice_jobs.items()):
            if not future.done():
                continue
            del self.voice_jobs[future]
            try:
                result = future.result()
            except Exception:
                log.exception("Voice operation failed")
                result = False, "Voice input failed"
            self.desktop.voice_result = result
            if req is not None and session == self.session_generation and self.link:
                self.send(hrproto.result(req, *result))

    def voice_command(self, name, req, target=None):
        if self._ir_owns_desktop():
            self.send(hrproto.result(req, False, "IR host fallback active"))
            return
        if self.desktop.voice_owner is not None:
            self.send(hrproto.result(req, False, "Voice input busy"))
            return
        if self.voice_worker is not None:
            try:
                self.queue_voice(name, target, req)
            except ValueError as error:
                self.send(hrproto.result(req, False, str(error)))
            return
        result = self.voice_action(name, target)
        self.send(hrproto.result(req, *result))

    def prepare_focus(self, name, target, tab_id=None, *, client=None):
        self.detail_keys_pane = None
        self.usage_keys.update(False)
        if hragent.THREAD.fullmatch(target) or hragent.PROJECT.fullmatch(target):
            if self.player_t3 and hragent.PROJECT.fullmatch(target):
                return lambda: (self.session_generation, None, "Focused")
            raise hrherdr.HerdrError("T3 target unavailable")
        if self.agents.control != "herdr":
            raise hrherdr.HerdrError("herdr control is off")
        if self._ir_owns_desktop():
            raise hrherdr.HerdrError("IR host fallback active")
        client = self.herdr if client is None else client
        session = self.session_generation
        pane = target if name == "fp" else None
        if name in ("fw", "ft"):
            ws = next((w for w in self.model["workspaces"] if w["id"] == target), None)
            if not ws:
                raise hrherdr.HerdrError("no workspace %s" % target)
            if name == "ft":
                tab = next((t for t in self.model["tabs"].get(target, [])
                            if t["id"] == tab_id), None)
                if not tab:
                    raise hrherdr.HerdrError("no tab %s in workspace %s" % (tab_id, target))
                action, args, text = client.focus_tab, (target, tab_id), "Focused " + tab["label"]
            else:
                action, args, text = client.focus_workspace, (target, ws["tab"]), "Focused " + ws["label"]
        else:
            action, args, text = client.focus_pane, (target,), "Focused pane"

        def focus():
            if self.agents.control != "herdr":
                raise hrherdr.HerdrError("herdr control is off")
            if self._ir_owns_desktop():
                raise hrherdr.HerdrError("IR host fallback active")
            action(*args)
            return session, pane, text

        return focus

    def complete_focus(self, result):
        session, pane, text = result
        if session == self.session_generation and pane == self.detail_pane:
            self.detail_keys_pane = pane
        return text

    def focus(self, name, req, target, tab_id=None):
        if name in ("fp", "fw") and hragent.THREAD.fullmatch(target):
            self.detail_keys_pane = None
            self.usage_keys.update(False)
            if not self.player_t3:
                self.send(hrproto.result(req, False, "T3 unavailable"))
                return
            previous = self.t3_focus_pending
            self.t3_focus_pending = req, target
            if previous:
                self.send(hrproto.result(previous[0], False, "Focus superseded"))
            return
        try:
            text = self.complete_focus(self.prepare_focus(name, target, tab_id)())
            self.send(hrproto.result(req, True, text))
            log.debug("%s %s", text, target)
        except hrherdr.HerdrError as error:
            self.send(hrproto.result(req, False, str(error)))
            log.warning("focus %s failed: %s", target, error)

    def pump_t3_focus(self):
        # Keep only the running opener and the newest pending target.
        if self.t3_focus_job:
            future, session, req, text = self.t3_focus_job
            if not future.done():
                return
            self.t3_focus_job = None
            if session == self.session_generation:
                try:
                    future.result()
                except ValueError as error:
                    self.send(hrproto.result(req, False, str(error)))
                else:
                    self.send(hrproto.result(req, True, text))

        if self.t3_focus_pending is None:
            return
        req, target = self.t3_focus_pending
        self.t3_focus_pending = None
        try:
            identity, text = self.agents.focus_target(target)
        except ValueError as error:
            self.send(hrproto.result(req, False, str(error)))
            return
        if not self.agents.follow_ipod:
            self.send(hrproto.result(req, True, text))
        elif self.t3_focus_worker is None:
            self.send(hrproto.result(req, False, "T3 desktop opener unavailable"))
        else:
            future = self.t3_focus_worker.submit(self.agents.client.open_thread, identity)
            self.t3_focus_job = future, self.session_generation, req, text

    def ensure_cider(self):
        if self.cider is None:
            self.cider = hrdesktop.make_bridge()
        return self.cider

    def apply_desktop_settings(self, values, token):
        hrdesktop.apply_settings(self, values, token)

    def run(self):
        self.agents.client.start()
        self.disk_control.start()
        self.usage_keys.start()
        self.usage.start()
        self.answerer.start()
        self.reader = ThreadPoolExecutor(max_workers=2, thread_name_prefix="herdremote-read")
        self.update_herdr_watcher()
        self.voice_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="herdremote-voice")
        self.t3_focus_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="herdremote-t3-focus")
        try:
            try:
                self.desktop.start()
            except OSError as error:
                log.warning("Desktop socket unavailable: %s", error)
            self.run_loop()
        finally:
            self.stopping.set()
            if self.watcher is not None:
                self.watcher.stopping.set()
            self.desktop.close()
            self.agents.client.close()
            self.disk_control.close()
            self.remote_log.close()
            if self.cider is not None:
                self.cider.close()
            self.usage_keys.close()
            self.keys.reset()
            if self.link:
                self.close_link()
            self.voice_worker.shutdown(wait=True, cancel_futures=False)
            self.voice_worker = None
            self.t3_focus_worker.shutdown(wait=True)
            self.t3_focus_worker = None
            self.voice.stop()
            self.reader.shutdown(wait=True, cancel_futures=True)
            self.reader = None
            self.usage.close()

    def run_loop(self):
        next_probe = 0.0
        while not self.stopping.is_set():
            now = time.monotonic()
            self.disk_control.poll(self.link)
            if not self.link:
                self.pump_reads()
                self.poll_desktop()
                if self.disk_control.serial_lease is None and now >= next_probe:
                    next_probe = now + 1.0
                    self.open_link()
                time.sleep(0.05)
                continue
            try:
                self.clock_sync.poll(self.send)
                self.link.poll()
                while self.link.frames:
                    frame = self.link.frames.popleft()
                    if frame.type == podlink.TEXT:
                        self.command(frame.payload)
                if self.cider is not None:
                    self.cider.check_liveness()
                self.adopt_foreground()
                self.usage_keys.poll()
                if self.pending_usage:
                    rows = self.usage.snapshot()
                    if not any(row.get("refreshing") for row in rows):
                        self.pending_usage = False
                        self.send(hrproto.usage(rows, time.time(), replace_errors=True))
                self.flush_replies()
                self.pump_reads()
                self.push()
                if self.cider is not None:
                    self.cider.poll(self.link.send, self.link.best_effort_delay)
                self.poll_desktop()
            except (podlink.LinkClosed, hrcider.SessionExpired) as error:
                log.info("PodLink closed (%s): %s", error.code, error.reason)
                self.link.opened = False
                self.close_link()
                continue
            except (OSError, TimeoutError, ValueError) as error:
                log.info("PodLink lost: %s", error)
                self.close_link()
                continue
            time.sleep(0.02)

    def poll_desktop(self):
        if getattr(self, "desktop", None) is not None:
            while not self.desktop_results.empty():
                result = self.complete_answer(self.desktop_results.get(), self.desktop.model)
                self.desktop.answer_reply(*result)
            self.flush_voice()
            self.desktop.poll()
            self.agents.watch(self.desktop.subscribed, self.sent_ws is not None and self.player_t3,
                              self.subscribed, self.subscribed_tab, self.detail_pane)


def main():
    desktop_env = {key: os.environ[key] for key in hrdesktop.SETTINGS_KEYS if key in os.environ}
    try:
        import podlink_settings
        for key, value in podlink_settings.load().items():
            os.environ.setdefault(key, value)
        if "CIDER_API_TOKEN" not in os.environ:
            token = podlink_settings.cider_token()
            if token:
                os.environ["CIDER_API_TOKEN"] = token
    except ImportError:
        pass
    except (OSError, ValueError) as error:
        logging.getLogger("herdremote").warning("Could not load desktop settings: %s", error)
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", help="herdr API socket "
                        "(default: $HERDR_SOCKET_PATH or ~/.config/herdr)")
    parser.add_argument("--port", help="use this tty instead of finding the "
                        "iPod (testing only)")
    parser.add_argument("--state", default=hrorder.default_path(),
                        help="file that keeps the list order across restarts "
                        "(default: %(default)s)")
    parser.add_argument("--once", action="store_true",
                        help="print the model as the player would get it, "
                        "then exit")
    parser.add_argument("--usage-dump", action="store_true",
                        help="print one read-only usage frame; no herdr/serial access")
    parser.add_argument("--usage-wait", type=float, default=6,
                        help="usage-dump wait budget in seconds (default: 6)")
    hrvoice.add_arguments(parser)
    hrkeys.add_arguments(parser)
    parser.add_argument("--debug", action="store_true", help="log metadata for every detail frame")
    args = parser.parse_args()
    args.desktop_env = desktop_env
    args.desktop_overrides = {arg.split("=", 1)[0] for arg in sys.argv[1:]}
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.usage_dump:
        client = hrt3.Client()
        client.configure(False, None)
        cache = hrt3.Usage(client)
        try:
            cache.request()
            deadline = time.monotonic() + max(0, args.usage_wait)
            while time.monotonic() < deadline:
                if not any(r.get("refreshing") for r in cache.snapshot()):
                    break
                time.sleep(0.02)
            print(hrproto.usage(cache.snapshot(), time.time(), replace_errors=True).decode())
        finally:
            cache.close()
        return
    daemon = Daemon(args)
    try:
        daemon.configure_agents(hragent.config(os.environ))
    except ValueError as error:
        parser.error(str(error))
    if args.once:
        if daemon.agents.control == "t3":
            client = daemon.agents.client
            if client.usage_token():
                client.watch(True, (None, None, None))
                client.start()
                try:
                    deadline = time.monotonic() + 3
                    while not client.state()["connected"] and time.monotonic() < deadline:
                        time.sleep(0.02)
                    picture = client.snapshot()
                finally:
                    client.close()
            else:
                picture = ({}, {}, {})
            spaces = daemon.agents.workspaces(picture)
            questions = daemon.agents.questions(picture)
        else:
            daemon.refresh()
            spaces = daemon.model["workspaces"]
            questions = daemon.model["questions"].values()
        print(hrproto.workspaces(spaces).decode())
        print(hrproto.status(spaces).decode())
        for q in questions:
            print(hrproto.question(q).decode())
        daemon.usage.close()
        return
    # systemd stops the service with SIGTERM: close the session cleanly
    signal.signal(signal.SIGTERM, lambda *_: daemon.stopping.set())
    try:
        daemon.run()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.close_link()
        daemon.usage.close()


if __name__ == "__main__":
    main()
