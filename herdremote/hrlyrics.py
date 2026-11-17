"""FU59 host-side lyrics normalization and bounded native-page rows."""
import re
import threading
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
    if (not anchors or anchors != sorted(anchors) or shift(anchors[-1]) > MAX_TIME
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
        for n, i in enumerate(indices, 1):
            starts[i] = before + (after - before) * n // (len(indices) + 1) + offset

    for enhanced in (True, False):
        encoded, used = [], 0
        for (text, _, end, cues), start in zip(lines, starts):
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

    def get(self, query, track, offset, fetch, row):
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
            self.query, self.rows = query, rows
            return rows
