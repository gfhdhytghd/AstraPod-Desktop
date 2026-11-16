"""FU41 file cap, process serialization, and interrupted rotation. No devices."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest import mock
import pytest
import hrlogcap
import hrremotelog


def total(path):
    return sum(p.stat().st_size for p in (path, path.with_name(path.name+'.1')) if p.exists())


def test_remote_log_uses_one_five_mib_total_budget(tmp_path):
    path = tmp_path/'remote-live.log'
    writer = hrremotelog.Writer(path)
    assert writer.max_bytes * 2 == hrlogcap.TOTAL_BYTES == 5*1024*1024
    for i in range(1800):
        writer._append(i, 0, b'LOG '+bytes([65+i%26])*4096, 0)
        assert total(path) <= hrlogcap.TOTAL_BYTES
    assert 'mono=1799.000000 LOG ' in path.read_text()
    assert max(p.stat().st_size for p in tmp_path.iterdir()) <= hrlogcap.PART_BYTES


def test_existing_oversized_logs_and_huge_record(tmp_path):
    p = tmp_path/'remote-live.log'
    p.write_bytes(b'old\n'*(hrlogcap.TOTAL_BYTES//4+1))
    p.with_name(p.name+'.1').write_bytes(b'old backup\n'*600000)
    data=b'NEW\n'*(hrlogcap.TOTAL_BYTES//4+100)
    hrlogcap.append(p,data)
    assert total(p)<=hrlogcap.TOTAL_BYTES
    assert p.read_bytes().endswith(b'NEW\n')
    assert p.with_name(p.name+'.1').read_bytes()+p.read_bytes() == data[-total(p):]


def test_threads_share_current_inode_and_keep_newest_records(tmp_path):
    p=tmp_path/'remote-live.log';p.touch();inode=p.stat().st_ino
    barrier=threading.Barrier(4)
    def write(n):
        for i in range(1000):
            hrlogcap.append(p,f'{n}:{i:04d} '.encode()+b'x'*100+b'\n',max_bytes=8192)
        barrier.wait()
        hrlogcap.append(p,f'FINAL {n}\n'.encode(),max_bytes=8192)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(write,range(4)))
    data=p.with_name(p.name+'.1').read_bytes()+p.read_bytes()
    assert total(p)<=16384 and p.stat().st_ino==inode
    for i in range(4):assert f'FINAL {i}\n'.encode() in data


def test_processes_serialize_rotation(tmp_path):
    p=tmp_path/'remote-live.log'
    script='''import sys
from pathlib import Path
import hrlogcap
for i in range(400):hrlogcap.append(Path(sys.argv[1]),(sys.argv[2]+':'+str(i)+' '+('x'*200)+'\\n').encode(),max_bytes=8192)
'''
    env={**os.environ,'PYTHONPATH':str(Path(hrlogcap.__file__).parent)}
    jobs=[subprocess.Popen([sys.executable,'-c',script,str(p),str(i)],env=env) for i in range(3)]
    assert all(j.wait(timeout=20)==0 for j in jobs)
    assert total(p)<=16384
    data=p.with_name(p.name+'.1').read_bytes()+p.read_bytes()
    # Completion order can vary; the last writer's final record must survive.
    assert any(f'{i}:399 '.encode() in data for i in range(3))


def test_failed_backup_sync_never_truncates_current(tmp_path):
    p=tmp_path/'remote-live.log';p.write_bytes(b'o'*92+b'OLDEND\n')
    before=p.read_bytes()
    with mock.patch.object(hrlogcap.os,'fsync',side_effect=OSError('fault')):
        with pytest.raises(OSError):hrlogcap.append(p,b'NEWEST\n',max_bytes=100)
    assert p.read_bytes()==before and total(p)<=200
    hrlogcap.append(p,b'NEWEST\n',max_bytes=100)
    assert p.read_bytes().endswith(b'NEWEST\n')


@pytest.mark.parametrize('phase',['copy','synced','truncated'])
def test_crash_mid_rotation_preserves_current_name_and_previous_tail(tmp_path,phase):
    p=tmp_path/'remote-live.log';p.write_bytes(b'o'*92+b'OLDEND\n')
    script='''import os,sys
from pathlib import Path
import hrlogcap
phase=sys.argv[2];original=Path.open
class Proxy:
 def __init__(self,f,path,mode):self.f=f;self.path=path;self.mode=mode
 def __getattr__(self,n):return getattr(self.f,n)
 def __enter__(self):return self
 def __exit__(self,*a):return self.f.__exit__(*a)
 def write(self,data):
  n=self.f.write(data);self.f.flush()
  if phase=='copy' and self.mode=='wb':os._exit(77)
  return n
 def truncate(self,n):
  value=self.f.truncate(n);self.f.flush()
  if phase=='truncated':os._exit(77)
  return value
Path.open=lambda path,mode,*a,**kw:Proxy(original(path,mode,*a,**kw),path,mode)
fsync=os.fsync
def sync(fd):
 fsync(fd)
 if phase=='synced':os._exit(77)
hrlogcap.os.fsync=sync
hrlogcap.append(Path(sys.argv[1]),b'NEWEST\\n',max_bytes=100)
'''
    env={**os.environ,'PYTHONPATH':str(Path(hrlogcap.__file__).parent)}
    child=subprocess.run([sys.executable,'-c',script,str(p),phase],env=env,timeout=20)
    assert child.returncode==77 and p.exists() and total(p)<=200
    combined=p.with_name(p.name+'.1').read_bytes()+p.read_bytes()
    assert b'OLDEND\n' in combined
    hrlogcap.append(p,b'RECOVERED\n',max_bytes=100)
    assert p.read_bytes().endswith(b'RECOVERED\n') and total(p)<=200
