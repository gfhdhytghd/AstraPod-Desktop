"""FU31 dead/stale tty and app sessions. Memory ports only; no serial access."""
from pathlib import Path
import sys,struct,tempfile,unittest
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import podlink as wire
import hrcider
from hrcider_cache import Cache

class Port:
    def __init__(self):self.incoming=bytearray();self.written=[]
    def read(self,n):out=bytes(self.incoming[:n]);del self.incoming[:n];return out
    def write(self,data):self.written.extend(wire.Decoder().feed(data));return len(data)
    def feed(self,kind,seq,payload):self.incoming.extend(wire.encode(kind,seq,payload))

class WireLiveness(unittest.TestCase):
    def test_incoming_foreground_pings_cannot_keep_a_dead_write_side_open(self):
        now=[0.]
        with patch.object(wire.time,'monotonic',side_effect=lambda:now[0]):
            port=Port();link=wire.Link(port,2);link.opened=True
            link.keepalive_interval=1;link.keepalive_timeout=3;link.nonce_keepalive=True
            with self.assertRaises(wire.LinkClosed):
                for tick in range(1,7):
                    now[0]=float(tick);port.feed(wire.PING,100+tick,b'PLFG\x02');link.poll()
            self.assertLessEqual(now[0],5)
    def test_new_peer_handshake_invalidates_the_old_open_session(self):
        port=Port();link=wire.Link(port,2);link.opened=True
        port.feed(wire.HELLO,0,struct.pack('<HI',1,16384))
        with self.assertRaises(wire.LinkClosed):link.poll()
    def test_only_matching_nonce_and_sequence_satisfy_the_probe(self):
        now=[0.]
        with patch.object(wire.time,'monotonic',side_effect=lambda:now[0]):
            port=Port();link=wire.Link(port,2);link.opened=True
            link.keepalive_interval=1;link.keepalive_timeout=3;link.nonce_keepalive=True
            now[0]=1.01;link.poll();probe=next(f for f in port.written if f.type==wire.PING)
            self.assertEqual(len(probe.payload),16)
            now[0]=2;port.feed(wire.PONG,probe.seq,b'old nonce');link.poll();self.assertIsNotNone(link.keepalive)
            port.feed(wire.PONG,probe.seq+1,probe.payload);link.poll();self.assertIsNotNone(link.keepalive)
            port.feed(wire.PONG,probe.seq,probe.payload);link.poll();self.assertIsNone(link.keepalive)

