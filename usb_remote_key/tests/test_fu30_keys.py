"""FU30 complete Apple IR bridge; offline, never opens USB or serial."""
from test_usb_remote_key import k, Recorder, decode
import unittest

class Keys(unittest.TestCase):
    def test_every_key_and_hold(self):
        # 07=right, 08=left, 04=centre in the supplied ir-menu.log capture.
        for code, name in ((2,'MENU'),(3,'MENU')):
            with self.subTest(code=code):
                recorder=Recorder(); forward=k.KeyForwarder(recorder)
                key=getattr(k,name)
                forward.frame(decode(bytes([0xee,0x87,code,0x59])),1)
                forward.frame(b'',1.1); forward.frame(b'',1.2)
                forward.poll(1.351)
                self.assertEqual(recorder.events,[(key,True,1),(key,True,1.1),(key,True,1.2),(key,False,1.351)])
    def test_replacement_and_orphan(self):
        r=Recorder();f=k.KeyForwarder(r)
        f.frame(b'',0);f.frame(bytes.fromhex('ee870459'),1)
        f.frame(bytes.fromhex('ee870759'),1.05);f.frame(b'\x01\x02\x03\x04',1.1)
        self.assertEqual(r.events,[])
        for code in (4,7,8):
            f.frame(bytes([0xee,0x87,code,0x59]),2)
            f.frame(b'',2.1);f.poll(2.5)
        self.assertEqual(r.events,[])

if __name__=='__main__':unittest.main()
