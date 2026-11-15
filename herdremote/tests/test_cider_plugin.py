"""FU38 offline bridge acceptance. No live Cider/profile/device access."""
import concurrent.futures
import importlib.util
import json
from pathlib import Path
import queue
import sys
import threading
import time
from unittest import mock

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import hrcider
from hrcider_plugin import Backend, PluginError, PluginTransport, install_secret, make_clients
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

STATE = dict(state='playing', nowPlaying=dict(id='one',name='One',durationInMillis=100000,
             playParams={'id':'one'},artwork={'url':'https://example.test/{w}x{h}.jpg'},inLibrary=True),
             time={'currentTime':12},volume=.5,rate=1,repeatMode=0,shuffleMode=0,loading=False)
ORIGIN = 'http://127.0.0.1:10767'


def wait_for(fn, timeout=2):
    end=time.monotonic()+timeout
    while not fn():
        if time.monotonic()>end:raise AssertionError('condition deadline')
        time.sleep(.005)


@pytest.fixture
def transport():
    t=PluginTransport('a'*64,port=0)
    t.start()
    yield t
    t.close()


def peer(t, secret='a'*64, version=1):
    ws=connect(f'ws://127.0.0.1:{t.port}/',origin=ORIGIN,open_timeout=1,compression=None,legacy=True)
    ws.send(json.dumps(dict(v=version,type='hello',secret=secret,state=STATE)))
    return ws


def test_hello_auth_origin_version_and_no_secret_logs(transport,caplog):
    for secret,version in [('b'*64,1),('a'*64,2)]:
        with peer(transport,secret,version) as ws:
            with pytest.raises(ConnectionClosed):ws.recv(timeout=1)
        assert not transport.connected
    with pytest.raises(InvalidStatus):
        connect(f'ws://127.0.0.1:{transport.port}/',origin='https://evil.test')
    with peer(transport) as ws:
        assert json.loads(ws.recv(timeout=1))=={'v':1,'type':'hello','ok':True}
        assert transport.connected
    assert 'a'*64 not in caplog.text and 'b'*64 not in caplog.text


def test_events_cached_snapshot_neighbors_no_polling(transport):
    events=[];transport.callbacks.append(lambda e,t:events.append(e))
    with peer(transport) as ws:
        ws.recv(timeout=1)
        client=hrcider.CiderPluginClient(transport)
        for _ in range(30):assert client.snapshot()['id']=='one'
        with pytest.raises(TimeoutError):ws.recv(timeout=.03)
        state=dict(STATE,time={'currentTime':23},loading=True,queue={'neighbors':[{'id':'one'}]})
        ws.send(json.dumps(dict(v=1,type='event',event='playbackStatus.loadingDidChange',state=state)))
        wait_for(lambda:client.snapshot()['position']==23000)
        assert client.snapshot()['loading'] is True
        assert client.request('/api/v1/playback/queue')==[{'id':'one'}]
        assert events[-1]['type']=='playbackStatus.loadingDidChange'


def test_timeout_request_ids_late_reply_and_pending_bound(transport):
    with peer(transport) as ws:
        ws.recv(timeout=1)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future=pool.submit(transport.request,'/api/v1/playback/next',{},.04)
            request=json.loads(ws.recv(timeout=1))
            with pytest.raises(PluginError,match='timeout'):future.result()
            assert not transport.pending
            ws.send(json.dumps(dict(v=1,type='reply',id=request['id'],data={'status':'ok'})))
            next_call=pool.submit(transport.request,'/api/v2/queue/position',None,.5)
            second=json.loads(ws.recv(timeout=1));assert second['id']!=request['id']
            ws.send(json.dumps(dict(v=1,type='reply',id=second['id'],data={'data':{'total':2}})))
            assert next_call.result()=={'data':{'total':2}}
        with transport.lock:transport.pending.update({str(i):queue.Queue(1) for i in range(16)})
        with pytest.raises(PluginError,match='busy'):transport.request('/x')
        transport.pending.clear()


