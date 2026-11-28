"""HerdRemote protocol v1: the messages carried in PodLink TEXT frames.

One message is one PodLink TEXT frame of UTF-8. Records are separated by
'\\n' and fields by '\\t'; the first field of the first record names the
message. Field text never contains either separator or any other control
character: encode() replaces them with spaces.
AstraPod's docs/podbox/herdremote-protocol.md is the reference; herdremote/tests
checks this module against it.
"""
import unicodedata
import datetime

VERSION = 1
MAX_MESSAGE = 16384

# Field budgets in UTF-8 bytes. They follow what the iPod screen stores and
# shows, so cutting here never loses text the player could have drawn.
LABEL_BYTES = 40
TITLE_BYTES = 60
LAST_BYTES = 90
QUESTION_BYTES = 120
QUESTION_LINES = 4
OPTION_BYTES = 90
MAX_OPTIONS = 8
MAX_WORKSPACES = 32
MAX_AGENTS = 16
MAX_QUESTIONS = 6
MAX_TABS = 32
DETAIL_TEXT_CHARS = 80
DETAIL_SAY_CHARS = 300
DETAIL_TEXT_BYTES = 240
DETAIL_SAY_BYTES = 900
USAGE_SERVICES = ("codex", "claude", "grok", "gemini")
USAGE_NOTE_BYTES = 40

STATUS_CODES = {"working": "w", "blocked": "b", "done": "d", "idle": "i",
                "unknown": "u"}


def clean(text, limit):
    """Replace control characters and cut to limit bytes on a UTF-8 boundary."""
    text = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in str(text))
    text = " ".join(text.split())
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def encode(records, max_message=MAX_MESSAGE):
    """records: a list of field lists. Returns the frame payload as bytes."""
    payload = "\n".join("\t".join(fields) for fields in records).encode("utf-8")
    maximum = min(MAX_MESSAGE, max_message)
    if len(payload) > maximum:
        raise ValueError("HerdRemote message exceeds %d bytes" % maximum)
    return payload


def decode(payload):
    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    return [line.split("\t") for line in text.split("\n")]


def _fit(head, rows, max_message=MAX_MESSAGE):
    """Drop trailing rows until head + rows fits one frame."""
    while True:
        try:
            return encode([head] + rows, max_message)
        except ValueError:
            if not rows:
                raise
            rows = rows[:-1]
            head = head[:-1] + [str(len(rows))]


def agent_mode(control):
    return encode([("M", control)])


def hello(daemon_version):
    return encode([["HR", str(VERSION), clean(daemon_version, 32)]])


def workspaces(items, max_message=MAX_MESSAGE):
    """items: dicts with id, label, focused, working, waiting, done, idle."""
    rows = [[clean(w["id"], 15), "f" if w["focused"] else "-",
             str(w["working"]), str(w["waiting"]), str(w["done"]),
             str(w["idle"]), clean(w["label"], LABEL_BYTES)]
            for w in items[:MAX_WORKSPACES]]
    first_t3 = next((i for i, row in enumerate(rows)
                     if row[0].startswith("t3p")), len(rows))
    if first_t3 == len(rows):
        return _fit(["W", str(len(rows))], rows, max_message)
    while len(rows) > first_t3:
        try:
            return encode([["W", str(len(rows))]] + rows[:first_t3] +
                          [["#", "T3"]] + rows[first_t3:], max_message)
        except ValueError:
            rows.pop()
    return _fit(["W", str(len(rows))], rows, max_message)


def status(items, max_message=MAX_MESSAGE):
    """items: dicts with id, tab1 and other, each a status letter or "-".

    Sent after every W; a player that does not know it keeps W's counts.
    """
    rows = [[clean(w["id"], 15), w["tab1"][:1] or "-", w["other"][:1] or "-"]
            for w in items[:MAX_WORKSPACES]]
    return _fit(["S", str(len(rows))], rows, max_message)


def agents(workspace_id, items, max_message=MAX_MESSAGE):
    """items: dicts with pane, kind, status, question, name, title, last."""
    rows = [[clean(a["pane"], 15), clean(a["kind"] or "?", 11),
             STATUS_CODES.get(a["status"], "u"),
             "1" if a["question"] else "0", clean(a.get("name") or "", 31),
             clean(a["title"], TITLE_BYTES), clean(a["last"], LAST_BYTES)]
            for a in items[:MAX_AGENTS]]
    return _fit(["A", clean(workspace_id, 15), str(len(rows))], rows, max_message)


def tabs(workspace_id, items, max_message=MAX_MESSAGE):
    """A complete tab list; seven-field T records cannot be mistaken for ticks."""
    items = items[:MAX_TABS]
    wid = clean(workspace_id, 15)
    while items:
        try:
            return encode([["T", wid, str(len(items)), str(i), clean(t["id"], 15),
                            clean(t["label"], LABEL_BYTES), t["status"]]
                           for i, t in enumerate(items)], max_message)
        except ValueError:
            items = items[:-1]
    return encode([["T", wid, "0", "0", "-", "", "-"]], max_message)


