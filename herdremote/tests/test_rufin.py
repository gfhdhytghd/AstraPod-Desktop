"""Rufin backend (FU56), exercised only against loopback mock servers.

Nothing here talks to the user's Rufin or Cider: every URL is an ephemeral
127.0.0.1 port opened by the test, every token is a test constant.
"""
import io
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hrcider  # noqa: E402
import hrrufin  # noqa: E402

TOKEN = 'test-token-0123456789abcdef'
PAGE = '<!doctype html><html><head><meta name="rufin-csrf" content="" /><title>Rufin</title></head></html>'


def track(n, album=7):
    uri = f'local:track/{n}'
    return {'kind': 'track', 'uri': uri, 'object_id': str(n), 'title': f'Song {n}', 'artist': 'Artist',
            'album': 'Album', 'duration_ms': 180000 + n, 'track_number': n, 'year': 2001, 'favorite': False,
            'links': {'album': [{'name': 'Album', 'page': {'route': {'AlbumDetail': f'local:album/{album}'}}}]}}


class LocalOpener:
    """Loopback-only opener (test_usage replaces urllib's global opener)."""
    def open(self, request, timeout=1):
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        assert parsed.hostname == '127.0.0.1' and parsed.port and parsed.port > 1024, 'mock servers only'
        connection = http.client.HTTPConnection('127.0.0.1', parsed.port, timeout=timeout)
        connection.request(request.get_method(), parsed.path + ('?' + parsed.query if parsed.query else ''),
                           request.data, dict(request.header_items()))
        response = connection.getresponse()
        if response.status >= 400:
            headers = response.headers
            response.read()
            response.close()
            connection.close()
            raise urllib.error.HTTPError(url, response.status, 'mock failure', headers, None)
        return response


def make_client(env, **kw):
    return hrrufin.RufinClient(env, opener=LocalOpener(), **kw)


class Rufin(BaseHTTPRequestHandler):
    """A small Rufin web controller, shaped after Rufin 0.17.1 crates/web."""
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_any()

    def do_POST(self):
        self.handle_any()

    def reply(self, code, value=None, raw=None, ctype='application/json', headers=()):
        data = raw if raw is not None else json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self):
        s = self.server
        body = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
        auth = self.headers.get('Authorization')
        s.calls.append((self.command, self.path, json.loads(body) if body else None, auth))
        path = urllib.parse.urlsplit(self.path).path
        qs = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        if path == '/' and self.command == 'GET':
            return self.reply(200, raw=PAGE.encode(), ctype='text/html')
        if s.down:
            return self.reply(503, {'error': 'down'})
        if auth != 'Bearer ' + s.token:
            s.failures += 1
            if s.failures > 10:
                return self.reply(429, {'error': 'Too many'}, headers=[('Retry-After', '30')])
            return self.reply(401, {'error': 'A valid Bearer token is required'})
        if self.command == 'POST':
            s.posts.append((path, json.loads(body) if body else None))
            if path == '/api/playback/play':
                s.state['state'] = 'playing'
            elif path == '/api/playback/pause':
                s.state['state'] = 'paused'
            elif path == '/api/playback/repeat':
                s.state['repeat'] = json.loads(body)['mode']
            elif path == '/api/playback/shuffle':
                s.state['shuffle'] = json.loads(body)['enabled']
            elif path.startswith('/api/queue'):
                data = json.loads(body)
                uri = data.get('anchor_uri') or (data.get('uris') or [None])[0] or 'local:track/1'
                s.state['current'] = {'position': 0, 'media_uri': uri, 'track': dict(track(int(uri.rsplit('/', 1)[1])))}
                s.state['state'] = 'playing'
            return self.reply(202, {'accepted': True})
        if path == '/api/playback':
            return self.reply(200, s.state)
        if path == '/api/sources':
            return self.reply(200, {'selected': 'local-1', 'library': None, 'libraries': [], 'sources': []})
        if path == '/api/queue':
            window = [{'position': i, 'track': track(i + 1)} for i in range(5)]
            return self.reply(200, {'total': 5, 'current_index': 2, 'loading': False, 'offset': 0, 'window': window})
        if path in ('/api/albums', '/api/albums/tracks', '/api/tracks', '/api/artists', '/api/playlists', '/api/page'):
            off, lim = int(qs.get('offset', 0)), int(qs.get('limit', 50))
            if path == '/api/albums':
                rows = [{'kind': 'album', 'id': i, 'uri': f'local:album/{i}', 'title': f'Album {i}', 'artist': 'Artist',
                         'year': 1999, 'track_count': 3, 'duration_ms': 600000} for i in range(1, 8)]
                key = 'albums'
            elif path == '/api/artists':
                rows = [{'kind': 'artist', 'id': i, 'name': f'Artist {i}', 'album_artists': qs.get('album_artists') == 'true',
                         'track_count': 4} for i in range(1, 4)]
                key = 'artists'
            elif path == '/api/playlists':
                rows = [{'kind': 'playlist', 'id': 'pl"1', 'name': 'Mix', 'track_count': 2}]
                key = 'playlists'
            elif path == '/api/page':
                rows = [{'kind': 'genre', 'id': 3, 'name': 'Jazz', 'track_count': 9}]
                key = 'tracks'
            else:
                rows = [track(i) for i in range(1, 4)]
                key = 'tracks'
            return self.reply(200, {key: rows[off:off + lim], 'total': len(rows), 'offset': off, 'limit': lim})
        if path == '/api/lyrics':
            if hasattr(s, 'lyrics'):
                if s.lyrics_hook:
                    s.lyrics_hook()
                return self.reply(200, s.lyrics)
            return self.reply(200, {'state': 'ready', 'document': {'lines': [{'text': 'la la'}, {'text': 'second'}]}})
        if path == '/api/artwork':
            from PIL import Image
            out = io.BytesIO()
            Image.new('RGB', (int(qs.get('size', 600)) + s.oversize, 200), (200, 40, 40)).save(out, 'PNG')
            return self.reply(200, raw=out.getvalue(), ctype='image/png')
        return self.reply(404, {'error': 'Route not found'})


