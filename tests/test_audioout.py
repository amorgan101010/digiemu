"""emu/audioout.py: the PCM helpers, the Master Volume gain, and the host
output devices.

Nothing here opens a real sound device unless asked: the two device tests
play a quiet tone through the default output, and fail on a machine with
none (a remote desktop, a runner), so they run only with
DIGIEMU_AUDIO_DEVICE_TESTS=1. Everything else stands in for the device.
"""
import contextlib
import io
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest import mock

from emu import audioout

DEVICE_TESTS = unittest.skipUnless(
    os.environ.get('DIGIEMU_AUDIO_DEVICE_TESTS') == '1'
    and sys.platform in ('win32', 'darwin', 'linux'),
    'plays through the default output: set DIGIEMU_AUDIO_DEVICE_TESTS=1 '
    '(Windows, macOS or Linux)')


def _pcm(*samples):
    return struct.pack('<%dh' % len(samples), *samples)


class AudioOutTest(unittest.TestCase):
    def test_frames_from_ssi(self):
        # 32-bit word, 24-bit sample: bytes 1 and 2 of each 4-byte word are 16-bit LE
        raw = bytes([0x00, 0x12, 0x34, 0x00, 0x00, 0x56, 0x78, 0x00])
        pcm = audioout.frames_from_ssi(raw, word_bits=32, sample_bits=24)
        self.assertEqual(len(pcm), 4)
        # first word: data[2]=0x34, data[1]=0x12 -> 0x34, 0x12
        # second word: data[6]=0x78, data[5]=0x56 -> 0x78, 0x56
        self.assertEqual(pcm, bytes([0x34, 0x12, 0x78, 0x56]))

    def test_trim_silence(self):
        silence = bytes(400)
        sound = struct.pack('<hh', 500, 500) * 100
        pcm = silence + sound + silence
        trimmed = audioout.trim_silence(pcm, channels=2, threshold=10, rate=48000, pad_ms=2)
        self.assertTrue(len(trimmed) > len(sound))
        self.assertTrue(len(trimmed) < len(pcm))

    def test_wav_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'test.wav')
            w = audioout.WavFile(path, rate=48000, channels=2)
            data = struct.pack('<hh', 100, 200) * 100
            w.write(data)
            self.assertEqual(w.queued(), 0)
            self.assertEqual(w.played, 1)
            w.close()

            with wave.open(path, 'rb') as r:
                self.assertEqual(r.getnchannels(), 2)
                self.assertEqual(r.getsampwidth(), 2)
                self.assertEqual(r.getframerate(), 48000)
                self.assertEqual(r.getnframes(), 100)

    def test_wav_file_applies_the_gain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'half.wav')
            w = audioout.WavFile(path)
            w.gain = 0.5
            w.write(_pcm(1000, -1000))
            w.close()
            with wave.open(path, 'rb') as r:
                self.assertEqual(r.readframes(1), _pcm(500, -500))


class GainTest(unittest.TestCase):
    """apply_gain: the Master Volume knob's software gain."""

    def test_unity_passes_the_same_bytes_through(self):
        pcm = _pcm(1, -2, 32767, -32768)
        self.assertIs(audioout.apply_gain(pcm, 1.0), pcm)
        self.assertEqual(audioout.apply_gain(b'', 0.5), b'')

    def test_scaling_and_silence(self):
        self.assertEqual(audioout.apply_gain(_pcm(1000, -1000, 3), 0.5),
                         _pcm(500, -500, 1))
        self.assertEqual(audioout.apply_gain(_pcm(1000, -1000), 0.0),
                         _pcm(0, 0))

    def test_boost_clips_to_16_bits(self):
        self.assertEqual(audioout.apply_gain(_pcm(30000, -30000, 100), 1.5),
                         _pcm(32767, -32768, 150))


class DeviceChoiceTest(unittest.TestCase):
    """WaveOut picks the host's output without opening anything here."""

    def test_each_platform_gets_its_own_output(self):
        made = []

        def fake(name):
            return lambda *a: made.append((name, a)) or name
        with mock.patch.object(audioout, '_WinMMOut', fake('winmm')), \
                mock.patch.object(audioout, '_AudioQueueOut', fake('audioqueue')), \
                mock.patch.object(audioout, '_PulseOut', fake('pulse')):
            for platform, want in (('win32', 'winmm'), ('darwin', 'audioqueue'),
                                   ('linux', 'pulse')):
                with mock.patch.object(audioout.sys, 'platform', platform):
                    self.assertEqual(audioout.WaveOut(44100, 2), want)
        self.assertEqual(made, [('winmm', (44100, 2, 16, 20)),
                                ('audioqueue', (44100, 2, 16, 20)),
                                ('pulse', (44100, 2, 16, 20))])

    def test_other_platforms_have_no_device(self):
        with mock.patch.object(audioout.sys, 'platform', 'sunos5'):
            with self.assertRaises(OSError):
                audioout.WaveOut()


