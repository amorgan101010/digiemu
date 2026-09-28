"""MIDI in through the mk1's DIN port, and a host port to feed it.

The MIDI IN jack of a Digitakt or Digitone mk1 is UART9 (0xEC074000; both
builds program its baud divisor to 132, 31250 baud at the 132 MHz bus
clock). The firmware never reads UART9's receive register itself: eDMA
channel 36 moves each byte into a 32 KB ring (TCD36's DMOD is 15), and
channel 36's completion interrupt, vector 156, drains the ring from its own
read index up to TCD36's DADDR and hands each byte to the MIDI parser. It is
the panel link's mechanism on UART8 / channel 34 / vector 154 again
(emu/panelin.py), so it is fed the same way: write the bytes where DADDR
points, advance DADDR modulo the ring, raise vector 156.

The firmware then filters on its own settings: MIDI CONFIG > PORT CONFIG >
INPUT FROM must include MIDI (the first boot here leaves it at USB), and
notes need the track or auto channel.

MIDI OUT is UART9's transmitter, and the driver (0x40002a3a on Digitone
1.43) has two ways to use it. A buffer goes into a 4 KB ring and eDMA
channel 37, started through SSRT, moves it to UART9's data register; its
completion interrupt, vector 157, sends what was queued meanwhile or marks
the driver idle. A single byte is written to the data register straight
away when the transmitter is idle, or else queued, with UART9's own
interrupt (vector 181) unmasked so that its handler sends the queue one
byte per interrupt and masks itself again. MidiOut catches the bytes both
ways, and raises the two interrupts the hardware would. Everything goes out
at once rather than at 31250 baud.

The firmware filters what it sends on MIDI CONFIG settings too (OUTPUT TO,
CLOCK SEND and so on).

The host side is python-rtmidi, an optional dependency (`uv sync --extra
midi`): a virtual input port where the platform has them (Linux, macOS), or
an existing port named by DIGIEMU_MIDI_IN (a substring of its name), and a
virtual output port of the same name (DIGIEMU_MIDI_OUT likewise).
"""
import collections
import heapq
import os
import re
import struct
import threading
import time

from unicorn import UC_HOOK_MEM_WRITE
from unicorn.m68k_const import UC_M68K_REG_SR

from emu.edma_sw import iteration_count, modulo_add
from emu.pit import PENDING_STEP, interrupt_level, render_holds

TCD36 = 0xFC045000 + 36 * 0x20
TCD37 = 0xFC045000 + 37 * 0x20
TCD_SADDR, TCD_ATTR, TCD_SOFF, TCD_NBYTES = 0x00, 0x04, 0x06, 0x08
TCD_DADDR, TCD_CITER = 0x10, 0x14
UART9 = 0xEC074000
UTB9 = UART9 + 0x0C             # transmit buffer (write)
UIMR9 = UART9 + 0x14            # interrupt mask (write); bit 0 TxRDY
INTC1_SIMR, INTC1_CIMR = 0xFC04C01C, 0xFC04C01D
TX_SOURCE = 53                  # UART9 on INTC1
RX_VECTOR = 156
TX_VECTOR = 181                 # UART9 (INTC1 source 53)
TX_DMA_VECTOR = 157             # eDMA channel 37 done (INTC1 source 29)
TX_CHANNEL = 37
# Bytes per delivery: DIN MIDI's pace. At 31250 baud a byte takes 320 us, so
# the ~5 ms chunk the GUI runs between deliveries carries about 16 of them.
# The firmware times its clock input by DTIM0's counter as each byte's
# interrupt runs (emu/dtim.py, Dtims._serve_count): it sums the gaps across
# 24 clocks and divides by that sum. That counter moves at step boundaries,
# so a backlog written all at once -- clock that queued up while the emulator
# was loading, or while the host stalled -- would give 24 clocks one
# timestamp, a sum of 0, a divide-by-zero and a halt on the firmware's
# EXCEPTION screen. Paced like the wire, a beat's clocks span deliveries.
MAX_FEED = 16


