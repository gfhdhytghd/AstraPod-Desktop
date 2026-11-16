/** Resolve and verify the actual renderer, independently of the plugin server. */
import {BridgeError} from './core.mjs';
export function rendererAssetURLs(manifest, rendererHref) {
  let renderer;
  try {renderer=new URL(rendererHref);} catch {throw new BridgeError('unsupported-host');}
  if (!manifest || typeof manifest!=='object' || Array.isArray(manifest)) throw new BridgeError('unsupported-host');
  if (!['http:','https:'].includes(renderer.protocol) ||
      !['127.0.0.1','localhost','[::1]'].includes(renderer.hostname) ||
      renderer.username || renderer.password || renderer.origin==='null') throw new BridgeError('unsupported-host');
  return Object.entries(manifest).map(([name,digest])=>{
    if (!/^[\w-]+\.js$/.test(name) || !/^[a-f0-9]{64}$/.test(digest)) throw new BridgeError('unsupported-host');
    return {url:new URL('/assets/'+name,renderer.origin).href,digest};
  });
}
export async function verifyRenderer(manifest, rendererHref, fetcher=globalThis.fetch, subtle=globalThis.crypto?.subtle) {
  const entries=rendererAssetURLs(manifest,rendererHref);
  if (!entries.length || !subtle) throw new BridgeError('unsupported-host');
  for (const {url,digest} of entries) {
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);
    try {
      const response=await fetcher(url,{signal:controller.signal,credentials:'omit',cache:'no-store',redirect:'error'});
      if (!response.ok || (response.url && response.url!==url)) throw new BridgeError('unsupported-host');
      const reader=response.body.getReader();let size=0;const chunks=[];
      try {
        while (true) {
          const {done,value}=await reader.read();if(done)break;
          size+=value.length;if(size>16*1024*1024)throw new BridgeError('unsupported-host');chunks.push(value);
        }
      } finally {await reader.cancel();reader.releaseLock();}
      const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
      const actual=Array.from(new Uint8Array(await subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('');
      if (actual!==digest) throw new BridgeError('unsupported-host');
    } catch {throw new BridgeError('unsupported-host');}
    finally {clearTimeout(timer);}
  }
}

/** Use injected objects only. Never re-execute an entry or import private aliases. */
export function hostBindings(app, system) {
  const store=system?.Stores?.appleMusicStore, metadataStore=app?.musicKitStore;
  if (!store || !metadataStore || typeof store.$subscribe!=='function' ||
      typeof metadataStore.getOptimisticValues!=='function' || typeof app.v3!=='function') throw new BridgeError('unsupported-host');
  const am=new Proxy(store,{get(target,key){
    if (key==='setPlaybackRate') return value=>target.setPlaybackSpeed(value);
    return Reflect.get(target,key);
  }});
  const metadata={
    subscribe:fn=>metadataStore.$subscribe(fn,{detached:true}),
    get isAuthorized(){return metadataStore.isAuthorized;},
    get player(){
      const item=store.nowPlayingItem, status=metadataStore.getOptimisticValues(item?.id || '');
      return {nowPlayingItem:item,inLibrary:!!status.addedToLibrary,inFavorites:!!status.favorited};
    },
    setOptimisticValue:(...args)=>metadataStore.setOptimisticValue(...args),
  };
  const listeners=new Map();let unsubscribe,player,removePlayer=()=>{},active=false,pendingItem=false;
  const stamp=()=>({state:store.state,id:store.nowPlayingItem?.id,time:store.currentTime,
    queue:store.queue,position:store.queuePosition,total:store.queue.length,
    volume:store.volume,repeat:store.repeatMode,shuffle:store.isShuffled,
    loading:!!store.isSwitching||!!store.isSeeking});
  let previous=stamp();
  const emit=(name)=>{for(const fn of [...(listeners.get(name) || [])])fn();};
  // nowPlayingItem is computed from a shallow queue in the pinned host. A
  // Pinia state subscription alone need not see an in-place track replacement.
  // The engine's metadata events supply item hints independently of that store.
  const itemHint=()=>{
    if (!active || pendingItem) return;
    pendingItem=true;
    queueMicrotask(()=>{pendingItem=false;if(active)emit('playbackStatus.nowPlayingItemDidChange');});
  };
  const bindPlayer=()=>{
    if (player===store.player) return;
    removePlayer();player=store.player;
    const target=player?.events;
    if (!target?.addEventListener || !target?.removeEventListener) {removePlayer=()=>{};return;}
    const events=['MKLite:itemIdChanged','MKLite:resolvedItemChanged','MKLite:loadedmetadata','MKLite:crossfadeComplete'];
    for(const event of events)target.addEventListener(event,itemHint);
    removePlayer=()=>{for(const event of events)target.removeEventListener(event,itemHint);};
  };
  const changed=()=>{
    bindPlayer();
    const next=stamp(),events=[];
    for (const [keys,event] of [
      [['state'],'playbackStatus.playbackStateDidChange'],[['id'],'playbackStatus.nowPlayingItemDidChange'],
      [['time'],'playbackStatus.playbackTimeDidChange'],[['queue','position','total'],'queueStatus.queueChanged'],
      [['volume'],'playerStatus.volumeDidChange'],[['repeat'],'playerStatus.repeatModeDidChange'],
      [['shuffle'],'playerStatus.shuffleModeDidChange'],[['loading'],'playbackStatus.loadingDidChange']]) {
      if(keys.some(key=>next[key]!==previous[key]))events.push(event);
    }
    previous=next;
    // A move can mutate the queue in place without changing position or count.
    if (!events.length) events.push('queueStatus.queueChanged');
    for(const event of events)emit(event);
  };
  const bus={emit,on(name,callback){
    if (!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(callback);
    if (!active) {
      active=true;bindPlayer();
      unsubscribe=store.$subscribe(changed,{detached:true,flush:'sync'});
      // Some hosts initialize the player only after the plugin connects.
      store.onPlayerReady?.(()=>{if(active)bindPlayer();});
    }
    return ()=>{listeners.get(name).delete(callback);if([...listeners.values()].every(s=>!s.size)){
      active=false;unsubscribe?.();unsubscribe=undefined;removePlayer();player=undefined;
    }};
  }};
  return {am,metadata,bus,
    // The pinned active frontend explicitly rethrows when this option is true.
    api:(path,args,options)=>app.v3(path,args,{...options,throwOnHTTPError:true}),
    storefront:()=>'$STOREFRONT',lyrics:id=>app.lyrics.getLyrics(id)};
}
