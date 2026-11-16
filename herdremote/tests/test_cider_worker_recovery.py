"""FU38 live fix 2: schema parity, backend transitions and actual worker faults."""
import copy
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from unittest.mock import Mock, patch

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import hrcider
from hrcider_plugin import Backend, PluginTransport

KEYS={'id','repeat5','title','artist','album','composer','duration','position','rate','volume',
      'repeat','shuffle','art','template','bg'}
TEXT={'id','title','artist','album','composer','art','template','bg'}


def playback():
    return dict(state='playing',nowPlaying=dict(id='item',name='Title',artistName='Artist',albumName='Album',
        composerName='Composer',durationInMillis=100000,playParams={'id':'i.library','catalogId':'123','libraryId':'l.other'},
        artwork={'url':'https://example.test/{w}x{h}bb.{f}','bgColor':'123abc'}),
        time={'currentTime':12.345},volume=.37,repeatMode=2,shuffleMode=1,rate=1.25)


class Transport:
    connected=True
    def __init__(self,data=None):self.data=playback() if data is None else data
    def playback(self):return copy.deepcopy(self.data)
    def close(self):self.connected=False


def clients(data):
    rpc=hrcider.Client()
    rpc.request=Mock(side_effect=lambda *a,**k:{'data':copy.deepcopy(data)})
    raw=copy.deepcopy(data)
    raw.update(loading=True,queue={'position':0,'total':2,'neighbors':[]})
    raw['nowPlaying'].update(inFavorites=True,inLibrary=True)
    return rpc,hrcider.CiderPluginClient(Transport(raw))


def run_thread(bridge,method):
    thread=threading.Thread(target=getattr(bridge,method),name='test-'+method,daemon=True)
    thread.start();thread.join(3)
    if thread.is_alive():bridge.stop.set();thread.join(1);pytest.fail('worker did not finish its injected recovery')
    return thread


@pytest.fixture
def bridge(tmp_path):
    client=Mock();client.ab_tick.return_value=None
    value=hrcider.Bridge(client=client,cache=hrcider.Cache(tmp_path/'cache'),volume=Mock())
    value.volume.read.return_value=(37,False)
    value.state=hrcider.Client()._snapshot_data(playback());value.available=True
    value.opened=True;value.generation=1
    yield value
    value.close()


def test_plugin_snapshot_exact_rpc_keys_types_and_private_metadata():
    rpc,plugin=clients(playback())
    a,b=rpc.snapshot(),plugin.snapshot()
    assert a==b and set(a)==KEYS
    assert {k:type(v) for k,v in a.items()}=={k:type(v) for k,v in b.items()}
    assert all(type(b[k]) is str for k in TEXT)
    assert all(type(b[k]) is int for k in KEYS-TEXT)
    assert (b['id'],b['position'],b['duration'],b['rate'],b['volume'],b['repeat'],b['shuffle'],b['repeat5'])==('i.library',12345,100000,1250,37,2,1,1)
    assert plugin.loading_state() is True and rpc.loading_state() is None
    assert plugin.now_playing()['inFavorites'] is True and plugin.now_playing()['inLibrary'] is True
    assert plugin.transport_snapshot()['ids']=={'item','i.library','123','l.other'}
    assert b['template']=='https://example.test/{w}x{h}bb.{f}' and b['art']==hrcider.art_id(b['template'])


@pytest.mark.parametrize('state',['paused','stopped','waiting'])
def test_nonplaying_rate_is_zero_for_both_backends(state):
    raw=playback();raw['state']=state
    rpc,plugin=clients(raw);assert rpc.snapshot()==plugin.snapshot()
    assert plugin.snapshot()['rate']==0


