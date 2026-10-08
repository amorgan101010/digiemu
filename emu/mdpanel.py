"""The Model:Cycles and Model:Samples window.

The machinery is emu/dtpanel.py's -- the emulator thread, input, key LEDs,
audio controls, MIDI, LOAD SAMPLES, session save and shutdown -- and this
module places the Models' controls: LEVEL/DATA at left, VOLUME at right,
fourteen parameter knobs in three rows right of the screen, menu keys
and transport beneath it, six velocity pads and sixteen trig keys in one row.

The Models scan their own panel (emu/modelboard.py), so a key is not a wire
position on a panel MCU: devices/model-cycles.toml and model-samples.toml say
which scan column and bit each key is, which ADC channel each pad, which LED
each lights and what each is called. Both products have the same panel; the
Model:Cycles' MACHINE, PUNCH and GATE keys are the Model:Samples' WAVE, LOOP
and FLIP, and five of their knobs differ.

A pad's velocity is where it is clicked: the top edge is 127, the bottom 30.
PITCH pushes: its switch is the key under the knob (on the Model:Samples it
opens a folder or picks a sample in the sample browser).

VOLUME is an encoder the firmware reads, not the Digitakt's analog pot.
Turning it sets the firmware's codec output level, which the host audio
output follows.

LOAD SAMPLES is on the Model:Samples only: its samples live on the +Drive's
ekFS volume, in the same format as the Digitakt's (emu/samples.py), and a
sample put in /incoming is in the sample browser after the rebuild.

    uv run python -m emu.mdpanel [snapshot] [--syx PATH]
                                 [--save-on-exit PATH] [--no-audio] [--app]
"""
import sys

from emu import config, dtpanel
from emu import device as devices
from emu.dtpanel import DigitaktPanel

PANEL_W, PANEL_H = 1380, 884
SCREEN = (150, 120, 384, 192)
BODY = (20, 74, 1340, 776)
PADS = tuple('T%d' % (i + 1) for i in range(6))
PAD_Y, PAD_H = 646, 76
BUTTONS = {
    'BACK': (64, 362, 58, 48, 'PAD MENU', None),
    'SETTINGS': (242, 362, 58, 48, 'TEMP SAVE', None),
    'TEMPO': (420, 362, 58, 48, 'TAP BPM', None),
    'RECORD': (64, 458, 58, 48, 'COPY', None),
    'PLAY': (242, 458, 58, 48, 'CLEAR', None),
    'STOP': (420, 458, 58, 48, 'PASTE', None),
    'FUNCTION': (64, 554, 86, 48, None, None),
    'RETRIG': (392, 554, 86, 48, 'RETRIG MENU', None),
    'PATTERN': (64, 658, 86, 48, 'RELOAD PTN', None),
    'TRACK': (392, 658, 86, 48, 'CTRL ALL / TRK MENU', None),
    'PAGE': (1236, 658, 86, 48, 'FILL / SCALE', None),
}
for i, (label, sub) in enumerate((('WAVE', 'SWING ALL'), ('LOOP', 'QUANTIZE'),
                                 ('FLIP', 'CLICK'), ('LFO', 'LFO SETUP'))):
    BUTTONS[label] = (568, 128 + i * 132, 56, 48, sub, None)
for new, old in {'MACHINE': 'WAVE', 'PUNCH': 'LOOP', 'GATE': 'FLIP'}.items():
    BUTTONS[new] = BUTTONS[old]
for i, label in enumerate(PADS):
    BUTTONS[label] = (546 + i * 112, PAD_Y, 84, PAD_H,
                       'BANK %s / MUTE' % chr(65 + i), None)
for i in range(16):
    BUTTONS[str(i + 1)] = (64 + i * 80, 778, 58, 48, None, None)
KNOBS = {
    'Model:Cycles': ('PITCH', 'DECAY', 'COLOR', 'SHAPE', 'SWEEP', 'CONTOUR',
                     'DELAY SEND', 'REVERB SEND', 'LFO SPEED', 'VOL+DIST',
                     'SWING', 'CHANCE'),
    'Model:Samples': ('PITCH', 'DECAY', 'SMPL START', 'SMPL LENGTH', 'CUTOFF',
                      'RESONANCE', 'DELAY SEND', 'REVERB SEND', 'LFO SPEED',
                      'VOL+DIST', 'SWING/NUDGE', 'CHANCE/COND'),
}


def encoders_for(order):
    out = {'LEVEL/DATA': (92, 158, 32), 'VOLUME': (1278, 158, 32),
           'REVERB SIZE': (1278, 350, 32), 'DELAY TIME': (1278, 542, 32)}
    out.update({label: (710 + i % 4 * 140, 158 + i // 4 * 192, 32)
                for i, label in enumerate(order)})
    return out


def knob_label_lines(label):
    return [label]


def layout(name):
    name = name if name in KNOBS else 'Model:Samples'
    encoders = encoders_for(KNOBS[name])
    buttons = dict(BUTTONS)
    unused = ('WAVE', 'LOOP', 'FLIP') if name == 'Model:Cycles' else ('MACHINE', 'PUNCH', 'GATE')
    for label in unused:
        del buttons[label]
    if name == 'Model:Cycles':
        buttons['MACHINE'] = (*buttons['MACHINE'][:4], 'PRESET MENU', None)
    x, y, r = encoders['PITCH']
    buttons['PITCH'] = (x - 28, y + r + 12, 56, 18, None, None)
    return buttons, encoders


def _product(syx):
    """-> the device the firmware is, or None: decided before the window is
    drawn, since LOAD SAMPLES is drawn with it."""
    try:
        return devices.identify(config.firmware(syx))[0]
    except BaseException:                          # noqa: BLE001
        return None


class ModelPanel(DigitaktPanel):
    DRAG_FLUSH_MS = 16
    PRODUCT = 'Model:Samples'
    TITLE = 'digiemu — Elektron Model emulator (unofficial)'
    SUBTITLE = ('Elektron Model emulator · unofficial, not affiliated with '
                'Elektron')
    BUTTONS = BUTTONS
    ENCODERS = {}
    SAMPLES = False
    PANEL_W, PANEL_H = PANEL_W, PANEL_H
    SCREEN_LEGEND = 'Model'
    SCREEN_X, SCREEN_Y = SCREEN[:2]
    LEGEND = '#ef726b'

    def __init__(self, snapshot, syx=None, **kw):
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

    def _remote_layout(self):
        result = super()._remote_layout()
        result.update(canvas=[self.PANEL_W, self.PANEL_H], master=None,
                      bounds=list(BODY))
        return result

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
