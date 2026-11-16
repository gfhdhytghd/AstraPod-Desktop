"""FU40: real split-album shapes; all playback mutations remain in memory."""
import copy
import json
import sys
import urllib.parse
from pathlib import Path
from unittest.mock import Mock
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hrcider_albums import AlbumView, merge_reason, ordered_tracks, resource
from hrcider_cache import Cache, Index, art_id
from hrcider_library import Library
from hrcider import Client, CiderPluginClient

FIXTURE = json.loads((Path(__file__).parent/'fixtures/fu40-albums.json').read_text())


def load(cache):
    for kind in ('albums','songs'):
        cache.ingest(kind,[resource(v) for v in FIXTURE[kind]],1)


@pytest.mark.parametrize('pair', FIXTURE['pairs'], ids=[p[0] for p in FIXTURE['pairs']])
def test_all_32_title_pairs_and_relationship_proven_localized_pair(pair):
    view=AlbumView(FIXTURE['albums'],FIXTURE['songs'])
    a,b=(view.raw[i] for i in pair)
    expected='art+artist+relationship-title-alias' if pair==['l.Bec2pra','l.RVOmIvx'] else 'title+artist+compatible-art'
    assert merge_reason(a,b,view.title_aliases)==expected
    assert view.by_id[a['id']] is view.by_id[b['id']]
    assert view.by_id[a['id']]['id']==min(pair)
    leader=min((a,b),key=lambda a:(-a['count'],a['id']))
    assert view.by_id[a['id']]['art']==leader['art']
    assert view.by_id[a['id']]['title']==leader['title']


def album(key,title='Album',artist='Artist',art='cover',catalog=()):
    return dict(id=key,kind='albums',type='library-albums',title=title,albumartist=artist,
                subtitle=artist,art=art,count=1,catalog=list(catalog))


@pytest.mark.parametrize('b',[
    album('b',artist='Other'), album('b',art='different'),
    album('b',title='Album (Deluxe)',art='different',catalog=['1']),
    album('b',title='Album (Live)',catalog=['1']),
    album('b',title='Unrelated'), album('b',title='Unknown Translation'),
])
def test_conflicting_editions_artists_and_unproven_translations_stay_separate(b):
    a=album('a',catalog=['1'])
    assert len(AlbumView([a,b],[]).rows)==2


def test_normalization_catalog_and_no_transitive_edition_bridge():
    a=album('a',title=' Ａlbum   ',artist=' ARTIST ')
    b=album('b',title='album',art='-')
    c=album('c',title='album',art='different')
    v=AlbumView([c,b,a],[])
    assert v.by_id['a'] is v.by_id['b']
    assert v.by_id['a'] is not v.by_id['c']
    b=album('b',title='中文专辑',catalog=['42'])
    a=album('a',catalog=['42'])
    assert len(AlbumView([a,b],[]).rows)==1
    a['catalog']=[];a['playParams']={'catalogId':'42'}
    assert len(AlbumView([a,b],[]).rows)==1
    b.update(title=a['title'],art='alternate-catalog-cover')
    assert len(AlbumView([a,b],[]).rows)==1


def song(key,track,disc=1,catalog=None,title=None):
    return dict(id=key,type='library-songs',attributes=dict(name=title or key,discNumber=disc,
                trackNumber=track,durationInMillis=1000,playParams=dict(id=key,**({'catalogId':catalog} if catalog else {}))))


def test_identity_union_disc_track_title_and_repeated_titles():
    a=song('a',2,catalog='100');b=song('b',2,catalog='100')
    c=song('c',1,title='City of Stars');d=song('d',3,title='City of Stars')
    e=song('e',1,disc=2);f=song('f',0,title='Z');g=song('g',0,title='A')
    union=ordered_tracks([e,f,a,b,d,c,g])
    assert [v['id'] for v in union]==['c','a','d','g','f','e']
    assert ordered_tracks(list(reversed([e,f,a,b,d,c,g])))==union


