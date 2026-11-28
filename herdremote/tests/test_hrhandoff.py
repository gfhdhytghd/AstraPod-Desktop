"""FU75 handoff over a synthetic clock and audio backends."""
import pytest

import hrhandoff as handoff


def test_clock_uses_minimum_network_roundtrip():
    slow = handoff.ClockSample(1000, 220, 222, 1120, 300)
    fast = handoff.ClockSample(1200, 224, 225, 1220, 350)
    result = handoff.estimate_clock([slow, fast])
    assert result.sample == fast
    assert result.rtt == 10
    assert result.offset == 1035
    assert result.position(1300) == 435


def test_matching_requires_all_fields_and_rejects_ambiguity():
    wanted = ('Song', 'Artist', 'Album', 10000)
    match = ('ＳＯＮＧ', ' artist ', 'Album', 12000, 'a')
    near = ('Song', 'Artist', 'Other Album', 10000, 'b')
    long = ('Song', 'Artist', 'Album', 12001, 'c')
    assert handoff.match_track(wanted, [near, long, match]) == 'a'
    assert handoff.match_track(wanted, [match, (*match[:4], 'd')]) is None
    assert handoff.match_track(('Song', '', 'Album', 10000), [match]) is None


class Player:
    name = 'Rufin'

    def __init__(self, proposal, lag=180):
        self.proposal = proposal
        self.lag = lag
        self.volume = .72
        self.position = 0
        self.anchor = 0
        self.playing = False
        self.commands = []

    def handoff_match(self, track):
        return ('track',)

    def handoff_state(self):
        p = self.proposal
        p.now += 4
        return dict(ids={'track'}, playing=self.playing, loading=False, volume=self.volume,
                    position=max(0, self.position + p.now - self.anchor))

    def handoff_volume(self, value):
        self.volume = value
        self.commands.append(('volume', value))

    def handoff_play(self, item):
        assert self.volume == 0
        self.proposal.now += 180
        self.anchor = self.proposal.now
        self.playing = True
        self.commands.append(('play', item))

    def handoff_seek(self, position):
        self.proposal.now += self.lag
        self.anchor = self.proposal.now
        self.position = position
        self.commands.append(('seek', position))

    def handoff_pause(self):
        self.commands.append(('pause',))
        self.playing = False


class SimulatedProposal(handoff.Proposal):
    def __init__(self, target, interruption=None):
        self.now = 100000
        self.player = Player(self)
        self.player.name = target
        self.events = []
        self.interruption = interruption
        super().__init__(1, ('Song', 'Artist', 'Album', 100000),
                         lambda: self.player, lambda player: None, lambda: self.now)

    def send(self, event, *args):
        self.events.append((event, *args))

    def sleep_until(self, when):
        self.now = max(when, self.now)
        if self.interruption == 'fade' and self.events[-1][0] == 'at':
            self.cancelled.set()
            raise handoff.HandoffError('cancelled')

    def wait(self, event, deadline):
        self.check(deadline)
        if event == 'p':
            _, seq = next(row for row in reversed(self.events) if row[0] == 'ping')
            sent = self.now
            self.now += 20
            device = (sent + 1000 + 10) // 10
            return self.now, (sent, seq, device, device, sent - 95000 + 10)
        if (event, self.interruption) in [('g', 'ack'), ('v', 'alignment-ack')]:
            self.now = deadline
            self.check(deadline)
        return self.now, ()


class PublishingRufin(Player):
    handoff_position_period_ms = 500

    def handoff_seek(self, position):
        self.position_before_seek = self.handoff_state()['position']
        requested_at = self.proposal.now
        echo_delay = 65 if hasattr(self, 'echo_at') else 300
        self.echo_at = requested_at + echo_delay
        self.anchor = requested_at + self.lag
        self.position = position
        self.commands.append(('seek', position))

    def handoff_state(self):
        state = super().handoff_state()
        if hasattr(self, 'echo_at'):
            now = self.proposal.now
            if now < self.echo_at:
                state['position'] = self.position_before_seek
            else:
                published = now // 500 * 500
                state['position'] = self.position + max(0, published - self.anchor)
        return state


