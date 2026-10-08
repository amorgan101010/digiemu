"""Build the original mk1 artwork, sprites and LED coverage masks.

    uv run --with pillow python tools/render_panel.py

Pillow and an installed Liberation Sans font are build tools only. The app
loads the resulting PNGs with Tk; no image library or font is needed at run
time. Artwork is procedural, with no photographs or firmware in the assets.
"""
import base64
import json
import math
from pathlib import Path
import random
import subprocess
import sys
import zlib

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from emu import panellayout as layout

S = 3
PAD, TW, TH = 10, 96, 84
# A step key's underline below the middle of its number, and the frame on
# the first step of each beat as insets (left and right, top, bottom).
UNDERLINE = 14
BEAT_FRAME = (8, 7, 11)
FONT = Path(subprocess.check_output(
    ['fc-match', '-f', '%{file}', 'Liberation Sans'], text=True).strip())
BOLD = FONT.with_name('LiberationSans-Bold.ttf')
ITALIC = FONT.with_name('LiberationSans-BoldItalic.ttf')


def box(coords):
    return tuple(round(v * S) for v in coords)


def font(size, bold=False, italic=False):
    return ImageFont.truetype(str(ITALIC if italic else BOLD if bold else FONT),
                              round(size * S))


def text(image, xy, value, size=11, fill='#ddddda', bold=False, anchor='mm', italic=False):
    ImageDraw.Draw(image).text(box(xy), value, font=font(size, bold, italic),
                               fill=fill, anchor=anchor)


def rr(image, rect, radius, fill, outline=None, width=1):
    ImageDraw.Draw(image).rounded_rectangle(box(rect), round(radius * S),
                                            fill, outline, round(width * S))


def line(image, points, fill, width=1):
    ImageDraw.Draw(image).line([box(p) for p in points], fill, round(width * S),
                               joint='curve')


def ellipse(image, rect, fill, outline=None, width=1):
    ImageDraw.Draw(image).ellipse(box(rect), fill, outline, round(width * S))


def shadow(image, rect, radius, blur=3, opacity=160):
    layer = Image.new('RGBA', image.size)
    rr(layer, rect, radius, (0, 0, 0, opacity))
    image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur * S)))


def rgb(value):
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def gradient_round(image, rect, radius, top, bottom):
    x0, y0, x1, y1 = box(rect)
    w, h = x1 - x0, y1 - y0
    grad = Image.new('RGBA', (w, h))
    d = ImageDraw.Draw(grad)
    for y in range(h):
        t = y / max(1, h - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,)
        d.line((0, y, w, y), fill=color)
    mask = Image.new('L', (w, h))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius * S, fill=255)
    grad.putalpha(mask)
    image.alpha_composite(grad, (x0, y0))


def knob(image, cx, cy, radius):
    # The stationary cap has a face offset up-left from its cylindrical base.
    layer = Image.new('RGBA', image.size)
    ellipse(layer, (cx - radius + 3, cy - radius + 6,
                    cx + radius + 9, cy + radius + 10), (0, 0, 0, 155))
    image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(4 * S)))
    ellipse(image, (cx - radius, cy - radius + 1, cx + radius + 2, cy + radius + 5),
            '#111213', '#0b0c0d', 1)
    ellipse(image, (cx - radius, cy - radius - 2, cx + radius, cy + radius + 2),
            '#333436', '#171819', 1)
    # Fine sidewall ribs are only visible on the lower edge.
    for angle in range(12, 169, 9):
        a = math.radians(angle)
        x, y = cx + math.cos(a) * (radius - 1), cy + math.sin(a) * (radius - 1)
        line(image, [(x, y), (x, y + 3)], '#242526', .7)
    fx, fy, fr = cx - 2, cy - 3, radius - 2
    size = round(fr * 2 * S + 2)
    cap = Image.new('RGBA', (size, size))
    pixels = cap.load()
    for y in range(size):
        for x in range(size):
            nx, ny = (x / S - fr) / fr, (y / S - fr) / fr
            dist = math.hypot(nx, ny)
            if dist <= 1:
                bevel = max(0, (dist - .91) / .09)
                light = 58 - ny * 5 - nx * 3 + bevel * (-ny * 13 - nx * 7 - 4)
                v = round(light)
                pixels[x, y] = (v, v + 1, v + 2, 255)
    image.alpha_composite(cap, box((fx - fr, fy - fr)))


