"""Offline Jellyfin 10.10 REST and Rufin playback integration."""
import contextlib
import http.client
import io
import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hrcider import AAT, Bridge, SIZES, native_page_frame
from hrcider_cache import Cache
from hrjellyfin import JellyfinClient, JellyfinError, JellyfinLibrary, MAX_REPLY, NoRedirect, Session, token_from
from hrrufin import RufinClient, rufin_id

KEY = 'fixture-jellyfin-secret'
SOURCE = 'podbox-音源'
FOLDER = 'f' * 32
ALBUM = 'a' * 32
TRACK = '1' * 32
USER = '2' * 32
ARTIST = '3' * 32
GENRE = '4' * 32
COMPOSER = '5' * 32
BASE = 'https://mock-jellyfin.invalid/music'
SOURCE_PART = 'podbox%2D%E9%9F%B3%E6%BA%90'
TRACK_URI = f'rufin:source/track/{SOURCE_PART}/jellyfin%3Atrack%3A{TRACK}'
ALBUM_URI = f'rufin:source/album/{SOURCE_PART}/jellyfin%3Aalbum%3A{ALBUM}'
OPEN = urllib.request.OpenerDirector.open


class Loopback:
    """Exercise urllib's redirect handling on an ephemeral mock server only."""
    def __init__(self, source, local):
        self.source = source
        self.local = local
        assert urllib.parse.urlsplit(local).hostname == '127.0.0.1'
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def open(self, req, timeout):
        assert req.full_url.startswith(self.source + '/')
        url = self.local + req.full_url[len(self.source):]
        local = urllib.request.Request(url, data=req.data, headers=dict(req.header_items()),
                                       method=req.get_method())
        # test_usage guards global urllib calls. Every target here is loopback.
        return OPEN(self.http, local, timeout=timeout)


@contextlib.contextmanager
def server(respond, protocol="HTTP/1.0"):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = protocol
        def request(self):
            target = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(length)) if length else None
            call = (self.command, target.path, dict(urllib.parse.parse_qsl(target.query)),
                    self.headers.get('Authorization'), body)
            self.server.calls.append(call)
            self.server.peers.append(self.client_address)
            status, reply, headers = respond(call)
            raw = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST = request

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as httpd:
        httpd.calls = []
        httpd.peers = []
        thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': .02})
        thread.start()
        try:
            yield httpd, f'http://127.0.0.1:{httpd.server_port}'
        finally:
            httpd.shutdown()
            thread.join(2)


