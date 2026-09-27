# pyright: reportMissingImports=false, reportArgumentType=false
# fmt: off
"""The four DMA timers, DTIM0..DTIM3.

The MCF5441x has four DMA timers at base addresses `0xFC070000`,
`0xFC074000`, `0xFC078000`, `0xFC07C000`. Their interrupt sources are INTC0
sources 32..35, so vectors 96..99 (vector = 64 + source). Register layout,
verified against the MCF54418 reference manual chapter 39
(`docs/refs/MCF5441XRM.pdf`, section 39.2, table 39-1):

    +0x00  DTMRn   16-bit  mode
    +0x02  DTXMRn   8-bit  extended mode (DMAEN bit 7)
    +0x03  DTERn    8-bit  event, write-1-to-clear; bit 1 = REF, bit 0 = CAP
    +0x04  DTRRn   32-bit  reference value
    +0x08  DTCRn   32-bit  capture
    +0x0C  DTCNn   32-bit  counter, read-only

DTMRn fields (manual table 39-2):

    15-8  PS    prescaler, divides by PS+1
    7-6   CE    capture edge; 00 = reference mode
    5     OM    output mode
    4     ORRI  interrupt on reference reached
    3     FRR   1 = restart on reference, 0 = free run
    2-1   CLK   00 stop, 01 bus clock / 1, 10 bus clock / 16, 11 external pin
    0     RST   1 = enabled, 0 = held in reset

Two timers are live in this firmware, both armed, unmasked and at INTC level
2, and neither was ever delivered before this change. They are entries 1 and
4 of the nineteen armed-but-undelivered vectors in
`docs/history/upstream/HANDOVER.md` section 7.

**DTIM1, vector 97, handler `0x40128c4c`** is a one-shot microsecond sleep.
`0x40128c7c(n)` writes DTRR1 = n, then DTMR1 = `0x841b` (PS = 132 so divide
by 133, CLK = 01 so bus clock, ORRI set, FRR set, RST set), then pends
semaphore `0x44e4d69c` at `0x40128d08`. The ISR write-1-clears DTER1 bit 1,
writes DTMR1 = 0 to stop the timer, and posts that semaphore. At the
132 MHz bus clock the prescaler gives a 992.5 kHz tick -- a 1 microsecond
resolution, which is what the firmware is evidently calibrating for.
`0x40128c7c` has twelve call sites and is the firmware's general-purpose
short sleep.

**DTIM3, vector 99, handler `0x400c30e4`** is the main application loop's
tick. DTMR3 = `0x001d` (PS = 0 so divide by 1, CLK = 10 so bus clock over
16, ORRI set, FRR set, RST set) and DTRR3 = `0x43238` = 274,488, which at
132 MHz is 33.27 ms -- **30.05 Hz**. Its ISR write-1-clears DTER3 bit 1 and
calls `queue_send(0x4094ef3c, ...)`. `0x4094ef3c` is the queue the main
application task `0x40032f5a` blocks on at its message-loop head
`0x40033492`. So DTIM3 is what wakes the whole user interface, and until it
is delivered the main task waits forever on a queue nothing feeds. A period
landing on 30.05 Hz from the independently-established 132 MHz bus clock is
the same kind of corroboration the three PIT rates gave.

Also state plainly: DTIM0 and DTIM2 are disabled in every run measured so
far (DTMR0 = 0, DTMR2 = 7 with CLK = 11, external pin) -- they are modelled
because the arithmetic is identical, not because they have been seen to
fire.
"""
import collections
import math
import struct

from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn.m68k_const import UC_M68K_REG_SR

from emu.pit import (F_BUS, ICR_BASE, IDLE_STEP, IMR_BASE, INSTR_PER_SEC,
                     INTC, PENDING_STEP, _nothing_due, mem_reader,
                     render_holds)

BASES   = (0xFC070000, 0xFC074000, 0xFC078000, 0xFC07C000)
VECTORS = (96, 97, 98, 99)              # INTC0 sources 32..35

DTMR, DTXMR, DTER, DTRR, DTCN = 0x00, 0x02, 0x03, 0x04, 0x0C
RST, FRR, ORRI = 0x01, 0x08, 0x10       # DTMR bit 0, bit 3, bit 4
DMAEN = 0x80                            # DTXMR bit 7