@pytest.mark.parametrize('key,count', [('l.OIt8bU7',15),('l.Bec2pra',11)])
def test_real_union_including_orphan_lalaland_membership(key,count):
    view=AlbumView(FIXTURE['albums'],FIXTURE['songs']);a=view.by_id[key]
    assert a['count']==count
    assert [v['attributes']['trackNumber'] for v in a['_tracks']]==list(range(1,count+1))
    assert a['duration']==sum(t['attributes']['durationInMillis'] for t in a['_tracks'])
    if count==15:assert sum(v['attributes']['name']=='City of Stars' for v in a['_tracks'])==2


class ReadOnlyLibrary:
    def __init__(self,view):
        self.calls=[];self.rows={}
        for key,count in [('l.OIt8bU7',15),('l.Bec2pra',11)]:
            a=view.by_id[key];tracks=a['_tracks'];first,second=a['members']
            # Endpoints are authoritative, and may repeat a matched song.
            self.rows[first]=tracks[:-1];self.rows[second]=tracks[-2:]
    def library_path(self,path):
        self.calls.append(path)
        key=urllib.parse.unquote(path.split('/albums/')[1].split('/')[0])
        q=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query));offset=int(q.get('offset',0));limit=int(q.get('limit',50))
        rows=self.rows.get(key,[])
        return dict(data=rows[offset:offset+limit],meta={'total':len(rows)},**({'next':'next'} if offset+limit<len(rows) else {}))


def test_shared_grid_letters_facets_search_recent_history_and_persistence(tmp_path):
    c=Cache(tmp_path);load(c);v=c.album_view();fake=ReadOnlyLibrary(v);lib=Library(c,fake)
    la=v.by_id['l.OIt8bU7'];pu=v.by_id['l.Bec2pra']
    before=c.generation
    page=lib.page('album/l.gIwqHi4',0,50)
    assert page['total']==15 and not page['stale']
    assert c.generation>before
    assert lib.page('album/l.RVOmIvx',5,3)['items'][0]['title']=='今夜的滋味'
    for key in (la['id'],pu['id']):
        assert sum(a['id']==key for a in c.page('albums',0,50)['items'])<=1
        assert sum(a['id']==key for a in lib.page('albums',0,50)['items'])<=1
    assert len([a for a in lib.page('albums?letter=L',0,50)['items'] if a['title'].startswith('La La Land')])==1
    assert len([a for a in lib.page('albums?artist='+urllib.parse.quote('朴树'),0,50)['items'] if a['id']==pu['id']])==1
    remote=[resource(v.raw[i]) for i in la['members']+pu['members']]
    # Duplicate members on opposite sides of the API page boundary.
    remote=remote[:1]+[dict(id='p'+str(i),type='library-playlists',attributes={'name':'P'}) for i in range(49)]+remote[1:]
    client=Mock()
    def read(path):
        q=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query));o=int(q.get('offset',0))
        return dict(data=remote[o:o+50],meta={'total':len(remote)},**({'next':'next'} if o+50<len(remote) else {}))
    client.library_path.side_effect=read;lib.client=client
    for path in ('recent','history/albums','search?q=album'):
        p=lib.page(path,0,50);q=lib.page(path,50,50)
        ids=[r['id'] for r in p['items']+q['items']]
        assert ids.count(la['id'])==ids.count(pu['id'])==1
        assert len(ids)==p['total']
    c.close();c=Cache(tmp_path)
    assert c.album_view().by_id[la['id']]['count']==15
    assert c.album_view().by_id[la['id']]['_complete']
    assert c.album_view().by_id[pu['id']]['id']==pu['id']
    assert c.album_view().by_id[la['id']]['art'] in c.library_arts(limit=100000)
    c.close()


def test_full_relationship_pagination_and_strict_failure_no_singleton(tmp_path):
    c=Cache(tmp_path);load(c);lib=Library(c,Mock());a=c.album_view().by_id['l.OIt8bU7']
    tracks=[song('s'+str(i),i+1) for i in range(120)]
    fake=ReadOnlyLibrary(c.album_view());fake.rows={a['members'][0]:tracks[:80],a['members'][1]:tracks[70:]};lib.client=fake
    # Exclude unrelated indexed-orphan songs to isolate the long collection.
    c.album_view().songs=[]
    p=lib.page('album/'+a['id'],50,50)
    assert p['total']==120 and len(p['items'])==50
    assert len(fake.calls)==3
    lib=Library(c,Mock());lib.client.library_path.side_effect=OSError('offline')
    p=lib.page('album/'+a['id'],0,50);assert p['stale'] and p['_error']
    lib.set_context('5|album/'+a['id'])
    with pytest.raises(OSError):lib.play('play/library-songs/s5')
    lib.client.control.assert_not_called();lib.client.play_collection.assert_not_called()
    c.close()


