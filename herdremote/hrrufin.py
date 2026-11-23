"""Rufin web-controller backend for the remote-music bridge (FU56).

Rufin (https://github.com/screwys/Rufin) serves a documented JSON API from its
web controller (``GET /api`` lists it).  API clients authenticate with
``Authorization: Bearer <token>``; the browser cookie/CSRF session is not used.
Only loopback HTTP is accepted, environment proxies are ignored and the token
is never logged, cached on disk or sent anywhere but the verified Rufin port.

Discovery sends nothing but ``GET /`` to a candidate port until that page shows
Rufin's ``rufin-csrf`` meta tag: port 1717 is commonly KDE Connect's.
"""
import collections
import hashlib
import io
import json
import logging
import os
import random
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import hrhandoff
from pathlib import Path

from hrcider import Client, HTTPStatusError, clean
from hrcider_cache import art_id
from hrcider_library import ROOT, row as menu_row, route as make_route
from hrlyrics import LyricsPages, lyric_sources, lyrics_rows, millis, source_rows

log = logging.getLogger('herdremote.rufin')

SIGNATURE = b'<meta name="rufin-csrf"'
DEFAULT_PORT = 1717
MAX_REPLY = 2 * 1024 * 1024
UNSUPPORTED = 'Not supported by Rufin'
CONTEXT_SECONDS = 15
REPEAT = {'off': 0, 'one': 1, 'all': 2}


class RufinAuthError(OSError):
    """401: the token was refused (never retried more than once)."""


class RufinBusy(OSError):
    """429 after too many failed logins; Retry-After seconds in `delay`."""
    def __init__(self, delay):
        super().__init__('Rufin refused logins for a while')
        self.delay = delay


def rufin_id(uri):
    """Short stable id for a track URI (CN id field and song routes)."""
    return 'r' + hashlib.sha256(uri.encode()).hexdigest()[:24]


def art_template(uri):
    return 'rufin:' + uri if uri else ''


def config_home(env):
    return Path(env.get('XDG_CONFIG_HOME') or Path(env.get('HOME') or Path.home()) / '.config')


def token_path(env=os.environ):
    return config_home(env) / 'podbox-herdremote' / 'rufin-token'


def load_token(env=os.environ):
    """(token, None) or (None, fixed reason). Never logs the token."""
    value = env.get('RUFIN_TOKEN')
    if value:
        value = value.strip()
        return (value, None) if re.fullmatch(r'[\x21-\x7e]{1,512}', value) else (None, 'Rufin token invalid')
    path = token_path(env)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None, 'Rufin token missing'
    except OSError:
        return None, 'Rufin token unreadable'
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None, 'Rufin token file must be a 0600 file of this user'
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    value = raw.decode('ascii', 'replace').strip()
    if len(raw) > 4096 or not re.fullmatch(r'[\x21-\x7e]{1,512}', value):
        return None, 'Rufin token invalid'
    return value, None


def loopback_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', 'localhost', '::1') or parts.path not in ('', '/'):
        raise ValueError('RUFIN_API_URL must be loopback HTTP')
    return url.rstrip('/')


def candidates(env=os.environ):
    """Base URLs to verify, in order. Settings port first, then port+1."""
    if env.get('RUFIN_API_URL'):
        return [loopback_url(env['RUFIN_API_URL'])]
    port, host = DEFAULT_PORT, '127.0.0.1'
    try:
        with open(config_home(env) / 'rufin' / 'settings.json', 'rb') as f:
            controller = json.loads(f.read(4 * 1024 * 1024)).get('web_controller') or {}
        if controller.get('enabled') is False:
            return []
        value = controller.get('port', DEFAULT_PORT)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65535:
            port = value
        if str(controller.get('address', '')).startswith(('::', '[::')):
            host = '[::1]'
    except (OSError, ValueError, AttributeError):
        pass
    return [f'http://{host}:{port}', f'http://{host}:{port + 1}']


