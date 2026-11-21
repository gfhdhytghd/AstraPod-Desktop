"""FU44 mock decoded IR/EP0/daemon; never open a receiver, device or GUI."""
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

sys.path.insert(0,str(Path(__file__).parents[2]))
from usb_remote_key import host_fallback as h
from usb_remote_key import usb_remote_key as k


def frame(code):return bytes([0xee,0x87,code,0x59])


def setup_router():
    remote=Mock();remote.send.return_value=True;remote.probe.return_value=True
    disk=Mock(active=False)
    client=Mock();client.status.return_value='ipod'
    usage=Mock()
    router=h.Router(remote,disk,k.KeyForwarder(remote),k.VolumeForwarder(remote,usage),client)
    return router,remote,disk,client,usage


def actions(client):return [(c.args[0],c.kwargs) for c in client.action.call_args_list]


def test_reachable_fu43_routing_no_fallback_or_duplicate():
    r,usb,disk,client,usage=setup_router()
    for i,code in enumerate((2,3,4,7,8,11,13)):
        at=i*2.
        r.frame(frame(code),at);r.frame(b'',at+.108);r.poll(at+.3)
    assert all(c.args[2] not in (k.LEFT,k.RIGHT,k.PLAY) for c in usb.send.call_args_list)
    assert any(c.args[2]==k.MENU for c in usb.send.call_args_list)
    assert any(c.args[2]==k.VOL_UP for c in usb.send.call_args_list)
    assert usage.report.call_count==2
    client.action.assert_not_called()


def test_switch_both_ways_quarantines_held_gesture_and_logs_once(caplog):
    r,usb,disk,client,usage=setup_router()
    caplog.set_level('INFO')
    r.frame(frame(11),0)
    client.status.return_value='host-fallback'
    r.frame(b'',.11);r.frame(b'',.22)
    assert client.action.call_count==0
    r.poll(.5)
    r.frame(frame(11),.6)
    assert actions(client)==[('key',dict(key='left',count=1))]
    usb.send.reset_mock()
    client.status.return_value='ipod'
    r.frame(b'',.71);r.frame(b'',.82)
    usb.send.assert_not_called()
    r.poll(1.1);r.frame(frame(13),1.2)
    assert usb.send.call_args.args[2]==k.VOL_DOWN
    assert [rec.message for rec in caplog.records if rec.message.startswith('IR mode')]==['IR mode host-fallback','IR mode ipod']


@pytest.mark.parametrize('code,low,high',[(11,'left','up'),(13,'right','down')])
def test_volume_identical_ages_skip_and_full_frame_reset(code,low,high):
    client=Mock();g=h.Gestures(client)
    key='plus' if code==11 else 'minus'
    g.frame(key,10,.068)
    g.frame('repeat',10.108)
    assert len(actions(client))==1
    for n in range(2,76):
        now=10+n*.108;g.frame('repeat',now)
        age=now-10+.068
        count=1 if age<.5 else 3 if age<1 else 10 if age<4 else 1 if age<7 else 3
        assert actions(client)[-1]==('key',dict(key=low if age<4 else high,count=count))
    before=len(actions(client));g.poll(20);g.frame('repeat',21)
    assert len(actions(client))==before
    g.frame(key,22,.068)
    assert actions(client)[-1]==('key',dict(key=low,count=1))


def test_centre_short_select_once_hold_voice_release_and_ui_gestures_noop():
    client=Mock();g=h.Gestures(client)
    g.frame('centre',0);g.poll(.151);g.poll(.36)
    assert actions(client)==[('press',{}),('discard',{}),('key',dict(key='enter',count=1))]
    client.action.reset_mock();g.frame('centre',1)
    for n in range(1,6):g.frame('repeat',1+n*.108)
    g.poll(1.7);g.poll(2)
    assert actions(client)==[('press',{}),('release',{})]
    client.action.reset_mock()
    for at in (3,3.31):g.frame('centre',at);g.poll(at+.151)
    g.poll(3.7)
    assert ('noop',dict(key='double')) in actions(client)
    assert not any(op=='key' for op,_ in actions(client))
    client.action.reset_mock()
    for at in (4,4.31,4.62):g.frame('centre',at);g.poll(at+.151)
    assert ('noop',dict(key='triple')) in actions(client)
    assert not any(op=='key' for op,_ in actions(client))


def test_left_backspace_repeat_menu_cancel_right_noop():
    client=Mock();g=h.Gestures(client)
    g.frame('left',0);g.frame('repeat',.108);g.frame('repeat',.216)
    assert len(actions(client))==1
    g.poll(.30);assert len(actions(client))==1
    g.poll(.31);assert len(actions(client))==2
    g.frame('repeat',.324);g.poll(.47);assert len(actions(client))==3
    g.poll(.7);assert len(actions(client))==3
    g.frame('right',1);g.poll(1.2)
    assert actions(client)[-1]==('noop',dict(key='right'))
    g.frame('centre',2);g.frame('menu',2.1);g.poll(2.3)
    assert actions(client)[-2:]==[('discard',{}),('noop',dict(key='menu'))]


def test_disk_menu_exclusive_priority_other_keys_fallback():
    r,usb,disk,client,usage=setup_router()
    disk.active=True;client.status.return_value='host-fallback'
    r.frame(frame(2),0)
    for n in range(1,13):r.frame(b'',n*.108)
    assert disk.frame.call_count==13
    client.action.assert_not_called();usb.send.assert_not_called()
    r.poll(2);r.frame(frame(8),2.1)
    assert actions(client)==[('key',dict(key='bs',count=1))]
    usage.report.assert_not_called()


def test_missing_daemon_unknown_and_orphan_frames_do_not_act():
    r,usb,disk,client,usage=setup_router();client.status.return_value=None
    r.frame(b'',0);r.frame(frame(11),1);r.frame(b'noise',1.05);r.frame(b'',1.1)
    assert usb.send.call_count==1
    client.action.assert_not_called()


def test_ep0_fresh_get_configuration_and_failure_without_any_device_io():
    error=type('USBError',(Exception,),{})
    util=Mock();remote=k.USBRemote(SimpleNamespace(USBError=error),util)
    dev=Mock();remote.device=dev
    dev.ctrl_transfer.return_value=bytes([1]);assert remote.probe(0)
    dev.ctrl_transfer.assert_called_with(0x80,8,0,0,1,timeout=100)
    dev.ctrl_transfer.side_effect=error('unavailable');assert not remote.probe(.1)
    util.dispose_resources.assert_called_once_with(dev)
    assert remote.device is None


def test_disk_priority_survives_ep0_failure_using_verified_volume():
    from usb_remote_key import disk_mode
    remote=Mock();remote.disk_status.return_value=None
    disk=disk_mode.DiskMode(remote,find=lambda:'/dev/mock-ipod1')
    disk.submit=Mock()
    try:
        disk.poll(0);assert disk.active
        disk.frame(frame(2),0)
        for n in range(1,12):disk.frame(b'',n*.108)
        disk.submit.assert_called_once_with(True,None)
        remote.disk_status.return_value=bytes([1,0,0,0,0,0,0,0])
        disk.poll(2);assert not disk.active
    finally:
        disk.close()
