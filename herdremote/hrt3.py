"""T3 orchestration v2 over Effect RPC, using a scoped loopback session."""
import base64
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import ipaddress
import itertools
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect

import podlink_settings
from hrproto import USAGE_SERVICES

VERSION = "0.1.10"
TOKEN_FILE = "t3-session-token"
LABEL = "AstraPod"
AGENT_SCOPES = ("orchestration:read", "orchestration:operate")
USAGE_SCOPE = "diagnostics:read"
PREFIX = "orchestration."
LIVE = {"queued", "preparing", "starting", "running", "waiting"}
STATUSES = LIVE | {"idle", "completed", "interrupted", "failed", "cancelled", "rolled_back"}
DECISIONS = {"accept", "acceptForSession", "acceptAlways", "decline", "cancel"}
WIRE_LOG = logging.getLogger("herdremote.t3.wire")
WIRE_LOG.disabled = True  # WebSocket debug logging includes the authorization header.
UUID_SHAPE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


class SchemaError(ValueError):
    pass


def checked(value, kind):
    if type(value) is not kind:
        raise SchemaError("Invalid T3 field")
    return value


def required(record, name, kind):
    return checked(checked(record, dict).get(name), kind)


def runtime_endpoint(path):
    runtime = json.loads(path.read_text(encoding="utf-8"))
    version = required(runtime, "serverVersion", str)
    origin = urlsplit(required(runtime, "origin", str))
    port = required(runtime, "port", int)
    host = origin.hostname
    if (origin.scheme != "http" or not host or not ipaddress.ip_address(host).is_loopback
            or origin.username is not None or origin.path not in ("", "/")
            or origin.query or origin.fragment or origin.port != port):
        raise ValueError("Invalid T3 loopback origin")
    return f"ws://{origin.netloc}/ws?orchestrationProtocol=2", version


def session_claims(token):
    try:
        if len(token) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise ValueError
        encoded, signature = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not signature or claims["kind"] != "session" or claims["method"] != "bearer-access-token":
            raise ValueError
        scopes = set(claims["scopes"])
        if scopes not in (set(AGENT_SCOPES), {*AGENT_SCOPES, USAGE_SCOPE}):
            raise ValueError
        return str(uuid.UUID(checked(claims["sid"], str))), scopes
    except (ValueError, KeyError, TypeError):
        raise ValueError("Invalid scoped T3 session") from None


def session_id(token):
    return session_claims(token)[0]


def auth(binary, *args):
    try:
        result = subprocess.run([binary, "auth", "session", *args],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=10, check=True)
        return result.stdout.decode("ascii").strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ValueError("T3 session command failed") from None


def socket_connection(endpoint, token):
    return connect(endpoint, additional_headers={"Authorization": "Bearer " + token},
                   proxy=None, open_timeout=3, close_timeout=.25, max_size=64 << 20,
                   ping_interval=20, logger=WIRE_LOG)


def usage_rows(state="none", note="", now=None):
    now = time.time() if now is None else now
    return [dict(svc=svc, state=state, note=note, updated_at=now, h5_pct=None,
                 h5_reset=None, wk_pct=None, wk_reset=None, tokens=None, usd_milli=None)
            for svc in USAGE_SERVICES]


