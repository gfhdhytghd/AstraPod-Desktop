"""FU37 page-error reporting and read-only catalog routing. All HTTP is mocked."""
from pathlib import Path
import io,json,sys,threading,time,urllib.error
from unittest.mock import Mock
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import hrcider
from hrcider_library import Library
from hrcider_cache import Cache

@pytest.fixture
def cache(tmp_path):
 c=Cache(tmp_path);yield c;c.close()

def client_error(message):
 stream=io.BytesIO(json.dumps({'message':message}).encode())
 opener=Mock();opener.open.side_effect=urllib.error.HTTPError('http://127.0.0.1/private?token=SECRET',500,'error',{},stream)
 return hrcider.Client(token='SECRET_TOKEN',opener=opener),stream

@pytest.mark.parametrize('query,kind',[('album/private-id','album'),('playlist/private-id','playlist'),('root/Recently%20Added','recent'),('history/tracks','history'),('search?q=SECRET','search')])
def test_actual_http_error_reaches_warning_for_each_page(cache,caplog,query,kind):
 client,stream=client_error('Network connection unavailable')
 bridge=hrcider.Bridge(client=client,cache=cache,volume=Mock());bridge.opened=True;bridge.generation=7;bridge.available=True;bridge.index.initial=False
 bridge.command(f'cq\t7\t11\t0\t50\t{query}'.encode());worker=threading.Thread(target=bridge._pages);worker.start();bridge.threads.append(worker)
 try:
  deadline=time.monotonic()+2
  while not bridge.scheduler.heap and time.monotonic()<deadline:time.sleep(.005)
  assert bridge.scheduler.heap and worker.is_alive() and stream.closed
  assert 'page failed gen=7 req=11' in caplog.text and 'kind='+kind in caplog.text
  assert 'HTTP 500: Network connection unavailable' in caplog.text
  assert 'SECRET' not in caplog.text and 'private-id' not in caplog.text
 finally:bridge.close()

def test_untrusted_server_message_is_not_logged_or_in_exception():
 client,stream=client_error('SECRET https://example.test/?token=private')
 with pytest.raises(hrcider.HTTPStatusError) as caught:client.library_path('/v1/me/library/albums')
 assert str(caught.value)=='Cider HTTP 500'
 assert getattr(caught.value,'cider_message','')=='' and stream.closed

def test_last_good_page_keeps_failure_reason_for_worker_log(cache):
 client=Mock();client.library_path.return_value={'data':[dict(id='s',type='library-songs',attributes={'name':'Keep'})],'meta':{'total':1}}
 library=Library(cache,client);assert library.page('playlist/p',0,50)['items'][0]['title']=='Keep'
 key=next(iter(library.remote));library.remote[key]=(time.monotonic()-61,library.remote[key][1])
 client.library_path.side_effect=hrcider.HTTPStatusError(500,'Network connection unavailable')
 page=library.page('playlist/p',0,50)
 assert page['stale'] and page['items'][0]['title']=='Keep'
 assert page['_error']=='HTTP 500: Network connection unavailable'

def test_catalog_routes_built_by_library_are_read_only_allowlisted(cache):
 client=hrcider.Client();client.request=Mock(return_value={'data':[]})
 for kind in ('album','playlist'):
  Library(cache,client).page(kind+'/catalog-us/123',0,50)
  client.request.assert_called_with('/api/v1/amapi/run-v3',{'path':f'/v1/catalog/us/{kind}s/123/tracks?offset=0&limit=50'},timeout=3)
 for path in ('https://example.test/','/v1/catalog/us/albums/123','/v1/catalog/us/playlists/x/delete','/v1/catalog/us/albums/../tracks'):
  with pytest.raises(ValueError):client.library_path(path)