def actions(target, items, max_message=MAX_MESSAGE):
    rows = [[action, clean(label, LABEL_BYTES), str(int(confirm))]
            for action, label, confirm in items[:12]]
    return _fit(["K", clean(target, 15), str(len(rows))], rows, max_message)


def detail_text(text, limit, byte_limit=None):
    text = " ".join("".join(" " if unicodedata.category(c) == "Cc" else c
                            for c in str(text)).split())
    byte_limit = byte_limit if byte_limit is not None else limit * 4
    if len(text) <= limit and len(text.encode("utf-8")) <= byte_limit:
        return text
    head = text[:max(0, limit - 1)].encode("utf-8")[:max(0, byte_limit - 3)]
    return head.decode("utf-8", errors="ignore") + "…"


def detail_input(text, cursor, char_limit, byte_limit):
    """The input window/escapes exactly as DETAIL-DESIGN.md specifies."""
    text = "".join(c if c == "\n" or unicodedata.category(c) != "Cc" else " " for c in str(text))
    cursor = max(0, min(len(text), cursor if type(cursor) is int else len(text)))

    def escape(value):
        return value.replace("\\", "\\\\").replace("\n", "\\n")

    costs = [0]
    for char in text:
        costs.append(costs[-1] + (2 if char in "\\\n" else len(char.encode("utf-8"))))
    if len(text) <= char_limit and costs[-1] <= byte_limit:
        return escape(text), cursor
    end = min(len(text), cursor + 100)
    if end <= char_limit and costs[end] <= byte_limit:
        return escape(text[:end]), cursor
    start = end
    while start > 0:
        candidate = start - 1
        if end - candidate + 1 > char_limit or costs[end] - costs[candidate] + 3 > byte_limit:
            break
        start = candidate
    value = "…" + text[start:end]
    return escape(value), max(0, min(len(value), cursor - start + 1))


def detail(pane, fields, max_message=MAX_MESSAGE):
    age = max(-2, min(2147483647, int(fields.get("state_age_s", -1))))
    steps = list(fields.get("steps", []))[-3:]
    steps += [""] * (3 - len(steps))
    large = max_message >= MAX_MESSAGE
    typed_input, cursor = detail_input(fields.get("input", ""), fields.get("cursor"),
                                       4000 if large else 500, 12000 if large else 1500)
    return encode([["D", clean(pane, 15), str(age),
                    detail_text(fields.get("task", ""), DETAIL_TEXT_CHARS, DETAIL_TEXT_BYTES),
                    detail_text(fields.get("now", ""), DETAIL_TEXT_CHARS, DETAIL_TEXT_BYTES),
                    detail_text(fields.get("say", ""), DETAIL_SAY_CHARS, DETAIL_SAY_BYTES)] +
                   [detail_text(s, DETAIL_TEXT_CHARS, DETAIL_TEXT_BYTES) for s in steps] +
                   [typed_input, str(cursor)] +
                   ([clean(fields["revision"], 16)] if fields.get("revision") else [])],
                  max_message=max_message)


def question(q, extended=False, max_message=MAX_MESSAGE):
    """q: dict with id, pane, workspace, multi, agent, lines, options.

    options are (number, label) pairs; number is what the answer message
    sends back.
    """
    rows = [["?", clean(line, QUESTION_BYTES)]
            for line in q["lines"][-QUESTION_LINES:] if clean(line, 8)]
    rows += [["o", str(n), clean(label, OPTION_BYTES)]
             for n, label in (q.get("all_options", q["options"]) if extended else q["options"])[:MAX_OPTIONS]]
    if extended:
        for row in rows:
            if row[0] == "o":
                row.append("c" if int(row[1]) in q.get("custom", []) else "-")
    head = ["Q", q["id"], clean(q["pane"], 15), clean(q["workspace"], 15),
            "m" if q["multi"] else "-", clean(q.get("display_agent", q["agent"]) if extended else q["agent"], LABEL_BYTES)]
    if extended:
        head += [str(q.get("yes", 0)), str(q.get("no", 0)), clean(q.get("tab", ""), LABEL_BYTES)]
    while True:
        try:
            return encode([head] + rows, max_message)
        except ValueError:
            if not rows:
                raise
            rows.pop(0 if rows[0][0] == "?" else -1)


def question_gone(qid):
    return encode([["X", qid]])


def result(req, ok, text=None):
    fields = ["R", clean(req, 7), "ok" if ok else "err"]
    if text is not None:
        fields.append(clean(text, 120))
    return encode([fields])


def tick(age_seconds, herdr_ok):
    return encode([["T", str(int(age_seconds)), "1" if herdr_ok else "0"]])