@pytest.fixture
def setup(tmp_path):
    def album(key, title):
        return {'Type': 'MusicAlbum', 'Id': key, 'Name': title, 'ProductionYear': 2024,
                'AlbumArtist': 'Album Artist', 'Genres': ['Jazz'], 'SongCount': 2,
                'RunTimeTicks': 2410000000, 'ImageTags': {'Primary': 'cover-v1'}}

    albums = [album(ALBUM, 'First'), album('b' * 32, 'Second'), album('c' * 32, 'Third')]
    track = {'Type': 'Audio', 'Id': TRACK, 'Name': 'Track', 'RunTimeTicks': 1210000000,
             'Artists': ['Artist'], 'ParentIndexNumber': 2, 'IndexNumber': 3,
             'AlbumId': ALBUM, 'AlbumPrimaryImageTag': 'cover-v1', 'UserData': {'Rating': 8.5}}
    state = {'failure': 0, 'user': False, 'size': None, 'selected': SOURCE, 'albums': albums}
    env = {'XDG_CONFIG_HOME': str(tmp_path)}
    rufin_home = tmp_path / 'rufin'
    rufin_home.mkdir()
    (rufin_home / 'settings.json').write_text(json.dumps({'sources': {'configured': [{
        'source_id': SOURCE, 'kind': 'jellyfin', 'credential': 'unused-age-ciphertext',
        'provider_payload': json.dumps({'base_url': BASE})}]}}))
    token = tmp_path / 'podbox-herdremote/jellyfin-token'
    token.parent.mkdir()
    token.write_text(KEY + '\n')
    token.chmod(0o600)

    def jellyfin(call):
        method, path, query, auth, _ = call
        assert method == 'GET' and auth == f'MediaBrowser Token="{KEY}"'
        if path.endswith('/Users/Me'):
            return (200, {'Id': USER}, {}) if state['user'] else (400, {}, {})
        if state['failure'] == 302:
            return 302, {}, {'Location': state['redirect']}
        if state['failure']:
            return state['failure'], {'message': f'Server echoed {KEY}'}, {}
        if path.endswith('/Images/Primary'):
            size = state['size'] or int(query['MaxWidth'])
            stream = io.BytesIO()
            Image.new('RGB', (size, size), 'blue').save(stream, 'JPEG')
            return 200, stream.getvalue(), {}
        assert not state['user'] or query['UserId'] == USER
        if path.endswith('/Persons'):
            assert query['PersonTypes'] == 'Composer'
            return 200, {'Items': [{'Id': COMPOSER, 'Name': 'Composer'}]}, {}
        collections = {'Artists': (ARTIST, 'Artist'), 'AlbumArtists': (ARTIST, 'Album Artist'),
                       'MusicGenres': (GENRE, 'Jazz'), 'Years': (USER, '2024')}
        key = path.rsplit('/', 1)[1]
        if key in collections:
            ident, title = collections[key]
            values = [{'Id': ident, 'Name': title}]
        elif query.get('IncludeItemTypes') == 'Audio':
            values = [track]
        else:
            values = albums
        offset, limit = int(query['StartIndex']), int(query['Limit'])
        return 200, {'Items': values[offset:offset + limit], 'TotalRecordCount': len(values)}, {}

    def rufin(call):
        method, path, query, auth, body = call
        assert auth == 'Bearer mock-rufin'
        if path == '/api/sources':
            return 200, {'selected': state['selected'], 'library': 'jellyfin:folder:' + FOLDER,
                         'sources': [{'id': SOURCE, 'kind': 'jellyfin'},
                                     {'id': 'other', 'kind': 'local'}]}, {}
        if path == '/api/albums':
            return 200, {'albums': [{'kind': 'album', 'id': 7, 'title': 'Rufin fallback', 'uri': ALBUM_URI}], 'total': 1}, {}
        if path == '/api/page':
            return 200, {'tracks': [{'kind': 'track', 'uri': TRACK_URI, 'title': 'Rufin track'}],
                         'total': 1}, {}
        if path == '/api/artwork':
            stream = io.BytesIO()
            Image.new('RGB', (240, 240), 'red').save(stream, 'PNG')
            return 200, stream.getvalue(), {}
        if path == '/api/playback':
            return 200, {'current': {'track': {'uri': TRACK_URI, 'title': 'Rufin Now Playing',
                                              'links': {'album': [{'name': 'First', 'page': {
                                                  'route': {'AlbumDetail': ALBUM_URI}, 'source': SOURCE}}]}}},
                         'state': 'playing'}, {}
        if path == '/api/queue' and method == 'POST':
            return 202, {}, {}
        return 404, {}, {}

    with server(jellyfin) as (jf, jbase), server(rufin) as (rf, rbase):
        client = RufinClient(env={'RUFIN_TOKEN': 'mock-rufin', **env}, opener=Loopback(rbase, rbase))
        client.base, client.token, client.ready = rbase, 'mock-rufin', True
        bridge = Bridge(client=Mock(), cache=Cache(tmp_path / 'art'), volume=Mock(),
                        rufin=client, jellyfin=JellyfinClient(env, opener=Loopback(BASE, jbase + '/music')))
        bridge._select_remote(0)
        state['redirect'] = rbase + '/token-sink'
        try:
            yield bridge, jf, rf, state, token
        finally:
            bridge.close()


def test_metadata_paging_and_rufin_queue(setup):
    bridge, jf, rf, _, _ = setup
    library = bridge.library
    page = library.page('root/Album', 0, 1)
    assert page['total'] == 3
    first, = page['items']
    assert (first['title'], first['subtitle'], first['year'], first['duration'], first['count']) == (
        'First', 'Album Artist · Jazz', 2024, 241000, 2)
    calls = len(jf.calls)
    later = library.page('albums', 1, 2)
    assert later['offset'] == 1 and [r['title'] for r in later['items']] == ['Second', 'Third']
    assert len(jf.calls) == calls and jf.calls[-1][2]['StartIndex'] == '0'
    assert jf.calls[-1][2]['ParentId'] == FOLDER
    album_page = library.page(first['route'], 0, 50)
    song, = album_page['items']
    assert song['title'] == '2.03 Track - 02:01' and song['duration'] == 121000
    assert song['art'] == first['art']
    assert 'ParentIndexNumber,IndexNumber,SortName' == jf.calls[-1][2]['SortBy']
    assert jf.calls[-1][2]['ParentId'] == ALBUM
    assert TRACK_URI == bridge.rufin.uri(song['id'])
    assert b'2.03 Track - 02:01' in native_page_frame(1, 2, album_page)
    library.set_context('0|' + first['route'])
    assert library.play(song['route'])['expected_ids'] == [rufin_id(TRACK_URI)]
    assert rf.calls[-1] == ('POST', '/api/queue', {}, 'Bearer mock-rufin', {
        'mode': 'replace', 'anchor_uri': TRACK_URI, 'index': 0,
        'page': {'route': {'AlbumDetail': ALBUM_URI}, 'source': SOURCE}})
    assert library.play(song['route'])['expected_ids'] == [song['id']]
    assert rf.calls[-1][-1] == {'uris': [TRACK_URI], 'mode': 'replace'}
    bridge.rufin.snapshot()
    assert library.page('now/info', 0, 20)['items'][0]['title'] == 'Rufin Now Playing'
    now_album = library.page('now/album', 0, 1)['items'][0]['route']
    assert library.page(now_album, 0, 1)['items'][0]['id'] == song['id']
    assert all(call[0] == 'GET' for call in jf.calls)


