"""emu/midi.py: MIDI in through UART9's eDMA channel 36 ring and vector 156.

No firmware: channel 36's descriptor is set up as the mk1 builds leave it
(ATTR 0x0078, a 32 KB destination ring), vector 156 points at an `rte`, and
INTC1 source 28 is armed at level 4, as on both builds.
"""
import struct
import unittest

from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC, UC_M68K_REG_SR

from emu import midi, pit
from emu.harness import VBR, Machine

HANDLER = 0x40100000
STACK = 0x40200000
RING = 0x4BC00000
INTC1 = 0xFC04C000


def _machine(level=4):
    m = Machine()
    for addr in (VBR, HANDLER, STACK - 0x100, INTC1, midi.TCD36, RING):
        m.ensure(addr)
    m.uc.mem_write(VBR + midi.RX_VECTOR * 4, struct.pack('>I', HANDLER))
    m.uc.mem_write(HANDLER, b'\x4e\x73')                     # rte
    m.uc.mem_write(INTC1 + pit.ICR_BASE + 28, bytes([level]))
    m.uc.mem_write(midi.TCD36 + midi.TCD_ATTR, struct.pack('>H', 0x0078))
    m.uc.mem_write(midi.TCD36 + midi.TCD_DADDR, struct.pack('>I', RING))
    m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
    m.uc.reg_write(UC_M68K_REG_A7, STACK)
    return m


def _daddr(m):
    return struct.unpack('>I', bytes(m.uc.mem_read(
        midi.TCD36 + midi.TCD_DADDR, 4)))[0]


class MidiInTest(unittest.TestCase):
    def test_bytes_land_in_the_ring_and_raise_the_vector(self):
        m = _machine()
        mi = midi.MidiIn()
        mi.put(b'\x90\x3c\x64')
        self.assertTrue(mi.deliver(m))
        self.assertEqual(bytes(m.uc.mem_read(RING, 3)), b'\x90\x3c\x64')
        self.assertEqual(_daddr(m), RING + 3)
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), HANDLER)
        self.assertEqual((mi.received, mi.delivered, mi.raised), (3, 3, 1))
        self.assertFalse(mi.deliver(m))                     # nothing pending

    def test_the_ring_wraps_at_its_modulo(self):
        m = _machine()
        m.uc.mem_write(midi.TCD36 + midi.TCD_DADDR,
                       struct.pack('>I', RING + 0x7FFE))
        midi.feed(m, b'\xf8\xfa\xfc')
        self.assertEqual(bytes(m.uc.mem_read(RING + 0x7FFE, 2)), b'\xf8\xfa')
        self.assertEqual(bytes(m.uc.mem_read(RING, 1)), b'\xfc')
        self.assertEqual(_daddr(m), RING + 1)

    def test_held_back_while_the_cpu_masks_it(self):
        m = _machine()
        m.uc.reg_write(UC_M68K_REG_SR, 0x2400)              # IPL 4
        mi = midi.MidiIn()
        mi.put(b'\xfa')
        self.assertFalse(mi.deliver(m))
        self.assertEqual((mi.delivered, mi.deferred), (0, 1))
        self.assertEqual(_daddr(m), RING)                   # nothing written
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        self.assertTrue(mi.deliver(m))
        self.assertEqual(_daddr(m), RING + 1)

    def test_not_armed_is_held_back(self):
        m = _machine(level=0)
        mi = midi.MidiIn()
        mi.put(b'\xfa')
        self.assertFalse(mi.deliver(m))
        self.assertEqual(mi.deferred, 1)

    def test_a_large_burst_goes_in_slices(self):
        m = _machine()
        mi = midi.MidiIn()
        mi.put(bytes(midi.MAX_FEED + 10))
        mi.deliver(m)
        self.assertEqual(mi.delivered, midi.MAX_FEED)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        mi.deliver(m)
        self.assertEqual(mi.delivered, midi.MAX_FEED + 10)


class ParserTest(unittest.TestCase):
    def test_running_status_realtime_and_sysex(self):
        p = midi.Parser()
        stream = bytes([0x90, 60, 100, 64, 90,        # running status
                        0x80, 60, 0xF8, 0,            # clock mid-message
                        0xF0, 0x00, 0x20, 0x3C, 0xF7,  # SysEx
                        0xC3, 5, 0xFA, 0xF4, 1])      # undefined F4 dropped
        self.assertEqual(p.feed(stream), [
            [0x90, 60, 100], [0x90, 64, 90], [0xF8], [0x80, 60, 0],
            [0xF0, 0x00, 0x20, 0x3C, 0xF7], [0xC3, 5], [0xFA]])

    def test_split_across_feeds(self):
        p = midi.Parser()
        self.assertEqual(p.feed(b'\xb0\x40'), [])
        self.assertEqual(p.feed(b'\x7f'), [[0xB0, 0x40, 0x7F]])


