#!/usr/bin/env python3
"""FU46 offline host/firmware integration, native 32-bit ASan+UBSan.

No device/serial or live Herdr writes. Requires an already captured data-dir
containing herdr-snapshot.json (herdr api snapshot), visible-panes.json and
usage.json. The snapshot is replayed by a private fake Herdr socket. The real
host Daemon/Link, real firmware serial/session/service, entire Agents screen,
model, navigation, Dock gesture, UTF-8/wrapping/detail code run together.
OS input, display/list/font, clock, voice bus, key tool and USB controller are
adapters. Cooperative service stepping is deterministic, not a scheduler test.
"""
import argparse,collections,copy,json,os,queue,select,socket,subprocess,sys,threading,time,types
from pathlib import Path
R=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(R/'tools/herdremote'),str(R/'tools')]
import herdremote,hrproto,hrherdr,podlink

class FakeHerdr(threading.Thread):
 def __init__(self,path,snapshot,panes):
  super().__init__(daemon=True);self.snapshot=snapshot;self.panes=panes;self.calls=[]
  self.sock=socket.socket(socket.AF_UNIX);self.sock.bind(str(path));self.sock.listen(8)
 def run(self):
  while True:
   try:c,_=self.sock.accept()
   except OSError:return
   with c:
    data=b''
    while not data.endswith(b'\n'):
     part=c.recv(65536)
     if not part:break
     data+=part
    if not data:continue
    q=json.loads(data);m=q['method'];p=q['params'];self.calls.append((m,p))
    if m=='ping':result={'capabilities':{'client_targeted_focus':True}}
    elif m=='session.snapshot':result={'snapshot':self.snapshot}
    elif m=='pane.read':result={'read':{'text':self.panes.get(p['pane_id'],'')}}
    elif m=='client.focus':result={'moved':[1]}
    elif m=='client.list':result={'clients':[{'origin':'local'}]}
    else:
     c.sendall(json.dumps({'id':q['id'],'error':{'message':'fake server rejected '+m}}).encode()+b'\n');continue
    c.sendall(json.dumps({'id':q['id'],'result':result}).encode()+b'\n')
 def close(self):
  self.sock.shutdown(socket.SHUT_RDWR);self.sock.close();self.join(3)

class Bus:
 def __init__(self):self.token=0;self.calls=[]
 def ready(self):pass
 def status(self):return (self.token,'recording' if self.token else 'idle')
 def call(self,m):
  self.calls.append(m)
  if m.startswith('HoldStart'):self.token=1
  elif m in ('HoldStop','Cancel'):self.token=0

