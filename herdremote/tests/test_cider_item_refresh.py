"""FU38 live fix 3: metadata events and coherent CN, with an offline renderer peer."""
import concurrent.futures
import copy
import hashlib
import json
import logging
from pathlib import Path
import sys
import threading
import time
from unittest.mock import Mock,patch
import pytest
from websockets.sync.client import connect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hrcider import Bridge,Cache,Client,CiderPluginClient
from hrcider_plugin import Backend,PluginError,PluginTransport


def state(key):
    return dict(state='playing',nowPlaying=dict(id=key,name='private title '+key,artistName='Artist',albumName='Album',
                durationInMillis=100000,playParams={'id':key},artwork={'url':'https://example.test/{w}x{h}bb.jpg'}),
                time={'currentTime':1},volume=.5,rate=1,repeatMode=0,shuffleMode=0,loading=False)


def wait(fn):
    end=time.monotonic()+2
    while not fn():
        if time.monotonic()>end:raise AssertionError('event deadline')
        time.sleep(.005)


@pytest.fixture
def transport():
    t=PluginTransport('a'*64,port=0);t.start()
    yield t
    t.close()


def peer(t):
    ws=connect(f'ws://127.0.0.1:{t.port}/',origin='http://127.0.0.1:10767',compression=None,legacy=True)
    ws.send(json.dumps(dict(v=1,type='hello',secret='a'*64,state=state('old'))))
    assert json.loads(ws.recv(timeout=1))['ok']
    return ws


def reply(ws,request,raw):
    assert request['method']=='GET' and request['path']=='/api/v2/playback' and request['body'] is None
    ws.send(json.dumps(dict(v=1,type='reply',id=request['id'],data={'data':raw})))


@pytest.mark.parametrize('operation',['auto-advance','next','previous','queue-jump'])
@pytest.mark.parametrize('source',['rpc','plugin'])
def test_item_hint_refreshes_whole_snapshot_before_cn_without_other_events(transport,tmp_path,operation,source):
    with peer(transport) as ws:
        plugin=CiderPluginClient(transport,timeout=1);rpc=Mock(base='http://127.0.0.1:1',token=None)
        b=Bridge(client=Backend(rpc,plugin,'plugin'),cache=Cache(tmp_path),volume=Mock())
        b.volume.read.return_value=(50,False);b.command(b'ci\t1\t131072');b.available=True
        b.state=plugin.snapshot();b.library.state=b.state
        frames=[];original=b.scheduler.push
        def push(priority,kind,payload,*args,**kwargs):
            original(priority,kind,payload,*args,**kwargs)
            if payload.startswith(b'CN\t'):
                frames.append(payload.decode().split('\t'));b.stop.set()
        b.scheduler.push=push
        transport.callbacks.append(b._on_event)
        new=state(operation)
        if source=='plugin':
            ws.send(json.dumps(dict(v=1,type='event',event='playbackStatus.nowPlayingItemDidChange',state=new)))
            wait(b.item_refresh.is_set)
        else:b._on_event({'type':'playbackStatus.nowPlayingItemDidChange','source':'rpc'},time.monotonic())
        # The callback must not make a blocking request on its own receiver.
        assert not transport.pending
        assert b.state['id']=='old' and b.state['title']=='private title old'
        worker=threading.Thread(target=b._poll,daemon=True);worker.start()
        request=json.loads(ws.recv(timeout=1));reply(ws,request,new)
        worker.join(2);assert not worker.is_alive()
        assert frames and all(f[10]==operation and f[13]=='private title '+operation for f in frames)
        assert b.state['id']==operation and b.state['title']=='private title '+operation
        assert not b.item_refresh.is_set()
        rpc.request.assert_not_called();rpc.control.assert_not_called()
        transport.callbacks.remove(b._on_event);b.close()


def test_applied_item_hash_seq_order_and_late_refresh_reply(transport,caplog):
    caplog.set_level(logging.INFO,logger='herdremote.cider')
    with peer(transport) as ws:
        plugin=CiderPluginClient(transport,timeout=1)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            fresh=pool.submit(plugin.refresh_snapshot);request=json.loads(ws.recv(timeout=1))
            reply(ws,request,state('reply-item'))
            ws.send(json.dumps(dict(v=1,type='event',event='playbackStatus.nowPlayingItemDidChange',state=state('newer-event'))))
            wait(lambda:plugin.snapshot()['id']=='newer-event');fresh.result()
            assert plugin.snapshot()['id']=='newer-event'
            late=pool.submit(transport.refresh_snapshot,.03);request=json.loads(ws.recv(timeout=1))
            with pytest.raises(PluginError,match='timeout'):late.result()
            reply(ws,request,state('late-old-item'));time.sleep(.04)
            assert plugin.snapshot()['id']=='newer-event'
        lines=[r.getMessage() for r in caplog.records if 'Cider plugin item applied' in r.getMessage()]
        assert len(lines)==3
        for i,key in enumerate(['old','reply-item','newer-event'],1):
            assert 'id_hash='+hashlib.sha256(key.encode()).hexdigest()[:12] in lines[i-1]
            assert 'seq='+str(i) in lines[i-1]
        assert 'private title' not in caplog.text and 'reply-item' not in caplog.text and 'a'*64 not in caplog.text