# A timer armed part-way through a step is not noticed until the step ends,
# because `service` only runs at an `emu_start` boundary and the boundaries
# are the timer deadlines themselves. So a sleep is longer than the firmware
# asked for by however far into the step the arming write fell.
#
# The size of that error is worth stating plainly, because it is large.
# DTIM1's period is about 480 instructions; the widest gap between boundaries
# is PIT2's period, about 78,000. A microsecond sleep can therefore take two
# orders of magnitude longer than it should, and the firmware's own 100us
# pacing between 4KB coprocessor transfers is correspondingly stretched.
#
# It costs fidelity, not correctness: every sleep still ends, in order, and
# the interrupt still lands on the instruction its deadline fell on. Nothing
# here has been tuned to hide it. Closing the gap needs sub-step resolution,
# which means either a per-instruction hook (7.6x, see longrun._FastStepper) or
# stopping the run from inside the write hook -- and `emu_stop` under `spin`
# makes the instruction accounting a lie, which HANDOVER section 4 spells out.
#
# `ARM_STEP` bounds only the case the arming write and the boundary genuinely
# interleave: the firmware writes DTRR and then DTMR three instructions later
# (`0x40128cf6` then `0x40128d02`), and a boundary between the two leaves the
# timer visible-but-not-yet-enabled. Then `deadline` cannot arm it and the
# `arm` flag survives to shorten the following step. That is a narrow case,
# and this constant should not be read as a fix for the paragraph above.
ARM_STEP = 256