class Other(BaseHTTPRequestHandler):
    """Something else on the settings port (KDE Connect holds 1717)."""
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.calls.append((self.command, self.path, self.headers.get('Authorization')))
        data = b'not rufin'
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_GET


def serve(handler, port=0):
    server = ThreadingHTTPServer(('127.0.0.1', port), handler)
    server.calls = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def rufin_server(port=0):
    server = serve(Rufin, port)
    server.token, server.failures, server.down, server.posts, server.oversize = TOKEN, 0, False, [], 0
    server.state = {'state': 'paused', 'position_ms': 61000, 'duration_ms': 180001, 'can_seek': True,
                    'current': {'position': 0, 'media_uri': 'local:track/1', 'track': track(1)},
                    'volume': 0.4, 'muted': False, 'shuffle': False, 'repeat': 'all', 'queue_total': 5}
    return server


class Env:
    """A private HOME with an optional token file and Rufin settings."""
    def __init__(self, token=TOKEN, port=None, mode=0o600):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.home / 'cfg')}
        if token is not None:
            self.write_token(token, mode)
        if port is not None:
            (self.home / 'cfg/rufin').mkdir(parents=True)
            (self.home / 'cfg/rufin/settings.json').write_text(json.dumps(
                {'web_controller': {'enabled': True, 'address': '0.0.0.0', 'port': port}}))

    def write_token(self, token, mode=0o600):
        path = self.home / 'cfg/podbox-herdremote/rufin-token'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + '\n')
        path.chmod(mode)

    def close(self):
        self.tmp.cleanup()


