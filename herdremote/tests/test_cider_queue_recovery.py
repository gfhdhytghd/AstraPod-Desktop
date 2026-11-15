"""FU32 collection success must mean a container queue, not one selected song."""
import logging,sys,tempfile
from pathlib import Path
from unittest.mock import Mock
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hrcider import Bridge
from hrcider_cache import Cache
from hrcider_library import Library

@pytest.mark.parametrize('route,ctype,cid',[('album/a','library-albums','a'),('album/catalog-us/99','albums','99'),('playlist/p','library-playlists','p')])
@pytest.mark.parametrize('broken',[False,True])
def test_partial_or_failed_collection_keeps_the_browsed_queue(route,ctype,cid,broken):
    with tempfile.TemporaryDirectory() as d:
        cache=Cache(d);client=Mock();lib=Library(cache,client)
        client.song_aliases.return_value={'chosen'};client.queue_state.return_value=(0,1)
        client.play_collection.side_effect=OSError('mock collection refused') if broken else None
        client.play_collection.return_value={'expected_ids':['chosen']}
        client.control.return_value={'expected_ids':['chosen']}
        rows=[{'id':v,'route':'play/library-songs/'+v} for v in ['before','chosen','after']]
        lib.page=Mock(return_value={'total':3,'items':rows})
        lib.set_context('1|'+route);result=lib.play('play/library-songs/chosen')
        client.play_collection.assert_called_once_with(ctype,cid,{'chosen'},1)
        assert callable(result.get('fill')), 'a singleton collection must recover the browsed neighbours'
        import threading
        cancel=threading.Event();result['fill'](cancel)
        client.fill_queue.assert_called_once_with({'chosen'},[('library-songs','before')],[('library-songs','after')],cancel)
        cache.close()

def test_rejected_control_is_logged_before_availability_check(caplog):
    with tempfile.TemporaryDirectory() as d:
        b=Bridge(client=Mock(),cache=Cache(d),volume=Mock());b.generation=7;b.opened=True;b.available=False
        b.controls.put((7,160,'next','0'));push=b.scheduler.push
        def stop(*args,**kwargs):push(*args,**kwargs);b.stop.set()
        b.scheduler.push=stop
        with caplog.at_level(logging.INFO,logger='herdremote.cider'):b._control()
        assert any('control request gen=7 req=160 command=next' in r.message for r in caplog.records)
        assert b.scheduler.next_frame()[1].startswith(b'CK\t7\t160\t0\t')
        b.close()