class Dtims:
    """Deliver DMA timer interrupts on an instruction-count clock.

    `channels` defaults to `(3,)` -- the main-loop tick alone. That is not
    because the others cannot be modelled but because DTIM3 is the one shown
    to help: delivering it takes the main application task from one pass
    through its message loop to sixty-five, and it is the only producer of
    messages for that queue at boot. DTIM1 is left out until the stale-arm
    problem `clear_stale` describes is settled on a clean snapshot; DTIM0 and
    DTIM2 have never been seen enabled. Pass `channels=(3, 1)` to try both.

    When more than one channel is given the order is the tie-break, on the
    same reasoning as `Pits`' `(3, 2, 0)`:
    both DTIM1 and DTIM3 are INTC level 2, so on a collision the first
    walked wins. The MCF5441x INTC breaks a same-level tie by highest
    source number, verified in the reference manual chapter 17 section
    17.3.1 table 19 -- DTIM3 is source 35 and DTIM1 is source 33, so
    highest-first is also what the hardware does.
    """

    def __init__(self, m, channels=(3,), instr_per_sec=INSTR_PER_SEC,
                 hold=False, clear_stale=True):
        self.m = m
        self.channels = tuple(channels)
        self.held = bool(hold)
        self.ips = instr_per_sec
        self.next = [None] * 4
        self.now = 0
        self.fired = collections.Counter()
        self.missed = collections.Counter()
        # Channels whose registers were written but which `deadline` has
        # not been able to arm yet -- see ARM_STEP. Normally empty.
        self.arm = set()
        # Channels with a tick the IPL has refused so far, as in Pits. This
        # is what keeps the UI alive under live audio: the mk1 render holds
        # the IPL above DTIM3's level for most of each pass, and dropping the
        # refused ticks took the main loop from 17 passes a second to 4.
        self.pending = set()

        self.stale = []
        if clear_stale:
            self.clear_stale()

        for ch in self.channels:
            b = BASES[ch]
            m.uc.hook_add(UC_HOOK_MEM_WRITE,
                          (lambda c: lambda uc, t, a, s, v, d: self.arm.add(c))(ch),
                          begin=b, end=b + 0x0F)

        # The instruction count at the last step or service: guest time.
        self.clock = 0
        for ch in range(4):
            m.uc.hook_add(UC_HOOK_MEM_READ,
                          (lambda c: lambda uc, t, a, s, v, d: self._serve_count(c))(ch),
                          begin=BASES[ch] + DTCN, end=BASES[ch] + DTCN + 3)

    def _serve_count(self, ch):
        """Put DTCNn's value in place just before the firmware reads it.

        The firmware timestamps things with DTIM0's counter: its MIDI clock
        input reads DTCN0 on every clock byte, sums the gaps over 24 clocks
        and divides by that sum (0x400d4464..0x400d4606 on Digitakt 1.53).
        A counter that never moves makes the sum 0 -- a divide-by-zero, the
        firmware's EXCEPTION screen and a halt on the first beat of incoming
        clock. Nothing in the OS image programs DTMR0, so the boot ROM must
        leave it counting; the tempo arithmetic (0x6EA42090 / (sum >> 9),
        clamped to 30..300 BPM in 1/120 BPM) only fits a count at the bus
        clock. So a DTIM0 still in reset counts at F_BUS; a timer the
        firmware did program counts at its own prescaler and clock, and
        restarts at DTRR when FRR is set.

        Resolution is a step boundary (see ARM_STEP), which is fine for
        timing events milliseconds apart.
        """
        raw = mem_reader(self.m)(BASES[ch] + DTMR, 8)
        dtmr, _dtxmr, _dter, dtrr = struct.unpack('>HBBI', raw)
        if dtmr & RST:
            clk = (dtmr >> 1) & 0x03
            if clk not in (1, 2):             # stopped, or an external pin
                return
            div = (((dtmr >> 8) & 0xFF) + 1) * (1 if clk == 1 else 16)
        elif ch == 0 and dtmr == 0:
            div = 1                           # as the boot ROM leaves it
        else:
            return
        count = int(self.clock / self.ips * F_BUS / div)
        if dtmr & RST and dtmr & FRR:
            count %= dtrr + 1
        self.m.uc.mem_write(BASES[ch] + DTCN,
                            struct.pack('>I', count & 0xFFFFFFFF))

    def clear_stale(self):
        """Stop any timer a snapshot left armed. -> the channels stopped.

        Every snapshot in `snapshots/` from `boot60M` onward carries DTIM1
        enabled with DTMR1 = 0x841b and its sleep semaphore `0x44e4d69c` at
        zero: armed, with nobody waiting on it. That state is not reachable
        on hardware. It exists because `0x40128c7c` arms the timer and pends,
        `unblock` force-satisfies the pend, and the ISR that would have
        written DTMR1 = 0 was never delivered -- so the arm has been rolling
        forward, unstopped, through every snapshot ever written. `boot40M`,
        taken before the sleep subsystem was first used, is the only clean one.

        Delivering that arm injects an interrupt nobody is waiting for. It is
        not survivable: measured from `postintro.snap` with channels `(1,)`,
        DTIM1 fires once at instruction 4,721 and the run reaches the fault
        handler and halts at 9.6M with zero tasks spawned, against six and no
        fault for the same run with the DMA timers left out.

        So on installation the model does what the missing ISR would have
        done. This is a repair of damaged state, not a model of hardware, and
        it is wrong for a snapshot written by a run that was itself
        delivering DMA timer interrupts -- there the arm is real and a task
        is genuinely asleep. Pass `clear_stale=False` for such a snapshot.
        """
        for ch in self.channels:
            if self.period(ch) is None:
                continue
            self.m.uc.mem_write(BASES[ch] + DTMR, b'\x00\x00')
            self.stale.append(ch)
        return self.stale

    def release(self):
        """Start delivering. Safe to call more than once."""
        self.held = False

    def checkpoint_state(self):
        return {'type': 'Dtims', 'version': 1, 'channels': self.channels,
                'ips': self.ips, 'next': list(self.next), 'now': self.now,
                'held': self.held, 'fired': dict(self.fired),
                'missed': dict(self.missed), 'arm': sorted(self.arm),
                'stale': list(self.stale), 'pending': sorted(self.pending)}

    def restore_checkpoint_state(self, state):
        if state.get('type') != 'Dtims' or state.get('version') != 1:
            raise RuntimeError('unsupported Dtims checkpoint state')
        if tuple(state['channels']) != self.channels or state['ips'] != self.ips:
            raise RuntimeError('Dtims checkpoint configuration mismatch')
        self.next = list(state['next']); self.now = state['now']
        self.held = state['held']; self.fired = collections.Counter(state['fired'])
        self.missed = collections.Counter(state['missed'])
        self.arm = set(state['arm']); self.stale = list(state['stale'])
        self.pending = set(state.get('pending', ()))

    def period(self, ch):
        """-> instructions between interrupts, or None if it cannot fire.

        DTMR..DTRR are read every call, in one read; the arithmetic is
        remembered, keyed on those bytes and the rate (as `Pits.period`).
        """
        raw = mem_reader(self.m)(BASES[ch] + DTMR, 8)
        cache = self.__dict__.setdefault('_period_cache', {})
        hit = cache.get(ch)
        if hit is not None and hit[0] == raw and hit[1] == self.ips:
            return hit[2]
        p = self._period_from(raw)
        cache[ch] = (raw, self.ips, p)
        return p

    def _period_from(self, raw):
        dtmr, dtxmr, _dter, dtrr = struct.unpack('>HBBI', raw)
        if not (dtmr & RST) or not (dtmr & ORRI) or (dtxmr & DMAEN):
            return None
        clk = (dtmr >> 1) & 0x03
        if clk == 0 or clk == 3:          # stopped, or an external pin
            return None
        div = 1 if clk == 1 else 16
        # dtrr + 1 matches the convention Pits.period uses for PMR; the
        # one-count difference is under a percent and below the resolution
        # of an instruction-count clock anyway.
        ticks = (dtrr + 1) * (((dtmr >> 8) & 0xFF) + 1) * div
        return ticks / F_BUS * self.ips

    def level(self, vec):
        """-> the source's interrupt level, or None if masked or disabled.

        Same routine as `Pits.level`.
        """
        for base, first in INTC:
            if first <= vec < first + 64:
                src = vec - first
                break
        else:
            return None
        read = mem_reader(self.m)
        icr = read(base + ICR_BASE + src, 1)[0] & 0x07
        if not icr:
            return None
        imrh, imrl = struct.unpack('>II', read(base + IMR_BASE, 8))
        masked = (imrl >> src) & 1 if src < 32 else (imrh >> (src - 32)) & 1
        return None if masked else icr

    def deadline(self, done):
        """-> the instruction count at which the next channel is due."""
        if self.held:
            return None
        best = None
        for ch in self.channels:
            p = self.period(ch)
            # Whatever the answer, this channel has now been looked at with
            # the registers as they stand, so it is no longer waiting to be
            # noticed. Discarding only on the arming branch leaves the flag
            # set for good the first time a channel is found switched off --
            # which is every time DTIM1's ISR writes DTMR = 0 -- and `step`
            # then caps every step at ARM_STEP for the rest of the run. That
            # bug cost a 120M-instruction run about 350,000 `emu_start`
            # boundaries instead of 1,500, and HANDOVER section 10 is
            # explicit that subdividing `emu_start` changes what the firmware
            # does. Clear it here, once, for both outcomes.
            self.arm.discard(ch)
            if p is None:
                self.next[ch] = None
                continue
            if self.next[ch] is None:
                self.next[ch] = done + p
            if best is None or self.next[ch] < best:
                best = self.next[ch]
        return best

    def step(self, done, remaining=None):
        """-> instructions to run before the next `service` call is due."""
        self.clock = done
        d = self.deadline(done)
        try:
            n = IDLE_STEP if d is None else max(1, int(math.ceil(d - done)))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError('invalid DTIM deadline') from exc
        if d is None and remaining is not None:
            n = remaining
        if self.arm:
            n = min(n, ARM_STEP)
        if self.pending and not all(
                render_holds(self.m, done, self.level(VECTORS[ch]))
                for ch in self.pending):
            n = min(n, PENDING_STEP)
        if remaining is not None:
            n = min(n, remaining)
        return max(1, n)

    def service(self, done):
        """Call at a chunk boundary with the instruction count so far."""
        self.clock = done
        if self.held:
            return
        if _nothing_due(self, done):
            return
        for ch in self.channels:
            p = self.period(ch)
            if p is None:
                self.next[ch] = None
                self.pending.discard(ch)
                continue
            if self.next[ch] is None:
                self.next[ch] = done + p
                continue
            if done >= self.next[ch]:
                self.next[ch] += p
                if self.next[ch] <= done:
                    self.next[ch] = done + p
                # Set DTER bit 1 (REF) before raising. The firmware's ISRs
                # write-1-clear it, so a model that never sets it is handing
                # them a register that reads zero. This is the DMA-timer
                # counterpart of the PIF bit. (DTER is plain memory here, so
                # the ISR's clear does not stick; REF cannot stand in for
                # `pending`.)
                dter = self.m.uc.mem_read(BASES[ch] + DTER, 1)[0]
                self.m.uc.mem_write(BASES[ch] + DTER, bytes([dter | 0x02]))
                if ch in self.pending:
                    self.missed[ch] += 1   # merges into the tick still waiting
                self.pending.add(ch)
            if ch not in self.pending:
                continue
            vec = VECTORS[ch]
            lvl = self.level(vec)
            if lvl is None:
                self.pending.discard(ch)
                self.missed[ch] += 1       # disabled at the INTC
                continue
            sr = self.m.uc.reg_read(UC_M68K_REG_SR)
            if ((sr >> 8) & 0x07) >= lvl:
                continue                   # waits for the mask to drop
            self.pending.discard(ch)
            if self.m.raise_vector(vec, level=lvl):
                # The mask is raised by the entry trampoline, in guest code --
                # see Machine.install_srtrap and the note in Pits.service.
                self.fired[ch] += 1
            # A one-shot's disarming comes from the firmware's own ISR
            # stopping the timer, not from us: the DTIM1 ISR writes
            # DTMR = 0, so the next `period(ch)` call returns None and the
            # channel disarms itself at the top of this loop.


