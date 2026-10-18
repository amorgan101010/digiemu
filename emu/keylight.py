"""The computer keyboard's own lights as the keymap.

A Keychron keyboard with per-key RGB (their QMK firmware's raw HID commands,
keyboards/keychron/common/rgb/keychron_rgb.c in their public source) is lit
by what each key does while a panel window has the focus: the trigs one
colour, the knobs another, and so on, the groups and colours of the keymap
sheet. Keys that do nothing are dark. When the focus leaves, the keyboard
gets back the colours and the effect it had.

The firmware's per-key effect has no dark: it takes a key's hue and
saturation and lights it at the keyboard's brightness. Its "mixed" effect
does: every key belongs to one of two regions, each with its own list of
effects, and a region with no effect is not lit. So the keys in use are
region 0, which runs the per-key effect, and the rest are region 1, which
runs nothing.

Nothing is saved on the keyboard: colours and effect are set in its RAM, so
unplugging it also restores what it had. Without such a keyboard, or without
access to its hidraw node, this does nothing. DIGIEMU_KEYLIGHT=0 turns it
off, and so does ELEKTREMU_KEYLIGHT=0, which the Elektremu panels honour too.

`python3 -m emu.keylight serve` is the same thing for a program that is not
Python (Elektremu's native JUCE panels): see Broker.

The keyboard is found by its raw HID interface (usage page 0xFF60) and has
to be the 84-key 75% layout below: its LEDs are numbered in reading order,
which is checked against the keyboard's own matrix before anything is lit.
"""
import glob
import json
import os
import select
import signal
import sys
import threading
import time

try:
    import fcntl
except ImportError:              # Windows: no hidraw keyboard there either
    fcntl = None

from emu import panelkeys

KEYCHRON = '00003434'
RAW_USAGE = bytes((0x06, 0x60, 0xFF))
KC_RGB = 0xA8                    # Keychron's RGB command group
RGB_GET_VERSION, RGB_GET_LED_COUNT, RGB_GET_LED_IDX = 0x01, 0x05, 0x06
PER_KEY_GET_TYPE, PER_KEY_SET_TYPE = 0x07, 0x08
PER_KEY_GET_COLOR, PER_KEY_SET_COLOR = 0x09, 0x0A
MIXED_GET_REGIONS, MIXED_SET_REGIONS = 0x0C, 0x0D
MIXED_GET_EFFECTS, MIXED_SET_EFFECTS = 0x0E, 0x0F
PER_KEY_SOLID = 0
VIA_SET, VIA_GET = 0x07, 0x08    # VIA custom values; never VIA's save (9)
VIA_RGB_MATRIX, VIA_EFFECT = 3, 2
# The firmware's own effects follow the 23 in the keyboard's VIA definition.
PER_KEY_EFFECT, MIXED_EFFECT = 23, 24
NO_EFFECT = 0
CLEAR_S = 0.06                  # for the keyboard to draw a dark frame
PER_PACKET = 9
REGIONS_PER_PACKET = 28
LAYERS, EFFECTS_PER_LAYER, EFFECT_LEN = 2, 5, 8
LIT, UNLIT = 0, 1               # the regions
# One effect of a region's list: effect, hue, saturation, speed, and how
# long it runs in ms (four bytes, little-endian).
PER_KEY_ENTRY = bytes((PER_KEY_EFFECT, 0, 255, 127)) + (5000).to_bytes(4, 'little')
NO_ENTRY = bytes(EFFECT_LEN)
MATRIX_COLS = 16

# The keys, as X keysyms, in the order of the LEDs. None has no keysym.
LAYOUT = (
    ('Escape', 'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10',
     'F11', 'F12', 'Print', 'Delete', None),
    ('grave', '1', '2', '3', '4', '5', '6', '7', '8', '9', '0', 'minus',
     'equal', 'BackSpace', 'Prior'),
    ('Tab', 'q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p', 'bracketleft',
     'bracketright', 'backslash', 'Next'),
    ('Caps_Lock', 'a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l', 'semicolon',
     'apostrophe', 'Return', 'Home'),
    ('Shift_L', 'z', 'x', 'c', 'v', 'b', 'n', 'm', 'comma', 'period',
     'slash', 'Shift_R', 'Up', 'End'),
    ('Control_L', 'Super_L', 'Alt_L', 'space', 'Alt_R', None, 'Control_R',
     'Left', 'Down', 'Right'),
)

