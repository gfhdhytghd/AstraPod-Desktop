import {CiderAdapter, connectBridge} from './core.mjs';
import config from './connection.mjs';
// Pinned internal helpers propagate Apple errors. No credential getter is used.
import {fi as api, dt as useStorefrontStore, Ai as useMembershipStore} from '/assets/ws-BYB7BHl2.js';
import {r as initEvents,t as bus} from '/assets/PlaybackEventBus--BJS1dhm.js';
export default {
  identifier:'org.podbox.cider-bridge', name:'PodBox Cider Bridge',
  description:'Local authenticated metadata and explicit remote controls',
  version:'1.0.0', author:'PodBox', pluginKitVersion:'4',
  async setup() {
    globalThis.__podboxBridgeStop?.();
    initEvents();
    const app=globalThis.CiderApp, am=globalThis.__PLUGINSYS__?.Stores?.appleMusicStore;
    if (!am) throw new Error('PodBox unsupported-host');
    const storageKey='podbox.bridge.secret.v1';
    let secret=localStorage.getItem(storageKey);
    if (!/^[a-f0-9]{64}$/.test(secret || '')) {
      secret=await globalThis.__PLUGINSYS__.Dialog.createPrompt(
        'Paste the PodBox bridge secret from the 0600 cider-plugin.secret file.', 'PodBox connection');
      if (!/^[a-f0-9]{64}$/.test(secret || '')) return;
      localStorage.setItem(storageKey,secret);
    }
    const membership=useMembershipStore();
    const metadata={
      subscribe:fn=>app.musicKitStore.$subscribe(fn,{detached:true}),
      get isAuthorized(){return app.musicKitStore.isAuthorized;},
      get player(){
        const item=am.nowPlayingItem, id=item?.id || '', type=item?.type || 'songs';
        return {nowPlayingItem:item,inLibrary:membership.isInLibrary(id,type),inFavorites:membership.isFavorited(id,type)};
      },
      setOptimisticValue:(key,id,value)=>app.musicKitStore.setOptimisticValue(key,id,value),
    };
    const adapter=new CiderAdapter({am,metadata,api,bus,lyrics:id=>app.lyrics.getLyrics(id),
      storefront:()=>useStorefrontStore().sid,online:()=>navigator.onLine});
    const stopConnection=connectBridge(adapter,{...config,secret});
    const networkChange=()=>bus.emit('playbackStatus.nowPlayingStatusDidChange',{});
    window.addEventListener('online',networkChange);
    window.addEventListener('offline',networkChange);
    globalThis.__podboxBridgeStop=()=>{
      stopConnection();window.removeEventListener('online',networkChange);window.removeEventListener('offline',networkChange);
    };
    window.addEventListener('beforeunload',()=>globalThis.__podboxBridgeStop?.(),{once:true});
  }
};
