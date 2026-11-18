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
            _, seq = self.events[-1]
            sent = self.now
            self.now += 20
            device = (sent + 1000 + 10) // 10
            return self.now, (sent, seq, device, device, sent - 95000 + 10)
        if (event, self.interruption) in [('g', 'ack'), ('v', 'alignment-ack')]:
            self.now = deadline
            self.check(deadline)
        return self.now, ()


@pytest.mark.parametrize('target', ['Rufin', 'Cider'])
def test_muted_prepare_corrects_latency_and_fades_to_prior_volume(target):
    proposal = SimulatedProposal(target)
    proposal.run()
    names = [event[0] for event in proposal.events]
    assert names == ['offer', *(['ping'] * 5), 'go', 'at', 'end']
    assert proposal.events[0] == ('offer', target, 1)
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


def test_no_match_does_not_touch_player():
    proposal = SimulatedProposal('Cider')
    proposal.player.handoff_match = lambda track: None
    proposal.run()
    assert proposal.events == [('offer', 'Cider', 0)]
    assert not proposal.player.commands


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
    player.handoff_play(matched)
    player.handoff_seek(1234)
    assert requests[-3:] == [('/api/playback/volume', {'volume': 0, 'persist': False}),
                             ('/api/queue', {'uris': ['jellyfin:track'], 'mode': 'replace'}),
                             ('/api/playback/seek', {'position_ms': 1234})]


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
