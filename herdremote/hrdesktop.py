"""Interface 2. Socket I/O and daemon dispatch run on the link thread."""
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
import logging
import os
from pathlib import Path
import queue
import re
import selectors
import socket
import stat
import time

import hragent
import hrcider
from hrcider_plugin import make_clients
import hrherdr
import hrkeys
import hrproto
import hrvoice
from hrrufin import RufinClient, loopback_url
from hrjellyfin import JellyfinClient, https_base
import podlink_settings
from podlink_disk import UUID_PATH

log = logging.getLogger("herdremote.desktop")

OPS = ("hello", "subscribe", "focus_workspace", "focus_tab", "focus_pane",
       "answer", "dismiss", "detail", "voice",
       "usage_refresh", "reload", "t3_connect", "t3_disconnect")
FOCUS = {"focus_workspace": ("fw", "workspace"),
         "focus_tab": ("ft", "workspace"), "focus_pane": ("fp", "pane")}
VOICE = {"press": "vd", "release": "vu", "cancel": "vc"}
SETTINGS_KEYS = (*podlink_settings.FIELDS, "CIDER_API_TOKEN")
STATE_INTERVAL = .25
MAX_CLIENTS = 16
MAX_REQUEST = 16384
MAX_OUTPUT = 2 << 20
MAX_JOBS = 16


def default_socket():
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / (
        "podbox-herdremote/desktop.sock")


def encode(message):
    return json.dumps(message, separators=(",", ":")).encode("ascii") + b"\n"


def string(request, key, choices=None, default=None):
    value = request.get(key, default)
    if (not isinstance(value, str) or not value or
            any(ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff for c in value)
            or (choices is not None and value not in choices)):
        raise ValueError(f"Invalid {key}")
    return value


class Client:
    def __init__(self, channel):
        self.channel = channel
        self.received = bytearray()
        self.output = bytearray()
        self.subscribed = self.logs = False
        self.last_state = None
        self.next_state = 0.0


class LogQueue(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = queue.Queue(128)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record):
        try:
            self.lines.put_nowait(self.format(record)[:4096])
        except queue.Full:
            pass


class CiderClient:
    """Keep the bridge's client identity when the local settings change."""
    def __init__(self):
        self.mode = os.environ.get("CIDER_BACKEND", "auto")
        client = make_clients(hrcider.Client)
        self.backend = None if self.mode == "rpc" else client
        self.rpc = client if self.backend is None else self.backend.rpc

    def __getattr__(self, name):
        return getattr(self.rpc if self.mode == "rpc" else self.backend, name)

    @property
    def rpc_observer(self):
        return self.rpc.rpc_observer

    @rpc_observer.setter
    def rpc_observer(self, callback):
        self.rpc.rpc_observer = callback
        if self.backend is not None:
            self.backend.rpc_observer = callback

    def start(self):
        if self.backend is not None:
            self.backend.start()

    def close(self):
        if self.backend is not None:
            self.backend.close()

    def configure(self, env, rpc):
        mode = env.get("CIDER_BACKEND", "auto")
        if mode != "rpc":
            if self.backend is None:
                backend = make_clients(hrcider.Client, env)
                backend.rpc = self.rpc
                backend.rpc_observer = self.rpc_observer
                backend.start()
                self.backend = backend
            self.backend.mode = mode
        self.rpc.base, self.rpc.token = rpc.base, rpc.token
        self.mode = mode


def make_bridge():
    rufin = None if os.environ.get("RUFIN_DISABLE") == "1" else RufinClient()
    bridge = hrcider.Bridge(client=CiderClient(), realtime=True, rufin=rufin,
                            jellyfin=JellyfinClient() if rufin is not None else None)
    try:
        bridge.start()
    except Exception:
        bridge.close()
        raise
    return bridge