class Timers:
    """The PITs and the DMA timers as one object, for `spin(pits=...)`.

    `step` takes the minimum across sources, so no source's deadline is
    ever stepped over. `service` runs sources in construction order, which
    is the tie-break when a PIT and a DMA timer are due on the same
    instruction.
    """

    def __init__(self, *sources):
        self.sources = tuple(sources)

    @property
    def now(self):
        return self.sources[0].now if self.sources else 0

    @now.setter
    def now(self, v):
        for s in self.sources:
            s.now = v

    @property
    def held(self):
        return all(s.held for s in self.sources)

    def release(self):
        for s in self.sources:
            s.release()

    def checkpoint_state(self):
        return {'type': 'Timers', 'version': 1,
                # Type/order are load-bearing arbitration configuration.
                'sources': [s.checkpoint_state() for s in self.sources]}

    def restore_checkpoint_state(self, state):
        if state.get('type') != 'Timers' or state.get('version') != 1:
            raise RuntimeError('unsupported Timers checkpoint state')
        saved = state.get('sources', [])
        if len(saved) != len(self.sources):
            raise RuntimeError('Timers checkpoint source count mismatch')
        for source, source_state in zip(self.sources, saved):
            if source_state.get('type') != type(source).__name__:
                raise RuntimeError('Timers checkpoint source order mismatch')
            source.restore_checkpoint_state(source_state)

    def step(self, done, remaining=None):
        return min(s.step(done, remaining) for s in self.sources)

    def service(self, done):
        for s in self.sources:
            s.service(done)

    @property
    def fired(self):
        from emu.pit import Pits
        out = {}
        for s in self.sources:
            prefix = 'PIT' if isinstance(s, Pits) else 'DTIM'
            for ch, n in s.fired.items():
                out['%s%d' % (prefix, ch)] = n
        return out

    @property
    def missed(self):
        from emu.pit import Pits
        out = {}
        for s in self.sources:
            prefix = 'PIT' if isinstance(s, Pits) else 'DTIM'
            for ch, n in s.missed.items():
                out['%s%d' % (prefix, ch)] = n
        return out


