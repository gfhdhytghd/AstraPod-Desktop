"""FU44 offline: private IPC, authorization, keys and mocked voice only."""
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).parents[1]))
sys.path.insert(0,str(Path(__file__).parents[2]))
import hrdisk
import hrfallback
from usb_remote_key import host_fallback


class Fixture:
    def __init__(self):
        self.now = 10.0
        self.keys = Mock(); self.keys.request.return_value = None
        self.voice = Mock(engaged=False); self.voice.press.return_value = (True,'Talking')
        self.executor = hrfallback.Executor(self.keys,self.voice,clock=lambda:self.now)
        self.client = host_fallback.Client(clock=lambda:self.now)
        self.link = None
        self.client.request = self.request

    def request(self, op, **fields):
        self.client.sequence += 1
        msg = dict(op=op,id=self.client.identity,seq=self.client.sequence,sent=self.now,**fields)
        if op != 'status':msg['token'] = self.client.token
        return json.loads(self.executor.handle(b'ir-'+json.dumps(msg).encode(),self.link))

    def fallback(self, ep0=False, disk=False):
        assert self.client.status(ep0,disk) == 'pending'
        self.now += .5; self.client.status(ep0,disk)
        self.now += .5
        assert self.client.status(ep0,disk) == 'host-fallback'


def wait_for(predicate):
    end=time.monotonic()+1
    while time.monotonic()<end:
        if predicate():return
        time.sleep(.002)
    assert predicate()


def test_debounce_ep0_session_and_disk_recovery():
    f=Fixture()
    f.link=SimpleNamespace(opened=True)
    assert f.client.status(True,False)=='ipod'
    f.now+=.1;assert f.client.status(False,False)=='ipod'
    f.now+=.5;assert f.client.status(True,False)=='ipod' # transient failure reset
    f.now+=.1;assert f.client.status(True,True)=='ipod'
    f.now+=.5;f.client.status(True,True)
    f.now+=.5;assert f.client.status(True,True)=='host-fallback'
    f.now+=.01;assert f.client.status(True,False)=='ipod'
    f.link=None
    f.now+=.01;f.client.status(True,False)
    f.now+=.5;f.client.status(True,False)
    f.now+=.5;assert f.client.status(True,False)=='host-fallback'
    f.link=SimpleNamespace(opened=True)
    assert f.client.status(True,False)=='ipod'
    f.executor.close()


def test_keys_exact_counts_guarded_and_no_replay():
    f=Fixture();f.fallback()
    for key,counts in [('bs',(1,)),('enter',(1,)),('left',(1,3,10)),('right',(1,3,10)),('up',(1,3)),('down',(1,3))]:
        for count in counts:
            assert f.client.action('key',key=key,count=count)['ok']
            args=f.keys.request.call_args
            assert args.args==('',key,count)
            assert args.kwargs['scaled'] is False and args.kwargs['silent'] is True
    guard=f.keys.request.call_args.kwargs['guard']
    assert guard();f.now+=.101;assert not guard()
    f.keys.request.reset_mock()
    for key,count in [('text',1),('enter',10),('up',10),('left',32)]:
        assert not f.client.action('key',key=key,count=count)['ok']
    f.keys.request.assert_not_called()
    f.executor.close()


def test_token_owner_sequence_timestamp_and_connected_gate():
    f=Fixture();f.fallback()
    raw=dict(op='key',id=f.client.identity,seq=100,sent=f.now,token=f.client.token,key='enter',count=1)
    send=lambda:json.loads(f.executor.handle(b'ir-'+json.dumps(raw).encode(),f.link))
    assert send()['ok'];assert not send()['ok']
    raw['seq']+=1;raw['sent']=float('nan');assert not send()['ok']
    raw['seq']+=1;raw['sent']=f.now+.1;assert not send()['ok']
    raw['seq']+=1;raw['sent']=f.now;raw['id']='b'*32;assert not send()['ok']
    f.executor.close()
    assert not send()['ok']


def test_voice_same_target_release_discard_lease_and_mode_cancel():
    f=Fixture();f.fallback()
    for kind in ('release','discard','lease','recovery'):
        f.voice.reset_mock()
        assert f.client.action('press')['ok']
        wait_for(lambda:f.voice.press.called)
        f.voice.press.assert_called_once_with('-')
        if kind in ('release','discard'):f.client.action(kind)
        elif kind=='lease':
            f.now+=1
            with f.executor.condition:f.executor.condition.notify_all()
        else:
            f.link=SimpleNamespace(opened=True);f.client.status(True,False)
        wait_for(lambda:not f.executor.talking)
        if kind=='release':f.voice.release.assert_called_once();f.voice.discard.assert_not_called()
        else:f.voice.discard.assert_called_once()
        if kind in ('lease','recovery'):
            f.link=None;f.now+=1;f.fallback()
    f.executor.close()


def test_backend_busy_drop_and_mode_cancels_deferred_key():
    f=Fixture();f.fallback()
    f.client.action('key',key='left',count=10)
    guard=f.keys.request.call_args.kwargs['guard']
    f.link=SimpleNamespace(opened=True);f.client.status(True,False)
    assert not guard()
    assert not f.client.action('key',key='enter',count=1)['ok']
    f.executor.close()