class Server:
    def __init__(self, daemon, path=None):
        self.daemon = daemon
        self.path = Path(path) if path is not None else default_socket()
        self.listener = self.selector = self.worker = None
        self.clients = set()
        self.logs = LogQueue()
        self.ids = itertools.count(-1, -1)
        self.jobs = {}
        self.focus_job = None
        self.t3_job = None
        self.answers = {}
        self.settings_revision = 0
        self.dismissed = set()
        self.focused_tab = None
        self.plugins = []
        self.voice_owner = None
        self.voice_result = None

    @property
    def subscribed(self):
        return any(client.subscribed for client in self.clients)

    @property
    def model(self):
        return self.daemon.model.get("desktop", self.daemon.model)

    def start(self):
        root = self.path.parent
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise PermissionError("desktop socket directory must be owned by this user and mode 0700")
        if os.path.lexists(self.path):
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise PermissionError("unsafe desktop socket path")
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(.05)
                try:
                    probe.connect(str(self.path))
                except ConnectionRefusedError:
                    self.path.unlink()
                else:
                    raise OSError("desktop socket already in use")
        listener = socket.socket(socket.AF_UNIX)
        try:
            listener.bind(str(self.path))
        except OSError:
            listener.close()
            raise
        self.listener = listener
        try:
            self.path.chmod(0o600)
            listener.listen(MAX_CLIENTS)
            listener.setblocking(False)
            self.selector = selectors.DefaultSelector()
            self.selector.register(listener, selectors.EVENT_READ)
            self.worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix="herdremote-desktop")
            logging.getLogger("herdremote").addHandler(self.logs)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.listener is None:
            return
        logging.getLogger("herdremote").removeHandler(self.logs)
        for client in tuple(self.clients):
            self.drop(client)
        if self.selector is not None:
            self.selector.close()
        self.listener.close()
        self.listener = None
        self.path.unlink(missing_ok=True)
        if self.worker is not None:
            self.worker.shutdown(wait=True, cancel_futures=True)

    def drop(self, client):
        self.selector.unregister(client.channel)
        self.clients.remove(client)
        client.channel.close()
        if self.voice_owner is client:
            self.voice_owner = None
            self.daemon.stop_voice(discard=True)

    def write(self, client, message):
        if client not in self.clients:
            return
        client.output.extend(message)
        if len(client.output) > MAX_OUTPUT:
            self.drop(client)
            return
        self.selector.modify(client.channel, selectors.EVENT_READ | selectors.EVENT_WRITE, client)

    def reply(self, client, request_id, result=None, error=None):
        response = ({"ok": False, "error": error} if error is not None
                    else {"ok": True, "result": result})
        self.write(client, encode(dict(id=request_id, **response)))

    def poll(self):
        if self.listener is None:
            return
        for key, events in self.selector.select(0):
            client = key.data
            if client is None:
                try:
                    channel, _ = self.listener.accept()
                except BlockingIOError:
                    continue
                if len(self.clients) >= MAX_CLIENTS:
                    channel.close()
                    continue
                channel.setblocking(False)
                client = Client(channel)
                self.clients.add(client)
                self.selector.register(channel, selectors.EVENT_READ, client)
                continue
            try:
                if events & selectors.EVENT_READ:
                    if b"\n" not in client.received:
                        data = client.channel.recv(4096)
                        if not data:
                            self.drop(client)
                            continue
                        client.received.extend(data)
                if events & selectors.EVENT_WRITE:
                    del client.output[:client.channel.send(client.output)]
                    if not client.output:
                        self.selector.modify(client.channel, selectors.EVENT_READ, client)
            except BlockingIOError:
                pass
            except OSError:
                self.drop(client)

        for client in tuple(self.clients):
            # Bound command work even when a client sends a batch of requests.
            end = client.received.find(b"\n")
            if end > MAX_REQUEST or (end < 0 and len(client.received) > MAX_REQUEST):
                self.drop(client)
            elif end >= 0:
                line = bytes(client.received[:end])
                del client.received[:end + 1]
                self.request(client, line)
        self.finish_jobs()
        self.publish(time.monotonic())

    def publish(self, now):
        subscribers = [client for client in self.clients if client.subscribed]
        due = [client for client in subscribers if now >= client.next_state]
        if due:
            payload = encode({"event": "state", "state": self.state()})
            for client in due:
                client.next_state = now + STATE_INTERVAL
                if payload != client.last_state:
                    self.write(client, payload)
                    client.last_state = payload
        readers = [client for client in subscribers if client.logs]
        for _ in range(16):
            try:
                line = self.logs.lines.get_nowait()
            except queue.Empty:
                break
            if readers:
                payload = encode({"event": "log", "line": line})
                for client in readers:
                    self.write(client, payload)

    def submit(self, client, request_id, work, *args, finish=None):
        if len(self.jobs) >= MAX_JOBS:
            raise ValueError("Desktop API busy")
        future = self.worker.submit(work, *args)
        self.jobs[future] = (client, request_id, finish)
        return future

    def finish_jobs(self):
        for future, (client, request_id, finish) in tuple(self.jobs.items()):
            if not future.done():
                continue
            del self.jobs[future]
            try:
                value = future.result()
                self.reply(client, request_id, finish(value) if finish else value)
            except (hrherdr.HerdrError, OSError, ValueError) as error:
                self.reply(client, request_id, error=str(error))
            except Exception:
                log.exception("Desktop request failed")
                self.reply(client, request_id, error="Desktop request failed")

    def request(self, client, line):
        request_id = None
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict) or type(request.get("id")) is not int:
                raise ValueError("Expected an integer id")
            request_id = request["id"]
            op = string(request, "op")
            if op not in OPS:
                raise ValueError("Unknown op: " + op)
            self.dispatch(client, request_id, op, request)
        except (hrherdr.HerdrError, OSError, ValueError, RecursionError) as error:
            self.reply(client, request_id, error=str(error))

    def dispatch(self, client, request_id, op, request):
        daemon = self.daemon
        if op in ("t3_connect", "t3_disconnect"):
            if self.t3_job in self.jobs or any(
                    finish == self.reload for _, _, finish in self.jobs.values()):
                raise ValueError("T3 session command busy")
            action = daemon.agents.client.t3_connect if op == "t3_connect" else daemon.agents.client.t3_disconnect
            self.t3_job = self.submit(client, request_id, action, finish=self.t3_session_changed)
            return
        if op in FOCUS or op in ("answer", "dismiss", "detail"):
            if not daemon.herdr_ok:
                raise ValueError("herdr unavailable")

        if op == "hello":
            result = {"version": 1, "ops": OPS}
        elif op == "subscribe":
            logs = request.get("logs", False)
            if type(logs) is not bool:
                raise ValueError("Invalid logs")
            client.subscribed, client.logs = True, logs
            client.last_state = None
            result = None
        elif op in FOCUS:
            name, key = FOCUS[op]
            target = string(request, key)
            tab = string(request, "tab") if name == "ft" else None
            if self.focus_job in self.jobs:
                raise ValueError("Focus busy")
            reader = hrherdr.Herdr(daemon.herdr.path, daemon.herdr.timeout)
            focus = daemon.prepare_focus(name, target, tab, client=reader)
            finish = lambda result: self.complete_focus(result, name, target, tab)
            self.focus_job = self.submit(client, request_id, focus, finish=finish)
            return
        elif op == "answer":
            question = string(request, "question")
            number = request.get("number")
            if type(number) is not int or not -1 <= number <= 2147483647:
                raise ValueError("Invalid number")
            if len(self.answers) >= MAX_JOBS:
                raise ValueError("Answerer busy")
            token = next(self.ids)
            daemon.queue_answer(token, question, str(number))
            self.answers[token] = (client, request_id)
            return
        elif op == "dismiss":
            question = string(request, "question")
            if question not in self.model["questions"]:
                raise ValueError("The question has gone")
            self.dismissed.add(question)
            result = None
        elif op == "detail":
            pane = string(request, "pane")
            if pane not in daemon.model["panes"]:
                raise ValueError("No such pane")
            client_reader = hrherdr.Herdr(daemon.herdr.path, daemon.herdr.timeout)
            self.submit(client, request_id, daemon.read_detail, pane, True,
                        daemon.detail_context(pane), client_reader, daemon.detail_limit(),
                        finish=detail_record)
            return
        elif op == "voice":
            action = string(request, "action", VOICE)
            target = string(request, "target", default="-")
            if len(target.encode("utf-8")) > 17 or target == "w:":
                raise ValueError("Invalid target")
            self.voice(client, request_id, action, target)
            return
        elif op == "usage_refresh":
            daemon.usage.request()
            result = None
        else:  # reload
            if self.t3_job in self.jobs or any(
                    finish == self.reload for _, _, finish in self.jobs.values()):
                raise ValueError("Settings reload busy")
            self.submit(client, request_id, read_settings, finish=self.reload)
            return
        self.reply(client, request_id, result)

    def t3_session_changed(self, result):
        self.daemon.usage.request()
        return result

    def voice(self, client, request_id, action, target):
        if self.daemon._ir_owns_desktop():
            raise ValueError("IR host fallback active")
        if self.voice_owner not in (None, client):
            raise ValueError("Another desktop client owns the voice hold")
        if self.voice_owner is None:
            if action != "press":
                self.reply(client, request_id, "Sent" if action == "release" else "Discarded")
                return
            voice = self.daemon.voice
            if voice.engaged or voice.down or self.daemon.voice_pending:
                raise ValueError("Voice input busy")
        if len(self.jobs) >= MAX_JOBS:
            raise ValueError("Desktop API busy")

        def finish(result):
            ok, message = result
            if (self.voice_owner is client and self.voice_latest is future
                    and (not ok or action != "press") and not self.daemon.voice.engaged):
                self.voice_owner = None
            if not ok:
                raise ValueError(message)
            return message

        future = self.daemon.queue_voice(VOICE[action], target)
        self.voice_latest = future
        if action == "press":
            self.voice_owner = client
        self.jobs[future] = (client, request_id, finish)

    def complete_focus(self, result, name, target, tab):
        message = self.daemon.complete_focus(result)
        if name == "fp":
            target, tab = next(((w["id"], t["id"])
                                for w in self.model["workspaces"]
                                for t in self.model["tabs"].get(w["id"], [])
                                if any(a["pane"] == target
                                       for a in self.model["tab_agents"].get(t["id"], []))),
                               (None, None))
        if target is not None:
            self.focused_tab = (target, tab)
        self.daemon.next_refresh = 0.0
        return message

    def answer_reply(self, token, ok, message):
        reply = self.answers.pop(token, None)
        if reply is not None:
            self.reply(*reply, result=message, error=None if ok else message)

    def reload(self, settings):
        self.daemon.apply_desktop_settings(*settings)
        self.settings_revision += 1
        return {"settings_revision": self.settings_revision}

    def state(self):
        daemon = self.daemon
        herdr_ok = daemon.agents.control == "herdr" and daemon.herdr_ok
        questions = self.model["questions"] if herdr_ok else {}
        if herdr_ok:
            self.dismissed.intersection_update(questions)
        return {
            "link": link_state(daemon),
            "agents": {"control": daemon.agents.control},
            "herdr": {"ok": herdr_ok,
                      "workspaces": workspace_state(self.model, daemon.state_seen) if herdr_ok else []},
            "questions": [question_state(q, qid in self.dismissed) for qid, q in questions.items()],
            "usage": [usage_state(row) for row in daemon.usage.snapshot()],
            "voice": voice_state(daemon.voice, self.voice_result, daemon.voice_pending),
            "input": {"ir_fallback": daemon.ir_fallback.mode, "plugins": self.plugins},
            "t3": t3_state(daemon.agents.client),
            "settings_revision": self.settings_revision,
        }