def test_categories_favorites_and_ratings(setup):
    bridge, jf, _, state, _ = setup
    state['user'] = True
    library = bridge.library
    checks = {
        'artists': ('Artist', 'ArtistIds', ARTIST),
        'albumartists': ('Album Artist', 'AlbumArtistIds', ARTIST),
        'genres': ('Jazz', 'GenreIds', GENRE),
        'years': ('2024', 'Years', '2024'),
        'composers': ('Composer', 'PersonIds', COMPOSER),
    }
    for category, (title, param, value) in checks.items():
        item, = library.page(category, 0, 1)['items']
        assert item['title'] == title
        assert library.page(item['route'], 0, 2)['items']
        assert jf.calls[-1][2][param] == value
    rows = library.page('root/User%20Rating', 0, 50)['items']
    assert [r['title'] for r in rows] == ['Favorites', 'Liked', 'Disliked', 'Track Ratings']
    for item, param, value in zip(rows, ['IsFavorite', 'Filters', 'Filters'], ['true', 'Likes', 'Dislikes']):
        assert library.page(item['route'], 0, 1)['items'][0]['id'] == rufin_id(TRACK_URI)
        assert jf.calls[-1][2][param] == value
        assert jf.calls[-1][2]['UserId'] == USER
    assert library.page(rows[-1]['route'], 0, 1)['items'][0]['title'] == 'Track - Rating 8.5'


def test_art_geometry_cache_and_fallback(setup):
    bridge, jf, rf, state, _ = setup
    key = bridge.library.page('albums', 0, 1)['items'][0]['art']
    prior = len(jf.calls)
    for tier in ('XS', 'S', 'M', 'L'):
        dimension = SIZES[tier]
        raw = bridge.artwork(key, tier)
        assert raw[:12] == AAT.pack(0x5441, 8, dimension, dimension, int(tier == 'M'), 0)
        assert len(raw) == 12 + 2 * dimension ** 2
        assert bridge.artwork(key, tier) == raw
    assert len(jf.calls) == prior + 1
    call = jf.calls[-1]
    assert call[1] == f'/music/Items/{ALBUM}/Images/Primary'
    assert call[2]['MaxWidth'] == call[2]['MaxHeight'] == '240'
    assert bridge.cache.get_art(key, 'original')
    state['failure'] = 404
    with bridge.cache.db:
        bridge.cache.db.execute("DELETE FROM art WHERE key=? AND tier IN ('M','original')", (key,))
    assert len(bridge.artwork(key, 'M')) == 12 + 2 * 128 ** 2
    assert rf.calls[-1][1] == '/api/artwork' and rf.calls[-1][2]['uri'] == ALBUM_URI
    assert bridge.cache.get_art(key, 'M') is None and bridge.library.online


