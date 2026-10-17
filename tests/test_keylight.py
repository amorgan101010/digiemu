"""emu/keylight.py: the keyboard's lights as the keymap."""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emu import dnpanel, dtpanel, keylight, mdpanel, panelkeys      # noqa: E402


class FakeKeyboard:
    """A keyboard that remembers its colours and effect, as the real one."""

    def __init__(self):
        self.hsv = [(i, 255, 255) for i in range(84)]
        self.mode, self.kind = 5, 2
        self.closed = False
        self.fail_writes_after = None    # a write that raises, as a lost HID
        self.writes = 0
        self.modes = []
        self.regions = [0] * 84
        self.effects = [[bytes((5,)) + bytes(7)] + [bytes(8)] * 4,
                        [bytes((2,)) + bytes(7)] + [bytes(8)] * 4]

    def effect(self):
        return self.mode

    def close(self):
        self.closed = True

    def _write(self):
        self.writes += 1
        if (self.fail_writes_after is not None
                and self.writes > self.fail_writes_after):
            raise OSError('the keyboard did not accept it')

    def set_effect(self, value):
        self._write()
        self.mode = value
        self.modes.append(value)

    def get_colours(self, count):
        return list(self.hsv[:count])

    def set_colours(self, hsv):
        self._write()
        self.hsv = list(hsv)

    def get_regions(self, count):
        return list(self.regions[:count])

    def set_regions(self, regions):
        self._write()
        self.regions = list(regions)

    def get_effects(self):
        return [list(entries) for entries in self.effects]

    def set_effects(self, lists):
        self._write()
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


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeLight(keylight.KeyLight):
    """A KeyLight whose keyboard is always the fake, even found again after
    a drop: a test must never reach for the real one."""

    def __init__(self, directory, kb):
        super().__init__(directory)
        self.fake = kb
        self._open()

    def _open(self):
        self.kb = self.fake
        self.order = [key for row in keylight.LAYOUT for key in row]
        return True


def bus(testcase, kb=None):
    """-> (keyboard, a function making a Broker for one more panel process)."""
    directory = tempfile.TemporaryDirectory()
    testcase.addCleanup(directory.cleanup)
    kb = kb or FakeKeyboard()
    clock = FakeClock()

    def make():
        return keylight.Broker(FakeLight(directory.name, kb), clock)
    return kb, make, clock, directory.name


MD_ISH = {'F1': 'steps', 'a': 'knobs', 'space': 'transport', 'Delete': 'mod'}
MM_ISH = {'F1': 'steps', 'q': 'tracks', 'a': 'knobs', 'Prior': 'nav'}


def show(keys):
    return json.dumps({'show': keys})


RESTORE = json.dumps({'restore': True})