def usage(items, now, *, replace_errors=False):
    """One U frame, four P records, and N. Reset fields are absolute in cache."""
    by_service = {item["svc"]: item for item in items}
    rows = []
    counts, costs = [], []
    oldest = now
    any_success = False
    # Bound numeric fields as well as text, so all four rows always fit.
    def integer(value):
        return str(max(0, min(10**18 - 1, int(value or 0))))

    for service in USAGE_SERVICES:
        item = by_service.get(service, {})
        state = item.get("state", "none")
        if state not in ("ok", "tok", "none", "err"):
            state = "err"
        any_success |= state != "err"
        oldest = min(oldest, item.get("updated_at", item.get("sampled_at", now)))
        fields = ["P", service, state]
        for prefix in ("h5", "wk"):
            pct = item.get(prefix + "_pct")
            reset = item.get(prefix + "_reset")
            fields += ["-" if pct is None else str(max(0, min(100, int(pct)))),
                       "-" if reset is None else integer(reset - now)]
        numbers = [item.get(key, 0) for key in ("tokens", "usd_milli")]
        for value, total in zip(numbers, (counts, costs)):
            if value is not None:
                total.append(int(integer(value)))
        fields += [integer(value) if value is not None else "-" for value in numbers]
        rows.append(fields + [clean(item.get("note", ""), USAGE_NOTE_BYTES)])
    # Current firmware retains its old rows on U 0. T3 error rows replace
    # stale figures so their explicit unavailable status reaches the screen.
    totals = [integer(sum(total)) if total else "-" for total in (counts, costs)]
    return encode([["U", integer(now - oldest), "1" if any_success or replace_errors else "0"]]
                  + rows + [["N", *totals]])


def parse_command(payload):
    """An iPod->PC message as (name, args), or None if it is not one."""
    try:
        records = decode(payload)
    except UnicodeDecodeError:
        return None
    fields = records[0]
    name, args = fields[0], fields[1:]
    if name == "vw":
        if len(records) != 1:
            return None
        if args in (["0"], ["1"]):
            return name, args
        if (len(args) == 2 and args[0] == "detail" and args[1] and
                len(args[1].encode('utf-8')) <= 15 and
                not any(ord(c) < 32 or ord(c) == 127 for c in args[1])):
            return name, args
        return None
    shapes = {"qx": 1, "tc": 1, "qi": 3, "hr": 1, "sub": 1, "fw": 2, "fp": 2, "a": 3, "us": 0,
              "ks": 1, "act": 3,
              "vd": 2, "vu": 1, "vc": 1, "k": 2, "ft": 3, "det": 1}
    if name in ("qx", "tc"):
        return (name, args) if len(records) == 1 and args == ["1"] else None
    if name == "qi":
        if (len(records) == 1 and len(args) == 4 and args[2] == "enter"
                and all(args) and len(args[0].encode("utf-8")) <= 7
                and len(args[1].encode("utf-8")) <= 11
                and len(args[3].encode("utf-8")) <= 1024
                and not any(ord(c) < 32 or ord(c) == 127 for arg in args for c in arg)):
            return name, args
        return (name, args) if len(records) == 1 and len(args) == 3 and args[2] in ("enter", "esc") and all(args) and len(args[0]) <= 7 and len(args[1]) <= 11 else None
    if name in ("ks", "act"):
        limits = (15,) if name == "ks" else (7, 15, 16)
        if len(records) == 1 and len(args) == len(limits) and all(
            0 < len(arg.encode("utf-8")) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in arg)
            for arg, limit in zip(args, limits)
        ):
            return name, args
        return None
    if name in ("sub", "ft", "det"):
        valid_lengths = (1, 2) if name == "sub" else (shapes[name],)
        if len(records) != 1 or len(args) not in valid_lengths:
            return None
        for i, arg in enumerate(args):
            limit = 7 if name == "ft" and i == 0 else 15
            if not arg or len(arg.encode("utf-8")) > limit or any(
                    ord(c) < 32 or ord(c) == 127 for c in arg):
                return None
        if name == "sub" and len(args) == 2 and args[0] == "-":
            return None
        return name, args
    if name in ("us", "vd", "vu", "vc", "k") and len(records) != 1:
        return None
    if name in ("vd", "vu", "vc", "k") and (
            not args or not args[0] or len(args[0].encode("utf-8")) > 7 or
            any(ord(c) < 32 or ord(c) == 127 for a in args for c in a)):
        return None
    if name == "vd" and (len(args) != 2 or not args[1] or
                         len(args[1].encode("utf-8")) > 17 or
                         args[1] == "w:"):
        return None
    if shapes.get(name) != len(args) and not (name == "k" and len(args) == 3):
        return None
    return name, args


def clock(local=None):
    """RTC local civil time, with an explicit UTC offset (seconds east)."""
    local = local or datetime.datetime.now().astimezone()
    offset = local.utcoffset()
    if offset is None or not 2000 <= local.year <= 2037:
        raise ValueError("RTC sync needs an aware local date in 2000..2037")
    seconds = int(offset.total_seconds())
    if abs(seconds) > 14 * 3600 or offset.total_seconds() != seconds:
        raise ValueError("Invalid UTC offset")
    return encode([["TS", "1", local.strftime("%Y-%m-%dT%H:%M:%S"), "%+d" % seconds]])
