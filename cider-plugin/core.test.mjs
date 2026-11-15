import test from 'node:test';
import assert from 'node:assert/strict';
import {BridgeError,connectBridge,errorCode,page,resource,MAX_BYTES} from './core.mjs';
import {makeHost} from './mock-host.mjs';
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));

test('snapshot metadata, identifiers, artwork, status, position and modes',()=>{
  const {host}=makeHost();const s=host.snapshot();
  assert.equal(s.nowPlaying.id,'one');assert.equal(s.nowPlaying.name,'Song one');
  assert.equal(s.time.currentTime,12);assert.equal(s.nowPlaying.durationInMillis,100000);
  assert.equal(s.nowPlaying.inLibrary,true);assert.equal(s.volume,.5);
  assert.equal(s.queue.neighbors.length,2);assert.equal(s.loading,false);
});
test('all explicit transport, seek, volume, shuffle, repeat and rate controls',async()=>{
  const {host,am,calls}=makeHost();
  for(const action of ['play','pause','playpause','next','previous']) await host.dispatch('/api/v1/playback/'+action,{},'POST');
  await host.dispatch('/api/v1/playback/seek',{position:33},'POST');
  await host.dispatch('/api/v2/playback/volume',{volume:.6},'PUT');
  await host.dispatch('/api/v2/playback/rate',{rate:1.5},'PATCH');
  for(let i=0;i<2;i++) await host.dispatch('/api/v2/playback/shuffle',{enabled:true},'PUT');
  await host.dispatch('/api/v2/playback/repeat',{mode:'all'},'PUT');
  assert.equal(am.currentTime,33);assert.equal(am.volume,.6);assert.equal(am.isShuffled,true);
  assert.equal(am.repeatMode,'all');assert.equal(host.snapshot().rate,1.5);assert(calls.includes('next'));
});
test('favorite, remove favorite, add library and status',async()=>{
  const {host,metadata,calls}=makeHost();
  await host.dispatch('/api/v2/library/now-playing/love',{},'POST');assert.equal(metadata.player.inFavorites,true);
  await host.dispatch('/api/v2/library/now-playing/rating',null,'DELETE');assert.equal(metadata.player.inFavorites,false);
  metadata.player.inLibrary=false;
  await host.dispatch('/api/v2/library/now-playing/add',{},'POST');assert.equal(metadata.player.inLibrary,true);
  assert(calls.some(row=>row[3]==='PUT'));assert(calls.some(row=>row[3]==='DELETE'));
});
test('collection creation, queue jump/list/add/move',async()=>{
  const {host,am}=makeHost();
  await host.dispatch('/api/v2/playback/play-collection',{type:'library-albums',id:'album'},'POST');
  await host.dispatch('/api/v2/queue/jump',{index:1},'POST');
  assert.equal((await host.dispatch('/api/v2/queue/position',null)).data.position,1);
  assert.equal((await host.dispatch('/api/v2/queue?offset=1&limit=1',null)).data.items[0].track.id,'two');
  await host.dispatch('/api/v2/queue/add-later',{type:'songs',id:'three'},'POST');
  await host.dispatch('/api/v2/queue/move',{from:2,to:0},'POST');assert.equal(am.queue[0].track.id,'three');
});
test('library pages and all current routes use in-process API',async()=>{
  const {host}=makeHost();
  for(const path of ['/v1/me/library/songs?limit=50&offset=100&include=albums,catalog',
    '/v1/me/library/albums','/v1/me/library/playlists','/v1/me/library/albums/a/tracks',
    '/v1/me/library/playlists/p/tracks','/v1/catalog/us/albums/a/tracks',
    '/v1/catalog/us/playlists/p/tracks','/v1/me/library/recently-added',
    '/v1/me/recent/played/tracks','/v1/me/recent/played','/v1/me/ratings/songs?ids=one,two',
    '/v1/me/library/search?term=hello&types=library-songs']) {
    const result=await host.dispatch('/api/v1/amapi/run-v3',{path},'POST');assert(result.data || result.results,path);
  }
});
test('status, timing, lyrics, quality and direct song queue',async()=>{
  const {host,calls}=makeHost();
  for(const path of ['/api/v2/playback','/api/v2/playback/time','/api/v2/playback/rate',
    '/api/v2/library/now-playing/status','/api/v2/playback/audio-quality','/api/v1/lyrics/one'])assert((await host.dispatch(path,null)).data);
  await host.dispatch('/api/v1/playback/play-item-href',{href:'/v1/catalog/us/songs/one'},'POST');
  assert(calls.includes('items'));
});
test('reject off-host URLs, traversal, arbitrary APIs and invalid controls',async()=>{
  const {host}=makeHost();
  for(const path of ['https://evil.test/v1/me/library/songs','/v1/me/../tokens','/v1/me/library/../../tokens','/v1/me/secret',
    '/v1/me/library/songs?limit=1001','/v1/me/library/songs?offset=-1'])await assert.rejects(host.read(path),BridgeError);
  await assert.rejects(host.dispatch('/api/v1/playback/seek',{position:NaN},'POST'),BridgeError);
  await assert.rejects(host.dispatch('/api/v2/queue/jump',{index:200},'POST'),BridgeError);
  await assert.rejects(host.dispatch('/api/v2/playback/shuffle',{enabled:1},'PUT'),BridgeError);
});
test('static offline/unauthorized errors and no raw credential error text',async()=>{
  const {host,metadata}=makeHost();metadata.isAuthorized=false;
  await assert.rejects(host.read('/v1/me/library/albums'),e=>errorCode(e)==='unauthorized');
  metadata.isAuthorized=true;host.online=()=>false;
  await assert.rejects(host.read('/v1/me/library/albums'),e=>errorCode(e)==='offline');
  assert.equal(errorCode(new Error('HTTP Error: 401 secret-token')),'unauthorized');
  assert.equal(errorCode(new Error('private host error secret-token')),'host-error');
});
test('resource whitelist strips tokens, host internals and unrelated relationships',()=>{
  const item={id:'one',developerToken:'NEVER',attributes:{name:'One',musicUserToken:'NEVER',artwork:{url:'x',secret:'NEVER'},playParams:{id:'one',token:'NEVER'}},relationships:{private:{data:[{token:'NEVER'}]}}};
  assert(!JSON.stringify(resource(item)).includes('NEVER'));
  assert(!JSON.stringify(page({data:[item],token:'NEVER'})).includes('NEVER'));
});
test('state, time, item, queue, buffering push events and unsubscribe',()=>{
  const {host,emitter,audio}=makeHost(),events=[];
  const stop=host.subscribe(e=>events.push(e));
  for(const event of ['playbackStatus.playbackTimeDidChange','playbackStatus.playbackStateDidChange',
    'playbackStatus.nowPlayingItemDidChange','queueStatus.queueChanged'])emitter.emit(event,{});
  audio.dispatchEvent(new Event('waiting'));assert.equal(host.snapshot().loading,true);
  audio.dispatchEvent(new Event('playing'));assert.equal(host.snapshot().loading,false);
  assert.equal(events.length,6);stop();emitter.emit('queueStatus.queueChanged');assert.equal(events.length,6);
});
class FakeSocket {
  static sockets=[];
  constructor(){this.readyState=1;this.bufferedAmount=0;this.sent=[];FakeSocket.sockets.push(this);}
  send(raw){this.sent.push(JSON.parse(raw));}
  close(){this.readyState=3;this.onclose?.();}
  message(value){return this.onmessage({data:JSON.stringify(value)});}
}
function setupSocket() {
  const fixture=makeHost();const stop=connectBridge(fixture.host,{url:'ws://127.0.0.1:10768/',secret:'a'.repeat(64)},FakeSocket);
  const ws=FakeSocket.sockets.at(-1);ws.onopen();return {...fixture,stop,ws};
}
test('WebSocket hello, correlated replies, large pages bound and sanitized errors',async()=>{
  const {host,ws,stop}=setupSocket();
  try {
    assert.equal(ws.sent[0].type,'hello');await ws.message({v:1,type:'hello',ok:true});
    host.dispatch=async()=>({data:'x'.repeat(MAX_BYTES+1)});
    await ws.message({v:1,type:'request',id:'1',path:'/large',method:'GET',timeoutMs:50});
    assert.equal(ws.sent.at(-1).error,'reply-too-large');
    host.dispatch=async()=>{throw new Error('Apple-token-secret');};
    await ws.message({v:1,type:'request',id:'2',path:'/x',method:'GET'});
    assert.equal(ws.sent.at(-1).id,'2');assert.equal(ws.sent.at(-1).error,'host-error');
    assert(!JSON.stringify(ws.sent).includes('Apple-token-secret'));
  } finally {stop();}
});
test('timeout keeps mutation single-flight until the host operation settles',async()=>{
  const {host,ws,stop}=setupSocket();let finish;
  try {
    await ws.message({v:1,type:'hello',ok:true});host.dispatch=()=>new Promise(resolve=>{finish=resolve;});
    await ws.message({v:1,type:'request',id:'one',path:'/control',method:'POST',timeoutMs:5});
    assert.equal(ws.sent.at(-1).error,'timeout');
    await ws.message({v:1,type:'request',id:'two',path:'/control',method:'POST'});assert.equal(ws.sent.at(-1).error,'busy');
    finish({status:'ok'});await sleep(1);
  } finally {stop();}
});
test('reconnect after host restart, disposal cancels reconnect',async()=>{
  const {ws,stop}=setupSocket();const count=FakeSocket.sockets.length;
  ws.close();await sleep(280);assert.equal(FakeSocket.sockets.length,count+1);
  stop();await sleep(280);assert.equal(FakeSocket.sockets.length,count+1);
});
test('reject non-loopback plugin endpoints',()=>{
  for(const url of ['ws://example.test/','ws://0.0.0.0/','ws://127.0.0.1/?secret=x'])assert.throws(()=>connectBridge(makeHost().host,{url,secret:'a'.repeat(64)},FakeSocket),BridgeError);
});
