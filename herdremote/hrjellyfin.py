"""Read-only Jellyfin library for the Rufin remote. Rufin owns playback."""
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import http.client
import json
import logging
import os
import re
import stat
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from hrcider_cache import art_id
from hrcider_library import ROOT, route, row
from hrrufin import RufinLibrary, config_home, library_generation

log = logging.getLogger('herdremote.jellyfin')

MAX_REPLY = 2 * 1024 * 1024
ITEM_FIELDS = 'Genres,ItemCounts,ChildCount'
GUID = re.compile(r'(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})')
TOKEN = re.compile(r'[A-Za-z0-9._~-]{1,512}')
PROBE_INTERVAL = 15
PROBE_FAILURES = 3
CATALOG_BATCH = 200
CATALOG_QUERIES = 8
FILTERED_CATALOG_TTL = 60


class JellyfinError(OSError):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def id_or_none(value):
    return value if isinstance(value, str) and GUID.fullmatch(value) else None


def number(value):
    return value if type(value) is int and value > 0 else 0


def token_from(env):
    token = env.get('JELLYFIN_API_KEY', '').strip()
    if not token:
        path = config_home(env) / 'podbox-herdremote' / 'jellyfin-token'
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise JellyfinError('invalid token file permissions')
                raw = stream.read(4097)
        except FileNotFoundError:
            raise JellyfinError('token missing') from None
        except JellyfinError:
            raise
        except OSError:
            raise JellyfinError('token file unreadable') from None
        token = raw.decode('ascii', 'replace').strip() if len(raw) <= 4096 else ''
    if not TOKEN.fullmatch(token):
        raise JellyfinError('invalid token')
    return token


def rufin_config(env, source):
    """Read the matching provider URL from settings without using its credential."""
    try:
        with (config_home(env) / 'rufin' / 'settings.json').open('rb') as stream:
            settings = json.loads(stream.read(4 * 1024 * 1024))
        sources = settings['sources']['configured']
        entry = next(s for s in sources if s['kind'] == 'jellyfin' and s['source_id'] == source)
        payload = entry['provider_payload']
        return (json.loads(payload) if isinstance(payload, str) else payload)['base_url']
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        raise JellyfinError('Jellyfin source URL missing in Rufin settings') from None


def https_base(value):
    if not isinstance(value, str):
        raise JellyfinError('invalid HTTPS server URL')
    try:
        parts = urllib.parse.urlsplit(value)
        if (parts.scheme == 'https' and parts.hostname and parts.username is None
                and not parts.query and not parts.fragment and '\\' not in value
                and not any(c.isspace() or ord(c) < 32 for c in value)):
            _ = parts.port  # Validate before an authorization header is attached.
            return value.rstrip('/')
    except (TypeError, ValueError):
        pass
    raise JellyfinError('invalid HTTPS server URL')


def rufin_uri(source, kind, item):
    # Rufin's Rust percent_encoding::NON_ALPHANUMERIC escapes even -._~.
    def encode(value):
        return ''.join(chr(c) if c in b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
                       else f'%{c:02X}' for c in value.encode('utf-8'))
    return f'rufin:source/{kind}/{encode(source)}/{encode(f"jellyfin:{kind}:{item}")}'


@dataclass(frozen=True)
class Session:
    base: str
    source: str
    folder: str | None
    token: str = field(repr=False)
    user: str | None = field(default=None, repr=False)

    @property
    def art_scope(self):
        return hashlib.sha256(f'{self.base}\0{self.source}'.encode()).hexdigest()[:24]


