"""emu/panelkeys.py: the computer keyboard as the front panel."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emu import dnpanel, dtpanel, panelkeys                        # noqa: E402
from emu.panelkeys import SHIFT_MASK, Keyboard                     # noqa: E402


class FakePanel:
    """Records what the keyboard asks of the panel; runs `after` callbacks
    only when the test says time has passed."""

    def __init__(self, labels=('1', '3', 'FUNC', 'PLAY', 'A', 'B', '9')):
        self.codes = {label: 100 + i for i, label in enumerate(labels)}
        self.enc_codes = {'A': 1, 'B': 2, 'LEVEL/DATA': 9}
        self.log = []
        self.timers = {}
        self.next_id = 0

    def press(self, code, latch):
        self.log.append(('press', code, latch))

    def release(self, code, force=False):
        self.log.append(('release', code))

    def turn(self, code, step):
        self.log.append(('turn', code, step))

    def release_everything(self):
        self.log.append(('release_all',))

    def after(self, ms, fn):
        self.next_id += 1
        self.timers[self.next_id] = fn
        return self.next_id

    def after_cancel(self, after_id):
        self.timers.pop(after_id, None)

    def elapse(self):
        timers, self.timers = self.timers, {}
        for fn in timers.values():
            fn()

    def take(self):
        log, self.log = self.log, []
        return log


# Keycodes are arbitrary here: the keyboard only needs them to be stable.
KC = {'F1': 67, 'F3': 69, 'space': 65, 'a': 38, 's': 39, 'g': 42,
      'equal': 21, 'minus': 20, 'bracketright': 35, 'Shift_L': 50,
      'Control_L': 37, 'Delete': 119, '1': 10, 'Tab': 23}


class Keys(unittest.TestCase):
    def setUp(self):
        self.panel = FakePanel()
        self.kb = Keyboard(self.panel, panelkeys.DIGITAKT)

    def down(self, keysym, state=0):
        return self.kb.key(True, keysym, KC[panelkeys.normalize(keysym)],
                           state)

    def up(self, keysym, state=0):
        return self.kb.key(False, keysym, KC[panelkeys.normalize(keysym)],
                           state)

    def test_normalize_unshifts(self):
        self.assertEqual(panelkeys.normalize('A'), 'a')
        self.assertEqual(panelkeys.normalize('exclam'), '1')
        self.assertEqual(panelkeys.normalize('plus'), 'equal')
        self.assertEqual(panelkeys.normalize('F3'), 'F3')
        self.assertEqual(panelkeys.normalize('='), 'equal')
        self.assertEqual(panelkeys.normalize(']'), 'bracketright')

    def test_hold_and_release_after_grace(self):
        p = self.panel
        self.assertTrue(self.down('F1'))
        self.assertEqual(p.take(), [('press', 100, False)])
        self.assertTrue(self.up('F1'))
        self.assertEqual(p.take(), [])         # waits out the grace
        p.elapse()
        self.assertEqual(p.take(), [('release', 100)])

    def test_autorepeat_pair_does_not_tap_a_held_key(self):
        p = self.panel
        self.down('F1')
        for _ in range(3):                     # X11: release/press per repeat
            self.up('F1')
            self.down('F1')
        self.assertEqual(p.take(), [('press', 100, False)])
        self.assertEqual(p.timers, {})

    def test_release_pressed_with_shift_after_shift_is_up(self):
        """Shift+1 arrives as `exclam` and its release, after Shift is up,
        as `1`. The keycode ties them together."""
        p = self.panel
        self.down('exclam', SHIFT_MASK)
        self.assertEqual(p.take(), [('press', 106, True)])
        self.up('Shift_L', SHIFT_MASK)
        self.assertEqual(p.take(), [])         # the key itself is still down
        self.up('1')
        p.elapse()
        self.assertEqual(p.take(), [('release', 106)])

    def test_shift_latches_until_shift_is_let_go(self):
        p = self.panel
        self.down('Shift_L')
        self.down('F3', SHIFT_MASK)
        self.up('F3', SHIFT_MASK)
        p.elapse()
        self.assertEqual(p.take(), [('press', 101, True)])
        self.up('Shift_L', SHIFT_MASK)
        self.assertEqual(p.take(), [('release', 101)])

    def test_ctrl_is_func_and_a_lost_keyup_is_caught(self):
        p = self.panel
        self.down('Control_L')
        self.assertEqual(p.take(), [('press', 102, False)])
        self.down('space', 0)                  # Ctrl no longer in the state
        self.assertEqual(p.take(), [('release', 102), ('press', 103, False)])

    def test_knob_hold_turns_and_tap_clicks(self):
        p = self.panel
        self.down('a')
        self.down('equal')
        self.down('equal')                     # the turn keys repeat
        self.up('equal')
        self.up('a')
        p.elapse()
        self.assertEqual(p.take(), [('turn', 1, 1), ('turn', 1, 1)])
        self.down('s')
        self.up('s')
        p.elapse()
        self.assertEqual(p.take(), [('press', 105, False), ('release', 105)])

    def test_press_turn_holds_the_push_before_the_first_detent(self):
        p = self.panel
        self.down('a')
        self.down('bracketright')
        self.assertEqual(p.take(), [('press', 104, False)])
        p.elapse()
        self.assertEqual(p.take(), [('turn', 1, 1)])
        self.up('bracketright')
        p.elapse()
        self.assertEqual(p.take(), [('release', 104)])

    def test_press_turn_without_a_push_switch_is_a_plain_turn(self):
        p = self.panel                         # LEVEL/DATA: no push code here
        self.down('g')
        self.down('bracketright')
        self.assertEqual(p.take(), [('turn', 9, 1)])
        self.up('g')
        p.elapse()
        self.assertEqual(p.take(), [])         # and no push to click

    def test_focus_loss_releases_without_clicking_a_knob(self):
        p = self.panel
        self.down('a')
        self.down('F1')
        p.take()
        self.kb.focus_lost()
        self.assertEqual(p.take(), [('release', 100)])

    def test_delete_releases_everything(self):
        p = self.panel
        self.down('F1')
        p.take()
        self.assertTrue(self.down('Delete'))
        self.assertEqual(p.take(), [('release', 100), ('release_all',)])

    def test_unmapped_keys_pass_through(self):
        self.assertFalse(self.down('Tab'))
        self.assertFalse(self.up('Tab'))
        self.assertEqual(self.panel.take(), [])


# Labels the keyboard reaches another way: FUNC is Ctrl, and the encoder
# push switches (A..H, LEVEL/DATA) are the knob keys.
BY_OTHER_MEANS = {'FUNC', *panelkeys.KNOBS.values()}


class Coverage(unittest.TestCase):
    def test_every_panel_key_has_a_computer_key(self):
        for panel in (dtpanel.DigitaktPanel, dnpanel.DigitonePanel):
            with self.subTest(panel=panel.PRODUCT):
                mapped = set(panel.KEYS.values())
                self.assertEqual(mapped - set(panel.BUTTONS), set())
                self.assertEqual(
                    set(panel.BUTTONS) - mapped - BY_OTHER_MEANS, set())
                self.assertLessEqual(set(panelkeys.KNOBS.values()),
                                     set(panel.ENCODERS))


if __name__ == '__main__':
    unittest.main()