def restore_timers(m, deferred):
    """Claim saved ``Timers`` safely against restored ``m``.

    DMA timer construction normally repairs stale guest DTMR registers.  A
    stateful checkpoint instead needs those armed registers intact, so this
    constructs DTIM sources with ``clear_stale=False`` before claiming state.
    Source order and each source's saved configuration are preserved.
    """
    def construct(state):
        if state.get('type') != 'Timers' or state.get('version') != 1:
            raise RuntimeError('unsupported Timers checkpoint state')
        sources = []
        for saved in state['sources']:
            if saved['type'] == 'Pits':
                from emu.pit import Pits
                sources.append(Pits(m, channels=tuple(saved['channels']),
                                    instr_per_sec=saved['ips'], hold=saved['held']))
            elif saved['type'] == 'Dtims':
                sources.append(Dtims(m, channels=tuple(saved['channels']),
                                     instr_per_sec=saved['ips'], hold=saved['held'],
                                     clear_stale=False))
            else:
                raise RuntimeError('unsupported Timers checkpoint source')
        return Timers(*sources)
    return deferred.claim_constructed('timers', construct)


def build_timers(m, pit_hold=False, dtim=True, channels=(3, 2, 0),
                 instr_per_sec=INSTR_PER_SEC):
    """-> a Timers over the PITs and, unless dtim=False, the DMA timers.

    The PITs come first, so a PIT and a DMA timer due on the same instruction
    are tried PIT-first. That is a guess, not a reading of the hardware: the
    two live DMA timers are INTC0 sources and the PITs are INTC2 sources, and
    arbitration *between* controllers is not something this session checked.
    It only matters on an exact collision.
    """
    from emu.pit import Pits
    sources = [Pits(m, channels=channels, instr_per_sec=instr_per_sec,
                    hold=pit_hold)]
    if dtim:
        sources.append(Dtims(m, instr_per_sec=instr_per_sec, hold=pit_hold))
    return Timers(*sources)