class AppLiveness(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.clock=[0.]
        self.time=patch.object(hrcider.time,'monotonic',side_effect=lambda:self.clock[0]);self.time.start()
        self.cache=Cache(self.tmp.name);self.bridge=hrcider.Bridge(client=Mock(),cache=self.cache,volume=Mock())
        self.bridge.state=dict(duration=10000,position=1,rate=1000,volume=37,shuffle=0,repeat=0,id='song',art='-',bg='181818',title='T',artist='A',album='B',muted=False,composer='')
        self.bridge.available=True;self.bridge.index.initial=False
        self.bridge.command(b'ci\t1\t131072\tcn18,neighbors,warm,play-ack,localstore,modes5,loading,localdiag-live')
        self.gen=self.bridge.generation
    def tearDown(self):self.bridge.close();self.time.stop();self.tmp.cleanup()
    def poll(self):
        frames=[];self.bridge.poll(lambda *f:frames.append(f));return [raw for _,raw in frames]
    def probe(self):return next(raw.decode().split('\t') for raw in self.poll() if raw.startswith(b'CH\t'))
    def test_initial_probe_follows_generation_announcement_without_credits(self):
        # Model the production device's generation gate: CH before CA is ignored.
        device_generation=0;acks=0
        for raw in self.poll():
            fields=raw.decode().split('\t')
            if fields[0]=='CA':device_generation=int(fields[1])
            if fields[0]=='CH' and int(fields[1])==device_generation:
                self.bridge.command(b'ch'+raw[2:]);acks+=1
        self.assertEqual(acks,1)
        self.assertIsNone(self.bridge.live_pending)
        self.assertEqual(self.bridge.scheduler.credit,0)
    def test_no_app_requests_and_successful_cn_writes_expire_in_three_seconds(self):
        # The link send callback always succeeds. CN pushes do not count as RX.
        self.poll();self.clock[0]=3.01
        with self.assertRaisesRegex(RuntimeError,'liveness'):self.poll()
    def test_wrong_generation_and_old_nonce_do_not_extend_deadline(self):
        _,gen,nonce=self.probe()
        self.clock[0]=2.9;self.bridge.command(f'ch\t{int(gen)+1}\t{nonce}'.encode());self.bridge.command(f'ch\t{gen}\t{"0"*16}'.encode())
        self.clock[0]=3.01
        with self.assertRaisesRegex(RuntimeError,'liveness'):self.poll()
    def test_idle_device_keeps_session_without_art_page_or_control_requests(self):
        for tick in range(31):
            self.clock[0]=float(tick)
            for raw in self.poll():
                if raw.startswith(b'CH\t'):
                    self.bridge.command(b'ch'+raw[2:])
        self.assertTrue(self.bridge.opened)
    def test_repeated_ci_from_a_new_boot_demands_transport_reopen(self):
        self.poll();old=self.gen
        self.bridge.command(b'ci\t1\t131072\tcn18')
        with self.assertRaisesRegex(RuntimeError,'session'):self.poll()
        self.assertEqual(self.bridge.generation,old)
    def test_legacy_firmware_is_not_forced_into_an_unadvertised_app_probe(self):
        self.bridge.disconnect();self.bridge.command(b'ci\t1\t131072\tcn18,loading,localdiag')
        self.clock[0]=60.;self.assertFalse(any(f.startswith(b'CH\t') for f in self.poll()))

class DaemonRecovery(unittest.TestCase):
    def test_expired_application_closes_port_resets_and_reopens_without_restart(self):
        # Actual daemon run/close/open paths, memory serial port only. Other app
        # services are inert; this is not a serial/device integration test.
        import herdremote,types,threading,collections
        daemon=object.__new__(herdremote.Daemon)
        daemon.disk_control=Mock(serial_lease=None)
        daemon.plugin_link=Mock()
        daemon.stopping=threading.Event();daemon.args=types.SimpleNamespace(port='/fake/tty')
        daemon.cider=Mock();daemon.cider.check_liveness.side_effect=hrcider.SessionExpired('stale application')
        daemon.clock_sync=Mock();daemon.usage_keys=Mock();daemon.reset_session=Mock()
        daemon.pump_reads=Mock();daemon.adopt_foreground=Mock();daemon.flush_replies=Mock();daemon.push=Mock()
        old=Mock();old.opened=True;old.frames=collections.deque();old_port=Mock()
        daemon.link=old;daemon.port=old_port
        replacement=Mock();replacement.frames=collections.deque();new_port=Mock()
        def opened():
            replacement.opened=True;daemon.stopping.set()
        replacement.open.side_effect=opened
        with patch.object(herdremote.podlink,'serial_port',return_value=new_port) as serial_open, patch.object(herdremote.podlink,'Link',return_value=replacement),patch.object(herdremote.time,'sleep'):
            daemon.run_loop()
        serial_open.assert_called_once_with('/fake/tty',2)
        old_port.close.assert_called_once();daemon.cider.disconnect.assert_called_once()
        daemon.plugin_link.link_lost.assert_called_once()
        replacement.open.assert_called_once();daemon.reset_session.assert_called()
        self.assertIs(daemon.link,replacement)
        self.assertEqual((replacement.keepalive_interval,replacement.keepalive_timeout,replacement.nonce_keepalive),(1.,3.,True))
        replacement.send.assert_called() # new protocol hello
