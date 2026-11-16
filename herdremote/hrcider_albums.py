"""Conservative, deterministic library-album projection shared by every host view.

Raw Apple rows stay intact. A song's explicit album relationship can prove a
localized title alias; identical artwork alone is never such proof.
"""
import hashlib
import json
import re
import unicodedata


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def aliases(item):
    attrs = item.get('attributes', {})
    params = attrs.get('playParams', {}) or item.get('playParams', {}) or {}
    ids = {str(v) for v in (item.get('id'), params.get('id'), params.get('catalogId'), params.get('libraryId')) if v}
    ids.update(str(v) for v in item.get('catalog', []))
    ids.update(str(v['id']) for v in item.get('relationships', {}).get('catalog', {}).get('data', []))
    return ids


def resource(value):
    """Reconstitute indexed metadata; no arbitrary API paths or credentials."""
    attrs = dict(name=value['title'], artistName=value.get('artist', value.get('subtitle', '')),
                 albumArtistName=value.get('albumartist', ''), albumName=value.get('album', ''),
                 durationInMillis=value.get('duration', 0), trackCount=value.get('count', 0),
                 trackNumber=value.get('track', 0), discNumber=value.get('disc', 0),
                 releaseDate=value.get('releaseDate', ''), playParams=value.get('playParams', {}),
                 artwork=dict(url=value.get('artwork', ''), bgColor=value.get('bg', '181818')))
    return dict(id=value['id'], type=value.get('type') or 'library-'+value['kind'], attributes=attrs,
                relationships={'catalog': {'data': [{'id': v} for v in value.get('catalog', [])]},
                               'albums': {'data': [{'id': v} for v in value.get('albums', [])]}})


def ordered_tracks(items):
    """Union by library/song/catalog identity, including transitive aliases.

    Never deduplicate by title: an album can contain several 'City of Stars'.
    Prefer a library resource, then the smallest id, independently of input order.
    """
    parent = list(range(len(items)))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    seen = {}
    for i, item in enumerate(items):
        for key in aliases(item):
            if key in seen: parent[root(i)] = root(seen[key])
            seen[key] = i
    groups = {}
    for i, item in enumerate(items): groups.setdefault(root(i), []).append(item)
    def preference(item):
        return (not item.get('type', '').startswith('library-'), not bool(item.get('attributes', {}).get('playParams')), str(item['id']), json.dumps(item, sort_keys=True))
    chosen = [min(group, key=preference) for group in groups.values()]
    def order(item):
        a = item.get('attributes', {})
        return (int(a.get('discNumber') or 1), int(a.get('trackNumber') or 1000000), normalized(a.get('name')), str(item['id']))
    return sorted(chosen, key=order)


# These qualifiers are version evidence, not punctuation to strip from a title.
EDITION = re.compile(r'\b(?:deluxe|live|remaster(?:ed)?|acoustic|anniversary|expanded|instrumental|karaoke)\b|现场|現場|演唱会|演唱會|豪华|豪華', re.I)


def editions(title):
    translations={'现场':'live','現場':'live','演唱会':'live','演唱會':'live','豪华':'deluxe','豪華':'deluxe','remastered':'remaster'}
    return {translations.get(token,token) for token in EDITION.findall(normalized(title))}


def merge_reason(a, b, title_aliases):
    artist = normalized(a.get('albumartist') or a.get('subtitle'))
    if not artist or artist != normalized(b.get('albumartist') or b.get('subtitle')):
        return None
    ta, tb = normalized(a['title']), normalized(b['title'])
    if not ta or not tb: return None
    aa, ab = a.get('art', '-'), b.get('art', '-')
    if ta == tb and (aa == ab or aa == '-' or ab == '-'):
        # Conflicting covers may be distinct editions with an identical title.
        return 'title+artist+compatible-art'
    if editions(ta) != editions(tb): return None
    ca = set(a.get('catalog', [])) | {a.get('playParams', {}).get('catalogId', '')}
    cb = set(b.get('catalog', [])) | {b.get('playParams', {}).get('catalogId', '')}
    if (ca & cb) - {''}: return 'catalog+artist'
    if aa != '-' and aa == ab and (tb in title_aliases.get(a['id'], set()) or ta in title_aliases.get(b['id'], set())):
        return 'art+artist+relationship-title-alias'
    return None