def screw(image, x, y):
    ellipse(image, (x - 8, y - 7, x + 9, y + 10), '#101112')
    ellipse(image, (x - 8, y - 8, x + 8, y + 8), '#36383a', '#151617')
    ellipse(image, (x - 6, y - 6, x + 6, y + 6), '#252729', '#454749')
    points = [(x + 3.7 * math.cos(a * math.pi / 3),
               y + 3.7 * math.sin(a * math.pi / 3)) for a in range(6)]
    ImageDraw.Draw(image).polygon([box(p) for p in points], fill='#08090a')
    line(image, [(x - 5, y - 3), (x - 3, y - 5)], '#77797a', .8)


def icon(mask, label, x, y, w, h, ink=255):
    """Hardware glyphs are paths, independent of platform font coverage."""
    cx, cy = x + w / 2, y + h / 2 - 2
    r = 10
    d = ImageDraw.Draw(mask)
    stroke = 2.2
    if label == 'RECORD':
        ellipse(mask, (cx-r, cy-r, cx+r, cy+r), None, ink, stroke)
    elif label == 'STOP':
        rr(mask, (cx-r, cy-r, cx+r, cy+r), 3, None, ink, stroke)
    elif label == 'PLAY':
        line(mask, [(cx-7, cy-11), (cx+11, cy), (cx-7, cy+11), (cx-7, cy-11)], ink, stroke)
    elif label in ('UP', 'DOWN', 'LEFT', 'RIGHT'):
        pts = {'UP': [(-9, 7), (0, -10), (9, 7)],
               'DOWN': [(-9, -7), (0, 10), (9, -7)],
               'LEFT': [(7, -10), (-9, 0), (7, 10)],
               'RIGHT': [(-7, -10), (9, 0), (-7, 10)]}[label]
        line(mask, [(cx+a, cy+b) for a, b in pts], ink, stroke)
    elif label == 'SONG':
        for dx in (-9, 0, 9):
            rr(mask, (cx+dx-2.4, cy-2.5, cx+dx+2.4, cy+2.5), .5, ink)
    elif label == 'GLOBAL':
        for i in range(12):
            a = i * math.pi / 6
            line(mask, [(cx+math.cos(a)*8, cy+math.sin(a)*8),
                        (cx+math.cos(a)*13, cy+math.sin(a)*13)], ink, 2.1)
        ellipse(mask, (cx-9, cy-9, cx+9, cy+9), None, ink, 2)
        ellipse(mask, (cx-4, cy-4, cx+4, cy+4), None, ink, 1.8)
    elif label == 'SAMPLE':
        for dx, length in ((-10, 8), (-5, 16), (0, 23), (5, 14), (10, 8)):
            line(mask, [(cx+dx, cy-length/2), (cx+dx, cy+length/2)], ink, 2)
    elif label == 'VOICE':
        for off in (-3, 0, 3):
            ellipse(mask, (cx-11, cy-5+off, cx+11, cy+5+off), None, ink, 1.3)
    elif label == 'TEMPO':
        line(mask, [(cx-11, cy+9), (cx-6, cy-9), (cx+5, cy-9),
                    (cx+11, cy+9), (cx-11, cy+9)], ink, 2)
        line(mask, [(cx, cy+5), (cx+10, cy-11)], ink, 2.5)
    elif label == 'KEYBOARD':
        line(mask, [(cx-5, cy+8), (cx-5, cy-9), (cx+9, cy-12), (cx+9, cy+4)], ink, 3)
        ellipse(mask, (cx-12, cy+4, cx-3, cy+11), ink)
        ellipse(mask, (cx+2, cy, cx+11, cy+7), ink)
    else:
        size = 22 if label.isdigit() else 10 if len(label) > 3 else 11
        text(mask, (cx, cy), label, size, ink, bold=not label.isdigit())
        if label.isdigit():
            line(mask, [(cx-8, cy+UNDERLINE), (cx+8, cy+UNDERLINE)], ink, 2)


