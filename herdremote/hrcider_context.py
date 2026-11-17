"""Read-only Cider Now Playing pages. Routes verified in Cider 4 app.asar.
Mutations are explicit controls, never page reads. No local music file access.
"""
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from decimal import Decimal
from hrlyrics import lyrics_rows, millis


def data(reply):
    return reply.get('data', reply) if isinstance(reply, dict) else reply


def text(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return str(value or '')


def lyric_sources(reply):
    value = data(reply)
    if isinstance(value, list):
        for item in value:
            yield from lyric_sources(item)
    elif isinstance(value, dict):
        for key in ('ttml', 'lyrics', 'text', 'content'):
            if value.get(key):
                yield from lyric_sources(value[key])
                return
        if isinstance(value.get('attributes'), dict):
            yield from lyric_sources(value['attributes'])
    elif isinstance(value, str):
        yield value


def ttml_time(value):
    """Apple lyrics use absolute media times, with optional hour fields."""
    if not value or len(value) > 20:
        return None
    clock = re.fullmatch(r'(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)(ms|s)?', value, re.ASCII)
    if not clock:
        return None
    hours, minutes, seconds, unit = clock.groups()
    if hours is not None and minutes is None:
        hours, minutes = None, hours
    if unit and minutes is not None:
        return None
    return int(Decimal(seconds) * (1 if unit == 'ms' else 1000)) + (
        int(hours or 0) * 3600 + int(minutes or 0) * 60) * 1000


def ttml_line(p):
    parts, cues = [], []
    pos = 0

    def append(text):
        nonlocal pos
        if text:
            parts.append(text)
            pos += len(text.encode('utf-8'))

    def inline(node, depth):
        first = pos
        append(node.text)
        count = len(cues)
        for child in node:
            # Excess nesting has no effect on timing and stays plain text.
            if depth < 16:
                inline(child, depth + 1)
            else:
                append(''.join(child.itertext()))
            append(child.tail)
        begin = ttml_time(node.get('begin'))
        if depth and begin is not None and pos > first and len(cues) == count:
            cues.append((begin, ttml_time(node.get('end')), first, pos))

    inline(p, 0)
    return ''.join(parts), ttml_time(p.get('begin')), ttml_time(p.get('end')), cues


def lyric_lines(reply):
    for source in lyric_sources(reply):
        if not source.lstrip().startswith('<'):
            yield from ((line, None, None, []) for line in source.splitlines())
            continue
        if '<!DOCTYPE' in source or '<!ENTITY' in source:
            continue
        try:
            tree = ET.fromstring(source)
        except ET.ParseError:
            continue
        for p in tree.iter():
            if p.tag.rsplit('}', 1)[-1] == 'p':
                yield ttml_line(p)


def lyrics_text(reply):
    return '\n'.join(line[0] for line in lyric_lines(reply))


def timed_lyrics(client, expected):
    from hrcider_library import row

    def track(info):
        return str((info.get('playParams') or {}).get('id') or info.get('id') or '')

    before = client.now_playing()
    if track(before) != expected:
        return None
    reply = client.request('/api/v1/lyrics/' + urllib.parse.quote(expected, safe=''))
    if track(client.now_playing()) != expected:
        return None
    return lyrics_rows(lyric_lines(reply), 0, millis(before.get('durationInMillis')) or 0, row)


def quality_text(quality):
    if not isinstance(quality, dict):return text(quality)
    parts = [quality.get('flavorLabel') or quality.get('flavor') or 'Unknown']
    config = quality.get('localPlayback') or quality.get('deviceAudioConfig') or {}
    if config.get('sampleRate'):parts.append(str(config['sampleRate'])+' Hz')
    if config.get('bitsPerSample'):parts.append(str(config['bitsPerSample'])+' bit')
    return ' / '.join(parts)


def page(library, name, offset, limit, stale):
    from hrcider_library import row, item_row, route
    client = library.client
    items = []
    if name == 'now/queue':
        position, total = client.queue_state()
        values = client.queue_slice(offset, limit)
        items = [dict(item_row(v), route='queue/'+str(offset+i), count=position) for i, v in enumerate(values)]
        return dict(generation=library.cache.generation, offset=offset, total=total, stale=stale, items=items)
    if name == 'now/time':
        position, total = client.queue_state()
        timing = data(client.request('/api/v2/playback/time'))
        elapsed = max(0, int(float(timing.get('currentTime', timing.get('currentPlaybackTime', 0)))*1000))
        remaining = 0
        for start in range(max(0, position), total, 50):
            for index, v in enumerate(client.queue_slice(start, min(50, total-start)), start):
                duration = max(0, int(v.get('attributes', {}).get('durationInMillis') or 0))
                remaining += max(0, duration-elapsed) if index == position else duration
        minutes, seconds = divmod(remaining//1000, 60)
        items = [row(f'{minutes}:{seconds:02}', '', id='remaining'), row(str(total-max(position, 0)), '', id='tracks')]
    else:
        info = client.now_playing()
        params = info.get('playParams') or {}
        track = str(params.get('id') or info.get('id') or '')
        if name == 'now/info':
            quality = data(client.request('/api/v2/playback/audio-quality'))
            status = data(client.request('/api/v2/library/now-playing/status'))
            rate = data(client.request('/api/v2/playback/rate'))
            values = [('title', info.get('name')), ('artist', info.get('artistName')), ('album', info.get('albumName')),
                      ('disc', info.get('discNumber')), ('track', info.get('trackNumber')), ('year', (info.get('releaseDate') or '')[:4]),
                      ('genre', ', '.join(info.get('genreNames') or [])), ('duration', info.get('durationInMillis')),
                      ('quality', quality_text(quality)), ('favorite', int(bool(info.get('inFavorites', status.get('rating') == 1)))),
                      ('library', int(bool(info.get('inLibrary', status.get('inLibrary'))))), ('speed', rate.get('rate', 1))]
            items = [row(str(v) if v is not None else '', '', id=k) for k, v in values]
        elif name == 'now/lyrics':
            if not re.fullmatch(r'[a-zA-Z0-9._-]{1,128}', track):
                raise ValueError('No lyrics id')
            lyrics = lyrics_text(client.request('/api/v1/lyrics/'+urllib.parse.quote(track, safe='')))
            # Bounded native rows, split without truncating long lines. Native transport
            # cleans control characters and limits each UTF-8 row to 120 bytes.
            for line in lyrics.splitlines():
                while line:
                    part = line.encode()[:116].decode('utf-8', 'ignore')
                    items.append(row(part, '', id=str(len(items))))
                    line = line[len(part):]
            if not items:
                items = [row('No lyrics', '')]
        elif name in ('now/album', 'now/artist'):
            # Resolve the album by current song membership before falling back to
            # a name filter. This avoids mixing different editions with one name.
            if name == 'now/album':
                songs = library._items('songs')
                song = next((v for v in songs if str(v['id']) == track), {})
                albums = song.get('albums') or []
                view = library.cache.album_view()
                logical = next((view.by_id[a] for a in albums if a in view.by_id), None)
                if logical is None:
                    logical = next((a for a in view.rows if any(t['id']==track for t in a.get('_tracks',[]))), None)
                if logical: albums = [logical['id']]
                target = 'album/'+urllib.parse.quote(str(albums[0]), safe='') if albums else route('albums', album=info.get('albumName', ''), artist=info.get('artistName', ''))
            else:
                target = route('albums', artist=info.get('artistName', ''))
            items = [row(info.get('albumName' if name.endswith('album') else 'artistName', ''), target)]
        else:
            raise ValueError('Unknown Now Playing page')
    return dict(generation=library.cache.generation, offset=offset, total=len(items), stale=stale, items=items[offset:offset+limit])