class RufinClient(Client):
    """Same consumer interface the bridge uses for Cider; Rufin's API underneath."""
    name = 'Rufin'
    using_plugin = False
    explicit_loading = True
    # Rufin 0.17.1 uses the same untimestamped projection for REST and SSE.
    handoff_position_period_ms = 500

    def __init__(self, env=None, timeout=.8, opener=None, verify_timeout=1.0):
        super().__init__('http://127.0.0.1:9', None, timeout, opener)
        self.env = os.environ if env is None else env
        self.base = None
        self.verify_timeout = verify_timeout
        self.ready = False
        self.reason = 'Rufin not checked'
        self.retry_at = 0.
        self.failures = 0
        self.state_lock = threading.RLock()
        self.uris = collections.OrderedDict()    # rufin_id -> track URI
        self.current = {}
        self.last_reason = None

    # --- connection, discovery and authentication -----------------------
    def _set_reason(self, reason, delay, now):
        self.ready = False
        self.reason = reason
        self.retry_at = now + delay
        if reason != self.last_reason:
            log.info('Rufin unavailable reason=%s', reason)
            self.last_reason = reason

    def mark_failed(self, reason, delay=5):
        with self.state_lock:
            self._set_reason(reason, delay, time.monotonic())

    def healthy(self):
        return self.ready and self.failures < 3

    def ensure(self, now=None):
        """True when Rufin is reachable and authenticated. Bounded retries."""
        now = time.monotonic() if now is None else now
        with self.state_lock:
            if self.healthy():
                return True
            if now < self.retry_at:
                return False
            token, reason = load_token(self.env)
            if token is None:
                self._set_reason(reason, 10, now)
                self.token = None
                return False
            self.token = token
            if self.base is None:
                try:
                    found = self.discover()
                except ValueError:
                    self._set_reason('RUFIN_API_URL must be loopback HTTP', 60, now)
                    return False
                if found is None:
                    self._set_reason('Rufin not running', 5, now)
                    return False
                self.base = found
            try:
                self._playback_raw()
            except RufinAuthError:
                self._set_reason('Rufin token rejected', 60, now)
                return False
            except RufinBusy as error:
                self._set_reason('Rufin token rejected', max(10, min(300, error.delay)), now)
                return False
            except (OSError, ValueError):
                self.base = None
                self._set_reason('Rufin not answering', 5, now)
                return False
            self.ready, self.failures, self.reason, self.last_reason = True, 0, '', None
            log.info('Rufin connected url=%s', self.base)
            return True

    def discover(self):
        for base in candidates(self.env):
            if self.verify(base):
                return base
        return None

    def verify(self, base):
        """Only GET / is sent to an unverified port; no token, no body."""
        request = urllib.request.Request(base + '/', headers={'Accept': 'text/html'}, method='GET')
        try:
            with self.opener.open(request, timeout=self.verify_timeout) as response:
                if response.status != 200:
                    return False
                return SIGNATURE in response.read(256 * 1024)
        except (OSError, ValueError, urllib.error.HTTPError):
            return False

    def _request(self, path, body=None, timeout=None, method=None, allow_list=False, retried=False):
        base, token = self.base, self.token
        if base is None or not token:
            raise OSError('Rufin not connected')
        headers = {'Accept': 'application/json', 'Authorization': 'Bearer ' + token}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        observer = getattr(self, 'rpc_observer', None)
        playback = body is not None and path.startswith(('/api/playback/', '/api/queue'))
        if observer and playback:
            observer('rpc-sent', path)
        try:
            with self.opener.open(req, timeout=self.timeout if timeout is None else timeout) as r:
                raw = r.read(MAX_REPLY + 1)
        except urllib.error.HTTPError as error:
            code = error.code
            retry = error.headers.get('Retry-After', '60') if error.headers else '60'
            error.close()
            if code == 401:
                # Re-read the token source once (the user may have paired again).
                fresh, _ = load_token(self.env)
                if not retried and fresh and fresh != token:
                    self.token = fresh
                    return self._request(path, body, timeout, method, allow_list, True)
                with self.state_lock:
                    self.ready = False
                    self.reason = 'Rufin token rejected'
                    self.retry_at = time.monotonic() + 60
                raise RufinAuthError('Rufin token rejected') from None
            if code == 429:
                with self.state_lock:
                    self.ready = False
                raise RufinBusy(int(retry) if retry.isdigit() else 60) from None
            if code >= 500:
                self.failures += 1
            raise HTTPStatusError(code) from None
        except OSError:
            self.failures += 1
            raise
        if len(raw) > MAX_REPLY:
            raise ValueError('Rufin reply too large')
        self.failures = 0
        if observer and playback:
            observer('rpc-returned', path)
        value = json.loads(raw) if raw.strip() else {}
        if value is None:
            return {}
        if allow_list and isinstance(value, list):
            return value
        if not isinstance(value, dict):
            raise ValueError('Malformed Rufin reply')
        return value

    def get(self, path, timeout=None, **params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self.request(path + ('?' + query if query else ''), timeout=timeout)

    def post(self, path, body=None, timeout=None):
        return self.request(path, {} if body is None else body, timeout=timeout, method='POST')

    # --- state ----------------------------------------------------------
    def remember(self, uri):
        key = rufin_id(uri)
        with self.state_lock:
            self.uris[key] = uri
            self.uris.move_to_end(key)
            while len(self.uris) > 20000:
                self.uris.popitem(last=False)
        return key

    def uri(self, key):
        with self.state_lock:
            return self.uris.get(key)

    @staticmethod
    def album_uri(track):
        """The album URI of a track row, from its album link's page token."""
        for span in ((track.get('links') or {}).get('album') or []):
            route = (span.get('page') or {}).get('route') if isinstance(span, dict) else None
            if isinstance(route, dict) and isinstance(route.get('AlbumDetail'), str):
                return route['AlbumDetail']
        return ''

    def _playback_raw(self):
        value = self._request('/api/playback', timeout=self.timeout)
        if not isinstance(value, dict):
            raise ValueError('Malformed Rufin playback')
        return value

    def playback(self, full=False):
        return self.request('/api/playback')

    def snapshot(self):
        try:
            return self._snapshot_data(self.playback())
        except (TypeError, AttributeError, KeyError, OverflowError):
            raise ValueError('Malformed Rufin state') from None

    def _snapshot_data(self, data):
        current = data.get('current') or {}
        track = current.get('track') or {}
        if not isinstance(current, dict) or not isinstance(track, dict):
            raise ValueError('Malformed Rufin track')
        text = lambda value: value if isinstance(value, str) else ''
        uri = text(track.get('uri')) or text(current.get('media_uri'))
        status = text(data.get('state')) or 'stopped'
        duration = max(0, int(data.get('duration_ms') or track.get('duration_ms') or 0))
        position = max(0, int(data.get('position_ms') or 0))
        if duration:
            position = min(duration, position)
        key = self.remember(uri) if uri else ''
        art_uri = self.album_uri(track) or uri
        template = art_template(art_uri)
        repeat = REPEAT.get(data.get('repeat'), 0)
        shuffle = int(bool(data.get('shuffle')))
        volume = data.get('volume')
        volume = max(0, min(100, round(float(volume) * 100))) if isinstance(volume, (int, float)) else 0
        with self.state_lock:
            self.current = dict(uri=uri, state=status, track=track, art_uri=art_uri)
        return dict(id=key, repeat5=self._mode5(repeat, shuffle, key), title=text(track.get('title')),
                    artist=text(track.get('artist')), album=text(track.get('album')), composer='',
                    duration=duration, position=position, rate=1000 if status == 'playing' else 0,
                    volume=volume, repeat=repeat, shuffle=shuffle, art=art_id(template),
                    template=template, bg='181818', favorite=bool(track.get('favorite')),
                    loading=status in ('resolving', 'buffering'), track_run=current.get('run'))

    def loading_state(self):
        with self.state_lock:
            return self.current.get('state') in ('resolving', 'buffering')

    def refresh_snapshot(self):
        pass

    def transport_snapshot(self):
        state = self.snapshot()
        return dict(id=state['id'], ids={state['id']} if state['id'] else set(),
                    duration=state['duration'], position=state['position'], rate=state['rate'])

    def now_playing(self):
        with self.state_lock:
            return dict(self.current)

    def same_song(self, route):
        parts = str(route).split('/')
        if len(parts) != 3 or parts[0] != 'play' or parts[1] != 'library-songs':
            return False
        state = self.transport_snapshot()
        if urllib.parse.unquote(parts[2]) not in state['ids']:
            return False
        if not state['rate']:
            self.post('/api/playback/play')
        return True

    def resume_selected(self):
        self.post('/api/playback/play')

    def pause_if_playing(self):
        if self.playback().get('state') == 'playing':
            self.post('/api/playback/pause')

    # Base Client keeps the Rockbox repeat5/A-B logic; only the RPCs differ.
    def _put_repeat(self, mode):
        return self.post('/api/playback/repeat', {'mode': {'none': 'off'}.get(mode, mode)})

    def _put_shuffle(self, on):
        return self.post('/api/playback/shuffle', {'enabled': bool(on)})

    def control(self, command, value=0):
        simple = {'resume': 'play', 'pause': 'pause', 'next': 'next', 'previous': 'previous'}
        if command in simple:
            return self.post('/api/playback/' + simple[command])
        if command == 'toggle':
            playing = self.playback().get('state') == 'playing'
            return self.post('/api/playback/' + ('pause' if playing else 'play'))
        if command == 'seek':
            ms = float(value)
            if ms != ms or ms in (float('inf'), float('-inf')):
                raise ValueError('Nonfinite seek')
            return self.post('/api/playback/seek', {'position_ms': max(0, int(ms))})
        if command in ('repeat5', 'ab-mark'):
            try:
                number = int(str(value).strip())
            except ValueError:
                raise ValueError('Invalid ' + command + ' value') from None
            return self._repeat5(number) if command == 'repeat5' else self._ab_mark(max(0, number))
        if command in ('shuffle', 'repeat'):
            try:
                number = int(str(value).strip())
            except ValueError:
                raise ValueError('Invalid ' + command + ' value') from None
            if command == 'shuffle':
                if number not in (0, 1):
                    raise ValueError('Invalid shuffle value')
                return self._put_shuffle(number)
            if number not in (0, 1, 2):
                raise ValueError('Invalid repeat value')
            return self._put_repeat(('none', 'one', 'all')[number])
        if command in ('love', 'unlove'):
            with self.state_lock:
                uri = self.current.get('uri')
            if not uri:
                raise ValueError('Nothing is playing')
            return self.post('/api/favorite', {'kind': 'track', 'uri': uri, 'favorite': command == 'love'})
        if command == 'queue-jump':
            index = int(value)
            if index < 0:
                raise ValueError('Invalid queue index')
            return self.post('/api/queue/activate', {'position': index})
        if command == 'play-item':
            kind, _, key = str(value).partition(':')
            uri = self.uri(key) if kind in ('library-songs', 'songs') else None
            if not uri:
                raise ValueError('Selected item unavailable')
            self.post('/api/queue', {'uris': [uri], 'mode': 'replace'})
            return {'status': 'ok', 'expected_ids': [key]}
        raise ValueError(UNSUPPORTED)

    def handoff_match(self, track):
        # Search the currently selected library through the controller.
        scope = RufinLibrary(self).scope()
        body = self.get('/api/tracks', q=track[0], limit=100, total='true', **scope)
        rows = RufinLibrary._rows(body)
        if body.get('total', len(rows)) > len(rows):
            return None
        found = hrhandoff.match_track(track, [
            (row.get('title', ''), row.get('artist', ''), row.get('album', ''),
             row.get('duration_ms', 0), row.get('uri', ''))
            for row in rows if isinstance(row, dict)
        ])
        return (found,) if found else None

    def handoff_state(self):
        data = self._playback_raw()
        item = data.get('current') or {}
        uri = (item.get('track') or {}).get('uri') or item.get('media_uri')
        return dict(ids={uri}, position=int(data.get('position_ms') or 0),
                    loading=bool(data.get('refilling')),
                    playing=data.get('state') == 'playing' and data.get('can_seek') is True,
                    volume=float(data['volume']))

    def handoff_volume(self, volume):
        self.post('/api/playback/volume', {'volume': volume, 'persist': False})

    def handoff_checkpoint(self):
        state = self._playback_raw()
        queue, _ = self.queue_window(1)
        if state.get('queue_total') != queue['total']:
            raise hrhandoff.HandoffError('queue-changed')
        status = state['state']
        if status in ('buffering', 'resolving'):
            status = 'playing'
        return dict(volume=float(state['volume']), status=status,
                    position=int(state.get('position_ms') or 0),
                    index=queue.get('current_index'), total=queue['total'])

    def _handoff_wait(self, observe, check):
        while True:
            check()
            if observe():
                return
            time.sleep(.025)

    def _handoff_queue_slot(self, before):
        queue, window = self.queue_window()
        if queue['total'] == before['total']:
            return False
        if queue['total'] != before['total'] + 1:
            raise hrhandoff.HandoffError('queue-changed')
        slot = before['added_index']
        if any(row['position'] == slot and row['track'].get('uri') == before['added_uri']
               for row in window):
            return True
        if not queue.get('loading'):
            raise hrhandoff.HandoffError('queue-changed')
        return False

    def _handoff_at(self, index):
        state = self._playback_raw()
        return (state.get('current') or {}).get('position') == index and (
            state.get('can_seek') is True and not state.get('refilling'))

    def handoff_prepare(self, matched, before, check):
        # Insert next to the selected track so the whole original queue stays
        # in Rufin, including entries outside the controller's queue window.
        index = before['index']
        slot = before['total'] if index is None else index + 1
        before.update(added_index=slot, added_uri=matched[0])
        self.post('/api/queue', {'uris': [matched[0]], 'mode': 'next'})
        self._handoff_wait(lambda: self._handoff_queue_slot(before), check)
        before['activate_issued'] = True
        self.post('/api/queue/activate', {'position': slot})
        self._handoff_wait(lambda: self._handoff_at(slot), check)

    def handoff_restore(self, before):
        if 'added_index' not in before:
            return
        deadline = time.monotonic() + 4

        def check():
            if time.monotonic() >= deadline:
                raise hrhandoff.HandoffError('rollback-timeout')

        self._handoff_wait(lambda: self._handoff_queue_slot(before), check)
        index = before['index']
        status = before['status'] if before['status'] in ('playing', 'paused') else 'stopped'
        try:
            if before.get('activate_issued'):
                if index is None:
                    self.post('/api/playback/stop')
                else:
                    self.post('/api/queue/activate', {'position': index})
                    self._handoff_wait(lambda: self._handoff_at(index), check)
                    if status == 'paused':
                        self.handoff_pause()
                    elif status == 'stopped':
                        self.post('/api/playback/stop')
                    self.handoff_seek(before['position'])
                self._handoff_wait(lambda: self._playback_raw()['state'] == status, check)
        finally:
            self.post('/api/queue/item', {'position': before['added_index']})
        self._handoff_wait(lambda: self.queue_state()[1] == before['total'], check)

    def handoff_seek(self, position):
        self.control('seek', position)

    def handoff_pause(self):
        self.control('pause')

    # --- queue ----------------------------------------------------------
    def queue_window(self, limit=100):
        data = self.get('/api/queue', limit=limit)
        window = data.get('window') or []
        if not isinstance(window, list):
            raise ValueError('Malformed Rufin queue')
        return data, [v for v in window if isinstance(v, dict) and isinstance(v.get('track'), dict)]

    def queue_state(self):
        data, _ = self.queue_window(1)
        current = data.get('current_index')
        return (int(current) if isinstance(current, int) else -1), int(data.get('total') or 0)

    def neighbor_art(self, current):
        data, window = self.queue_window(3)
        index = data.get('current_index')
        rows = {v.get('position'): v['track'] for v in window}
        keys, templates = [], {}
        for position in ((index - 1, index + 1) if isinstance(index, int) else ()):
            track = rows.get(position)
            uri = (self.album_uri(track) or track.get('uri') or '') if track else ''
            template = art_template(uri)
            key = art_id(template)
            keys.append(key)
            if template:
                templates[key] = template
        while len(keys) < 2:
            keys.append('-')
        return keys, templates

    def artwork(self, uri, size=240):
        """Fetch Rufin cover bytes and bound the dimensions to the requested size."""
        data = self._raw('/api/artwork?' + urllib.parse.urlencode({'uri': uri, 'size': size}))
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as image:
            if image.width <= size and image.height <= size:
                return data
            small = ImageOps.contain(image.convert('RGB'), (size, size))
        out = io.BytesIO()
        small.save(out, 'PNG')
        return out.getvalue()

    def _raw(self, path):
        base, token = self.base, self.token
        if base is None or not token:
            raise OSError('Rufin not connected')
        req = urllib.request.Request(base + path, headers={'Authorization': 'Bearer ' + token})
        try:
            with self.opener.open(req, timeout=2) as response:
                raw = response.read(MAX_REPLY + 1)
        except urllib.error.HTTPError as error:
            error.close()
            raise HTTPStatusError(error.code) from None
        if len(raw) > MAX_REPLY:
            raise ValueError('Artwork too large')
        return raw


def jarg(value):
    """A Rufin id parameter is JSON text (numbers stay numbers)."""
    return json.dumps(value, separators=(',', ':'))


def library_generation(*parts):
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(',', ':')).encode())
    return int.from_bytes(digest.digest()[:4], 'big') or 1


