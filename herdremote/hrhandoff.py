"""Version 1 continuity handoff. Only the daemon thread writes PodLink."""
from dataclasses import dataclass
import logging
import math
import queue
import threading
import time
import unicodedata

CAPABILITY = b'HF\t1'
TICK_MS = 10
FADE_MS = 3000
START_MS = 5000
ALIGN_MS = 50
log = logging.getLogger('herdremote.handoff')


def millis():
    return time.monotonic_ns() // 1_000_000


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', text or '').casefold().split()) if isinstance(text, str) else ''


def match_track(wanted, candidates):
    title, artist, album = map(normalize, wanted[:3])
    if not (title and artist and album):
        return None
    matches = {row[4] for row in candidates
               if row[4] and isinstance(row[3], (int, float))
               and row[3] > 0 and abs(row[3] - wanted[3]) <= 2000
               and tuple(map(normalize, row[:3])) == (title, artist, album)}
    return matches.pop() if len(matches) == 1 else None


@dataclass(frozen=True)
class ClockSample:
    t0: int
    received: int
    sent: int
    t3: int
    elapsed: int


@dataclass(frozen=True)
class Clock:
    sample: ClockSample
    offset: int
    rtt: int

    def position(self, host_ms):
        return self.sample.elapsed + host_ms + self.offset - self.sample.sent * TICK_MS

    def device_tick(self, host_ms):
        return (host_ms + self.offset + TICK_MS - 1) // TICK_MS


def estimate_clock(samples):
    samples = [(s.t3 - s.t0 - (s.sent - s.received) * TICK_MS, s)
               for s in samples if s.sent >= s.received and s.t3 >= s.t0]
    if not samples:
        raise HandoffError('clock')
    rtt, sample = min(samples, key=lambda v: max(0, v[0]))
    offset = ((sample.sent + sample.received) * TICK_MS - sample.t0 - sample.t3) // 2
    return Clock(sample, offset, max(rtt, 0))


class HandoffError(Exception):
    pass