class HostNamesTest(unittest.TestCase):
    def test_port_name_drops_alsa_numbers(self):
        self.assertEqual(
            midi.port_name('CASIO USB-MIDI:CASIO USB-MIDI MIDI 1 36:0'),
            'CASIO USB-MIDI:CASIO USB-MIDI MIDI 1')
        self.assertEqual(midi.port_name('IAC Driver Bus 1'),
                         'IAC Driver Bus 1')

    def test_settings_live_in_the_firmware_folder(self):
        import os
        import tempfile
        from emu.dtpanel import _midi_settings_path
        with tempfile.TemporaryDirectory() as root:
            snaps = os.path.join(root, 'fw', 'snapshots', 'Digitone_OS1.43')
            os.makedirs(snaps)
            snap = os.path.join(snaps, 'resume.snap')
            self.assertEqual(_midi_settings_path(snap),
                             os.path.join(snaps, 'midi.json'))
            open(os.path.join(root, 'fw', 'firmware.json'), 'w').close()
            self.assertEqual(_midi_settings_path(snap),
                             os.path.join(root, 'fw', 'midi.json'))
        self.assertIsNone(_midi_settings_path(None))


TX_RING = 0x4BC09000


def _tx_machine():
    m = _machine()
    for addr in (midi.TCD37, midi.UART9, TX_RING):
        m.ensure(addr)
    for vec in (midi.TX_VECTOR, midi.TX_DMA_VECTOR):
        m.uc.mem_write(VBR + vec * 4, struct.pack('>I', HANDLER))
    m.uc.mem_write(INTC1 + pit.ICR_BASE + 53, bytes([4]))   # UART9
    m.uc.mem_write(INTC1 + pit.ICR_BASE + 29, bytes([4]))   # eDMA 37
    return m


class MidiOutTest(unittest.TestCase):
    def test_a_direct_write_goes_out(self):
        m = _tx_machine()
        got = []
        mo = midi.MidiOut(got.append)
        mo.install(m)
        # move.b #$f8,UTB9 as guest code: the CPU's own store.
        m.uc.mem_write(HANDLER + 0x100, b'\x13\xfc\x00\xf8'
                       + struct.pack('>I', midi.UTB9))
        m.uc.emu_start(HANDLER + 0x100, HANDLER + 0x108)
        self.assertEqual(got, [b'\xf8'])
        self.assertEqual((mo.sent, mo.direct), (1, 1))

    def test_a_dma_run_goes_out_and_completes(self):
        from types import SimpleNamespace
        m = _tx_machine()
        got = []
        mo = midi.MidiOut(got.append)
        bank = SimpleNamespace(before_ssrt=None, after_ssrt=None)
        mo.install(m, bank)
        # A 4 KB ring (SMOD 12) whose three bytes wrap past its end.
        m.uc.mem_write(TX_RING + 0xFFE, b'\x90\x3c')
        m.uc.mem_write(TX_RING, b'\x64')
        m.uc.mem_write(midi.TCD37, struct.pack('>IHhI', TX_RING + 0xFFE,
                                                12 << 11, 1, 1))
        m.uc.mem_write(midi.TCD37 + midi.TCD_CITER, struct.pack('>H', 3))
        bank.before_ssrt(SimpleNamespace(channel=midi.TX_CHANNEL))
        self.assertEqual(got, [b'\x90\x3c\x64'])
        self.assertTrue(mo.deliver(m))                     # vector 157
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), HANDLER)
        self.assertEqual(mo.raised[midi.TX_DMA_VECTOR], 1)

    def test_the_uart_interrupt_follows_the_drivers_mask(self):
        m = _tx_machine()
        mo = midi.MidiOut(lambda d: None)
        mo.install(m)
        code = HANDLER + 0x100

        def store(addr, value):             # move.b #value,addr.l as guest
            m.uc.mem_write(code, b'\x13\xfc' + struct.pack('>HI', value, addr))
            m.uc.emu_start(code, code + 8)
        m.uc.mem_write(midi.UIMR9, b'\x01')
        self.assertFalse(mo.deliver(m))                    # not unmasked yet
        store(midi.INTC1_CIMR, midi.TX_SOURCE)             # a byte queued
        m.uc.mem_write(midi.UIMR9, b'\x00')                # TxRDY disabled
        self.assertFalse(mo.deliver(m))
        m.uc.mem_write(midi.UIMR9, b'\x01')
        self.assertTrue(mo.deliver(m))
        self.assertEqual(mo.raised[midi.TX_VECTOR], 1)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        store(midi.INTC1_SIMR, midi.TX_SOURCE)             # queue empty
        self.assertFalse(mo.deliver(m))


if __name__ == '__main__':
    unittest.main()
