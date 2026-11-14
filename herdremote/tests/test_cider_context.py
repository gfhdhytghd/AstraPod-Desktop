"""FU30 production REST controls and paged Now Playing data, without the app."""
import sys,unittest,tempfile
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hrcider import Client,Bridge
from hrcider_library import Library
from hrcider_cache import Cache
from hrcider_context import lyrics_text

class Context(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.cache=Cache(self.tmp.name)
        self.client=Client();self.library=Library(self.cache,self.client)
        self.info=dict(name='Title',artistName='Artist',albumName='Album',discNumber=2,trackNumber=3,releaseDate='2020-02-03',genreNames=['Rock'],durationInMillis=123000,inFavorites=True,inLibrary=False,playParams={'id':'123'})
        def request(path,*args,**kwargs):
            return {'/api/v2/playback':{'data':{'state':'playing','nowPlaying':self.info,'time':{'currentTime':10},'volume':.37,'rate':1.25,'repeatMode':0,'shuffleMode':0}},'/api/v2/playback/audio-quality':{'data':{'flavorLabel':'AAC 256kbps','deviceAudioConfig':{'sampleRate':44100}}},'/api/v2/library/now-playing/status':{'data':{'rating':1,'inLibrary':False}},'/api/v2/playback/rate':{'data':{'rate':1.25}},'/api/v1/lyrics/123':{'ttml':'<tt><body><p>First</p><p>Second</p></body></tt>'},'/api/v2/playback/time':{'data':{'currentTime':10}}}[path]
        self.client.request=Mock(side_effect=request)
        self.client.queue_state=Mock(return_value=(55,60))
        self.client.queue_slice=Mock(side_effect=lambda off,lim:[dict(id=str(i),type='songs',attributes=dict(name=f'Track {i}',durationInMillis=120000)) for i in range(off,min(60,off+lim))])
    def tearDown(self):self.cache.close();self.tmp.cleanup()
    def test_track_info_and_status_fields(self):
        p=self.library.page('now/info?nonce=1',0,50)
        v={x['id']:x['title'] for x in p['items']}
        for k,want in [('title','Title'),('artist','Artist'),('album','Album'),('disc','2'),('track','3'),('year','2020'),('genre','Rock'),('duration','123000'),('favorite','1'),('library','0'),('speed','1.25')]:self.assertEqual(v[k],want)
        self.assertIn('AAC 256kbps',v['quality'])
        self.assertTrue(all(not args or not args[0].startswith('/api/v2/library/now-playing/a') for args,_ in self.client.request.call_args_list))
    def test_queue_highlight_absolute_jump_route_and_paging(self):
        p=self.library.page('now/queue',50,10)
        self.assertEqual((p['total'],p['offset'],len(p['items'])),(60,50,10))
        self.assertEqual(p['items'][5]['route'],'queue/55');self.assertTrue(all(v['count']==55 for v in p['items']))
    def test_queue_remaining_includes_current_less_elapsed(self):
        p=self.library.page('now/time',0,50)
        self.assertEqual(p['items'][0]['title'],'9:50');self.assertEqual(p['items'][1]['title'],'5')
    def test_lyrics_exposed_rpc_and_ttml(self):
        self.assertEqual([v['title'] for v in self.library.page('now/lyrics',0,50)['items']],['First','Second'])
        self.assertEqual(lyrics_text({'data':[{'attributes':{'ttml':'<tt><p>A<span>B</span></p></tt>'}}]}),'AB')
    def test_navigation_uses_current_album_membership(self):
        self.cache.ingest('songs',[dict(id='123',type='library-songs',attributes=dict(name='Title',artistName='Artist',albumName='Album'),relationships={'albums':{'data':[{'id':'a1'}]}})],1)
        self.library._items=Mock(return_value=[{'id':'123','albums':['a1']}])
        self.assertEqual(self.library.page('now/album',0,50)['items'][0]['route'],'album/a1')
        self.assertEqual(self.library.page('now/artist',0,50)['items'][0]['route'],'albums?artist=Artist')
    def test_explicit_routes_and_validation(self):
        c=Client();c.request=Mock(return_value={});c.queue_state=Mock(return_value=(55,60))
        for command,number,path,body,method in [('love',0,'/library/now-playing/love',{},'POST'),('unlove',0,'/library/now-playing/rating',None,'DELETE'),('add-library',0,'/library/now-playing/add',{},'POST'),('pause',0,'/playback/pause',{},'POST'),('rate',1250,'/playback/rate',{'rate':1.25},'PATCH')]:
            c.control(command,number);c.request.assert_called_with('/api/v2'+path,body,method=method)
        c.control('queue-jump',55);c.request.assert_called_with('/api/v2/queue/jump',{'index':55})
        for command,value in [('rate',0),('rate',4001),('rate','nan'),('queue-jump',-1),('queue-jump',60)]:
            with self.assertRaises(ValueError):c.control(command,value)