def test_missing_key_then_recovery_and_401_fallback(setup, caplog, tmp_path):
    bridge, jf, rf, state, token = setup
    library = bridge.library
    token.unlink()
    with caplog.at_level('INFO', logger='herdremote.jellyfin'):
        for _ in range(2):
            assert library.page('albums', 0, 1)['items'][0]['title'] == 'Rufin fallback'
        assert not jf.calls
        token.write_text(KEY)
        token.chmod(0o600)
        probe(library)
        album = library.page('albums', 0, 1)
        assert album['items'][0]['title'] == 'First'
        assert library.page('ratings', 0, 3)['items'] == []  # app key has no user
        state['failure'] = 401
        for _ in range(2):
            probe(library)
            assert library.page('albums', 0, 1) == album
        probe(library)
        fallback = library.page('albums', 0, 1)
        assert fallback['items'][0]['title'] == 'Rufin fallback'
        assert fallback['items'][0]['art'] == album['items'][0]['art']
        assert fallback['generation'] != album['generation']
        assert library.page(album['items'][0]['route'], 0, 1)['items'][0]['title'] == 'Rufin track'
    assert [record.message for record in caplog.records] == [
        'Rufin library source=Rufin reason=token missing',
        'Rufin library source=Jellyfin',
        'Rufin library source=Rufin reason=HTTP 401',
    ]
    assert KEY not in repr(library) and KEY not in caplog.text
    assert all(KEY.encode() not in p.read_bytes() for p in (tmp_path / 'art').iterdir() if p.is_file())
    assert all(KEY not in str(call) for call in rf.calls)
    assert all(KEY not in str(call[2]) for call in jf.calls)


def test_redirect_and_oversize_are_rejected(setup):
    bridge, jf, rf, state, _ = setup
    library = bridge.library
    state['failure'] = 302
    assert library.page('albums', 0, 1)['items'][0]['title'] == 'Rufin fallback'
    assert not any(call[1] == '/token-sink' for call in rf.calls)
    request = library.jellyfin.request
    def oversize(call):
        return 200, b'x' * (MAX_REPLY + 1), {}
    with server(oversize) as (_, endpoint):
        original = library.jellyfin.opener
        library.jellyfin.opener = Loopback(BASE, endpoint)
        from hrjellyfin import JellyfinError, Session
        with pytest.raises(JellyfinError, match='size limit'):
            request(Session(BASE, SOURCE, None, KEY), 'Items')
        library.jellyfin.opener = original


def test_unreachable_server_recovers_without_switching_playback(setup):
    bridge, _, _, _, _ = setup
    library = bridge.library
    assert library.page('albums', 0, 1)['items'][0]['title'] == 'First'
    opener = library.jellyfin.opener
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(library.jellyfin, 'opener',
                      Mock(open=Mock(side_effect=ConnectionRefusedError(KEY))))
        probe(library)
        assert library.page('albums', 0, 1)['items'][0]['title'] == 'First'
        assert bridge.client is bridge.rufin
    assert library.jellyfin.opener is opener
    original = library.generation
    probe(library)
    assert library.generation == original
    assert library.probe_failures == 0
    assert library.page('albums', 0, 1)['items'][0]['title'] == 'First'


def probe(library):
    library.retry_at = 0
    library.refresh()


def test_whole_catalogue_shared_by_inventory_and_page_formats(setup, monkeypatch):
    bridge, jf, _, _, _ = setup
    monkeypatch.setattr('hrjellyfin.CATALOG_BATCH', 2)
    library = bridge.library
    later = library.page('albums?format=panel', 1, 2)
    assert [row['title'] for row in later['items']] == ['Second', 'Third']
    assert [call[2]['StartIndex'] for call in jf.calls if call[1].endswith('/Items')] == ['0', '2']
    calls = len(jf.calls)
    page = library.page('root/Album', 0, 2)
    assert page['items'][0]['subtitle'] == 'Album Artist · Jazz'
    assert library.page('albums', 9, 2)['items'] == []
    generation, count, keys = library.album_inventory()
    assert generation == page['generation'] and count == len(keys) == 3
    assert len(jf.calls) == calls
    library.page('albums?year=2024', 0, 1)
    assert len(jf.calls) == calls + 2
    library.page('albums?format=panel&year=2024', 2, 1)
    assert len(jf.calls) == calls + 2


def test_catalogue_content_generation_survives_restart_and_recovers(setup):
    bridge, _, _, state, _ = setup
    library = bridge.library
    original = library.page('albums', 0, 1)
    restarted = JellyfinLibrary(bridge.rufin, library.jellyfin)
    assert restarted.page('albums', 0, 1) == original
    state['albums'][0]['UserData'] = {'PlayCount': 37}
    probe(restarted)
    assert restarted.generation == original['generation']
    state['albums'][0]['Name'] = 'Changed'
    probe(restarted)
    updated = restarted.page('albums', 0, 1)
    assert updated['generation'] != original['generation']
    assert updated['items'][0]['title'] == 'Changed'
    assert JellyfinLibrary(bridge.rufin, library.jellyfin).page('albums', 0, 1) == updated
    state['failure'] = 503
    for _ in range(3):
        probe(restarted)
    assert restarted.generation != updated['generation']
    state['failure'] = 0
    probe(restarted)
    assert restarted.page('albums', 0, 1) == updated


