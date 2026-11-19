"""Read-only Jellyfin library for the Rufin remote. Rufin owns playback."""
import hashlib
import http.client
import json
import logging
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from hrcider_cache import art_id
from hrcider_library import ROOT, route, row
from hrrufin import RufinLibrary, config_home

log = logging.getLogger('herdremote.jellyfin')

MAX_REPLY = 2 * 1024 * 1024
ITEM_FIELDS = 'Genres,ItemCounts,ChildCount'
GUID = re.compile(r'(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})')
TOKEN = re.compile(r'[A-Za-z0-9._~-]{1,512}')


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
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.timeout = timeout

    def request(self, session, path, image=False, **params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(
            session.base + '/' + path + ('?' + query if query else ''), method='GET',
            headers={'Authorization': f'MediaBrowser Token="{session.token}"',
                     'Accept': 'image/jpeg' if image else 'application/json'})
        deadline = time.monotonic() + 2 * self.timeout
        body = bytearray()
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
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
        self.page(session, 'Items', 0, 1, IncludeItemTypes='MusicAlbum',
                  Recursive='true', ParentId=session.folder, EnableImages='false')
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

    def __init__(self, client, jellyfin):
        super().__init__(client)
        self.jellyfin = jellyfin
        self.session = None
        self.art_origin = None
        self.online = None
        self.retry_at = 0.
        self.composers = None

    def refresh(self):
        with self.lock:
            now = time.monotonic()
            if now < self.retry_at:
                return self.session
            previous = self.session
            self.retry_at = now + 15
        try:
            session = self.jellyfin.connect(self.client, previous)
        except JellyfinError as error:
            self.failed(previous, error)
        else:
            with self.lock:
                # An overlapping page failure may have invalidated this probe.
                if self.session is previous and self.retry_at == now + 15:
                    if session is not previous:
                        self.generation += 1
                        self.composers = None
                    self.session = session
                    self.art_origin = (session.art_scope, session.source)
                    if self.online is not True:
                        log.info('Rufin library source=Jellyfin')
                    self.online = True
        return self.session

    def failed(self, session, error):
        with self.lock:
            if self.session is not session:
                return
            if session is not None:
                self.generation += 1
            self.session = None
            self.composers = None
            self.retry_at = time.monotonic() + 10
            if self.online is not False:
                log.info('Rufin library source=Rufin reason=%s', error)
            self.online = False

    def jellyfin_art(self, session, kind, item, tag):
        tag_key = hashlib.sha256(tag.encode()).hexdigest()[:24]
        template = f'jellyfin:{session.art_scope}:{kind}:{item}:{tag_key}'
        key = art_id(template)
        with self.lock:
            self.templates[key] = template
            self.templates.move_to_end(key)
            while len(self.templates) > 20000:
                self.templates.popitem(last=False)
        return key

    def artwork(self, template, size):
        """Bytes and whether they are the primary image named by the cache key."""
        _, scope, kind, item, _ = template.split(':')
        session = self.session
        if session and session.art_scope == scope:
            try:
                raw = self.jellyfin.request(session, f'Items/{item}/Images/Primary',
                                           image=True, MaxWidth=size, MaxHeight=size, Format='Jpg')
                return raw, True
            except JellyfinError as error:
                if error.status != 404:
                    self.failed(session, error)
        origin = self.art_origin
        if origin is None or origin[0] != scope:
            raise ValueError('Unknown Jellyfin artwork')
        return self.client.artwork(rufin_uri(origin[1], kind, item)), False

    def item_row(self, session, value, album_tracks=False, display=None):
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
            return row(title, route('albums', artistid=ident), 'artist', id=ident)
        raise JellyfinError('unexpected library item type')

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
        if name not in ('albums', 'artists', 'albumartists', 'genres', 'years', 'composers',
                        'ratings', 'songs', 'recent', 'search') and not name.startswith('album/t'):
            return super().page(query, offset, limit, stale)
        session = self.refresh()
        if session:
            try:
                value = self.read_page(session, name, args, offset, limit, stale)
                if value is not None and self.session is session:
                    return value
            except JellyfinError as error:
                self.failed(session, error)
        return super().page(query, offset, limit, stale)

    def read_page(self, session, name, args, offset, limit, stale):
        def result(items, total):
            return dict(generation=self.generation, offset=offset, total=total, stale=bool(stale), items=items)

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
            generation = self.generation
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
                    if self.session is session:
                        self.composers = cached
            items = cached[2]
            return result(items[offset:offset + limit], len(items))

        params = dict(ParentId=session.folder, SortBy='SortName', SortOrder='Ascending')
        album_id = self.album_id(session, name)
        if name.startswith('album/') and not album_id:
            return None
        if name in ('artists', 'albumartists', 'genres', 'years'):
            endpoint = {'artists': 'Artists', 'albumartists': 'Artists/AlbumArtists',
                        'genres': 'MusicGenres', 'years': 'Years'}[name]
            params.update(IncludeItemTypes='MusicAlbum' if name == 'years' else 'Audio,MusicAlbum',
                          EnableImages='false')
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
                    items.append(row(title, route('albums', **{arg: ident}), 'menu' if name == 'genres' else 'artist'))
            return result(items, total)

        params.update(Recursive='true', Fields=ITEM_FIELDS,
                      EnableImageTypes='Primary', ImageTypeLimit=1, EnableUserData='true')
        if name == 'search':
            term = args.get('q')
            if not term:
                return result([], 0)
            params.update(IncludeItemTypes='Audio,MusicAlbum,MusicArtist', SearchTerm=term)
        elif name == 'songs' or album_id:
            params['IncludeItemTypes'] = 'Audio'
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
        values, total = self.jellyfin.page(session, 'Items', offset, limit, **params)
        return result([self.item_row(session, v, bool(album_id), args.get('format')) for v in values], total)
