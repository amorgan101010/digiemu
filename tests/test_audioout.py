"""emu/audioout.py: the PCM helpers, the Master Volume gain, and the host
output devices.

Nothing here opens a real sound device unless asked: the two device tests
play a quiet tone through the default output, and fail on a machine with
none (a remote desktop, a runner), so they run only with
DIGIEMU_AUDIO_DEVICE_TESTS=1. Everything else stands in for the device.
"""
import os
import struct
import sys
import tempfile
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
