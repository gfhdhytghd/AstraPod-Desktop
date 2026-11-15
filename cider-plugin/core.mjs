/** PodBox metadata/control adapter. Never serializes host objects or credentials. */
export const VERSION = 1;
export const MAX_BYTES = 2 * 1024 * 1024;
const ATTRS = ['name','artistName','albumName','albumArtistName','composerName','durationInMillis',
  'trackCount','trackNumber','discNumber','releaseDate','dateAdded','genreNames','contentRating',
  'inFavorites','inLibrary','personalRating','rating','value','hasLyrics','url'];
const EVENTS = ['playbackStatus.playbackStateDidChange','playbackStatus.nowPlayingItemDidChange',
  'playbackStatus.playbackTimeDidChange','playbackStatus.loadingDidChange','queueStatus.queueChanged','playerStatus.volumeDidChange',
  'playerStatus.shuffleModeDidChange','playerStatus.repeatModeDidChange','playbackStatus.nowPlayingStatusDidChange'];
export class BridgeError extends Error {
  constructor(code) { super(code); this.code = code; }
}
export function errorCode(error) {
  if (error instanceof BridgeError) return error.code;
  // Only static reasons cross the bridge. Never include an exception body/URL.
  const status = error?.status ?? error?.httpStatus;
  if (status === 401 || status === 403 || /HTTP Error: (401|403)\b/.test(error?.message ?? '')) return 'unauthorized';
  if (error?.name === 'AbortError') return 'timeout';
  if (error instanceof TypeError || /offline|Network connection unavailable|Failed to fetch/i.test(error?.message ?? '')) return 'offline';
  return 'host-error';
}
const str = (v, n=2048) => typeof v === 'string' ? v.slice(0,n) : '';
const number = (v, fallback=0) => Number.isFinite(Number(v)) ? Number(v) : fallback;
function finite(v, low, high) {
  if (typeof v !== 'number' || !Number.isFinite(v) || v < low || v > high) throw new BridgeError('invalid-request');
  return v;
}
function index(v, max=1000000) {
  finite(v,0,max); if (!Number.isInteger(v)) throw new BridgeError('invalid-request'); return v;
}
export function resource(item, depth=0) {
  if (!item || typeof item !== 'object') return null;
  const a = item.attributes ?? item;
  const attributes = {};
  for (const key of ATTRS) {
    const v = a[key];
    if (typeof v === 'string') attributes[key] = str(v);
    else if (typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v))) attributes[key] = v;
    else if (key === 'genreNames' && Array.isArray(v)) attributes[key] = v.slice(0,32).map(x=>str(x,128));
  }
  if (a.artwork) attributes.artwork = Object.fromEntries(['url','bgColor','textColor1','textColor2','width','height']
    .filter(k=>typeof a.artwork[k]==='string'||typeof a.artwork[k]==='number').map(k=>[k,a.artwork[k]]));
  if (a.playParams) attributes.playParams = Object.fromEntries(['id','kind','catalogId','libraryId','isLibrary']
    .filter(k=>['string','boolean'].includes(typeof a.playParams[k])).map(k=>[k,a.playParams[k]]));
  if (attributes.durationInMillis === undefined && Number.isFinite(a.durationInMilliseconds)) attributes.durationInMillis=a.durationInMilliseconds;
  const out = {id:str(item.id || a.playParams?.id,128), type:str(item.type || 'songs',32), attributes};
  if (typeof item.href === 'string') out.href = str(item.href,1024);
  if (depth < 2 && item.relationships) {
    out.relationships = {};
    for (const key of ['albums','artists','catalog','library','tracks']) {
      const relation = item.relationships[key];
      if (relation) out.relationships[key] = page(relation,depth+1);
    }
  }
  return out;
}
export function page(value, depth=0) {
  const out = {};
  if (Array.isArray(value?.data)) out.data = value.data.map(x=>resource(x,depth)).filter(Boolean);
  if (value?.results && typeof value.results === 'object') {
    out.results = {};
    for (const key of ['library-songs','library-albums','library-artists','library-playlists','songs','albums','playlists']) {
      if (value.results[key]) out.results[key] = page(value.results[key],depth);
    }
  }
  if (!out.data && !out.results) throw new BridgeError('host-error');
  if (typeof value.next === 'string') out.next = str(value.next,2048);
  if (Number.isFinite(value.meta?.total)) out.meta = {total:value.meta.total};
  return out;
}
export class CiderAdapter {
  constructor({am,metadata,api,bus,lyrics,storefront,online=()=>true}) {
    this.am=am;this.metadata=metadata;this.api=api;this.bus=bus;this.lyrics=lyrics;
    this.storefront=storefront;this.online=online;this.loading=false;
  }
  check() {
    if (!this.online()) throw new BridgeError('offline');
    if (this.metadata.isAuthorized === false) throw new BridgeError('unauthorized');
  }
  snapshot() {
    const am=this.am, p=this.metadata.player;
    const item=resource(p.nowPlayingItem);
    const info=item ? {...item.attributes,id:item.id,type:item.type,
      inFavorites:!!p.inFavorites,inLibrary:!!p.inLibrary} : null;
    return {state:str(am.state || p.playbackState,32)||'stopped',nowPlaying:info,
      time:{currentTime:number(am.currentTime ?? p.currentPlaybackTime)},
      volume:number(am.volume),rate:number(am.getPlaybackRate?.() ?? am.audioElement?.playbackRate,1),
      repeatMode:({none:0,one:1,all:2})[am.repeatMode]??0,shuffleMode:am.isShuffled?1:0,
      loading:this.loading || !!am.isSwitching || !!am.isSeeking,
      queue:{position:am.queuePosition,total:am.queue.length,neighbors:am.queue.slice(Math.max(0,am.queuePosition-1),am.queuePosition+2).map(i=>resource(i.track))}};
  }
  subscribe(callback) {
    const remove=EVENTS.map(name=>this.bus.on(name,()=>callback(name)));
    // These are event subscriptions only. Never read audio samples or sources.
    const element=this.am.audioElement;
    for (const name of ['waiting','stalled','loadstart','seeking','playing','canplay','seeked','pause','ended','error','ratechange']) {
      const fn=()=>{
        if (['waiting','stalled','loadstart','seeking'].includes(name)) this.loading=true;
        else this.loading=false;
        callback('playbackStatus.loadingDidChange');
      };
      element?.addEventListener(name,fn);
      remove.push(()=>element?.removeEventListener(name,fn));
    }
    if (this.metadata.subscribe) remove.push(this.metadata.subscribe(()=>callback('playbackStatus.nowPlayingStatusDidChange')));
    return ()=>remove.forEach(fn=>fn());
  }
  path(type,id) {
    if (!/^(?:library-)?(?:songs|albums|playlists)$/.test(type) || !/^[\w.:-]{1,128}$/.test(id)) throw new BridgeError('invalid-request');
    return (type.startsWith('library-')?'/v1/me/library/':'/v1/catalog/'+this.storefront()+'/')+type.replace('library-','')+'/'+encodeURIComponent(id);
  }
  async read(path,signal) {
    this.check();
    if (typeof path!=='string' || path.length>2048 || /[#\\]/.test(path)) throw new BridgeError('invalid-request');
    // Resolve a host-known storefront without reading any token.
    path=path.replace('$STOREFRONT',this.storefront());
    const parsed=new URL(path,'https://amp-api.music.apple.com');
    if (parsed.origin!=='https://amp-api.music.apple.com' ||
      !/^\/v1\/(?:me\/(?:library(?:\/(?:songs|albums|artists|playlists)(?:\/[\w.:-]+(?:\/(?:tracks|catalog))?)?|\/(?:search|recently-added))?|ratings\/songs|recent\/played(?:\/tracks)?)|catalog\/(?:[A-Za-z-]{2,16}|\$STOREFRONT)\/(?:songs|albums|playlists)(?:\/[\w.:-]+(?:\/(?:tracks|library))?)?)$/.test(parsed.pathname)) throw new BridgeError('invalid-request');
    for (const key of ['offset','limit']) if (parsed.searchParams.has(key)) {
      const n=Number(parsed.searchParams.get(key)); index(n,key==='limit'?1000:1000000);
      if (key==='limit' && !n) throw new BridgeError('invalid-request');
    }
    const reply=await this.api(parsed.pathname+parsed.search,{}, {fetchOptions:{signal}});
    return page(reply.data);
  }
  async dispatch(path,body,method='GET',signal) {
    this.check();
    const am=this.am;
    if (typeof path!=='string'||path.length>4096) throw new BridgeError('invalid-request');
    const url=new URL(path,'http://127.0.0.1'), route=url.pathname;
    if (route==='/api/v1/amapi/run-v3' && method==='POST') return this.read(body?.path,signal);
    if (route==='/api/v2/playback' && method==='GET') return {data:this.snapshot()};
    if (route==='/api/v2/playback/time' && method==='GET') return {data:{currentTime:this.snapshot().time.currentTime}};
    if (route==='/api/v2/playback/rate' && method==='GET') return {data:{rate:this.snapshot().rate}};
    if (route==='/api/v2/library/now-playing/status' && method==='GET') {
      const p=this.metadata.player;return {data:{inLibrary:!!p.inLibrary,rating:p.inFavorites?1:0}};
    }
    if (route==='/api/v2/playback/audio-quality' && method==='GET') return {data:{flavorLabel:str(am.player?.flavorLabel || am.player?.flavor || 'Unknown',64)}};
    if (/^\/api\/v1\/lyrics\/[\w.-]{1,128}$/.test(route) && method==='GET') {
      const result=await this.lyrics(route.split('/').pop());
      return {data:Array.isArray(result)?result.map(x=>({text:str(x?.text || x?.content,16384)})):{text:str(result?.text || result,65536)}};
    }
    if (route==='/api/v2/queue/position' && method==='GET') return {data:{position:am.queuePosition,total:am.queue.length}};
    if (route==='/api/v2/queue' && method==='GET') {
      const offset=index(Number(url.searchParams.get('offset')||0)),limit=index(Number(url.searchParams.get('limit')||50),1000);
      return {data:{items:am.queue.slice(offset,offset+limit).map(i=>({track:resource(i.track)})),position:am.queuePosition,total:am.queue.length}};
    }
    if (!['POST','PUT','PATCH','DELETE'].includes(method)) throw new BridgeError('unsupported-operation');
    const actions={
      '/api/v1/playback/play':()=>am.play(), '/api/v1/playback/playpause':()=>am.playpause(),
      '/api/v1/playback/pause':()=>am.pause(), '/api/v2/playback/pause':()=>am.pause(),
      '/api/v1/playback/next':()=>am.skipToNext(), '/api/v1/playback/previous':()=>am.skipToPrevious(),
      '/api/v1/playback/seek':()=>am.seekTo(finite(body?.position,0,86400)),
      '/api/v2/playback/rate':()=>am.setPlaybackRate(finite(body?.rate,.25,4)),
      '/api/v2/playback/volume':()=>{am.volume=finite(body?.volume,0,1);},
      '/api/v2/queue/jump':()=>am.jumpToQueuePosition(index(body?.index,am.queue.length-1)),
      '/api/v2/queue/move':()=>am.moveQueueItemAbsolute(index(body?.from,am.queue.length-1),index(body?.to,am.queue.length-1)),
      '/api/v2/queue/add-later':()=>am.addItemToQueueByHref(this.path(body?.type,body?.id),'last'),
    };
    if (actions[route]) {await actions[route]();return {status:'ok'};}
    if (route==='/api/v2/playback/shuffle') {
      if (typeof body?.enabled!=='boolean') throw new BridgeError('invalid-request');
      await am.setShuffleMode(body.enabled);return {status:'ok'};
    }
    if (route==='/api/v2/playback/repeat') {
      if (!['none','one','all'].includes(body?.mode)) throw new BridgeError('invalid-request');
      await am.setRepeatMode(body.mode);
      if (am.repeatMode!==body.mode) throw new BridgeError('host-error');return {status:'ok'};
    }
    if (route==='/api/v2/playback/play-collection') {
      const href=this.path(body?.type,body?.id);
      const item=(await this.read(href,signal)).data?.[0];
      if (!item) throw new BridgeError('host-error');
      await am.ensurePlayer();await am.setQueueFromCollection(item,{play:true});return {status:'ok'};
    }
    if (route==='/api/v1/playback/play-item-href' || route==='/api/v2/playback/play-href') {
      const result=await this.read(body?.href,signal);
      if (!result.data?.length) throw new BridgeError('host-error');
      await am.ensurePlayer();
      const item=result.data[0];
      if (/albums|playlists/.test(item.type)) await am.setQueueFromCollection(item,{play:true});
      else await am.setQueueFromItems(result.data,0,{play:true,shuffle:!!am.isShuffled,
        ...(result.next && {continuation:{id:'',type:'library-songs',next:result.next,total:result.meta?.total,loaded:result.data.length}})});
      return {status:'ok'};
    }
    if (route.startsWith('/api/v2/library/now-playing/')) {
      const current=resource(this.metadata.player.nowPlayingItem),params=current?.attributes.playParams;
      const id=params?.catalogId || current?.id;
      if (!id || !/^[\w.:-]{1,128}$/.test(id)) throw new BridgeError('invalid-request');
      const type=(current.type || 'songs').replace('library-','');
      if (!['songs','albums','playlists'].includes(type)) throw new BridgeError('invalid-request');
      if (route.endsWith('/love') || route.endsWith('/rating')) {
        const remove=method==='DELETE';
        await this.api('/v1/me/ratings/'+type+'/'+encodeURIComponent(id),{},
          {fetchOptions:{signal,method:remove?'DELETE':'PUT',...(!remove&&{body:JSON.stringify({attributes:{value:1}})})}});
        this.metadata.setOptimisticValue?.('favorited',current.id,!remove);
      } else if (route.endsWith('/add')) {
        await this.api('/v1/me/library',{['ids['+type+']']:id},{paramsInURL:true,fetchOptions:{signal,method:'POST'}});
        this.metadata.setOptimisticValue?.('addedToLibrary',current.id,true);
      } else throw new BridgeError('unsupported-operation');
      return {status:'ok'};
    }
    throw new BridgeError('unsupported-operation');
  }
}

export function connectBridge(adapter, config, WebSocketClass=globalThis.WebSocket) {
  const endpoint=new URL(config.url);
  if (endpoint.protocol!=='ws:' || endpoint.hostname!=='127.0.0.1' || endpoint.pathname!=='/' || endpoint.username || endpoint.password || endpoint.search || endpoint.hash || !/^[a-f0-9]{64}$/.test(config.secret)) throw new BridgeError('invalid-request');
  let ws, reconnect, handshake, closed=false, ready=false, delay=250, active=0, mutation=false;
  const controllers=new Set();
  const send=value=>{
    if (ws?.readyState!==1) return;
    const raw=JSON.stringify(value);
    if (new TextEncoder().encode(raw).length>MAX_BYTES || ws.bufferedAmount>MAX_BYTES) throw new BridgeError('reply-too-large');
    ws.send(raw);
  };
  const reason=()=>{try {adapter.check();return '';} catch(error){return errorCode(error);}};
  const push=event=>{
    if (!ready) return;
    try {send({v:VERSION,type:'event',event,state:adapter.snapshot(),reason:reason()});}
    catch {ws?.close(1011,'bridge event failed');}
  };
  const unsubscribe=adapter.subscribe(push);
  function open() {
    if (closed) return;
    ready=false;
    ws=new WebSocketClass(config.url);
    handshake=setTimeout(()=>ws.close(1008,'hello timeout'),2500);
    ws.onopen=()=>{try {send({v:VERSION,type:'hello',secret:config.secret,state:adapter.snapshot(),reason:reason()});} catch {ws.close(1011,'hello failed');}};
    ws.onerror=()=>{}; // Close drives reconnect; no host/browser error text is logged.
    ws.onclose=()=>{
      ready=false;clearTimeout(handshake);
      for (const controller of controllers) controller.abort();
      if (!closed) {reconnect=setTimeout(open,delay);delay=Math.min(10000,delay*2);}
    };
    ws.onmessage=async event=>{
      const socket=ws;
      if (typeof event.data!=='string' || new TextEncoder().encode(event.data).length>MAX_BYTES) {socket.close(1009,'frame bound');return;}
      let request;
      try {request=JSON.parse(event.data);} catch {socket.close(1008,'protocol');return;}
      if (!request || typeof request!=='object' || request.v!==VERSION) {socket.close(1008,'version');return;}
      if (!ready) {
        if (request.type!=='hello'||request.ok!==true) {socket.close(1008,'hello');return;}
        ready=true;delay=250;clearTimeout(handshake);push('playbackStatus.nowPlayingItemDidChange');return;
      }
      if (request.type!=='request'||typeof request.id!=='string'||request.id.length>64) {socket.close(1008,'protocol');return;}
      const mutates=!(request.method==='GET'||request.path==='/api/v1/amapi/run-v3');
      if (active>=16 || (mutates && mutation)) {send({v:VERSION,type:'reply',id:request.id,error:'busy'});return;}
      const controller=new AbortController();controllers.add(controller);active++;if(mutates)mutation=true;
      let timer;
      try {
        const ms=Math.min(10000,Math.max(1,number(request.timeoutMs,800)));
        const expired=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(new BridgeError('timeout'));},ms);});
        // A timed-out mutation may still finish inside the host. Keep the gate
        // until it settles; never replay it and never queue another mutation.
        const operation=Promise.resolve().then(()=>adapter.dispatch(request.path,request.body,request.method,controller.signal));
        operation.finally(()=>{active--;controllers.delete(controller);if(mutates)mutation=false;}).catch(()=>{});
        const data=await Promise.race([operation,expired]);
        if (socket===ws && ready) {send({v:VERSION,type:'reply',id:request.id,data});push('playbackStatus.nowPlayingStatusDidChange');}
      } catch(error) {
        if (socket===ws && ready) {
          try {send({v:VERSION,type:'reply',id:request.id,error:errorCode(error)});} catch {socket.close(1011,'reply failed');}
        }
      } finally {clearTimeout(timer);}
    };
  }
  open();
  return ()=>{closed=true;clearTimeout(reconnect);clearTimeout(handshake);unsubscribe();for(const c of controllers)c.abort();ws?.close(1001,'plugin unload');};
}