def _take(m, vector):
    """Raise `vector` if its source is armed, unmasked and above the CPU's
    level. -> True if taken."""
    level = interrupt_level(m, vector)
    sr = m.uc.reg_read(UC_M68K_REG_SR)
    if level is None or ((sr >> 8) & 7) >= level:
        return False
    return m.raise_vector(vector, level=level)


def feed(m, data):
    """Write `data` where channel 36's DADDR points and advance it, modulo
    the ring the descriptor's DMOD sets, as the channel itself would."""
    attr = struct.unpack('>H', bytes(m.uc.mem_read(TCD36 + TCD_ATTR, 2)))[0]
    size = 1 << ((attr >> 3) & 0x1F)
    daddr = struct.unpack('>I', bytes(m.uc.mem_read(TCD36 + TCD_DADDR, 4)))[0]
    base = daddr & ~(size - 1)
    for byte in data:
        m.uc.mem_write(daddr, bytes([byte]))
        daddr = base + ((daddr - base + 1) & (size - 1))
    m.uc.mem_write(TCD36 + TCD_DADDR, struct.pack('>I', daddr))


class MidiIn:
    """Bytes from any thread, delivered to the guest.

    Two ways in. `deliver` writes whatever is pending at once, at a chunk
    boundary (tests and tools). `attach` makes this an event source for
    longrun.spin instead, which is what the GUI uses: each byte is stamped
    with the host time it arrived and delivered at the matching point of
    *emulated* time, so the gaps between bytes survive. The firmware times
    MIDI clock by those gaps (emu/dtim.py, Dtims._serve_count), and the
    emulator runs in bursts -- ahead to fill its audio cushion, then waiting,
    and behind when the host is busy -- so a byte handed over "at the next
    chunk" lands tens to hundreds of ms early or late in guest time, which
    the firmware reads as tempo jumping by tens of BPM. Gearmulator gets the
    same result by giving each event its sample position in the audio block.

    Host time maps to emulated time through the offset's upper envelope:
    it follows the furthest the emulator runs ahead of the host (it bursts
    ahead to fill its audio cushion) and sinks by `DECAY` per second, so a
    byte's slot, `MARGIN_S` past that, is still in the future when the byte
    is scheduled. A fixed average plus 30 ms was tried first and left 12% of
    a Digitone session's bytes late -- delivered on arrival, timing lost. A
    byte is never placed closer than one DIN byte time (`BYTE_S`) after the
    one before, which also spreads a backlog; bytes due within `WINDOW_S` of
    each other go in together, so a SysEx does not cut the run into 320 us
    steps (clock bytes are 20 ms and more apart).
    """

    MARGIN_S = 0.015                # covers the clock smoother moving a tick earlier
    DECAY = 0.05                    # envelope sinks 50 ms per second
    BYTE_S = 10 / 31250             # one DIN byte: start + 8 data + stop
    WINDOW_S = 0.001
    # Clock bytes never closer than this: each goes in on its own delivery, so
    # a backlog that is late all at once (the host stalled) still gives the
    # firmware's 24-clock sum a non-zero span. 2 ms is 1250 BPM.
    CLOCK_GAP_S = 2 * WINDOW_S
    # Clock smoothing (see _clock_slot): an alpha-beta tracker on 0xF8.
    PLL_PHASE = 0.1                 # share of a tick's error taken at once
    PLL_FREQ = PLL_PHASE ** 2 / (2 - PLL_PHASE)   # matched: follows a ramp
    LOST_TICKS = 4
    MAX_TICK_S = 0.25               # a longer gap is a pause (10 BPM and up)

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._stamped = collections.deque()   # (host time, bytes)
        self._queue = []                      # heap: (emulated target, seq, byte)
        self._seq = 0
        self._clk_raw = None                  # last 0xF8's unsmoothed slot
        self._clk_pred = None                 # where the next one is expected
        self._clk_period = None
        self.m = None
        self.ips = None
        self._offset = None                   # emulated minus host*ips
        self._offset_t = None
        self._last_target = None
        self._last_clock = None               # slot of the last 0xF8 queued
        self.received = 0           # bytes the host handed us
        self.delivered = 0          # bytes written into the ring
        self.raised = 0             # vector 156 taken
        self.deferred = 0           # boundaries where it could not be taken
        self.late = 0               # bytes whose slot had already passed

    def put(self, data):
        now = time.monotonic()
        with self._lock:
            if self.m is None:
                self._pending += bytes(data)
            else:
                self._stamped.append((now, bytes(data)))
            self.received += len(data)

    def attach(self, m, ips):
        """Deliver on emulated time from now on (see the class docstring)."""
        with self._lock:
            self.m, self.ips = m, ips
            now = time.monotonic()
            # Arrived before the clock was known, so it has no timing left.
            # Clock that queued up meanwhile (a session resumed while a sender
            # runs) is dropped: stamped as one moment it went in with one
            # interrupt, and the firmware's tempo divide by its 24-clock span
            # hit zero and halted it at the first instruction. The live clock
            # after it gives the tempo.
            backlog = bytes(b for b in self._pending if b != 0xF8)
            if backlog:
                self._stamped.append((now, backlog))
            self._pending.clear()

    def set_ips(self, ips):
        self.ips = ips
        self._offset = None          # the mapping restarts at the new rate

    def _track(self, done):
        """Update the offset's upper envelope. -> host time now."""
        now = time.monotonic()
        sample = done - now * self.ips
        if self._offset is None:
            self._offset = sample
        else:
            sunk = self._offset - self.DECAY * (now - self._offset_t) * self.ips
            self._offset = max(sample, sunk)
        self._offset_t = now
        return now

    def _clock_slot(self, raw):
        """-> where to put a MIDI clock byte whose arrival maps to `raw`.

        Emulated senders tick in lumps: Gearmulator's MD makes its MIDI once
        per audio block and sends it when the block is done, so its 0xF8s
        measured 7.7 to 31 ms apart around a 19.7 ms mean, and a 24-tick
        beat read 124.9 to 129.7 BPM. The Elektron firmware re-measures the
        tempo every beat, so it showed all of that. Hardware clock followers
        smooth their input; this does the same with an alpha-beta tracker:
        each tick lands `PLL_PHASE` of the way from where it was expected to
        where it came, and `PLL_FREQ` of the error trims the period, so the
        tracker glides with a tempo that changes rather than snapping to it.
        It starts over only after a pause (a gap over `MAX_TICK_S`) or when it
        has lost the sender by more than `LOST_TICKS` ticks; a Start or
        Continue re-anchors its phase (`transport`).
        """
        last, p, pred = self._clk_raw, self._clk_period, self._clk_pred
        self._clk_raw = raw
        gap = None if last is None else raw - last
        if gap is None or not 0 < gap <= self.MAX_TICK_S * self.ips:
            self._clk_period = self._clk_pred = None      # after a pause
            return raw
        if p is None:
            self._clk_period, self._clk_pred = gap, raw + gap
            return raw
        if pred is None:                                   # re-anchored
            self._clk_pred = raw + p
            return raw
        err = raw - pred
        if abs(err) > self.LOST_TICKS * p:
            self._clk_period, self._clk_pred = gap, raw + gap
            return raw
        slot = pred + self.PLL_PHASE * err
        self._clk_period = p + self.PLL_FREQ * err
        self._clk_pred = slot + self._clk_period
        return slot

    def _push(self, target, b):
        self._seq += 1
        heapq.heappush(self._queue, (target, self._seq, b))

    def _schedule(self, done):
        with self._lock:
            stamped, self._stamped = self._stamped, collections.deque()
        gap = self.BYTE_S * self.ips
        for t, data in stamped:
            target = (t + self.MARGIN_S) * self.ips + self._offset
            for b in data:
                if b >= 0xF8:
                    # Realtime: it may fall between any two bytes on the wire,
                    # so it skips the DIN chain. Clock takes its smoothed
                    # slot; Start/Continue keep their own and re-anchor the
                    # clock's phase, so the first tick after them -- which
                    # arrived later -- can never be placed ahead of them.
                    if b == 0xF8:
                        slot = self._clock_slot(target)
                    else:
                        slot = target
                        if b in (0xFA, 0xFB):
                            self._clk_pred = None
                    if slot < done:
                        self.late += 1
                        slot = done
                    if b == 0xF8:
                        if self._last_clock is not None:
                            slot = max(slot, self._last_clock + self.CLOCK_GAP_S * self.ips)
                        self._last_clock = slot
                    self._push(slot, b)
                    continue
                if self._last_target is not None:
                    target = max(target, self._last_target + gap)
                if target < done:
                    self.late += 1
                    target = done
                self._push(target, b)
                self._last_target = target

    def step(self, done, remaining=None):
        """longrun.spin: instructions until the next byte is due, or None."""
        if self.m is None:
            return None
        self._track(done)
        self._schedule(done)
        if not self._queue:
            return None
        wait = int(self._queue[0][0] - done)
        if wait > 0:
            return wait
        # Due but masked. Under the audio render, its rte ends the step and
        # the byte goes in at the first boundary after (emu/pit.render_holds);
        # polling every PENDING_STEP there cut a Digitone session into ~25,000
        # extra steps and made it crackle.
        if render_holds(self.m, done, interrupt_level(self.m, RX_VECTOR)):
            return remaining
        return PENDING_STEP

    def service(self, done):
        """longrun.spin: write the bytes now due and raise vector 156."""
        if not self._queue or self._queue[0][0] > done:
            return False
        level = interrupt_level(self.m, RX_VECTOR)
        sr = self.m.uc.reg_read(UC_M68K_REG_SR)
        if level is None or ((sr >> 8) & 7) >= level:
            self.deferred += 1      # masked: `step` retries on the next pass
            return False
        data = bytearray()
        until = done + self.WINDOW_S * self.ips
        while self._queue and self._queue[0][0] <= until:
            data.append(heapq.heappop(self._queue)[2])
        feed(self.m, data)
        self.delivered += len(data)
        if self.m.raise_vector(RX_VECTOR, level=level):
            self.raised += 1
            return True
        return False

    def deliver(self, m):
        """Worker thread, between chunks. -> True if the vector was raised
        (the PC changed)."""
        if not self._pending:
            return False
        level = interrupt_level(m, RX_VECTOR)
        sr = m.uc.reg_read(UC_M68K_REG_SR)
        if level is None or ((sr >> 8) & 7) >= level:
            self.deferred += 1      # masked, or not set up yet: next time
            return False
        with self._lock:
            data = bytes(self._pending[:MAX_FEED])
            del self._pending[:MAX_FEED]
        feed(m, data)
        self.delivered += len(data)
        # Bytes already in the ring are drained by the next vector if this
        # one is not taken, so nothing is lost either way.
        if m.raise_vector(RX_VECTOR, level=level):
            self.raised += 1
            return True
        return False