@pytest.mark.parametrize('position,volume,expected',[(150,.5,(100000,50)),(-2,-.5,(0,0)),(0,2,(0,100))])
def test_position_clamp_millisecond_units_and_volume_range(position,volume,expected):
    raw=playback();raw.update(time={'currentTime':position},volume=volume)
    rpc,plugin=clients(raw);assert rpc.snapshot()==plugin.snapshot()
    result=plugin.snapshot();assert (result['position'],result['volume'])==expected


@pytest.mark.parametrize('repeat,repeat5',[(0,0),(1,2),(2,1)])
def test_repeat_shuffle_and_virtual_mode_contract(repeat,repeat5):
    raw=playback();raw.update(repeatMode=repeat,shuffleMode=0)
    rpc,plugin=clients(raw);assert rpc.snapshot()==plugin.snapshot()
    assert plugin.snapshot()['repeat5']==repeat5
    for client in (rpc,plugin):client.virtual='shuffle';client.expect=(repeat,0)
    assert rpc.snapshot()['repeat5']==plugin.snapshot()['repeat5']==3


def test_unknown_optional_values_default_and_foreign_top_level_keys_do_not_escape():
    raw=playback();raw['nowPlaying']=dict(name=None,artistName=3,albumName={},composerName=[],
        id=None,playParams={'id':{}},artwork={'url':None,'bgColor':None})
    raw['time']={'currentTime':0}
    rpc,plugin=clients(raw);a,b=rpc.snapshot(),plugin.snapshot()
    assert a==b and set(b)==KEYS
    assert all(b[k]=='' for k in ('id','title','artist','album','composer','template'))
    assert b['bg']=='181818' and b['art']=='-' and b['duration']==b['position']==0
    for key in ('volume','rate','repeatMode','shuffleMode'):plugin.transport.data.pop(key,None)
    unknown=plugin.snapshot()
    assert set(unknown)==KEYS
    assert (unknown['volume'],unknown['rate'],unknown['repeat'],unknown['shuffle'])==(0,1000,0,0)


def test_real_js_adapter_wire_snapshot_normalizes_to_rpc(tmp_path):
    fixture=Path(__file__).resolve().parents[2]/'cider-plugin/mock-host.mjs'
    script='import {makeHost} from '+json.dumps(fixture.as_uri())+';process.stdout.write(JSON.stringify(makeHost().host.snapshot()));'
    wire=json.loads(subprocess.check_output(['node','--input-type=module','-e',script],text=True))
    rpc=hrcider.Client();rpc.request=Mock(return_value={'data':wire})
    plugin=hrcider.CiderPluginClient(Transport(wire))
    assert plugin.snapshot()==rpc.snapshot()
    assert set(plugin.snapshot())==KEYS and 'loading' not in plugin.snapshot()


def test_poll_rpc_plugin_rpc_with_differing_legacy_keys_publishes_each_transition(bridge):
    rpc,plugin=clients(playback());plugin.transport.connected=False
    plugin.transport.data['nowPlaying']['playParams']['id']='plugin-item'
    original=rpc.snapshot
    rpc.snapshot=lambda:dict(original(),legacy_only=True)
    backend=Backend(rpc,plugin);bridge.client=backend;bridge.available=False
    seen=[]
    def publish(*args):
        seen.append(dict(bridge.state))
        if len(seen)==1:plugin.transport.connected=True
        elif len(seen)==2:plugin.transport.connected=False
        else:bridge.stop.set()
    bridge._state=publish
    run_thread(bridge,'_poll')
    assert [s['id'] for s in seen]==['i.library','plugin-item','i.library']
    assert ['legacy_only' in s for s in seen]==[True,False,True]
    assert all('inFavorites' not in s and 'inLibrary' not in s and 'loading' not in s for s in seen)
    assert bridge.available and bridge.failures==0