class _RecordingOut:
    """Stands in for a device: records each write and the gain it had."""
    made = []

    def __init__(self, rate, channels):
        self.gain = 1.0
        self.writes = []
        _RecordingOut.made.append(self)

    def write(self, pcm, block=False, abort=None):
        self.writes.append((len(pcm), self.gain))

    def drain(self, abort=None):
        pass

    def close(self):
        pass


class PlayerGainTest(unittest.TestCase):
    def test_replay_follows_master_volume(self):
        _RecordingOut.made = []
        with mock.patch.object(audioout, 'WaveOut', _RecordingOut):
            player = audioout.Player(rate=48000, channels=2)
            player.gain = 0.25
            player.play(bytes(48000 * 4 // 4))           # a quarter second
            player._thread.join(5)
        out, = _RecordingOut.made
        self.assertEqual(sum(n for n, _g in out.writes), 48000)
        self.assertEqual({g for _n, g in out.writes}, {0.25})
        self.assertGreater(len(out.writes), 1)            # in tenths of a second


class _FakePulse:
    """Stands in for libpulse-simple and for libpulse (ctypes.CDLL returns
    it for both): a server that takes every block at once, unless told to
    fail, to refuse a connection, or to hold a write back."""

    def __init__(self):
        self.written = []           # the blocks pa_simple_write was given
        self.fail = False           # writes fail, as with the server gone
        self.refuse = False         # pa_simple_new fails
        self.latency = 0            # pa_simple_get_latency's answer, us
        self.gate = None            # an Event a write waits for
        self.entered = threading.Event()        # a write has begun
        self.opened = self.freed = 0
        self.tlength = None
        for name in ('pa_simple_new', 'pa_simple_write', 'pa_simple_free',
                     'pa_simple_get_latency', 'pa_simple_flush',
                     'pa_simple_drain', 'pa_strerror'):
            # A plain function, which takes .argtypes and .restype.
            setattr(self, name,
                    (lambda f: lambda *a: f(*a))(getattr(self, '_' + name)))

    def _pa_simple_new(self, *a):
        if self.refuse:
            a[8]._obj.value = 6
            return None
        self.tlength = a[7]._obj.tlength
        self.opened += 1
        return self.opened

    def _pa_simple_write(self, pa, chunk, n, err):
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail:
            err._obj.value = 6
            return -1
        self.written.append(bytes(chunk[:n]))
        return 0

    def _pa_simple_get_latency(self, pa, err):
        return self.latency

    def _pa_simple_free(self, pa):
        self.freed += 1

    def _pa_simple_flush(self, pa, err):
        return 0

    _pa_simple_drain = _pa_simple_flush

    def _pa_strerror(self, code):
        return b'Connection refused'


class PulseOutTest(unittest.TestCase):
    """_PulseOut against a stand-in library: no server, no sound."""

    def setUp(self):
        self.lib = _FakePulse()
        self.said = io.StringIO()
        for p in (mock.patch.object(audioout.ctypes, 'CDLL',
                                    lambda path: self.lib),
                  mock.patch.object(audioout.ctypes.util, 'find_library',
                                    lambda name: name),
                  mock.patch.object(audioout._PulseOut, 'RETRY_S', 0.01),
                  mock.patch.object(audioout._PulseOut, 'CLOSE_WAIT_S', 0.2),
                  mock.patch.dict(os.environ),
                  contextlib.redirect_stdout(self.said)):
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)
        os.environ.pop('DIGIEMU_PULSE_MS', None)

    def open(self, buffers=8, block_ms=10):
        out = audioout._PulseOut(48000, 2, buffers, block_ms)
        self.addCleanup(out.close)
        return out

    def until(self, cond, seconds=5.0):
        end = time.monotonic() + seconds
        while not cond():
            self.assertLess(time.monotonic(), end, 'still waiting')
            time.sleep(0.002)

    def blocks(self, out, n):
        return [bytes([i + 1]) * out.block for i in range(n)]

    def test_blocks_reach_the_server_in_order(self):
        out = self.open()
        blocks = self.blocks(out, 3)
        out.write(b''.join(blocks) + b'\x7f' * 8)
        self.until(lambda: len(self.lib.written) == 3)
        self.assertEqual(self.lib.written, blocks)
        self.assertEqual((out.played, out.dropped), (3, 0))
        out.drain()                                 # the part block, padded
        self.assertEqual(self.lib.written[3],
                         (b'\x7f' * 8).ljust(out.block, b'\0'))
        self.assertEqual(out.queued(), 0)

    def test_the_server_buffer_is_two_blocks_and_at_least_1024_frames(self):
        self.open(block_ms=10)
        self.assertEqual(self.lib.tlength, 1024 * 4)        # 21 ms
        self.open(block_ms=20)
        self.assertEqual(self.lib.tlength, 2 * 960 * 4)     # 40 ms
        os.environ['DIGIEMU_PULSE_MS'] = '43'
        self.open(block_ms=10)
        self.assertEqual(self.lib.tlength, 48000 * 43 // 1000 * 4)
        os.environ['DIGIEMU_PULSE_MS'] = 'a lot'
        self.open(block_ms=10)
        self.assertEqual(self.lib.tlength, 1024 * 4)

    def test_no_server_is_a_missing_device(self):
        self.lib.refuse = True
        with self.assertRaisesRegex(OSError, 'Connection refused'):
            audioout._PulseOut()

    def test_a_block_that_finds_the_queue_full_is_dropped(self):
        self.lib.gate = threading.Event()
        self.addCleanup(self.lib.gate.set)
        out = self.open(buffers=2)
        first, *rest = self.blocks(out, 5)
        out.write(first)
        self.until(self.lib.entered.is_set)         # the writer holds it
        out.write(b''.join(rest))
        self.assertEqual((out.played, out.dropped), (3, 2))
        self.assertEqual(out.queued(), 3)
        self.lib.gate.set()
        self.until(lambda: len(self.lib.written) == 3)
        self.assertEqual(self.lib.written, [first] + rest[:2])

    def test_the_devices_own_latency_is_not_counted_as_queued(self):
        self.lib.latency = 500_000                  # a slow sink
        out = self.open()
        out.write(self.blocks(out, 1)[0])
        self.until(lambda: len(self.lib.written) == 1)
        self.assertLessEqual(out.queued(), 2)       # tlength, in blocks
        out.drain()
        self.assertEqual(out.queued(), 0)

    def test_a_latency_the_server_cannot_give_is_nothing_queued(self):
        self.lib.latency = audioout.PA_USEC_INVALID
        out = self.open()
        out.write(self.blocks(out, 1)[0])
        self.until(lambda: len(self.lib.written) == 1)
        self.until(lambda: out.queued() == 0, 1.0)
        out.drain()

    def test_a_failed_write_drops_until_there_is_a_server_again(self):
        out = self.open()
        self.lib.fail = self.lib.refuse = True      # the server went away
        out.write(b''.join(self.blocks(out, 2)))
        self.until(lambda: out.lost == 1)
        self.assertEqual((out.played, out.dropped), (0, 2))
        self.assertEqual(out.queued(), 0)
        out.write(b''.join(self.blocks(out, 3)), block=True)    # no waiting
        out.drain()
        self.assertEqual((out.played, out.dropped), (0, 5))
        self.assertEqual(self.lib.freed, 1)
        self.assertIn('write failed (Connection refused)',
                      self.said.getvalue())

        self.lib.fail = self.lib.refuse = False     # and came back
        self.until(lambda: 'connected again' in self.said.getvalue())
        block = self.blocks(out, 1)[0]
        out.write(block)
        self.until(lambda: self.lib.written == [block])
        self.assertEqual((out.played, out.lost), (1, 1))
        out.close()
        self.assertEqual(self.lib.freed, 2)
        self.assertIn('server lost 1 times', self.said.getvalue())

    def test_close_does_not_wait_for_a_write_that_never_returns(self):
        self.lib.gate = threading.Event()
        self.addCleanup(self.lib.gate.set)
        out = self.open()
        out.write(self.blocks(out, 1)[0])
        self.until(self.lib.entered.is_set)
        began = time.monotonic()
        out.close()
        self.assertLess(time.monotonic() - began, 5)
        self.assertEqual(self.lib.freed, 0)         # not under the write
        self.assertIn('not answering', self.said.getvalue())
        self.lib.gate.set()                         # it returns after all
        self.until(lambda: self.lib.freed == 1)

    def test_a_clean_session_closes_once_and_says_nothing(self):
        self.lib.latency = 21_000
        out = self.open()
        for block in self.blocks(out, 20):
            out.write(block, block=True)
        self.until(lambda: len(self.lib.written) == 20)
        out.close()
        out.close()
        self.assertEqual(self.lib.freed, 1)
        self.assertEqual(self.said.getvalue(), '')


class DeviceTest(unittest.TestCase):
    @DEVICE_TESTS
    def test_waveout_lifecycle(self):
        out = audioout.WaveOut(rate=48000, channels=2, buffers=8, block_ms=10)
        self.assertEqual(out.rate, 48000)
        self.assertEqual(out.channels, 2)
        self.assertEqual(out.queued(), 0)

        # Write two blocks
        pcm = struct.pack('<hh', 100, 100) * (out.block // 4) * 2
        out.write(pcm, block=True)
        self.assertTrue(out.played > 0)
        out.drain()
        self.assertEqual(out.queued(), 0)
        out.close()

    @DEVICE_TESTS
    def test_player_lifecycle(self):
        player = audioout.Player(rate=48000, channels=2)
        self.assertIsNone(player.error)
        self.assertFalse(player.playing)

        pcm = struct.pack('<hh', 100, 100) * 960  # 20ms
        player.play(pcm)
        time.sleep(0.01)
        player.stop()
        self.assertFalse(player.playing)
        self.assertIsNone(player.error)


if __name__ == '__main__':
    unittest.main()
