"""The Model:Cycles and Model:Samples window.

The machinery is emu/dtpanel.py's -- the emulator thread, input, key LEDs,
audio controls, MIDI, LOAD SAMPLES, session save and shutdown -- and this
module places the Models' controls: VOLUME and LEVEL/DATA at the top left,
fourteen parameter knobs in two rows right of the screen, a row of keys
under the screen, the transport, the six velocity pads and the sixteen trig
keys in one row.

The Models scan their own panel (emu/modelboard.py), so a key is not a wire
position on a panel MCU: devices/model-cycles.toml and model-samples.toml say
which scan column and bit each key is, which ADC channel each pad, which LED
each lights and what each is called. Both products have the same panel; the
Model:Cycles' MACHINE, PUNCH and GATE keys are the Model:Samples' WAVE, LOOP
and FLIP, and five of their knobs differ.

A pad's velocity is where it is clicked: the top edge is 127, the bottom 30.
PITCH pushes: its switch is the key under the knob (on the Model:Samples it
opens a folder or picks a sample in the sample browser).

VOLUME is an encoder the firmware reads, not the Digitakt's analog pot, so
this window has no software Master Volume: turning VOLUME sets the
firmware's own output level.

LOAD SAMPLES is on the Model:Samples only: its samples live on the +Drive's
ekFS volume, in the same format as the Digitakt's (emu/samples.py), and a
sample put in /incoming is in the sample browser after the rebuild.

    uv run python -m emu.mdpanel [snapshot] [--syx PATH]
                                 [--save-on-exit PATH] [--no-audio] [--app]
"""
import sys

from emu import config, dtpanel
from emu import device as devices
from emu.dtpanel import AMBER, PLAY_C, REC_C, DigitaktPanel

PANEL_W, PANEL_H = 1180, 790

# The key row under the screen, by the Model:Samples' names; the Model:Cycles'
# MACHINE, PUNCH and GATE take WAVE's, LOOP's and FLIP's places.
KEY_ROW = ('FUNCTION', 'PATTERN', 'TRACK', 'PAGE', 'TEMPO', 'SETTINGS',
           'BACK', 'RETRIG', 'WAVE', 'LOOP', 'FLIP', 'LFO')
SAME_PLACE = {'MACHINE': 'WAVE', 'PUNCH': 'LOOP', 'GATE': 'FLIP'}

# label -> (x, y, w, h, secondary caption, tint)
BUTTONS = {label: (30 + i * 92, 400, 84, 40, None,
                   AMBER if label == 'FUNCTION' else None)
           for i, label in enumerate(KEY_ROW)}
for _new, _old in SAME_PLACE.items():
    BUTTONS[_new] = BUTTONS[_old]
BUTTONS.update({
    'STOP': (30, 470, 80, 46, None, None),
    'PLAY': (120, 470, 80, 46, None, PLAY_C),
    'RECORD': (210, 470, 80, 46, None, REC_C),
})
# The six pads, big enough to aim a velocity at.
PADS = tuple('T%d' % (i + 1) for i in range(6))
PAD_Y, PAD_H = 460, 120
for _i, _label in enumerate(PADS):
    BUTTONS[_label] = (330 + _i * 132, PAD_Y, 122, PAD_H, None, None)
# Sixteen trig keys in one row.
for _i in range(16):
    BUTTONS[str(_i + 1)] = (30 + _i * 70, 640, 62, 64, None, None)

# The knobs right of the screen, two rows of seven, top row first: the
# voice, then the effects and the track's swing and chance.
KNOBS = {
    'Model:Cycles': ('PITCH', 'DECAY', 'COLOR', 'SHAPE', 'SWEEP', 'CONTOUR',
                     'LFO SPEED',
                     'VOL+DIST', 'SWING', 'CHANCE', 'DELAY SEND',
                     'DELAY TIME', 'REVERB SEND', 'REVERB SIZE'),
    'Model:Samples': ('PITCH', 'DECAY', 'SMPL START', 'SMPL LENGTH', 'CUTOFF',
                      'RESONANCE', 'LFO SPEED',
                      'VOL+DIST', 'SWING/NUDGE', 'CHANCE/COND', 'DELAY SEND',
                      'DELAY TIME', 'REVERB SEND', 'REVERB SIZE'),
}
KNOB_X, KNOB_DX, KNOB_R = 738, 66, 22
KNOB_Y = (140, 266)