T3_STATES = {"blocked": "running", "working": "running", "done": "completed", "idle": "idle"}


def t3_state(client, limit=50):
    """Client status plus the newest unarchived threads for the Agents page."""
    state = client.state()
    _, threads, _ = client.snapshot()
    try:
        environment = client.runtime.with_name("environment-id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError, AttributeError):
        environment = ""
    rows = sorted((row for row in threads.values() if not row.get("archivedAt")),
                  key=lambda row: -hragent.instant(row.get("updatedAt")))[:limit]
    state["threads"] = [dict(
        id=row["id"], project=row.get("projectId"), title=row.get("title") or "",
        status=T3_STATES[hragent.status(row)],
        waiting=row.get("pendingRuntimeRequest") is not None,
        model=(row.get("modelSelection") or {}).get("model"), branch=row.get("branch"),
        settled=hragent.settled(row), snoozed=hragent.instant(row.get("snoozedUntil")) > time.time(), archived=False,
        updated_at=row.get("updatedAt"),
        link=f"t3code://threads/{environment}/{row['id']}" if environment else None,
    ) for row in rows]
    return state


def read_settings():
    return podlink_settings.load(), podlink_settings.cider_token()


def apply_settings(daemon, values, token):
    inherited = getattr(daemon.args, "desktop_env", {})
    env = os.environ.copy()
    values = {**values, "CIDER_API_TOKEN": token}
    for key in SETTINGS_KEYS:
        value = inherited.get(key, values.get(key))
        if value is not None:
            env[key] = value
        else:
            env.pop(key, None)
    agent_settings = hragent.config(env)
    mode = env.get("CIDER_BACKEND", "auto")
    if mode not in ("auto", "plugin", "rpc"):
        raise ValueError("CIDER_BACKEND must be auto, plugin or rpc")
    if env.get("RUFIN_API_URL"):
        loopback_url(env["RUFIN_API_URL"])
    if env.get("JELLYFIN_URL"):
        https_base(env["JELLYFIN_URL"])
    rpc = hrcider.Client(env.get("CIDER_API_URL", "http://127.0.0.1:10767"),
                         env.get("CIDER_API_TOKEN"))
    overrides = getattr(daemon.args, "desktop_overrides", ())
    voice_focus = (daemon.voice.voice_focus if "--voice-focus" in overrides else
                   hrvoice.boolean(env.get("HERDREMOTE_VOICE_FOCUS", "off")))
    key_tool = (daemon.keys.tool.tool if "--key-tool" in overrides else
                hrkeys.tool_name(env.get("HERDREMOTE_KEY_TOOL", "auto")))
    rufin = daemon.cider.rufin if daemon.cider is not None else None
    if rufin is not None and not rufin.state_lock.acquire(blocking=False):
        raise ValueError("Rufin busy. Retry settings reload.")
    try:
        if daemon.cider is not None:
            daemon.cider.cider_client.configure(env, rpc)
        for key in SETTINGS_KEYS:
            if key in env:
                os.environ[key] = env[key]
            else:
                os.environ.pop(key, None)
        if rufin is not None:
            rufin.base = None
            rufin.mark_failed("Reloading settings", delay=0)
            daemon.cider.remote_check_at = 0
    finally:
        if rufin is not None:
            rufin.state_lock.release()
    daemon.voice.voice_focus = voice_focus
    daemon.keys.tool.tool = key_tool
    previous_control = daemon.agents.control
    daemon.configure_agents(agent_settings)
    path = getattr(daemon.args, "socket", None) or hrherdr.default_socket()
    changed_path = daemon.herdr.path != path
    if changed_path:
        daemon.herdr.path = path
        daemon.herdr.client_targeted_focus = False
        daemon.herdr_ok = False
        daemon.model_job = None
        daemon.next_refresh = 0
    if changed_path or daemon.agents.control != previous_control:
        daemon.update_herdr_watcher()
    if daemon.cider is not None:
        daemon.cider.wake.set()
    daemon.usage.request()


def detail_record(payload):
    _, pane, age, task, now, say, *tail = hrproto.decode(payload)[0]
    return dict(pane=pane, age=int(age), task=task, now=now, say=say, steps=tail[:3],
                input=re.sub(r"\\([\\n])", lambda m: "\n" if m[1] == "n" else "\\", tail[3]),
                cursor=int(tail[4]), revision=tail[5] if len(tail) > 5 else None)


def link_state(daemon):
    active = bool(daemon.link and daemon.link.opened)
    borrowed = daemon.disk_control.serial_lease is not None
    disk = not active and not borrowed and os.path.exists(UUID_PATH)
    return dict(connected=active or disk, port=daemon.link_device if active or borrowed else None,
                mode="podlink" if active or borrowed else "disk" if disk else "none",
                serial=daemon.link_serial if active or borrowed else None,
                borrowed=borrowed, firmware=None)


def workspace_state(model, state_seen):
    def agent(row):
        pane = row["pane"]
        status, since = state_seen.get(pane, (None, None))
        if status != row.get("status"):
            since = None
        return dict(pane=pane, name=row.get("name"), kind=row.get("kind"),
                    status=row.get("status"), activity=row.get("last"), since=since)

    def tab(row):
        return dict(id=row["id"], name=row["label"],
                    agents=[agent(a) for a in model["tab_agents"].get(row["id"], [])])

    return [dict(id=w["id"], name=w["label"],
                 counts={key: w[key] for key in ("working", "waiting", "done", "idle")},
                 tabs=[tab(t) for t in model["tabs"].get(w["id"], [])])
            for w in model["workspaces"]]


def question_state(q, dismissed):
    return dict(id=q["id"], pane=q["pane"], workspace=q["workspace"],
                title=q.get("display_agent", q.get("agent")), text="\n".join(q["lines"]),
                options=[dict(number=n, label=label)
                         for n, label in q.get("all_options", q["options"])],
                multi=q["multi"], dismissed=dismissed)


def usage_state(row):
    status = "T3 unavailable" if row.get("note") == "T3 unavailable" else row["state"]
    cost = row["usd_milli"]
    return dict(provider=row["svc"], status=status,
                five_hour=dict(percent=row["h5_pct"], resets_at=row["h5_reset"]),
                weekly=dict(percent=row["wk_pct"], resets_at=row["wk_reset"]),
                tokens_week=row["tokens"], cost_usd=None if cost is None else cost / 1000,
                refreshing=row.get("refreshing", row.get("note") == "refreshing..."))


def voice_state(voice, last, pending):
    if pending and pending[0] in ("vu", "vc"):
        state = "submitting"
    elif voice.engaged and voice.down and not voice.expired:
        state = "recording"
    elif pending or not last or last[0]:
        state = "idle"
    else:
        state = "error"
    return dict(state=state, method=voice.voice_method, focus=voice.voice_focus,
                last_result=last[1] if last else None)
