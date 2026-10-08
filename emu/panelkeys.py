"""The computer keyboard as the front panel.

The layout is the one Gearmulator's Monomachine and Machinedrum windows use,
so the keys that the machines share are on the same computer keys: the
sixteen trigs on F1-F8 and 1-8, Ctrl for FUNC, the arrows, Enter/Backspace
for YES/NO, Space/End/Home for PLAY/STOP/RECORD, the knobs on A-F and Z-V
(the panel's 2x4 grid) with LEVEL/DATA on G. Keys a Digitakt or Digitone has
and the Monomachine does not sit on keys that layout leaves free, or on the
nearest thing it has (` is the MM's bank-group key, BANK here).

The Models have twelve parameter knobs in three rows of four, so their knobs
take Q-R above A-F and Z-V, as the panel has them: see MODEL below.

Holding a key holds the panel key. Shift LATCHES what you press until Shift
is let go, as Gearmulator does; the panel window lets go of its mouse
shift-click latches then too. Delete lets go of everything.

Knobs: hold the knob's key and tap - or = to turn it one detent, or [ or ] to
turn it with its push switch held (press-turn). Tapping the knob's key
without turning clicks its push switch.

AUTO-REPEAT. X11 sends a release/press pair for every auto-repeat of a held
key. A release only counts once no press of the same key follows within
RELEASE_GRACE_MS, so a held trig is not tapped (which would clear it) and a
held knob stays held. The turn keys repeat on purpose: holding = keeps
turning.

Keys are tracked by keycode, not keysym: Shift turns 1 into `exclam`, and a
key pressed with Shift down and let go after Shift is up would otherwise
never be released.
"""

# The shifted keysym -> the key's own keysym, so Shift+1 is still the 1 key.
SHIFTED = {
    'exclam': '1', 'at': '2', 'numbersign': '3', 'dollar': '4',
    'percent': '5', 'asciicircum': '6', 'ampersand': '7', 'asterisk': '8',
    'parenleft': '9', 'parenright': '0', 'asciitilde': 'grave',
    'underscore': 'minus', 'plus': 'equal', 'braceleft': 'bracketleft',
    'braceright': 'bracketright', 'colon': 'semicolon',
    'quotedbl': 'apostrophe', 'bar': 'backslash', 'less': 'comma',
    'greater': 'period', 'question': 'slash', 'ISO_Left_Tab': 'Tab',
    'KP_Enter': 'Return',
    # Punctuation can arrive as the character rather than its keysym name
    # (Tk's own generated events do), so the character counts as the key.
    '`': 'grave', '~': 'grave', '-': 'minus', '_': 'minus', '=': 'equal',
    '+': 'equal', '[': 'bracketleft', '{': 'bracketleft',
    ']': 'bracketright', '}': 'bracketright', ';': 'semicolon',
    ':': 'semicolon', "'": 'apostrophe', '"': 'apostrophe',
    '\\': 'backslash', '|': 'backslash', ',': 'comma', '.': 'period',
    '/': 'slash', '!': '1', '@': '2', '#': '3', '$': '4', '%': '5',
    '^': '6', '&': '7', '*': '8',
}


def normalize(keysym):
    """-> the unshifted keysym: 'A' and 'exclam' become 'a' and '1'."""
    keysym = SHIFTED.get(keysym, keysym)
    return keysym.lower() if len(keysym) == 1 else keysym


# Keysym -> panel label, for what every product has.
COMMON = {
    **{'F%d' % (i + 1): str(i + 1) for i in range(8)},
    **{str(i + 1): str(i + 9) for i in range(8)},
    'Up': 'UP', 'Down': 'DOWN', 'Left': 'LEFT', 'Right': 'RIGHT',
    'Return': 'YES', 'BackSpace': 'NO',
    'space': 'PLAY', 'End': 'STOP', 'Home': 'RECORD',
    # The Monomachine's data-page keys. A Digi has one PAGE key, forward only.
    'Prior': 'PAGE', 'Next': 'PAGE',
    # The Monomachine's menu row U I O P, with O per product.
    'u': 'TEMPO', 'i': 'GLOBAL', 'p': 'PTN',
    'm': 'SONG',                     # the MM's SONG ENABLE
    'grave': 'BANK',                 # the MM's bank group
}

# The parameter pages sit right-aligned on the home row after the knobs, so
# FLTR, AMP and LFO are the same keys on both products.
DIGITAKT = {
    **COMMON,
    'q': 'TRK',
    'o': 'SAMPLE',
    'j': 'TRIG', 'k': 'SRC', 'l': 'FLTR', 'semicolon': 'AMP',
    'apostrophe': 'LFO',
}