class TokenAndDiscovery(unittest.TestCase):
    def test_token_sources(self):
        e = Env()
        try:
            self.assertEqual(hrrufin.load_token(e.env), (TOKEN, None))
            self.assertEqual(hrrufin.load_token(dict(e.env, RUFIN_TOKEN='env-token')), ('env-token', None))
            e.write_token(TOKEN, 0o644)
            self.assertEqual(hrrufin.load_token(e.env)[0], None)
            self.assertIn('0600', hrrufin.load_token(e.env)[1])
        finally:
            e.close()
        e = Env(token=None)
        try:
            self.assertEqual(hrrufin.load_token(e.env), (None, 'Rufin token missing'))
        finally:
            e.close()

    def test_candidates_from_settings_and_env(self):
        e = Env(port=1718)
        try:
            self.assertEqual(hrrufin.candidates(e.env), ['http://127.0.0.1:1718', 'http://127.0.0.1:1719'])
            self.assertEqual(hrrufin.candidates(dict(e.env, RUFIN_API_URL='http://127.0.0.1:5/')), ['http://127.0.0.1:5'])
            with self.assertRaises(ValueError):
                hrrufin.candidates(dict(e.env, RUFIN_API_URL='http://example.com:1717'))
        finally:
            e.close()
        e = Env()
        try:
            self.assertEqual(hrrufin.candidates(e.env)[0], 'http://127.0.0.1:1717')
        finally:
            e.close()

    def test_discovery_skips_a_port_that_is_not_rufin(self):
        for _ in range(50):
            other = serve(Other)
            port = other.server_address[1]
            try:
                rufin = rufin_server(port + 1)
                break
            except OSError:
                other.shutdown()
                other.server_close()
        else:
            self.skipTest('no adjacent free ports')
        e = Env(port=port)
        try:
            client = make_client(e.env)
            self.assertTrue(client.ensure(0))
            self.assertEqual(client.base, f'http://127.0.0.1:{port + 1}')
            # The unverified port saw exactly one GET / and never a token.
            self.assertEqual(other.calls, [('GET', '/', None)])
            self.assertEqual(rufin.calls[0], ('GET', '/', None, None))
            self.assertEqual(rufin.calls[1][3], 'Bearer ' + TOKEN)
        finally:
            for s in (other, rufin):
                s.shutdown()
                s.server_close()
            e.close()

    def test_no_rufin_means_unavailable_with_backoff(self):
        other = serve(Other)
        e = Env(port=other.server_address[1])
        try:
            client = make_client(e.env, verify_timeout=.3)
            self.assertFalse(client.ensure(100))
            self.assertEqual(client.reason, 'Rufin not running')
            calls = len(other.calls)
            self.assertFalse(client.ensure(101))          # within the 5 s backoff: nothing sent
            self.assertEqual(len(other.calls), calls)
            self.assertTrue(all(c == ('GET', '/', None) for c in other.calls))
        finally:
            other.shutdown()
            other.server_close()
            e.close()


class Auth(unittest.TestCase):
    def setUp(self):
        self.server = rufin_server()
        self.e = Env()
        self.env = dict(self.e.env, RUFIN_API_URL=f'http://127.0.0.1:{self.server.server_address[1]}')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.e.close()

    def test_missing_token_never_contacts_rufin(self):
        self.e.close()
        self.e = Env(token=None)
        client = make_client(dict(self.e.env, RUFIN_API_URL=self.env['RUFIN_API_URL']))
        self.assertFalse(client.ensure(0))
        self.assertEqual(client.reason, 'Rufin token missing')
        self.assertEqual(self.server.calls, [])

    def test_rejected_token_backs_off_and_logs_no_secret(self):
        self.server.token = 'other'
        client = make_client(self.env)
        with self.assertLogs('herdremote.rufin', logging.INFO) as logs:
            self.assertFalse(client.ensure(0))
        self.assertEqual(client.reason, 'Rufin token rejected')
        self.assertFalse(any(TOKEN in line for line in logs.output))
        sent = len(self.server.calls)
        self.assertFalse(client.ensure(30))               # one attempt per minute (Rufin allows 10)
        self.assertEqual(len(self.server.calls), sent)

    def test_401_reloads_the_token_once_and_continues(self):
        client = make_client(self.env)
        self.assertTrue(client.ensure(0))
        self.server.token = 'rotated-token'
        self.e.write_token('rotated-token')
        self.assertEqual(client.snapshot()['title'], 'Song 1')   # re-login with the new token
        self.assertEqual(self.server.calls[-1][3], 'Bearer rotated-token')
        self.server.token = 'again'
        with self.assertRaises(hrrufin.RufinAuthError):
            client.snapshot()
        self.assertFalse(client.healthy())
        self.assertEqual(client.reason, 'Rufin token rejected')

    def test_429_is_a_backoff(self):
        self.server.token = 'other'
        self.server.failures = 10
        client = make_client(self.env)
        self.assertFalse(client.ensure(0))
        self.assertGreaterEqual(client.retry_at, 30)