@pytest.mark.parametrize('key,index', [('l.OIt8bU7',0),('l.OIt8bU7',7),('l.OIt8bU7',14),('l.Bec2pra',5),('l.Bec2pra',10)])
def test_context_full_order_selected_index_and_album_play(tmp_path,key,index):
    c=Cache(tmp_path);load(c);v=c.album_view();fake=ReadOnlyLibrary(v);client=Mock()
    client.library_path.side_effect=fake.library_path;lib=Library(c,client)
    a=v.by_id[key];tracks=a['_tracks'];chosen=tracks[index]
    client.song_aliases.return_value=Client.aliases(chosen)
    lib.set_context(str(index)+'|album/'+a['members'][-1]);lib.play('play/library-songs/'+chosen['id'])
    args=client.play_items.call_args.args
    assert args[1]==index
    assert [t['id'] for t in args[0]]==[t['id'] for t in tracks]
    client.play_collection.assert_not_called();client.control.assert_not_called()
    client.play_items.reset_mock();lib.play('play/library-albums/'+a['id'])
    assert client.play_items.call_args.args[1]==0
    # A changed dedup list resolves the selected id rather than trusting a stale index.
    lib.set_context('0|album/'+a['id']);lib.play('play/library-songs/'+chosen['id'])
    assert client.play_items.call_args.args[1]==index
    c.close()


class QueueClient(Client):
    def __init__(self,tracks):
        super().__init__();self.tracks={t['id']:t for t in tracks};self.q=[];self.pos=0;self.mutations=[]
    def control(self,command,value=0):
        assert command=='play-item';_,key=value.split(':',1)
        self.q=[self.tracks[key]];self.pos=0;self.mutations.append(('start',key))
        return {'expected_ids':list(self.aliases(self.q[0]))}
    def queue_state(self):return self.pos,len(self.q)
    def queue_slice(self,offset,limit):return self.q[offset:offset+limit]
    def request(self,path,body=None,**kwargs):
        self.mutations.append((path,body))
        if path.endswith('add-later'):self.q.append(self.tracks[body['id']])
        elif path.endswith('/move'):
            playing=self.q[self.pos];t=self.q.pop(body['from']);self.q.insert(body['to'],t);self.pos=self.q.index(playing)
        else:raise AssertionError(path)
        return {'status':'ok'}


@pytest.mark.parametrize('index',[0,7,14])
def test_actual_rpc_and_inherited_plugin_queue_algorithm(index):
    tracks=[song('s'+str(i),i+1) for i in range(15)];c=QueueClient(tracks)
    result=c.play_items(tracks,index)
    assert c.q==tracks and c.pos==index
    assert c.mutations[0]==('start','s'+str(index))
    assert 's'+str(index) in result['expected_ids']
    assert CiderPluginClient.play_items is Client.play_items


def test_queue_failure_is_not_success_or_replay():
    tracks=[song('s0',1),song('s1',2)];c=QueueClient(tracks)
    c.fill_queue=Mock(return_value=False)
    with pytest.raises(OSError,match='incomplete'):c.play_items(tracks,1)
    assert c.mutations==[('start','s1')]


def test_index_hydrates_one_read_per_step_updates_counts_and_stays_offline(tmp_path):
    c=Cache(tmp_path);load(c);v=c.album_view();a=v.by_id['l.OIt8bU7'];fake=ReadOnlyLibrary(v)
    index=Index(c,fake);index.album_jobs=[dict(album=a,signature=v.signature,member=0,offset=0,tracks=[],orphans=[])];index.last_full=100
    index.step(100);assert len(fake.calls)==1
    index.step(101);assert len(fake.calls)==2 and not index.album_jobs
    assert c.album_view().by_id[a['id']]['_complete']
    assert c.album_view().by_id[a['id']]['count']==15
    c.close()