@pytest.mark.parametrize('error',[RuntimeError,KeyError,TypeError,AssertionError])
def test_poll_survives_repeated_unexpected_snapshot_and_logs_once(bridge,caplog,error):
    good=dict(bridge.state);calls=[]
    def snapshot():
        calls.append(1)
        if len(calls)<=2:raise error('must-not-log-credential-or-private-body')
        return dict(good)
    bridge.client.snapshot=snapshot
    bridge._state=lambda *args:bridge.stop.set()
    run_thread(bridge,'_poll')
    assert len(calls)==3 and bridge.available and bridge.failures==0 and bridge.state['id']==good['id']
    faults=[r for r in caplog.records if 'Cider worker exception' in r.getMessage()]
    assert len(faults)==1 and 'must-not-log' not in caplog.text


def test_index_worker_retries_after_fault_and_stops_interruptibly(bridge,caplog):
    calls=[]
    def step():
        calls.append(1)
        if len(calls)<=2:raise RuntimeError('private')
        bridge.stop.set()
    bridge.index.step=step
    run_thread(bridge,'_index')
    assert len(calls)==3 and caplog.text.count('Cider worker exception')==1


def test_page_worker_sends_failed_page_then_handles_next_job(bridge,caplog):
    bridge.pages=queue.Queue(2)
    for req in (1,2):bridge.pages.put((1,req,0,10,'recent',bridge.page_token))
    def page(*args,**kwargs):
        if bridge.pages.qsize():raise RuntimeError('private')
        bridge.stop.set();return dict(generation=0,offset=0,total=0,stale=False,items=[])
    bridge.library.page=page
    run_thread(bridge,'_pages')
    frames=[entry[5] for entry in bridge.scheduler.heap]
    assert any(f.startswith(b'CL\t1\t1\t') for f in frames)
    assert any(f.startswith(b'CL\t1\t2\t') for f in frames)
    assert caplog.text.count('Cider worker exception')==1


def test_control_worker_never_replays_unexpected_failure_and_sends_failure_ack(bridge,caplog):
    calls=[]
    for req in (1,2):bridge.controls.put((1,req,'toggle',0))
    def control(*args):
        calls.append(args)
        if len(calls)==1:raise RuntimeError('private')
        bridge.stop.set();return {}
    bridge.client.control=control
    run_thread(bridge,'_control')
    frames=[entry[5] for entry in bridge.scheduler.heap]
    assert len(calls)==2 and any(f.startswith(b'CK\t1\t1\t0\t') for f in frames)
    assert any(f.startswith(b'CK\t1\t2\t1\t') for f in frames)
    assert caplog.text.count('Cider worker exception')==1


def test_prefetch_worker_clears_failed_reservation_and_handles_next_job(bridge,caplog):
    pairs=[('a'*64,'L'),('b'*64,'L')];bridge.warm_window=set(pairs);bridge.warm_jobs={pair:1 for pair in pairs}
    for pair in pairs:bridge.prefetch.put((1,pair))
    calls=[]
    def artwork(*args):
        calls.append(args)
        if len(calls)==1:raise RuntimeError('private')
        bridge.stop.set();return b'data'
    bridge.artwork=artwork
    run_thread(bridge,'_prefetch_worker')
    assert len(calls)==2 and pairs[0] not in bridge.warm_jobs
    assert caplog.text.count('Cider worker exception')==1


def test_art_worker_returns_error_ack_and_continues(bridge,caplog):
    tokens=[object(),object()];bridge.requests={1:tokens[0],2:tokens[1]}
    for req in (1,2):bridge.jobs.put((1,0,req,1,req,'a'*64,'L',0,tokens[req-1]))
    calls=[]
    def artwork(*args):
        calls.append(args)
        if len(calls)==1:raise RuntimeError('private')
        bridge.stop.set();return b'data'
    bridge.artwork=artwork
    run_thread(bridge,'_work')
    assert len(calls)==2 and not bridge.requests and 2 in bridge.scheduler.live
    assert any(entry[5].startswith(b'CK\t1\t1\t0\t') for entry in bridge.scheduler.heap)
    assert caplog.text.count('Cider worker exception')==1 and 'private' not in caplog.text