class Mapping(unittest.TestCase):
    def setUp(self):
        self.server = rufin_server()
        self.e = Env()
        self.client = make_client(dict(self.e.env, RUFIN_API_URL=f'http://127.0.0.1:{self.server.server_address[1]}'))
        self.assertTrue(self.client.ensure(0))
        self.library = hrrufin.RufinLibrary(self.client)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.e.close()

    def lyrics_fixture(self, lines, offset=0):
        self.server.state['current']['run'] = 17
        self.server.lyrics = dict(state='ready', media_uri='local:track/1', run=17,
                                 document=dict(lines=lines, offset_millis=offset))
        self.server.lyrics_hook = None
        return 'now/lyrics?lrc=1&track=' + hrrufin.rufin_id('local:track/1')

    def test_timed_lyrics_offset_cjk_partial_and_legacy(self):
        text = '星 光'
        cues = [dict(byte_start=0, byte_end_exclusive=3, start_millis=1200, end_millis=1500),
                dict(byte_start=4, byte_end_exclusive=7, start_millis=1800)]
        lines = [dict(text=text, start_millis=1200, end_millis=2500,
                      cue_lines=[dict(text=text, cues=cues)]),
                 dict(text='Missing line start'),
                 dict(text='No complete words', start_millis=5200,
                      cue_lines=[dict(text='No complete words', cues=[
                          dict(byte_start=0, byte_end_exclusive=2)])])]
        q = self.lyrics_fixture(lines, 200)
        result = self.library.page(q+'&nonce=1', 0, 50)['items']
        self.assertEqual([r['title'] for r in result],
                         ['[00:01.00]<00:01.00>星<00:01.30> <00:01.60>光<00:02.30>',
                          '[00:03.00]Missing line start', '[00:05.00]No complete words'])
        self.server.lyrics['document']['offset_millis'] = -500
        self.assertIn('[00:01.70]<00:01.70>星', self.library.page(q+'&nonce=2', 0, 50)['items'][0]['title'])
        cues[0]['byte_end_exclusive'] = 2
        self.assertEqual(self.library.page(q+'&nonce=3', 0, 50)['items'][0]['title'], '[00:01.70]星 光')
        legacy = self.library.page('now/lyrics?nonce=4', 0, 50)['items']
        self.assertEqual([r['title'] for r in legacy], [line['text'] for line in lines])
        self.assertTrue(all(r['id'] != 'lrc1' for r in legacy))

    def test_timed_lyrics_size_and_frozen_fragments(self):
        text = '星' * 40 + ' ' + '光' * 20
        def line(n):
            start = n*1000
            cues = [dict(byte_start=i*3, byte_end_exclusive=(i+1)*3, start_millis=start+i*10)
                    for i in range(40)]
            cues.append(dict(byte_start=121, byte_end_exclusive=181, start_millis=start+400))
            return dict(text=text, start_millis=start, cue_lines=[dict(text=text, cues=cues)])
        q = self.lyrics_fixture([line(n) for n in range(40)])
        first = self.library.page(q+'&nonce=1', 0, 1)
        self.assertEqual(first['items'][0]['id'], 'lrc1+')
        self.assertNotIn('<', first['items'][0]['title'])  # Word stamps over budget, line stamps fit.
        self.server.lyrics['document']['lines'] = [dict(text='Changed', start_millis=10)]
        second = self.library.page(q+'&nonce=1', 1, 1)
        self.assertEqual(first['total'], second['total'])
        self.assertEqual(first['items'][0]['title'] + second['items'][0]['title'], '[00:00.00]'+text)
        self.assertEqual(self.library.page(q+'&nonce=2', 0, 1)['total'], 1)
        self.server.lyrics['document']['lines'] = [line(n) for n in range(300)]
        plain = self.library.page(q+'&nonce=3', 0, 50)
        self.assertTrue(all(r['id'].isdigit() for r in plain['items']))
        self.assertLess(plain['total'], 600)
        self.assertEqual(self.server.posts, [])

    def test_timed_lyrics_keeps_nonchronological_lrc_timed(self):
        # An LRC can list verse timestamps out of order.
        q = self.lyrics_fixture([dict(text='Refrain', start_millis=4100),
                                 dict(text='Verse', start_millis=1230),
                                 dict(text='Before refrain', start_millis=3007)], 200)
        result = self.library.page(q+'&nonce=1', 0, 50)['items']
        self.assertEqual([r['id'] for r in result], ['lrc1'] * 3)
        self.assertEqual([r['title'] for r in result],
                         ['[00:01.03]Verse', '[00:02.80]Before refrain', '[00:03.90]Refrain'])
        self.assertEqual(self.server.posts, [])

    def test_timed_lyrics_track_change_in_flight(self):
        q = self.lyrics_fixture([dict(text='Old track!', start_millis=10)])
        self.server.lyrics_hook = lambda: self.server.state['current'].update(
            media_uri='local:track/2', track=track(2), run=18)
        self.assertEqual(self.library.page(q+'&nonce=1', 0, 50)['items'][0]['id'], 'lyric-wait')
        self.server.lyrics_hook = None
        q = q.replace(hrrufin.rufin_id('local:track/1'), hrrufin.rufin_id('local:track/2'))
        self.assertEqual(self.library.page(q+'&nonce=2', 0, 50)['items'][0]['id'], 'lyric-wait')
        self.server.lyrics.update(media_uri='local:track/2', run=17)
        self.assertEqual(self.library.page(q+'&nonce=3', 0, 50)['items'][0]['id'], 'lyric-wait')
        self.server.lyrics.update(state='loading', run=18)
        self.assertEqual(self.library.page(q+'&nonce=4', 0, 50)['items'][0]['id'], 'lyric-wait')
        self.server.lyrics.update(state='ready', document={'lines': [dict(text='New track', start_millis=1500)]})
        self.assertEqual(self.library.page(q+'&nonce=5', 0, 50)['items'][0]['title'], '[00:01.50]New track')
        self.assertEqual(self.server.posts, [])

    def test_snapshot(self):
        s = self.client.snapshot()
        self.assertEqual(s['id'], hrrufin.rufin_id('local:track/1'))
        self.assertEqual((s['duration'], s['position'], s['rate']), (180001, 61000, 0))
        self.assertEqual((s['repeat'], s['shuffle'], s['repeat5']), (2, 0, 1))
        self.assertEqual(s['template'], 'rufin:local:album/7')
        self.assertEqual(s['art'], hrcider.art_id('rufin:local:album/7'))
        self.server.state['state'] = 'buffering'
        self.client.snapshot()
        self.assertTrue(self.client.loading_state())
        self.server.state['current'] = None
        self.assertEqual(self.client.snapshot()['id'], '')

    def test_controls(self):
        for command, value in (('next', 0), ('previous', 0), ('seek', 12345), ('resume', 0), ('pause', 0),
                               ('shuffle', 1), ('repeat', 1), ('queue-jump', 3)):
            self.client.control(command, value)
        self.assertEqual(self.server.posts, [
            ('/api/playback/next', {}), ('/api/playback/previous', {}), ('/api/playback/seek', {'position_ms': 12345}),
            ('/api/playback/play', {}), ('/api/playback/pause', {}), ('/api/playback/shuffle', {'enabled': True}),
            ('/api/playback/repeat', {'mode': 'one'}), ('/api/queue/activate', {'position': 3})])
        self.server.posts.clear()
        self.client.snapshot()
        self.client.control('repeat5', 3)                 # Rockbox Shuffle: repeat all + shuffle
        self.assertEqual(self.server.posts, [('/api/playback/repeat', {'mode': 'all'}),
                                             ('/api/playback/shuffle', {'enabled': True})])
        self.client.control('love', 0)
        self.assertEqual(self.server.posts[-1], ('/api/favorite', {'kind': 'track', 'uri': 'local:track/1', 'favorite': True}))
        for unsupported in ('add-library', 'rate', 'volume'):
            with self.assertRaises(ValueError):
                self.client.control(unsupported, 1000)

    def test_pages(self):
        root = self.library.page('root/Album', 0, 50)
        self.assertEqual(root['total'], 7)
        album = root['items'][0]
        self.assertEqual((album['kind'], album['route'], album['count']), ('album', 'album/1', 3))
        self.assertEqual(album['art'], hrcider.art_id('rufin:local:album/1'))
        self.assertEqual(self.library.template(album['art']), 'rufin:local:album/1')
        page = self.library.page('root/Album', 5, 50)
        self.assertEqual((page['offset'], len(page['items'])), (5, 2))
        tracks = self.library.page(album['route'], 0, 50)['items']
        self.assertEqual(tracks[1]['route'], 'play/library-songs/' + hrrufin.rufin_id('local:track/2'))
        self.assertEqual(tracks[1]['kind'], 'song')
        call = next(c for c in self.server.calls if c[1].startswith('/api/albums/tracks'))
        self.assertIn('id=1', call[1])
        playlists = self.library.page('playlists', 0, 50)['items']
        self.assertEqual(playlists[0]['route'], 'playlist/' + urllib.parse.quote('"pl\\"1"', safe=''))
        self.assertEqual(self.library.page('root/Genre', 0, 50)['items'][0]['route'], 'genre/3')
        self.assertEqual(self.library.page('root/Composer', 0, 50)['items'], [])        # unsupported: empty
        self.assertEqual(self.library.page('songs?rating=1', 0, 50)['items'], [])
        search = self.library.page('search?q=so', 0, 50)['items']
        self.assertEqual([r['kind'] for r in search][:4], ['song', 'song', 'song', 'album'])
        self.assertTrue(self.library.page('songs?format=duration', 0, 1)['items'][0]['title'].endswith(' - 03:00'))
        self.client.snapshot()
        info = {r['id']: r['title'] for r in self.library.page('now/info', 0, 50)['items']}
        self.assertEqual((info['title'], info['album'], info['year']), ('Song 1', 'Album', ''))
        self.assertEqual([r['title'] for r in self.library.page('now/lyrics', 0, 50)['items']], ['la la', 'second'])
        queue = self.library.page('now/queue', 0, 50)['items']
        self.assertEqual((len(queue), queue[3]['route'], queue[0]['count']), (5, 'queue/3', 2))
        album_row = self.library.page('now/album', 0, 50)['items'][0]
        self.assertTrue(album_row['route'].startswith('album/t'))
        self.library.page(album_row['route'], 0, 50)
        page_call = [c for c in self.server.calls if c[1].startswith('/api/page')][-1]
        self.assertIn(urllib.parse.quote('AlbumDetail'), page_call[1])

    def test_play_in_context(self):
        tracks = self.library.page('album/4', 0, 50)['items']
        self.library.set_context('1|album/4')
        result = self.library.play(tracks[1]['route'])
        self.assertEqual(result['expected_ids'], [hrrufin.rufin_id('local:track/2')])
        self.assertEqual(self.server.posts[-1], ('/api/queue/album', {'mode': 'replace', 'anchor_uri': 'local:track/2',
                                                                      'index': 1, 'id': '4'}))
        songs = self.library.page('songs', 0, 50)['items']
        self.library.set_context('2|songs')
        self.library.play(songs[2]['route'])
        self.assertEqual(self.server.posts[-1], ('/api/queue/source', {'mode': 'replace', 'anchor_uri': 'local:track/3',
                                                                       'index': 2, 'source': 'local-1'}))
        self.library.play(songs[0]['route'])            # no context: the song alone
        self.assertEqual(self.server.posts[-1], ('/api/queue', {'uris': ['local:track/1'], 'mode': 'replace'}))
        self.library.play('play/library-albums/5')
        self.assertEqual(self.server.posts[-1], ('/api/queue/album', {'id': '5', 'mode': 'replace'}))
        # After a restart the URI map is empty; the context row restores it.
        fresh = hrrufin.RufinLibrary(make_client(self.client.env))
        self.assertTrue(fresh.client.ensure(0))
        fresh.set_context('0|album/4')
        fresh.play('play/library-songs/' + hrrufin.rufin_id('local:track/1'))
        self.assertEqual(self.server.posts[-1][1]['anchor_uri'], 'local:track/1')
        with self.assertRaises(ValueError):
            fresh.play('play/library-songs/r' + '0' * 24)

    def test_same_song_resumes_without_queue_change(self):
        state = self.client.snapshot()
        self.assertTrue(self.client.same_song('play/library-songs/' + state['id']))
        self.assertEqual(self.server.posts, [('/api/playback/play', {})])
        self.assertFalse(self.client.same_song('play/library-songs/other'))


