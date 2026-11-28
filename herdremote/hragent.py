"""Project the T3 shell into Agents and execute advertised action sheets."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import count
import re
import uuid

import hrherdr
import hrproto
import hrt3
import podlink_settings

PROJECT = re.compile(r"t3p[0-9]+\Z")
FLAT = "t3"  # the workspace ID of the thread list across projects
THREAD = re.compile(r"t3t[0-9]+\Z")
DEFAULT_APPROVALS = (("decline", "Decline"), ("acceptForSession", "Always allow this session"),
                     ("accept", "Approve"))
STATUS_LABELS = {"blocked": "Waiting", "working": "Running", "done": "Completed", "idle": "Idle"}


def config(env):
    mode = env.get("AGENT_CONTROL", podlink_settings.FIELDS["AGENT_CONTROL"][1])
    if mode not in ("herdr", "t3"):
        raise ValueError("AGENT_CONTROL must be herdr or t3")
    follow = env.get("T3_FOLLOW_IPOD", podlink_settings.FIELDS["T3_FOLLOW_IPOD"][1])
    if follow not in ("on", "off"):
        raise ValueError("T3_FOLLOW_IPOD must be on or off")
    defaults = podlink_settings.FIELDS["AGENT_QUICK_REPLIES"][1]
    replies = tuple(line.strip() for line in env.get("AGENT_QUICK_REPLIES", defaults).splitlines()
                    if line.strip())
    if len(replies) > 8 or any(len(r.encode("utf-8")) > 60 or
                               any(ord(c) < 32 or ord(c) == 127 for c in r) for r in replies):
        raise ValueError("Use up to 8 quick replies of at most 60 bytes each")
    return mode, env.get("T3_BIN"), replies, follow == "on"


def instant(text):
    if not isinstance(text, str):
        return 0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0


def settled(row):
    return row.get("settledOverride") == "settled"


def status(row):
    if row["pendingRuntimeRequest"] is not None:
        return "blocked"
    if row["status"] in hrt3.LIVE or row.get("activityRunStatus") in hrt3.LIVE:
        return "working"
    if settled(row) or row["status"] != "idle":
        return "done"
    return "idle"


def rows_for(threads, project, tab):
    return ordered((row for row in threads.values() if row["projectId"] == project), tab)


def flat_rows(projects, threads, tab):
    """Every live project's main threads as one list, in rows_for's order."""
    live = {key for key, row in projects.items() if not row.get("archivedAt")}
    return ordered((row for row in threads.values() if row["projectId"] in live), tab)


def main_thread(row):
    lineage = row.get("lineage") or {}
    return not lineage.get("parentThreadId") and lineage.get("relationshipToParent") != "subagent"


def ordered(rows, tab):
    """All: newest first. Active: waiting (oldest request first), then
    running, then the rest, each newest first; settled, snoozed and archived
    threads are left out unless they wait."""
    rows = sorted((row for row in rows if not row.get("deletedAt") and main_thread(row)),
                  key=lambda row: (-instant(row["updatedAt"]), row["id"]))
    if tab == "all":
        return rows
    now = datetime.now(timezone.utc).timestamp()
    rows = [row for row in rows if not row.get("archivedAt") and
            (status(row) == "blocked" or
             (not settled(row) and instant(row.get("snoozedUntil")) <= now))]
    priority = {"blocked": 0, "working": 1, "idle": 2, "done": 2}
    return sorted(rows, key=lambda row: (
        priority[status(row)], instant((row["pendingRuntimeRequest"] or {}).get("createdAt"))))


def assistant_messages(detail):
    return sorted((message for message in detail.get("messages", {}).values()
                   if message["role"] in ("assistant", "user")),
                  key=lambda message: instant(message["createdAt"]))


def action_command(row, action, quick=None, now=None):
    if quick is not None:
        return dict(type="message.dispatch", messageId=str(uuid.uuid4()), text=quick,
                    attachments=[], createdBy="user", creationSource="mobile",
                    dispatchMode={"type": "queue_after_active"})
    if action == "interrupt":
        if not row["activeRunId"]:
            raise ValueError("No active T3 turn")
        return dict(type="run.interrupt", runId=row["activeRunId"], holdQueue=True)
    if action in ("settle", "pin", "unpin"):
        return dict(type="thread." + action)
    if action == "reopen":
        return dict(type="thread.unsettle", reason="user")
    now = now or datetime.now().astimezone()
    if action == "snooze1h":
        until = now.astimezone(timezone.utc) + timedelta(hours=1)
    else:
        tomorrow = now.date() + timedelta(days=1)
        # Convert a local calendar time using the offset on the target date.
        until = datetime.combine(tomorrow, datetime.min.time()).astimezone()
    return dict(type="thread.snooze",
                snoozedUntil=until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))


class Aliases:
    def __init__(self, prefix):
        self.prefix = prefix
        self.ids = count(1)
        self.forward = {}
        self.reverse = {}

    def alias(self, identity):
        if identity not in self.forward:
            alias = self.prefix + str(next(self.ids))
            self.forward[identity] = alias
            self.reverse[alias] = identity
        return self.forward[identity]

    def get(self, alias):
        return self.reverse.get(alias)


class Agents:
    def __init__(self, client=None):
        self.client = client or hrt3.Client()
        self.client.select_threads = self.select_threads
        self.projects = Aliases("t3p")
        self.threads = Aliases("t3t")
        self.qids = Aliases("t3q")
        self.approvals = Aliases("t3a")
        self.jobs = {}
        self.control, _, self.replies, self.follow_ipod = config({})
        self.reset()

    def reset(self):
        self.sheet = None
        self.drafts = {}
        self.choices = {}
        self.editing = None
        self.answered = set()
        self.recent_questions = {}

    def configure(self, settings):
        self.control, binary, self.replies, self.follow_ipod = settings
        self.sheet = None
        self.client.configure(self.control == "t3", binary)

    def focus_target(self, alias):
        identity = self.threads.get(alias)
        row = self.client.snapshot()[1].get(identity)
        if row is None or row.get("deletedAt"):
            raise ValueError("T3 thread unavailable")
        return identity, "Focused " + row["title"]

    def select_threads(self, projects, threads, selection):
        project, tab, detail = selection
        visible = (flat_rows(projects, threads, tab) if project == FLAT else
                   rows_for(threads, project, tab) if project in projects else ())
        visible = visible[:hrproto.MAX_AGENTS]
        return [row["id"] for row in visible if row["latestVisibleMessage"] is not None
                and row["latestVisibleMessage"]["role"] != "assistant"] + (
                    [detail] if detail in threads else [])

    def watch(self, desktop, player, workspace, tab, pane):
        self.client.watch(self.control == "t3" and (desktop or player), (
            (FLAT if workspace == FLAT else self.projects.get(workspace)) if player else None,
            tab if player else None,
            self.threads.get(pane) if player else None))

    def workspaces(self, picture):
        projects, threads, _ = picture
        counts = {}
        for row in threads.values():
            if not row.get("archivedAt") and not row.get("deletedAt"):
                counts.setdefault(row["projectId"], Counter())[status(row)] += 1
        return [dict(id=self.projects.alias(project["id"]), label=project["title"], focused=False,
                     tab1=hrherdr.best_state([code for key, code in hrproto.STATUS_CODES.items()
                                              if c[key]]), other="-",
                     working=c["working"], waiting=c["blocked"], done=c["done"], idle=c["idle"])
                for project in sorted(projects.values(), key=lambda row: (
                    not row.get("pinnedAt"), -instant(row.get("updatedAt")), row["id"]))
                if not project.get("archivedAt")
                for c in (counts.get(project["id"], Counter()),)]

    def tab_rows(self, picture, workspace, tab):
        projects, threads, details = picture
        project = self.projects.get(workspace)
        if project not in projects or projects[project].get("archivedAt"):
            return [], []
        tabs = []
        for name in ("active", "all"):
            rows = rows_for(threads, project, name)
            tabs.append(dict(id=name, label=name.title(), status=hrherdr.best_state(
                [hrproto.STATUS_CODES[status(row)] for row in rows])))
        rows = rows_for(threads, project, tab)[:hrproto.MAX_AGENTS]
        return tabs, [self.agent_row(row, details.get(row["id"], {})) for row in rows]

    def thread_rows(self, picture, tab):
        """The thread list across projects; the title field names the project."""
        projects, threads, details = picture
        rows = flat_rows(projects, threads, tab)[:hrproto.MAX_AGENTS]
        return [dict(self.agent_row(row, details.get(row["id"], {})),
                     title=projects[row["projectId"]]["title"]) for row in rows]

    def agent_row(self, row, detail):
        last = next((m["text"] for m in reversed(assistant_messages(detail))
                     if m["role"] == "assistant"), "")
        summary = row["latestVisibleMessage"] or {}
        if summary.get("role") == "assistant":
            last = summary["text"]
        provider = row["providerInstanceId"].split(":", 1)[0]
        return dict(pane=self.threads.alias(row["id"]), kind=provider,
                    status=status(row), question=row["pendingRuntimeRequest"] is not None,
                    name=row["title"], title=" · ".join(filter(None, (
                        row["modelSelection"]["model"], row.get("branch")))),
                    last=next(iter(last.splitlines()), ""))

    def detail(self, picture, pane, limit):
        _, threads, details = picture
        tid = self.threads.get(pane)
        row = threads.get(tid)
        if row is None:
            return hrproto.detail(pane, {}, limit)
        messages = assistant_messages(details.get(tid, {}))
        last = next((m["text"] for m in reversed(messages) if m["role"] == "assistant"), "")
        if not last and (row["latestVisibleMessage"] or {}).get("role") == "assistant":
            last = row["latestVisibleMessage"]["text"]
        pending = next((q for q in self.questions(picture) if q["pane"] == pane), None)
        steps = [("You" if m["role"] == "user" else "Assistant") + ": " + m["text"]
                 for m in messages[-3:]]
        fields = dict(task=row["title"], now=STATUS_LABELS[status(row)] + " · " + row["modelSelection"]["model"],
                      say=last, steps=steps, input="\n".join(pending["lines"]) if pending else "")
        if self.editing and self.editing[1] == pane:
            fields["input"] = ""
        while True:
            try:
                return hrproto.detail(pane, fields, limit)
            except ValueError:
                longest = max(("task", "now", "say", "input"), key=lambda key: len(fields[key]))
                if len(fields[longest]) > 1:
                    fields[longest] = fields[longest][:len(fields[longest]) // 2]
                elif fields["steps"]:
                    fields["steps"].pop(0)
                else:
                    raise

    def questions(self, picture, extended=False):
        projects, threads, details = picture
        questions = []
        pending = set()
        for tid, detail in details.items():
            row = threads.get(tid)
            if row is None:
                continue
            project = projects.get(row["projectId"])
            if project is None or project.get("archivedAt") or row.get("deletedAt"):
                continue
            requests = detail["runtimeRequests"]
            for request in requests.values():
                if request["status"] == "pending":
                    pending.add((tid, request["id"]))
            for item in detail["turnItems"].values():
                request = requests.get(item["requestId"])
                if not request or request["status"] != "pending":
                    continue
                key = (tid, request["id"])
                if key in self.answered:
                    continue
                question = self.question(item, key, extended)
                if question is None:
                    continue
                pane = self.threads.alias(tid)
                questions.append(dict(question, pane=pane, workspace=self.projects.alias(project["id"]),
                                      asked=instant(request["createdAt"]), key=key,
                                      agent=row["title"], tab=project["title"]))
        self.answered.intersection_update(pending)
        return sorted(questions, key=lambda q: (q["asked"], q["id"]))

    def question(self, item, key, extended):
        dismissible = item["type"] == "user_input_request"
        drafts = self.drafts.get(key, {})
        text_id, multi, custom = None, False, []
        if dismissible:
            fields = item["questions"]
            current = next((q for q in fields if q["id"] not in drafts), None)
            if current is None:
                return None
            text_id = current["id"]
            multi = current.get("multiSelect", False)
            label = current["question"]
            options = [(option.get("value", option["label"]), option["label"])
                       for option in current["options"]]
            if extended and current.get("allowCustomAnswer", True):
                options = options[:hrproto.MAX_OPTIONS - 1]
                custom = [len(options) + 1]
                options.append((None, "Type an answer"))
        else:
            fields = []
            label = item.get("prompt") or item.get("reason") or item.get("title") or "Approval requested"
            options = [(o["decision"], o["label"]) for o in item["options"]
                       ] if "options" in item else list(DEFAULT_APPROVALS)
        qid = (self.qids if dismissible else self.approvals).alias((*key, text_id))
        selected = self.choices.get(qid, ())
        numbered = list(enumerate((("[x] " if value in selected else "[ ] ") + text
                                   if multi and value is not None else text
                                   for value, text in options), 1))
        return dict(id=qid, lines=label.splitlines() or [label], options=numbered, custom=custom,
                    values=dict(enumerate((value for value, _ in options), 1)),
                    yes=0, no=0, dismissible=dismissible, text_id=text_id,
                    fields=fields, multi=multi)

    def actions(self, row, zoomed=False):
        if row is not None:
            actions = [
                ("interrupt", "Interrupt turn", True),
                ("reopen", "Reopen", False) if settled(row) else ("settle", "Mark done", False),
                ("snooze1h", "Snooze 1 h", False),
                ("tomorrow", "Snooze until tomorrow", False),
                ("unpin", "Unpin", False) if row.get("pinnedAt") else ("pin", "Pin", False),
            ]
        else:
            actions = [("interrupt", "Interrupt", True),
                       ("unzoom", "Unzoom", False) if zoomed else ("zoom", "Zoom", False)]
        # Keep the configured ordering. T3 has five fixed actions and twelve slots.
        return actions + [(f"reply{i}", text, False) for i, text in
                          enumerate(self.replies[:12 - len(actions)], 1)]

    def sheet_payload(self, target, row, zoomed, limit, valid):
        items = self.actions(row, zoomed) if valid else []
        payload = hrproto.actions(target, items, limit)
        self.sheet = (target, {fields[0]: (self.replies[int(fields[0][5:]) - 1]
                                             if fields[0].startswith("reply") else None)
                               for fields in hrproto.decode(payload)[1:]},
                      row["activeRunId"] if row is not None else None)
        return payload

    def action(self, daemon, req, target, action):
        if self.sheet is None or target != self.sheet[0] or action not in self.sheet[1]:
            raise ValueError("Action is no longer available")
        quick = self.sheet[1][action]
        run = self.sheet[2]
        self.sheet = None
        if THREAD.fullmatch(target):
            if not daemon.player_t3:
                raise ValueError("T3 unavailable")
            tid = self.threads.get(target)
            row = self.client.snapshot()[1].get(tid)
            if not row:
                raise ValueError("The T3 thread has gone")
            if action == "interrupt" and run != row["activeRunId"]:
                raise ValueError("The active T3 turn has changed")
            text = "Done"
            if quick is not None:
                text = "Queued" if row["activeRunId"] else "Sent"
            self.submit(daemon, req, tid, action_command(row, action, quick), text)
            return
        if self.control != "herdr" or not daemon.herdr_ok or target not in daemon.model["panes"]:
            raise ValueError("The herdr pane has gone")
        if quick is not None:
            if daemon.detail_context(target)["status"] == "blocked":
                raise ValueError("The herdr pane is waiting for an answer")
            daemon.herdr.call("pane.send_input", pane_id=target, text=quick, keys=["enter"])
        elif action == "interrupt":
            daemon.herdr.send_keys(target, ["esc"])
        else:
            daemon.herdr.call("pane.zoom", pane_id=target, mode="on" if action == "zoom" else "off")
        daemon.send(hrproto.result(req, True, "Sent" if quick is not None else "Done"))

    def answer(self, daemon, req, qid, number):
        q = self.live_question(daemon, qid)
        advertised = self.recent_questions[qid]
        if number > 0 and (number not in advertised or advertised[number] != q["values"].get(number)):
            raise ValueError("The T3 option has changed")
        if number == -2 and q["dismissible"]:
            self.submit(daemon, req, q["key"][0], dict(type="thread.user-input.dismiss",
                        requestId=q["key"][1]), "Dismissed", q["key"])
        elif number in q["custom"]:
            self.editing = (qid, q["pane"])
            daemon.send(hrproto.result(req, True, "Input ready"))
        elif q["multi"] and number > 0:
            choices = self.choices.setdefault(qid, [])
            value = q["values"][number]
            if value in choices:
                choices.remove(value)
            else:
                choices.append(value)
            daemon.send(hrproto.result(req, True, "Updated"))
        elif q["multi"] and number == 0 and self.choices.get(qid):
            self.respond(daemon, req, q, self.choices[qid][:])
        elif not q["multi"] and number > 0:
            self.respond(daemon, req, q, q["values"][number])
        else:
            raise ValueError("Invalid T3 answer")

    def live_question(self, daemon, qid):
        if not daemon.player_t3:
            raise ValueError("T3 unavailable")
        q = next((q for q in self.questions(self.client.snapshot(), daemon.question_ext)
                  if q["id"] == qid), None)
        if not q or qid not in self.recent_questions or any(
                key == q["key"] for _, _, _, key in self.jobs.values()):
            raise ValueError("The question has gone")
        return q

    def custom(self, daemon, req, qid, key, text=None):
        q = self.live_question(daemon, qid)
        if self.editing != (qid, daemon.detail_pane) or not q["custom"]:
            raise ValueError("Input is not focused")
        if key == "esc":
            self.editing = None
            daemon.send(hrproto.result(req, True, "Cancelled"))
        elif text and text.strip():
            self.respond(daemon, req, q, text)
            self.editing = None
        else:
            raise ValueError("Enter an answer")

    def respond(self, daemon, req, question, value):
        key = question["key"]
        command = dict(type="runtime-request.respond", requestId=key[1])
        if question["text_id"] is None:
            command["decision"] = value
        else:
            answers = {**self.drafts.get(key, {}), question["text_id"]: value}
            if len(answers) < len(question["fields"]):
                self.drafts[key] = answers
                self.choices.pop(question["id"], None)
                daemon.send(hrproto.result(req, True, "Next question"))
                return
            command["answers"] = answers
        self.submit(daemon, req, key[0], command, "Answered", key)

    def submit(self, daemon, req, tid, command, text, key=None):
        future = self.client.dispatch(tid, command)
        self.jobs[future] = daemon.session_generation, req, text, key

    def flush(self, daemon):
        for future, (generation, req, text, key) in tuple(self.jobs.items()):
            if not future.done():
                continue
            del self.jobs[future]
            if generation != daemon.session_generation or not daemon.link:
                continue
            try:
                future.result()
            except ValueError as error:
                daemon.send(hrproto.result(req, False, str(error)))
            else:
                if key is not None:
                    self.answered.add(key)
                    self.drafts.pop(key, None)
                daemon.send(hrproto.result(req, True, text))
