/** Offline fixture only. Not copied by the installer. */
import {EventEmitter} from 'node:events';
import {CiderAdapter} from './core.mjs';
export function makeHost() {
  const calls=[], emitter=new EventEmitter(), audio=new EventTarget();audio.playbackRate=1;
  const track=id=>({id,type:'songs',attributes:{name:'Song '+id,durationInMillis:100000,playParams:{id},artwork:{url:'https://example.test/{w}x{h}.jpg'}}});
  const metadata={isAuthorized:true,player:{nowPlayingItem:track('one'),inLibrary:true,inFavorites:false},
    setOptimisticValue:(key,id,value)=>{metadata.player[key==='favorited'?'inFavorites':'inLibrary']=value;}};
  const am={state:'playing',volume:.5,repeatMode:'none',isShuffled:false,currentTime:12,
    queue:[{track:track('one')},{track:track('two')}],queuePosition:0,audioElement:audio,
    play:()=>{calls.push('play');am.state='playing';},pause:()=>{calls.push('pause');am.state='paused';},
    playpause:()=>calls.push('toggle'),skipToNext:()=>calls.push('next'),skipToPrevious:()=>calls.push('previous'),
    seekTo:value=>{calls.push(['seek',value]);am.currentTime=value;},setPlaybackRate:value=>{audio.playbackRate=value;},
    setShuffleMode:value=>{am.isShuffled=value;},setRepeatMode:value=>{am.repeatMode=value;},
    ensurePlayer:async()=>{},setQueueFromCollection:async item=>{am.queue=[{track:track('one')},{track:track('two')}];am.queuePosition=0;calls.push(['collection',item.id]);},
    setQueueFromItems:async items=>{am.queue=items.map(track=>({track}));am.queuePosition=0;calls.push('items');},
    jumpToQueuePosition:async i=>{am.queuePosition=i;metadata.player.nowPlayingItem=am.queue[i].track;},
    addItemToQueueByHref:async(path,where)=>{calls.push(['add',where]);am.queue.push({track:track(path.split('/').pop())});},
    moveQueueItemAbsolute:async(from,to)=>{am.queue.splice(to,0,am.queue.splice(from,1)[0]);},
  };
  const api=async(path,args,options)=>{
    calls.push(['api',path,args,options?.fetchOptions?.method||'GET']);
    if(options?.fetchOptions?.method && options.fetchOptions.method!=='GET')return {data:{data:[]}};
    if(path.includes('/search'))return {data:{results:{'library-songs':{data:[track('one')]}}}};
    if(/\/(songs|albums|playlists)\/[^/?]+$/.test(path))return {data:{data:[path.includes('albums')?{id:'album',type:'library-albums',attributes:{name:'Album'}}:track(path.split('/').pop())]}};
    return {data:{data:[track('one'),track('two')],meta:{total:2}}};
  };
  const bus={on:(name,fn)=>{emitter.on(name,fn);return ()=>emitter.off(name,fn);}};
  const host=new CiderAdapter({am,metadata,api,bus,lyrics:async()=>({text:'Lyrics'}),storefront:()=> 'us'});
  return {host,am,metadata,calls,emitter,audio,track};
}