class CiderStub:
    """Cider as the bridge sees it; playback is never driven for real."""
    def __init__(self, rate=0):
        self.rate = rate
        self.requests = []

    def snapshot(self):
        return dict(id='cider-1', repeat5=0, title='Cider song', artist='A', album='B', composer='', duration=1000,
                    position=0, rate=self.rate, volume=0, repeat=0, shuffle=0, art='-', template='', bg='181818')

    def ab_tick(self, state):
        return None

    def request(self, path, body=None, **kw):
        self.requests.append(path)
        return [] if 'queue' in path else {}

    def control(self, *a):
        self.requests.append(a)


class Priority(unittest.TestCase):
    def setUp(self):
        self.server = rufin_server()
        self.e = Env()
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = hrcider.Cache(self.tmp.name)
        self.cider = CiderStub()
        volume = mock.Mock()
        volume.read.return_value = (20, False)
        self.rufin = make_client(dict(self.e.env, RUFIN_API_URL=f'http://127.0.0.1:{self.server.server_address[1]}'))
        self.bridge = hrcider.Bridge(client=self.cider, cache=self.cache, volume=volume, rufin=self.rufin)

    def tearDown(self):
        self.bridge.close()
        self.cache.close()
        self.tmp.cleanup()
        self.server.shutdown()
        self.server.server_close()
        self.e.close()

    def frames(self):
        out = []
        for _ in range(8):                 # poll() sends at most four frames per call
            self.bridge.poll(lambda *f: out.append(f))
        return [p for _, p in out]

    def open(self, caps='cn18,neighbors,warm,play-ack,localstore,loading,localdiag-live,remote-name'):
        self.bridge.state = self.cider.snapshot()
        self.bridge.available = True
        self.bridge.command(b'ci\t1\t131072\t' + caps.encode())
        return self.bridge.generation

    def test_rufin_first_when_cider_is_idle(self):
        gen = self.open()
        self.assertIn(f'CF\t{gen}\tremote\tCider'.encode(), self.frames())
        before = self.bridge.wire_library_generation()
        with self.assertLogs('herdremote.cider', logging.INFO) as logs:
            self.bridge._select_remote(0)
        self.assertIs(self.bridge.client, self.rufin)
        self.assertEqual(self.bridge.remote_name(), 'Rufin')
        self.assertTrue(any('remote switch from=Cider to=Rufin' in line for line in logs.output))
        self.assertFalse(any(TOKEN in line for line in logs.output))
        frames = self.frames()
        self.assertIn(f'CF\t{gen}\tremote\tRufin'.encode(), frames)
        self.assertNotEqual(self.bridge.wire_library_generation(), before)
        self.assertIn(f'CG\t{gen}\t{self.bridge.wire_library_generation()}'.encode(), frames)
        self.assertFalse(any(p.startswith(b'CA\t') and p.split(b'\t')[2] == b'0' for p in frames))

    def test_old_firmware_gets_no_remote_record(self):
        self.open('cn18,neighbors,warm,play-ack,localstore,modes5,loading,localdiag-live')
        self.bridge._select_remote(0)
        self.assertIs(self.bridge.client, self.rufin)
        self.assertFalse(any(p.startswith(b'CF\t') and b'remote' in p for p in self.frames()))

    def test_no_interrupt_while_cider_plays_then_home_row(self):
        self.cider.rate = 1000
        gen = self.open()
        self.bridge._select_remote(0)
        self.assertIs(self.bridge.client, self.cider)
        self.assertTrue(self.bridge.switch_deferred)
        self.bridge._select_remote(5)
        self.assertIs(self.bridge.client, self.cider)
        self.bridge.command(f'cc\t{gen}\t9\tremote-home\t0'.encode())
        self.assertIn(f'CK\t{gen}\t9\t1\tOK'.encode(), self.frames())
        self.bridge._select_remote(5.5)
        self.assertIs(self.bridge.client, self.rufin)

    def test_no_interrupt_until_cider_stops(self):
        self.cider.rate = 1000
        self.open()
        self.bridge._select_remote(0)
        self.assertIs(self.bridge.client, self.cider)
        self.bridge.state = dict(self.bridge.state, rate=0)
        self.bridge._select_remote(2)
        self.assertIs(self.bridge.client, self.rufin)

    def test_fallback_to_cider_and_back(self):
        self.open()
        self.bridge._select_remote(0)
        self.assertIs(self.bridge.client, self.rufin)
        epoch = self.bridge.remote_epoch
        self.server.down = True
        thread = threading.Thread(target=self.bridge._poll, daemon=True)
        self.bridge.threads.append(thread)
        thread.start()
        deadline = time.monotonic() + 8
        while self.bridge.client is not self.cider and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertIs(self.bridge.client, self.cider)
        self.assertGreater(self.bridge.remote_epoch, epoch)
        self.server.down = False
        deadline = time.monotonic() + 12
        while self.bridge.client is not self.rufin and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertIs(self.bridge.client, self.rufin)
        deadline = time.monotonic() + 3
        while (not self.bridge.state or self.bridge.state.get('title') != 'Song 1') and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.bridge.state['title'], 'Song 1')
        self.assertEqual(self.bridge.state['volume'], 20)  # host system volume, not Rufin's

    def test_controls_and_art_through_the_bridge(self):
        gen = self.open()
        self.bridge._select_remote(0)
        self.bridge.state = self.rufin.snapshot()
        self.bridge.command(f'cc\t{gen}\t3\tnext\t0'.encode())
        self.bridge.controls.put((gen, 4, 'toggle', 0, self.bridge.remote_epoch - 1))   # queued for Cider
        stop = threading.Thread(target=self.bridge._control, daemon=True)
        self.bridge.threads.append(stop)
        stop.start()
        deadline = time.monotonic() + 3
        frames = []
        while time.monotonic() < deadline and not any(f.startswith(f'CK\t{gen}\t4\t'.encode()) for f in frames):
            frames += self.frames()
            time.sleep(.05)
        self.assertIn(f'CK\t{gen}\t3\t1\tOK'.encode(), frames)
        self.assertIn(f'CK\t{gen}\t4\t0\tControl failed; not retried'.encode(), frames)
        self.assertEqual([p for p, _ in self.server.posts], ['/api/playback/next'])
        key = self.bridge.state['art']
        self.bridge.templates[key] = self.bridge.state['template']
        self.server.oversize = 30
        art = self.bridge.artwork(key, 'M')
        self.assertEqual(len(art), 12 + 128 * 128 * 2)
        call = [c for c in self.server.calls if c[1].startswith('/api/artwork')][-1]
        self.assertEqual(call[3], 'Bearer ' + TOKEN)
        self.assertIn('size=240', call[1])

    def test_pages_through_the_bridge_carry_the_wire_generation(self):
        gen = self.open()
        self.bridge._select_remote(0)
        self.bridge.state = self.rufin.snapshot()
        thread = threading.Thread(target=self.bridge._pages, daemon=True)
        self.bridge.threads.append(thread)
        thread.start()
        self.bridge.command(f'cq\t{gen}\t11\t0\t50\troot/Album'.encode())
        deadline = time.monotonic() + 3
        frames = []
        while time.monotonic() < deadline and not any(f.startswith(b'CL\t') for f in frames):
            self.bridge.command(f'cg\t{gen}\t131072'.encode())
            frames += self.frames()
            time.sleep(.05)
        page = next(f for f in frames if f.startswith(b'CL\t')).split(b'\n')[0].split(b'\t')
        self.assertEqual(int(page[3]), self.bridge.wire_library_generation())
        self.assertEqual(page[5], b'7')

    def test_production_bridge_can_disable_rufin(self):
        with mock.patch.dict(os.environ, {'RUFIN_DISABLE': '1'}), \
                mock.patch('hrcider_plugin.make_clients', return_value=self.cider):
            bridge = hrcider.Bridge(cache=self.cache, volume=mock.Mock())
        self.assertIsNone(bridge.rufin)


if __name__ == '__main__':
    unittest.main()