def key_sprite(product, label, spec, pressed):
    w, h = spec[2:4]
    image = Image.new('RGBA', (TW*S, TH*S))
    x, y = PAD, PAD
    base = rgb(layout.face(product, label))
    light = max(base) > 100
    shadow(image, (x+2, y+5, x+w+4, y+h+5), 10, 2, 155)
    rr(image, (x-1, y-1, x+w+1, y+h+1), 10, '#08090a', '#38393a', .8)
    gradient_round(image, (x+1, y+1, x+w-1, y+h-1), 9,
                   (51, 52, 53), (17, 18, 19))
    shift = 2 if pressed else 0
    top = tuple(min(255, b + (12 if light else 14)) for b in base)
    bottom = tuple(max(0, b - (14 if light else 4)) for b in base)
    gradient_round(image, (x+4, y+3+shift, x+w-4, y+h-5+shift), 6, top, bottom)
    line(image, [(x+10, y+5+shift), (x+w-10, y+5+shift)],
         tuple(min(255, c + 25) for c in base) + (255,), .7)
    ink = Image.new('L', image.size)
    icon(ink, label, x, y+shift, w, h)
    if label in ('1', '5', '9', '13'):
        side, top, bottom = BEAT_FRAME
        rr(ink, (x+side, y+top+shift, x+w-side, y+h-bottom+shift), 3, None, 190, 1.3)
    color = '#777670' if label.isdigit() and int(label) >= 9 and product == 'Digitone' else '#e2e3df'
    tint = Image.new('RGBA', image.size, rgb(color) + (255,))
    tint.putalpha(ink)
    image.alpha_composite(tint)
    # A lit rim on every key is physically inside its cap, never a flat fill.
    rr(ink, (x+7, y+6+shift, x+w-7, y+h-10+shift), 3, None, 215, 1.2)
    glow = ink.filter(ImageFilter.GaussianBlur(2*S)).point(lambda v: round(v*.5))
    emission = ImageChops.lighter(ink, glow)
    return (image.resize((TW, TH), Image.Resampling.LANCZOS),
            emission.resize((TW, TH), Image.Resampling.LANCZOS))


def plate(product):
    image = Image.new('RGBA', (layout.WIDTH*S, layout.HEIGHT*S), '#111214')
    x, y, w, h = layout.BODY
    shadow(image, (x+3, y+5, x+w+3, y+h+7), 8, 5, 210)
    rr(image, (x, y, x+w, y+h), 6, '#101112', '#48494b', .8)
    # Fine powder coat, rather than a noisy photographic overlay.
    coat = Image.new('RGBA', (w-4, h-5))
    rng = random.Random(2917)
    data = []
    for cy in range(h-5):
        for cx in range(w-4):
            v = round(26 + 3*(1-cy/h) + 1.4*(1-cx/w) + rng.uniform(-1.8, 1.8))
            data.append((v, v+1, v+1, 255))
    coat.putdata(data)
    image.alpha_composite(coat.resize(((w-4)*S, (h-5)*S), Image.Resampling.BICUBIC),
                          box((x+2, y+2)))
    for sx, sy in ((42, 94), (504, 94), (958, 94), (42, 828), (504, 828), (958, 828)):
        screw(image, sx, sy)
    text(image, (933, 114), 'digiemu', 30, '#eeeeea', bold=True, italic=True, anchor='rm')
    sx, sy, sw, sh = layout.SCREEN
    # A narrow recess keeps the deliberately enlarged display open and legible.
    rr(image, (sx-5, sy-5, sx+sw+5, sy+sh+5), 2, '#090a09', '#42443c', .7)
    rr(image, (sx-2, sy-2, sx+sw+2, sy+sh+2), 1, '#030502')
    legend = 'FM SYNTHESIZER' if product == 'Digitone' else 'DRUM COMPUTER & SAMPLER'
    text(image, (sx, sy-17), legend, 10, '#c5c6c0', anchor='lm')
    for label, (cx, cy, r) in layout.ENCODERS.items():
        knob(image, cx, cy, r)
        printed = 'Level/Data' if label == 'LEVEL/DATA' else label
        spec = layout.buttons(product)[label]
        text(image, (cx, spec[1]+8), printed, 11)
        if spec[4]:
            text(image, (cx, spec[1]+spec[3]+11), spec[4], 10.5, layout.accent(product))
    cx, cy, r = layout.MASTER_VOLUME
    knob(image, cx, cy, r)
    text(image, (cx, cy+r+21), 'Master Volume', 10.5)
    specs = layout.buttons(product)
    for label, (kx, ky, kw, kh, sub, _tint) in specs.items():
        if label in layout.ENCODERS or not sub:
            continue
        color = '#d6d6d0' if label.isdigit() else layout.accent(product)
        text(image, (kx+kw/2, ky+kh+11), sub, 9 if label.isdigit() else 10, color)
    for i, (lx, ly) in enumerate(layout.page_lights(specs['PAGE'])):
        ellipse(image, (lx-5, ly-5, lx+5, ly+5), '#080909', '#41403a', .8)
        ellipse(image, (lx-3, ly-3, lx+3, ly+3), '#302c1c')
        ellipse(image, (lx-1.5, ly-2, lx+.2, ly-.3), '#6c6250')
        text(image, (lx, ly-15), f'{i+1}:4', 8.5)
    if product == 'Digitakt':
        line(image, [(940, 670), (947, 670), (947, 775), (940, 775)], layout.accent(product), .8)
        text(image, (963, 714), 'Quick', 8, layout.accent(product))
        text(image, (963, 726), 'Mute', 8, layout.accent(product))
    return image.resize((layout.WIDTH, layout.HEIGHT), Image.Resampling.LANCZOS)


