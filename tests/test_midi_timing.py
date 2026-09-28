"""MIDI in keeps its timing: bytes land on emulated time, spaced as they came.

The firmware follows external clock by timing the bytes; handing them over
at whatever chunk boundary came next made its tempo jump by tens of BPM.
"""
import collections
import unittest

from emu import midi

IPS = 1_000_000                     # one instruction a microsecond


def _scheduled(stamped, done=0):
    mi = midi.MidiIn()
    mi.m, mi.ips, mi._offset = object(), IPS, 0.0
    mi._stamped = collections.deque(stamped)
    mi._schedule(done)
    return mi, sorted(t for t, _s, _b in mi._queue)


class MidiTimingTest(unittest.TestCase):
    def test_gaps_between_arrivals_survive(self):
        _mi, t = _scheduled([(1.000, b'\x90'), (1.020, b'\x3c'), (1.047, b'\x64')])
        lat = midi.MidiIn.MARGIN_S * IPS
        self.assertEqual([round(x - lat) for x in t], [1_000_000, 1_020_000, 1_047_000])

    def test_a_burst_is_spread_at_the_din_rate(self):
        _mi, t = _scheduled([(1.0, b'\x90\x3c\x64')])
        gap = midi.MidiIn.BYTE_S * IPS
        self.assertAlmostEqual(t[1] - t[0], gap)
        self.assertAlmostEqual(t[2] - t[1], gap)

    def test_a_slot_already_past_is_delivered_now_and_counted(self):
        mi, t = _scheduled([(0.0, b'\xf8')], done=5_000_000)
        self.assertEqual(t, [5_000_000])
        self.assertEqual(mi.late, 1)

    def test_step_waits_for_the_next_byte(self):
        mi, _t = _scheduled([(1.0, b'\xf8')])
        mi._track = lambda done: None               # keep the offset fixed
        due = int((1.0 + midi.MidiIn.MARGIN_S) * IPS)
        self.assertEqual(mi.step(due - 500), 500)

    def test_the_envelope_follows_the_furthest_ahead_then_sinks(self):
        mi = midi.MidiIn()
        mi.m, mi.ips = object(), IPS
        clock = iter([10.0, 10.1, 11.1])
        midi.time.monotonic, real = (lambda: next(clock)), midi.time.monotonic
        try:
            mi._track(10_000_000 + 50_000)          # 50 ms ahead
            mi._track(10_100_000)                   # back level: envelope holds
            self.assertAlmostEqual(mi._offset, 50_000 - 0.1 * midi.MidiIn.DECAY * IPS)
            mi._track(11_100_000)                   # a second later: sunk to it
            self.assertAlmostEqual(mi._offset, 0.0)
        finally:
            midi.time.monotonic = real


    def test_a_jittery_clock_comes_out_steady(self):
        import random
        rng = random.Random(1)
        period = 0.0197
        arrivals = [(i * period + rng.uniform(-0.006, 0.006), b'\xf8') for i in range(400)]
        _mi, t = _scheduled(arrivals)
        beats_in = [60 / (arrivals[i + 24][0] - arrivals[i][0]) for i in range(200, 376, 24)]
        beats_out = [60 * IPS / (t[i + 24] - t[i]) for i in range(200, 376, 24)]
        spread = lambda xs: max(xs) - min(xs)
        self.assertLess(spread(beats_out), spread(beats_in) / 3)
        self.assertAlmostEqual(sum(beats_out) / len(beats_out), 60 / (24 * period), delta=0.5)

    def test_a_stop_and_restart_starts_the_tracker_over(self):
        ticks = [(i * 0.02, b'\xf8') for i in range(30)] + \
                [(5 + i * 0.01, b'\xf8') for i in range(30)]
        _mi, t = _scheduled(ticks)
        self.assertAlmostEqual(t[31] - t[30], 10_000, delta=50)   # new tempo at once
        self.assertAlmostEqual(t[40] - t[39], 10_000, delta=50)

    def test_a_tempo_ramp_is_followed_without_a_step(self):
        import random
        rng = random.Random(2)
        # 127 -> 100 BPM over 4 s, with MD-sized jitter (+-6 ms per tick)
        t, arrivals, bpm = 0.0, [], 127.0
        while t < 8.0:
            bpm = 127.0 - 27.0 * min(max(t - 2.0, 0.0), 4.0) / 4.0
            arrivals.append((t, bpm))
            t += 60 / bpm / 24
        stamped = [(a + rng.uniform(-0.006, 0.006), b'\xf8') for a, _b in arrivals]
        _mi, out = _scheduled(stamped)
        worst = 0.0
        for i in range(48, len(out) - 24, 24):
            got = 60 * IPS / (out[i + 24] - out[i])
            true = 60 / (arrivals[i + 24][0] - arrivals[i][0])
            worst = max(worst, abs(got - true))
        self.assertLess(worst, 1.5)

    def test_clock_queued_before_attach_is_dropped(self):
        # A resumed session while MD sends clock: 35 bytes waited for the
        # emulator, went in with one interrupt, and the firmware divided by
        # a zero 24-clock span (vector 257 at the first instruction).
        mi = midi.MidiIn()
        mi.put(b'\xf8' * 30 + b'\x90\x3c\x64' + b'\xf8' * 2)
        mi.attach(object(), IPS)
        mi._offset = 0.0
        mi._schedule(0)
        self.assertEqual(sorted(b for _t, _s, b in mi._queue), [0x3C, 0x64, 0x90])

    def test_clock_that_is_late_all_at_once_is_spread(self):
        mi, t = _scheduled([(0.001 * i, b'\xf8') for i in range(30)], done=5_000_000)
        gaps = [b - a for a, b in zip(t, t[1:])]
        self.assertGreaterEqual(min(gaps), midi.MidiIn.CLOCK_GAP_S * IPS)
        self.assertGreater(midi.MidiIn.CLOCK_GAP_S, midi.MidiIn.WINDOW_S)

    def test_start_is_never_overtaken_by_a_later_clock(self):
        ticks = [(i * 0.02, b'\xf8') for i in range(20)]
        # Start arrives just before tick 20, which comes late: a smoothed
        # slot for that tick must still land after the Start.
        stamped = ticks + [(0.399, b'\xfa'), (0.410, b'\xf8')]
        mi, _t = _scheduled(stamped)
        order = [b for _t, _s, b in sorted(mi._queue)]
        self.assertLess(order.index(0xFA), len(order) - 1)
        self.assertEqual(order[-1], 0xF8)


if __name__ == '__main__':
    unittest.main()
