"""FU31b: loopback RPC fixtures only; never contacts Cider or serial hardware."""
import io,json,os,sys,threading,time,tempfile,types,urllib.error,urllib.parse
from pathlib import Path
from unittest.mock import Mock,patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
if os.environ.get('FU31B_SOURCE'):sys.path.insert(0,os.environ['FU31B_SOURCE'])
import hrcider

def playback():
    return dict(state='playing',nowPlaying=dict(name='Title',artistName='Artist',albumName='Album',
        composerName='Composer',durationInMillis=100000,discNumber=2,trackNumber=3,
        playParams={'id':'123','catalogId':'456'},artwork={'url':'https://example.test/{w}x{h}bb.jpg','bgColor':'123456'}),
        time={'currentTime':12},volume=.37,repeatMode=2,shuffleMode=1,rate=1.25)

class Response(io.BytesIO):
    pass

class Opener:
    def __init__(self):self.calls=[];self.data=playback();self.failure=None;self.v1_hangs=True
    def open(self,req,timeout):
        path=urllib.parse.urlsplit(req.full_url).path;self.calls.append(path)
        if path=='/api/v1/playback/now-playing' and self.v1_hangs:
            # The observed fault: v1 remains pending through the client deadline.
            time.sleep(.002);raise TimeoutError('mock v1 now-playing hung')
        if path=='/api/v2/playback':
            if self.failure:raise self.failure
            reply={'data':self.data}
        else:
            reply={'/api/v1/playback/now-playing':{'info':playback()['nowPlaying']},
                '/api/v1/playback/is-playing':{'is_playing':True},
                '/api/v2/playback/time':{'data':{'currentTime':12}},
                '/api/v1/playback/repeat-mode':{'value':2},'/api/v1/playback/shuffle-mode':{'value':1},
                '/api/v2/playback/audio-quality':{'data':{'flavorLabel':'AAC'}},
                '/api/v2/library/now-playing/status':{'data':{'inLibrary':True}},
                '/api/v2/playback/rate':{'data':{'rate':1.25}},
                '/api/v1/lyrics/123':{'ttml':'<tt><p>Words</p></tt>'}}[path]
        return Response(json.dumps(reply).encode())

def client():
    opener=Opener();return hrcider.Client(opener=opener,timeout=.01),opener

def test_twenty_snapshots_never_touch_hanging_v1_and_use_one_coherent_rpc_each():
    c,o=client()
    for _ in range(20):
        s=c.snapshot()
        assert (s['id'],s['title'],s['composer'],s['position'],s['duration'],s['volume'],s['repeat'],s['shuffle'],s['repeat5'],s['rate'])==('123','Title','Composer',12000,100000,37,2,1,1,1250)
        assert s['art']!='-' and s['bg']=='123456'
    assert o.calls==['/api/v2/playback']*20

@pytest.mark.parametrize('value',[None,{},'broken',{'state':'playing','nowPlaying':'bad','time':{}},dict(playback(),rate=float('nan')),dict(playback(),time={'currentTime':float('inf')})])
def test_malformed_v2_falls_back_to_legacy(value):
    c,o=client();o.data=value;o.v1_hangs=False
    assert c.snapshot()['id']=='123'
    assert o.calls[0]=='/api/v2/playback' and '/api/v1/playback/now-playing' in o.calls

@pytest.mark.parametrize('code',[404,405,501])
def test_missing_v2_falls_back(code):
    c,o=client();o.v1_hangs=False;o.failure=urllib.error.HTTPError('http://localhost/api/v2/playback',code,'missing',{},None)
    assert c.snapshot()['position']==12000
    assert '/api/v1/playback/now-playing' in o.calls

@pytest.mark.parametrize('error',[TimeoutError('v2 deadline'),OSError('connection lost'),urllib.error.HTTPError('http://localhost/api/v2/playback',503,'busy',{},None)])
def test_transient_v2_failure_never_fans_out_to_v1(error):
    c,o=client();o.failure=error
    with pytest.raises(OSError):c.snapshot()
    assert o.calls==['/api/v2/playback']

def test_now_playing_pages_and_transport_use_v2_despite_hanging_v1():
    c,o=client()
    with tempfile.TemporaryDirectory() as d:
        cache=hrcider.Cache(d)
        try:
            library=hrcider.Library(cache,c)
            page=library.page('now/info',0,50);assert {v['id']:v['title'] for v in page['items']}['title']=='Title'
            assert library.page('now/lyrics',0,50)['items'][0]['title']=='Words'
            assert c.transport_snapshot()['ids']=={'123','456'}
        finally:cache.close()
    assert '/api/v1/playback/now-playing' not in o.calls

def test_native_play_confirmation_uses_same_v2_transport_sample_for_id_and_rate():
    c,o=client();c.same_song=Mock(return_value=False)
    with tempfile.TemporaryDirectory() as d:
        b=hrcider.Bridge(client=c,cache=hrcider.Cache(d),volume=Mock());b.state=c.snapshot();b.available=True
        b.command(b'ci\t1\t131072\tloading')
        b.library.play=Mock(side_effect=lambda _: b.stop.set() or {'expected_ids':['123']})
        b.controls.put((b.generation,1,'native-play','play/songs/123'));before=len(o.calls)
        b._control();frames=[];b.poll(lambda _,raw:frames.append(raw))
        assert any(raw.startswith(f'CK\t{b.generation}\t1\t1\t'.encode()) for raw in frames)
        assert o.calls[before:]==['/api/v2/playback'];b.close()

