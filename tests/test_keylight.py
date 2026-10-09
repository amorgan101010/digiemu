"""emu/keylight.py: the keyboard's lights as the keymap."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emu import dnpanel, dtpanel, keylight, mdpanel, panelkeys      # noqa: E402


class FakeKeyboard:
    """A keyboard that remembers its colours and effect, as the real one."""

    def __init__(self):
        self.hsv = [(i, 255, 255) for i in range(84)]
        self.mode, self.kind = 5, 2
        self.closed = False
        self.modes = []
        self.regions = [0] * 84
        self.effects = [[bytes((5,)) + bytes(7)] + [bytes(8)] * 4,
                        [bytes((2,)) + bytes(7)] + [bytes(8)] * 4]

    def effect(self):
        return self.mode

    def set_effect(self, value):
        self.mode = value
        self.modes.append(value)

    def get_colours(self, count):
        return list(self.hsv[:count])

    def set_colours(self, hsv):
        self.hsv = list(hsv)

    def get_regions(self, count):
        return list(self.regions[:count])

    def set_regions(self, regions):
        self.regions = list(regions)

    def get_effects(self):
        return [list(entries) for entries in self.effects]

    def set_effects(self, lists):
        self.effects = [list(entries) for entries in lists]

    def state(self):
        return (self.mode, self.kind, list(self.hsv), list(self.regions),
                [list(entries) for entries in self.effects])

    def rgb(self, command, *args):
        if command == keylight.PER_KEY_GET_TYPE:
            return bytes((self.kind,))
        if command == keylight.PER_KEY_SET_TYPE:
            self.kind = args[0]
        return b''


class Colours(unittest.TestCase):
    def test_the_layout_is_the_84_keys_in_led_order(self):
        self.assertEqual([len(row) for row in keylight.LAYOUT],
                         [16, 15, 15, 14, 14, 10])

    def test_every_computer_key_of_every_panel_is_on_the_keyboard(self):
        on_board = {key for row in keylight.LAYOUT for key in row}
        tables = [(p.KEYS, p.KNOB_KEYS)
                  for p in (dtpanel.DigitaktPanel, dnpanel.DigitonePanel)]
        tables += [mdpanel.keys(name) for name in mdpanel.KNOBS]
        for keys, knobs in tables:
            self.assertEqual(set(keylight.colours(keys, knobs)) - on_board,
                             set())

    def test_groups(self):
        table = keylight.colours(panelkeys.DIGITAKT, panelkeys.KNOBS)
        for key, group in (('F1', 'steps'), ('8', 'steps'), ('a', 'knobs'),
                           ('minus', 'knobs'), ('space', 'transport'),
                           ('Up', 'nav'), ('u', 'menus'), ('grave', 'banks'),
                           ('q', 'tracks'), ('l', 'pages'), ('Prior', 'pages'),
                           ('Control_L', 'mod'), ('Delete', 'mod')):
            self.assertEqual(table[key], keylight.COLOURS[group], key)
        self.assertNotIn('9', table)

    def test_a_model_s_pads_and_its_knob_push(self):
        table = keylight.colours(*mdpanel.keys('Model:Cycles'))
        self.assertEqual(table['b'], keylight.COLOURS['tracks'])
        self.assertEqual(table['q'], keylight.COLOURS['knobs'])
        self.assertEqual(table['Return'], keylight.COLOURS['knobs'])
        self.assertEqual(table['BackSpace'], keylight.COLOURS['nav'])
        self.assertNotIn('Up', table)


class Lights(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.kb = FakeKeyboard()
        self.light = self.panel()
        self.before = self.kb.state()

    def panel(self):
        """Another panel's light, on the same keyboard."""
        light = keylight.KeyLight(self.dir.name)
        light.kb = self.kb
        light.order = [key for row in keylight.LAYOUT for key in row]
        return light

    def test_the_panel_losing_the_focus_does_not_undo_the_one_gaining_it(self):
        other = self.panel()
        self.light.show(panelkeys.DIGITONE, panelkeys.KNOBS)
        other.show(*mdpanel.keys('Model:Cycles'))   # focus moves to it...
        shown = self.kb.state()
        self.light.restore()                 # ...and the first hears late
        self.assertEqual(self.kb.state(), shown)
        other.restore()
        self.assertEqual(self.kb.state(), self.before)

    def test_focus_going_back_and_forth(self):
        other = self.panel()
        self.light.show(panelkeys.DIGITONE, panelkeys.KNOBS)
        other.show(*mdpanel.keys('Model:Cycles'))
        self.light.restore()
        self.light.show(panelkeys.DIGITONE, panelkeys.KNOBS)
        other.restore()
        self.assertEqual(self.kb.mode, keylight.MIXED_EFFECT)
        self.light.restore()
        self.assertEqual(self.kb.state(), self.before)

    def test_show_lights_the_keys_and_restore_puts_it_back(self):
        self.light.show(panelkeys.DIGITAKT, panelkeys.KNOBS)
        self.assertEqual(self.kb.modes, [keylight.NO_EFFECT,
                                         keylight.MIXED_EFFECT])
        # The keys in use run the per-key effect; the rest run nothing.
        self.assertEqual(self.kb.regions[1], keylight.LIT)            # F1
        self.assertEqual(self.kb.regions[9], keylight.UNLIT)          # F9
        self.assertEqual(self.kb.effects[keylight.LIT][0][0],
                         keylight.PER_KEY_EFFECT)
        self.assertEqual(set(self.kb.effects[keylight.UNLIT]), {bytes(8)})
        self.assertEqual(self.kb.kind, keylight.PER_KEY_SOLID)
        self.assertEqual(self.kb.hsv[1], keylight.COLOURS['steps'])   # F1
        self.assertEqual(self.kb.hsv[15], keylight.DARK)              # light
        self.light.restore()
        self.assertEqual(self.kb.state(), self.before)

    def test_a_second_show_keeps_what_was_there_first(self):
        self.light.show(panelkeys.DIGITAKT, panelkeys.KNOBS)
        self.light.show(panelkeys.DIGITONE, panelkeys.KNOBS)
        self.light.restore()
        self.light.restore()
        self.assertEqual(self.kb.state(), self.before)


if __name__ == '__main__':
    unittest.main()
