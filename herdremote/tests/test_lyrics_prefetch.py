import sys
import threading
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hrcider_library import row
from hrlyrics import LyricsPages


def get(cache, track, nonce, fetch, offset=0):
    return cache.get('now/lyrics?nonce=' + nonce, track, offset, fetch, row)


def test_prefetched_pages_are_stable_and_only_current_track_is_retained():
    cache = LyricsPages()
    first = [row('first', '', id='lrc1'), row('second', '', id='lrc1')]
    second = [row('new track', '', id='lrc1')]
    fetch = Mock(side_effect=[first, second])
    assert cache.select('one')
    assert cache.prefetch('one', fetch)
    assert get(cache, 'one', '1', fetch) is first
    assert get(cache, 'one', '1', fetch, offset=1) is first
    assert get(cache, 'one', '2', fetch) is first
    fetch.assert_called_once_with('one')
    assert cache.select('two')
    assert get(cache, 'one', '1', fetch)[0]['id'] == 'lyric-wait'
    assert get(cache, 'two', '3', fetch) is second
    assert fetch.call_count == 2


def test_track_race_never_publishes_old_rpc_or_blocks_page_worker():
    cache = LyricsPages()
    cache.select('one', 1)
    entered, release = threading.Event(), threading.Event()
    old = [row('old', '', id='lrc1')]
    fresh = [row('fresh', '', id='lrc1')]

    def fetch_old(track):
        assert track == 'one'
        entered.set()
        assert release.wait(2)
        return old

    worker = threading.Thread(target=cache.prefetch, args=('one', fetch_old))
    worker.start()
    try:
        assert entered.wait(2)
        fetch_new = Mock(return_value=fresh)
        assert get(cache, 'one', '1', fetch_new)[0]['id'] == 'lyric-wait'
        fetch_new.assert_not_called()
        # A new Rufin run of the same song must also invalidate the cache.
        assert cache.select('one', 2)
        assert cache.prefetch('one', fetch_new)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert get(cache, 'one', '2', fetch_new) is fresh
    fetch_new.assert_called_once_with('one')


def test_unready_retry_and_backend_invalidation():
    cache = LyricsPages()
    item = [row('converted', '', id='lrc1')]
    fetch = Mock(side_effect=[None, item])
    assert cache.select('same')
    assert not cache.prefetch('same', fetch)
    assert get(cache, 'same', '1', fetch) is item
    assert not cache.select('')
    assert get(cache, 'same', '1', fetch)[0]['id'] == 'lyric-wait'
    assert fetch.call_count == 2
