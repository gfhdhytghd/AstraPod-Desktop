"""Host side of the iPod-local cover store: inventory ledger, diff and offers.

The player keeps covers in its own packs behind a hash table. The host never
trusts memory of what it sent: it mirrors the player's (art-id prefix, tier,
CRC) inventory in SQLite, checks it against the player's count/digest on every
connect, and re-lists only on a mismatch. Offers are cheap text; the player
pulls the bytes itself, only while its disk already spins."""
import struct
from bisect import bisect_right
import threading
import time
import zlib

TIERS = {'L': 0, 'M': 1, 'S': 2, 'XS': 3}
NAMES = {v: k for k, v in TIERS.items()}
LIST_END = 4096 << 5          # player cursor value that ends a listing
FILL_BYTES = {'XS': 2560, 'M': 33280, 'S': 6656}
FILL_ORDER = ('XS', 'M', 'S')  # whole library, cheapest browse tier first


BURST_COVERS = 6
BURST_MAX = 16364
_BURST_HEADER = struct.Struct('<4sII')
_BURST_ITEM = struct.Struct('<32sBHHH')


def burst_frames(generation, request, items, limit):
    """Pack consecutive cover fragments and terminate with a header-only frame."""
    header = _BURST_HEADER.pack(b'CFS1', generation, request)
    frame = bytearray(header)
    for key, tier, raw in items:
        binary_key = bytes.fromhex(key)
        offset = 0
        while offset < len(raw):
            room = limit - len(frame) - _BURST_ITEM.size
            if room <= 0:
                yield bytes(frame)
                frame = bytearray(header)
                room = limit - len(frame) - _BURST_ITEM.size
            count = min(room, len(raw) - offset)
            frame.extend(_BURST_ITEM.pack(binary_key, TIERS[tier], len(raw), offset, count))
            frame.extend(raw[offset:offset + count])
            offset += count
    if len(frame) > len(header):
        yield bytes(frame)
    yield header


def item_hash(k8, tier, crc):
    return zlib.crc32(k8 + bytes([tier]) + struct.pack('<I', crc))


def digest_of(items):
    """items: iterable of (k8, tier, crc). Same sum the player keeps."""
    return sum(item_hash(*i) for i in items) & 0xffffffff


class Ledger:
    """What the player holds. Rows are only ever replaced by player reports."""
    def __init__(self, cache):
        self.cache = cache
        with cache.lock, cache.db:
            cache.db.executescript('''CREATE TABLE IF NOT EXISTS ipod_art (
                k8 BLOB, tier INTEGER, crc INTEGER, PRIMARY KEY(k8,tier));
                CREATE TABLE IF NOT EXISTS ipod_meta (id INTEGER PRIMARY KEY, epoch INTEGER);
                INSERT OR IGNORE INTO ipod_meta VALUES (0,0);''')
        self.pending = None          # listing in progress: dict, epoch, expected
        self.synced = False

    def _rows(self):
        return [(bytes(k), t, c) for k, t, c in self.cache.db.execute('SELECT k8,tier,crc FROM ipod_art')]

    def summary(self):
        with self.cache.lock:
            rows = self._rows()
            epoch = self.cache.db.execute('SELECT epoch FROM ipod_meta').fetchone()[0]
        return epoch, len(rows), digest_of(rows)

    def status(self, epoch, count, digest):
        """The player's `ls`. True when the ledger already matches it."""
        self.synced = self.summary() == (epoch, count, digest)
        if not self.synced:
            self.pending = dict(rows={}, epoch=epoch, count=count, digest=digest, cursor=0)
        return self.synced

    def listing_start(self):
        return 0 if self.pending is not None else None

    def page(self, cursor, nxt, rows):
        """One `lv` page. Returns the next cursor to request, or None when the
        listing ended (ledger replaced on a verified match) or failed."""
        p = self.pending
        if p is None or cursor != p['cursor'] or nxt <= cursor:
            self.pending = None
            return None
        for k8, tier, crc in rows:
            p['rows'][(k8, tier)] = crc
        p['cursor'] = nxt
        if nxt < LIST_END:
            return nxt
        self.pending = None
        items = [(k, t, c) for (k, t), c in p['rows'].items()]
        if len(items) != p['count'] or digest_of(items) != p['digest']:
            return None              # inconsistent while the player changed; retry later
        with self.cache.lock, self.cache.db:
            self.cache.db.execute('DELETE FROM ipod_art')
            self.cache.db.executemany('INSERT INTO ipod_art VALUES (?,?,?)', items)
            self.cache.db.execute('UPDATE ipod_meta SET epoch=?', (p['epoch'],))
        self.synced = True
        return None

    def ack(self, key, tier, crc):
        k8 = bytes.fromhex(key[:16])
        with self.cache.lock, self.cache.db:
            self.cache.db.execute('INSERT OR REPLACE INTO ipod_art VALUES (?,?,?)', (k8, TIERS[tier], crc))

    def get(self, key, tier):
        with self.cache.lock:
            row = self.cache.db.execute('SELECT crc FROM ipod_art WHERE k8=? AND tier=?',
                                        (bytes.fromhex(key[:16]), TIERS[tier])).fetchone()
        return row[0] if row else None


def parse_rows(text):
    """`lv` body: 16-hex key prefix, tier digit, 8-hex CRC per line."""
    rows = []
    for line in text.split('\n'):
        if not line:
            continue
        if len(line) != 25 or not line[16].isdigit() or int(line[16]) > 3:
            raise ValueError('Invalid inventory row')
        rows.append((bytes.fromhex(line[:16]), int(line[16]), int(line[17:], 16)))
    return rows