def encoders_for(order):
    """-> {label: (x, y, r)}: VOLUME and LEVEL/DATA at the top left, `order`
    in rows of seven right of the screen."""
    out = {'VOLUME': dtpanel.MASTER_VOLUME, 'LEVEL/DATA': (90, 300, 28)}
    for i, label in enumerate(order):
        out[label] = (KNOB_X + (i % 7) * KNOB_DX, KNOB_Y[i // 7], KNOB_R)
    return out


def knob_label_lines(label):
    """-> the lines a knob's name is drawn in: two words on two lines, and a
    name with a slash broken after it -- seven knobs to a row leave about
    64 px for a name."""
    return label.replace(' ', '\n').replace('/', '/\n').split('\n')


def layout(name):
    """-> (buttons, encoders) for the Model called `name` (a device file's
    [device] name): its knobs in KNOBS order, PITCH's push switch under its
    knob."""
    encoders = encoders_for(KNOBS.get(name, KNOBS['Model:Samples']))
    x, y, r = encoders['PITCH']
    buttons = dict(BUTTONS, PITCH=(x - 28, y + r + 6, 56, 18, None, None))
    return buttons, encoders


def _product(syx):
    """-> the device the firmware is, or None: decided before the window is
    drawn, since LOAD SAMPLES is drawn with it."""
    try:
        return devices.identify(config.firmware(syx))[0]
    except BaseException:                          # noqa: BLE001
        return None


class ModelPanel(DigitaktPanel):
    PRODUCT = 'Model'
    TITLE = 'digiemu — Elektron Model emulator (unofficial)'
    SUBTITLE = ('Elektron Model emulator · unofficial, not affiliated with '
                'Elektron')
    BUTTONS = BUTTONS
    ENCODERS = {}
    SAMPLES = False
    PANEL_W, PANEL_H = PANEL_W, PANEL_H
    SCREEN_LEGEND = 'Model'

    def __init__(self, snapshot, syx=None, **kw):
        self.text_fill = {}
        dev = _product(syx)
        name = getattr(dev, 'name', None)
        if name:
            self.PRODUCT = name
            self.TITLE = 'digiemu — %s emulator (unofficial)' % name
            self.SUBTITLE = ('%s emulator · unofficial, not affiliated with '
                             'Elektron' % name)
            self.SAMPLES = getattr(dev, 'short', None) == 'ms'
        self.BUTTONS, self.ENCODERS = layout(name)
        super().__init__(snapshot, syx=syx, **kw)

    def _draw_master_volume(self):
        # VOLUME is the firmware's own encoder (see the module docstring).
        pass

    def _draw_chrome(self):
        """The Models use drawn controls; the mk1 artwork has no Model skin."""
        c = self.canvas
        self.key_labels = {}
        self.extra_items = {}
        self.enc_push_items = {}
        sx, sy = self.SCREEN_X, self.SCREEN_Y
        c.create_text(24, 21, text='digiemu', fill=dtpanel.TEXT,
                      font=('Helvetica', -20, 'bold'), anchor='w')
        c.create_text(24, 48, text=self.SUBTITLE, fill=dtpanel.DIM,
                      font=('Helvetica', -10), anchor='w')
        c.create_image(sx, sy, image=self.big, anchor='nw')
        self.err_box = c.create_rectangle(
            sx, sy, sx + dtpanel.W * dtpanel.SCALE,
            sy + dtpanel.H * dtpanel.SCALE,
            fill='#080b04', outline='', state='hidden')
        self.err_text = c.create_text(
            sx + 14, sy + 14, text='', fill=dtpanel.ERR,
            font=('Helvetica', -12), anchor='nw',
            width=dtpanel.W * dtpanel.SCALE - 28, state='hidden')
        self.status = c.create_text(24, 768, text='Starting…',
                                    fill=dtpanel.DIM, anchor='w', width=900)
        clear = c.create_text(self.PANEL_W - 24, 768,
                              text='clear latched', fill=dtpanel.DIM,
                              anchor='e')
        c.tag_bind(clear, '<Button-1>', lambda _e: self.clear_latched())
        c.bind('<ButtonRelease-1>', self._release_pointer)
        self._draw_audio_controls()

    def _build_controls(self):
        c = self.canvas
        for label, code in sorted(self.codes.items()):
            spec = self.BUTTONS.get(label)
            if spec is None:
                continue
            x, y, w, h, sub, tint = spec
            rect = self._rr(x, y, w, h, 6, fill=dtpanel.FACE,
                            outline=dtpanel.EDGE)
            txt = c.create_text(x + w / 2, y + h / 2,
                                text=label[:14], fill=tint or dtpanel.TEXT,
                                font=('Helvetica', -10, 'bold'))
            self.items[code] = (rect, txt)
            self.text_fill[code] = tint or dtpanel.TEXT
            self.key_labels[code] = label
            self.layout.append({'k': 'b', 'code': code, 'label': label,
                                'x': x, 'y': y, 'w': w, 'h': h,
                                'sub': sub, 'tint': tint})
            for item in (rect, txt):
                self._bind_button(item, code)
        dev = getattr(self.emu, 'device', None)
        self.led_of = {code: led for led, code in
                       (getattr(dev, 'leds', None) or {}).items()}
        self._led_version = -1
        for label, code in self.enc_codes.items():
            if label not in self.ENCODERS:
                continue
            x, y, r = self.ENCODERS[label]
            ring = c.create_oval(x-r, y-r, x+r, y+r,
                                 fill=dtpanel.FACE, outline=dtpanel.EDGE,
                                 width=2)
            mark = c.create_line(x, y-r+6, x, y-4,
                                 fill=dtpanel.TEXT, width=3)
            self.enc_items[code] = [ring, mark, 0.0]
            self.layout.append({'k': 'e', 'code': code, 'label': label,
                                'x': x, 'y': y, 'r': r})
            for item in (ring, mark):
                try:
                    c.tag_bind(item, '<MouseWheel>',
                               lambda e, k=code: self.turn(k, 1 if e.delta > 0 else -1, e))
                except dtpanel.tk.TclError:
                    pass
                c.tag_bind(item, '<Button-4>', lambda e, k=code: self.turn(k, 1, e))
                c.tag_bind(item, '<Button-5>', lambda e, k=code: self.turn(k, -1, e))
                c.tag_bind(item, '<ButtonPress-1>',
                           lambda e, k=code: self._drag_start(k, e))
                c.tag_bind(item, '<B1-Motion>', lambda e, k=code: self._drag(k, e))
                c.tag_bind(item, '<ButtonRelease-1>',
                           lambda e, k=code, name=label: self._drag_end(k, name, e))
        self.remote_layout = self._remote_layout()
        # The knobs have no push switches to carry their names (PITCH's
        # switch is a key under its knob and carries PITCH), so the names are
        # written under each knob (knob_label_lines).
        c = self.canvas
        for label, (x, y, r) in self.ENCODERS.items():
            if label in self.enc_codes and label not in self.codes:
                c.create_text(x, y + r + 4,
                              text='\n'.join(knob_label_lines(label)),
                              fill=dtpanel.DIM, font=('Helvetica', 8),
                              justify='center', anchor='n')

    def _paint(self, code):
        item = self.items.get(code)
        if item is None:
            return
        rect, txt = item
        led = self._led_rgb(self.led_of.get(code))
        pressed = code in self.held or code in self.latched
        self.canvas.itemconfigure(rect, fill=dtpanel._hex(led) if led else
                                  (self.LEGEND if pressed else dtpanel.FACE))
        self.canvas.itemconfigure(txt, fill=dtpanel.BG if (led or pressed)
                                  else self.text_fill[code])

    def _remote_layout(self):
        leds = sorted(set(self.led_of.values()))
        index = {led: i for i, led in enumerate(leds)}
        controls = []
        for item in self.layout:
            item = dict(item)
            if item['k'] == 'b':
                item['led'] = index.get(self.led_of.get(item['code']))
            elif item['k'] == 'e':
                item['push'] = self.codes.get(item['label'])
            controls.append(item)
        sx, sy = self.SCREEN_X, self.SCREEN_Y
        return {'product': self.PRODUCT, 'screen_legend': self.SCREEN_LEGEND,
                'controls': controls, 'leds': leds, 'skin': None,
                'screen': {'x': sx, 'y': sy,
                           'w': dtpanel.W * dtpanel.SCALE,
                           'h': dtpanel.H * dtpanel.SCALE},
                'canvas': [self.PANEL_W, self.PANEL_H], 'master': None,
                'bounds': [0, 74, self.PANEL_W, self.PANEL_H - 74]}

    def press(self, code, event=None):
        """A pad press carries its velocity: where on the pad it was."""
        dev = getattr(self.emu, 'device', None)
        if event is None or code not in getattr(dev, 'pads', {}):
            return super().press(code, event)
        y = self.canvas.canvasy(event.y)
        frac = min(1.0, max(0.0, (y - PAD_Y) / float(PAD_H)))
        velocity = int(round(127 - frac * 97))
        self.held.add(code)
        self.emu.inbox.append(('press', code, velocity))
        self._paint(code)


def main(argv):
    """Run a Model's panel until its window closes. -> the process exit
    code, as emu.dtpanel.main documents."""
    return dtpanel.run(argv, ModelPanel, prog='python -m emu.mdpanel',
                       description='The Model:Cycles and Model:Samples '
                                   'front panel.')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
