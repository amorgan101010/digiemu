"""emu/remote.py: the front panel served to a browser over WebSocket."""
import base64
import collections
import json
import os
import socket
import struct
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emu import remote                                             # noqa: E402

LAYOUT = {
    'product': 'Digitakt',
    'controls': [
        {'k': 'b', 'code': 19, 'label': 'FUNC', 'x': 30, 'y': 400, 'w': 100,
         'h': 40, 'sub': None, 'tint': None, 'led': 0},
        {'k': 'b', 'code': 0, 'label': '1', 'x': 150, 'y': 598, 'w': 98,
         'h': 72, 'sub': None, 'tint': None, 'led': None},
        {'k': 'e', 'code': 3, 'label': 'A', 'x': 750, 'y': 150, 'r': 34},
        {'k': 'd', 'led': 1, 'x': 1053, 'y': 388},
    ],
    'leds': [5, 43],
    'screen': {'x': 168, 'y': 102, 'w': 512, 'h': 256},
    'bounds': [14, 70, 1130, 690],
}


class FakeDevice:
    encoder_counts = 4


class FakeEmu:
    def __init__(self):
        self.inbox = collections.deque()
        self.fb = None
        self.version = 0
        self.leds = {}
        self.led_version = 0
        self.device = FakeDevice()


