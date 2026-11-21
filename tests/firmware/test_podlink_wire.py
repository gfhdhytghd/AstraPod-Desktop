"""Compare Python PodLink framing with the firmware Rust wire ABI."""
import ctypes
import os
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

from _support import MISSING_FIRMWARE, find_firmware
import podlink as p


class CMessage(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint8), ("flags", ctypes.c_uint8),
                ("seq", ctypes.c_uint32), ("length", ctypes.c_uint32),
                ("data", ctypes.POINTER(ctypes.c_ubyte))]



class CParser(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * (p.MAX_MESSAGE + 20)), ("used", ctypes.c_uint),
                ("consumed", ctypes.c_uint)]



class WireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        firmware = find_firmware()
        if firmware is None:
            raise unittest.SkipTest(MISSING_FIRMWARE)
        with patch.object(sys, 'path', [str(firmware / 'tools/tests'), *sys.path]):
            from podlink_rust_build import shared_wire
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / 'wire.so'
            build = os.environ.get('PODLINK_TEST_BUILD', tmp)
            with patch.dict(os.environ, {'PODLINK_TEST_BUILD': build}):
                shared_wire(lib)
            cls.c = ctypes.CDLL(str(lib))
        cls.c.podlink_encode.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint32,
                                         ctypes.c_void_p, ctypes.c_uint]
        cls.c.podlink_decode.argtypes = [ctypes.POINTER(CParser), ctypes.c_ubyte,
                                         ctypes.POINTER(CMessage)]

    def test_cross_language_wire_and_random_fragmentation(self):
        rng = random.Random(721)
        for length in [0, 1, 4, 31, 512, 4095, 4096, 16383, 16384]:
            for kind in range(1, 8):
                payload = rng.randbytes(length)
                out = ctypes.create_string_buffer(p.MAX_MESSAGE + 20)
                n = self.c.podlink_encode(out, kind, 0x12345678, payload, length)
                expected = p.encode(kind, 0x12345678, payload)
                self.assertEqual(out.raw[:n], expected)
                parser, msg = CParser(), CMessage()
                for byte in expected[:-1]: self.assertEqual(self.c.podlink_decode(ctypes.byref(parser), byte, ctypes.byref(msg)), 0)
                self.assertEqual(self.c.podlink_decode(ctypes.byref(parser), expected[-1], ctypes.byref(msg)), 1)
                self.assertEqual(ctypes.string_at(msg.data, msg.length), payload)
                decoder, frames = p.Decoder(), []
                for i in range(0, n, 17): frames += decoder.feed(expected[i:i+17])
                self.assertEqual(frames, [p.Frame(kind, 0x12345678, payload)])


    def test_bulk_decoder_preserves_fragmentation_and_message_boundaries(self):
        self.c.podlink_decode_bytes.argtypes = [ctypes.POINTER(CParser), ctypes.c_void_p,
            ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(CMessage)]
        corrupt = bytearray(p.encode(p.TEXT, 9, b"bad crc")); corrupt[-1] ^= 1
        messages = [p.Frame(p.BINARY, 10, p.pattern(0,p.MAX_MESSAGE)),
                    p.Frame(p.TEXT, 11, b""), p.Frame(p.TEXT, 12, b"last")]
        stream = b"noise" + corrupt + b"".join(p.encode(f.type,f.seq,f.payload) for f in messages)
        for chunk_size in [1,7,64,512,4096,p.MAX_MESSAGE,p.MAX_MESSAGE+20]:
            parser, msg, actual = CParser(), CMessage(), []
            for offset in range(0,len(stream),chunk_size):
                chunk = bytes(stream[offset:offset+chunk_size])
                used = 0
                while used < len(chunk):
                    consumed = ctypes.c_uint()
                    rc = self.c.podlink_decode_bytes(ctypes.byref(parser), chunk[used:],
                        len(chunk)-used, ctypes.byref(consumed), ctypes.byref(msg))
                    self.assertTrue(consumed.value or rc)
                    used += consumed.value
                    if rc == 1:
                        actual.append(p.Frame(msg.type,msg.seq,ctypes.string_at(msg.data,msg.length)))
            self.assertEqual(actual,messages)


    def test_resync_retains_trailing_frames_and_drains_without_input(self):
        self.c.podlink_decode_bytes.argtypes = [ctypes.POINTER(CParser), ctypes.c_void_p,
            ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(CMessage)]
        expected = [p.Frame(p.TEXT, 2, b"first"), p.Frame(p.TEXT, 3, b"second")]
        for outer_length in (32, 47):
            stream = p.HEADER.pack(p.MAGIC, p.TEXT, 0, 0, 1, outer_length)
            stream += b"".join(p.encode(f.type, f.seq, f.payload) for f in expected)
            self.assertEqual(p.Decoder().feed(stream), expected)
            for chunk_size in (1, 7, 64, 4096):
                parser, msg, actual = CParser(), CMessage(), []
                for offset in range(0, len(stream), chunk_size):
                    chunk = stream[offset:offset + chunk_size]
                    used = 0
                    # Include zero-input calls to drain frames buffered during recovery.
                    for _ in range(len(chunk) + 100):
                        consumed = ctypes.c_uint()
                        rc = self.c.podlink_decode_bytes(ctypes.byref(parser), chunk[used:],
                            len(chunk) - used, ctypes.byref(consumed), ctypes.byref(msg))
                        used += consumed.value
                        if rc == 1:
                            actual.append(p.Frame(msg.type, msg.seq,
                                ctypes.string_at(msg.data, msg.length)))
                        if rc == 0 and used == len(chunk):
                            break
                    else:
                        self.fail("decoder did not drain")
                self.assertEqual(actual, expected, (outer_length, chunk_size))


    def test_16385_rejected_by_both_codecs(self):
        payload = p.pattern(0, 16385)
        with self.assertRaises(ValueError):
            p.encode(p.BINARY, 1, payload)
        out = ctypes.create_string_buffer(p.MAX_MESSAGE + 20)
        self.assertEqual(self.c.podlink_encode(out, p.BINARY, 1, payload, len(payload)), 0)
        parser, msg = CParser(), CMessage()
        header = p.HEADER.pack(p.MAGIC, p.BINARY, 0, 0, 1, len(payload))
        for byte in header[:-1]:
            self.assertEqual(self.c.podlink_decode(ctypes.byref(parser), byte, ctypes.byref(msg)), 0)
        self.assertEqual(self.c.podlink_decode(ctypes.byref(parser), header[-1], ctypes.byref(msg)), -2)
