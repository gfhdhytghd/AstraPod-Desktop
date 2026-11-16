"""Paged native Music tree over the independent Cider library index.

Routes contain bounded, escaped filter values, never arbitrary Apple API paths.
Only this worker calls the read-only Apple endpoints. Playback stays explicit.
"""
import collections
import json
import logging
import random
import re
import time
import urllib.parse
from hrcider_cache import art_id
from hrcider_featured import featured_guests
from hrcider_albums import normalized, ordered_tracks, resource

log = logging.getLogger('herdremote.cider')

LIST_BEFORE, LIST_AFTER = 5, 20  # queue window around a song of a plain list


class LibraryReadError(OSError):
    def __init__(self, reason, items):
        # `reason` comes exclusively from page_error(), never a server body.
        super().__init__(reason)
        self.reason, self.items = reason, items


def page_error(error):
    if isinstance(error, LibraryReadError): return error.reason
    if getattr(error, 'plugin_reason', None):
        return 'Cider plugin: ' + error.plugin_reason
    code=getattr(error,'code',None)
    reason='HTTP '+str(code) if isinstance(code,int) else type(error).__name__
    if getattr(error,'cider_message','') == 'Network connection unavailable':
        reason += ': Network connection unavailable'
    return reason

ROOT = {'Search':'search', 'Album':'albums', 'Random Album':'random',
        'Album Artist':'albumartists', 'Artist':'artists', 'Genre':'genres',
        'Year':'years', 'Composer':'composers', 'Featured Artists':'featured',
        'First Letter':'letters', 'Tracks by':'tracks', 'Shuffle Songs':'shuffle',
        'Search by...':'searchby', 'User Rating':'ratings',
        'Recently Added':'recent', 'Playback History':'history',
        'Same as currently played track':'same', 'Custom menu':'empty',
        'Audiobooks':'books', 'Podcasts':'podcasts', 'Quiz':'empty'}


def route(path, **filters):
    return path + ('?' + urllib.parse.urlencode(filters) if filters else '')


def row(title, path, kind='menu', **extra):
    return dict(id=extra.pop('id', path), title=title, subtitle='', art='-',
                duration=0, count=0, kind=kind, route=path, bg='181818', year=0, **extra)


def item_row(item, duration=False):
    attrs = item.get('attributes', {})
    kind = item.get('type', 'library-songs')
    name = attrs.get('name', '')
    ms = int(attrs.get('durationInMillis', 0) or 0)
    if duration:
        name += f' - {ms//60000:02}:{ms//1000%60:02}'
    key = str(item['id'])
    if 'albums' in kind:
        catalog=re.match(r'^/v1/catalog/([a-zA-Z-]+)/albums/',item.get('href',''))
        prefix='catalog-'+catalog[1]+'/' if catalog else ''
        path, view = 'album/'+prefix+urllib.parse.quote(key, safe=''), 'album'
    elif 'playlists' in kind:
        catalog=re.match(r'^/v1/catalog/([a-zA-Z-]+)/playlists/',item.get('href',''))
        prefix='catalog-'+catalog[1]+'/' if catalog else ''
        path, view = 'playlist/'+prefix+urllib.parse.quote(key, safe=''), 'playlist'
    elif 'artists' in kind:
        path, view = route('albums', artist=attrs.get('name', '')), 'artist'
    else:
        path, view = 'play/'+kind+'/'+urllib.parse.quote(key, safe=''), 'song'
    return dict(id=key, title=name, subtitle=attrs.get('artistName', ''),
                art=art_id(attrs.get('artwork', {}).get('url', '')),
                duration=ms, count=attrs.get('trackCount', 0), kind=view,
                route=path, bg=attrs.get('artwork', {}).get('bgColor', '181818'),
                year=int((attrs.get('releaseDate') or '0')[:4]))