# Group -> (hue, saturation, value), QMK's 0..255. The keymap sheet's groups
# in its hues, but fully saturated and spread round the wheel: under a keycap
# the sheet's own softer colours are hard to tell apart.
COLOURS = {
    'steps': (16, 255, 255), 'menus': (45, 255, 255),
    'transport': (85, 255, 255), 'knobs': (128, 255, 255),
    'tracks': (170, 255, 255), 'nav': (195, 255, 255),
    'banks': (218, 255, 255), 'pages': (240, 255, 255),
    'mod': (0, 0, 255),
}
DARK = (0, 0, 0)
GROUPS = {
    'transport': ('PLAY', 'STOP', 'RECORD'),
    'nav': ('UP', 'DOWN', 'LEFT', 'RIGHT', 'YES', 'NO', 'BACK'),
    'tracks': ('TRK', 'T1', 'T2', 'T3', 'T4', 'T5', 'T6', 'MIDI', 'TRACK'),
    'menus': ('TEMPO', 'GLOBAL', 'PTN', 'SONG', 'SAMPLE', 'VOICE',
              'KEYBOARD', 'SETTINGS', 'RETRIG', 'PATTERN'),
    'banks': ('BANK',),
}
GROUP_OF = {label: group for group, labels in GROUPS.items()
            for label in labels}
MOD_KEYS = (*panelkeys.FUNC_KEYS, *panelkeys.SHIFT_KEYS,
            panelkeys.RELEASE_ALL, 'Escape')


def group(label):
    """-> the keymap group of a panel key: anything unlisted is a page key."""
    if label.isdigit():
        return 'steps'
    return GROUP_OF.get(label, 'pages')


def colours(keys, knobs):
    """-> keysym -> (h, s, v) for a panel's keys (keysym -> label) and knobs
    (keysym -> encoder label). A key that pushes a knob is a knob's."""
    out = {key: COLOURS['mod'] for key in MOD_KEYS}
    for key, label in keys.items():
        out[key] = COLOURS['knobs' if label in knobs.values()
                           else group(label)]
    for key in (*knobs, *panelkeys.TURN, *panelkeys.PRESS_TURN):
        out[key] = COLOURS['knobs']
    return out


def by_group(groups):
    """-> keysym -> (h, s, v) from group -> keysyms, for a machine whose
    keys are not a panel's here (tools outside this package use it). The
    turn keys are knobs and the modifiers are lit, as for a panel."""
    out = {key: COLOURS['mod'] for key in MOD_KEYS}
    out.update({key: COLOURS['knobs']
                for key in (*panelkeys.TURN, *panelkeys.PRESS_TURN)})
    for name, keys in groups.items():
        out.update({key: COLOURS[name] for key in keys})
    return out


def disabled(environ=None):
    """-> True if the user has turned the keyboard's lights off."""
    environ = os.environ if environ is None else environ
    return '0' in (environ.get('DIGIEMU_KEYLIGHT', ''),
                   environ.get('ELEKTREMU_KEYLIGHT', ''))


def find():
    """-> the hidraw path of a Keychron's raw HID interface, or None."""
    for sys_dir in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        try:
            with open(sys_dir + '/device/uevent') as f:
                uevent = f.read()
            with open(sys_dir + '/device/report_descriptor', 'rb') as f:
                usage = f.read(3)
        except OSError:
            continue
        if ':%s:' % KEYCHRON in uevent and usage == RAW_USAGE:
            return '/dev/' + os.path.basename(sys_dir)
    return None


