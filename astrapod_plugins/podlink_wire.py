"""APL1 envelopes and local records for the resident PodLink bridge."""
from base64 import b64decode, b64encode
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
import os
import re
import struct


HEADER = struct.Struct("<4sB3xII")
U16 = struct.Struct("<H")
U32 = struct.Struct("<I")
END = struct.Struct("<II")
LOCAL_HEADER = struct.Struct("<4sQ")
MAGIC = b"APL1"
MSF1 = b"MSF1"
REGISTER = b"REG1"
LOCAL_DATA = b"DAT1"
MAX_DATA = 2048
MAX_FILE = 64 * 1024 * 1024
MAX_CREDIT = 2
MAX_PACKET = HEADER.size + 8 + MAX_DATA
MAX_LOCAL = LOCAL_HEADER.size + MAX_PACKET
MAX_CHANNELS = 32
CHANNEL = re.compile(rb"[a-z0-9][a-z0-9._-]{0,31}\Z")
OBJECT = re.compile(rb"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}\Z")


class Op(IntEnum):
    OPEN = 1
    OPEN_OK = 2
    CLOSE = 3
    TEXT = 4
    BINARY = 5
    PUSH = 6
    PULL = 7
    ACCEPT = 8
    CREDIT = 9
    CHUNK = 10
    FINISH = 11
    DONE = 12
    CANCEL = 13


class Status(IntEnum):
    OK = 0
    NO_HANDLER = 1
    BUSY = 2
    BAD_FRAME = 3
    IO = 4
    TIMEOUT = 5
    CANCELLED = 6
    LOST = 7
    LIMIT = 8


@dataclass(frozen=True)
class Packet:
    op: Op
    channel: int
    transfer: int = 0
    data: bytes = b""

    def __post_init__(self):
        if not isinstance(self.op, Op) or not isinstance(self.data, bytes):
            raise ValueError("invalid opcode or payload")
        if not 0 < self.channel <= 0xffffffff or not 0 <= self.transfer <= 0xffffffff:
            raise ValueError("invalid channel or transfer number")
        if (self.op >= Op.PUSH) != bool(self.transfer):
            raise ValueError("incorrect transfer number for opcode")
        if not valid_payload(self.op, self.data):
            raise ValueError("invalid APL1 payload")

    def encode(self):
        return HEADER.pack(MAGIC, self.op, self.channel, self.transfer) + self.data

    @classmethod
    def decode(cls, raw):
        if not HEADER.size <= len(raw) <= MAX_PACKET or raw[5:8] != b"\0\0\0":
            raise ValueError("invalid APL1 header")
        magic, op, channel, transfer = HEADER.unpack_from(raw)
        if magic != MAGIC:
            raise ValueError("unknown APL1 signature")
        return cls(Op(op), channel, transfer, raw[HEADER.size:])

    @classmethod
    def stop(cls, channel, status, transfer=0):
        return cls(Op.CANCEL if transfer else Op.CLOSE, channel, transfer,
                   U16.pack(status))


def valid_payload(op, data):
    size = len(data)
    if op == Op.OPEN:
        return CHANNEL.fullmatch(data) is not None
    if op in (Op.OPEN_OK, Op.DONE):
        return not size
    if op in (Op.CLOSE, Op.CANCEL):
        return size == U16.size and U16.unpack(data)[0] <= Status.LIMIT
    if op in (Op.TEXT, Op.BINARY):
        if size > MAX_DATA:
            return False
        if op == Op.TEXT:
            data.decode("utf-8")
        return True
    if op == Op.PUSH:
        return (size > U32.size and U32.unpack_from(data)[0] <= MAX_FILE
                and OBJECT.fullmatch(data[U32.size:]) is not None)
    if op == Op.PULL:
        return OBJECT.fullmatch(data) is not None
    if op == Op.ACCEPT:
        return size == U32.size and U32.unpack(data)[0] <= MAX_FILE
    if op == Op.CREDIT:
        return size == 1 and 0 < data[0] <= MAX_CREDIT
    if op == Op.CHUNK:
        return (8 < size <= 8 + MAX_DATA and data.startswith(MSF1)
                and U32.unpack_from(data, 4)[0] + size - 8 <= MAX_FILE)
    return op == Op.FINISH and size == END.size and END.unpack(data)[0] <= MAX_FILE


def socket_path():
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    return runtime / "podbox-herdremote" / "plugin-link.sock"


def registration(names):
    data = ",".join(sorted(names)).encode("ascii")
    parse_registration(REGISTER + data)
    return REGISTER + data


def parse_registration(raw):
    if not raw.startswith(REGISTER):
        raise ValueError("expected a channel registration")
    names = raw[len(REGISTER):].split(b",") if raw != REGISTER else []
    if (len(names) > MAX_CHANNELS or len(set(names)) != len(names)
            or any(CHANNEL.fullmatch(name) is None for name in names)):
        raise ValueError("invalid channel registration")
    return frozenset(name.decode("ascii") for name in names)


def encode_local(token, packet):
    return LOCAL_HEADER.pack(LOCAL_DATA, token) + packet.encode()


def decode_local(raw):
    if len(raw) < LOCAL_HEADER.size:
        raise ValueError("short local record")
    magic, token = LOCAL_HEADER.unpack_from(raw)
    if magic != LOCAL_DATA or not token:
        raise ValueError("invalid local record header")
    return token, Packet.decode(raw[LOCAL_HEADER.size:])


def child_record(token, packet):
    return {"type": "podlink", "token": token,
            "packet": b64encode(packet.encode()).decode("ascii")}


def parse_child(record):
    token = record.get("token")
    encoded = record.get("packet")
    if (type(token) is not int or not 0 < token < 2**64
            or not isinstance(encoded, str) or len(encoded) > ((MAX_PACKET + 2) // 3) * 4):
        raise ValueError("invalid plugin packet")
    return token, Packet.decode(b64decode(encoded, validate=True))