class AlbumView:
    def __init__(self, albums, songs, overrides=(), signature=''):
        self.signature = signature
        self.raw = {a['id']: a for a in albums}
        self.songs = songs
        self.title_aliases = {a['id']: {normalized(a['title'])} for a in albums}
        for song in songs:
            for key in song.get('albums', []):
                album = self.raw.get(key)
                if album and normalized(song.get('albumartist')) == normalized(album.get('albumartist') or album.get('subtitle')):
                    title = normalized(song.get('album'))
                    if title: self.title_aliases[key].add(title)
        # Complete-link grouping: A~B and B~C cannot bridge conflicting A/C.
        groups = []
        for album in sorted(albums, key=lambda a: a['id']):
            group = next((g for g in groups if all(merge_reason(a, album, self.title_aliases) for a in g)), None)
            if group is None: groups.append([album])
            else: group.append(album)
        self.by_id, self.rows, self.groups = {}, [], {}
        overrides = dict(overrides)
        for group in groups:
            key = min(a['id'] for a in group)
            saved = overrides.get(key, {})
            counts = saved.get('member_counts', {}) if isinstance(saved,dict) else {}
            leader = min(group, key=lambda a: (-int(counts.get(a['id'],a.get('count')) or 0), a['id']))
            value = dict(leader, id=key, members=sorted(a['id'] for a in group))
            self.groups[key] = group
            for member in group: self.by_id[member['id']] = value
            self.rows.append(value)
        for value in self.rows:
            if len(value['members']) > 1:
                saved = overrides.get(value['id'])
                tracks = saved.get('tracks', []) if isinstance(saved,dict) else saved if saved is not None else self.indexed_tracks(value)
                value['count'] = len(tracks)
                value['duration'] = sum(int(t.get('attributes', {}).get('durationInMillis') or 0) for t in tracks)
                value['_tracks'] = tracks
                value['_complete'] = value['id'] in overrides

    def indexed_tracks(self, album, orphans_only=False):
        ids = set(album['members'])
        group = self.groups[album['id']]
        titles = set().union(*(self.title_aliases[a['id']] for a in group))
        arts = {a.get('art') for a in group} - {'-', None, ''}
        items = []
        for song in self.songs:
            refs = set(song.get('albums', []))
            direct = bool(ids & refs)
            # Apple can leave songs pointing at a no-longer-listed album id.
            # Require both title and artwork, and never steal another indexed album.
            orphan = not (refs & self.raw.keys()) and normalized(song.get('album')) in titles and song.get('art') in arts
            if orphan or (direct and not orphans_only): items.append(resource(song))
        return ordered_tracks(items)

    def find_resource(self, item, art_key):
        if item.get('type', '').startswith('library-') and item['id'] in self.by_id:
            return self.by_id[item['id']]
        attrs = item.get('attributes', {})
        candidate = dict(id=str(item['id']), title=attrs.get('name', ''), subtitle=attrs.get('artistName', ''),
                         albumartist=attrs.get('albumArtistName') or attrs.get('artistName', ''), art=art_key,
                         catalog=[v['id'] for v in item.get('relationships', {}).get('catalog', {}).get('data', [])],
                         playParams=attrs.get('playParams') or {})
        if not item.get('type', '').startswith('library-'): candidate['catalog'].append(item['id'])
        # Recently played can return catalog metadata in another locale. An
        # already indexed, uniquely owned catalog identity resolves that row
        # without depending on its translated artist/title. This does not join
        # library groups, and ambiguous catalog ownership is never guessed.
        wanted=(set(candidate['catalog']) | {candidate['playParams'].get('catalogId','')}) - {''}
        owners=[row for row in self.rows if any(
            wanted & (set(a.get('catalog',[])) | {a.get('playParams',{}).get('catalogId','')})
            for a in self.groups[row['id']])]
        if len(owners)==1:return owners[0]
        for row in self.rows:
            if all(merge_reason(candidate, member, self.title_aliases) for member in self.groups[row['id']]): return row
        return None


def source_signature(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
