"""Native-source providers and monitor volume; all mutations are mocked."""
from pathlib import Path
import json,socket,sys,threading,unittest,tempfile,subprocess
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hrcider_volume import MonitorVolume
from hrcider_library import Library
from hrcider_cache import Cache

class Native(unittest.TestCase):
    def test_monitor_socket_and_confirmed_state(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as d, socket.socket(socket.AF_UNIX) as server:
            run=Mock();volume=MonitorVolume(env={'HOME':d,'XDG_RUNTIME_DIR':d},run=run)
            volume.state.parent.mkdir(parents=True)
            volume.state.write_text(json.dumps(dict(volume_percent=42,muted=False,confirmed=True)))
            server.bind(str(volume.socket));server.listen();server.settimeout(2)
            requests=[]

            def serve():
                with server.accept()[0] as client, client.makefile('rb') as stream:
                    requests.append(json.loads(stream.readline()))
                    client.sendall(b'{"volume_percent":49,"muted":false,"confirmed":false}\n')

            worker=threading.Thread(target=serve);worker.start()
            self.assertEqual(volume.read(),(42,False));volume.step(7);worker.join(2)
            self.assertEqual(requests,[dict(action='set',value=49)])
            self.assertEqual(volume.read(),(49,False));run.assert_not_called()
            confirmed=volume.state.with_suffix('.new')
            confirmed.write_text(json.dumps(dict(volume_percent=0,muted=True,confirmed=True)))
            confirmed.replace(volume.state)
            self.assertEqual(volume.read(),(0,True))

    def test_monitor_cli_fallback_only(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as d:
            run=Mock(return_value=subprocess.CompletedProcess([],0,b'{"hardware":false}\n',b''))
            volume=MonitorVolume(env={'HOME':d,'XDG_RUNTIME_DIR':d},run=run)
            volume.step(2);volume.set(55)
            self.assertEqual([c.args[0] for c in run.call_args_list],
                             [['python3',str(volume.helper),'inc']]*2 +
                             [['python3',str(volume.helper),'set','55']])
            self.assertEqual(volume.read(),(55,False))
    def test_provider_tree_facets_tracks_and_generation(self):
        with tempfile.TemporaryDirectory() as d:
            c=Cache(d);client=Mock();lib=Library(c,client)
            c.ingest('albums',[{'id':'a','type':'library-albums','attributes':{'name':'Album','artistName':'Lead','trackCount':1}}],1)
            c.ingest('songs',[{'id':'s','type':'library-songs','attributes':{'name':'Song (feat. Guest)','artistName':'Lead','albumName':'Album','albumArtistName':'Lead','durationInMillis':123000,'genreNames':['Rock'],'releaseDate':'2020-01-01','composerName':'Writer','trackNumber':1}}],1)
            for route,title in [('albums','Album'),('albumartists','Lead'),('artists','Lead'),('genres','Rock'),('years','2020'),('composers','Writer'),('featured','Guest')]:
                p=lib.page(route,0,50);self.assertEqual(p['items'][0]['title'],title);self.assertEqual(p['generation'],c.generation)
            client.library_path.return_value={'data':[{'id':'s','type':'library-songs','attributes':{'name':'Song','durationInMillis':123000}}]}
            p=lib.page('album/a',0,50);self.assertEqual(p['items'][0]['kind'],'song');self.assertEqual(p['items'][0]['route'],'play/library-songs/s')
            self.assertIn('/albums/a/tracks',client.library_path.call_args.args[0]);c.close()
    def test_duration_and_paging(self):
        with tempfile.TemporaryDirectory() as d:
            c=Cache(d);lib=Library(c,Mock())
            c.ingest('songs',[{'id':str(i),'attributes':{'name':f'Title {i:03}','durationInMillis':123000}} for i in range(125)],1)
            p=lib.page('songs?format=duration',50,50)
            self.assertEqual((p['offset'],p['total'],len(p['items'])),(50,125,50));self.assertTrue(p['items'][0]['title'].endswith(' - 02:03'));c.close()


class NativeWorkers(unittest.TestCase):
    def test_no_cider_volume_and_system_coalescing(self):
        import hrcider,threading,time
        with tempfile.TemporaryDirectory() as d:
            cache=Cache(d);client=Mock();volume=Mock()
            bridge=hrcider.Bridge(client=client,cache=cache,volume=volume)
            bridge.command(b'ci\t1\t131072');bridge.available=True
            gen=bridge.generation
            for i in range(25):bridge.command(f'cc\t{gen}\t{i+1}\tvolume-step\t1'.encode())
            t=threading.Thread(target=bridge._volume_worker);t.start()
            end=time.monotonic()+1
            while not volume.step.called and time.monotonic()<end:time.sleep(.005)
            bridge.stop.set();t.join(1)
            volume.step.assert_called_once_with(25);client.control.assert_not_called()
            cache.close()
    def test_absolute_and_relative_coalesce_for_every_backend(self):
        import hrcider,time
        for backend in ('Cider','Rufin','Jellyfin'):
            with tempfile.TemporaryDirectory(dir='/tmp') as d:
                client=Mock(name=backend);volume=Mock();cache=Cache(d)
                bridge=hrcider.Bridge(client=client,cache=cache,volume=volume)
                bridge.command(b'ci\t1\t131072');gen=bridge.generation
                bridge.command(f'cc\t{gen}\t1\tvolume\t50'.encode())
                bridge.command(f'cc\t{gen}\t2\tvolume-step\t-1'.encode())
                worker=threading.Thread(target=bridge._volume_worker);worker.start()
                end=time.monotonic()+1
                while not volume.set.called and time.monotonic()<end:time.sleep(.005)
                bridge.stop.set();worker.join(1)
                volume.set.assert_called_once_with(49)
                volume.step.assert_not_called();client.control.assert_not_called()
                cache.close()
    def test_source_local_checks_live_state_once_and_never_starts_local(self):
        import hrcider,threading,time
        for playing in (False,True):
            with tempfile.TemporaryDirectory() as d:
                cache=Cache(d);client=Mock();client.request.return_value={'is_playing':playing}
                b=hrcider.Bridge(client=client,cache=cache,volume=Mock());b.command(b'ci\t1\t131072');b.available=True
                b.command(f'cc\t{b.generation}\t1\tsource-local\t0'.encode());t=threading.Thread(target=b._control);t.start()
                end=time.monotonic()+1
                while not client.request.called and time.monotonic()<end:time.sleep(.005)
                b.stop.set();t.join(1)
                client.request.assert_called_once_with('/api/v1/playback/is-playing')
                if playing:client.control.assert_called_once_with('toggle')
                else:client.control.assert_not_called()
                cache.close()
    def test_browse_routes_native_menu_and_ratings(self):
        with tempfile.TemporaryDirectory() as d:
            c=Cache(d);client=Mock();lib=Library(c,client)
            self.assertEqual(lib.page('random',0,50)['total'],0)
            self.assertEqual(lib.page('shuffle',0,50)['total'],0)
            song={'id':'s','type':'library-songs','attributes':{'name':'Title','artistName':'Lead','albumName':'Album','durationInMillis':123000},'relationships':{'catalog':{'data':[{'id':'123'}]},'albums':{'data':[{'id':'a'}]}}}
            c.ingest('songs',[song],1);c.ingest('albums',[{'id':'a','attributes':{'name':'Album','artistName':'Various Artists'}}],1)
            self.assertEqual(lib.page('albums?artist=Lead',0,50)['items'][0]['id'],'a')
            self.assertEqual(lib.page('random',0,50)['items'][0]['route'],'play/library-albums/a')
            self.assertEqual(lib.page('shuffle',0,50)['items'][0]['route'],'shuffle')
            c.apply_ratings([{'id':'123','attributes':{'value':1}}]);self.assertEqual(lib.page('songs?rating=1',0,50)['total'],1)
            self.assertEqual(lib.page('songs?rating=-1',0,50)['total'],0)
            c.ingest('songs',[song],2)
            self.assertEqual(lib.page('songs?rating=1',0,50)['total'],1)
            from hrcider_cache import Index
            index=Index(c,client);index.rating_jobs=[['123']]
            client.library_path.return_value={'data':[]};index.step(10000)
            self.assertEqual(lib.page('songs?rating=1',0,50)['total'],0)
            c.ingest('songs',[{'id':'s2','attributes':{'name':'Other','artistName':'Lead Two','albumName':'Second'},'relationships':{'albums':{'data':[{'id':'a2'}]}}}],2)
            c.ingest('albums',[{'id':'a2','attributes':{'name':'Second','artistName':'Lead Two'}}],2)
            self.assertEqual(lib.page('albums?artist=Lead',0,50)['total'],1)
            self.assertEqual(lib.page('albums?artist~=Lead',0,50)['total'],2)
            self.assertEqual([r['title'] for r in lib.page('tracks',0,50)['items']],['Filename','Title','Title (with track duration)'])
            self.assertFalse(any(r['title'] in ('Filename','Score','Comment') for r in lib.page('searchby',0,50)['items']))
            c.close()
    def test_latest_two_waiting_pages_survive_queue_overflow(self):
        import hrcider
        with tempfile.TemporaryDirectory() as d:
            c=Cache(d);b=hrcider.Bridge(client=Mock(),cache=c,volume=Mock());b.command(b'ci\t1\t131072')
            for i in range(50):self.assertTrue(b.command(f'cq\t{b.generation}\t{i+1}\t{i}\t50\tsongs'.encode()))
            self.assertEqual(b.pages.qsize(),2)
            self.assertEqual([b.pages.get_nowait()[1] for _ in range(2)],[49,50]);b.close()
    def test_feature_parser_native_marker_boundaries(self):
        from hrcider_cache import featured_guests
        for text,want in [('Defeat',[]),('Feather',[]),('Song with a Broken Heart',[]),('Song (feat. X) (Live)',[]),('Song featuring X & Y',[]),('Song w/o X',[]),('Song (duet with X)',[])]:
            self.assertEqual(featured_guests(text),want)
    def test_search_wrapped_results_and_relationship_order(self):
        with tempfile.TemporaryDirectory() as d:
            c=Cache(d);client=Mock();lib=Library(c,client)
            client.library_path.return_value={'data':{'results':{'library-songs':{'data':[{'id':'s','type':'library-songs','attributes':{'name':'Title'}}]}}}}
            p=lib.page('search?q=Title',0,50);self.assertEqual(p['items'][0]['route'],'play/library-songs/s');self.assertIn('library/search?term=Title',client.library_path.call_args.args[0]);c.close()

if __name__=='__main__':unittest.main()