def test_fast_sample_never_pairs_new_identity_with_old_metadata(transport,tmp_path):
    with peer(transport) as ws:
        plugin=CiderPluginClient(transport);b=Bridge(client=plugin,cache=Cache(tmp_path),volume=Mock())
        b.state=plugin.snapshot();b.available=True
        ws.send(json.dumps(dict(v=1,type='event',event='playbackStatus.playbackTimeDidChange',state=state('new'))))
        wait(lambda:plugin.snapshot()['id']=='new');b._fast_sample()
        assert b.state['id']=='old' and b.state['title']=='private title old'
        assert b.item_refresh.is_set() and b.wake.is_set();b.close()


def test_refresh_failure_retains_pending_hint_until_success(tmp_path):
    client=Mock(using_plugin=True);raw=state('new');canonical=Client()._snapshot_data(raw)
    client.snapshot.return_value=canonical;client.loading_state.return_value=False;client.ab_tick.return_value=None
    client.refresh_snapshot.side_effect=[OSError('offline'),None]
    b=Bridge(client=client,cache=Cache(tmp_path),volume=Mock());b.opened=True;b.available=True
    b.state=Client()._snapshot_data(state('old'));b.volume.read.return_value=(50,False)
    b._on_event({'type':'playbackStatus.nowPlayingItemDidChange'},time.monotonic())
    b._state=lambda *a:b.stop.set()
    worker=threading.Thread(target=b._poll,daemon=True);worker.start();worker.join(2)
    assert not worker.is_alive() and client.refresh_snapshot.call_count==2
    assert b.state['id']=='new' and not b.item_refresh.is_set();b.close()


def test_short_seek_reanchors_remote_clock(tmp_path):
    client = Mock(using_plugin=False, explicit_loading=False)
    raw = state('one')
    raw['state'] = 'paused'
    before = Client()._snapshot_data(raw)
    after = dict(before, position=before['position']+200)
    client.snapshot.side_effect = [before, after]
    client.ab_tick.return_value = None
    bridge = Bridge(client=client, cache=Cache(tmp_path), volume=Mock())
    bridge.volume.read.return_value = (50, False)
    assert bridge.command(b'ci\t1\t131072')
    frames = []
    push = bridge.scheduler.push

    def record(priority, kind, payload, *args, **kwargs):
        push(priority, kind, payload, *args, **kwargs)
        if payload.startswith(b'CN\t'):
            frames.append(int(payload.split(b'\t')[5]))
            if len(frames) == 2:
                bridge.stop.set()

    bridge.scheduler.push = record
    worker = threading.Thread(target=bridge._poll, daemon=True)
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    assert frames == [1000, 1200]
    bridge.stop.clear()
    bridge._loading_begin = Mock(return_value=False)
    client.control.side_effect = lambda *_: bridge.stop.set()
    assert bridge.command(f'cc\t{bridge.generation}\t99\tseek\t1400'.encode())
    bridge._control()
    assert bridge.item_refresh.is_set()
    client.control.assert_called_once_with('seek','1400')
    bridge.close()


@pytest.mark.parametrize('plugin', [False, True])
def test_seek_publishes_the_landing_after_a_stale_snapshot(tmp_path, plugin):
    client = Mock(using_plugin=plugin, explicit_loading=False)
    client.ab_tick.return_value = None
    client.loading_state.return_value = False
    before = Client()._snapshot_data(state('one'))
    target = before['position'] + 600
    bridge = Bridge(client=client, cache=Cache(tmp_path), volume=Mock())
    bridge.volume.read.return_value = (50, False)
    bridge.command(b'ci\t1\t131072')
    bridge.state, bridge.available = before, True

    # A completed RPC can precede the new position. With the running
    # clock's usual 2s tolerance this seek could go unpublished.
    client.control.side_effect = lambda *_: bridge.stop.set()
    bridge.command(f'cc\t{bridge.generation}\t99\tseek\t{target}'.encode())
    bridge._control()
    client.control.assert_called_once_with('seek', str(target))
    bridge.stop.clear()
    times = []
    sent = []
    publish = bridge.scheduler.push

    def snapshot():
        times.append(time.monotonic())
        if len(times) == 1:
            return dict(before)
        return dict(before, position=target)

    def push(priority, kind, payload, *args, **kwargs):
        publish(priority, kind, payload, *args, **kwargs)
        if payload.startswith(b'CN\t'):
            sent.append(int(payload.split(b'\t')[5]))
            if sent[-1] == target:
                bridge.stop.set()

    client.snapshot.side_effect = snapshot
    bridge.scheduler.push = push
    thread = threading.Thread(target=bridge._poll, daemon=True)
    thread.start()
    try:
        thread.join(1)
        assert sent == [before['position'], target]
        assert len(times) == 2
        assert times[1] - times[0] < .22
        assert client.refresh_snapshot.call_count == (2 if plugin else 0)
    finally:
        bridge.close()
        thread.join(1)


@pytest.mark.parametrize('mode',['auto','plugin'])
def test_rpc_item_hints_still_watched_while_plugin_is_selected(mode):
    rpc=Mock(base='http://127.0.0.1:1',token=None);plugin=Mock();plugin.transport.connected=True;plugin.transport.callbacks=[]
    backend=Backend(rpc,plugin,mode);stop=threading.Event();seen=[]
    def watch(client,worker_stop,callback):
        assert client is rpc
        callback({'type':'playbackStatus.nowPlayingItemDidChange'},time.monotonic());stop.set()
    with patch('hrcider_events.playback_events',watch):backend.watch_events(stop,lambda event,stamp:seen.append(event))
    assert seen==[{'type':'playbackStatus.nowPlayingItemDidChange','source':'rpc'}]
    assert not plugin.transport.callbacks