def build(product):
    dest = ROOT / 'emu' / 'assets' / 'panels' / product.lower()
    dest.mkdir(parents=True, exist_ok=True)
    body = plate(product)
    body.save(dest / 'plate.png', optimize=True)
    specs = {k: v for k, v in layout.buttons(product).items() if k not in layout.ENCODERS}
    atlas = Image.new('RGBA', (TW*2, TH*len(specs)))
    masks = Image.new('RGBA', atlas.size)
    meta = {'tile': [TW, TH], 'pad': PAD, 'keys': {}}
    preview = body.copy()
    for row, (label, spec) in enumerate(specs.items()):
        entry = {'row': row, 'masks': []}
        for pressed in (False, True):
            sprite, coverage = key_sprite(product, label, spec, pressed)
            atlas.alpha_composite(sprite, (int(pressed)*TW, row*TH))
            layer = Image.new('RGBA', (TW, TH), 'white')
            layer.putalpha(coverage)
            masks.alpha_composite(layer, (int(pressed)*TW, row*TH))
            entry['masks'].append(base64.b64encode(zlib.compress(coverage.tobytes())).decode())
            if not pressed:
                preview.alpha_composite(sprite, (spec[0]-PAD, spec[1]-PAD))
                if label in ('1', '5', 'SRC', 'SYN1', 'PLAY'):
                    layer = Image.new('RGBA', (TW, TH), '#83ed91' if label == 'PLAY' else '#ff635a')
                    layer.putalpha(coverage)
                    preview.alpha_composite(layer, (spec[0]-PAD, spec[1]-PAD))
        meta['keys'][label] = entry
    atlas.save(dest / 'keys.png', optimize=True)
    masks.save(dest / 'masks.png', optimize=True)
    (dest / 'skin.json').write_text(json.dumps(meta, separators=(',', ':'))+'\n')
    # An explicitly labelled sample display, not a firmware screenshot.
    screen = Image.new('RGB', (128, 64), '#080b04')
    d = ImageDraw.Draw(screen)
    f = ImageFont.truetype(str(BOLD), 8)
    ink = '#e6ed78'
    d.text((3, 1), 'A01  PANEL PREVIEW', font=f, fill=ink)
    d.line((2, 12, 125, 12), fill=ink)
    d.rectangle((3, 17, 18, 48), outline=ink)
    d.text((7, 26), '1', font=f, fill=ink)
    d.line([(25, 41), (40, 19), (48, 32), (81, 33), (113, 44)], fill=ink)
    for i, name in enumerate(('ATK', 'DEC', 'SUS', 'REL')):
        d.text((26+i*25, 49), name, font=f, fill=ink)
    preview.paste(screen.resize(layout.SCREEN[2:], Image.Resampling.NEAREST), layout.SCREEN[:2])
    # Master-volume pointer belongs to the control, not to the static plate.
    p = ImageDraw.Draw(preview)
    cx, cy, _r = layout.MASTER_VOLUME
    p.ellipse((cx+10, cy-25, cx+16, cy-19), fill='#f4f4ee')
    out = ROOT / 'out' / 'ui'
    out.mkdir(parents=True, exist_ok=True)
    preview.save(out / (product.lower()+'-mk1.png'))
    print(f'{product}: {len(specs)} caps; artwork and preview written')


if __name__ == '__main__':
    for product in ('Digitakt', 'Digitone'):
        build(product)
