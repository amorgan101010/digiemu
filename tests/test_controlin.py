"""emu/controlin.py: encoder turns from a local Unix datagram socket."""
import os
import socket
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emu import controlin                                           # noqa: E402

CODES = {'A': 10, 'H': 17, 'LEVEL/DATA': 18}


def fake_emu(counts=1):
    return SimpleNamespace(inbox=[], device=SimpleNamespace(encoder_counts=counts))


class ControlInputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patch = mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def started(self, emu, product='Digitakt'):
        control = controlin.ControlInput(emu, lambda: CODES, product)
        self.assertTrue(control.start(), control.error)
        self.addCleanup(control.stop)
        return control

    def send(self, control, text):
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(text.encode(), control.path)

    def wait_for(self, emu, count):
        deadline = time.monotonic() + 2
        while len(emu.inbox) < count and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_turns_reach_the_inbox(self):
        emu = fake_emu()
        control = self.started(emu)
        self.assertTrue(control.path.endswith('elektremu/digitakt.sock'))
        self.send(control, 'turn A 3')
        self.send(control, 'turn LEVEL/DATA -2')
        self.wait_for(emu, 2)
        self.assertEqual(emu.inbox, [('encoder', 10, 3), ('encoder', 18, -2)])

    def test_bad_lines_are_ignored(self):
        emu = fake_emu()
        control = self.started(emu)
        for text in ('turn Z 1', 'turn A x', 'turn A 0', 'push A', 'turn A'):
            self.send(control, text)
        self.send(control, 'turn H 1')
        self.wait_for(emu, 1)
        time.sleep(0.05)
        self.assertEqual(emu.inbox, [('encoder', 17, 1)])

    def test_large_turns_are_split_like_the_remote(self):
        emu = fake_emu(counts=4)
        control = controlin.ControlInput(emu, lambda: CODES, 'Digitone')
        control.handle('turn A 70')
        self.assertEqual(sum(n for _, _, n in emu.inbox), 70)
        self.assertTrue(all(n <= 127 // 4 for _, _, n in emu.inbox))

    def test_leds_are_sent_back_to_the_asker(self):
        lights = {'1': (0, 200, 0), '2': None}
        control = controlin.ControlInput(fake_emu(), lambda: CODES, 'Digitakt',
                                         leds=lambda: lights)
        self.assertTrue(control.start(), control.error)
        self.addCleanup(control.stop)
        asker = os.path.join(self.tmp.name, 'asker.sock')
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.bind(asker)
            s.settimeout(2)
            s.sendto(b'leds', control.path)
            reply = s.recv(4096).decode()
        self.assertEqual(reply, 'leds {"1":[0,200,0],"2":[0,0,0]}')

    def test_a_second_window_goes_without(self):
        first = self.started(fake_emu())
        second = controlin.ControlInput(fake_emu(), lambda: CODES, 'Digitakt')
        self.assertFalse(second.start())
        self.assertTrue(os.path.exists(first.path))

    def test_stop_removes_the_socket_and_a_stale_one_is_replaced(self):
        control = self.started(fake_emu())
        control.stop()
        self.assertFalse(os.path.exists(control.path))
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        stale.bind(control.path)
        stale.close()                     # the file stays, nobody reads it
        again = self.started(fake_emu())
        self.assertTrue(os.path.exists(again.path))


if __name__ == '__main__':
    unittest.main()
