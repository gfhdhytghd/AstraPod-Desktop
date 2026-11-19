"""FU59 host-side lyrics normalization and bounded native-page rows."""
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal
from itertools import groupby


ROW_BYTES = 116
LINE_BYTES = 767  # firmware's existing UTF-8 LRC scratch, excluding NUL
DOCUMENT_BYTES = 32 * 1024
MAX_TIME = 5_999_990  # 99:59.99, the firmware parser's largest timestamp
LITERAL_STAMP = re.compile(r'^\[\d{1,2}:\d{2}(?:[.:]\d{2,3})?\]|<\d{1,2}:\d{2}(?:[.:]\d{2,3})?>', re.ASCII)


def millis(value):
    return value if type(value) is int and value >= 0 else None


def safe_text(text):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', text)


def chunks(text):
    raw = text.encode('utf-8')
    at = 0
    while at < len(raw):
        part = raw[at:at + ROW_BYTES].decode('utf-8', 'ignore')
        yield part
        at += len(part.encode('utf-8'))


def stamp(time):
    seconds, hundredths = divmod(time // 10, 100)
    return f'{seconds // 60:02}:{seconds % 60:02}.{hundredths:02}'


def word_lrc(text, start, end, cues, shift):
    """Use only ordered, valid byte ranges covering the visible words."""
    raw = text.encode('utf-8')
    parts = []
    at, last = 0, start
    for begin, stop, first, limit in cues:
        if not (type(first) is type(limit) is int and at <= first < limit <= len(raw)):
            return text, 1
        if raw[first] & 0xc0 == 0x80 or (limit < len(raw) and raw[limit] & 0xc0 == 0x80):
            return text, 1
        before = raw[at:first].decode('utf-8')
        if before.strip():
            return text, 1
        begin = max(start, shift(begin))
        if begin < last:
            return text, 1
        if before:
            if not parts:
                parts.append(f'<{stamp(start)}>')
            parts.append(before)
        parts.extend((f'<{stamp(begin)}>', raw[first:limit].decode('utf-8')))
        at, last = limit, begin
        stop = shift(stop) if stop is not None else (end if cues[-1][3] == limit else None)
        if stop is not None and stop > begin:
            parts.append(f'<{stamp(stop)}>')
            last = stop
    after = raw[at:].decode('utf-8')
    if not parts or after.strip():
        return text, 1
    parts.append(after)
    # Each marker costs at most one word struct, plus an initial text run.
    return ''.join(parts), len(cues) * 2 + 1


def plain_rows(texts, row):
    rows, used = [], 0
    for part in (part for text in texts for part in chunks(text)):
        used += len(part.encode('utf-8')) + 29  # line, word, index and NUL
        if used > DOCUMENT_BYTES:
            break
        rows.append(row(part, '', id=str(len(rows))))
    return rows or [row('No lyrics', '')]


def lyrics_rows(lines, offset, duration, row):
    """Prefer enhanced LRC, followed by line LRC, followed by plain rows.

    Lines contain text, optional start/end in ms and a list of byte-range cues.
    """
    lines = [(safe_text(text), start, end, cues) for text, start, end, cues in lines if text.strip()]
    texts = [line[0] for line in lines]
    shift = lambda time: max(0, time - offset)
    starts = [start if start is not None else (cues[0][0] if cues else None)
              for _, start, _, cues in lines]
    anchors = [value for value in starts if value is not None]
    if (not anchors or shift(max(anchors)) > MAX_TIME
            or any(LITERAL_STAMP.search(text) for text in texts)):
        return plain_rows(texts, row)
    # Place missing line starts between real anchors without dropping text.
    for missing, run in groupby(range(len(starts)), key=lambda i: starts[i] is None):
        if not missing:
            continue
        indices = list(run)
        first, last = indices[0], indices[-1]
        before = shift(starts[first - 1]) if first else 0
        after = shift(starts[last + 1]) if last + 1 < len(starts) else min(MAX_TIME, max(before, duration))
        after = max(before, after)
        for n, i in enumerate(indices, 1):
            starts[i] = before + (after - before) * n // (len(indices) + 1) + offset

    # LRC file order need not match playback order. Keep simultaneous
    # lines in file order and paginate by playback time.
    timed_lines = sorted(zip(lines, starts), key=lambda entry: entry[1])
    for enhanced in (True, False):
        encoded, used = [], 0
        for (text, _, end, cues), start in timed_lines:
            start = shift(start)
            end = min(MAX_TIME, shift(end)) if end is not None else None
            body, words = text, 1
            if enhanced and cues and all(shift(t) <= MAX_TIME for cue in cues for t in cue[:2] if t is not None):
                body, words = word_lrc(text, start, end, cues, shift)
            line = f'[{stamp(start)}]{body}'
            size = len(line.encode('utf-8'))
            # Conservative 32-bit model cost, including line/index and word
            # structs, NUL, alignment slack and the wire timestamp bytes.
            used += size + 24 + words * 8
            if size > LINE_BYTES or used > DOCUMENT_BYTES:
                break
            encoded.append(line)
        else:
            rows = []
            for line in encoded:
                parts = list(chunks(line))
                rows.extend(row(part, '', id='lrc1+' if i < len(parts) - 1 else 'lrc1')
                            for i, part in enumerate(parts))
            return rows
    return plain_rows(texts, row)


def lyric_sources(reply):
    value = reply.get('data', reply) if isinstance(reply, dict) else reply
    if isinstance(value, list):
        for item in value:
            yield from lyric_sources(item)
    elif isinstance(value, dict):
        for key in ('ttml', 'lyrics', 'text', 'content'):
            if value.get(key):
                yield from lyric_sources(value[key])
                return
        if isinstance(value.get('attributes'), dict):
            yield from lyric_sources(value['attributes'])
    elif isinstance(value, str):
        yield value


def ttml_time(value):
    """Apple lyrics use absolute media times, with optional hour fields."""
    if not value or len(value) > 20:
        return None
    clock = re.fullmatch(r'(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)(ms|s)?', value, re.ASCII)
    if not clock:
        return None
    hours, minutes, seconds, unit = clock.groups()
    if hours is not None and minutes is None:
        hours, minutes = None, hours
    if unit and minutes is not None:
        return None
    return int(Decimal(seconds) * (1 if unit == 'ms' else 1000)) + (
        int(hours or 0) * 3600 + int(minutes or 0) * 60) * 1000


def ttml_line(p):
    parts, cues = [], []
    pos = 0

    def append(text):
        nonlocal pos
        if text:
            parts.append(text)
            pos += len(text.encode('utf-8'))

    def inline(node, depth):
        first = pos
        append(node.text)
        count = len(cues)
        for child in node:
            # Excess nesting has no effect on timing and stays plain text.
            if depth < 16:
                inline(child, depth + 1)
            else:
                append(''.join(child.itertext()))
            append(child.tail)
        begin = ttml_time(node.get('begin'))
        if depth and begin is not None and pos > first and len(cues) == count:
            cues.append((begin, ttml_time(node.get('end')), first, pos))

    inline(p, 0)
    return ''.join(parts), ttml_time(p.get('begin')), ttml_time(p.get('end')), cues


def ttml_tree(source):
    if '<!DOCTYPE' in source or '<!ENTITY' in source:
        return None
    try:
        return ET.fromstring(source)
    except ET.ParseError:
        return None


def local_name(tag):
    return tag.rsplit('}', 1)[-1]


def lyric_lines(reply):
    for source in lyric_sources(reply):
        if not source.lstrip('\ufeff \t\r\n').startswith('<'):
            yield from ((line, None, None, []) for line in source.splitlines())
            continue
        tree = ttml_tree(source)
        if tree is not None:
            for p in tree.iter():
                if local_name(p.tag) == 'p':
                    yield ttml_line(p)


TTM = '{http://www.w3.org/ns/ttml#metadata}'
XML_ID = '{http://www.w3.org/XML/1998/namespace}id'
TTML_PACKET_BYTES = 8192
TTML_DOCUMENT_BYTES = 96 * 1024
TTML_AGENTS = 64
TTML_PERSON = 3
TTML_GROUP = 0x100
TTML_OTHER = 0x200


@dataclass
class LyricsVariants:
    legacy: list
    duet: list


def duet_rows(trees, offset, duration, row):
    """Emit independent, bounded paragraphs with track-wide performer indices.

    Code 0 is unspecified and 1 is a list of performers. The remaining
    codes identify each performer in a range for their type. The subtitle
    carries the track-wide performer count for stable progressive layout.
    """
    indices, kinds, seen, paragraphs = {}, {}, set(), []
    counts = {TTML_PERSON: 0, TTML_GROUP: 0, TTML_OTHER: 0}
    has_voices = False
    for source, tree in enumerate(trees):
        for agent in tree.iter(TTM + 'agent'):
            if agent.get(XML_ID):
                kinds[source, agent.get(XML_ID)] = agent.get('type', 'person')

    def performer(ids, source):
        nonlocal has_voices
        ids = dict.fromkeys(ids.split())
        seen.update((source, name) for name in ids)
        has_voices |= bool(ids)
        codes = []
        for name in ids:
            key = source, name
            kind = kinds.get(key, 'person')
            if key not in indices:
                base = {'person': TTML_PERSON, 'group': TTML_GROUP}.get(kind, TTML_OTHER)
                indices[key] = base + counts[base]
                counts[base] += 1
            codes.append(indices[key])
        return 1 if len(codes) > 1 else next(iter(codes), 0)

    def node_agent(node, source, default):
        ids = node.get(TTM + 'agent')
        return performer(ids, source) if ids is not None else default

    def timing(source, target):
        begin, end, dur = (ttml_time(source.get(name)) for name in ('begin', 'end', 'dur'))
        if end is None and begin is not None and dur is not None:
            end = begin + dur
        for name, value in (('begin', begin), ('end', end)):
            if value is not None:
                target.set(name, f'{max(0, value - offset)}ms')
        if dur is not None and end is None:
            target.set('dur', f'{dur}ms')
        return begin

    def inline(node, source, inherited, depth=0):
        nonlocal has_voices
        if depth > 16:
            raise ValueError('TTML span nesting')
        agent = node_agent(node, source, inherited)
        target = ET.Element('span', {'a': str(agent)} if agent != inherited else {})
        timing(node, target)
        if node.get(TTM + 'role') == 'x-bg':
            has_voices = True
            target.set('bg', '1')
        target.text = safe_xml_text(node.text)
        for child in node:
            if local_name(child.tag) == 'br':
                item = ET.Element('br')
            else:
                item = inline(child, source, agent, depth + 1)
            item.tail = safe_xml_text(child.tail)
            target.append(item)
        return target

    def collect(node, source, inherited=0, depth=0):
        if depth > 16:
            raise ValueError('TTML block nesting')
        kind = local_name(node.tag)
        if kind in ('head', 'metadata'):
            return
        agent = node_agent(node, source, inherited)
        if kind == 'p':
            target = ET.Element('p', {'a': str(agent)})
            begin = timing(node, target)
            if begin is None:
                # The firmware can infer a line start from its first timed span.
                begin = min((t for span in node.iter()
                             if (t := ttml_time(span.get('begin'))) is not None),
                            default=0)
            target.text = safe_xml_text(node.text)
            for child in node:
                span = ET.Element('br') if local_name(child.tag) == 'br' else inline(child, source, agent)
                span.tail = safe_xml_text(child.tail)
                target.append(span)
            paragraphs.append((begin, target))
        else:
            for child in node:
                collect(child, source, agent, depth + 1)

    try:
        for source, tree in enumerate(trees):
            collect(tree, source)
    except ValueError:
        return None
    if not has_voices or not paragraphs or len(seen) > TTML_AGENTS:
        return None

    records, cost = [], 0
    for _, p in sorted(paragraphs, key=lambda item: item[0]):
        visible = ''.join(p.itertext()).strip()
        if not visible:
            continue
        packet = ET.tostring(p, encoding='unicode', short_empty_elements=True)
        # Budget for both voice records and at most one firmware cue per
        # text/tail run. The firmware checks the same arena before each write.
        size = len(packet.encode())
        runs = sum(bool(n.text) + bool(n.tail) + (local_name(n.tag) == 'br') for n in p.iter())
        cost += sum(len((n.text or "").encode()) + len((n.tail or "").encode())
                    for n in p.iter()) + 84 + 17 * runs
        if size >= TTML_PACKET_BYTES or cost > TTML_DOCUMENT_BYTES:
            return None
        parts = list(chunks(packet))
        records.extend(dict(row(part, '', id='ttml2+' if i < len(parts) - 1 else 'ttml2'),
                            subtitle=str(len(seen)))
                       for i, part in enumerate(parts))
    return records or None


def safe_xml_text(value):
    if value is None:
        return None
    return re.sub(r'[\x00-\x1f\x7f]', ' ', value)


def source_rows(reply, offset, duration, row):
    legacy = lyrics_rows(lyric_lines(reply), offset, duration, row)
    sources = list(lyric_sources(reply))
    trees = [tree for text in sources if (tree := ttml_tree(text)) is not None]
    duet = duet_rows(trees, offset, duration, row) if len(trees) == len(sources) else None
    return LyricsVariants(legacy, duet) if duet else legacy


class LyricsPages:
    """One converted track in memory and one stable nonce snapshot."""
    def __init__(self):
        self._lock = threading.Lock()
        self._identity = None
        self._epoch = 0
        self._fetching = None
        self._ready = None
        self.query = None
        self.rows = []

    def select(self, track, run=None):
        """Only the current playback identity may publish a converted track."""
        identity = track, run
        with self._lock:
            if self._identity == identity:
                return False
            self._identity = identity
            self._epoch += 1
            self._ready = None
            self.query, self.rows = None, []
            return bool(track)

    def prefetch(self, track, fetch):
        with self._lock:
            epoch = self._epoch
            if not self._identity or self._identity[0] != track:
                return False
            if self._ready is not None:
                return True
            if self._fetching == epoch:
                return False
            self._fetching = epoch
        rows = None
        try:
            rows = fetch(track)
        finally:
            with self._lock:
                if self._fetching == epoch:
                    self._fetching = None
                if self._epoch == epoch and rows is not None:
                    self._ready = rows
        return rows is not None and self._epoch == epoch

    def get(self, query, track, offset, fetch, row, duet=False):
        if not re.fullmatch(r'[a-zA-Z0-9._-]{1,79}', track):
            return []
        wait = [row('', '', id='lyric-wait')]
        with self._lock:
            epoch = self._epoch
            managed = self._identity is not None
            if managed and self._identity[0] != track:
                return wait
            if self.query == query:
                return self.rows
            if offset:
                return []
            if managed and self._fetching == epoch:
                return wait
        # Older/standalone callers can start a query without bridge state.
        if managed:
            self.prefetch(track, fetch)
            rows = None
        else:
            rows = fetch(track)
        with self._lock:
            if self._epoch != epoch:
                return wait
            if managed:
                rows = self._ready
            if rows is None:
                return wait
            if isinstance(rows, LyricsVariants):
                rows = rows.duet if duet else rows.legacy
            self.query, self.rows = query, rows
            return rows