class MidiOut:
    """UART9's transmitter: bytes the firmware sends go to `sink(bytes)`,
    called on the emulator's thread."""

    def __init__(self, sink):
        self.sink = sink
        self._dma_done = False
        # Source 53 unmasked by the driver. It says so through INTC1's
        # SIMR/CIMR, which nothing here turns into IMR bits, so they are
        # watched directly: CIMR when it queues a byte, SIMR once the queue
        # is empty. Off until then, as after the driver's own init.
        self.tx_enabled = False
        self.sent = 0               # bytes out
        self.direct = 0             # of which written to UTB9 by the CPU
        self.dma_runs = 0           # channel 37 transfers
        self.raised = {TX_VECTOR: 0, TX_DMA_VECTOR: 0}

    def install(self, m, bank=None):
        """Hook UTB9 and, when there is a software eDMA bank, its channel
        37 starts (chained onto whatever else watches SSRT)."""
        m.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_utb, begin=UTB9, end=UTB9)
        m.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_mask, begin=INTC1_SIMR,
                      end=INTC1_CIMR)
        if bank is None:
            return
        before, after = bank.before_ssrt, bank.after_ssrt

        def chained_before(channel):
            if before is not None:
                before(channel)
            if channel.channel == TX_CHANNEL:
                self._on_dma(m)

        def chained_after(channel):
            if after is not None:
                after(channel)
        bank.before_ssrt, bank.after_ssrt = chained_before, chained_after

    def _on_mask(self, uc, access, address, size, value, data):
        if size == 1 and value & 0x7F == TX_SOURCE:
            self.tx_enabled = address == INTC1_CIMR

    def _on_utb(self, uc, access, address, size, value, data):
        self.direct += 1
        self._out(bytes([value & 0xFF]))

    def _on_dma(self, m):
        """Channel 37 is about to move its buffer to UTB9: take the bytes
        from the descriptor as it would read them, and complete it at the
        next boundary."""
        rd = lambda off, n: bytes(m.uc.mem_read(TCD37 + off, n))
        saddr = struct.unpack('>I', rd(TCD_SADDR, 4))[0]
        attr = struct.unpack('>H', rd(TCD_ATTR, 2))[0]
        soff = struct.unpack('>h', rd(TCD_SOFF, 2))[0]
        nbytes = struct.unpack('>I', rd(TCD_NBYTES, 4))[0]
        citer = iteration_count(struct.unpack('>H', rd(TCD_CITER, 2))[0])
        smod = (attr >> 11) & 0x1F
        out = bytearray()
        for _ in range(min(citer * nbytes, 0x10000)):
            out += bytes(m.uc.mem_read(saddr, 1))
            saddr = modulo_add(saddr, soff, smod)
        self.dma_runs += 1
        self._dma_done = True
        self._out(bytes(out))

    def _out(self, data):
        self.sent += len(data)
        if data:
            self.sink(data)

    def deliver(self, m):
        """Worker thread, between chunks: the transmit interrupts. -> True
        if one was raised (the PC changed)."""
        if self._dma_done:
            if _take(m, TX_DMA_VECTOR):
                self._dma_done = False
                self.raised[TX_DMA_VECTOR] += 1
                return True
            return False
        # UART9's TxRDY is always set here (nothing takes time to send), so
        # its interrupt is pending whenever the driver enables it: the
        # driver masks source 53 once its queue is empty.
        if (self.tx_enabled and m.uc.mem_read(UIMR9, 1)[0] & 1
                and _take(m, TX_VECTOR)):
            self.raised[TX_VECTOR] += 1
            return True
        return False