class RufinLibrary:
    """Native Music tree routes over Rufin's own library lists."""
    COLLECTIONS = {'album': ('/api/albums/tracks', '/api/queue/album', {}),
                   'playlist': ('/api/playlists/entries', '/api/queue/playlist', {}),
                   'artist': ('/api/artists/tracks', '/api/queue/artist', {}),
                   'albumartist': ('/api/artists/tracks', '/api/queue/artist', {'album_artists': 'true'}),
                   'genre': ('/api/genres/tracks', '/api/queue/genre', {}),
                   'smartplaylist': ('/api/smart-playlists/tracks', '/api/queue/smart-playlist', {})}

    def __init__(self, client):
        self.client = client
        self.state = {}
        self.context = None
        self.generation = library_generation("rufin-pending-v1")
        self.native_albums = None
        self.native_refresh_at = 0.
        self.lock = threading.RLock()
        self.templates = collections.OrderedDict()   # art id -> template
        self.tokens = collections.OrderedDict()      # route hash -> page token JSON
        self.sources = (0., {})
        self.lyrics = LyricsPages()

    # --- registries -----------------------------------------------------
    def template(self, key):
        with self.lock:
            return self.templates.get(key)

    def art(self, uri):
        return self.register_art(art_template(uri))

    def register_art(self, template):
        if not template:
            return '-'
        key = art_id(template)
        with self.lock:
            self.templates[key] = template
            self.templates.move_to_end(key)
            while len(self.templates) > 20000:
                self.templates.popitem(last=False)
        return key

    def token_route(self, prefix, token):
        text = json.dumps(token, separators=(',', ':'), sort_keys=True)
        key = 't' + hashlib.sha256(text.encode()).hexdigest()[:24]
        with self.lock:
            self.tokens[key] = text
            self.tokens.move_to_end(key)
            while len(self.tokens) > 4096:
                self.tokens.popitem(last=False)
        return prefix + '/' + key

    def scope(self, *, fresh=False):
        """The source and music folder Rufin has selected (30 s cache)."""
        stamp, value = self.sources
        if not fresh and time.monotonic() - stamp < 30 and value:
            return dict(value)
        data = self.client.get('/api/sources')
        value = {}
        if isinstance(data.get('selected'), str) and data['selected']:
            value['source'] = data['selected']
            if isinstance(data.get('library'), str) and data['library']:
                value['folder'] = data['library']
        self.sources = (time.monotonic(), value)
        return dict(value)

    # --- rows -----------------------------------------------------------
    def track_row(self, track, duration=False):
        uri = track.get('uri') if isinstance(track.get('uri'), str) else ''
        if not uri:
            return None
        key = self.client.remember(uri)
        ms = int(track.get('duration_ms') or 0)
        title = str(track.get('title') or '')
        if duration:
            title += f' - {ms // 60000:02}:{ms // 1000 % 60:02}'
        year = track.get('year')
        return dict(id=key, title=title, subtitle=str(track.get('artist') or ''),
                    art=self.art(self.client.album_uri(track) or uri), duration=ms, count=0, kind='song',
                    route='play/library-songs/' + key, bg='181818',
                    year=year if isinstance(year, int) and 0 <= year < 10000 else 0)

    def collection_row(self, value, duration=False):
        kind = value.get('kind')
        if kind == 'track':
            return self.track_row(value, duration)
        ident = value.get('id')
        if ident is None:
            return None
        quoted = urllib.parse.quote(jarg(ident), safe='')
        count = int(value.get('track_count') or 0)
        ms = int(value.get('duration_ms') or 0)
        if kind == 'album':
            year = value.get('year')
            return dict(id=jarg(ident), title=str(value.get('title') or ''), subtitle=str(value.get('artist') or ''),
                        art=self.art(value.get('uri') or ''), duration=ms, count=count, kind='album',
                        route='album/' + quoted, bg='181818',
                        year=year if isinstance(year, int) and 0 <= year < 10000 else 0)
        names = {'artist': ('albumartist' if value.get('album_artists') else 'artist', 'artist'),
                 'playlist': ('playlist', 'playlist'), 'smart-playlist': ('smartplaylist', 'playlist'),
                 'playlist-folder': ('smartfolder' if value.get('smart') else 'plfolder', 'menu'),
                 'genre': ('genre', 'menu')}
        if kind not in names:
            return None
        path, view = names[kind]
        art = '-'
        if kind == 'artist':
            uri = value.get('uri')
            uri = uri if isinstance(uri, str) else ''
            template = 'rufin-artist:' + jarg([path, ident, uri])
            art = self.register_art(template)
        return dict(id=jarg(ident), title=str(value.get('name') or value.get('title') or ''), subtitle='', art=art,
                    duration=ms, count=count, kind=view, route=path + '/' + quoted, bg='181818', year=0)

    def artist_artwork(self, template, size):
        kind, ident, uri = json.loads(template.removeprefix('rufin-artist:'))
        if uri:
            try:
                return self.client.artwork(uri, size=size)
            except (OSError, ValueError):
                pass
        endpoint, _, filters = self.COLLECTIONS[kind]
        best = None
        offset = 0
        while True:
            page = self.client.get(endpoint, id=jarg(ident), offset=offset,
                                   limit=100, total='true', **filters)
            tracks = self._rows(page)
            for track in tracks:
                album_uri = self.client.album_uri(track)
                if not album_uri:
                    continue
                year = track.get('year')
                rank = (year if isinstance(year, int) and year > 0 else 10000,
                        str(track.get('album') or '').casefold(), album_uri)
                if best is None or rank < best:
                    best = rank
            offset += len(tracks)
            if not tracks or offset >= page.get('total', offset):
                break
        if best is None:
            raise ValueError('No album for artist artwork')
        return self.client.artwork(best[2], size=size)

    # --- reading --------------------------------------------------------
    @staticmethod
    def _rows(data):
        for name in ('tracks', 'albums', 'artists', 'playlists', 'smart_playlists', 'entries'):
            if isinstance(data.get(name), list):
                return data[name]
        raise ValueError('Malformed Rufin page')

    def _list(self, endpoint, params, offset, limit, duration=False):
        data = self.client.get(endpoint, timeout=3, offset=offset, limit=limit, total='true', **params)
        values = [v for v in self._rows(data) if isinstance(v, dict)]
        rows = [r for r in (self.collection_row(v, duration) for v in values) if r]
        total = data.get('total')
        total = int(total) if isinstance(total, int) else offset + len(rows)
        first = data.get('offset')
        return rows, total, int(first) if isinstance(first, int) else offset

    def refresh(self):
        if time.monotonic() < self.native_refresh_at:
            return
        self.native_refresh_at = time.monotonic() + 15
        try:
            scope, rows = self.native_album_catalogue()
        except (OSError, ValueError, TypeError):
            return
        revision = library_generation('rufin-v1', scope, rows)
        with self.lock:
            self.native_albums = rows
            self.generation = revision

    def album_inventory(self):
        with self.lock:
            if self.native_albums is None:
                return None
            return self.generation, len(self.native_albums), self.album_art_keys(self.native_albums)

    @staticmethod
    def album_art_keys(rows):
        return {row['art'] for row in rows if row.get('art') not in (None, '-')}

    def native_album_catalogue(self, scope=None):
        """Read the same album rows exposed by the unfiltered native route."""
        if scope is None:
            scope = self.scope(fresh=True)
        rows = []
        expected = None
        while True:
            items, total, first = self._list('/api/albums', dict(scope, q=None), len(rows), 50)
            if (first != len(rows) or not first <= total <= 1000000
                    or (expected is not None and total != expected)
                    or (not items and first < total)):
                raise ValueError('Incomplete Rufin album catalogue')
            rows.extend(items)
            if len(rows) >= total:
                return scope, tuple(rows)
            expected = total

    def collection(self, name):
        """(read endpoint, queue endpoint, params) of a list route, or None."""
        if '/' not in name:
            return None
        kind, _, rest = name.partition('/')
        if kind in ('list', 'album') and rest.startswith('t'):
            with self.lock:
                token = self.tokens.get(rest)
            if token is None:
                raise ValueError('Unknown Rufin page')
            return '/api/page', '/api/queue', {'page': token}
        if kind not in self.COLLECTIONS:
            return None
        read, play, extra = self.COLLECTIONS[kind]
        ident = urllib.parse.unquote(rest)
        json.loads(ident)   # malformed ids fail before any request
        params = dict(extra, id=ident)
        if kind == 'smartplaylist':
            params.update(self.scope())
        return read, play, params

    def _plain(self, name, args):
        """(endpoint, params) for the flat lists; None when unsupported."""
        scope = self.scope()
        if name == 'songs':
            return '/api/tracks', dict(scope, q=args.get('title~'))
        if name == 'albums':
            return '/api/albums', dict(scope, q=args.get('album~'))
        if name == 'artists':
            return '/api/artists', dict(scope, q=args.get('artist~'))
        if name == 'albumartists':
            return '/api/artists', dict(scope, album_artists='true', q=args.get('albumartist~'))
        if name == 'playlists':
            return '/api/playlists', {'source': scope.get('source')}
        if name == 'recent':
            return '/api/albums', dict(scope, sort='date_added', descending='true')
        if name == 'history/tracks':
            return '/api/tracks', dict(scope, sort='last_played', descending='true')
        if name == 'history/albums':
            return '/api/albums', dict(scope, sort='last_played', descending='true')
        if name == 'history/artists':
            return '/api/artists', dict(scope, sort='last_played', descending='true')
        if name == 'genres':
            token = {'route': 'Genres'}
            token.update({k: v for k, v in scope.items() if k in ('source', 'folder')})
            return '/api/page', {'page': json.dumps(token, separators=(',', ':'))}
        return None

    def page(self, query, offset, limit, stale=False):
        if len(query) > 380 or offset < 0 or not 1 <= limit <= 50:
            raise ValueError('Invalid native library query')
        parsed = urllib.parse.urlsplit(query)
        name, args = parsed.path, dict(urllib.parse.parse_qsl(parsed.query))
        if name.startswith('root/'):
            name = ROOT.get(urllib.parse.unquote(name[5:]), 'empty')
        with self.lock:
            generation, albums = self.generation, self.native_albums
        result = lambda items, total, first=offset: dict(generation=generation, offset=first, total=total,
                                                         stale=bool(stale), items=items)
        if albums is not None and name == 'albums' and set(args) <= {'format'}:
            return result(albums[offset:offset + limit], len(albums))
        if name == 'now/lyrics' and args.get('lrc') == '1':
            items = self.lyrics.get(query, args.get('track', ''), offset, self.timed_lyrics, menu_row, duet=args.get('duet') == '1')
            return dict(result(items[offset:offset + limit], len(items)), _lyrics_v1=True)
        if name.startswith('now/'):
            items = self.now(name)
            return result(items[offset:offset + limit], len(items))
        if name.startswith('play/library-albums/'):
            raise ValueError('Invalid native library query')
        if set(args) - {'format', 'title~', 'album~', 'artist~', 'albumartist~', 'q'}:
            return result([], 0)      # filters Rufin cannot answer: native empty list
        menus = {
            'tracks': [('Title', 'songs'), ('Title (with track duration)', 'songs?format=duration')],
            'searchby': [(x, make_route('input', field=y)) for x, y in
                         [('Album', 'album'), ('Artist', 'artist'), ('Album Artist', 'albumartist'), ('Title', 'title')]],
            'history': [('Recently played tracks', 'history/tracks'), ('Recently Played Albums', 'history/albums'),
                        ('Recently Played Artists', 'history/artists')],
        }
        if name in menus:
            items = [menu_row(t, p) for t, p in menus[name]]
            return result(items[offset:offset + limit], len(items))
        if name == 'random':
            _, total, _ = self._list('/api/albums', self.scope(), 0, 1)
            if not total:
                return result([], 0)
            rows, _, _ = self._list('/api/albums', self.scope(), random.randrange(total), 1)
            items = []
            for value in rows[:1]:
                value = dict(value, kind='song', route='play/library-albums/' + urllib.parse.quote(value['id'], safe=''))
                items.append(value)
            return result(items[offset:offset + limit], len(items))
        if name == 'shuffle':
            _, total, _ = self._list('/api/tracks', self.scope(), 0, 1)
            items = [menu_row('Shuffle Songs', 'shuffle', 'song')] if total else []
            return result(items[offset:offset + limit], len(items))
        if name == 'search':
            term = args.get('q', '')
            if not term:
                return result([], 0)
            scope, items = self.scope(), []
            for endpoint in ('/api/tracks', '/api/albums', '/api/artists'):
                rows, _, _ = self._list(endpoint, dict(scope, q=term), 0, 50)
                items.extend(rows)
            return result(items[offset:offset + limit], len(items))
        if name.startswith(('plfolder/', 'smartfolder/')):
            folder = urllib.parse.unquote(name.split('/', 1)[1])
            json.loads(folder)
            endpoint = '/api/playlists' if name.startswith('plfolder/') else '/api/smart-playlists'
            rows, total, first = self._list(endpoint, {'playlist_folder': folder}, offset, limit)
            return result(rows, total, first)
        collection = self.collection(name)
        if collection:
            read, _, params = collection
            rows, total, first = self._list(read, params, offset, limit, args.get('format') == 'duration')
            return result(rows, total, first)
        plain = self._plain(name, args)
        if plain:
            rows, total, first = self._list(plain[0], plain[1], offset, limit, args.get('format') == 'duration')
            return result(rows, total, first)
        # Composer, year, letters, ratings, podcasts...: no Rufin equivalent.
        return result([], 0)

    def timed_lyrics(self, track):
        def playing():
            playback = self.client.playback()
            current = playback.get('current') or {}
            item = current.get('track') or {}
            return (current.get('media_uri') or item.get('uri'), current.get('run'),
                    millis(playback.get('duration_ms')) or millis(item.get('duration_ms')) or 0)

        uri, run, duration = playing()
        if not isinstance(uri, str) or rufin_id(uri) != track:
            return None

        def current_lyrics():
            reply = self.client.get('/api/lyrics')
            if playing()[:2] != (uri, run):
                return None
            if isinstance(reply, dict) and (reply.get('media_uri') != uri or
                                           (run is not None and reply.get('run') != run)):
                return None
            return reply

        reply = current_lyrics()
        if isinstance(reply, dict) and reply.get('state') == 'empty':
            # GET is a snapshot. Rufin uses "empty" both before a lookup
            # and after a miss. POST starts the lookup once for the current
            # media; Rufin leaves an already attempted lookup alone.
            self.client.post('/api/lyrics')
            reply = current_lyrics()
        if isinstance(reply, str):
            return source_rows(reply, 0, duration, menu_row)
        if not isinstance(reply, dict) or reply.get('state') == 'loading':
            return None
        if reply.get('state') != 'ready':
            return lyrics_rows([], 0, duration, menu_row)
        document = reply.get('document') or {}
        offset = document.get('offset_millis') if isinstance(document, dict) else 0
        offset = offset if type(offset) is int else 0
        sources = [value for container in (document, reply) for value in lyric_sources(container)
                   if value.lstrip('\ufeff \t\r\n').startswith('<')]
        if sources:
            return source_rows(sources[:1], offset, duration, menu_row)
        if not isinstance(document, dict):
            return lyrics_rows([], 0, duration, menu_row)
        lines = []
        for line in document.get('lines') or []:
            if not isinstance(line, dict) or not isinstance(line.get('text'), str):
                continue
            text = line['text']
            start, end = millis(line.get('start_millis')), millis(line.get('end_millis'))
            cues = []
            for cue_line in line.get('cue_lines') or []:
                if not isinstance(cue_line, dict) or cue_line.get('text') != text:
                    continue
                if start is None:
                    start = millis(cue_line.get('start_millis'))
                if end is None:
                    end = millis(cue_line.get('end_millis'))
                for cue in cue_line.get('cues') or []:
                    if isinstance(cue, dict):
                        begin = millis(cue.get('start_millis'))
                        if begin is not None:
                            cues.append((begin, millis(cue.get('end_millis')),
                                         cue.get('byte_start'), cue.get('byte_end_exclusive')))
                break
            lines.append((text, start, end, cues))
        return lyrics_rows(lines, offset, duration, menu_row)

    def now(self, name):
        client = self.client
        current = client.now_playing()
        track = current.get('track') or {}
        if name == 'now/info':
            ms = track.get('duration_ms')
            values = [('title', track.get('title')), ('artist', track.get('artist')), ('album', track.get('album')),
                      ('disc', None), ('track', None), ('year', None), ('genre', None), ('duration', ms),
                      ('quality', None), ('favorite', int(bool(track.get('favorite')))), ('library', None), ('speed', None)]
            return [menu_row(str(v) if v is not None else '', '', id=k) for k, v in values]
        if name in ('now/album', 'now/artist'):
            field = 'album' if name == 'now/album' else 'artist'
            for span in ((track.get('links') or {}).get(field) or []):
                page = span.get('page') if isinstance(span, dict) else None
                if isinstance(page, dict) and page.get('route'):
                    target = self.token_route('album' if field == 'album' else 'list', page)
                    return [menu_row(str(span.get('name') or track.get(field) or ''), target)]
            return []
        if name == 'now/lyrics':
            data = client.get('/api/lyrics')
            lines = ((data.get('document') or {}).get('lines') or []) if data.get('state') == 'ready' else []
            items = []
            for line in lines:
                text = str(line.get('text') or '') if isinstance(line, dict) else ''
                while text:
                    part = text.encode()[:116].decode('utf-8', 'ignore') or text[:1]
                    items.append(menu_row(part, '', id=str(len(items))))
                    text = text[len(part):]
            return items or [menu_row('No lyrics', '')]
        if name in ('now/queue', 'now/time'):
            data, window = client.queue_window(100)
            position = data.get('current_index')
            position = position if isinstance(position, int) else -1
            total = int(data.get('total') or 0)
            if name == 'now/queue':
                items = []
                for entry in window:
                    item = self.track_row(entry['track'])
                    if item and isinstance(entry.get('position'), int):
                        items.append(dict(item, route='queue/' + str(entry['position']), count=position))
                return items
            remaining = ''
            ends = window and window[-1].get('position') == total - 1
            if ends:
                elapsed = int(self.state.get('position') or 0)
                ms = 0
                for entry in window:
                    at, length = entry.get('position'), int(entry['track'].get('duration_ms') or 0)
                    if isinstance(at, int) and at >= position:
                        ms += max(0, length - elapsed) if at == position else length
                minutes, seconds = divmod(ms // 1000, 60)
                remaining = f'{minutes}:{seconds:02}'
            return [menu_row(remaining, '', id='remaining'), menu_row(str(max(0, total - max(position, 0))), '', id='tracks')]
        raise ValueError('Unknown Now Playing page')

    # --- playback -------------------------------------------------------
    def set_context(self, value):
        index, _, route = str(value).partition('|')
        self.context = None
        if index.isdigit() and 0 < len(route) <= 380 and int(index) < 100000:
            self.context = (int(index), route, time.monotonic())

    def play(self, path):
        client = self.client
        context, self.context = self.context, None
        if path == 'shuffle':
            client.post('/api/playback/shuffle', {'enabled': True})
            client.post('/api/queue/source', dict(self.scope(), mode='replace'), timeout=4)
            return {'status': 'ok'}
        if path == 'random':
            items = self.page('random', 0, 1)['items']
            if not items:
                raise ValueError('No albums')
            path = items[0]['route']
        parts = path.split('/')
        if len(parts) != 3 or parts[0] != 'play':
            raise ValueError('Invalid play route')
        kind, key = parts[1], urllib.parse.unquote(parts[2])
        if kind == 'library-albums':
            json.loads(key)
            client.post('/api/queue/album', {'id': key, 'mode': 'replace'}, timeout=4)
            return {'status': 'ok'}
        if kind != 'library-songs':
            raise ValueError('Invalid play route')
        uri = client.uri(key)
        if context and time.monotonic() - context[2] < CONTEXT_SECONDS:
            index, route = context[0], context[1]
            if not uri and route:
                # After a daemon restart: the listed row names its URI again.
                self.page(route, index, 1)
                uri = client.uri(key)
            if uri:
                body = self.queue_body(route, uri, index)
                if body is not None:
                    endpoint, payload = body
                    client.post(endpoint, payload, timeout=4)
                    return {'status': 'ok', 'expected_ids': [key]}
        if not uri:
            raise ValueError('Selected item unavailable')
        client.post('/api/queue', {'uris': [uri], 'mode': 'replace'}, timeout=4)
        return {'status': 'ok', 'expected_ids': [key]}

    def queue_body(self, route, uri, index):
        """Queue a whole list from its row `index`, as Rufin's own Play does."""
        parsed = urllib.parse.urlsplit(route)
        name, args = parsed.path, dict(urllib.parse.parse_qsl(parsed.query))
        if name.startswith('root/'):
            name = ROOT.get(urllib.parse.unquote(name[5:]), 'empty')
        base = {'mode': 'replace', 'anchor_uri': uri, 'index': index}
        collection = self.collection(name)
        if collection:
            _, endpoint, params = collection
            payload = dict(base)
            for k, v in params.items():
                # Collection ids travel as their JSON text; a page token as its object.
                payload[k] = json.loads(v) if k == 'page' else v
            return endpoint, payload
        if name == 'songs' and set(args) <= {'format', 'title~'}:
            payload = dict(base, **self.scope())
            if args.get('title~'):
                payload['q'] = args['title~']
            return '/api/queue/source', payload
        return None