DIGITONE = {
    **COMMON,
    'q': 'T1', 'w': 'T2', 'e': 'T3', 'r': 'T4', 't': 'MIDI',
    'o': 'VOICE',
    'b': 'KEYBOARD',
    'h': 'TRIG', 'j': 'SYN1', 'k': 'SYN2', 'l': 'FLTR', 'semicolon': 'AMP',
    'apostrophe': 'LFO',
}

# Keysym -> encoder label. Held, then turned with TURN or PRESS_TURN.
KNOBS = {
    'a': 'A', 's': 'B', 'd': 'C', 'f': 'D',
    'z': 'E', 'x': 'F', 'c': 'G', 'v': 'H',
    'g': 'LEVEL/DATA',
}

# The Model:Cycles and Model:Samples. They have no arrows, YES/NO or BANK:
# BACK is on Backspace, and Enter is LEVEL/DATA's push switch, the only knob
# that has one. The six pads are the bottom row's B to /, under the knobs as on
# the panel, and play at the default velocity. TRACK is Tab, to hold while a
# pad is hit; RETRIG takes the menu row's per-product O.
MODEL = {
    **{key: label for key, label in COMMON.items()
       if label.isdigit() or label in ('PLAY', 'STOP', 'RECORD', 'PAGE',
                                       'TEMPO')},
    'BackSpace': 'BACK', 'Return': 'LEVEL/DATA',
    'i': 'SETTINGS', 'o': 'RETRIG', 'p': 'PATTERN',
    'Tab': 'TRACK',
    'apostrophe': 'LFO',
    **{key: 'T%d' % (i + 1) for i, key in enumerate(
        ('b', 'n', 'm', 'comma', 'period', 'slash'))},
}
# The three keys above LFO, by product, right-aligned as the Digis' pages are.
MODEL_PAGE_KEYS = ('k', 'l', 'semicolon')
MODEL_CYCLES = {**MODEL,
                **dict(zip(MODEL_PAGE_KEYS, ('MACHINE', 'PUNCH', 'GATE')))}
MODEL_SAMPLES = {**MODEL,
                 **dict(zip(MODEL_PAGE_KEYS, ('WAVE', 'LOOP', 'FLIP')))}
MODEL_KEYS = {'Model:Cycles': MODEL_CYCLES, 'Model:Samples': MODEL_SAMPLES}
MODEL_FUNC = 'FUNCTION'
# The parameter knobs' keys, in the panel's order: three rows of four.
MODEL_KNOB_GRID = 'qwerasdfzxcv'


def model_knobs(order):
    """-> keysym -> encoder label for a Model whose twelve parameter knobs
    are `order`, left to right and top to bottom. VOLUME is above
    LEVEL/DATA's G, and REVERB SIZE above DELAY TIME beside them."""
    return {**dict(zip(MODEL_KNOB_GRID, order)),
            'g': 'LEVEL/DATA', 't': 'VOLUME',
            'y': 'REVERB SIZE', 'h': 'DELAY TIME'}

TURN = {'minus': -1, 'equal': 1}
PRESS_TURN = {'bracketleft': -1, 'bracketright': 1}
FUNC_KEYS = ('Control_L', 'Control_R')
SHIFT_KEYS = ('Shift_L', 'Shift_R')
RELEASE_ALL = 'Delete'

RELEASE_GRACE_MS = 50
# The panel link holds a button change back until the previous one has dwelt
# (emu/gui.py PANEL_DWELL_MS) but sends encoder detents at once. A press-turn
# waits this long after pressing the push switch, so its first detent does
# not overtake the push.
PRESS_TURN_DELAY_MS = 60

SHIFT_MASK = 0x0001
CONTROL_MASK = 0x0004


