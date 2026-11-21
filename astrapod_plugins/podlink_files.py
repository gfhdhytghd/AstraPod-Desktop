"""Serve APL1 file requests within a companion's selected directory."""
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import BinaryIO
import zlib

from .podlink_wire import END, MAX_CREDIT, MAX_DATA, MAX_FILE, MSF1, U32, Op, Packet, Status


TRANSFER_TIMEOUT = 10.0


@dataclass
class Transfer:
    token: int
    channel: int
    number: int
    total: int
    file: BinaryIO
    deadline: float
    target: Path | None = None
    temp: Path | None = None
    offset: int = 0
    crc: int = 0
    credit: int = 0
    finished: bool = False

    def packet(self, op, data=b""):
        return Packet(op, self.channel, self.number, data)

    def write(self, data):
        if self.file.write(data) != len(data):
            raise OSError("short file write")
        self.advance(data)

    def advance(self, data):
        self.offset += len(data)
        self.crc = zlib.crc32(data, self.crc)

    def close(self):
        try:
            self.file.close()
        finally:
            if self.temp is not None:
                self.temp.unlink(missing_ok=True)


class PodLinkFiles:
    """Return replies to validated file packets from the supervising host."""

    def __init__(self, directory, *, progress=None, clock=time.monotonic):
        self.directory = Path(directory).resolve(strict=True)
        if not self.directory.is_dir():
            raise ValueError("PodLink file root must be a directory")
        self.progress = progress
        self.clock = clock
        self.transfer = None

    def close(self):
        transfer, self.transfer = self.transfer, None
        if transfer is not None:
            transfer.close()

    def expire(self):
        transfer = self.transfer
        if transfer is None or self.clock() < transfer.deadline:
            return None
        reply = transfer.token, Packet.stop(transfer.channel, Status.TIMEOUT, transfer.number)
        self.close()
        return reply

    def _begin(self, token, packet):
        if self.transfer is not None:
            return (Packet.stop(packet.channel, Status.BUSY, packet.transfer),)
        if packet.op == Op.PUSH:
            total = U32.unpack_from(packet.data)[0]
            target = self.directory / packet.data[U32.size:].decode("ascii")
            file = tempfile.NamedTemporaryFile(dir=self.directory, prefix=".apl1-", delete=False)
            self.transfer = Transfer(token, packet.channel, packet.transfer, total, file,
                                     self.clock() + TRANSFER_TIMEOUT,
                                     target, Path(file.name), credit=MAX_CREDIT)
            return (self.transfer.packet(Op.ACCEPT, U32.pack(total)),
                    self.transfer.packet(Op.CREDIT, bytes([MAX_CREDIT])))

        source = self.directory / packet.data.decode("ascii")
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("PodLink source must be a regular file")
            if info.st_size > MAX_FILE:
                return (Packet.stop(packet.channel, Status.LIMIT, packet.transfer),)
            file = os.fdopen(fd, "rb")
            fd = -1
        finally:
            if fd >= 0:
                os.close(fd)
        self.transfer = Transfer(token, packet.channel, packet.transfer, info.st_size,
                                 file, self.clock() + TRANSFER_TIMEOUT)
        return (self.transfer.packet(Op.ACCEPT, U32.pack(info.st_size)),)

    def _progress(self, transfer):
        transfer.deadline = self.clock() + TRANSFER_TIMEOUT
        if self.progress is not None:
            self.progress(transfer.offset, transfer.total)

    def _receive_chunk(self, transfer, payload):
        offset = U32.unpack_from(payload, 4)[0]
        data = payload[8:]
        if (transfer.credit <= 0 or offset != transfer.offset
                or offset + len(data) > transfer.total):
            raise ValueError("chunk is outside the receive window")
        transfer.credit -= 1
        transfer.write(data)
        self._progress(transfer)
        if transfer.offset == transfer.total:
            return ()
        transfer.credit += 1
        return (transfer.packet(Op.CREDIT, b"\x01"),)

    def _send_chunks(self, transfer, credit):
        if transfer.finished:
            return ()  # Window credits can arrive after the last send.
        replies = []
        for _ in range(credit):
            size = min(MAX_DATA, transfer.total - transfer.offset)
            if not size:
                break
            data = transfer.file.read(size)
            if len(data) != size:
                raise OSError("short file read")
            replies.append(transfer.packet(Op.CHUNK, MSF1 + U32.pack(transfer.offset) + data))
            transfer.advance(data)
            self._progress(transfer)
        if transfer.offset == transfer.total:
            transfer.finished = True
            transfer.deadline = self.clock() + TRANSFER_TIMEOUT
            replies.append(transfer.packet(Op.FINISH, END.pack(transfer.total, transfer.crc)))
        return tuple(replies)

    def _finish(self, transfer, data):
        if END.unpack(data) != (transfer.total, transfer.crc) or transfer.offset != transfer.total:
            raise ValueError("file length or CRC32 differs")
        transfer.file.flush()
        os.fsync(transfer.file.fileno())
        transfer.file.close()
        os.replace(transfer.temp, transfer.target)
        transfer.temp = None
        self.close()
        return (transfer.packet(Op.DONE),)

    def handle(self, token, packet):
        transfer = self.transfer
        if packet.op == Op.CLOSE:
            if transfer and transfer.token == token:
                self.close()
            return ()
        try:
            if packet.op in (Op.PUSH, Op.PULL):
                return self._begin(token, packet)
            if (transfer is None or (token, packet.channel, packet.transfer) !=
                    (transfer.token, transfer.channel, transfer.number)):
                return ()
            if packet.op == Op.CANCEL:
                self.close()
                return ()
            if transfer.target is not None:
                if packet.op == Op.CHUNK:
                    return self._receive_chunk(transfer, packet.data)
                if packet.op == Op.FINISH:
                    return self._finish(transfer, packet.data)
            else:
                if packet.op == Op.CREDIT:
                    return self._send_chunks(transfer, packet.data[0])
                if packet.op == Op.DONE and transfer.finished:
                    self.close()
                    return ()
            raise ValueError("unexpected file packet")
        except (OSError, ValueError) as error:
            self.close()
            status = Status.IO if isinstance(error, OSError) else Status.BAD_FRAME
            return (Packet.stop(packet.channel, status, packet.transfer),)
