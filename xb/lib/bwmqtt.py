import socket
import ssl
import struct

if not hasattr(socket, 'IPPROTO_SEC'):
    socket.IPPROTO_SEC = socket.IPPROTO_TCP


    def sock_write(self, stuff, length=None):
        if isinstance(stuff, str):
            stuff = bytearray(stuff, 'utf-8')
        if length:
            return self.send(stuff, length)
        else:
            return self.send(stuff)


    socket.socket.write = sock_write
    socket.socket.read = socket.socket.recv


class MQTTException(Exception):
    pass


class MQTTNetworkError(MQTTException):
    pass

class MQTTBadUsernameOrPassword(MQTTException):
    pass

class MQTTDisconnected(MQTTException):
    pass


class MQTTClient:
    def __init__(self, client_id, server, port=0, user=None, password=None, keepalive=0):
        self.client_id = client_id
        self.sock = None
        self.server = server
        self.port = port
        self.pid = 0
        self.cb = None
        self.puback = None
        self.user = user
        self.pswd = password
        self.keepalive = keepalive
        self.lw_topic = None
        self.lw_msg = None
        self.lw_qos = 0
        self.lw_retain = False

    def _send_str(self, s):
        self.sock.write(struct.pack("!H", len(s)))
        self.sock.write(bytearray(s, 'latin1'))

    def _recv_len(self):
        n = 0
        sh = 0
        while 1:
            b = self.sock.read(1)[0]
            n |= (b & 0x7f) << sh
            if not b & 0x80:
                return n
            sh += 7

    def disconnect(self):
        if self.sock:
            self.sock.close()

    def connect(self, clean_session=True):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_SEC)
        self.sock = ssl.wrap_socket(self.sock)
        try:
            addr = socket.getaddrinfo(self.server, self.port)[0][-1]
        except OSError:
            raise MQTTNetworkError("DNS Failure. Check if network is connected")
        try:
            self.sock.connect(addr)  # also sometimes slow
        except OSError:
            raise MQTTNetworkError("DNS or connection error: [{}]. Check if network is connected".format(addr))
        premsg = bytearray(b"\x10\0\0\0\0\0")
        msg = bytearray(b"\x04MQTT\x04\x02\0\0")

        sz = 10 + 2 + len(self.client_id)
        msg[6] = clean_session << 1
        if self.user is not None:
            sz += 2 + len(self.user) + 2 + len(self.pswd)
            msg[6] |= 0xC0
        if self.keepalive:
            assert self.keepalive < 65536
            msg[7] |= self.keepalive >> 8
            msg[8] |= self.keepalive & 0x00FF
        if self.lw_topic:
            sz += 2 + len(self.lw_topic) + 2 + len(self.lw_msg)
            msg[6] |= 0x4 | (self.lw_qos & 0x1) << 3 | (self.lw_qos & 0x2) << 3
            msg[6] |= self.lw_retain << 5

        i = 1
        while sz > 0x7f:
            premsg[i] = (sz & 0x7f) | 0x80
            sz >>= 7
            i += 1
        premsg[i] = sz

        self.sock.write(premsg[:i + 2])
        self.sock.write(msg)
        self._send_str(self.client_id)
        if self.lw_topic:
            self._send_str(self.lw_topic)
            self._send_str(self.lw_msg)
        if self.user is not None:
            self._send_str(self.user)
            self._send_str(self.pswd)
        resp = self.sock.read(4)
        try:
            assert resp[0] == 0x20 and resp[1] == 0x02
            if resp[3] != 0:
                if resp[3] == 0x04 or resp[3] == 0x05:
                    raise MQTTBadUsernameOrPassword(resp[3])
                else:
                    raise MQTTException(resp[3])
            return resp[2] & 1
        except IndexError:
            raise MQTTNetworkError("Didn't receive enough bytes for valid CONNACK: [{}]".format(resp))

    def ping(self):
        pkt = bytearray(b"\xC0\0")
        self.sock.write(pkt)

    def pub(self, topic, msg, retain=False, qos=0, pid=None):
        pkt = bytearray(b"\x30\0\0\0")
        pkt[0] |= qos << 1 | retain
        sz = 2 + len(topic) + len(msg)
        if qos > 0:
            sz += 2
        assert sz < 2097152
        i = 1
        while sz > 0x7f:
            pkt[i] = (sz & 0x7f) | 0x80
            sz >>= 7
            i += 1
        pkt[i] = sz
        self.sock.write(pkt[:i + 1])
        self._send_str(topic)
        if qos > 0:
            self.sock.write(bytearray([(pid & 0xff00) >> 8, pid & 0x00ff]))
        self.sock.write(bytearray(msg, 'latin1'))

    def sub(self, topic, qos=0):
        assert self.cb is not None, "sub callback is not set"
        pkt = bytearray(b"\x82\0\0\0")
        self.pid += 1
        struct.pack_into("!BH", pkt, 1, 2 + 2 + len(topic) + 1, self.pid)
        self.sock.write(pkt)
        self._send_str(topic)
        self.sock.write(qos.to_bytes(1, "little"))
        while 1:
            op = self.wait_msg()
            if op == 0x90:
                resp = self.sock.read(4)
                assert resp[1] == pkt[2] and resp[2] == pkt[3]
                if resp[3] == 0x80:
                    raise MQTTException(resp[3])
                return

    def wait_msg(self):
        try:
            res = self.sock.read(1)
        except ssl.SSLWantReadError:
            return None
        self.sock.setblocking(True)
        if res is None:
            return None
        if res == b"":
            raise MQTTDisconnected("Socket disconnected")
        op = res[0]
        # puback or pingresp
        if op & 0xf0 in [0x40, 0xD0]:
            sz = self._recv_len()
            pid = None
            if sz:
                pid = self.sock.read(sz)
                pid = pid[0] << 8 | pid[1]
            return self.puback(pid)
        if op & 0xf0 != 0x30:
            return op
        sz = self._recv_len()
        topic_len = self.sock.read(2)
        topic_len = (topic_len[0] << 8) | topic_len[1]
        topic = self.sock.read(topic_len)
        sz -= topic_len + 2
        if op & 6:
            pid = self.sock.read(2)
            pid = pid[0] << 8 | pid[1]
            sz -= 2
        msg = self.sock.read(sz)
        self.cb(topic, msg)
        if op & 6 == 2:
            pkt = bytearray(b"\x40\x02\0\0")
            struct.pack_into("!H", pkt, 2, pid)
            self.sock.write(pkt)
        elif op & 6 == 4:
            assert 0

    def check_msg(self):
        self.sock.setblocking(False)
        return self.wait_msg()
