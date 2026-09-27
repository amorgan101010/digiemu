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
import os
import re
import struct
import threading

from unicorn import UC_HOOK_MEM_WRITE
from unicorn.m68k_const import UC_M68K_REG_SR

from emu.edma_sw import iteration_count, modulo_add
from emu.pit import interrupt_level

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
MAX_FEED = 4096                 # bytes per delivery, well inside the ring


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
    """Bytes from any thread, delivered to the guest at a chunk boundary."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = bytearray()
        self.received = 0           # bytes the host handed us
        self.delivered = 0          # bytes written into the ring
        self.raised = 0             # vector 156 taken
        self.deferred = 0           # boundaries where it could not be taken

    def put(self, data):
        with self._lock:
            self._pending += bytes(data)
            self.received += len(data)

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