class Brokering(unittest.TestCase):
    def setUp(self):
        self.kb, self.make, self.clock, self.dir = bus(self)
        self.before = self.kb.state()

    def settle(self, broker):
        self.clock.now += keylight.DEBOUNCE_S + 0.01
        broker.tick()

    def test_a_show_paints_exactly_the_keys_sent_in_their_group_colours(self):
        broker = self.make()
        broker.handle(show(MD_ISH))
        order = [key for row in keylight.LAYOUT for key in row]
        for index, key in enumerate(order):
            if key in MD_ISH:
                self.assertEqual(self.kb.hsv[index],
                                 keylight.COLOURS[MD_ISH[key]], key)
                self.assertEqual(self.kb.regions[index], keylight.LIT)
            else:
                self.assertEqual(self.kb.hsv[index], keylight.DARK, key)
                self.assertEqual(self.kb.regions[index], keylight.UNLIT, key)

    def test_what_is_not_a_key_or_a_group_is_dark(self):
        broker = self.make()
        broker.handle(show({'F1': 'steps', 'NoSuchKey': 'steps',
                            'F2': 'no-such-group', 'Menu': 'steps'}))
        self.assertEqual(self.kb.regions.count(keylight.LIT), 1)

    def test_a_restore_waits_and_a_show_cancels_it(self):
        broker = self.make()
        broker.handle(show(MD_ISH))
        broker.handle(RESTORE)
        broker.tick()
        self.assertNotEqual(self.kb.state(), self.before)   # not yet
        self.assertAlmostEqual(broker.timeout(), keylight.DEBOUNCE_S)
        broker.handle(show(MM_ISH))
        self.settle(broker)
        self.assertEqual(self.kb.mode, keylight.MIXED_EFFECT)
        broker.handle(RESTORE)
        self.settle(broker)
        self.assertEqual(self.kb.state(), self.before)

    def test_focus_out_restores_to_the_exact_prior_state(self):
        broker = self.make()
        broker.handle(show(MM_ISH))
        broker.handle(RESTORE)
        self.settle(broker)
        self.assertEqual(self.kb.state(), self.before)
        self.assertIsNone(broker.timeout())

    def test_switching_models_changes_the_keys_and_the_colours(self):
        md, mm = self.make(), self.make()
        md.handle(show(MD_ISH))
        md_hsv, md_regions = list(self.kb.hsv), list(self.kb.regions)
        mm.handle(show(MM_ISH))
        self.assertNotEqual(self.kb.hsv, md_hsv)
        self.assertNotEqual(self.kb.regions, md_regions)
        order = [key for row in keylight.LAYOUT for key in row]
        self.assertEqual(self.kb.hsv[order.index('q')],
                         keylight.COLOURS['tracks'])

    def test_the_loser_of_a_direct_transfer_cannot_overwrite_the_winner(self):
        md, mm = self.make(), self.make()
        md.handle(show(MD_ISH))
        mm.handle(show(MM_ISH))          # focus moved to MM...
        shown = self.kb.state()
        md.handle(RESTORE)               # ...and MD hears of it later
        self.settle(md)
        self.assertEqual(self.kb.state(), shown)
        md.finish()
        self.assertEqual(self.kb.state(), shown)
        mm.handle(RESTORE)
        self.settle(mm)
        self.assertEqual(self.kb.state(), self.before)

    def test_focus_ping_pong_ends_in_the_original_state(self):
        md, mm = self.make(), self.make()
        for _ in range(5):
            md.handle(show(MD_ISH))
            mm.handle(show(MM_ISH))
            md.handle(RESTORE)
            md.handle(show(MD_ISH))
            mm.handle(RESTORE)
            self.settle(mm)
            self.settle(md)
        self.assertEqual(self.kb.mode, keylight.MIXED_EFFECT)
        md.handle(RESTORE)
        self.settle(md)
        self.assertEqual(self.kb.state(), self.before)

    def test_closing_the_owner_restores(self):
        md, mm = self.make(), self.make()
        md.handle(show(MD_ISH))
        mm.finish()                       # a panel that never had the focus
        self.assertNotEqual(self.kb.state(), self.before)
        md.finish()
        self.assertEqual(self.kb.state(), self.before)

    def test_closing_a_non_owner_leaves_the_owner_s_lights(self):
        md, mm = self.make(), self.make()
        md.handle(show(MD_ISH))
        mm.handle(show(MM_ISH))
        shown = self.kb.state()
        md.finish()                       # MD closes while MM owns the keys
        self.assertEqual(self.kb.state(), shown)
        mm.finish()
        self.assertEqual(self.kb.state(), self.before)

    def test_a_dead_owner_s_snapshot_is_inherited_not_painted_over(self):
        crashed, next_panel = self.make(), self.make()
        crashed.handle(show(MD_ISH))      # ...and is never heard of again
        next_panel.handle(show(MM_ISH))
        next_panel.finish()
        self.assertEqual(self.kb.state(), self.before)

    def test_lines_that_are_not_ours_do_nothing(self):
        broker = self.make()
        for line in (b'', b'not json', b'[1]', b'{"show": 3}', b'null'):
            self.assertTrue(broker.handle(line))
        self.assertEqual(self.kb.state(), self.before)

    def test_quit_and_end_of_input_restore(self):
        broker = self.make()
        broker.handle(show(MD_ISH))
        self.assertFalse(broker.handle(json.dumps({'quit': True})))
        broker.finish()
        self.assertEqual(self.kb.state(), self.before)

    def test_serve_restores_when_the_panel_s_pipe_closes(self):
        read, write = os.pipe()
        light = FakeLight(self.dir, self.kb)
        thread = threading.Thread(target=keylight.serve, args=(read, light))
        thread.start()
        os.write(write, (show(MD_ISH) + '\n' + RESTORE + '\n').encode())
        os.write(write, show(MM_ISH).encode()[:7])     # a torn last line
        os.close(write)                   # the panel crashed
        thread.join(5)
        os.close(read)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.kb.state(), self.before)