class Proposal:
    def __init__(self, ident, track, select, pin, clock=millis):
        self.ident, self.track, self.select, self.pin = ident, track, select, pin
        self.clock = clock
        self.inbox = queue.Queue(16)
        self.outbox = queue.Queue(16)
        self.cancelled = threading.Event()
        self.running = False
        self.finished = False

    def send(self, command, *args):
        self.outbox.put_nowait('\t'.join(map(str, ('HF', 1, self.ident, command, *args))).encode())

    def check(self, deadline):
        if self.cancelled.is_set():
            raise HandoffError('cancelled')
        if self.clock() >= deadline:
            raise HandoffError('timeout')

    def wait(self, command, deadline):
        while True:
            self.check(deadline)
            try:
                stamp, event, *args = self.inbox.get(timeout=.025)
                if event == command:
                    return stamp, args
            except queue.Empty:
                pass

    def sleep_until(self, when):
        delay = (when - self.clock()) / 1000
        if delay > 0:
            self.cancelled.wait(delay)
        if self.cancelled.is_set():
            raise HandoffError('cancelled')

    def sync(self, deadline):
        samples = []
        for seq in range(5):
            self.send('ping', seq)
            stamp, args = self.wait('p', deadline)
            sent, sequence, received, transmitted, elapsed = map(int, args)
            if seq != sequence or not 0 <= elapsed < self.track[3]:
                raise HandoffError('clock')
            samples.append(ClockSample(sent, received, transmitted, stamp, elapsed))
        return estimate_clock(samples)

    def read_player(self, player):
        before = self.clock()
        state = player.handoff_state()
        after = self.clock()
        return state, (before + after) // 2, after - before

    @staticmethod
    def ready(state, matched):
        return matched[0] in state['ids'] and state['playing'] and not state['loading']

    def measure(self, player, matched, deadline, seek=None):
        previous = None
        while True:
            self.check(deadline)
            state, stamp, rtt = self.read_player(player)
            if self.ready(state, matched):
                position = state['position']
                if seek is None:
                    return position, stamp, rtt
                # Rufin v0.17.1 publishes position every 500 ms. Use the
                # change edge, not the time of a read of an unchanged value.
                if previous and position > previous[0] and position >= seek - ALIGN_MS:
                    position_stamp = (previous[1] + stamp) // 2
                    return position, position_stamp, rtt + stamp - previous[1]
                previous = (position, stamp)
            else:
                previous = None
            self.sleep_until(self.clock() + 25)

    def schedule(self, player, matched, clock, deadline):
        launch = self.clock()
        self.play_issued = True
        player.handoff_play(matched)
        self.measure(player, matched, deadline)
        startup = self.clock() - launch

        desired = max(0, clock.position(self.clock()))
        player.handoff_seek(desired)
        position, stamp, rtt = self.measure(player, matched, deadline, desired)
        error = position - clock.position(stamp)
        if abs(error) > ALIGN_MS:
            # Use one correction for a late or early seek, while still muted.
            desired = max(0, clock.position(self.clock()) - error)
            player.handoff_seek(desired)
            position, stamp, rtt = self.measure(player, matched, deadline, desired)
            error = position - clock.position(stamp)
        self.check(deadline)
        if abs(error) > ALIGN_MS:
            raise HandoffError('alignment')

        tick = clock.device_tick(self.clock() + max(300, clock.rtt * 3))
        start = tick * TICK_MS - clock.offset
        if start >= deadline or clock.position(start + FADE_MS) >= self.track[3]:
            raise HandoffError('end-of-track')
        self.send('go', tick & 0xffffffff, clock.offset % (1 << 32),
                  clock.rtt, error, startup, (clock.rtt + rtt + TICK_MS) // 2)
        self.wait('g', start)
        return start, position + start - stamp

    def fade(self, player, matched, volume, start, position_at_start):
        self.sleep_until(start)
        state = player.handoff_state()
        if not self.ready(state, matched):
            raise HandoffError('player-changed')
        # Project the last measured host sample to the agreed PCM boundary.
        # The iPod compares that with its local clock sampled on that boundary.
        self.send('at', position_at_start)
        self.wait('v', start + 250)
        end = start + FADE_MS
        for step in range(1, 61):
            self.sleep_until(start + step * 50)
            now = self.clock()
            self.check(end + 500)
            player.handoff_volume(volume * min(1, (now - start) / FADE_MS))
            if now >= end:
                break
        if not self.ready(player.handoff_state(), matched):
            raise HandoffError('player-changed')
        self.send('end')
        self.wait('d', end + 2300)

    def run(self):
        player, volume = None, None
        mutated, committed = False, False
        self.play_issued = False
        try:
            player = self.select()
            matched = player.handoff_match(self.track) if player else None
            self.send('offer', player.name if player else 'Cider', int(bool(matched)))
            if not matched:
                return
            self.wait('a', float('inf'))
            deadline = self.clock() + START_MS
            self.running = True
            self.pin(player)
            clock = self.sync(deadline)
            before = player.handoff_state()
            volume = before['volume']
            if not math.isfinite(volume) or not 0 <= volume <= 1:
                raise HandoffError('volume')
            self.check(deadline)
            mutated = True
            player.handoff_volume(0)
            start, position_at_start = self.schedule(player, matched, clock, deadline)
            self.fade(player, matched, volume, start, position_at_start)
            committed = True
        except (OSError, ValueError, TypeError, KeyError, OverflowError,
                HandoffError, queue.Full) as error:
            reason = str(error) if isinstance(error, HandoffError) else 'host'
            log.info('handoff id=%d outcome=%s', self.ident, reason)
            if not self.cancelled.is_set():
                try:
                    self.send('fail', reason)
                except queue.Full:
                    pass
        finally:
            if mutated and not committed:
                # Pause before restoring the user's previous setting. Restore
                # even after an ambiguous volume RPC timeout.
                try:
                    if self.play_issued:
                        player.handoff_pause()
                except (OSError, ValueError):
                    log.warning('handoff player pause failed')
                try:
                    player.handoff_volume(volume)
                except (OSError, ValueError):
                    log.warning('handoff player volume restore failed')
            self.running = False
            self.finished = True


class Controller:
    def __init__(self, select, pin):
        self.select, self.pin = select, pin
        self.proposal = None
        self.retiring = None

    def disconnect(self):
        if self.proposal is not None:
            self.proposal.cancelled.set()
            self.retiring = self.proposal
            self.proposal = None

    def close(self):
        self.disconnect()
        if self.retiring is not None:
            self.retiring.thread.join(timeout=5)

    def command(self, payload):
        if not payload.startswith(b'hf\t'):
            return False
        try:
            version, event, ident, *args = payload.decode().split('\t')[1:]
            ident = int(ident)
            if version != '1' or not 0 < ident < 2**32:
                return True
            if event == 'q' and len(args) == 4:
                duration = int(args[0])
                if not 0 < duration < 2**31 or any(len(v) > 96 for v in args[1:]):
                    return True
                old = self.proposal
                if old and not old.finished and old.ident == ident:
                    return True
                # Serialize even an in-flight rollback from the last cable.
                old = old if old is not None else self.retiring
                self.disconnect()
                self.proposal = Proposal(ident, (*args[1:], duration), self.select, self.pin)

                def run(proposal):
                    if old is not None and old.thread.is_alive():
                        old.thread.join()
                    if not proposal.cancelled.is_set():
                        proposal.run()
                    else:
                        proposal.finished = True

                proposal = self.proposal
                proposal.thread = threading.Thread(target=run, args=(proposal,), daemon=True)
                proposal.thread.start()
            elif self.proposal and self.proposal.ident == ident:
                if event == 'x':
                    self.proposal.cancelled.set()
                elif event == 'p' and len(args) == 4:
                    self.proposal.inbox.put_nowait((millis(), event, *map(int, args)))
                elif event in ('a', 'g', 'v', 'd') and not args:
                    self.proposal.inbox.put_nowait((millis(), event))
        except (ValueError, queue.Full):
            pass
        return True

    def poll(self, send):
        proposal = self.proposal
        if proposal is None or proposal.cancelled.is_set():
            return
        while True:
            try:
                payload = proposal.outbox.get_nowait()
            except queue.Empty:
                return
            if b'\tping\t' in payload:
                payload += b'\t' + str(millis()).encode()
            send(payload)
