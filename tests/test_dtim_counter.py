"""DTCNn counts with guest time, as the firmware's timestamps need.

The mk1 firmware times incoming MIDI clock by DTIM0's counter and divides by
the gaps it measures; a counter that never moved made that a divide-by-zero
and halted both Digitakt and Digitone on the first beat of external clock.
"""
import struct
import unittest

from unicorn.m68k_const import UC_M68K_REG_D0

from emu import dtim, pit
from emu.harness import Machine

CODE = 0x40001000
READ_DTCN0 = struct.pack('>HI', 0x2039, dtim.BASES[0] + dtim.DTCN)   # move.l abs.l,d0


def _machine(dtmr0=0):
    m = Machine()
    m.ensure(CODE)
    m.ensure(dtim.BASES[0])
    m.uc.mem_write(CODE, READ_DTCN0 + b'\x4e\x71')                     # + nop
    m.uc.mem_write(dtim.BASES[0] + dtim.DTMR, struct.pack('>H', dtmr0))
    return m, dtim.Dtims(m, channels=(3,), instr_per_sec=64_000_000,
                         clear_stale=False)


def _read(m):
    m.uc.emu_start(CODE, CODE + len(READ_DTCN0))
    return m.uc.reg_read(UC_M68K_REG_D0)


class DtimCounterTest(unittest.TestCase):
    def test_dtim0_left_in_reset_counts_at_the_bus_clock(self):
        m, timers = _machine()
        timers.service(64_000_000)                  # one guest second
        self.assertEqual(_read(m), pit.F_BUS)

    def test_it_moves_between_reads(self):
        m, timers = _machine()
        timers.step(1_000_000)
        first = _read(m)
        timers.step(1_320_000)                      # 5 ms later
        self.assertEqual(_read(m) - first,
                         int(1_320_000 / 64_000_000 * pit.F_BUS)
                         - int(1_000_000 / 64_000_000 * pit.F_BUS))

    def test_a_programmed_timer_uses_its_prescaler(self):
        dtmr = dtim.RST | (2 << 1) | (3 << 8)       # bus / 16, PS = 3 (/4)
        m, timers = _machine(dtmr)
        timers.service(64_000_000)
        self.assertEqual(_read(m), pit.F_BUS // 64)

    def test_a_stopped_timer_is_left_alone(self):
        m, timers = _machine(dtim.RST)              # CLK = 00: stopped
        m.uc.mem_write(dtim.BASES[0] + dtim.DTCN, struct.pack('>I', 1234))
        timers.service(64_000_000)
        self.assertEqual(_read(m), 1234)


if __name__ == '__main__':
    unittest.main()
