"""A Digitone-shaped front panel: the Digitone (mk1) window.

The machinery is emu/dtpanel.py's -- the emulator thread, multitouch input,
key LEDs, audio controls and Master Volume, session save and shutdown -- and
this module only places a different instrument's keys, on the Digitakt
window's plan: Master Volume and LEVEL/DATA at the top left, the OLED, the
eight encoders in two rows, one row of keys under them (FUNC, the menus, the
six parameter pages TRIG, SYN1, SYN2, FLTR, AMP, LFO, and PAGE), the four
track keys T1..T4 and MIDI down the left, transport, confirm and cursor keys,
and sixteen trig keys.

Controls are placed by the measured names in devices/digitone.toml, not by
the firmware's panel-test tables: the Digitone builds those at run time, and
on this product (as on the Digitakt) they label the test screen, not the keys
(see the device file for how each code was identified). Every encoder
pushes: each knob's name sits on its push switch below it.

No LOAD SAMPLES: the Digitone has no sample engine and its +Drive no sample
volume.

Sound: the Digitone's FM voices are rendered by its second CPU, which the
firmware calls the DSP. emu/dsplink.py runs that CPU's own code on a second
engine, on its own thread while audio is live, and the main OS mixes its
voices with the effects and plays them as on the Digitakt.

    uv run python -m emu.dnpanel [snapshot] [--syx PATH]
                                 [--save-on-exit PATH] [--no-audio] [--app]
"""
import sys

from emu import dtpanel, panelkeys
from emu.dtpanel import AMBER, PLAY_C, REC_C, DigitaktPanel

# Firmware-independent label -> (x, y, w, h, secondary caption, tint): the
# labels are devices/digitone.toml's [panel.labels].
BUTTONS = {
    # One row under the screen and encoders: FUNC, then eleven keys, 84 wide
    # at a 92 pitch -- the four menus, the six parameter pages, and PAGE at
    # the far right with the pattern-page LEDs above it.
    'FUNC': (30, 400, 100, 40, None, AMBER),
    'SONG': (140, 400, 84, 40, None, None),
    'GLOBAL': (232, 400, 84, 40, None, None),
    'VOICE': (324, 400, 84, 40, None, None),
    'TEMPO': (416, 400, 84, 40, None, None),
    'TRIG': (508, 400, 84, 40, None, None),
    'SYN1': (600, 400, 84, 40, None, None),
    'SYN2': (692, 400, 84, 40, None, None),
    'FLTR': (784, 400, 84, 40, None, None),
    'AMP': (876, 400, 84, 40, None, None),
    'LFO': (968, 400, 84, 40, None, None),
    'PAGE': (1060, 400, 84, 40, None, None),
    # left column, under FUNC: the four tracks, then MIDI
    'T1': (30, 460, 100, 40, None, None),
    'T2': (30, 510, 100, 40, None, None),
    'T3': (30, 560, 100, 40, None, None),
    'T4': (30, 610, 100, 40, None, None),
    'MIDI': (30, 668, 100, 40, None, None),
    # transport, then BANK and PTN beside it; KEYBOARD below
    'STOP': (150, 460, 76, 46, None, None),
    'PLAY': (236, 460, 76, 46, None, PLAY_C),
    'RECORD': (322, 460, 76, 46, None, REC_C),
    'BANK': (412, 460, 80, 46, None, None),
    'PTN': (500, 460, 80, 46, None, None),
    'KEYBOARD': (150, 516, 120, 40, 'Keyboard Setup', None),
    # confirm, stacked, and the cursor cross over trig keys 6..8: the
    # Digitakt window's places
    'YES': dtpanel.BUTTONS['YES'][:4] + (None, None),
    'NO': dtpanel.BUTTONS['NO'][:4] + (None, None),
    'UP': dtpanel.BUTTONS['UP'],
    'LEFT': dtpanel.BUTTONS['LEFT'],
    'DOWN': dtpanel.BUTTONS['DOWN'],
    'RIGHT': dtpanel.BUTTONS['RIGHT'],
    # Every encoder pushes (46..54): the push switch carries the knob's
    # name, under the knob as on the Digitakt.
    **{k: dtpanel.BUTTONS[k] for k in 'ABCDEFGH'},
    'LEVEL/DATA': dtpanel.BUTTONS['LEVEL/DATA'][:4] + (None, None),
}
# sixteen trig keys, two rows of eight
for _i in range(16):
    BUTTONS[str(_i + 1)] = (150 + (_i % 8) * 108, 598 + (_i // 8) * 82,
                            98, 72, None, None)

# Encoder label -> (centre x, centre y, radius): the Digitakt window's knobs,
# LEVEL/DATA at the top left under Master Volume.
ENCODERS = dict(dtpanel.ENCODERS)


class DigitonePanel(DigitaktPanel):
    PRODUCT = 'Digitone'
    TITLE = 'digiemu — Digitone mk1 emulator (unofficial)'
    SUBTITLE = ('Digitone mk1 emulator · unofficial, not affiliated with '
                'Elektron')
    BUTTONS = BUTTONS
    ENCODERS = ENCODERS
    SAMPLES = False
    KEYS = panelkeys.DIGITONE


def main(argv):
    """Run the Digitone panel until its window closes. -> the process exit
    code, as emu.dtpanel.main documents (SAMPLES_ADDED never happens here)."""
    return dtpanel.run(argv, DigitonePanel, prog='python -m emu.dnpanel',
                       description='The Digitone mk1 front panel.')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