class Planner:
    """Chooses what to offer next: now playing L first, then the library by tier."""
    def __init__(self, cache, ledger, recent=60):
        self.cache, self.ledger, self.recent = cache, ledger, recent
        self.lock = threading.Lock()
        self.failures = {}
        self.reset()

    def reset(self):
        self.tier = 0
        self.after = ''
        self.offered = {}
        self.done_at = None

    def failed(self, key, tier, all_tiers=False, now=None):
        now = time.monotonic() if now is None else now
        pair = (key, None if all_tiers else tier)
        with self.lock:
            count, deadline = self.failures.pop(pair, (0, 0))
            if deadline > now:
                self.failures[pair] = (count, deadline)
                return
            count = min(count + 1, 8)
            base = 1800 if all_tiers else self.recent
            self.failures[pair] = (count, now + min(21600, base * 2 ** (count - 1)))
            if len(self.failures) > 20000:
                self.failures.pop(next(iter(self.failures)))

    def eligible(self, key, tier, now):
        return ((key, tier) not in self.offered
                and self.failures.get((key, tier), (0, 0))[1] <= now
                and self.failures.get((key, None), (0, 0))[1] <= now
                and self.wanted(key, tier))

    def wanted(self, key, tier):
        if not key or key == '-':
            return False
        have = self.ledger.get(key, tier)
        if have is None:
            return True
        mine = self.cache.art_crc(key, tier)   # changed render -> push the new one
        return mine is not None and mine != have

    def next(self, n, priority=(), now=None, keys=None):
        """Up to n (key, tier) pairs the player lacks or holds stale."""
        now = time.monotonic() if now is None else now
        out = []
        with self.lock:
            self.offered = {k: t for k, t in self.offered.items() if now - t < self.recent}
            if self.tier >= len(FILL_ORDER):
                # A finished pass rescans after a pause: offers the player skipped come back.
                if self.done_at is None:self.done_at = now
                if now - self.done_at >= self.recent:self.tier, self.after, self.done_at = 0, '', None
            for key in priority:
                if len(out) < n and self.eligible(key, 'L', now):
                    out.append((key, 'L'))
            while len(out) < n and self.tier < len(FILL_ORDER):
                tier = FILL_ORDER[self.tier]
                if keys is None:
                    page = self.cache.library_arts(self.after, 64)
                else:
                    start = bisect_right(keys, self.after)
                    page = keys[start:start + 64]
                if not page:
                    self.tier += 1
                    self.after = ''
                    continue
                for key in page:
                    self.after = key
                    if self.eligible(key, tier, now):
                        out.append((key, tier))
                        if len(out) == n:
                            break
            for pair in out:
                self.offered[pair] = now
        return out


    def burst(self, keys, anchors, budget, count=BURST_COVERS):
        """Select missing previews first, with catalogue neighbours first per tier."""
        now = time.monotonic()
        positions = {key: i for i, key in enumerate(keys)}
        pivot = next((positions[key] for key in anchors if key in positions), 0)
        ordered = sorted(keys, key=lambda key: abs(positions[key] - pivot))
        with self.lock:
            self.offered = {pair: t for pair, t in self.offered.items() if now - t < self.recent}
            out = []
            for tier in FILL_ORDER:
                for key in ordered:
                    if budget >= FILL_BYTES[tier] and self.eligible(key, tier, now):
                        budget -= FILL_BYTES[tier]
                        pair = (key, tier)
                        self.offered[pair] = now
                        out.append(pair)
                        if len(out) == count:
                            return out
            return out

    def release(self, pairs):
        with self.lock:
            for pair in pairs:
                self.offered.pop(pair, None)

    def progress(self, initial=False, capacity=1 << 30, with_generation=False, keys=None):
        """Completion requires device ACKs with the rendered CRC for every tier.
        Offers, transport-ready and index enumeration are never completion evidence.
        """
        with self.cache.lock:
            generation = self.cache.generation
            missing_art = 0
            if keys is None:
                keys = self.cache.library_arts(limit=1000000)
                missing_art = sum(a['art']=='-' for a in self.cache.album_view().rows)
            rendered = {(k,t):c for k,t,c in self.cache.db.execute('SELECT key,tier,crc FROM art_crc')}
            held = {(bytes(k).hex(),t):c for k,t,c in self.cache.db.execute('SELECT k8,tier,crc FROM ipod_art')}
        total = len(keys) * len(FILL_ORDER)
        done = sum(rendered.get((key,tier)) is not None and
                   held.get((key[:16],TIERS[tier])) == rendered[(key,tier)]
                   for key in keys for tier in FILL_ORDER)
        desired = {(key[:16], TIERS[tier]) for key in keys for tier in FILL_ORDER}
        extra = [tier for prefix, tier in held if (prefix, tier) not in desired]
        record_bytes = {0:115712, 1:33280, 2:6656, 3:2560}
        required = len(keys) * (33280 + 6656 + 2560) + sum(record_bytes[t] for t in extra) + 115712
        if initial or not self.ledger.synced:state = 0
        elif total + len(extra) > 49152 or required > capacity or missing_art:state = 3
        else:state = 2 if done == total else 1
        result = (done, total, state)
        return (*result, generation) if with_generation else result


def offer_records(generation, pairs):
    """CO records, one tier each, at most five keys."""
    by_tier = {}
    for key, tier in pairs:
        by_tier.setdefault(tier, []).append(key)
    return [f'CO\t{generation}\t{tier}\t{",".join(keys[i:i + 5])}'.encode()
            for tier, keys in by_tier.items() for i in range(0, len(keys), 5)]