def test_cider_restart_reconnect_and_pending_release(transport):
    first=peer(transport);first.recv(timeout=1)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        call=pool.submit(transport.request,'/api/v1/playback/next',{},1)
        first.recv(timeout=1);first.close()
        with pytest.raises(PluginError,match='offline'):call.result()
    wait_for(lambda:not transport.connected)
    with peer(transport) as second:
        second.recv(timeout=1)
        assert hrcider.CiderPluginClient(transport).snapshot()['id']=='one'


@pytest.mark.parametrize('reason',['offline','unauthorized'])
def test_short_reason_for_state_and_library(transport,reason):
    from hrcider_library import page_error
    with peer(transport) as ws:
        ws.recv(timeout=1)
        ws.send(json.dumps(dict(v=1,type='event',event='playbackStatus.playbackStateDidChange',state=STATE,reason=reason)))
        wait_for(lambda:transport.reason==reason)
        with pytest.raises(PluginError,match=reason):transport.playback()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result=pool.submit(transport.request,'/api/v1/amapi/run-v3',{'path':'/v1/me/library/albums'},.5)
            request=json.loads(ws.recv(timeout=1))
            ws.send(json.dumps(dict(v=1,type='reply',id=request['id'],error=reason)))
            with pytest.raises(PluginError) as error:result.result()
            assert page_error(error.value)=='Cider plugin: '+reason


def test_large_library_pages_and_oversize_rejection(transport):
    with peer(transport) as ws:
        ws.recv(timeout=1)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future=pool.submit(hrcider.CiderPluginClient(transport).library,'songs',50,50)
            request=json.loads(ws.recv(timeout=1))
            assert 'offset=50' in request['body']['path']
            rows=[dict(id=str(i),attributes={'name':'歌'*2000}) for i in range(50)]
            ws.send(json.dumps(dict(v=1,type='reply',id=request['id'],data={'data':rows,'meta':{'total':10000},'next':'/v1/me/library/songs?offset=100'})))
            assert len(future.result()['data'])==50
        ws.send('x'*(2*1024*1024+1))
        with pytest.raises(ConnectionClosed):ws.recv(timeout=1)
    wait_for(lambda:not transport.connected)


def test_auto_fallback_and_never_replay_mutation(transport):
    rpc=mock.Mock(base=ORIGIN,token=None)
    rpc.snapshot.return_value={'id':'rpc'}
    plugin=hrcider.CiderPluginClient(transport,timeout=.05)
    backend=Backend(rpc,plugin)
    assert backend.snapshot()=={'id':'rpc'}
    with peer(transport) as ws:
        ws.recv(timeout=1)
        assert backend.snapshot()['id']=='one'
        with pytest.raises(PluginError,match='timeout'):backend.control('next')
        request=json.loads(ws.recv(timeout=1));assert request['path'].endswith('/next')
        rpc.control.assert_not_called()
    wait_for(lambda:not transport.connected)
    assert backend.snapshot()=={'id':'rpc'}
    forced=Backend(rpc,plugin,'plugin')
    with pytest.raises(PluginError):forced.snapshot()


def test_secret_0600_persistent_and_no_symlinks(tmp_path):
    p=tmp_path/'config/bridge.secret'
    a=install_secret(p)
    assert len(a)==64 and a==install_secret(p)
    assert p.stat().st_mode&0o777==0o600
    p.chmod(0o644)
    with pytest.raises(ValueError,match='0600'):install_secret(p)
    link=tmp_path/'link';link.symlink_to(p)
    with pytest.raises(OSError):install_secret(link)


def test_config_switch_rpc_never_creates_secret(tmp_path):
    result=make_clients(hrcider.Client,{'CIDER_BACKEND':'rpc','CIDER_PLUGIN_SECRET_FILE':str(tmp_path/'absent')})
    assert type(result) is hrcider.Client
    assert not (tmp_path/'absent').exists()
    with pytest.raises(ValueError):make_clients(hrcider.Client,{'CIDER_BACKEND':'typo'})