class Parser:
    """A MIDI byte stream -> whole messages: running status, realtime bytes
    in the middle of a message, and SysEx."""

    LENGTHS = {0x80: 2, 0x90: 2, 0xA0: 2, 0xB0: 2, 0xC0: 1, 0xD0: 1,
               0xE0: 2, 0xF1: 1, 0xF2: 2, 0xF3: 1, 0xF6: 0}

    def __init__(self):
        self.status = None
        self.data = []
        self.sysex = None

    def feed(self, data):
        out = []
        for b in data:
            if b >= 0xF8:                       # realtime: anywhere
                out.append([b])
            elif b == 0xF0:
                self.sysex = [b]
                self.status = None
            elif b == 0xF7:
                if self.sysex is not None:
                    out.append(self.sysex + [b])
                self.sysex = None
            elif self.sysex is not None and b < 0x80:
                self.sysex.append(b)
            elif b >= 0x80:
                self.sysex = None
                self.data = []
                need = self.LENGTHS.get(b if b >= 0xF0 else b & 0xF0)
                if need is None:                # undefined: 0xF4, 0xF5
                    self.status = None
                elif need == 0:
                    out.append([b])
                    self.status = None
                else:
                    # System common cancels running status after it.
                    self.status = b
            elif self.status is not None:
                self.data.append(b)
                s = self.status
                if len(self.data) == self.LENGTHS[s if s >= 0xF0 else s & 0xF0]:
                    out.append([s] + self.data)
                    self.data = []
                    if s >= 0xF0:
                        self.status = None
        return out


