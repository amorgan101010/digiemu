"""emu/longrun.never_fake_set_posts: a semaphore real code posts through
the RTOS's set-post routine stops being faked by `unblock`.

No firmware: the routine is a `nop` at a code address (only its entry
matters), entered with a stack holding a return address and the semaphore,
as a call leaves it.
"""
import struct
import unittest

from unicorn.m68k_const import UC_M68K_REG_A7

from emu.harness import Machine
from emu.longrun import never_fake_set_posts

CODE = 0x40010000
STACK = 0x40200000
SEM = 0x41234560


class SetPostTest(unittest.TestCase):
    def test_a_post_marks_its_semaphore_never_fake(self):
        m = Machine()
        for addr in (CODE, STACK - 0x100, SEM):
            m.ensure(addr)
        m.uc.mem_write(CODE, b'\x4e\x71')                  # nop
        ev = {'unblock_skip': {0x1111}}
        self.assertEqual(never_fake_set_posts(m, ev, CODE), CODE)
        sp = STACK - 0x10
        m.uc.mem_write(sp, struct.pack('>II', 0x40000000, SEM))
        m.uc.reg_write(UC_M68K_REG_A7, sp)
        m.uc.emu_start(CODE, CODE + 2)
        self.assertIn(SEM, ev['unblock_skip'])
        self.assertEqual(ev['never_fake_seen'], {SEM})

    def test_nothing_to_hook(self):
        self.assertIsNone(never_fake_set_posts(Machine(), {'unblock_skip': set()},
                                               None))
        self.assertIsNone(never_fake_set_posts(Machine(), {}, CODE))


if __name__ == '__main__':
    unittest.main()
