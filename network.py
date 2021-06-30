import logging
from binascii import crc32
from queue import Queue, Empty
from selectors import DefaultSelector, EVENT_READ, EVENT_WRITE
from socket import socket, MSG_WAITALL
from struct import pack, unpack
from threading import Thread
from typing import Any, Dict, List, NamedTuple, Set, Tuple

from const import EOF, PacketSnippet, Flag, LEN_HEAD


class Packet(NamedTuple):
    flag: Flag
    body: bytes

    def __str__(self) -> str:
        return f'Flag: {self.flag} Len={self.length}'

    @property
    def length(self) -> int:
        return len(self.body)

    @property
    def chksum(self) -> int:
        return crc32(self.body)

    @staticmethod
    def load(flag: Flag, *args) -> 'Packet':
        '''将包体封包'''
        if flag == Flag.PULL or flag == Flag.PUSH:
            if isinstance(args[0], bytes):
                body = args[0]
            else:
                body = str(args[0]).encode('utf8')
        elif flag == Flag.SID or flag == Flag.ATTACH:
            body = pack('>H', *args)
        elif flag == Flag.FILE_COUNT:
            body = pack('>H', *args)
        elif flag == Flag.DIR_INFO:
            length = len(args[-1])
            body = pack(f'>2H{length}s', *args)
        elif flag == Flag.FILE_INFO:
            length = len(args[-1])
            body = pack(f'>2HQd16s{length}s', *args)
        elif flag == Flag.FILE_READY:
            body = pack('>H', *args)
        elif flag == Flag.FILE_CHUNK:
            length = len(args[-1])
            body = pack(f'>HI{length}s', *args)
        elif flag == Flag.DONE:
            body = pack('>I', EOF)
        elif flag == Flag.RESEND:
            body = pack('>BIH', *args)
        else:
            raise ValueError('Invalid flag')
        return Packet(flag, body)

    def pack(self) -> bytes:
        '''封包'''
        fmt = f'>BIH{self.length}s'
        return pack(fmt, self.flag, self.chksum, self.length, self.body)

    @staticmethod
    def unpack_head(head: bytes) -> Tuple[Flag, int, int]:
        '''解析 head'''
        flag, chksum, length = unpack('>BIH', head)
        return Flag(flag), chksum, length

    def unpack_body(self) -> Tuple[Any, ...]:
        '''将 body 解包'''
        if self.flag == Flag.PULL or self.flag == Flag.PUSH:
            return (self.body.decode('utf-8'),)  # dest path

        elif self.flag == Flag.SID or self.flag == Flag.ATTACH:
            return unpack('>H', self.body)  # Worker ID

        elif self.flag == Flag.FILE_COUNT:
            return unpack('>H', self.body)  # file count

        elif self.flag == Flag.DIR_INFO:
            # file_id | perm | path
            #   2B    |  2B  |  ...
            fmt = f'>2H{self.length - 4}s'
            return unpack(fmt, self.body)

        elif self.flag == Flag.FILE_INFO:
            # file_id | perm | size | mtime | chksum | path
            #   2B    |  2B  |  8B  |  8B   |  16B   |  ...
            fmt = f'>2HQd16s{self.length - 36}s'
            return unpack(fmt, self.body)

        elif self.flag == Flag.FILE_READY:
            return unpack('>H', self.body)  # file id

        elif self.flag == Flag.FILE_CHUNK:
            # file_id |  seq  | chunk
            #    2B   |  4B   |  ...
            fmt = f'>HI{self.length - 6}s'
            return unpack(fmt, self.body)

        elif self.flag == Flag.DONE:
            return unpack('>I', self.body)

        elif self.flag == Flag.RESEND:
            return unpack('>BIH', self.body)

        else:
            raise TypeError

    def is_valid(self, chksum: int):
        '''是否是有效的包体'''
        return self.chksum == chksum


class Buffer:
    __slots__ = ('waiting', 'remain', 'flag', 'chksum', 'data')

    def __init__(self,
                 waiting: PacketSnippet = PacketSnippet.HEAD,
                 remain: int = LEN_HEAD) -> None:
        self.waiting = waiting
        self.remain = remain
        self.flag: Any = None
        self.chksum: int = 0
        self.data: bytearray = bytearray()

    def reset(self):
        self.waiting = PacketSnippet.HEAD
        self.remain = LEN_HEAD
        self.flag = None
        self.chksum = 0
        self.data.clear()


class Cookie:
    def __init__(self) -> None:
        self.head = bytearray()
        self.body = bytearray()
        self.sent: Dict[bytes, bytes] = {}  # 已发送的包


