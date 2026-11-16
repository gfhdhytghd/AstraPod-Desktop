import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto,createHash} from 'node:crypto';
import {mkdtemp,readFile,writeFile,copyFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {rendererAssetURLs,verifyRenderer,hostBindings} from './renderer.mjs';
import {CiderAdapter,BridgeError} from './core.mjs';
import {makeHost} from './mock-host.mjs';
const bytes=Buffer.from('/* pinned fixture: must never execute */ globalThis.executedHostEntry = true;');
const digest=createHash('sha256').update(bytes).digest('hex');
const manifest={'index-test.js':digest};
const renderer='http://127.0.0.1:19501/index.html#/am/library';
const plugin='http://127.0.0.1:19502/plugins/org.podbox.cider-bridge/plugin.js';

test('renderer assets use document origin when plugin module has a different origin',()=>{
 const [{url}]=rendererAssetURLs(manifest,renderer);
 assert.equal(url,'http://127.0.0.1:19501/assets/index-test.js');
 assert.notEqual(url,new URL('/assets/index-test.js',plugin).href);
});
test('reject opaque, remote, spoofed and credential-bearing renderer URLs',()=>{
 for(const url of ['null','file:///tmp/cider/index.html','https://evil.test/',
  'http://127.0.0.1.evil.test/','http://user@127.0.0.1:19501/']) assert.throws(()=>rendererAssetURLs(manifest,url),BridgeError);
 assert.throws(()=>rendererAssetURLs({'../x.js':digest},renderer),BridgeError);
});
test('runtime hash check fetches renderer bytes and never executes them',async()=>{
 const calls=[];
 await verifyRenderer(manifest,renderer,async(url,options)=>{calls.push({url,options});return new Response(bytes);},webcrypto.subtle);
 assert.equal(calls[0].url,'http://127.0.0.1:19501/assets/index-test.js');
 assert.equal(calls[0].options.credentials,'omit');assert.equal(calls[0].options.redirect,'error');
 assert.equal(globalThis.executedHostEntry,undefined);
 await assert.rejects(verifyRenderer(manifest,renderer,async()=>new Response('changed'),webcrypto.subtle),BridgeError);
 await assert.rejects(verifyRenderer(manifest,renderer,async()=>new Response('',{status:404}),webcrypto.subtle),BridgeError);
 await assert.rejects(verifyRenderer({},renderer,async()=>new Response(bytes),webcrypto.subtle),BridgeError);
});
function injected() {
 const fixture=makeHost(),changes=new Set();
 fixture.am.nowPlayingItem=fixture.metadata.player.nowPlayingItem;
 fixture.am.$subscribe=fn=>{changes.add(fn);return ()=>changes.delete(fn);};
 fixture.am.setPlaybackSpeed=value=>{fixture.am.audioElement.playbackRate=value;};
 fixture.am.getPlaybackRate=()=>fixture.am.audioElement.playbackRate;
 const metaChanges=new Set(),requests=[];
 const app={musicKitStore:{isAuthorized:true,
  $subscribe:fn=>{metaChanges.add(fn);return ()=>metaChanges.delete(fn);},
  getOptimisticValues:()=>({addedToLibrary:true,favorited:false}),setOptimisticValue:()=>{}},
  v3:async(...args)=>{requests.push(args);return {data:{data:[]}};},lyrics:{getLyrics:async()=>[]}};
 return {...fixture,app,system:{Stores:{appleMusicStore:fixture.am}},changes,metaChanges,requests};
}
test('injected bridge preserves strict errors, memberships, placeholders and speed control',async()=>{
 const fixture=injected(),bindings=hostBindings(fixture.app,fixture.system),adapter=new CiderAdapter(bindings);
 assert.equal(adapter.snapshot().nowPlaying.inLibrary,true);
 await adapter.read('/v1/catalog/$STOREFRONT/albums/a/tracks');
 assert.equal(fixture.requests[0][0],'/v1/catalog/$STOREFRONT/albums/a/tracks');
 assert.equal(fixture.requests[0][2].throwOnHTTPError,true);
 fixture.app.v3=async()=>{throw Object.assign(new Error('private'),{status:401});};
 await assert.rejects(adapter.read('/v1/me/library/songs'));
 await adapter.dispatch('/api/v2/playback/rate',{rate:1.25},'PATCH');assert.equal(adapter.snapshot().rate,1.25);
});
test('reactive subscriptions push time, item, queue, buffering and modes without polling',()=>{
 const fixture=injected(),bindings=hostBindings(fixture.app,fixture.system),adapter=new CiderAdapter(bindings),events=[];
 const stop=adapter.subscribe(name=>events.push(name));
 fixture.am.currentTime=25;for(const fn of fixture.changes)fn();
 fixture.am.queuePosition=1;for(const fn of fixture.changes)fn();
 fixture.am.state='paused';for(const fn of fixture.changes)fn();
 fixture.am.isSwitching=true;for(const fn of fixture.changes)fn();
 fixture.am.nowPlayingItem=fixture.track('other');for(const fn of fixture.changes)fn();
 fixture.am.repeatMode='all';for(const fn of fixture.changes)fn();
 for(const name of ['playbackTime','playbackState','nowPlayingItem','loading'])assert(events.includes('playbackStatus.'+name+'DidChange'),name);
 assert(events.includes('queueStatus.queueChanged'));assert(events.includes('playerStatus.repeatModeDidChange'));
 stop();assert.equal(fixture.changes.size,0);assert.equal(fixture.metaChanges.size,0);
});
test('actual plugin bootstrap loads only plugin-local modules and verified renderer assets',async()=>{
 const directory=await mkdtemp(join(tmpdir(),'fu38-bootstrap-')),fixture=injected();
 const globals=['location','CiderApp','__PLUGINSYS__','localStorage','window','navigator','fetch','WebSocket','crypto','__podboxBridgeStop'];
 const previous=new Map(globals.map(name=>[name,Object.getOwnPropertyDescriptor(globalThis,name)]));
 const requests=[];let socket;
 class Socket {constructor(url){this.url=url;this.readyState=1;this.bufferedAmount=0;socket=this;}send(){}close(){this.readyState=3;this.onclose?.();}}
 try {
  for(const name of ['plugin.js','core.mjs','renderer.mjs'])await copyFile(new URL('./'+name,import.meta.url),join(directory,name));
  await writeFile(join(directory,'package.json'),JSON.stringify({type:'module'}));
  await writeFile(join(directory,'connection.mjs'),'export default '+JSON.stringify({url:'ws://127.0.0.1:19503/',assets:manifest})+';');
  const values={location:{href:renderer},CiderApp:fixture.app,__PLUGINSYS__:fixture.system,
   localStorage:{getItem:()=> 'a'.repeat(64)},window:new EventTarget(),navigator:{onLine:true},
   fetch:async url=>{requests.push(url);return new Response(bytes);},WebSocket:Socket,crypto:webcrypto};
  for(const [name,value]of Object.entries(values))Object.defineProperty(globalThis,name,{value,writable:true,configurable:true});
  const {default:pluginModule}=await import(pathToFileURL(join(directory,'plugin.js')));
  await pluginModule.setup();
  assert.equal(socket.url,'ws://127.0.0.1:19503/');assert.deepEqual(requests,['http://127.0.0.1:19501/assets/index-test.js']);
  const source=await readFile(join(directory,'plugin.js'),'utf8');assert(!/from ['"]\/assets\//.test(source));
 } finally {
  globalThis.__podboxBridgeStop?.();
  for(const [name,descriptor]of previous)if(descriptor)Object.defineProperty(globalThis,name,descriptor);else delete globalThis[name];
  await rm(directory,{recursive:true});
 }
});

test('engine-only auto advance, next/previous and queue jump push the new item without Pinia/time/state events',async()=>{
 const f=injected(),engine=new EventTarget();f.am.player={events:engine};
 const bindings=hostBindings(f.app,f.system),adapter=new CiderAdapter(bindings),received=[];
 const stop=adapter.subscribe(event=>received.push({event,state:adapter.snapshot()}));
 const select=id=>{f.am.nowPlayingItem=f.track(id);engine.dispatchEvent(new Event('MKLite:itemIdChanged'));};
 f.am.skipToNext=()=>select('next');f.am.skipToPrevious=()=>select('previous');
 f.am.jumpToQueuePosition=()=>select('jump');
 select('auto');await Promise.resolve();
 assert.equal(received.at(-1).state.nowPlaying.id,'auto');
 for(const [path,body,id] of [['/api/v1/playback/next',{},'next'],['/api/v1/playback/previous',{},'previous'],['/api/v2/queue/jump',{index:1},'jump']]) {
  await adapter.dispatch(path,body,'POST');await Promise.resolve();
  const last=received.at(-1);assert.equal(last.event,'playbackStatus.nowPlayingItemDidChange');
  assert.equal(last.state.nowPlaying.id,id);assert.equal(last.state.nowPlaying.name,'Song '+id);
  assert.equal((await adapter.dispatch('/api/v2/playback',null,'GET')).data.nowPlaying.id,id);
 }
 assert.equal(received.length,4);
 stop();select('after-unload');await Promise.resolve();assert.equal(received.length,4);
});

test('late/replaced engine listeners detach and a burst coalesces into one current-item hint',async()=>{
 const f=injected();let ready;f.am.player=null;f.am.onPlayerReady=fn=>{ready=fn;};
 const b=hostBindings(f.app,f.system),events=[];const off=b.bus.on('playbackStatus.nowPlayingItemDidChange',()=>events.push(b.metadata.player.nowPlayingItem.id));
 const first=new EventTarget();f.am.player={events:first};ready();f.am.nowPlayingItem=f.track('late');
 first.dispatchEvent(new Event('MKLite:itemIdChanged'));first.dispatchEvent(new Event('MKLite:resolvedItemChanged'));await Promise.resolve();
 assert.deepEqual(events,['late']);
 const second=new EventTarget();f.am.player={events:second};for(const fn of f.changes)fn();events.length=0;
 f.am.nowPlayingItem=f.track('replacement');first.dispatchEvent(new Event('MKLite:itemIdChanged'));await Promise.resolve();assert.equal(events.length,0);
 second.dispatchEvent(new Event('MKLite:itemIdChanged'));await Promise.resolve();assert.deepEqual(events,['replacement']);
 off();second.dispatchEvent(new Event('MKLite:itemIdChanged'));await Promise.resolve();assert.equal(events.length,1);
});