class Keyboard:
    """One Keychron over raw HID: 32-byte packets, each answered."""

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def close(self):
        os.close(self.fd)

    def ask(self, *packet):
        """-> the 32-byte answer, or None: no answer, or the keyboard did
        not know the command (it answers 0xFF)."""
        while select.select([self.fd], [], [], 0)[0]:
            os.read(self.fd, 64)
        os.write(self.fd, bytes((0, *packet)) + bytes(32 - len(packet)))
        if not select.select([self.fd], [], [], 0.5)[0]:
            return None
        answer = os.read(self.fd, 64)
        return answer if answer[:1] == bytes(packet[:1]) else None

    def rgb(self, command, *args):
        """A Keychron RGB command -> its answer's data, or None if it
        failed."""
        answer = self.ask(KC_RGB, command, *args)
        if answer is None or answer[2] != 0:
            return None
        return answer[3:]

    def led_rows(self):
        """-> the LED numbers of each matrix row, left to right, or None."""
        if self.rgb(RGB_GET_VERSION) is None:
            return None
        rows = []
        for row in range(len(LAYOUT)):
            data = self.rgb(RGB_GET_LED_IDX, row, 0xFF, 0xFF, 0xFF)
            if data is None:
                return None
            rows.append([led for led in data[:MATRIX_COLS] if led != 0xFF])
        return rows

    def get_colours(self, count):
        out = []
        for start in range(0, count, PER_PACKET):
            n = min(PER_PACKET, count - start)
            data = self.rgb(PER_KEY_GET_COLOR, start, n)
            if data is None:
                return None
            out += [tuple(data[i * 3:i * 3 + 3]) for i in range(n)]
        return out

    def need(self, command, *args):
        """A Keychron RGB command that has to be accepted: OSError if not,
        so that half a picture is not taken for a whole one."""
        if self.rgb(command, *args) is None:
            raise OSError('the keyboard did not accept RGB command %#x'
                          % command)

    def set_colours(self, hsv):
        for start in range(0, len(hsv), PER_PACKET):
            part = hsv[start:start + PER_PACKET]
            self.need(PER_KEY_SET_COLOR, start, len(part),
                      *(c for colour in part for c in colour))

    def get_regions(self, count):
        out = []
        for start in range(0, count, REGIONS_PER_PACKET):
            n = min(REGIONS_PER_PACKET, count - start)
            data = self.rgb(MIXED_GET_REGIONS, start, n)
            if data is None:
                return None
            out += list(data[:n])
        return out

    def set_regions(self, regions):
        for start in range(0, len(regions), REGIONS_PER_PACKET):
            part = regions[start:start + REGIONS_PER_PACKET]
            self.need(MIXED_SET_REGIONS, start, len(part), *part)

    def get_effects(self):
        """-> each region's effect list, EFFECT_LEN bytes an entry."""
        out = []
        for layer in range(LAYERS):
            entries = []
            for start in range(0, EFFECTS_PER_LAYER, 3):
                n = min(3, EFFECTS_PER_LAYER - start)
                data = self.rgb(MIXED_GET_EFFECTS, layer, start, n)
                if data is None:
                    return None
                entries += [bytes(data[i * EFFECT_LEN:(i + 1) * EFFECT_LEN])
                            for i in range(n)]
            out.append(entries)
        return out

    def set_effects(self, lists):
        for layer, entries in enumerate(lists):
            for start in range(0, len(entries), 3):
                part = entries[start:start + 3]
                self.need(MIXED_SET_EFFECTS, layer, start, len(part),
                          *b''.join(part))

    def effect(self):
        answer = self.ask(VIA_GET, VIA_RGB_MATRIX, VIA_EFFECT)
        return None if answer is None else answer[3]

    def set_effect(self, value):
        self.ask(VIA_SET, VIA_RGB_MATRIX, VIA_EFFECT, value)