class SlowPodLinkProposal(SimulatedProposal):
    def wait(self, event, deadline):
        if event == 'p':
            self.check(deadline)
            _, seq = next(row for row in reversed(self.events) if row[0] == 'ping')
            sent = self.now
            delay = (120, 103, 118, 110, 117)[seq]
            self.now += delay
            received = (sent + 1000 + delay // 2) // 10
            return self.now, (sent, seq, received, received, received * 10 - 96000)
        return super().wait(event, deadline)


def test_rufin_alignment_ignores_target_echo(caplog):
    caplog.set_level('INFO', logger='herdremote.handoff')
    proposal = SlowPodLinkProposal('Rufin')
    proposal.player = PublishingRufin(proposal, lag=300)
    proposal.run()
    assert proposal.events[-1] == ('end',), caplog.text
    assert proposal.events[6][3] == 103
    assert abs(proposal.events[6][4]) <= 50
    assert proposal.player.volume == .72


@pytest.mark.parametrize(('lags', 'expected'), [
    ((300, 500, 300), 'coarse'), ((300, 700, 300), 'degraded'),
    ((300, 500, 1800), 'alignment'),
])
def test_rufin_alignment_thresholds(caplog, lags, expected):
    caplog.set_level('INFO', logger='herdremote.handoff')
    proposal = SlowPodLinkProposal('Rufin')
    player = proposal.player = PublishingRufin(proposal)
    seek = player.handoff_seek
    delays = iter(lags)

    def variable_seek(position):
        player.lag = next(delays)
        seek(position)

    player.handoff_seek = variable_seek
    proposal.run()
    if expected == 'alignment':
        assert proposal.events[-1] == ('fail', 'alignment'), caplog.text
    else:
        assert proposal.events[-1] == ('end',), caplog.text
        assert f'aligned={expected}' in caplog.text
    assert len([name for name, *_ in player.commands if name == 'seek']) == 3
    assert player.volume == .72


@pytest.mark.parametrize('target', ['Rufin', 'Cider'])
def test_muted_prepare_corrects_latency_and_fades_to_prior_volume(target):
    proposal = SimulatedProposal(target)
    proposal.run()
    names = [event[0] for event in proposal.events]
    assert names == ['ping', 'offer', *(['ping'] * 4), 'go', 'at', 'end']
    assert proposal.events[1] == ('offer', target, 1)
    _, scheduled, offset, rtt, error, startup, uncertainty = proposal.events[6]
    assert scheduled * 10 - offset < 105000
    assert rtt == 20 and abs(error) <= 50
    assert startup >= 180 and uncertainty < 50
    commands = proposal.player.commands
    assert [name for name, *_ in commands[:4]] == ['volume', 'play', 'seek', 'seek']
    steps = [v for name, v in commands if name == 'volume']
    assert steps == sorted(steps)
    assert steps[0] == 0 and steps[-1] == .72 and len(steps) >= 55


@pytest.mark.parametrize('reason', ['fade', 'ack', 'alignment-ack'])
def test_abort_pauses_prepared_track_and_restores_exact_volume(reason):
    proposal = SimulatedProposal('Rufin', reason)
    proposal.run()
    assert proposal.player.commands[-2:] == [('pause',), ('volume', .72)]
    assert not any(event[0] == 'end' for event in proposal.events)


@pytest.mark.parametrize(('completion', 'keeps_playing'), [
    ('ack', True),
    ('missing-ack', True),
    ('disconnect-after-end', True),
    ('cancel', False),
    ('write-failed', False),
    ('end-not-written', False),
])
def test_rufin_completion_preserves_playback_after_lost_ack(
        monkeypatch, caplog, completion, keeps_playing):
    caplog.set_level('INFO', logger='herdremote.handoff')
    proposal = SimulatedProposal('Rufin')
    player = proposal.player
    player.current = 'old'
    controller = handoff.Controller(proposal.select, proposal.pin)
    controller.proposal = proposal
    monkeypatch.setattr(handoff, 'millis', proposal.clock)
    play, send, wait = player.handoff_play, proposal.send, proposal.wait

    def play_new(matched):
        play(matched)
        player.current = matched[0]

    def restore(before):
        player.current = 'old'
        player.commands.append(('restore',))
        player.handoff_pause()

    def send_end(event, *args):
        send(event, *args)
        if event == 'end':
            handoff.Proposal.send(proposal, event)

    def write_end(payload):
        assert payload == b'HF\t1\t1\tend'
        assert player.current == 'track' and player.playing
        volumes = [cmd[1] for cmd in player.commands if cmd[0] == 'volume']
        assert volumes == sorted(volumes)
        assert volumes[0] == 0 and volumes[-1] == .72 and len(volumes) > 1
        if completion == 'write-failed':
            raise TimeoutError('synthetic PodLink write timeout')

    def wait_for_device(event, deadline):
        if event != 'd':
            return wait(event, deadline)
        if completion == 'write-failed':
            with pytest.raises(TimeoutError):
                controller.poll(write_end)
            controller.disconnect()
        elif completion != 'end-not-written':
            controller.poll(write_end)
        if completion == 'ack':
            controller.command(b'hf\t1\td\t1')
        elif completion == 'cancel':
            controller.command(b'hf\t1\tx\t1')
        elif completion == 'disconnect-after-end':
            controller.disconnect()
        elif completion in ('missing-ack', 'end-not-written'):
            proposal.now = deadline
        return handoff.Proposal.wait(proposal, event, deadline)

    player.handoff_play = play_new
    player.handoff_restore = restore
    proposal.send = send_end
    proposal.wait = wait_for_device
    proposal.run()

    assert player.volume == .72
    if keeps_playing:
        assert proposal.events[-1] == ('end',), caplog.text
        assert player.current == 'track' and player.playing
        assert ('pause',) not in player.commands and ('restore',) not in player.commands
        outcome = 'accepted' if completion == 'ack' else 'unconfirmed'
        assert f'outcome={outcome}' in caplog.text
    else:
        assert player.current == 'old' and not player.playing
        assert player.commands[-3:] == [('restore',), ('pause',), ('volume', .72)]
        if completion == 'end-not-written':
            sent = []
            controller.poll(sent.append)
            assert sent == []


def test_no_match_does_not_touch_player():
    proposal = SimulatedProposal('Cider')
    proposal.player.handoff_match = lambda track: None
    proposal.run()
    assert proposal.events == [('ping', 0), ('offer', 'Cider', 0)]
    assert not proposal.player.commands



def test_presync_expires_old_low_rtt_and_refreshes_without_playing():
    proposal = SimulatedProposal('Rufin')
    first = proposal.request_ping(0)
    wait = proposal.wait
    attempts = 0

    def late_accept(event, deadline):
        nonlocal attempts
        assert not proposal.player.commands
        if event == 'a':
            attempts += 1
            if attempts < 5:
                proposal.now = deadline
                proposal.check(deadline)
        elif attempts:
            proposal.now += 110
        return wait(event, deadline)

    proposal.wait = late_accept
    clock, deadline = proposal.presync(first)
    assert deadline == proposal.accepted_at + handoff.START_MS
    assert clock.sample.t0 > first + handoff.SYNC_FRESH_MS
    assert len(proposal.events) == 9


def test_select_during_lost_legacy_probe_restarts_sync_before_mutation():
    proposal = SimulatedProposal('Cider')
    first = proposal.request_ping(0)
    wait = proposal.wait

    def late_reply(event, deadline):
        assert not proposal.player.commands
        if proposal.accepted_at is None:
            proposal.accepted_at = first + 40
            proposal.now = deadline
            proposal.check(deadline)
        return wait(event, deadline)

    proposal.wait = late_reply
    clock, deadline = proposal.presync(first)
    assert deadline == first + 40 + handoff.START_MS
    assert clock.rtt == 20
    assert proposal.events == [('ping', 0), *(('ping', i) for i in range(5))]

def test_cider_library_and_catalog_search(monkeypatch):
    from hrcider import Client

    player = Client()
    paths = []

    def request(path, body=None, **kwargs):
        api_path = body['path']
        paths.append(api_path)
        if api_path.startswith('/v1/me/'):
            return {'data': {'results': {'library-songs': {'data': []}}}}
        return {'results': {'songs': {'data': [
            {'id': '12', 'attributes': {'name': 'Song', 'artistName': 'Artist',
                                         'albumName': 'Album', 'durationInMillis': 10000}}
        ]}}}

    monkeypatch.setattr(player, 'request', request)
    assert player.handoff_match(('Song', 'Artist', 'Album', 10000)) == (
        '12', '/v1/catalog/$STOREFRONT/songs/12')
    assert [p.split('?')[0] for p in paths] == ['/v1/me/library/search',
                                                '/v1/catalog/$STOREFRONT/search']


def test_rufin_search_and_transient_controls(monkeypatch):
    from hrrufin import RufinClient

    player = RufinClient(env={})
    requests = []

    def get(path, **params):
        requests.append((path, params))
        if path == '/api/sources':
            return {'selected': 'jellyfin', 'library': 'music'}
        return {'total': 1, 'tracks': [
            dict(uri='jellyfin:track', title='Song', artist='Artist', album='Album',
                 duration_ms=10000)]}

    monkeypatch.setattr(player, 'get', get)
    monkeypatch.setattr(player, 'post', lambda path, body: requests.append((path, body)))
    matched = player.handoff_match(('Song', 'Artist', 'Album', 10000))
    assert matched == ('jellyfin:track',)
    assert requests[1][1]['source'] == 'jellyfin'
    assert requests[1][1]['folder'] == 'music'
    player.handoff_volume(0)
    player.handoff_seek(1234)
    assert requests[-2:] == [('/api/playback/volume', {'volume': 0, 'persist': False}),
                             ('/api/playback/seek', {'position_ms': 1234})]


@pytest.mark.parametrize('status', ['playing', 'paused', 'stopped', 'empty'])
def test_rufin_abort_restores_prior_queue_and_position(monkeypatch, status):
    from hrrufin import RufinClient

    proposal = SlowPodLinkProposal('Rufin')
    player = proposal.player = RufinClient(env={})
    queue = ['before', 'old', 'after'] if status != 'empty' else []
    previous = tuple(queue)
    index, position, volume = (1, 23456, .83) if queue else (None, 0, .83)
    mode = 'stopped' if status == 'empty' else status

    def playback():
        current = dict(position=index, media_uri=queue[index]) if index is not None else None
        return dict(state=mode, position_ms=position, volume=volume, current=current,
                    queue_total=len(queue), can_seek=bool(queue))

    def get(path, **params):
        assert path == '/api/queue'
        return dict(total=len(queue), current_index=index, loading=False,
                    window=[dict(position=i, track=dict(uri=uri)) for i, uri in enumerate(queue)])

    def post(path, body=None):
        nonlocal index, mode, position, volume
        if path == '/api/playback/volume':
            assert body['persist'] is False
            volume = body['volume']
        else:
            assert volume == 0
            if path == '/api/queue':
                assert body == dict(uris=['new'], mode='next')
                queue.insert(index + 1 if index is not None else len(queue), 'new')
            elif path == '/api/queue/activate':
                index, mode, position = body['position'], 'playing', 0
            elif path == '/api/playback/seek':
                if queue[index] == 'new':
                    raise OSError('synthetic failed seek')
                position = body['position_ms']
            elif path == '/api/playback/pause':
                mode = 'paused'
            elif path == '/api/playback/stop':
                mode, position = 'stopped', 0
            elif path == '/api/queue/item':
                assert queue.pop(body['position']) == 'new'
                if not queue:
                    index = None
            else:
                raise AssertionError(path)

    monkeypatch.setattr(player, '_playback_raw', playback)
    monkeypatch.setattr(player, 'handoff_match', lambda track: ('new',))
    monkeypatch.setattr(player, 'get', get)
    monkeypatch.setattr(player, 'post', post)
    proposal.run()
    assert proposal.events[-1] == ('fail', 'host')
    assert tuple(queue) == previous
    assert (index, position, volume, mode) == (
        (1, 23456, .83, status) if previous else (None, 0, .83, 'stopped'))


def test_rufin_preference_and_cider_fallback():
    from types import SimpleNamespace
    from hrcider import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge.rufin = SimpleNamespace(ensure=lambda: True, handoff_state=lambda: {})
    bridge.cider_client = SimpleNamespace(handoff_state=lambda: {})
    assert bridge._handoff_player() is bridge.rufin

    def offline():
        raise OSError('Rufin offline')

    bridge.rufin.handoff_state = offline
    assert bridge._handoff_player() is bridge.cider_client