class ConnectionPool:
    max_size = 128
    timeout = 0.001  # 1ms

    def __init__(self, size: int) -> None:
        self.size = min(size, self.max_size)
        # 发送、接收队列
        self.send_q: Queue[Packet] = Queue(self.size * 5)
        self.recv_q: Queue[Packet] = Queue(self.size * 5)

        # 所有 Socket
        self.socks: Set[socket] = set()

        # 发送、接收多路复用
        self.sender = DefaultSelector()
        self.receiver = DefaultSelector()

        self.is_working = True
        self.threads: List[Thread] = []

    def send(self, packet: Packet):
        '''发送'''
        self.send_q.put(packet)

    def recv(self, block=True, timeout=None) -> Packet:
        '''接收'''
        return self.recv_q.get(block, timeout)

    def add(self, sock: socket):
        '''添加 sock'''
        if len(self.socks) < self.size:
            self.sender.register(sock, EVENT_WRITE)
            self.receiver.register(sock, EVENT_READ, Buffer())
            self.socks.add(sock)
            return True
        else:
            return False

    def remove(self, sock: socket):
        '''删除 sock'''
        self.sender.unregister(sock)
        self.receiver.unregister(sock)
        self.socks.remove(sock)
        sock.close()

    def parse_head(self, buf: Buffer):
        '''解析 head'''
        # 解包
        buf.flag, buf.chksum, buf.remain = Packet.unpack_head(buf.data)

        # 切换 Buffer 为等待接收 body 状态
        buf.waiting = PacketSnippet.BODY
        buf.data.clear()

    def parse_body(self, buf: Buffer):
        '''解析 body'''
        pkt = Packet(buf.flag, bytes(buf.data))
        # 检查校验码
        if pkt.is_valid(buf.chksum):
            logging.debug(f'-> {pkt.flag.name}: length={pkt.length} chksum={pkt.chksum}')
            self.recv_q.put(pkt)  # 正确的数据包放入队列
        else:
            logging.error('丢弃错误包，请求重传')
            resend_pkt = Packet.load(Flag.RESEND, buf.flag, buf.chksum, pkt.length)
            self.send_q.put(resend_pkt)

        # 一个数据包解析完成后，重置 buf
        buf.reset()

    def _send(self):
        '''从 send_q 获取数据，并封包发送到对端'''
        while self.is_working:
            for key, _ in self.sender.select():
                try:
                    packet = self.send_q.get(timeout=0.1)
                except Empty:
                    continue

                try:
                    # 发送数据
                    msg = packet.pack()
                    key.fileobj.send(msg)
                except ConnectionResetError:
                    self.remove(key.fileobj)
                    # 若发送失败，则将 packet 放回队列首位，重入循环
                    self.send_q.queue.insert(0, packet)
                    logging.error(f'<-x {packet.flag.name}: length={packet.length} chksum={packet.chksum}')
                else:
                    logging.debug(f'<- {packet.flag.name}: length={packet.length} chksum={packet.chksum}')

    def _recv(self):
        '''接收并解析数据包, 解析结果存入 recv_q 队列'''
        while self.is_working:
            for key, _ in self.receiver.select(timeout=1):
                sock, buf = key.fileobj, key.data
                try:
                    data = sock.recv(buf.remain, MSG_WAITALL)
                except ConnectionResetError:
                    self.remove(sock)  # 关闭连接
                    break

                if data:
                    buf.remain -= len(data)  # 更新剩余长度
                    buf.data.extend(data)  # 合并数据

                    if buf.remain == 0:
                        if buf.waiting == PacketSnippet.HEAD:
                            self.parse_head(buf)  # 解析 head 部分
                        else:
                            self.parse_body(buf)  # 解析 Body 部分
                else:
                    self.remove(sock)  # 关闭连接

    def start(self):
        s_thread = Thread(target=self.send, daemon=True)
        s_thread.start()
        self.threads.append(s_thread)

        r_thread = Thread(target=self.recv, daemon=True)
        r_thread.start()
        self.threads.append(r_thread)

    def stop(self):
        '''关闭所有连接'''
        logging.info('[ConnectionPool] closing all connections')
        self.is_working = False
        for t in self.threads:
            t.join()

        self.sender.close()
        self.receiver.close()

        for sock in self.socks:
            sock.close()


class NetworkMixin:
    sock: socket

    def send_msg(self, flag: Flag, *args):
        '''发送数据报文'''
        packet = Packet.load(flag, *args)
        datagram = packet.pack()
        logging.debug('<- %r' % datagram)
        self.sock.send(datagram)

    def recv_msg(self) -> Packet:
        '''接收数据报文'''
        # 接收并解析 head 部分
        head = self.sock.recv(LEN_HEAD, MSG_WAITALL)
        flag, chksum, len_body = Packet.unpack_head(head)
        logging.debug(f'-> {flag} len={len_body}')

        if not Flag.contains(flag):
            raise ValueError('unknow flag: %d' % flag)

        # 接收 body 部分
        body = self.sock.recv(len_body, MSG_WAITALL)

        # 错误重传
        if crc32(body) != chksum:
            self.send_msg(Flag.RESEND, head)
            return self.recv_msg()

        return Packet(flag, body)