class JellyfinClient:
    def __init__(self, env=None, opener=None, timeout=1.5):
        self.env = os.environ if env is None else env
        self.opener = opener
        self.timeout = timeout
        self.pool_lock = threading.Lock()
        self.idle = {}

    def close(self):
        with self.pool_lock:
            for connection in self.idle.values():
                connection.close()
            self.idle.clear()

    @contextmanager
    def open_reply(self, req):
        if self.opener is not None:
            with self.opener.open(req, timeout=self.timeout) as response:
                yield response
            return
        target = urllib.parse.urlsplit(req.full_url)
        https_base(urllib.parse.urlunsplit(target._replace(query='')))
        path = urllib.parse.urlunsplit(('', '', target.path, target.query, ''))
        origin = (target.hostname, target.port or 443)
        with self.pool_lock:
            connection = self.idle.pop(origin, None)
        reused = connection is not None
        if not reused:
            connection = http.client.HTTPSConnection(*origin, timeout=self.timeout)
        try:
            try:
                connection.request(req.get_method(), path, req.data, dict(req.header_items()))
                response = connection.getresponse()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                if not reused or req.get_method() != 'GET':
                    raise
                connection.close()
                connection.request(req.get_method(), path, req.data, dict(req.header_items()))
                response = connection.getresponse()
            with response:
                if not 200 <= response.status < 300:
                    # Never follow a redirect or return a server-supplied reason.
                    raise JellyfinError(f'HTTP {response.status}', response.status)
                yield response
                if response.isclosed() and connection.sock is not None:
                    with self.pool_lock:
                        if origin not in self.idle and len(self.idle) < 4:
                            self.idle[origin] = connection
                            connection = None
        finally:
            if connection is not None:
                connection.close()

    def request(self, session, path, image=False, **params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(
            session.base + '/' + path + ('?' + query if query else ''), method='GET',
            headers={'Authorization': f'MediaBrowser Token="{session.token}"',
                     'Accept': 'image/jpeg' if image else 'application/json'})
        return self.read_reply(req, image=image)

    def authenticate(self, base, username, password):
        # Jellyfin uses the client identity to associate logins with a device.
        device = hashlib.sha256(("AstraPod:" + socket.gethostname()).encode()).hexdigest()
        request = urllib.request.Request(
            https_base(base) + "/Users/AuthenticateByName",
            data=json.dumps({"Username": username, "Pw": password}).encode("utf-8"),
            headers={
                "Authorization": f'MediaBrowser Client="AstraPod", Device="Desktop", '
                                 f'DeviceId="{device}", Version="1.0"',
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST")
        return self.read_reply(request)

    def read_reply(self, req, image=False):
        deadline = time.monotonic() + 2 * self.timeout
        body = bytearray()
        try:
            with self.open_reply(req) as response:
                while time.monotonic() < deadline:
                    chunk = response.read1(min(65536, MAX_REPLY + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > MAX_REPLY:
                        raise JellyfinError('reply exceeds size limit')
                else:
                    raise JellyfinError('reply timed out')
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise JellyfinError(f'HTTP {status}', status) from None
        except JellyfinError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            # Exception text may contain a URL, proxy reply or server-supplied value.
            raise JellyfinError('server unavailable') from None
        if image:
            return bytes(body)
        try:
            result = json.loads(body)
            if isinstance(result, dict):
                return result
        except (ValueError, RecursionError):
            pass
        raise JellyfinError('malformed JSON reply')

    def connect(self, rufin, previous):
        token = token_from(self.env)
        try:
            data = rufin.get('/api/sources', timeout=self.timeout)
            source = data.get('selected')
            kind = next(s['kind'] for s in data.get('sources', []) if s.get('id') == source)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
            raise JellyfinError('Rufin source unavailable') from None
        if kind != 'jellyfin' or not isinstance(source, str) or not source:
            raise JellyfinError('selected Rufin source is not Jellyfin')
        folder = data.get('library')
        if isinstance(folder, str) and not id_or_none(folder):
            folder = folder.rsplit(':', 1)[-1]
        if folder and not id_or_none(folder):
            raise JellyfinError('invalid Rufin music folder')
        base = https_base(self.env.get('JELLYFIN_URL') or rufin_config(self.env, source))
        candidate = Session(base, source, folder or None, token)
        if previous and (base, token) == (previous.base, previous.token):
            user = previous.user
        else:
            try:
                user = id_or_none(self.request(candidate, 'Users/Me').get('Id'))
                if user is None:
                    raise JellyfinError('malformed user reply')
            except JellyfinError as error:
                # Jellyfin 10.10 returns 400 here for a valid app API key.
                if error.status != 400:
                    raise
                user = None
        session = Session(base, source, folder or None, token, user)
        return previous if session == previous else session

    def page(self, session, endpoint, offset, limit, **params):
        result = self.request(session, endpoint, StartIndex=offset, Limit=limit,
                              UserId=session.user, **params)
        values, total = result.get('Items'), result.get('TotalRecordCount')
        if (not isinstance(values, list) or len(values) > limit or type(total) is not int
                or not 0 <= total <= 1000000 or (values and total < offset + len(values))
                or (not values and offset < total)
                or any(not isinstance(v, dict) for v in values)):
            raise JellyfinError('malformed library page')
        return values, total


class JellyfinLibrary(RufinLibrary):
    """Use Jellyfin for supported lists, keeping Rufin's other native routes."""

    def __init__(self, client, jellyfin, cache=None):
        super().__init__(client)
        self.jellyfin = jellyfin
        self.cache = cache
        self.session = None
        self.art_origin = None
        self.online = None
        self.retry_at = 0.
        self.composers = None
        self.probe_lock = threading.Lock()
        self.probe_failures = 0
        self.album_cache = OrderedDict()
        self.fallback_albums = None
        self.art_aliases = {}

    @staticmethod
    def album_params(session):
        return dict(ParentId=session.folder, SortBy='SortName', SortOrder='Ascending',
                    Recursive='true', Fields=ITEM_FIELDS, EnableImageTypes='Primary',
                    ImageTypeLimit=1, EnableUserData='true', IncludeItemTypes='MusicAlbum')

    def catalog(self, session, params):
        items, ids = [], set()
        expected = None
        while True:
            values, total = self.jellyfin.page(session, 'Items', len(items), CATALOG_BATCH, **params)
            if expected is not None and total != expected:
                raise JellyfinError('library changed during catalogue scan')
            expected = total
            for value in values:
                ident = id_or_none(value.get('Id'))
                if not ident or value.get('Type') != 'MusicAlbum' or not isinstance(value.get('Name'), str):
                    raise JellyfinError('malformed library item')
                if ident in ids:
                    raise JellyfinError('library changed during catalogue scan')
                ids.add(ident)
            items.extend(values)
            if len(items) == total:
                return tuple(items)

    def refresh(self):
        # Cold readers join the first probe. Warm readers keep slicing the last
        # complete snapshot while the inventory worker checks for changes.
        with self.probe_lock:
            if time.monotonic() < self.retry_at:
                return
            previous = self.session
            try:
                session = self.jellyfin.connect(self.client, previous)
                params = self.album_params(session)
                albums = self.catalog(session, params)
                # Exclude per-user playback history, which changes on every play.
                signature = [{k: v for k, v in value.items() if k != 'UserData'} for value in albums]
                revision = library_generation('jellyfin-v1', session.art_scope, session.folder,
                                              session.user, signature)
            except JellyfinError as error:
                self.probe_failures += 1
                if previous is not None and self.probe_failures < PROBE_FAILURES:
                    self.retry_at = time.monotonic() + PROBE_INTERVAL
                    return
                self.refresh_fallback(error)
            else:
                changed = revision != self.generation or session is not previous
                if changed:
                    aliases = {}
                    for value in albums:
                        tags = value.get('ImageTags')
                        tag = tags.get('Primary') if isinstance(tags, dict) else None
                        if isinstance(tag, str) and tag:
                            ident = value['Id']
                            aliases[rufin_uri(session.source, 'album', ident)] = self.art_template(
                                session, 'album', ident, tag)
                    if self.cache is not None:
                        try:
                            self.cache.remember_rufin_album_art(session.source, session.art_scope, aliases)
                        except sqlite3.Error:
                            log.warning('Could not persist Rufin artwork aliases')
                with self.lock:
                    if changed:
                        self.album_cache.clear()
                        self.composers = None
                        self.art_aliases = aliases
                        for template in aliases.values():
                            self.register_art(template)
                    self.session = session
                    self.generation = revision
                    self.art_origin = (session.art_scope, session.source)
                    key = tuple(sorted(params.items()))
                    self.album_cache[key] = (float('inf'), albums)
                    self.album_cache.move_to_end(key)
                    self.fallback_albums = None
                    if self.online is not True:
                        log.info('Rufin library source=Jellyfin')
                    self.online = True
                self.probe_failures = 0
            self.retry_at = time.monotonic() + PROBE_INTERVAL

    def refresh_fallback(self, error):
        try:
            scope = self.scope(fresh=True)
            source = scope.get('source')
            if self.cache is not None and source and (not self.art_origin or source != self.art_origin[1]):
                try:
                    saved = self.cache.rufin_album_art(source)
                except (sqlite3.Error, ValueError):
                    saved = None
                if saved is not None:
                    art_scope, aliases = saved
                    with self.lock:
                        self.art_origin = (art_scope, source)
                        self.art_aliases = aliases
            scope, rows = self.native_album_catalogue(scope)
            revision = library_generation('rufin-v1', scope, rows)
        except (OSError, ValueError, TypeError):
            # Only publish a fallback revision after its catalogue is complete.
            if self.online is not None:
                return
            rows, revision = None, self.generation
        with self.lock:
            self.session = None
            self.album_cache.clear()
            self.composers = None
            if rows is not None or self.online is not False:
                self.fallback_albums = rows
            self.generation = revision
            if self.online is not False:
                log.info('Rufin library source=Rufin reason=%s', error)
            self.online = False

    def album_inventory(self):
        with self.lock:
            if self.session is not None:
                key = tuple(sorted(self.album_params(self.session).items()))
                return self.generation, len(self.album_cache[key][1]), {art_id(template) for template in self.art_aliases.values()}
            if self.fallback_albums is not None:
                rows = self.fallback_albums
                return self.generation, len(rows), self.album_art_keys(rows)
            return None

    def album_art_order(self):
        with self.lock:
            if self.session is not None:
                return tuple(art_id(template) for template in self.art_aliases.values())
            return tuple(row['art'] for row in self.fallback_albums or ()
                         if row.get('art') not in (None, '-'))

    def art(self, uri):
        # Rufin uses the same URI as Jellyfin for a known album or track.
        with self.lock:
            template = self.art_aliases.get(uri)
        return self.register_art(template) if template else super().art(uri)

    @staticmethod
    def art_template(session, kind, item, tag):
        tag_key = hashlib.sha256(tag.encode()).hexdigest()[:24]
        return f'jellyfin:{session.art_scope}:{kind}:{item}:{tag_key}'

    def jellyfin_art(self, session, kind, item, tag):
        return self.register_art(self.art_template(session, kind, item, tag))

    def artwork(self, template, size):
        """Bytes and whether they are the primary image named by the cache key."""
        _, scope, kind, item, _ = template.split(':')
        session = self.session
        if session and session.art_scope == scope:
            try:
                if kind.startswith('album-for-'):
                    field = 'AlbumArtistIds' if kind == 'album-for-albumartist' else 'ArtistIds'
                    rows, _ = self.jellyfin.page(
                        session, 'Items', 0, 1, ParentId=session.folder,
                        Recursive='true', IncludeItemTypes='MusicAlbum',
                        SortBy='ProductionYear,SortName', SortOrder='Ascending,Ascending',
                        **{field: item})
                    album = id_or_none(rows[0].get('Id')) if rows else None
                    if not album:
                        raise ValueError('No album for artist artwork')
                    return self.jellyfin.request(
                        session, f'Items/{album}/Images/Primary', image=True,
                        MaxWidth=size, MaxHeight=size, Format='Jpg'), True
                raw = self.jellyfin.request(session, f'Items/{item}/Images/Primary',
                                           image=True, MaxWidth=size, MaxHeight=size, Format='Jpg')
                return raw, True
            except JellyfinError:
                pass
        origin = self.art_origin
        if origin is None or origin[0] != scope:
            raise ValueError('Unknown Jellyfin artwork')
        return self.client.artwork(rufin_uri(origin[1], kind, item), size=size), False

    def item_row(self, session, value, album_tracks=False, display=None, generation=None):
        ident = id_or_none(value.get('Id'))
        title = value.get('Name')
        if not ident or not isinstance(title, str):
            raise JellyfinError('malformed library item')
        kind = value.get('Type')
        year = number(value.get('ProductionYear'))
        if not year:
            premiere = value.get('PremiereDate')
            if isinstance(premiere, str) and re.match(r'[0-9]{4}-', premiere):
                year = int(premiere[:4])
        ticks = number(value.get('RunTimeTicks'))
        ms = min(ticks // 10000, 2147483647)
        tags = value.get('ImageTags')
        tag = tags.get('Primary') if isinstance(tags, dict) else None
        if kind == 'MusicAlbum':
            uri = rufin_uri(session.source, 'album', ident)
            page = {'route': {'AlbumDetail': uri}, 'source': session.source}
            artwork = self.jellyfin_art(session, 'album', ident, tag) if isinstance(tag, str) and tag else '-'
            subtitle = [value.get('AlbumArtist')]
            genres = value.get('Genres')
            if display != 'panel' and isinstance(genres, list):
                subtitle.extend(genres)
            path = self.token_route('album', page)
            if display == 'panel':
                path += '?format=panel'
            return dict(row(title, path, 'album', id=ident),
                        art=artwork, subtitle=' · '.join(s for s in subtitle if isinstance(s, str) and s),
                        year=year, duration=ms, count=number(value.get('SongCount')) or number(value.get('ChildCount')))
        if kind == 'Audio':
            key = self.client.remember(rufin_uri(session.source, 'track', ident))
            artist = value.get('Artists')
            subtitle = ', '.join(s for s in artist if isinstance(s, str)) if isinstance(artist, list) else ''
            album_id = id_or_none(value.get('AlbumId'))
            album_tag = value.get('AlbumPrimaryImageTag')
            artwork = '-'
            if album_id and isinstance(album_tag, str) and album_tag:
                artwork = self.jellyfin_art(session, 'album', album_id, album_tag)
            elif isinstance(tag, str) and tag:
                artwork = self.jellyfin_art(session, 'track', ident, tag)
            if display != 'panel' and album_tracks and number(value.get('IndexNumber')):
                disc = number(value.get('ParentIndexNumber')) or 1
                title = f"{disc}.{value['IndexNumber']:02d} {title}"
            if display != 'panel' and (album_tracks or display == 'duration') and ticks:
                title += f' - {ms // 60000:02}:{ms // 1000 % 60:02}'
            if display == 'rating' and session.user:
                user_data = value.get('UserData')
                rating = user_data.get('Rating') if isinstance(user_data, dict) else None
                if type(rating) in (int, float) and 0 <= rating <= 100:
                    title += f' - Rating {rating:g}'
            return dict(row(title, 'play/library-songs/' + key, 'song', id=key),
                        art=artwork, subtitle=subtitle, duration=ms, year=year)
        if kind == 'MusicArtist':
            return self.artist_row(session, value, 'artistid', generation)
        raise JellyfinError('unexpected library item type')

    def artist_row(self, session, value, arg, generation):
        ident, title = value['Id'], value['Name']
        tags = value.get('ImageTags')
        tag = tags.get('Primary') if isinstance(tags, dict) else None
        role = 'albumartist' if arg == 'albumartistid' else 'artist'
        art = self.jellyfin_art(session, 'artist', ident, tag) if tag else self.jellyfin_art(
            session, 'album-for-' + role, ident, str(generation))
        return dict(row(title, route('albums', **{arg: ident}), 'artist', id=ident), art=art)

    def album_id(self, session, name):
        if not name.startswith('album/t'):
            return None
        with self.lock:
            token = self.tokens.get(name.split('/', 1)[1])
        if token:
            path = json.loads(token).get('route')
            uri = path.get('AlbumDetail') if isinstance(path, dict) else None
            prefix = rufin_uri(session.source, 'album', '')
            if isinstance(uri, str) and uri.startswith(prefix):
                return id_or_none(urllib.parse.unquote(uri[len(prefix):]))
        return None

    def page(self, query, offset, limit, stale=False):
        if len(query) > 380 or not 0 <= offset <= 1000000 or not 1 <= limit <= 50:
            raise ValueError('Invalid native library query')
        parsed = urllib.parse.urlsplit(query)
        name, args = parsed.path, dict(urllib.parse.parse_qsl(parsed.query))
        if name.startswith('root/'):
            name = ROOT.get(urllib.parse.unquote(name[5:]), 'empty')
        supported = (name in ('albums', 'artists', 'albumartists', 'genres', 'years', 'composers',
                              'ratings', 'songs', 'recent', 'search') or name.startswith('album/t'))
        if supported and self.online is None:
            self.refresh()
        with self.lock:
            session, generation = self.session, self.generation
            fallback = self.fallback_albums
        if session and supported:
            value = self.read_page(session, generation, name, args, offset, limit, stale)
            if value is not None:
                return value
        elif fallback is not None and name == 'albums' and set(args) <= {'format'}:
            return dict(generation=generation, offset=offset, total=len(fallback),
                        stale=bool(stale), items=fallback[offset:offset + limit])
        return dict(super().page(query, offset, limit, stale), generation=generation)

    def read_page(self, session, generation, name, args, offset, limit, stale):
        def result(items, total):
            return dict(generation=generation, offset=offset, total=total, stale=bool(stale), items=items)

        if name == 'ratings':
            if not session.user:
                return None
            items = [row('Favorites', 'songs?favorite=1'),
                     row('Liked', 'songs?rating=liked'),
                     row('Disliked', 'songs?rating=disliked'),
                     row('Track Ratings', 'songs?format=rating')]
            return result(items[offset:offset + limit], len(items))
        if name == 'composers':
            cached = self.composers
            if cached is None or cached[0] != generation or time.monotonic() > cached[1]:
                # 10.10's Persons endpoint has no StartIndex. Keep a bounded vocabulary.
                data = self.jellyfin.request(session, 'Persons', PersonTypes='Composer',
                                             UserId=session.user, EnableImages='false', Limit=4096)
                people = data.get('Items')
                if not isinstance(people, list) or len(people) > 4096:
                    raise JellyfinError('malformed composer list')
                items = [row(p['Name'], route('songs', composerid=p['Id']))
                         for p in people if isinstance(p, dict) and id_or_none(p.get('Id'))
                         and isinstance(p.get('Name'), str)]
                items.sort(key=lambda r: (r['title'].casefold(), r['route']))
                cached = (generation, time.monotonic() + 60, items)
                with self.lock:
                    if self.session is session and self.generation == generation:
                        self.composers = cached
            items = cached[2]
            return result(items[offset:offset + limit], len(items))

        params = self.album_params(session)
        album_id = self.album_id(session, name)
        if name.startswith('album/') and not album_id:
            return None
        if name in ('artists', 'albumartists', 'genres', 'years'):
            endpoint = {'artists': 'Artists', 'albumartists': 'Artists/AlbumArtists',
                        'genres': 'MusicGenres', 'years': 'Years'}[name]
            params = dict(ParentId=session.folder, SortBy='SortName', SortOrder='Ascending',
                          IncludeItemTypes='MusicAlbum' if name == 'years' else 'Audio,MusicAlbum',
                          EnableImages='true' if name in ('artists', 'albumartists') else 'false')
            term = args.get('artist~' if name == 'artists' else 'albumartist~')
            if term:
                params['SearchTerm'] = term
            values, total = self.jellyfin.page(session, endpoint, offset, limit, **params)
            items = []
            for v in values:
                ident, title = id_or_none(v.get('Id')), v.get('Name')
                if name == 'years':
                    if not isinstance(title, str) or not re.fullmatch(r'[0-9]{1,4}', title):
                        raise JellyfinError('malformed year')
                    items.append(row(title, route('albums', year=title)))
                else:
                    if not ident or not isinstance(title, str):
                        raise JellyfinError('malformed library item')
                    arg = {'artists': 'artistid', 'albumartists': 'albumartistid', 'genres': 'genreid'}[name]
                    items.append(row(title, route('albums', **{arg: ident})) if name == 'genres'
                                 else self.artist_row(session, v, arg, generation))
            return result(items, total)

        if name == 'search':
            params['SortBy'] = 'SortName'
            term = args.get('q')
            if not term:
                return result([], 0)
            params.update(IncludeItemTypes='Audio,MusicAlbum,MusicArtist', SearchTerm=term)
        elif name == 'songs' or album_id:
            params.update(IncludeItemTypes='Audio', SortBy='SortName')
            if album_id:
                params.update(ParentId=album_id, SortBy='ParentIndexNumber,IndexNumber,SortName')
            if args.get('title~'):
                params['SearchTerm'] = args['title~']
        else:
            params['IncludeItemTypes'] = 'MusicAlbum'
            if args.get('album~'):
                params['SearchTerm'] = args['album~']
            if name == 'recent':
                params.update(SortBy='DateCreated,SortName', SortOrder='Descending,Ascending')
        for arg, key in {'artistid': 'ArtistIds', 'albumartistid': 'AlbumArtistIds',
                         'genreid': 'GenreIds', 'composerid': 'PersonIds'}.items():
            if arg in args:
                ident = id_or_none(args[arg])
                if not ident:
                    return result([], 0)
                params[key] = ident
        if 'composerid' in args:
            params['PersonTypes'] = 'Composer'
        if 'year' in args:
            if not re.fullmatch(r'[0-9]{1,4}', args['year']):
                return result([], 0)
            params['Years'] = args['year']
        if args.get('favorite') == '1':
            if not session.user:
                return result([], 0)
            params['IsFavorite'] = 'true'
        if args.get('rating') in ('liked', 'disliked'):
            if not session.user:
                return result([], 0)
            params['Filters'] = 'Likes' if args['rating'] == 'liked' else 'Dislikes'
        if set(args) - {'album~', 'title~', 'format', 'q', 'artistid', 'albumartistid',
                        'genreid', 'composerid', 'year', 'favorite', 'rating'}:
            return None
        if params['IncludeItemTypes'] == 'MusicAlbum':
            key = tuple(sorted(params.items()))
            with self.lock:
                cached = None
                if self.session is session and self.generation == generation:
                    cached = self.album_cache.get(key)
                    if cached and cached[0] > time.monotonic():
                        self.album_cache.move_to_end(key)
            if cached and cached[0] > time.monotonic():
                values = cached[1]
            else:
                values = self.catalog(session, params)
                with self.lock:
                    if self.session is session and self.generation == generation:
                        self.album_cache[key] = (time.monotonic() + FILTERED_CATALOG_TTL, values)
                        self.album_cache.move_to_end(key)
                        base = tuple(sorted(self.album_params(session).items()))
                        self.album_cache.move_to_end(base)
                        if len(self.album_cache) > CATALOG_QUERIES:
                            self.album_cache.popitem(last=False)
            total = len(values)
            values = values[offset:offset + limit]
        else:
            values, total = self.jellyfin.page(session, 'Items', offset, limit, **params)
        return result([self.item_row(session, v, bool(album_id), args.get('format'), generation) for v in values], total)