def test_installer_temporary_profile_backup_and_no_asset_secret(tmp_path,capsys):
    source=Path(__file__).resolve().parents[2]/'cider-plugin/install.py'
    spec=importlib.util.spec_from_file_location('fu38_installer',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    profile=tmp_path/'profile';secret=tmp_path/'config/key'
    import hashlib,shutil
    staged=tmp_path/'source';shutil.copytree(source.parent,staged)
    module.SOURCE=staged
    assets=tmp_path/'assets';assets.mkdir()
    (assets/'fixture.js').write_text('offline asset')
    (staged/'host-assets.json').write_text(json.dumps({'fixture.js':hashlib.sha256((assets/'fixture.js').read_bytes()).hexdigest()}))
    module.install(profile,secret,10768,assets)
    target=profile/'plugins'/module.IDENTIFIER
    value=secret.read_text().strip()
    assert value not in (target/'connection.mjs').read_text()
    (target/'existing-file').write_text('keep me')
    module.install(profile,secret,10768,assets)
    backups=list((profile/'podbox-backups').iterdir())
    assert len(backups)==1 and (backups[0]/'existing-file').read_text()=='keep me'
    assert secret.stat().st_mode&0o777==0o600
    assert value not in capsys.readouterr().out
    with pytest.raises(FileNotFoundError):module.install(profile,secret,10768,tmp_path/'wrong-assets')


def test_python_interface_to_real_node_plugin_adapter():
    transport=PluginTransport('a'*64,port=0,origins=[None])
    transport.start()
    import subprocess
    source=Path(__file__).resolve().parents[2]/'cider-plugin/integration-peer.mjs'
    node=subprocess.Popen(['node',str(source)],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    node.stdin.write(json.dumps({'url':f'ws://127.0.0.1:{transport.port}/','secret':'a'*64}));node.stdin.close()
    # Node's built-in WebSocket has no browser Origin. This test explicitly
    # allows absent Origin; production keeps the exact Cider Origin check.
    try:
        wait_for(lambda:transport.connected or node.poll() is not None)
        assert transport.connected, node.stderr.read() if node.poll() is not None else 'not connected'
        client=hrcider.CiderPluginClient(transport)
        assert client.snapshot()['id']=='one'
        client.control('seek',25000)
        wait_for(lambda:client.snapshot()['position']==25000)
        client.control('volume',60)
        client.control('shuffle',1);client.control('repeat',2)
        assert client.library('songs',50,50)['meta']['total']==2
        assert client.library_path('/v1/me/library/search?term=hello')['results']['library-songs']['data'][0]['id']=='one'
        result=client.play_collection('library-albums','album',{'two'},1)
        assert result['expected_ids']==['two'] and client.queue_state()[0]==1
        client.control('love');client.control('unlove');client.control('add-library')
        client.control('rate',1250)
        assert client.request('/api/v2/playback/rate')['data']['rate']==1.25
    finally:
        node.terminate()
        try:node.wait(timeout=3)
        except subprocess.TimeoutExpired:node.kill();node.wait(timeout=3)
        node.stderr.close()
        transport.close()


def test_plugin_loading_events_are_authoritative_over_stream_metadata(transport,tmp_path):
    client=hrcider.CiderPluginClient(transport)
    bridge=hrcider.Bridge(client=client,cache=hrcider.Cache(tmp_path/'cache'),volume=mock.Mock())
    try:
        with peer(transport) as ws:
            ws.recv(timeout=1)
            bridge.state=client.snapshot();bridge.available=True
            ws.send(json.dumps(dict(v=1,type='event',event='playbackStatus.loadingDidChange',state=dict(STATE,loading=True))))
            wait_for(lambda:client.snapshot()['loading'])
            bridge._on_event({'type':'playbackStatus.loadingDidChange'},time.monotonic())
            assert bridge.loading.value is True
            bridge._on_stream({'running':True},time.monotonic())
            assert bridge.loading.value is True
            ws.send(json.dumps(dict(v=1,type='event',event='playbackStatus.loadingDidChange',state=STATE)))
            wait_for(lambda:not client.snapshot()['loading'])
            bridge._on_event({'type':'playbackStatus.loadingDidChange'},time.monotonic())
            assert bridge.loading.value is False
    finally:bridge.close()


def test_stalled_sender_does_not_extend_request_deadline(transport):
    with peer(transport) as ws:
        ws.recv(timeout=1)
        release=threading.Event()
        original=transport.ws.send
        def slow_send(raw):
            release.wait(.5)
            original(raw)
        try:
            with mock.patch.object(transport.ws,'send',side_effect=slow_send):
                start=time.monotonic()
                with pytest.raises(PluginError,match='timeout'):transport.request('/api/v1/playback/next',{},.03)
                assert time.monotonic()-start<.2
                assert not transport.pending
                release.set()
        finally:release.set()


def test_renderer_origin_is_independent_of_rpc_and_strict(tmp_path):
    from hrcider_plugin import renderer_origin
    env={'CIDER_API_URL':'http://127.0.0.1:19999','CIDER_PLUGIN_SECRET_FILE':str(tmp_path/'secret')}
    backend=make_clients(hrcider.Client,env)
    assert backend.plugin.transport.origins==['http://127.0.0.1:10767']
    backend=make_clients(hrcider.Client,dict(env,CIDER_PLUGIN_RENDERER_ORIGIN='http://localhost:19000/'))
    assert backend.plugin.transport.origins==['http://localhost:19000']
    assert renderer_origin('http://LOCALHOST:80/')=='http://localhost'
    for origin in ['null','*','https://evil.test','http://127.0.0.1.evil.test',
                   'http://user@127.0.0.1:19000','http://127.0.0.1:19000/path']:
        with pytest.raises(ValueError):renderer_origin(origin)


def test_explicit_renderer_origin_handshake_and_wrong_plugin_origin_rejected():
    origin='http://127.0.0.1:19501'
    transport=PluginTransport('a'*64,port=0,origins=[origin]);transport.start()
    try:
        with pytest.raises(InvalidStatus):connect(f'ws://127.0.0.1:{transport.port}/',origin=ORIGIN)
        with connect(f'ws://127.0.0.1:{transport.port}/',origin=origin) as ws:
            ws.send(json.dumps(dict(v=1,type='hello',secret='a'*64,state=STATE)))
            assert json.loads(ws.recv(timeout=1))['ok'] is True
    finally:transport.close()


def test_installer_prefers_active_ota_and_rejects_changed_bytes(tmp_path):
    import hashlib,shutil,zipfile
    source=Path(__file__).resolve().parents[2]/'cider-plugin/install.py'
    spec=importlib.util.spec_from_file_location('fu38_ota_installer',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    staged=tmp_path/'source';shutil.copytree(source.parent,staged);module.SOURCE=staged
    profile=tmp_path/'profile';profile.mkdir();secret=tmp_path/'secret'
    raw=b'known active renderer entry'
    (staged/'host-assets.json').write_text(json.dumps({'index-fixture.js':hashlib.sha256(raw).hexdigest()}))
    with zipfile.ZipFile(profile/'update.cup','w') as z:z.writestr('spa/assets/index-fixture.js',raw)
    module.install(profile,secret,10768)
    target=profile/'plugins'/module.IDENTIFIER
    assert (target/'renderer.mjs').exists()
    assert 'index-fixture.js' in (target/'connection.mjs').read_text()
    with zipfile.ZipFile(profile/'update.cup','w') as z:z.writestr('spa/assets/index-fixture.js',b'changed')
    before=(target/'plugin.js').read_bytes()
    with pytest.raises(ValueError,match='Unsupported Cider'):module.install(profile,secret,10768)
    assert (target/'plugin.js').read_bytes()==before
    assert not (profile/'podbox-backups').exists()
