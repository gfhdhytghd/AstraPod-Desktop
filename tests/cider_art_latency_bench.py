"""Host request -> native art ready -> framed pull -> simulated device CRC/ACK.
Actual host code and wall clock; fixture CDN and 1 ms/frame transport, no device.
"""
from pathlib import Path
import sys,io,time,threading,json,logging,statistics,zlib
r=Path(__file__).resolve().parents[1];o=Path(sys.argv[1]).resolve();o.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(r/'herdremote'))
from hrcider import Bridge,Client,render,decode_chunk
from hrcider_cache import Cache,art_id
from PIL import Image
logging.basicConfig(filename=o/'journal.log',filemode='w',level=logging.INFO,format='%(message)s')
stream=io.BytesIO();Image.new('RGB',(240,240),(87,54,190)).save(stream,format='PNG');png=stream.getvalue()
def fetch(*a,**k):return io.BytesIO(png)
cache=Cache(o/'cache');b=Bridge(client=Client(),cache=cache,fetch=fetch,volume=object())
b.state=dict(duration=1000,position=0,rate=0,volume=37,shuffle=0,repeat=0,id='fixture',art='-',bg='181818',title='Fixture',artist='A',album='B');b.available=True
b.command(b'ci\t1\t131072\tcn18,neighbors,warm,play-ack,localstore,modes5,localdiag');g=b.generation;b.command(f'cg\t{g}\t131072'.encode())
t=threading.Thread(target=b._work,daemon=True);t.start();req=0;results=[]
try:
 for tier in ('M','XS','S','L'):
  keys=[]
  for i in range(20):
   template=f'https://fixture.invalid/{tier}/{i}/{{w}}x{{h}}bb.jpg';key=art_id(template);keys.append(key);b.templates[key]=template
  for phase in ('cold-native','warm-native'):
   measurements=[]
   for key in keys:
    req+=1;start=time.monotonic();b.command(f'cr\t{g}\t{req}\t{key}\t{tier}\t0\t3\t0\t1'.encode());joined=bytearray();deadline=start+5
    while True:
     if time.monotonic()>deadline:raise RuntimeError('fixture art timeout')
     frame=b.scheduler.next_frame()
     if frame is None:time.sleep(.0002);continue
     kind,raw=frame
     if kind!=3:continue
     time.sleep(.001)  # explicit fixture transport assumption, not USB measurement
     chunk=decode_chunk(raw);assert chunk['request']==req and chunk['offset']==len(joined);joined.extend(chunk['data']);b.command(f'cg\t{g}\t{len(raw)}'.encode())
     if len(joined)==chunk['total']:
      assert zlib.crc32(joined)==chunk['crc'];break
    ready=b.art_times[req][3];ack=time.monotonic();b.command(f'lr\t{g}\t{req}\t{len(joined)}'.encode());measurements.append(((ready-start)*1000,(ack-start)*1000));assert req not in b.art_times
   vals=list(zip(*measurements));row=dict(tier=tier,phase=phase,n=20,request_ready_p50_ms=round(statistics.median(vals[0]),3),request_ready_p95_ms=round(sorted(vals[0])[18],3),request_ack_p50_ms=round(statistics.median(vals[1]),3),request_ack_p95_ms=round(sorted(vals[1])[18],3));results.append(row);print(json.dumps(row))
finally:
 b.stop.set();t.join(2);cache.close()
(o/'results.json').write_text(json.dumps(results,indent=2))
