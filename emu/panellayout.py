"""Mk1 faceplate geometry shared by the window, artwork and browser panel.

Coordinates describe the physical controls; the app toolbar sits outside
the chassis. Each model uses the available width for its step keys.
"""
WIDTH, HEIGHT = 1000, 884
BODY = (20, 74, 960, 776)
SCALE = 3
# Deliberately larger than the hardware OLED, at an exact integer zoom.
SCREEN = (166, 183, 384, 192)
BEZEL = (161, 178, 394, 202)
MASTER_VOLUME = (94, 210, 32)
ENCODERS = {letter: (610 + col * 100, 210 + row * 115, 32)
            for row, letters in enumerate(('ABCD', 'EFGH'))
            for col, letter in enumerate(letters)}
ENCODERS['LEVEL/DATA'] = (94, 325, 32)
TRIG_LEGENDS = ('KICK', 'SNARE', 'TOM', 'CLAP', 'COWBELL', 'CLOSED HAT',
                'OPEN HAT', 'CYMBAL', 'MIDI A', 'MIDI B', 'MIDI C', 'MIDI D',
                'MIDI E', 'MIDI F', 'MIDI G', 'MIDI H')


def trig_positions(product):
    # The FM model reserves space for its four track keys. The sampler fills
    # that space with wider step caps, like its physical counterpart.
    pitch, width = (78, 60) if product == 'Digitone' else (95, 74)
    return {str(i + 1): (195 + (i % 8) * pitch,
                         646 + (i // 8) * 92, width, 60) for i in range(16)}


def buttons(product):
    """Firmware name -> (x, y, width, height, secondary legend, text tint)."""
    tone = product == 'Digitone'
    out = {
        'FUNC': (66, 452, 74, 46, None, None),
        'PTN': (66, 654, 74, 46, 'Metronome', None),
        'BANK': (66, 746, 74, 46, 'Mute Mode', None),
        'SONG': (195, 452, 48, 46, 'Imp/Exp', None),
        'GLOBAL': (267, 452, 48, 46, 'Save Proj', None),
        'TEMPO': (411, 452, 48, 46, 'Tap Tempo', None),
        'RECORD': (195, 524, 68, 46, 'Copy', None),
        'PLAY': (286, 524, 68, 46, 'Clear', None),
        'STOP': (377, 524, 68, 46, 'Paste', None),
        'YES': (536, 504, 48, 46, 'Save Ptn', None),
        'NO': (536, 574, 48, 46, 'Reload Ptn', None),
        'UP': (674, 504, 48, 46, 'Note/Oct+' if tone else 'Rtrg/Oct+', None),
        'LEFT': (604, 574, 48, 46, 'µTime−', None),
        'DOWN': (674, 574, 48, 46, 'Note/Oct−' if tone else 'Rtrg/Oct−', None),
        'RIGHT': (744, 574, 48, 46, 'µTime+', None),
        'PAGE': (876, 574, 60, 46, 'Fill/Scale', None),
    }
    if tone:
        out.update({
            'MIDI': (66, 524, 74, 46, 'MIDI Config', None),
            'KEYBOARD': (80, 587, 46, 40, 'Add Notes/Arp', None),
            'VOICE': (339, 452, 48, 46, 'Unison', None),
            'T1': (834, 651, 52, 52, 'Mute', None),
            'T2': (906, 651, 52, 52, 'Mute', None),
            'T3': (834, 743, 52, 52, 'Mute', None),
            'T4': (906, 743, 52, 52, 'Mute', None),
        })
        pages = ('TRIG', 'SYN1', 'SYN2', 'FLTR', 'AMP', 'LFO')
        legends = ('Setup', 'Arp Menu', 'Chorus', 'Delay', 'Reverb', 'Master')
        pitch = 74
    else:
        out.update({'TRK': (66, 524, 74, 46, 'Chromatic', None),
                    'SAMPLE': (339, 452, 48, 46, 'Direct', None)})
        pages = ('TRIG', 'SRC', 'FLTR', 'AMP', 'LFO')
        legends = ('Quantize', 'Assign', 'Delay', 'Reverb', 'Master')
        pitch = 92
    out.update({name: (536 + i * pitch, 428, 48, 46, legends[i], None)
                for i, name in enumerate(pages)})
    for label, (cx, cy, r) in ENCODERS.items():
        width = 84 if label == 'LEVEL/DATA' else 40
        out[label] = (cx - width / 2, cy + r + 12, width, 16,
                      'Sound Browser' if label == 'LEVEL/DATA' else None, None)
    out.update({label: (*rect, None if tone else TRIG_LEGENDS[int(label) - 1], None)
                for label, rect in trig_positions(product).items()})
    return out


def page_lights(spec, count=4):
    """The four LED lenses and printed 1:4 .. 4:4 legends above PAGE."""
    x, y, w, _h = spec[:4]
    return [(x + w / 2 + (i - (count - 1) / 2) * 18, y - 30)
            for i in range(count)]


def face(product, label):
    if label == 'FUNC':
        return '#1ba3af' if product == 'Digitone' else '#d68d49'
    if product == 'Digitone':
        if label.isdigit() and int(label) >= 9:
            return '#d8d5cc'
        return {'T1': '#ba3932', 'T2': '#c9ac38', 'T3': '#367c43',
                'T4': '#97509c'}.get(label, '#292a2c')
    return '#292a2c'


def accent(product):
    return '#43b8bc' if product == 'Digitone' else '#e7bd4c'
