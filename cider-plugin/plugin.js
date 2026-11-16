import {CiderAdapter, connectBridge} from './core.mjs';
import config from './connection.mjs';
import {verifyRenderer, hostBindings} from './renderer.mjs';
export default {
  identifier:'org.podbox.cider-bridge', name:'PodBox Cider Bridge',
  description:'Local authenticated metadata and explicit remote controls',
  version:'1.0.2', author:'PodBox', pluginKitVersion:'4',
  async setup() {
    globalThis.__podboxBridgeStop?.();
    await verifyRenderer(config.assets,location.href);
    const bindings=hostBindings(globalThis.CiderApp,globalThis.__PLUGINSYS__);
    const storageKey='podbox.bridge.secret.v1';
    let secret=localStorage.getItem(storageKey);
    if (!/^[a-f0-9]{64}$/.test(secret || '')) {
      secret=await globalThis.__PLUGINSYS__.Dialog.createPrompt(
        'Paste the PodBox bridge secret from the 0600 cider-plugin.secret file.', 'PodBox connection');
      if (!/^[a-f0-9]{64}$/.test(secret || '')) return;
      localStorage.setItem(storageKey,secret);
    }
    const adapter=new CiderAdapter({...bindings,online:()=>navigator.onLine});
    const stopConnection=connectBridge(adapter,{...config,secret});
    const networkChange=()=>bindings.bus.emit('playbackStatus.nowPlayingStatusDidChange',{});
    window.addEventListener('online',networkChange);
    window.addEventListener('offline',networkChange);
    globalThis.__podboxBridgeStop=()=>{
      stopConnection();window.removeEventListener('online',networkChange);window.removeEventListener('offline',networkChange);
    };
    window.addEventListener('beforeunload',()=>globalThis.__podboxBridgeStop?.(),{once:true});
  }
};