def test_neighbors_worker_continues_after_unexpected_exception(bridge,caplog):
    bridge.capabilities={'neighbors'};calls=[]
    def request(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:raise RuntimeError('private')
        bridge.stop.set();return []
    bridge.client.request=request
    run_thread(bridge,'_neighbors')
    assert len(calls)==2 and caplog.text.count('Cider worker exception')==1


@pytest.mark.parametrize('method,patch_name',[('_event_worker','playback_events'),('_stream_worker','AudioStream.watch')])
def test_event_and_stream_workers_recover(bridge,caplog,method,patch_name):
    calls=[]
    def watch(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:raise RuntimeError('private')
        bridge.stop.set()
    with patch('hrcider_events.'+patch_name,side_effect=watch):run_thread(bridge,method)
    assert len(calls)==2 and caplog.text.count('Cider worker exception')==1


def test_plugin_sender_survives_bad_job_and_processes_next_without_replay(caplog):
    transport=PluginTransport('a'*64,port=0);sent=[]
    def send(raw):sent.append(raw);transport.sender_stop.set()
    ws=Mock(send=send);transport.ws=ws;transport.pending['1']=queue.Queue(1)
    transport.outgoing.put(('malformed',));transport.outgoing.put((ws,'1','valid',time.monotonic()+2))
    thread=threading.Thread(target=transport._send_worker,name='test-plugin-sender',daemon=True)
    thread.start();thread.join(2);transport.sender_stop.set();thread.join(1)
    assert not thread.is_alive() and sent==['valid'] and caplog.text.count('Cider worker exception')==1


def test_rpc_event_loop_logs_unexpected_fault_once_and_recovers(bridge,caplog):
    from unittest.mock import MagicMock
    bridge.client=hrcider.Client()
    ws=MagicMock()
    def receive(*args,**kwargs):bridge.stop.set();return '40'
    ws.recv.side_effect=receive
    context=MagicMock();context.__enter__.return_value=ws
    with patch('websockets.sync.client.connect',side_effect=[RuntimeError('private'),RuntimeError('private'),context]) as connect:
        run_thread(bridge,'_event_worker')
    assert connect.call_count==3 and caplog.text.count('Cider worker exception')==1
    assert 'private' not in caplog.text


def test_event_callback_faults_do_not_kill_plugin_connection_and_log_once(caplog):
    transport=PluginTransport('a'*64,port=0)
    calls=[]
    def broken(*args):calls.append('bad');raise RuntimeError('private')
    transport.callbacks=[broken,lambda *args:calls.append('good')]
    transport._emit('event');transport._emit('event')
    assert calls==['bad','good','bad','good']
    assert caplog.text.count('Cider worker exception')==1 and 'private' not in caplog.text


@pytest.mark.parametrize('name,number',[('none',0),('one',1),('all',2)])
@pytest.mark.parametrize('shuffle',[False,True])
def test_current_rpc_named_repeat_and_boolean_shuffle_match_numeric_plugin(name,number,shuffle):
    rpc_wire=playback();rpc_wire.update(repeatMode=name,shuffleMode=shuffle)
    plugin_wire=playback();plugin_wire.update(repeatMode=number,shuffleMode=int(shuffle))
    rpc=hrcider.Client();rpc.request=Mock(return_value={'data':rpc_wire})
    plugin=hrcider.CiderPluginClient(Transport(plugin_wire))
    assert rpc.snapshot()==plugin.snapshot()
    assert rpc.request.call_args_list[0].args[0]=='/api/v2/playback'
    assert rpc.request.call_count==1
    assert rpc_wire['repeatMode']==name and type(rpc_wire['shuffleMode']) is bool


def test_unknown_repeat_mode_still_fails_validation():
    raw=playback();raw['repeatMode']='unknown'
    with pytest.raises(ValueError,match='modes'):hrcider.Client._playback_valid(raw,True)