class Page:
    """A minimal WebSocket client, as a browser would speak it."""

    def __init__(self, port):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET /ws HTTP/1.1\r\nHost: x\r\n'
                           'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                           'Sec-WebSocket-Key: %s\r\n'
                           'Sec-WebSocket-Version: 13\r\n\r\n'
                           % key).encode())
        head = b''
        while b'\r\n\r\n' not in head:
            head += self.sock.recv(1)
        self.head = head.decode()
        self.expected = remote.accept_key(key)

    def send(self, text, opcode=0x1):
        data = text.encode()
        mask = os.urandom(4)
        body = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        self.sock.sendall(struct.pack('!BB', 0x80 | opcode, 0x80 | len(data))
                          + mask + body)

    def _exact(self, n):
        out = b''
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise EOFError
            out += chunk
        return out

    def recv(self):
        """-> (opcode, payload), skipping pings."""
        while True:
            b0, b1 = self._exact(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack('!H', self._exact(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self._exact(8))[0]
            payload = self._exact(n)
            if b0 & 0x0F != 0x9:
                return b0 & 0x0F, payload

    def recv_until(self, want, secs=3.0):
        """-> the first (opcode, payload) `want` accepts. A frame repeated
        after 'hello' can come before the one a test is waiting for."""
        end = time.monotonic() + secs
        while time.monotonic() < end:
            frame = self.recv()
            if want(*frame):
                return frame
        raise AssertionError('no matching frame')
    def close(self):
        self.sock.close()


def wait_for(cond, secs=3.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class Pure(unittest.TestCase):
    def test_accept_key_is_rfc6455s_example(self):
        self.assertEqual(remote.accept_key('dGhlIHNhbXBsZSBub25jZQ=='),
                         's3pPLMBiTxaQ9kYGzzhZRbK+xOo=')

    def test_split_turn_keeps_every_detent_under_the_limit(self):
        self.assertEqual(remote.split_turn(70, 31), [31, 31, 8])
        self.assertEqual(remote.split_turn(-5, 31), [-5])
        self.assertEqual(remote.split_turn(0, 31), [])

    def test_pack_screen_is_msb_first_row_major(self):
        fb = bytearray(remote.W * remote.H)
        fb[0] = 1                         # (0, 0)
        fb[9] = 1                         # (9, 0)
        fb[remote.W * 63 + 127] = 1       # bottom-right
        packed = remote.pack_screen(fb)
        self.assertEqual(len(packed), 1024)
        self.assertEqual(packed[0], 0x80)
        self.assertEqual(packed[1], 0x40)
        self.assertEqual(packed[-1], 0x01)
        self.assertEqual(remote.pack_screen(None), bytes(1024))

    def test_state_frame_carries_leds_in_layout_order(self):
        frame = remote.state_frame(None, {43: (255, 0, 10)}, [5, 43])
        self.assertEqual(frame[:1], b'S')
        self.assertEqual(frame[1025:], b'\0\0\0' + bytes([255, 0, 10]))

    def test_encode_frame_lengths(self):
        self.assertEqual(remote.encode_frame(0x1, b'hi'), b'\x81\x02hi')
        self.assertEqual(remote.encode_frame(0x2, bytes(300))[:4],
                         b'\x82\x7e\x01\x2c')


class Served(unittest.TestCase):
    def setUp(self):
        self.emu = FakeEmu()
        self.layout = LAYOUT
        self.server = remote.RemotePanel(self.emu, lambda: self.layout, 0)
        self.assertTrue(self.server.start())
        self.page = Page(self.server.port)

    def tearDown(self):
        self.page.close()
        self.server.stop()

    def hello(self):
        self.page.send('hello')
        op, text = self.page.recv()
        self.assertEqual(op, 0x1)
        self.assertTrue(text.startswith(b'L '))
        self.assertEqual(json.loads(text[2:])['leds'], [5, 43])
        op, state = self.page.recv()
        self.assertEqual(op, 0x2)
        return state

    def test_handshake(self):
        # HTTP/1.1, not the handler's default 1.0: Firefox refuses to
        self.assertTrue(self.page.head.startswith('HTTP/1.1 101 '))
        self.assertIn('Sec-WebSocket-Accept: %s' % self.page.expected,
                      self.page.head)

    def test_page_is_served(self):
        s = socket.create_connection(('127.0.0.1', self.server.port))
        s.sendall(b'GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
        body = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            body += chunk
        s.close()
        self.assertIn(b'200', body.split(b'\r\n', 1)[0])
        self.assertIn(b'Digitakt remote', body)

    def test_state_follows_the_screen_and_leds(self):
        state = self.hello()
        self.assertEqual(state, remote.state_frame(None, {}, [5, 43]))
        fb = bytearray(remote.W * remote.H)
        fb[0] = 1
        self.emu.leds = {5: (0, 255, 0)}
        self.emu.fb = fb
        op, state = self.page.recv_until(
            lambda op, p: op == 0x2 and p[1] == 0x80)
        self.assertEqual(state[1025:1028], bytes([0, 255, 0]))

    def test_keys_and_encoders_reach_the_inbox(self):
        self.hello()
        self.page.send('b 19 1')
        self.page.send('b 19 1')           # already held: not sent twice
        self.page.send('b 19 0')
        self.page.send('e 3 70')           # 70 detents at 4 counts: split
        self.page.send('b 99 1')           # not a key on this panel
        self.page.send('e 19 1')           # a key is not an encoder
        self.assertTrue(wait_for(lambda: len(self.emu.inbox) >= 5))
        time.sleep(0.1)
        self.assertEqual(list(self.emu.inbox), [
            ('press', 19, 0), ('release', 19, 0),
            ('encoder', 3, 31), ('encoder', 3, 31), ('encoder', 3, 8)])

    def test_disconnect_releases_what_the_page_held(self):
        self.hello()
        self.page.send('b 19 1')
        self.page.send('b 0 1')
        self.assertTrue(wait_for(lambda: len(self.emu.inbox) == 2))
        self.page.close()
        self.assertTrue(wait_for(lambda: len(self.emu.inbox) == 4))
        self.assertEqual(sorted(list(self.emu.inbox)[2:]),
                         [('release', 0, 0), ('release', 19, 0)])
        self.assertTrue(wait_for(lambda: not self.server.clients()))

    def test_release_all_message(self):
        self.hello()
        self.page.send('b 0 1')
        self.page.send('r')
        self.assertTrue(wait_for(lambda: len(self.emu.inbox) == 2))
        self.assertEqual(self.emu.inbox[1], ('release', 0, 0))

    def test_nothing_until_the_controls_are_named(self):
        self.layout = None
        self.page.send('hello')
        self.page.send('b 19 1')
        self.page.sock.settimeout(0.3)
        with self.assertRaises(socket.timeout):
            self.page.recv()
        self.assertEqual(list(self.emu.inbox), [])
        self.page.sock.settimeout(5)
        self.layout = LAYOUT
        op, text = self.page.recv()
        self.assertTrue(text.startswith(b'L '))

    def test_a_silent_page_is_dropped_and_let_go(self):
        self.hello()
        self.page.send('b 19 1')
        self.assertTrue(wait_for(lambda: len(self.emu.inbox) == 1))
        client = self.server.clients()[0]
        client.heard -= remote.DEAD_S + 1
        self.assertTrue(wait_for(lambda: len(self.emu.inbox) == 2))
        self.assertEqual(self.emu.inbox[1], ('release', 19, 0))


class SoundEmu(FakeEmu):
    def __init__(self):
        super().__init__()
        self.audio_cfg = {'rate': 48000}
        self.audio_live = True
        self.audio_taps = ()


class Sound(unittest.TestCase):
    def setUp(self):
        self.emu = SoundEmu()
        self.server = remote.RemotePanel(self.emu, lambda: LAYOUT, 0)
        self.assertTrue(self.server.start())
        self.page = Page(self.server.port)
        self.page.send('hello')

    def tearDown(self):
        self.page.close()
        self.server.stop()

    def feed(self, pcm):
        for tap in self.emu.audio_taps:
            tap(pcm)

    def test_the_rate_is_announced(self):
        self.page.recv_until(lambda op, p: op == 0x1 and p == b'A 48000')

    def test_no_rate_without_live_audio(self):
        self.assertEqual(remote.RemotePanel(FakeEmu(), lambda: None, 0)
                         .sound_rate(), 0)
        self.emu.audio_live = False
        self.assertEqual(self.server.sound_rate(), 0)

    def test_nothing_is_kept_while_no_page_listens(self):
        self.assertEqual(len(self.emu.audio_taps), 1)
        self.feed(bytes(400))
        self.assertEqual(len(self.server._pcm), 0)

    def test_a_page_that_asks_gets_the_sound(self):
        self.page.send('a 1')
        self.assertTrue(wait_for(lambda: self.server._listening))
        pcm = bytes(range(256)) * 4
        self.feed(pcm)
        op, frame = self.page.recv_until(
            lambda op, p: op == 0x2 and p[:1] == b'A')
        self.assertEqual(frame, b'A' + pcm)
        self.page.send('a 0')
        self.assertTrue(wait_for(lambda: not self.server._listening))

    def test_only_pages_that_ask(self):
        other = Page(self.server.port)
        try:
            other.send('a 1')
            self.assertTrue(wait_for(lambda: self.server._listening))
            self.feed(bytes(64))
            other.recv_until(lambda op, p: op == 0x2 and p[:1] == b'A')
            self.page.sock.settimeout(0.3)
            with self.assertRaises(socket.timeout):
                while True:
                    op, p = self.page.recv()
                    self.assertFalse(op == 0x2 and p[:1] == b'A')
        finally:
            other.close()

    def test_the_backlog_is_bounded(self):
        self.page.send('a 1')
        self.assertTrue(wait_for(lambda: self.server._listening))
        self.feed(bytes(4 * 48000 * 4))      # 4 s, in one block
        op, frame = self.page.recv_until(
            lambda op, p: op == 0x2 and p[:1] == b'A')
        self.assertEqual(len(frame), 1 + int(remote.SOUND_KEEP_S * 48000) * 4)

    def test_disconnect_stops_listening_and_stop_unhooks(self):
        self.page.send('a 1')
        self.assertTrue(wait_for(lambda: self.server._listening))
        self.page.close()
        self.assertTrue(wait_for(lambda: not self.server._listening))
        self.server.stop()
        self.assertEqual(self.emu.audio_taps, ())


if __name__ == '__main__':
    unittest.main()