def test_fallback_album_art_and_revision_survive_daemon_restart(setup):
    bridge, _, _, state, _ = setup
    library = bridge.library
    original = library.page('albums', 0, 1)
    key = original['items'][0]['art']
    state['failure'] = 503
    for _ in range(3):
        probe(library)
    fallback = library.page('albums', 0, 1)
    assert fallback['items'][0]['art'] == key
    reopened_cache = Cache(bridge.cache.root)
    try:
        reopened = JellyfinLibrary(bridge.rufin, library.jellyfin, reopened_cache)
        assert reopened.page('albums', 0, 1) == fallback
        assert reopened.template(key) == library.template(key)
        assert reopened.artwork(reopened.template(key), 240)[1] is False
    finally:
        reopened_cache.close()


def test_https_connection_reuse_and_no_redirect(monkeypatch):
    def respond(call):
        if call[1] == '/redirect':
            return 302, {}, {'Location': '/sink'}
        return 200, {'ok': True}, {}

    with server(respond, 'HTTP/1.1') as (httpd, endpoint):
        # Substitute only the TLS constructor so we can test actual HTTP/1.1
        # socket reuse on a local server without relaxing production TLS.
        monkeypatch.setattr('hrjellyfin.http.client.HTTPSConnection', http.client.HTTPConnection)
        client = JellyfinClient()
        session = Session(endpoint.replace('http://', 'https://'), SOURCE, None, KEY)
        try:
            assert client.request(session, 'one') == {'ok': True}
            assert client.request(session, 'two') == {'ok': True}
            assert httpd.peers[0] == httpd.peers[1]
            with pytest.raises(JellyfinError, match='HTTP 302'):
                client.request(session, 'redirect')
            assert client.request(session, 'three') == {'ok': True}
            assert httpd.peers[3] != httpd.peers[1]
            assert [call[1] for call in httpd.calls] == ['/one', '/two', '/redirect', '/three']
        finally:
            client.close()


def test_old_generation_pages_dropped_before_lower_cg(setup, monkeypatch):
    bridge, _, _, _, _ = setup
    library = bridge.library
    original = library.page('albums', 0, 1)
    assert original['generation'] > 7
    bridge.command(b'ci\t1\t65535')
    bridge.available = True
    blocked, resume = threading.Event(), threading.Event()
    page = library.page

    def slow(query, offset, limit, stale):
        if offset == 1:
            blocked.set()
            assert resume.wait(2)
            return dict(original, offset=offset)
        return page(query, offset, limit, stale)

    monkeypatch.setattr(library, 'page', slow)
    worker = threading.Thread(target=bridge._pages)
    bridge.threads.append(worker)
    worker.start()
    frames = []
    send = lambda _, raw: frames.append(raw)
    gen = bridge.generation
    try:
        bridge.poll(send)
        assert bridge.command(f'cq\t{gen}\t10\t0\t1\talbums'.encode())
        assert bridge.command(f'cq\t{gen}\t11\t1\t1\talbums'.encode())
        assert blocked.wait(2)  # 10 is queued with zero credit; 11 is still computing.
        assert any(frame[5].startswith(f'CL\t{gen}\t10\t'.encode()) for frame in bridge.scheduler.heap)
        library.generation = 7
        bridge.poll(send)
        assert frames[-1] == f'CG\t{gen}\t7'.encode()
        assert bridge.command(f'cq\t{gen}\t12\t0\t1\talbums'.encode())
    finally:
        resume.set()
    deadline = time.monotonic() + 2
    while not frames[-1].startswith(f'CL\t{gen}\t12\t7\t'.encode()) and time.monotonic() < deadline:
        bridge.scheduler.grant(65535)
        bridge.poll(send)
        time.sleep(.002)
    assert [raw.split(b'\t')[:4] for raw in frames if raw.startswith(b'CL\t')] == [
        [b'CL', str(gen).encode(), b'12', b'7']]


def test_token_permissions_and_env_precedence(setup):
    bridge, _, _, _, token = setup
    env = bridge.library.jellyfin.env
    token.chmod(0o640)
    from hrjellyfin import JellyfinError, https_base
    with pytest.raises(JellyfinError, match='permissions'):
        token_from(env)
    assert token_from(dict(env, JELLYFIN_API_KEY='env-key')) == 'env-key'
    for url in ('http://localhost', 'https://user@host', 'https://host?api_key=secret', 'https://x:NaN', 42):
        with pytest.raises(JellyfinError, match='invalid HTTPS'):
            https_base(url)