class Failing(unittest.TestCase):
    def setUp(self):
        self.kb, self.make, self.clock, self.dir = bus(self)
        self.before = self.kb.state()

    def test_a_failed_paint_does_not_strand_the_ownership(self):
        # Every point at which the HID could go away during a paint.
        for fail_at in range(0, 8):
            with self.subTest(fail_at=fail_at):
                self.kb.set_effect(self.before[0])       # undo the last case
                self.kb.set_colours(self.before[2])
                self.kb.set_regions(self.before[3])
                self.kb.set_effects(self.before[4])
                self.kb.kind = self.before[1]
                self.kb.writes, self.kb.fail_writes_after = 0, fail_at
                broker = self.make()
                broker.handle(show(MD_ISH))
                self.kb.fail_writes_after = None
                broker.finish()          # the panel closes: keys come back
                self.assertEqual(self.kb.state(), self.before)
                self.assertFalse(os.path.exists(
                    os.path.join(self.dir, 'digiemu-keylight.json')))

    def test_a_failed_restore_keeps_the_snapshot_for_the_next_try(self):
        broker = self.make()
        broker.handle(show(MD_ISH))
        self.kb.writes, self.kb.fail_writes_after = 0, 1
        broker.finish()
        self.assertNotEqual(self.kb.state(), self.before)
        self.assertTrue(os.path.exists(
            os.path.join(self.dir, 'digiemu-keylight.json')))
        self.kb.fail_writes_after = None
        broker.finish()
        self.assertEqual(self.kb.state(), self.before)

    def test_a_snapshot_that_fails_midway_leaves_the_keyboard_alone(self):
        self.kb.get_regions = lambda count: None
        broker = self.make()
        broker.handle(show(MD_ISH))
        self.assertEqual(self.kb.state(), self.before)
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, 'digiemu-keylight.json')))


class Absent(unittest.TestCase):
    """No keyboard, no permission, or switched off: nothing happens."""

    def light(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return keylight.Broker(keylight.KeyLight(directory.name))

    def test_no_compatible_keyboard(self):
        with mock.patch.object(keylight, 'find', return_value=None):
            broker = self.light()
            broker.handle(show(MD_ISH))
            broker.finish()
            self.assertIsNone(broker.light.kb)

    def test_no_hidraw_permission(self):
        with mock.patch.object(keylight, 'find', return_value='/dev/hidraw99'), \
                mock.patch.object(os, 'access', return_value=False), \
                mock.patch.object(keylight, 'Keyboard') as keyboard:
            broker = self.light()
            broker.handle(show(MD_ISH))
            broker.finish()
            keyboard.assert_not_called()

    def test_a_keyboard_of_another_layout_is_left_alone(self):
        wrong = mock.Mock()
        wrong.led_rows.return_value = [[0, 1], [2]]
        with mock.patch.object(keylight, 'find', return_value='/dev/x'), \
                mock.patch.object(os, 'access', return_value=True), \
                mock.patch.object(keylight, 'Keyboard', return_value=wrong):
            broker = self.light()
            broker.handle(show(MD_ISH))
            wrong.set_colours.assert_not_called()
            wrong.close.assert_called()

    def test_switched_off_by_either_variable(self):
        for name in ('DIGIEMU_KEYLIGHT', 'ELEKTREMU_KEYLIGHT'):
            with self.subTest(name=name), \
                    mock.patch.dict(os.environ, {name: '0'}), \
                    mock.patch.object(keylight, 'find') as find:
                broker = self.light()
                broker.handle(show(MD_ISH))
                broker.finish()
                find.assert_not_called()

    def test_other_values_do_not_switch_it_off(self):
        self.assertFalse(keylight.disabled({'ELEKTREMU_KEYLIGHT': '1'}))
        self.assertTrue(keylight.disabled({'DIGIEMU_KEYLIGHT': '0'}))
        self.assertTrue(keylight.disabled({'ELEKTREMU_KEYLIGHT': '0'}))


if __name__ == '__main__':
    unittest.main()
