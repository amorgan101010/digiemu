"""Load the generated mk1 skin with Tk and the standard library only.

Cap illumination uses the firmware's RGB values and pre-rendered coverage
masks, so the textured plastic stays dark while its legends and rim glow.
Pillow is needed only by tools/render_panel.py when rebuilding the assets.
"""
import base64
from collections import OrderedDict
import json
from pathlib import Path
import struct
import tkinter as tk
import zlib

ROOT = Path(__file__).with_name('assets') / 'panels'


def asset(product, filename):
    product = product.lower().replace(':', '-')
    if product not in ('digitakt', 'digitone', 'model-samples', 'model-cycles'):
        raise ValueError('unknown panel skin')
    if filename not in ('plate.png', 'keys.png', 'masks.png', 'skin.json'):
        raise ValueError('unknown panel asset')
    return ROOT / product / filename


def rgba_png(width, height, pixels):
    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload
                + struct.pack('>I', zlib.crc32(kind + payload)))
    stride = width * 4
    rows = b''.join(b'\0' + pixels[y * stride:(y + 1) * stride]
                    for y in range(height))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 3)) + chunk(b'IEND', b''))


class Skin:
    def __init__(self, master, product):
        self.master = master
        self.product = product.lower().replace(':', '-')
        self.meta = json.loads(asset(product, 'skin.json').read_text())
        self.plate = tk.PhotoImage(master=master, file=asset(product, 'plate.png'))
        self.atlas = tk.PhotoImage(master=master, file=asset(product, 'keys.png'))
        self.crops = []
        self.caps = {}
        self.masks = {}
        self.lights = OrderedDict()
        self.blank = tk.PhotoImage(master=master, width=1, height=1)
        self.visible = {}  # Holds displayed light images even after cache eviction.
        self.tile_w, self.tile_h = self.meta['tile']
        for label, entry in self.meta['keys'].items():
            self.masks[label] = [zlib.decompress(base64.b64decode(m))
                                 for m in entry['masks']]
            for pressed in (False, True):
                image = tk.PhotoImage(master=master,
                                      width=self.tile_w, height=self.tile_h)
                x, y = int(pressed) * self.tile_w, entry['row'] * self.tile_h
                master.tk.call(image, 'copy', self.atlas, '-from', x, y,
                               x + self.tile_w, y + self.tile_h, '-to', 0, 0)
                self.caps[label, pressed] = image

    def crop(self, x, y, width, height):
        image = tk.PhotoImage(master=self.master, width=width, height=height)
        self.master.tk.call(image, 'copy', self.plate, '-from', x, y,
                            x + width, y + height, '-to', 0, 0)
        self.crops.append(image)
        return image

    def cap(self, label, pressed=False):
        return self.caps[label, bool(pressed)]

    def light(self, label, rgb, pressed=False):
        if rgb is None:
            self.visible[label] = self.blank
            return self.blank
        rgb = tuple(int(max(0, min(255, c))) for c in rgb)
        key = label, rgb, bool(pressed)
        image = self.lights.get(key)
        if image is None:
            alpha = self.masks[label][int(bool(pressed))]
            colors = [bytes((*rgb, a)) for a in range(256)]
            pixels = b''.join(colors[a] for a in alpha)
            image = tk.PhotoImage(master=self.master,
                                  data=rgba_png(self.tile_w, self.tile_h, pixels))
            self.lights[key] = image
            if len(self.lights) > 128:
                self.lights.popitem(last=False)
        else:
            self.lights.move_to_end(key)
        self.visible[label] = image
        return image

    def public_meta(self):
        return {'product': self.product, 'tile': self.meta['tile'],
                'pad': self.meta['pad'],
                'rows': {k: v['row'] for k, v in self.meta['keys'].items()}}