class KeyLight:
    """Lights the keyboard for a panel, and puts back what it showed.

    Two panels are two processes with one keyboard between them, and the
    one losing the focus hears of it no sooner than the one gaining it. So
    what the keyboard had, and which panel's keymap is on it now, are in a
    file both read under a lock (`state_dir`, the user's runtime directory
    by default): a panel only puts the lights back if no other has taken
    them since, and none talks to the keyboard while another is.
    """

    def __init__(self, state_dir=None):
        self.kb = None
        self.order = None        # LED number -> keysym or None
        self.me = '%d-%d' % (os.getpid(), id(self))
        self.dir = state_dir or os.environ.get('XDG_RUNTIME_DIR') or '/tmp'
        self.state = os.path.join(self.dir, 'digiemu-keylight.json')

    def _open(self):
        if self.kb is not None:
            return True
        if disabled():
            return False
        path = find()
        if path is None or not os.access(path, os.R_OK | os.W_OK):
            return False
        kb = Keyboard(path)
        rows = kb.led_rows()
        flat = [led for row in rows or () for led in row]
        if (rows is None or [len(r) for r in rows] != [len(r) for r in LAYOUT]
                or flat != list(range(len(flat)))):
            kb.close()           # not this layout: leave its lights alone
            return False
        self.kb = kb
        self.order = [key for row in LAYOUT for key in row]
        return True

    def _locked(self, fn):
        """Run fn() with the keyboard to ourselves. -> False if there is
        no locking here (not Linux), where there is no keyboard either."""
        if fcntl is None:
            return False
        try:
            with open(self.state + '.lock', 'w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                fn()
        except OSError:
            self._drop()
        return True

    def _read(self):
        """-> (owner, what the keyboard had) or (None, None)."""
        try:
            with open(self.state) as f:
                data = json.load(f)
            effect, kind, hsv, regions, effects = data['saved']
            return data['owner'], (
                effect, kind, [tuple(c) for c in hsv], regions,
                [[bytes.fromhex(e) for e in entries] for entries in effects])
        except (OSError, ValueError, KeyError, TypeError):
            return None, None

    def _write(self, saved):
        effect, kind, hsv, regions, effects = saved
        data = {'owner': self.me, 'saved': [
            effect, kind, hsv, regions,
            [[e.hex() for e in entries] for entries in effects]]}
        with open(self.state + '.tmp', 'w') as f:
            json.dump(data, f)
        os.replace(self.state + '.tmp', self.state)

    def show(self, keys, knobs):
        """Light the keys of a panel: `keys` and `knobs` as panelkeys has
        them."""
        self.paint(colours(keys, knobs))

    def paint(self, table):
        """Light the keys of `table`, keysym -> (h, s, v); the rest go
        dark."""
        self._locked(lambda: self._paint(table))

    def _paint(self, table):
        if not self._open():
            return
        _owner, saved = self._read()
        if saved is None:        # no keymap is on it: this is its own state
            kind = self.kb.rgb(PER_KEY_GET_TYPE)
            saved = (self.kb.effect(), kind and kind[0],
                     self.kb.get_colours(len(self.order)),
                     self.kb.get_regions(len(self.order)),
                     self.kb.get_effects())
            if None in saved:
                return
        self._write(saved)
        # Put every key out first: a region with no effect keeps
        # whatever was last drawn on its keys.
        self.kb.set_effect(NO_EFFECT)
        time.sleep(CLEAR_S)
        try:
            self.kb.set_colours([table.get(key, DARK) for key in self.order])
            self.kb.set_regions([LIT if key in table else UNLIT
                                 for key in self.order])
            rest = [NO_ENTRY] * (EFFECTS_PER_LAYER - 1)
            self.kb.set_effects([[PER_KEY_ENTRY, *rest], [NO_ENTRY, *rest]])
            self.kb.rgb(PER_KEY_SET_TYPE, PER_KEY_SOLID)
            self.kb.set_effect(MIXED_EFFECT)
        except OSError:
            # Half a picture: give back what it had if we still can. If not,
            # the file still has it, and the next restore tries again.
            try:
                self._restore()
            except OSError:
                pass
            raise

    def restore(self):
        """Give the keyboard back the colours and effect it had, unless
        another panel has lit it since."""
        self._locked(self._restore)

    def _restore(self):
        owner, saved = self._read()
        if owner != self.me:
            return
        # An error while painting drops the keyboard but leaves us the
        # owner of what it had: find it again to give that back.
        if not self._open():
            return
        effect, kind, hsv, regions, effects = saved
        self.kb.set_colours(hsv)
        self.kb.set_regions(regions)
        self.kb.set_effects(effects)
        self.kb.rgb(PER_KEY_SET_TYPE, kind)
        self.kb.set_effect(effect)
        # Only now: if a write above failed, what the keyboard had is
        # still on file for the next try.
        try:
            os.remove(self.state)
        except FileNotFoundError:
            pass

    def _drop(self):
        """The keyboard went away: forget it, and look again next time."""
        try:
            if self.kb is not None:
                self.kb.close()
        except OSError:
            pass
        self.kb = None


# What the panels that are not Python send: group names, never colours, so
# the palette above stays the one.
DEBOUNCE_S = 0.15


class Broker:
    """The lights for a program that is not Python, one per panel process.

    `python3 -m emu.keylight serve` reads one JSON object a line from stdin:

        {"show": {"<keysym>": "<group>", ...}}   light these keys
        {"restore": true}                        the panel lost the focus
        {"quit": true}                           (or end of input) restore, exit

    and keeps one KeyLight, so the owner token in the state file is the same
    for the panel's whole life. A panel that crashes closes the pipe, so its
    lights go back whatever happened to it. A restore waits DEBOUNCE_S, and
    a show in that time cancels it: window managers deliver the focus
    leaving one panel before the focus arriving at the next, and the
    keyboard should not flash what it had in between. The owner check
    runs when the restore does, so a restore that comes after another panel
    has lit the keyboard changes nothing.
    """

    def __init__(self, light, clock=time.monotonic):
        self.light = light
        self.clock = clock
        self.restore_at = None

    @staticmethod
    def table(keys):
        """-> keysym -> (h, s, v) from a {keysym: group} the panel sent.
        What is not a key of the keyboard, or not a group, is ignored."""
        on_board = {key for row in LAYOUT for key in row}
        return {key: COLOURS[group] for key, group in keys.items()
                if key in on_board and group in COLOURS}

    def handle(self, line):
        """Act on one line. -> False when the panel is done."""
        try:
            message = json.loads(line)
            keys = message.get('show')
            if isinstance(keys, dict):
                self.restore_at = None
                self.light.paint(self.table(keys))
            elif message.get('restore'):
                self.restore_at = self.clock() + DEBOUNCE_S
            elif message.get('quit'):
                return False
        except (ValueError, AttributeError, TypeError):
            pass                 # a line that is not ours: not worth dying for
        return True

    @staticmethod
    def coalesce(lines):
        """-> the lines that matter of those waiting. A paint is about a
        second of HID traffic, so a panel switched back and forth faster than
        that would queue a paint for each switch: only where the focus ended
        up counts, and a quit is kept."""
        last, quit_line = None, None
        for line in lines:
            try:
                message = json.loads(line)
                if message.get('quit'):
                    quit_line = line
                elif isinstance(message.get('show'), dict) or message.get('restore'):
                    last = line
                else:
                    continue
            except (ValueError, AttributeError, TypeError):
                continue
        return [line for line in (last, quit_line) if line is not None]

    def timeout(self):
        """-> seconds to wait for the next line, or None for as long as it
        takes."""
        if self.restore_at is None:
            return None
        return max(0.0, self.restore_at - self.clock())

    def tick(self):
        """Do a restore that has waited long enough."""
        if self.restore_at is not None and self.clock() >= self.restore_at:
            self.restore_at = None
            self.light.restore()

    def finish(self):
        self.restore_at = None
        self.light.restore()


class Stop(Exception):
    """A signal asked the broker to finish."""


def _stop(_signum, _frame):
    raise Stop()


def serve(fd=0, light=None):
    """Run a Broker on a file descriptor until it ends or a signal comes."""
    broker = Broker(light or KeyLight())
    buffer = b''
    main = threading.current_thread() is threading.main_thread()
    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        if main:
            signal.signal(number, _stop)
    try:
        running = True
        while running:
            ready = select.select([fd], [], [], broker.timeout())[0]
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buffer += chunk
                if b'\n' in buffer:
                    *lines, buffer = buffer.split(b'\n')
                    for line in broker.coalesce(lines):
                        if running:
                            running = broker.handle(line)
            broker.tick()
    except Stop:
        pass
    finally:
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            if main:
                signal.signal(number, signal.SIG_IGN)
        try:
            broker.finish()
        except Exception as error:              # noqa: BLE001
            print('keylight: could not restore: %s' % error, file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(serve() if sys.argv[1:] == ['serve'] else 2)
