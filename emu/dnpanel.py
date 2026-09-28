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
pushes: click the knob, or drag to turn it.

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
from emu.dtpanel import DigitaktPanel

CYAN = '#28bdc7'

# Shared geometry source; this model leaves room for its four track keys.
BUTTONS = dtpanel.panellayout.buttons('Digitone')
ENCODERS = dict(dtpanel.ENCODERS)


class DigitonePanel(DigitaktPanel):
    PRODUCT = 'Digitone'
    TITLE = 'digiemu — FM'
    SUBTITLE = 'FM synthesizer · mk1 emulator'
    BUTTONS = BUTTONS
    ENCODERS = ENCODERS
    SAMPLES = False
    KEYS = panelkeys.DIGITONE
    REMOTE_PORT = 8796
    LEGEND = CYAN
    SCREEN_LEGEND = 'Polyphonic Digital Synthesizer'


def main(argv):
    """Run the Digitone panel until its window closes. -> the process exit
    code, as emu.dtpanel.main documents (SAMPLES_ADDED never happens here)."""
    return dtpanel.run(argv, DigitonePanel, prog='python -m emu.dnpanel',
                       description='The Digitone mk1 front panel.')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
