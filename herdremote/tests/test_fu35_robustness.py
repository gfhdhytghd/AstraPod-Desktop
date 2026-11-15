"""FU35 retry identity, failed fills, bounded warm queues and session recovery."""
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import hrcider
import pytest

@pytest.fixture
def bridge(tmp_path):
    b=hrcider.Bridge(client=Mock(),cache=hrcider.Cache(tmp_path),volume=Mock())
    b.opened=True;b.generation=7
    yield b
    b.close()

def worker(b):
    t=threading.Thread(target=b._work);t.start();b.threads.append(t)

def wait_until(condition):
    end=time.monotonic()+3
    while not condition() and time.monotonic()<end:time.sleep(.005)
    assert condition()

def request(b,req=393,key='a'*64,priority=4):
    assert b.command(f'cr\t{b.generation}\t{req}\t{key}\tXS\t0\t{priority}\t0\t1'.encode())

def test_slow_fill_retry_does_not_replace_inflight_token(bridge):
    entered=threading.Event();release=threading.Event()
    def artwork(key,tier):
        entered.set();assert release.wait(2);return b'x'*2060
    bridge.artwork=artwork;request(bridge);token=bridge.requests[393];worker(bridge)
    try:
        assert entered.wait(1)
        for _ in range(30):request(bridge)
        assert bridge.requests[393] is token
        assert bridge.jobs.qsize()==0
    finally:release.set()
    wait_until(lambda:393 not in bridge.requests)
    bridge.scheduler.grant(131072)
    assert bridge.scheduler.next_frame()[1].startswith(b'CAR1')

def test_unknown_fill_gets_failure_reply_and_worker_survives(bridge,caplog):
    request(bridge);worker(bridge)
    wait_until(lambda:393 not in bridge.requests)
    assert bridge.scheduler.next_frame()[1]==b'CK\t7\t393\t0\tArtwork unavailable'
    assert bridge.threads[0].is_alive()

def test_warm_queue_saturation_never_kills_caller(bridge):
    bridge.warm_window={(f'{i:064x}','M') for i in range(7)}
    bridge.warm_queue={(f'{i:064x}','L') for i in range(2)}
    bridge.offer_pairs={(f'{i:064x}','XS') for i in range(13)}
    for _ in range(50):bridge._schedule_warm()
    assert bridge.prefetch.qsize()<=10
    assert len(bridge.warm_jobs)<=10

def test_cancelled_old_generation_has_no_reply_to_new_session(bridge):
    entered=threading.Event();release=threading.Event()
    def artwork(key,tier):
        entered.set();assert release.wait(2);return b'x'*2060
    bridge.artwork=artwork;request(bridge);worker(bridge)
    try:
        assert entered.wait(1)
        bridge.disconnect()
        assert bridge.command(b'ci\t1\t131072\tcn18,localstore,localdiag-live')
        new=bridge.generation;assert new!=7
    finally:release.set()
    # A fresh request using the same numeric id must survive old completion.
    request(bridge);wait_until(lambda:393 not in bridge.requests)
    bridge.scheduler.grant(131072)
    frames=[]
    while (frame:=bridge.scheduler.next_frame()) is not None:frames.append(frame[1])
    assert any(p.startswith(b'CAR1') and int.from_bytes(p[4:8],'little')==new for p in frames)
    assert not any(p.startswith(b'CAR1') and int.from_bytes(p[4:8],'little')==7 for p in frames)

def test_packed_xs_retry_keeps_current_job(bridge):
    payload=('cb\t7\t42\t'+','.join(['a'*64,'b'*64])).encode()
    assert bridge.command(payload);token=bridge.requests[42]
    assert bridge.command(payload)
    assert bridge.requests[42] is token
    assert bridge.jobs.qsize()==1

def test_request_cap_keeps_existing_work_and_allows_its_retry(bridge):
    for i in range(1,25):request(bridge,i)
    tokens=dict(bridge.requests);request(bridge,25)
    assert bridge.requests==tokens
    request(bridge,1)
    assert bridge.requests==tokens

def test_full_response_queue_reopens_instead_of_killing_art_worker(bridge):
    bridge.artwork=Mock(return_value=b'x'*2060)
    for _ in range(64):bridge.scheduler.push(0,2,b'CN')
    request(bridge);worker(bridge)
    wait_until(lambda:393 not in bridge.requests)
    assert bridge.threads[0].is_alive()
    with pytest.raises(hrcider.SessionExpired,match='response queue full'):bridge.check_liveness()

def test_foreground_precedes_fill_and_fill_completes_afterward(bridge):
    order=[]
    def artwork(key,tier):order.append(key);return b'x'*2060
    bridge.artwork=artwork
    request(bridge,1,'a'*64,4);request(bridge,2,'b'*64,3);worker(bridge)
    wait_until(lambda:not bridge.requests)
    assert order==['b'*64,'a'*64]
    bridge.scheduler.grant(131072)
    assert [int.from_bytes(bridge.scheduler.next_frame()[1][8:12],'little') for _ in range(2)]==[2,1]