def run_polls(outcomes):
    d=tempfile.TemporaryDirectory();c=Mock();c.ab_tick.return_value=None
    b=hrcider.Bridge(client=c,cache=hrcider.Cache(d.name),volume=Mock())
    # Obtain production field shape; only the polling schedule is adapted.
    real,_=client();state=real.snapshot();state['muted']=False;b.state=state;b.available=True;b.volume.read.return_value=(37,False)
    clock=[0.];stopped=[False];seen=[];publications=[];results=iter(outcomes)
    def sample():
        seen.append(b.available);value=next(results)
        if len(seen)==len(outcomes):stopped[0]=True
        if isinstance(value,Exception):raise value
        return dict(state)
    c.snapshot.side_effect=sample
    b.stop=types.SimpleNamespace(is_set=lambda:stopped[0],wait=lambda seconds:clock.__setitem__(0,clock[0]+seconds),set=lambda:stopped.__setitem__(0,True))
    b._state=lambda *args:publications.append((b.available,b.reason,b.state.copy()))
    with patch.object(hrcider.time,'monotonic',side_effect=lambda:clock[0]):b._poll()
    result=(seen,publications,b.available,b.failures,b.state.copy(),state)
    b.close();d.cleanup();return result

def test_alternating_failed_successful_polls_never_publish_unavailable():
    seen,pubs,available,failures,state,good=run_polls([TimeoutError('poll'),True]*10)
    assert all(seen) and available and failures==0 and state==good
    assert all(available for available,_,_ in pubs)

def test_two_failed_polls_keep_last_good_state_then_recovery_resets_threshold():
    seen,pubs,available,failures,state,good=run_polls([OSError('poll'),TimeoutError('poll'),True,OSError('poll'),True])
    assert all(seen) and available and failures==0 and state==good
    assert all(available for available,_,_ in pubs)

def test_three_consecutive_failures_publish_unavailable_once_and_keep_metadata():
    seen,pubs,available,failures,state,good=run_polls([OSError('poll')]*3)
    assert seen==[True,True,True] and not available and failures==3 and state==good
    assert len(pubs)==1 and pubs[0][0] is False

def test_endpoint_stays_owned_until_ignored_timeout_operation_actually_returns():
    entered=threading.Event();release=threading.Event();c,o=client();original=o.open;counts=[];errors=[]
    def open_(req,timeout):
        if urllib.parse.urlsplit(req.full_url).path=='/api/v2/playback':
            counts.append(timeout);entered.set();assert release.wait(2)
        return original(req,timeout)
    o.open=open_
    def owner():
        try:c.snapshot()
        except BaseException as e:errors.append(e)
    thread=threading.Thread(target=owner);thread.start()
    try:
        assert entered.wait(1);time.sleep(.03) # beyond the specified .01s timeout
        for _ in range(20):
            with pytest.raises(OSError,match='already in flight'):c.snapshot()
        # A different endpoint remains available; there is no global I/O lock.
        assert c.request('/api/v2/playback/time')['data']['currentTime']==12
        assert len(counts)==1
    finally:release.set();thread.join(2)
    assert not thread.is_alive() and not errors and not c.inflight
    assert c.snapshot()['id']=='123'

def test_read_timeout_closes_response_and_releases_endpoint_for_recovery():
    c,o=client()
    class TimeoutResponse(Response):
        def read(self,*args):raise TimeoutError('read deadline')
    response=TimeoutResponse(b'{}');o.open=lambda *args,**kwargs:response
    with pytest.raises(TimeoutError):c.request('/api/v2/playback')
    assert response.closed and not c.inflight
    o.open=Opener().open;assert c.snapshot()['id']=='123'

@pytest.mark.parametrize('raw',[b'null',b'[]',b'"broken"',b'{invalid-json'])
def test_malformed_v2_json_root_falls_back_and_closes_response(raw):
    c,o=client();o.v1_hangs=False;original=o.open;responses=[]
    def open_(req,timeout):
        if urllib.parse.urlsplit(req.full_url).path=='/api/v2/playback':
            response=Response(raw);responses.append(response);return response
        return original(req,timeout)
    o.open=open_;assert c.snapshot()['id']=='123'
    assert all(v.closed for v in responses) and not c.inflight

def test_recovery_after_confirmed_outage_publishes_available_and_resets_failures():
    seen,pubs,available,failures,state,good=run_polls([OSError('poll')]*4+[True])
    assert seen==[True,True,True,False,False] and available and failures==0 and state==good
    assert [value for value,_,_ in pubs]==[False,True]

def test_normal_concurrent_consumers_serialize_without_losing_a_request():
    c,o=client();c.timeout=.5;original=o.open
    entered=threading.Event();release=threading.Event();counter=threading.Lock()
    active=[0];maximum=[0];errors=[];results=[]
    def open_(req,timeout):
        with counter:active[0]+=1;maximum[0]=max(maximum[0],active[0])
        try:
            entered.set();assert release.wait(2)
            return original(req,timeout)
        finally:
            with counter:active[0]-=1
    o.open=open_
    def sample():
        try:results.append(c.snapshot()['id'])
        except BaseException as e:errors.append(e)
    threads=[threading.Thread(target=sample) for _ in range(2)]
    threads[0].start()
    try:
        assert entered.wait(1);threads[1].start();time.sleep(.02)
        assert active[0]==1
    finally:
        release.set()
        for t in threads:
            if t.ident is not None:t.join(2)
    assert results==['123','123'] and not errors and maximum[0]==1
    assert not c.inflight and not c.request_slots