def epoch(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(checked(value, str))
    if parsed.tzinfo is None:
        raise SchemaError("Missing T3 timestamp offset")
    return parsed.timestamp()


def quantity(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise SchemaError("Invalid T3 usage quantity")
    return value


def map_usage(config, summary, now):
    rows = {r["svc"]: r for r in usage_rows(now=now)}
    providers = [checked(p, dict) for p in required(config, "providers", list)]
    # Prefer the built-in instance when it has limits. Never merge account quotas.
    providers = sorted(providers, key=lambda p: p.get("instanceId") != p.get("driver"))
    selected = set()
    for provider in providers:
        driver = provider.get("driver")
        service = "claude" if driver == "claudeAgent" else driver
        limits = provider.get("usageLimits")
        if service not in rows or service in selected or limits is None:
            continue
        row = rows[service]
        for window in required(limits, "windows", list):
            checked(window, dict)
            if window.get("modelIds") or window.get("providerIds"):
                continue
            duration = window.get("windowDurationMins")
            kind = window.get("kind")
            prefix = ("h5" if kind == "session" and duration == 300 else
                      "wk" if kind == "weekly" and duration in (None, 10080) else None)
            if prefix is None or any(row[prefix + key] is not None for key in ("_pct", "_reset")):
                continue
            pct = window.get("usedPercent")
            if pct is not None and quantity(pct) > 100:
                raise SchemaError("Invalid T3 usage percentage")
            row[prefix + "_pct"] = pct
            row[prefix + "_reset"] = epoch(window.get("resetsAt"))
            if pct is not None or row[prefix + "_reset"] is not None:
                row["state"] = "ok"
        if row["state"] == "ok":
            selected.add(service)
            row["updated_at"] = epoch(limits.get("checkedAt")) or now

    if summary is not None:
        costs = {}
        incomplete_tokens = set()
        incomplete_cost = set()
        for bucket in required(summary, "buckets", list):
            checked(bucket, dict)
            service = bucket.get("provider")
            if service not in rows:
                continue
            row = rows[service]
            totals = checked(bucket.get("totals", {}), dict)
            # T3's schema includes reasoning tokens in outputTokens.
            parts = [totals.get(key) for key in ("uncachedInputTokens", "cachedInputTokens",
                                                  "cacheCreationTokens", "outputTokens")]
            if None in parts:
                incomplete_tokens.add(service)
            else:
                row["tokens"] = (row["tokens"] or 0) + sum(map(quantity, parts))
            if (bucket.get("costSource") == "unpriced" or bucket.get("unpricedRecords")
                    or bucket.get("costUsd") is None):
                incomplete_cost.add(service)
            else:
                costs[service] = costs.get(service, 0) + quantity(bucket["costUsd"])
        for service in incomplete_tokens:
            rows[service]["tokens"] = None
        for service in costs.keys() - incomplete_cost:
            rows[service]["usd_milli"] = round(costs[service] * 1000)
        for row in rows.values():
            if row["state"] == "none" and (row["tokens"] is not None or row["usd_milli"] is not None):
                row["state"] = "tok"
    return list(rows.values())


def read_usage(runtime, token):
    now = datetime.now(timezone.utc)
    if not token:
        return usage_rows("err", "T3 unavailable")
    _, scopes = session_claims(token)
    endpoint, _ = runtime_endpoint(runtime)
    requests = {"0": ("server.getConfig", {})}
    if USAGE_SCOPE in scopes:
        start = now.date() - timedelta(days=6)
        requests["1"] = ("server.getUsageSummary", {
            "sinceDay": start.isoformat(), "untilDay": now.date().isoformat(),
            "timeZone": "UTC", "resolution": "day", "provisional": True,
        })
    replies = {}
    with socket_connection(endpoint, token) as socket:
        for rid, (method, payload) in requests.items():
            socket.send(json.dumps(dict(_tag="Request", id=rid, tag=method, payload=payload,
                                        headers=[])))
        deadline = time.monotonic() + 4
        while len(replies) < len(requests):
            try:
                decoded = json.loads(socket.recv(timeout=max(0, deadline - time.monotonic())))
            except TimeoutError:
                break
            for envelope in decoded if isinstance(decoded, list) else [decoded]:
                checked(envelope, dict)
                rid = envelope.get("requestId")
                if envelope.get("_tag") == "Exit" and rid in requests:
                    result = required(envelope, "exit", dict)
                    replies[rid] = result.get("value") if result.get("_tag") == "Success" else None
            if time.monotonic() >= deadline:
                break
    if replies.get("0") is None and replies.get("1") is None:
        return usage_rows("err", "T3 unavailable")
    return map_usage(replies.get("0") or {"providers": []}, replies.get("1"), now.timestamp())


class Usage:
    """One on-demand read at a time, independent of the Agents subscription."""
    def __init__(self, client):
        self.client = client
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="herdremote-usage")
        self.job = None
        self.token = None
        self.rows = usage_rows("err", "T3 unavailable")

    def start(self):
        self.request()

    def request(self):
        self.snapshot()
        if self.job is None and self.token:
            self.job = (self.token, self.worker.submit(self.read, self.token))
        return self.job is not None and self.job[0] == self.token

    def read(self, token):
        try:
            return read_usage(self.client.runtime, token)
        except (OSError, ValueError, TypeError, KeyError, OverflowError, WebSocketException):
            return usage_rows("err", "T3 unavailable")

    def snapshot(self):
        token = self.client.usage_token()
        if token != self.token:
            self.token = token
            self.rows = usage_rows("err", "T3 unavailable")
        if self.job is not None and self.job[1].done():
            requested, future = self.job
            self.job = None
            if requested == token:
                self.rows = future.result()
            else:
                self.request()
        if self.job is not None and self.job[0] == token:
            return [dict(row, refreshing=True) for row in self.rows]
        return self.rows

    def close(self):
        self.worker.shutdown(wait=True, cancel_futures=True)


class Client:
    def __init__(self, runtime=None):
        self.runtime = runtime or Path.home() / ".t3/userdata/server-runtime.json"
        self.lock = threading.Lock()
        self.auth_lock = threading.Lock()
        self.enabled = False
        self.binary = None
        self.token = None
        self.demand = False
        self.select_threads = lambda projects, threads, selection: ()
        self.selection = (None, None, None)
        self.generation = 0
        self.version = None
        self.error = None
        self.connected = False
        self.picture = ({}, {}, {})
        self.jobs = queue.Queue(maxsize=16)
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.worker = None

    def configure(self, enabled, binary):
        with self.auth_lock:
            try:
                token = podlink_settings.read_private_token(TOKEN_FILE)
                if token:
                    session_id(token)
            except (OSError, ValueError):
                self.set_config(enabled, binary, None, "Invalid T3 session file")
            else:
                self.set_config(enabled, binary, token, None)

    def set_config(self, enabled, binary, token, error):
        with self.lock:
            self.binary = binary
            if ((enabled, token) != (self.enabled, self.token) or error
                    or (token is None and self.error is not None)):
                self.enabled, self.token = enabled, token
                self.generation += 1
                self.set_status(False, error)
        self.wake.set()

    def t3_connect(self):
        with self.auth_lock:
            if not self.token:
                token = auth(self.auth_binary(), "issue", "--scope", AGENT_SCOPES[0],
                             "--scope", AGENT_SCOPES[1], "--scope", USAGE_SCOPE, "--label", LABEL,
                             "--ttl", "90d", "--token-only")
                sid = session_id(token)
                try:
                    podlink_settings.write_private(
                        podlink_settings.settings_path().with_name(TOKEN_FILE), token)
                except OSError:
                    auth(self.auth_binary(), "revoke", sid)
                    raise ValueError("Could not save the T3 session") from None
                self.set_config(self.enabled, self.binary, token, None)
        return self.state()

    def t3_disconnect(self):
        with self.auth_lock:
            if self.token:
                auth(self.auth_binary(), "revoke", session_id(self.token))
                try:
                    podlink_settings.settings_path().with_name(TOKEN_FILE).unlink(missing_ok=True)
                finally:
                    self.set_config(self.enabled, self.binary, None, None)
        return self.state()

    def auth_binary(self):
        fallback = Path.home() / ".local/bin/t3"
        return self.binary or shutil.which("t3") or os.fspath(fallback)

    def state(self):
        with self.lock:
            return dict(enabled=self.enabled, connected=self.connected,
                        server_version=self.version, error=self.error,
                        session_label=LABEL if self.token else None)

    def snapshot(self):
        with self.lock:
            return self.picture

    def usage_token(self):
        with self.lock:
            return self.token

    def open_thread(self, thread):
        """Ask the local T3 desktop to show a thread. Run in the focus worker."""
        if not UUID_SHAPE.fullmatch(thread):
            raise ValueError("Invalid T3 thread ID")
        try:
            environment = self.runtime.with_name("environment-id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            raise ValueError("T3 environment ID unavailable") from None
        if not UUID_SHAPE.fullmatch(environment):
            raise ValueError("Invalid T3 environment ID")
        url = f"t3code://threads/{environment}/{thread}"
        try:
            subprocess.run(["xdg-open", url], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=2, check=True)
        except subprocess.TimeoutExpired:
            raise ValueError("T3 desktop open timed out") from None
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Could not open T3 desktop") from None

    def set_status(self, connected, error):
        self.connected, self.error = connected, error
        if not connected:
            self.picture = ({}, {}, {})

    def start(self):
        self.worker = threading.Thread(target=self.run, name="herdremote-t3", daemon=True)
        self.worker.start()

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.worker:
            self.worker.join()

    def watch(self, demand, selection):
        with self.lock:
            if (self.demand, self.selection) == (demand, selection):
                return
            self.selection = selection
            if self.demand != demand:
                self.demand = demand
                self.generation += 1
                if not demand:
                    self.set_status(False, self.error)
        self.wake.set()

    def dispatch(self, thread, command):
        future = Future()
        with self.lock:
            if not self.connected:
                raise ValueError("T3 unavailable")
            try:
                self.jobs.put_nowait((self.generation, future, {
                    **command, "threadId": thread, "commandId": str(uuid.uuid4())
                }))
            except queue.Full:
                raise ValueError("T3 busy") from None
        return future

    def run(self):
        backoff = 0.25
        last_generation = None
        incompatible = None
        while not self.stopping.is_set():
            self.wake.clear()
            with self.lock:
                generation, token = self.generation, self.token
                wanted = self.demand and self.enabled and token
            if generation != last_generation:
                backoff = 0.25
                incompatible = None
                last_generation = generation
            if not wanted:
                self.wake.wait(1)
                continue
            version = None
            try:
                endpoint, version = runtime_endpoint(self.runtime)
                with self.lock:
                    if generation != self.generation:
                        continue
                    self.version = version
                if (endpoint, version) == incompatible:
                    self.wake.wait(1)
                    continue
                with socket_connection(endpoint, token) as socket:
                    self.stream(socket, generation)
                backoff = 0.25
                continue
            except InvalidStatus as error:
                status = error.response.status_code
                if status == 426:
                    incompatible = (endpoint, version)
                    message = self.incompatible(version)
                else:
                    message = "T3 authentication failed" if status in (401, 403) else "T3 handshake failed"
            except (SchemaError, KeyError, TypeError, UnicodeError, json.JSONDecodeError):
                message = self.incompatible(version)
                if version is not None:
                    incompatible = (endpoint, version)
            except (OSError, ValueError, TimeoutError, WebSocketException):
                message = "T3 server unavailable"
            with self.lock:
                if generation == self.generation:
                    if self.connected:
                        backoff = 0.25
                    self.set_status(False, message)
            self.wake.wait(backoff)
            backoff = min(8, backoff * 2)

    @staticmethod
    def incompatible(version):
        return f"T3 {version or VERSION} 的接口与 AstraPod 不兼容"

    def stream(self, socket, generation):
        with self.lock:
            if generation != self.generation:
                return
        rpc = Stream(socket)
        try:
            while not self.stopping.is_set():
                with self.lock:
                    if generation != self.generation:
                        return
                    selection = self.selection
                desired = set(self.select_threads(rpc.projects, rpc.threads, selection))
                desired.update(t["id"] for t in rpc.threads.values()
                               if t["pendingRuntimeRequest"] is not None)
                rpc.watch_threads(desired, selection)
                while not self.jobs.empty():
                    job_generation, future, command = self.jobs.get_nowait()
                    with self.lock:
                        valid = job_generation == generation == self.generation
                    if not valid:
                        future.set_exception(ValueError("T3 session changed"))
                        continue
                    rpc.dispatch(command, future)
                if rpc.receive() and rpc.ready:
                    with self.lock:
                        if generation == self.generation:
                            self.picture = (rpc.projects.copy(), rpc.threads.copy(),
                                            rpc.details.copy())
                            self.set_status(True, None)
        finally:
            rpc.close()
            with self.lock:
                if generation == self.generation:
                    self.set_status(False, "T3 connection closed")
                while not self.jobs.empty():
                    _, future, _ = self.jobs.get_nowait()
                    future.set_exception(ValueError("T3 connection closed"))


class Stream:
    """One socket's request IDs, subscriptions and subscribed projections."""
    def __init__(self, socket):
        self.socket = socket
        self.ids = itertools.count()
        self.projects, self.threads, self.details = {}, {}, {}
        self.watched = {}
        self.commands = {}
        self.archive = None
        self.archive_loaded = False
        self.archive_changes = set()
        self.ready = False
        self.has_snapshot = False
        self.deadline = time.monotonic() + 10
        self.shell = self.request("subscribeShell", {"requestCompletionMarker": True})

    def send(self, envelope, **fields):
        self.socket.send(json.dumps({"_tag": envelope, **fields}))

    def request(self, method, payload):
        request_id = str(next(self.ids))
        self.send("Request", id=request_id, tag=PREFIX + method, payload=payload, headers=[])
        return request_id

    def dispatch(self, command, future):
        try:
            request_id = self.request("dispatchCommand", command)
        except Exception:
            future.set_exception(ValueError("T3 connection closed"))
            raise
        self.commands[request_id] = future, time.monotonic() + 10

    def watch_threads(self, desired, selection):
        if self.ready and selection[1] == "all" and not (self.archive_loaded or self.archive):
            self.archive = self.request("getArchivedShellSnapshot", {})
        watching = set(self.watched.values())
        for request_id, thread in tuple(self.watched.items()):
            if thread not in desired:
                self.send("Interrupt", requestId=request_id)
                del self.watched[request_id]
                self.details.pop(thread, None)
        for thread in sorted(desired - watching):
            request_id = self.request("subscribeThread", {
                "threadId": thread, "acceptBoundedSnapshot": True,
            })
            self.watched[request_id] = thread

    def receive(self):
        now = time.monotonic()
        if not self.ready and now >= self.deadline:
            raise TimeoutError
        if any(now >= deadline for _, deadline in self.commands.values()):
            raise TimeoutError
        try:
            decoded = json.loads(self.socket.recv(timeout=.1))
        except TimeoutError:
            return False
        changed = False
        for message in decoded if isinstance(decoded, list) else [decoded]:
            tag = required(message, "_tag", str)
            if tag == "Pong":
                continue
            request_id = required(message, "requestId", str)
            if tag == "Chunk":
                values = required(message, "values", list)
                for item in values:
                    if request_id == self.shell:
                        self.shell_item(item)
                    elif request_id in self.watched:
                        self.thread_item(self.watched[request_id], item)
                self.send("Ack", requestId=request_id)
                changed = True
            elif tag == "Exit":
                changed |= self.exit(request_id, required(message, "exit", dict))
            else:
                raise SchemaError("Invalid T3 RPC envelope")
        return changed

    def exit(self, request_id, exit):
        success = required(exit, "_tag", str)
        if success not in ("Success", "Failure"):
            raise SchemaError("Invalid T3 exit")
        if request_id in self.commands:
            future, _ = self.commands[request_id]
            if success == "Success":
                required(exit.get("value"), "sequence", int)
                future.set_result(None)
            elif any(cause.get("_tag") == "Die" for cause in required(exit, "cause", list)):
                raise SchemaError("T3 command schema rejected")
            else:
                future.set_exception(ValueError("T3 rejected the action"))
            del self.commands[request_id]
        elif request_id == self.archive and success == "Success":
            archive = exit.get("value")
            if required(archive, "schemaVersion", int) != 2:
                raise SchemaError("Invalid T3 archive version")
            for thread in required(archive, "threads", list):
                tid = thread_id(thread)
                if tid not in self.archive_changes:
                    self.threads.setdefault(tid, thread)
            self.archive_changes.clear()
            self.archive_loaded = True
            return True
        elif request_id == self.shell or request_id == self.archive:
            self.stream_failed(exit)
        elif request_id in self.watched:
            if success == "Failure":
                self.stream_failed(exit)
            self.details.pop(self.watched.pop(request_id), None)
            return True
        return False

    @staticmethod
    def stream_failed(exit):
        if any(required(cause, "_tag", str) == "Die" for cause in exit.get("cause", ())):
            raise SchemaError("T3 subscription schema rejected")
        raise OSError("T3 subscription closed")

    def shell_item(self, item):
        kind = required(item, "kind", str)
        if kind == "snapshot":
            snapshot = required(item, "snapshot", dict)
            if required(snapshot, "schemaVersion", int) != 2:
                raise SchemaError("Invalid T3 shell version")
            projects = {project_id(p): p for p in required(snapshot, "projects", list)}
            threads = {thread_id(t): t for key in ("threads", "archivedThreads")
                       for t in required(snapshot, key, list)}
            if "resolvedRepositoryIdentityRoots" in item:
                self.projects.update(projects)
            else:
                self.projects, self.threads = projects, threads
        elif kind == "synchronized":
            if not self.has_snapshot:
                raise SchemaError("T3 shell synchronization without snapshot")
            self.ready = True
        elif kind == "project.updated":
            project = item["project"]
            self.projects[project_id(project)] = project
        elif kind == "project.removed":
            self.projects.pop(required(item, "projectId", str), None)
        elif kind == "thread.updated":
            thread = item["thread"]
            tid = thread_id(thread)
            if self.archive and not self.archive_loaded:
                self.archive_changes.add(tid)
            self.threads[tid] = thread
        elif kind == "thread.removed":
            tid = required(item, "threadId", str)
            if self.archive and not self.archive_loaded:
                self.archive_changes.add(tid)
            current = self.threads.get(tid)
            if current and bool(current.get("archivedAt")) == (item["location"] == "archive"):
                del self.threads[tid]
        else:
            raise SchemaError("Invalid T3 shell event")
        if kind == "snapshot":
            self.has_snapshot = True

    def thread_item(self, thread, item):
        kind = required(item, "kind", str)
        if kind == "snapshot":
            projection = required(item, "projection", dict)
            if required(projection.get("thread"), "id", str) != thread:
                raise SchemaError("T3 projection target mismatch")
            self.details[thread] = {
                name: dict(filter(None, (project_record(name, row)
                                         for row in required(projection, name, list))))
                for name in ("messages", "runtimeRequests", "turnItems")
            }
        elif kind == "event":
            event = required(item, "event", dict)
            name = {"message.updated": "messages", "runtime-request.updated": "runtimeRequests",
                    "turn-item.updated": "turnItems"}.get(required(event, "type", str))
            if name and thread in self.details:
                record = project_record(name, event["payload"])
                if record:
                    old = self.details[thread]
                    self.details[thread] = {**old, name: {**old[name], record[0]: record[1]}}
        elif kind != "synchronized":
            raise SchemaError("Invalid T3 thread event")

    def close(self):
        for future, _ in self.commands.values():
            future.set_exception(ValueError("T3 action outcome unknown"))


def project_id(row):
    required(row, "title", str)
    return required(row, "id", str)


def thread_id(row):
    for field in ("projectId", "title", "updatedAt", "providerInstanceId"):
        required(row, field, str)
    required(required(row, "modelSelection", dict), "model", str)
    if required(row, "status", str) not in STATUSES:
        raise SchemaError("Invalid T3 thread status")
    for field in ("pendingRuntimeRequest", "latestVisibleMessage", "activeRunId"):
        if field not in row:
            raise SchemaError("Missing T3 thread field")
    summary = row["latestVisibleMessage"]
    if summary is not None:
        required(summary, "role", str)
        required(summary, "text", str)
    pending = row["pendingRuntimeRequest"]
    if pending is not None:
        required(pending, "id", str)
        required(pending, "createdAt", str)
    return required(row, "id", str)


def project_record(name, row):
    identity = required(row, "id", str)
    if name == "messages":
        required(row, "text", str)
        required(row, "role", str)
        required(row, "createdAt", str)
    elif name == "runtimeRequests":
        required(row, "status", str)
        required(row, "kind", str)
        required(row, "createdAt", str)
    else:
        kind = required(row, "type", str)
        if kind not in ("approval_request", "user_input_request"):
            return None
        required(row, "requestId", str)
        if kind == "approval_request":
            for option in checked(row.get("options", []), list):
                if required(option, "decision", str) not in DECISIONS:
                    raise SchemaError("Invalid T3 approval option")
                required(option, "label", str)
        else:
            for question in required(row, "questions", list):
                required(question, "id", str)
                required(question, "question", str)
                for option in required(question, "options", list):
                    required(option, "label", str)
                    if "value" in option:
                        required(option, "value", str)
    return identity, row