def test_cover_grid_play_item_uses_logical_album_and_same_song_keeps_queue(tmp_path):
    from hrcider import Bridge
    c=Cache(tmp_path);load(c);client=Mock();client.same_song.return_value=False
    client.transport_snapshot.return_value={'ids':set(),'rate':1000}
    b=Bridge(client=client,cache=c,volume=Mock());b.command(b'ci\t1\t131072');b.available=True
    b.library.play=Mock(return_value={})
    original=b.scheduler.push
    def stop(*args,**kwargs):original(*args,**kwargs);b.stop.set()
    b.scheduler.push=stop
    key=c.album_view().by_id['l.OIt8bU7']['id']
    b.controls.put((b.generation,1,'play-item','library-albums:'+key));b._control()
    b.library.play.assert_called_once_with('play/library-albums/'+key)
    b.stop.clear();b.library.play.reset_mock();client.same_song.return_value=True
    b.library.set_context('7|album/'+key)
    b.controls.put((b.generation,2,'native-play','play/library-songs/i.selected'));b._control()
    b.library.play.assert_not_called();client.play_items.assert_not_called()
    assert b.library.context is None
    b.close()


def test_authoritative_member_counts_choose_cover_and_localstore_uses_same_view(tmp_path):
    from hrcider_store import Ledger,Planner
    c=Cache(tmp_path)
    values=[album('a',art='unused'),album('b',art='chosen')]
    # A catalog identity can unite localized titles with distinct covers.
    values[0].update(title='English',catalog=['42'],artwork='https://x/unused/{w}x{h}bb.jpg',count=10)
    values[1].update(title='中文',catalog=['42'],artwork='https://x/chosen/{w}x{h}bb.jpg',count=1)
    c.ingest('albums',[resource(a) for a in values],1)
    a=c.album_view().by_id['a'];s=[song('s',1)]
    c.remember_album_tracks('a',c.album_view().signature,s,{'a':1,'b':5})
    chosen=art_id(values[1]['artwork']);unused=art_id(values[0]['artwork'])
    assert c.album_view().by_id['a']['art']==chosen
    assert chosen in c.library_arts() and unused not in c.library_arts()
    planner=Planner(c,Ledger(c));assert planner.progress()[1]==3
    c.close()


def test_localized_album_search_filter_and_letters_during_partial_index(tmp_path):
    c=Cache(tmp_path);load(c);lib=Library(c,Mock())
    result=lib.page('albums?album='+urllib.parse.quote('Life Like Summer Flowers'),0,50)
    assert [v['id'] for v in result['items']]==['l.Bec2pra']
    tracks=lib.page('songs?album='+urllib.parse.quote('Life Like Summer Flowers'),0,50)
    assert tracks['total']==11
    c.ingest('albums',[dict(id='l.partial',type='library-albums',attributes={'name':'Lately','artistName':'Artist'})],1)
    assert any(v['id']=='l.partial' for v in lib.page('albums?letter=L',0,50)['items'])
    c.close()


def test_catalog_history_identity_survives_locale_changes_without_ambiguous_ownership():
    values=[album('a',title='English',catalog=['42']),album('b',title='中文',catalog=['42'])]
    v=AlbumView(values,[])
    remote=dict(id='42',type='albums',attributes={'name':'Another locale','artistName':'Translated Artist'})
    assert v.find_resource(remote,'unknown-cover')['id']=='a'
    values.append(album('c',title='Unrelated',artist='Other Artist',catalog=['42']))
    assert AlbumView(values,[]).find_resource(remote,'unknown-cover') is None


def test_edition_qualifiers_distinguish_chinese_live_deluxe_and_normalize_scripts():
    a=album('a',title='专辑 (现场)',catalog=['42'])
    b=album('b',title='专辑 (豪华)',catalog=['42'])
    assert len(AlbumView([a,b],[]).rows)==2
    b['title']='專輯 (現場)'
    assert len(AlbumView([a,b],[]).rows)==1
    b['title']='Album (Live)'
    assert len(AlbumView([a,b],[]).rows)==1
