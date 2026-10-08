"""emu/panelkeys.py: the computer keyboard as the front panel."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emu import dnpanel, dtpanel, mdpanel, panelkeys               # noqa: E402
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


MODEL_KC = {'Control_L': 37, 'g': 42, 'b': 56, 'k': 45, 'q': 24, 'w': 25,
            'Return': 36, 'equal': 21, 'bracketright': 35}


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


class _Window:
    """dtpanel.DigitaktPanel's input methods, without a Tk window."""
    press = dtpanel.DigitaktPanel.press
    release = dtpanel.DigitaktPanel.release
    clear_latched = dtpanel.DigitaktPanel.clear_latched
    _key = dtpanel.DigitaktPanel._key

    def __init__(self):
        fake = FakePanel()
        self.codes, self.enc_codes = fake.codes, fake.enc_codes
        self.after, self.after_cancel = fake.after, fake.after_cancel
        self.elapse = fake.elapse
        self.held, self.latched, self._named = set(), set(), True
        self.emu = type('Emu', (), {'inbox': []})()
        self.keyboard = Keyboard(self, dtpanel.DigitaktPanel.KEYS)

    def _paint(self, code):
        pass

    def key(self, keysym, down, state=0):
        ev = type('Ev', (), dict(keysym=keysym, keycode=KC[keysym],
                                 state=state))()
        self._key(ev, down)


class ShiftClick(unittest.TestCase):
    def test_letting_go_of_shift_clears_mouse_latches(self):
        """A mouse shift-click latches until Shift is let go, like the
        keyboard's; a key the keyboard still holds stays down."""
        w = _Window()
        func, one = w.codes['FUNC'], w.codes['1']
        w.key('Shift_L', True)
        w.key('F1', True, SHIFT_MASK)            # keyboard, held
        w.press(func, type('Ev', (), {'state': SHIFT_MASK})())    # mouse
        w.release(func)                          # mouse-up: stays latched
        self.assertEqual(w.held, {func, one})
        w.key('Shift_L', False, SHIFT_MASK)
        self.assertEqual(w.held, {one})
        self.assertIn(('release', func, 0), w.emu.inbox)
        w.key('F1', False)
        w.elapse()
        self.assertEqual(w.held, set())


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

    def test_every_model_key_pad_and_knob_has_a_computer_key(self):
        for name in mdpanel.KNOBS:
            with self.subTest(panel=name):
                buttons, encoders = mdpanel.layout(name)
                keys, knobs = mdpanel.keys(name)
                mapped = set(keys.values())
                self.assertEqual(mapped - set(buttons), {'LEVEL/DATA'})
                self.assertEqual(
                    set(buttons) - mapped, {panelkeys.MODEL_FUNC})
                self.assertEqual(set(knobs.values()), set(encoders))
                self.assertEqual(len(knobs), len(encoders))

    def test_no_computer_key_is_both_a_key_and_a_knob(self):
        tables = [(p.KEYS, p.KNOB_KEYS)
                  for p in (dtpanel.DigitaktPanel, dnpanel.DigitonePanel)]
        tables += [mdpanel.keys(name) for name in mdpanel.KNOBS]
        turns = set(panelkeys.TURN) | set(panelkeys.PRESS_TURN)
        for keys, knobs in tables:
            self.assertEqual(set(keys) & set(knobs), set())
            self.assertEqual((set(keys) | set(knobs)) & turns, set())


class Model(unittest.TestCase):
    """A Model: FUNCTION for FUNC, knobs by name, LEVEL/DATA the one that pushes."""

    def setUp(self):
        self.panel = FakePanel(('1', 'FUNCTION', 'LEVEL/DATA', 'T1', 'MACHINE'))
        self.panel.enc_codes = {'PITCH': 13, 'DECAY': 1, 'LEVEL/DATA': 4}
        keys, knobs = mdpanel.keys('Model:Cycles')
        self.kb = Keyboard(self.panel, keys, knobs, panelkeys.MODEL_FUNC)
        self.code = self.panel.codes

    def key(self, down, keysym, state=0):
        return self.kb.key(down, keysym, MODEL_KC[keysym], state)

    def test_ctrl_holds_function(self):
        self.key(True, 'Control_L')
        self.assertEqual(self.panel.take(),
                         [('press', self.code['FUNCTION'], False)])
        self.key(False, 'Control_L')
        self.assertEqual(self.panel.take(),
                         [('release', self.code['FUNCTION'])])

    def test_keys_and_pads(self):
        for keysym, label in (('b', 'T1'), ('k', 'MACHINE'),
                              ('Return', 'LEVEL/DATA')):
            self.key(True, keysym)
            self.assertEqual(self.panel.take(),
                             [('press', self.code[label], False)])
            self.key(False, keysym)
            self.panel.elapse()
            self.panel.take()

    def test_knobs_turn_by_name(self):
        self.key(True, 'w')                      # DECAY, second in the grid
        self.key(True, 'equal')
        self.assertEqual(self.panel.take(), [('turn', 1, 1)])

    def test_only_level_data_clicks(self):
        for keysym, clicks in (('g', True), ('q', False)):
            self.key(True, keysym)
            self.key(False, keysym)
            self.panel.elapse()
            pitch = self.code['LEVEL/DATA']
            self.assertEqual(self.panel.take(),
                             [('press', pitch, False), ('release', pitch)]
                             if clicks else [])

    def test_a_press_turn_without_a_push_switch_is_a_turn(self):
        self.key(True, 'w')
        self.key(True, 'bracketright')
        self.assertEqual(self.panel.take(), [('turn', 1, 1)])

    def test_level_data_press_turns(self):
        self.key(True, 'g')
        self.key(True, 'bracketright')
        self.assertEqual(self.panel.take(),
                         [('press', self.code['LEVEL/DATA'], False)])
        self.panel.elapse()
        self.assertEqual(self.panel.take(), [('turn', 4, 1)])

    def test_the_window_takes_a_keyboard_press(self):
        """ModelPanel.press takes the keyboard's latch, as the Digitakt's
        does, for a key and for a pad (which plays at the default)."""
        emu = type('Emu', (), {})()
        emu.device = type('Dev', (), {'pads': {33: 5}})()
        emu.inbox = []
        w = object.__new__(mdpanel.ModelPanel)   # no window: press only
        w.emu, w._paint = emu, lambda code: None
        w.held, w.latched = set(), set()
        w.press(5, latch=True)
        w.press(33, latch=False)
        self.assertEqual(emu.inbox, [('press', 5, 0), ('press', 33, 0)])
        self.assertEqual((w.held, w.latched), ({5, 33}, {5}))


if __name__ == '__main__':
    unittest.main()