def _rtmidi():
    try:
        import rtmidi
    except ImportError as exc:
        raise OSError('python-rtmidi is not installed '
                      '(uv sync --extra midi)') from exc
    return rtmidi


def port_name(full):
    """A port's name without ALSA's trailing client:port numbers, which
    change when a device is plugged in again: what a saved choice keeps."""
    return re.sub(r'\s+\d+:\d+$', '', full)


class HostMidi:
    """The host's side of the DIN ports: a virtual input and output named
    `name` where the platform has them (Linux, macOS), for a DAW to connect
    to, and one chosen device input and output (set_input, set_output), for
    a controller or a synth without any patching.

    Messages from either input go to `sink(bytes)`; send() takes the
    firmware's byte stream and sends whole messages to both outputs.
    DIGIEMU_MIDI_IN and DIGIEMU_MIDI_OUT pick a device by a substring of
    its name at start.
    """

    def __init__(self, name, sink):
        self._rt = _rtmidi()
        self.name = name
        self._sink = sink
        self._lock = threading.Lock()
        self._parser = Parser()
        self.problems = []
        self._vin = self._vout = None
        self._din = self._dout = None
        self.input = self.output = None             # chosen devices' names
        try:
            self._vin = self._open_in(lambda p: p.open_virtual_port(name))
        except Exception as exc:                          # noqa: BLE001
            self.problems.append('no virtual MIDI input (%s)' % exc)
        try:
            self._vout = self._rt.MidiOut(name=name)
            self._vout.open_virtual_port(name)
        except Exception as exc:                          # noqa: BLE001
            self._vout = None
            self.problems.append('no virtual MIDI output (%s)' % exc)
        for env, pick, ports in (('DIGIEMU_MIDI_IN', self.set_input,
                                  self.inputs),
                                 ('DIGIEMU_MIDI_OUT', self.set_output,
                                  self.outputs)):
            want = os.environ.get(env)
            if want:
                hit = next((p for p in ports() if want in p), None)
                try:
                    pick(hit or want)
                except OSError as exc:
                    self.problems.append(str(exc))

    def _open_in(self, how):
        port = self._rt.MidiIn(name=self.name)
        # Keep SysEx and clock; drop active sensing.
        port.ignore_types(sysex=False, timing=False, active_sense=True)
        try:
            how(port)
        except Exception as exc:
            port.delete()
            raise OSError(str(exc)) from exc
        port.set_callback(lambda event, _data: self._sink(bytes(event[0])))
        return port

    def _list(self, cls):
        probe = cls(name=self.name + ' (list)')
        try:
            names = [port_name(p) for p in probe.get_ports()]
        finally:
            probe.delete()
        return [n for n in names if not n.startswith(self.name)]

    def inputs(self):
        """Device inputs that can be chosen, by name, not ours."""
        return self._list(self._rt.MidiIn)

    def outputs(self):
        return self._list(self._rt.MidiOut)

    @staticmethod
    def _index(port, name):
        for i, full in enumerate(port.get_ports()):
            if port_name(full) == name:
                return i
        raise OSError('MIDI port %r is not there' % name)

    def set_input(self, name):
        """Take input from the device `name` too (None: none). Raises
        OSError when it is not there, leaving no device input."""
        with self._lock:
            old, self._din, self.input = self._din, None, None
        if old is not None:
            old.close_port()
            old.delete()
        if name:
            self._din = self._open_in(
                lambda p: p.open_port(self._index(p, name)))
            self.input = name

    def set_output(self, name):
        with self._lock:
            old, self._dout, self.output = self._dout, None, None
        if old is not None:
            old.close_port()
            old.delete()
        if name:
            port = self._rt.MidiOut(name=self.name)
            try:
                port.open_port(self._index(port, name))
            except Exception as exc:
                port.delete()
                raise OSError(str(exc)) from exc
            with self._lock:
                self._dout, self.output = port, name

    def send(self, data):
        with self._lock:
            for msg in self._parser.feed(data):
                for port in (self._vout, self._dout):
                    if port is not None:
                        port.send_message(msg)

    def close(self):
        self.set_input(None)
        self.set_output(None)
        with self._lock:
            ports, self._vin, self._vout = (self._vin, self._vout), None, None
        for port in ports:
            if port is not None:
                port.close_port()
                port.delete()