def test_socket_roundtrip_private_permissions_and_existing_disk_command(tmp_path):
    tmp_path.chmod(0o700)
    f=Fixture()
    control=hrdisk.Control(str(tmp_path/'disk.sock'),fallback=f.executor)
    control.start()
    client=host_fallback.Client(control.path,clock=lambda:f.now)
    stopped=threading.Event()
    def serve():
        while not stopped.is_set():control.poll(None);time.sleep(.001)
    worker=threading.Thread(target=serve);worker.start()
    try:
        assert (tmp_path/'disk.sock').stat().st_mode&0o777==0o600
        assert client.status(False,False)=='pending'
        f.now+=.5;client.status(False,False)
        f.now+=.5;assert client.status(False,False)=='host-fallback'
        assert client.action('key',key='enter',count=1)['ok']
        f.keys.request.assert_called_once()
    finally:
        stopped.set();worker.join();control.close()


def test_configured_real_hold_to_talk_bus_path_current_focus():
    import hrvoice
    f=Fixture();f.fallback()
    bus=Mock();bus.status.side_effect=[(0,'idle'),(7,'recording'),(7,'recording')]
    focus=Mock()
    voice=hrvoice.HoldToTalk(focus,voice_focus=True,voice_method='HoldStartRaw',
                           bus=bus,timer=Mock())
    f.executor.voice=voice
    assert f.client.action('press')['ok']
    wait_for(lambda:voice.down)
    bus.call.assert_called_with('HoldStartRaw')
    focus.assert_not_called() # target '-' never changes keyboard focus
    f.client.action('discard')
    wait_for(lambda:not f.executor.talking)
    assert [c.args[0] for c in bus.call.call_args_list]==['HoldStartRaw','Cancel','HoldStop']
    f.executor.close()


def test_action_logs_whitelist_only_and_no_arbitrary_payload(caplog):
    f=Fixture();f.fallback();caplog.set_level('INFO')
    assert f.client.action('key',key='bs',count=1)['ok']
    assert f.client.action('noop',key='menu')['ok']
    assert not f.client.action('key',key='private typed text',count=1)['ok']
    lines=[r.message for r in caplog.records]
    assert lines==['host-fallback action key-bs count=1','host-fallback action noop-menu']
    f.executor.close()


def test_healthy_status_does_not_reset_fu43_keyboard_worker():
    f=Fixture();f.link=SimpleNamespace(opened=True)
    assert f.client.status(True,False)=='ipod'
    f.now+=.1;assert f.client.status(True,False)=='ipod'
    f.keys.reset.assert_not_called()
    f.executor.close();f.keys.reset.assert_not_called()


def test_private_socket_rejects_other_uid_before_fallback(tmp_path, monkeypatch):
    import os
    tmp_path.chmod(0o700)
    executor=Mock()
    control=hrdisk.Control(str(tmp_path/'disk.sock'),fallback=executor)
    control.start()
    monkeypatch.setattr(hrdisk.struct,'unpack',lambda *args:(123,os.getuid()+1,123))
    result=[]
    client=host_fallback.Client(control.path)
    worker=threading.Thread(target=lambda:result.append(client.status(False,False)))
    worker.start()
    try:
        while worker.is_alive():control.poll(None);time.sleep(.001)
        worker.join()
        assert result==[None]
        executor.handle.assert_not_called()
    finally:
        control.close()


def test_live_cdc_with_ep0_failure_blocks_duplicate_ipod_computer_actions():
    import herdremote
    import hrusagekeys
    f=Fixture();f.link=SimpleNamespace(opened=True);f.fallback(ep0=False)
    daemon=object.__new__(herdremote.Daemon)
    daemon.ir_fallback=f.executor
    daemon.sent_ws=b'';daemon.keys=Mock();daemon.voice=Mock();daemon.send=Mock()
    daemon.key_command('1','enter')
    daemon.voice_command('vd','2','-')
    daemon.voice_command('vu','3')
    daemon.keys.request.assert_not_called();daemon.voice.press.assert_not_called();daemon.voice.release.assert_not_called()
    assert daemon.send.call_count==3
    # Real UsageKeys gate, including the deferred-worker recheck, is denied.
    lease=hrusagekeys.UsageKeys(f.keys,lambda:not daemon._ir_owns_desktop(),clock=lambda:f.now)
    lease.update(True)
    assert not lease.active
    f.client.status(True,False)
    assert not daemon._ir_owns_desktop()
    f.executor.close()


def test_entering_fallback_discards_old_ipod_owned_recording():
    f=Fixture();f.voice.engaged=True;f.fallback()
    wait_for(lambda:f.voice.discard.called and not f.executor.talking)
    f.voice.discard.assert_called_once()
    f.executor.close()


def test_fallback_blocks_dock_cider_control_before_bridge_execution():
    import herdremote
    f=Fixture();f.link=SimpleNamespace(opened=True);f.fallback(ep0=False)
    daemon=object.__new__(herdremote.Daemon)
    daemon.ir_fallback=f.executor
    daemon.remote_log=Mock();daemon.remote_log.submit.return_value=False
    daemon.cider=Mock()
    daemon.command(b'cc\t1\t2\tplay-pause\t-')
    daemon.cider.command.assert_not_called()
    f.executor.close()


def test_shutdown_discards_before_daemon_generic_voice_stop():
    import hrvoice
    f=Fixture();f.fallback()
    bus=Mock();bus.status.side_effect=[(0,'idle'),(9,'recording'),(9,'recording')]
    voice=hrvoice.HoldToTalk(Mock(),bus=bus,timer=Mock())
    f.executor.voice=voice
    f.client.action('press');wait_for(lambda:voice.down)
    f.executor.close()
    voice.stop() # The daemon calls this after Control.close().
    wait_for(lambda:not f.executor.talking)
    assert [c.args[0] for c in bus.call.call_args_list]==['HoldStart','Cancel','HoldStop']