class Port:
 def __init__(self,exe,out):
  self.err=(out/'sanitizer.log').open('w');self.trace=(out/'bridge.log').open('w')
  env=dict(os.environ,ASAN_OPTIONS='detect_leaks=1:halt_on_error=1',UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
  self.proc=subprocess.Popen([str(exe)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.err,text=True,bufsize=1,env=env)
  self.buffer=bytearray();self.wait=None;self.exited=False;self.collect(('READY',))
 def collect(self,ends):
  while True:
   # readline uses a pipe; the child only emits bounded lines and explicit
   # boundaries. The process timeout is enforced by the top-level runner.
   s=self.proc.stdout.readline()
   if not s:raise RuntimeError('firmware harness exited: '+str(self.proc.poll()))
   s=s.rstrip();self.trace.write('< '+s+'\n');self.trace.flush()
   if s.startswith('WIRE '):self.buffer.extend(bytes.fromhex(s[5:]));continue
   if s.startswith('WAIT '):self.wait=tuple(map(int,s.split()[1:]));self.exited=False
   if s.startswith('EXIT '):self.exited=True
   if any(s==e or s.startswith(e+' ') for e in ends):return s
 def rpc(self,s,ends=('OK',)):
  self.trace.write('> '+s+'\n');self.trace.flush();self.proc.stdin.write(s+'\n');self.proc.stdin.flush();return self.collect(ends)
 def write(self,data):self.rpc('RX '+data.hex());return len(data)
 def read(self,size):
  if not self.buffer:self.rpc('PUMP')
  b=bytes(self.buffer[:size]);del self.buffer[:size];return b
 def enter(self):return self.rpc('ENTER',('WAIT','EXIT'))
 def action(self,action='ACTION_NONE',button='NONE',ticks=10,held=False):return self.rpc(f'ACTION {action} {button} {ticks} {int(held)}',('WAIT','EXIT'))
 def close(self):
  if self.proc.poll() is None:
   if not self.exited and self.wait:self.action('ACTION_STD_MENU','MENU')
   self.proc.stdin.write('QUIT\n');self.proc.stdin.flush()
  code=self.proc.wait(timeout=10);self.err.close();self.trace.close();assert code==0,code

class KeyTool:
 def __init__(self):self.calls=[]
 def tap(self,*args):self.calls.append(args)

def run(exe,data,out,variant,iterations):
 out.mkdir(parents=True,exist_ok=True)
 snap=json.loads((data/'herdr-snapshot.json').read_text());snap=snap.get('result',{}).get('snapshot',snap)
 panes=json.loads((data/'visible-panes.json').read_text());usage=json.loads((data/'usage.json').read_text())
 if variant=='stress':
  # Boundary shapes through the real host encoder: more rows than each firmware
  # cap, long multibyte names, many tabs and dense waiting agents.
  snap=copy.deepcopy(snap)
  snap['workspaces']=[dict(workspace_id=f'w{i}',label='空间🙂e\u0301题'*80) for i in range(80)]
  snap['tabs']=[dict(tab_id=f'w{i}:t{t}',workspace_id=f'w{i}',label='标签'*80) for i in range(80) for t in range(12)]
  snap['panes']=[dict(pane_id=f'w{i}:p{p}',tab_id=f'w{i}:t0',agent='codex',agent_status='working',terminal_title_stripped='长标题🙂'*120) for i in range(80) for p in range(40)]
  snap['agents']=[];panes={p['pane_id']:'中文消息 '*100 for p in snap['panes']};snap['focused_workspace_id']='w0'
 elif variant=='no-questions':panes={k:'working 中文正常输出' for k in panes}
 sockpath=out/'herdr.sock'
 if sockpath.exists():raise RuntimeError('refuse to replace socket')
 server=FakeHerdr(sockpath,snap,panes);server.start()
 args=types.SimpleNamespace(socket=str(sockpath),state=None,port='RAM',key_tool='auto',key_scale=1,voice_focus=False,voice_max_hold=120,voice_method='HoldStart',disk_control_socket=str(out/'unused-disk.sock'))
 d=herdremote.Daemon(args);d.voice.bus=Bus();tool=KeyTool();d.keys.tool=tool
 d.usage.rows={r['svc']:r for r in usage};d.usage.request=lambda:None
 # The logger is production code, but any LOG test traffic stays in this run.
 d.remote_log.path=out/'remote-live.log'
 d.sessions.read=lambda meta:None
 d.appserver.read=lambda meta:None
 port=Port(exe,out);link=podlink.Link(port,2);d.port=port;d.link=link
 records=[];seq=[]
 original_send=link.send
 def send(kind,payload=b'',seq=None):
  raw=payload.encode() if isinstance(payload,str) else bytes(payload)
  records.append(dict(direction='host',type=kind,length=len(raw),hex=raw.hex()))
  return original_send(kind,payload,seq)
 link.send=send;link.open();d.reset_session();d.send(hrproto.hello(herdremote.VERSION));d.clock_sync.poll(d.send);d.refresh()
 def settle():
  for _ in range(12):
   link.poll();d.adopt_foreground()
   while link.frames:
    f=link.frames.popleft();records.append(dict(direction='device',type=f.type,length=len(f.payload),hex=f.payload.hex()))
    if f.type==podlink.TEXT:d.command(f.payload)
   d.push()
   if not port.buffer:break
 def action(a='ACTION_NONE',b='NONE',ticks=10,held=False):
  result=port.action(a,b,ticks,held);settle();seq.append(dict(action=a,button=b,result=result));return result
 try:
  settle()
  for i in range(iterations):
   port.enter();settle()
   for _ in range(4):action()
   # All entry variants leave with MENU. Add explicit usage, workspace and
   # talk actions on cold/warm entry; these mutate only the fake services.
   if i==1:
    if port.wait[0]==4:action('ACTION_STD_OK','RIGHT')
    action('ACTION_STD_OK','NONE')
    for _ in range(4):action()
    action('ACTION_STD_OK','NONE')
    for _ in range(4):action()
   if i==0:
    action('ACTION_STD_OK','RIGHT')
    for _ in range(3):action()
    action('ACTION_AGENTS_REMOTE_TALK','PLAY',10,True)
    action('ACTION_AGENTS_TALK_END','NONE',40,False)
   result=action('ACTION_STD_MENU','MENU');assert result=='EXIT 0',result
  port.close();d.voice.stop();d.remote_log.close()
  assert not (out/'sanitizer.log').read_text(),(out/'sanitizer.log').read_text()
  summary=dict(variant=variant,entries=iterations,workspaces=len(snap['workspaces']),tabs=len(snap['tabs']),panes=len(snap['panes']),messages=len(records),max_host_text=max((x['length'] for x in records if x['direction']=='host' and x['type']==2),default=0),host_text_types=dict(collections.Counter(bytes.fromhex(x['hex']).split(b'\t')[0].decode(errors='replace') for x in records if x['direction']=='host' and x['type']==2)),voice_calls=d.voice.bus.calls,key_calls=len(tool.calls),asan_ubsan='PASS',hardware_scheduler='not simulated')
  (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
  print(json.dumps(summary,ensure_ascii=False))
 finally:
  (out/'messages.json').write_text(json.dumps(records,indent=2)+'\n');(out/'actions.json').write_text(json.dumps(seq,indent=2)+'\n');(out/'herdr-calls.json').write_text(json.dumps(server.calls,indent=2)+'\n');server.close()

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--exe',type=Path,required=True);p.add_argument('--data',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--variant',choices=['captured','no-questions','stress'],default='captured');p.add_argument('--iterations',type=int,default=50);a=p.parse_args();run(a.exe.resolve(),a.data.resolve(),a.out.resolve(),a.variant,a.iterations)