class Keyboard:
    """Turns key events into panel presses, releases and turns.

    `panel` supplies codes (label -> button code), enc_codes (label ->
    encoder code), press(code, latch), release(code, force), turn(code,
    step), release_everything(), after(ms, fn) -> id and after_cancel(id).
    `keys` is keysym -> panel label, `knobs` keysym -> encoder label and
    `func` the label of the key Ctrl holds.
    """

    def __init__(self, panel, keys, knobs=KNOBS, func='FUNC'):
        self.panel = panel
        self.keys = keys
        self.knobs = knobs
        self.func_label = func
        self.held = {}          # keycode -> ('button', code) / ('knob', label)
        self.pending = {}       # keycode -> after id of a deferred release
        self.latched = set()    # button codes latched by keyboard Shift
        self.func = None        # FUNC's code while Ctrl holds it
        self.knob = None        # the label of the knob being held
        self.turned = False     # the held knob was turned
        self.push = None        # the push switch code a press-turn holds

    # ---------------------------------------------------------------- events
    def key(self, down, keysym, keycode, state):
        """Handle one key event. -> True if the panel used it."""
        self._sync_func(keysym, down, state)
        if keysym in FUNC_KEYS:
            return True
        if keysym in SHIFT_KEYS:
            if not down:
                self._release_latched()
            return True
        if down:
            after_id = self.pending.pop(keycode, None)
            if after_id is not None:     # auto-repeat: the key never went up
                self.panel.after_cancel(after_id)
                return self._down(keycode, normalize(keysym), state,
                                  repeat=True)
            return self._down(keycode, normalize(keysym), state)
        if keycode in self.held or keycode in self.pending:
            if keycode not in self.pending:
                self.pending[keycode] = self.panel.after(
                    RELEASE_GRACE_MS, lambda k=keycode: self._up(k))
            return True
        return False

    def focus_lost(self):
        """Let go of whatever the keyboard holds: its key-ups will not come."""
        for after_id in self.pending.values():
            self.panel.after_cancel(after_id)
        self.pending.clear()
        self.knob = None                 # a knob let go this way is no click
        self._end_press_turn()
        for keycode in list(self.held):
            self._up(keycode)
        self._release_latched()
        if self.func is not None:
            self.panel.release(self.func, force=True)
            self.func = None

    # ----------------------------------------------------------------- inner
    def _code(self, label):
        return self.panel.codes.get(label)

    def _sync_func(self, keysym, down, state):
        """Ctrl is FUNC. Every event carries the modifier state, so a lost
        Ctrl key-up is caught by the next key."""
        if keysym in FUNC_KEYS:
            ctrl = down
        else:
            ctrl = bool(state & CONTROL_MASK)
        if ctrl and self.func is None:
            code = self._code(self.func_label)
            if code is not None:
                self.func = code
                self.panel.press(code, latch=bool(state & SHIFT_MASK))
                if state & SHIFT_MASK:
                    self.latched.add(code)
        elif not ctrl and self.func is not None:
            if self.func not in self.latched:
                self.panel.release(self.func, force=True)
            self.func = None

    def _down(self, keycode, key, state, repeat=False):
        if key == RELEASE_ALL:
            self.focus_lost()
            self.panel.release_everything()
            return True
        step = TURN.get(key) or PRESS_TURN.get(key)
        if step is not None:
            self.held.setdefault(keycode, ('turn', key))
            self._turn(step, key in PRESS_TURN)
            return True
        if repeat or keycode in self.held:
            return True                  # a held key: nothing new to do
        if key in self.knobs:
            self.held[keycode] = ('knob', self.knobs[key])
            self._end_press_turn()
            self.knob, self.turned = self.knobs[key], False
            return True
        label = self.keys.get(key)
        code = self._code(label) if label else None
        if code is None:
            return label is not None
        self.held[keycode] = ('button', code)
        latch = bool(state & SHIFT_MASK)
        if latch:
            self.latched.add(code)
        self.panel.press(code, latch=latch)
        return True

    def _up(self, keycode):
        self.pending.pop(keycode, None)
        kind, what = self.held.pop(keycode, (None, None))
        if kind == 'button':
            if what not in self.latched:
                self.panel.release(what, force=True)
        elif kind == 'turn':
            if what in PRESS_TURN:
                self._end_press_turn()
        elif kind == 'knob' and what == self.knob:
            push = self._code(self.knob)
            if not self.turned and push is not None:
                self.panel.press(push, latch=False)       # a click
                self.panel.release(push, force=True)
            self._end_press_turn()
            self.knob = None

    def _turn(self, step, press):
        if self.knob is None:
            return
        code = self.panel.enc_codes.get(self.knob)
        if code is None:
            return
        self.turned = True
        push = self._code(self.knob) if press else None
        if push is not None and self.push is None:
            self.push = push
            self.panel.press(push, latch=False)
            self.panel.after(PRESS_TURN_DELAY_MS,
                             lambda: self.panel.turn(code, step))
            return
        self.panel.turn(code, step)

    def _end_press_turn(self):
        if self.push is not None:
            self.panel.release(self.push, force=True)
            self.push = None

    def holding(self):
        """-> the button codes keys are physically holding down now."""
        codes = {what for kind, what in self.held.values() if kind == 'button'}
        if self.func is not None:
            codes.add(self.func)
        return codes
    def _release_latched(self):
        still_held = self.holding()
        for code in list(self.latched):
            self.latched.discard(code)
            if code not in still_held:
                self.panel.release(code, force=True)