def cached_row(v,duration=False):
    attrs={'name':v['title'],'artistName':v.get('subtitle',''),'durationInMillis':v.get('duration',0),'trackCount':v.get('count',0),'releaseDate':v.get('releaseDate',''),'artwork':{'url':v.get('artwork',''),'bgColor':v.get('bg','181818')}}
    return item_row(dict(id=v['id'],type=v.get('type') or 'library-'+v['kind'],attributes=attrs),duration)


class Library:
    def __init__(self, cache, client):
        self.cache, self.client = cache, client
        self.remote = collections.OrderedDict()
        self.state = {}
        self.context = None
        self.merged = {}

    def _items(self, kind):
        if kind == 'albums': return self.cache.album_view().rows
        with self.cache.lock:
            rows = self.cache.db.execute('SELECT value FROM items WHERE kind=?', (kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def _read(self, path, offset, limit):
        key = (path, offset, limit)
        cached = self.remote.get(key)
        if cached and time.monotonic()-cached[0] < 60:
            return cached[1]
        sep = '&' if '?' in path else '?'
        try:
            reply = self.client.library_path(path+sep+f'offset={offset}&limit={limit}')
            if isinstance(reply.get('data'), dict):
                reply = reply['data']
            if isinstance(reply.get('results'),dict):
                results=reply['results']
                reply=dict(data=[item for kind in ('library-songs','library-albums','library-artists') for item in results.get(kind,{}).get('data',[])],
                           next=any(v.get('next') for v in results.values()))
            if not isinstance(reply.get('data'), list):
                raise ValueError('Invalid library relationship')
            self.remote[key] = (time.monotonic(), reply)
            self.remote.move_to_end(key)
            while len(self.remote) > 32:
                self.remote.popitem(last=False)
            return reply
        except (OSError, ValueError, TimeoutError) as error:
            if cached:
                return dict(cached[1], stale=True, _error=page_error(error))
            return dict(data=[], stale=True, meta={'total':0}, _error=page_error(error))

    def _all(self, path):
        """Read complete relationships before logical deduplication/pagination."""
        data=[]; offset=0
        while offset<10000:
            reply=self._read(path,offset,50)
            if reply.get('_error'): raise LibraryReadError(reply['_error'],data+reply['data'])
            rows=reply['data']; data.extend(rows)
            total=reply.get('meta',{}).get('total')
            if not reply.get('next') and (total is None or len(data)>=int(total)):
                return data
            if not rows: raise ValueError('Incomplete library relationship')
            offset+=50 if '/library/search?' in path else len(rows)
        raise ValueError('Library relationship exceeds 10000 rows')

    def _merged_tracks(self, album, strict=False):
        view=self.cache.album_view(); key=album['id']; saved=self.merged.get(key)
        if saved and saved[0]==view.signature and time.monotonic()-saved[1]<60:
            return saved[2], None
        try:
            tracks=[];member_counts={}
            for member in album['members']:
                path='/v1/me/library/albums/'+urllib.parse.quote(member,safe='')+'/tracks?include=catalog'
                rows=self._all(path);tracks.extend(rows)
                member_counts[member]=len(ordered_tracks(rows))
            # Include strongly matched songs whose Apple album id is absent from
            # the index (observed for 13 of La La Land's 15 indexed songs).
            tracks=ordered_tracks(tracks+view.indexed_tracks(album,orphans_only=True))
            if not tracks: raise ValueError('Merged album has no playable tracks')
            self.cache.register_templates(tracks)
            self.cache.remember_album_tracks(key,view.signature,tracks,member_counts)
            self.merged[key]=(view.signature,time.monotonic(),tracks)
            return tracks,None
        except (OSError,ValueError,TimeoutError) as error:
            if strict: raise
            return album.get('_tracks',[]),page_error(error)

    def _album_resource_rows(self, items):
        view=self.cache.album_view(); seen=set(); rows=[]
        for item in items:
            if 'albums' in item.get('type',''):
                logical=view.find_resource(item,art_id(item.get('attributes',{}).get('artwork',{}).get('url','')))
                if logical: item=resource(logical)
                identity=(item.get('type'),item['id'])
                if identity in seen: continue
                seen.add(identity)
            rows.append(item_row(item))
        return rows

    def page(self, query, offset, limit, stale=False):
        if len(query) > 380 or offset < 0 or not 1 <= limit <= 50:
            raise ValueError('Invalid native library query')
        parsed = urllib.parse.urlsplit(query)
        name = parsed.path
        args = dict(urllib.parse.parse_qsl(parsed.query))
        if name.startswith('now/'):
            from hrcider_context import page
            return page(self, name, offset, limit, stale)
        items, remote = [], None
        if name.startswith('root/'):
            name = ROOT.get(urllib.parse.unquote(name[5:]), 'empty')
        if name=='albums':stale=stale or any(not a.get('_complete',True) for a in self._items('albums'))
        if name=='albums' and (not args or set(args)=={'format'}):
            values=sorted(self._items('albums'),key=lambda a:(a['title'].casefold(),a['id']))
            return dict(generation=self.cache.generation,offset=offset,total=len(values),stale=bool(stale),
                        items=[cached_row(v,args.get('format')=='duration') for v in values[offset:offset+limit]])
        if name in ('albums','songs','playlists') and (not args or set(args)=={'format'}):
            with self.cache.lock:
                total=self.cache.db.execute('SELECT count(*) FROM items WHERE kind=?',(name,)).fetchone()[0]
                values=self.cache.db.execute("SELECT value FROM items WHERE kind=? ORDER BY json_extract(value,'$.title') COLLATE NOCASE,id LIMIT ? OFFSET ?",(name,limit,offset)).fetchall()
                generation=self.cache.generation
            return dict(generation=generation,offset=offset,total=total,stale=bool(stale),items=[cached_row(json.loads(v[0]),args.get('format')=='duration') for v in values])
        if name=='random':
            albums=self._items('albums')
            if albums:
                value=cached_row(random.choice(albums))
                value['route']='play/library-albums/'+urllib.parse.quote(value['id'],safe='')
                value['kind']='song';items=[value]
            return dict(generation=self.cache.generation,offset=offset,total=len(items),stale=bool(stale),items=items[offset:offset+limit])
        if name=='shuffle':
            with self.cache.lock:
                have=self.cache.db.execute("SELECT 1 FROM items WHERE kind='songs' LIMIT 1").fetchone()
            items=[row('Shuffle Songs','shuffle','song')] if have else []
            return dict(generation=self.cache.generation,offset=offset,total=len(items),stale=bool(stale),items=items[offset:offset+limit])
        menus = {
            'letters':[('Albums','letters/albums'),('Album Artists','letters/albumartists'),('Artists','letters/artists'),('Tracks','letters/songs')],
            'tracks':[('Filename','songs'),('Title','songs'),('Title (with track duration)','songs?format=duration')],
            'searchby':[(x,route('input',field=y)) for x,y in [('Album','album'),('Artist','artist'),('Album Artist','albumartist'),('Title','title'),('Albums by Year','year'),('Albums between Years','range'),('Artists between Years','artistrange'),('User Rating','rating')]],
            'history':[(x,'history/'+y) for x,y in [('Recently played tracks','tracks'),('Recently Played Albums','albums'),('Recently Played Artists','artists')]],
            'same':[(x,route('same/'+y)) for x,y in [('Title','title'),('Artist','artist'),('Album','album'),('Composer','composer')]],
            'ratings':[('0','songs?rating=0'),('1','songs?rating=1'),('2','songs?rating=-1')],
        }
        if name in menus:
            items=[row(t,p) for t,p in menus[name]]
        elif name.startswith('letters/'):
            target=name[8:]
            if target not in ('albums','albumartists','artists','songs'):
                raise ValueError('Invalid letter target')
            items=[row('Numeric',route(target,letter='numeric'))]+[row(c,route(target,letter=c)) for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ']+[row('Special Character',route(target,letter='special'))]
        elif name.startswith(('album/','playlist/')):
            parts=name.split('/')
            kind=parts[0];key=parts[-1]
            if not re.fullmatch(r'[\w.:-]{1,128}',urllib.parse.unquote(key)):
                raise ValueError('Invalid library id')
            if len(parts)==3 and re.fullmatch(r'catalog-[A-Za-z-]{2,16}',parts[1]):
                path='/v1/catalog/'+parts[1][8:]+'/'+kind+'s/'+key+'/tracks'
            elif len(parts)==2:path='/v1/me/library/'+kind+'s/'+key+'/tracks'
            else:raise ValueError('Invalid collection route')
            album=self.cache.album_view().by_id.get(urllib.parse.unquote(key)) if kind=='album' and len(parts)==2 else None
            if album and len(album['members'])>1:
                tracks,error=self._merged_tracks(album)
                page=dict(generation=self.cache.generation,offset=offset,total=len(tracks),stale=bool(stale or error),
                          items=[item_row(v) for v in tracks[offset:offset+limit]])
                if error:page['_error']=error
                return page
            remote=self._read(path,offset,limit)
        elif name=='recent':
            return self._album_page('/v1/me/library/recently-added',offset,limit,stale)
        elif name.startswith('history/'):
            target=name.split('/',1)[1]
            endpoint={'tracks':'/v1/me/recent/played/tracks','albums':'/v1/me/recent/played','artists':'/v1/me/recent/played/tracks'}.get(target)
            if endpoint:
                if target=='albums':return self._album_page(endpoint,offset,limit,stale,albums_only=True)
                remote=self._read(endpoint,offset,limit)
                if target=='artists':
                    names=list(dict.fromkeys(i.get('attributes',{}).get('artistName','') for i in remote['data']))
                    remote=dict(remote,data=[dict(id=n,type='artists',attributes={'name':n}) for n in names if n])
                else:remote=dict(remote,data=[i for i in remote['data'] if target in i.get('type','')])
        elif name=='search' and args.get('q'):
            return self._album_page('/v1/me/library/search?term='+urllib.parse.quote(args['q'])+'&types=library-songs,library-albums,library-artists',offset,limit,stale)
        elif name.startswith('same/'):
            field=name[5:]
            value=self.state.get(field,'')
            return self.page(route('songs' if field in ('title','composer','album') else 'albums',**{field:value or '\x00'}),offset,limit,stale)
        elif name in ('albums','songs','playlists','artists','albumartists','genres','years','composers','featured','books','podcasts'):
            songs=self._items('songs')
            album_matches=set()
            if args.get('album') or args.get('album~'):
                view=self.cache.album_view(); term=normalized(args.get('album',args.get('album~')))
                for a in view.rows:
                    titles=set().union(*(view.title_aliases[k] for k in a['members']))
                    if any(term in t if 'album~' in args else term==t for t in titles):
                        album_matches.update(t['id'] for t in a.get('_tracks',view.indexed_tracks(a)))
            if name=='featured' or 'guest' in args:
                known={s.get('albumartist') or s.get('subtitle','') for s in songs}
                known.update(a['title'] for a in self._items('artists'))
                songs=[dict(s,guests=featured_guests(s['title'],known)+featured_guests(s.get('artist',''),known)) for s in songs]
            def match(s):
                for raw_field,val in args.items():
                    field=raw_field.removesuffix('~')
                    contains=raw_field.endswith('~')
                    if field in ('format','letter'):continue
                    if field=='genre' and val not in s.get('genres',[]):return False
                    if field=='year' and not s.get('releaseDate','').startswith(val):return False
                    if field=='range':
                        bounds=val.split('-',1)
                        if len(bounds)!=2 or not bounds[0]<=s.get('releaseDate','')[:4]<=bounds[1]:return False
                    elif field=='guest' and (val not in s.get('guests',[]) or val.strip().lower()==s.get('albumartist','').strip().lower()):return False
                    elif field not in ('genre','year','guest','rating'):
                        if field=='album' and s['id'] in album_matches:continue
                        actual=str(s.get(field,'')).casefold()
                        if (val.casefold() not in actual) if contains else (val.casefold()!=actual):return False
                    if field=='rating' and str(s.get('rating',0))!=('-1' if val=='2' else val):return False
                return True
            songs=[s for s in songs if match(s)]
            if name in ('books','podcasts'):
                term='audiobook' if name=='books' else 'podcast'
                songs=[s for s in songs if any(term in g.casefold() for g in s.get('genres',[]))]
                name='albums'
            if name in ('genres','years','composers','albumartists','artists','featured'):
                field={'genres':'genre','years':'year','composers':'composer','albumartists':'albumartist','artists':'artist','featured':'guest'}[name]
                values=set()
                for s in songs:
                    if field=='genre':values.update(s.get('genres',[]))
                    elif field=='year':values.add(s.get('releaseDate','')[:4])
                    elif field=='guest':values.update(g for g in s.get('guests',[]) if g.strip().lower()!=s.get('albumartist','').strip().lower())
                    else:values.add(s.get(field,'') or (s.get('subtitle','') if field in ('artist','albumartist') else ''))
                target='albumartists' if field in ('genre','year') else 'albums'
                items=[row(v,route(target,**(args|{field:v})), 'artist' if field in ('artist','albumartist','guest') else 'menu') for v in sorted(values,key=str.casefold) if v]
            else:
                values=songs if name=='songs' else self._items(name)
                if name=='songs':values=songs
                elif name=='albums' and set(args)-{'letter','format'}:
                    album_ids={key for s in songs for key in s.get('albums',[])}
                    song_ids={s['id'] for s in songs}
                    albums={s.get('album','') for s in songs if not s.get('albums')}
                    values=[s for s in values if set(s.get('members',[s['id']])) & album_ids or s['title'] in albums
                            or any(v['id'] in song_ids for v in s.get('_tracks',[]))]
                for v in values:
                    kind=v.get('type') or 'library-'+name
                    attrs={'name':v['title'],'artistName':v.get('subtitle',''),'durationInMillis':v.get('duration',0),'trackCount':v.get('count',0),'releaseDate':v.get('releaseDate',''),'artwork':{'url':v.get('artwork',''),'bgColor':v.get('bg','181818')}}
                    items.append(item_row(dict(id=v['id'],type=kind,attributes=attrs),args.get('format')=='duration'))
                if name=='songs' and args.get('album'):
                    rank={v['id']:(v.get('disc',0),v.get('track',0)) for v in values}
                    items.sort(key=lambda r:rank[r['id']])
                else:items.sort(key=lambda r:r['title'].casefold())
            if args.get('letter'):
                c=args['letter'];items=[v for v in items if (v['title'][:1].isdigit() if c=='numeric' else v['title'][:1].upper() not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789' if c=='special' else v['title'][:1].upper()==c)]
        if remote is not None:
            items=[item_row(i) for i in remote['data']]
            # Relationship artwork is registered only as whitelisted metadata.
            self.cache.register_templates(remote['data'])
            total=remote.get('meta',{}).get('total',offset+len(items)+(1 if remote.get('next') else 0))
            page=dict(generation=self.cache.generation,offset=offset,total=total,stale=stale or remote.get('stale',False),items=items)
            if remote.get('_error'):page['_error']=remote['_error']
            return page
        return dict(generation=self.cache.generation,offset=offset,total=len(items),stale=bool(stale),items=items[offset:offset+limit])

    def _album_page(self,path,offset,limit,stale,albums_only=False):
        error=None
        try: data=self._all(path)
        except (OSError,ValueError,TimeoutError) as e:
            data=getattr(e,'items',[]);error=page_error(e)
        if albums_only:data=[v for v in data if 'albums' in v.get('type','')]
        self.cache.register_templates(data)
        rows=self._album_resource_rows(data)
        result=dict(generation=self.cache.generation,offset=offset,total=len(rows),stale=bool(stale or error),items=rows[offset:offset+limit])
        if error:result['_error']=error
        return result

    def play(self, path):
        if path=='random':
            albums=self._items('albums')
            if not albums:raise ValueError('No albums')
            chosen=random.choice(albums)
            return self.play('play/library-albums/'+urllib.parse.quote(chosen['id'],safe=''))
        if path=='shuffle':
            return self.client.shuffle_library()
        parts=path.split('/')
        if len(parts)!=3 or parts[0]!='play':raise ValueError('Invalid play route')
        kind,key=parts[1],urllib.parse.unquote(parts[2])
        context,self.context=self.context,None
        album=self.cache.album_view().by_id.get(key) if kind=='library-albums' else None
        if album and len(album['members'])>1:
            return self.client.play_items(self._merged_tracks(album,strict=True)[0],0)
        if context and kind in ('songs','library-songs') and time.monotonic()-context[2]<15:
            match=re.fullmatch(r'album/([^/?]+)',context[1])
            album=self.cache.album_view().by_id.get(urllib.parse.unquote(match[1])) if match else None
            if album and len(album['members'])>1:
                tracks=self._merged_tracks(album,strict=True)[0]
                from hrcider_albums import aliases
                wanted=self.client.song_aliases(kind,key)
                index=context[0]
                if not 0<=index<len(tracks) or not (aliases(tracks[index]) & wanted):
                    index=next((i for i,t in enumerate(tracks) if aliases(t)&wanted),None)
                if index is None:raise ValueError('Selected song is not in merged album')
                return self.client.play_items(tracks,index)
            try:
                result=self._play_in_list(context[0],context[1],kind,key)
                if result is not None:return result
            except (OSError,ValueError,TimeoutError):
                pass  # the song still plays on its own
        return self.client.control('play-item',kind+':'+key)

    def set_context(self, value):
        """native-context: 'index|list-route', for the native-play that follows."""
        index,_,route=str(value).partition('|')
        self.context=None
        if index.isdigit() and 0<len(route)<=380 and int(index)<100000:
            self.context=(int(index),route,time.monotonic())

    def _play_in_list(self, index, route, kind, key):
        """Play song `key` of the list `route` (row `index`) so that Cider's queue is
        that list: a whole album or playlist, or a window around the row."""
        aliases=self.client.song_aliases(kind,key)
        match=re.fullmatch(r'(album|playlist)/(?:catalog-([a-zA-Z-]+)/)?([^/?]+)',route)
        if match:
            ctype=('' if match[2] else 'library-')+('albums' if match[1]=='album' else 'playlists')
            try:
                result=self.client.play_collection(ctype,urllib.parse.unquote(match[3]),aliases,index)
                _,queued=self.client.queue_state()
                expected=self.page(route,0,1)['total']
                if queued>=expected and expected>0:
                    log.info('collection queue verified type=%s id=%s entries=%d expected=%d',ctype,match[3],queued,expected)
                    return result
                log.warning('collection queue incomplete type=%s id=%s entries=%d expected=%d; recovering list window',ctype,match[3],queued,expected)
            except (OSError,ValueError,TimeoutError) as error:
                log.warning('collection queue failed type=%s id=%s reason=%s; recovering list window',ctype,match[3],error)
            # A refused/truncated collection must not silently degrade to a
            # single song. Reuse the verified paged-list queue fill below.
        start=max(0,index-LIST_BEFORE)
        rows=self.page(route,start,LIST_BEFORE+LIST_AFTER+1)['items']
        songs=[(r['id'],r['route'].split('/')[1],urllib.parse.unquote(r['route'].split('/')[2]))
               for r in rows if r['route'].startswith('play/') and r['route'].count('/')==2]
        here=next((i for i,v in enumerate(songs) if v[2]==key),None)
        if here is None:return None
        before=[v[1:] for v in songs[:here]];after=[v[1:] for v in songs[here+1:]]
        if not before and not after:return None
        result=self.client.control('play-item',kind+':'+key)
        result['fill']=lambda cancel:self.client.fill_queue(aliases,before,after,cancel)
        return result
