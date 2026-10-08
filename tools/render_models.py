"""Build original Model faceplates and key atlases: python tools/render_models.py."""
import base64
import json
import zlib

import render_panel as art
from PIL import Image, ImageDraw
from emu import mdpanel, panellayout


_original_icon = art.icon


def model_icon(mask, label, x, y, w, h, ink=255):
    aliases = {'SETTINGS': 'GLOBAL', 'WAVE': 'SAMPLE', 'MACHINE': 'VOICE',
               'FUNCTION': 'FUNC'}
    cx, cy = x + w / 2, y + h / 2 - 2
    if label in mdpanel.PADS:
        art.text(mask, (cx, cy), label, 22, ink, bold=True)
    elif label == 'LFO':
        art.line(mask, [(cx-13,cy), (cx-6,cy-7), (cx+6,cy+7), (cx+13,cy)], ink, 2.5)
    elif label == 'BACK':
        art.line(mask, [(cx-10,cy-3), (cx+9,cy-3), (cx+9,cy+9), (cx-5,cy+9)], ink, 2.5)
        art.line(mask, [(cx-4,cy-9), (cx-11,cy-3), (cx-4,cy+3)], ink, 2.5)
    elif label in ('LOOP', 'PUNCH', 'FLIP', 'GATE'):
        art.line(mask, [(cx-12,cy-6), (cx+10,cy-6), (cx+4,cy-12)], ink, 2.5)
        art.line(mask, [(cx+12,cy+6), (cx-10,cy+6), (cx-4,cy+12)], ink, 2.5)
    else:
        _original_icon(mask, aliases.get(label,label), x,y,w,h,ink)


art.icon = model_icon

def build(product):
    samples = product == 'Model:Samples'
    buttons, encoders = mdpanel.layout(product)
    image = Image.new('RGBA', (mdpanel.PANEL_W * art.S, mdpanel.PANEL_H * art.S), '#111214')
    art.shadow(image, (24, 80, 1360, 851), 30, 5, 180)
    art.gradient_round(image, (20, 74, 1360, 850), 30,
                       (233, 234, 227) if samples else (162, 166, 156),
                       (201, 204, 196) if samples else (130, 135, 127))
    art.rr(image, (40, 94, 1340, 838), 25, None, '#acb0a6' if samples else '#7c8278', 2)
    ink = '#292e2c'
    sx, sy, sw, sh = mdpanel.SCREEN
    art.rr(image, (sx-7, sy-7, sx+sw+7, sy+sh+7), 9, '#141817', '#969c91', 1)
    art.text(image, (sx+sw/2, sy+sh+21), product, 20, ink, bold=True)
    for label, (cx, cy, r) in encoders.items():
        color = ('#d75924' if samples else '#4c9aaf') if label == 'LEVEL/DATA' else '#e8e7df' if label == 'VOLUME' else '#aaa9a1' if label in ('REVERB SIZE', 'DELAY TIME') else '#646b68'
        art.shadow(image, (cx-r+2, cy-r+5, cx+r+5, cy+r+8), r, 4, 110)
        art.ellipse(image, (cx-r, cy-r, cx+r, cy+r), color, '#8c9289', 2)
        art.ellipse(image, (cx-r+5, cy-r+5, cx+r-5, cy+r-5), color, '#b4b8ae' if label == 'VOLUME' else '#777e75', 2)
        printed = {'VOLUME': 'MAIN VOLUME', 'SMPL START': 'SAMPLE START', 'SMPL LENGTH': 'SAMPLE LENGTH', 'VOL+DIST': 'VOLUME + DIST', 'SWING': 'SWING / NUDGE', 'CHANCE': 'CHANCE / COND'}.get(label, label)
        art.text(image, (cx, cy+r+21), printed, 11, ink, bold=True)
        sub = {'LEVEL/DATA': 'TRIG / PAN', 'REVERB SIZE': 'REV TONE', 'DELAY TIME': 'DEL FEEDBACK'}.get(label)
        if sub:
            art.text(image, (cx, cy+r+38), sub, 9, '#575f56')
    for label, (x,y,w,h,sub,_) in buttons.items():
        if sub:
            art.text(image, (x+w/2,y+h+14), sub, 9, '#50584f')
    for i,(x,y) in enumerate(panellayout.page_lights(buttons['PAGE'])):
        art.rr(image,(x-4,y-4,x+4,y+4),2,'#383f35')
        art.text(image,(x,y+13),f'{i+1}:4',8,ink)
    dest = art.ROOT / 'emu/assets/panels' / product.lower().replace(':','-')
    dest.mkdir(parents=True,exist_ok=True)
    plate=image.resize((mdpanel.PANEL_W,mdpanel.PANEL_H),Image.Resampling.LANCZOS)
    plate.save(dest/'plate.png',optimize=True)
    specs={k:v for k,v in buttons.items() if k not in encoders}
    art.TW,art.TH=112,100
    atlas=Image.new('RGBA',(224,100*len(specs)))
    masks=Image.new('RGBA',atlas.size)
    meta={'tile':[112,100],'pad':art.PAD,'keys':{}}
    preview=plate.copy()
    for row,(label,spec) in enumerate(specs.items()):
        entry={'row':row,'masks':[]}
        for pressed in (False,True):
            sprite,coverage=art.key_sprite(product,label,spec,pressed)
            atlas.alpha_composite(sprite,(int(pressed)*112,row*100))
            mask=Image.new('RGBA',(112,100),'white');mask.putalpha(coverage)
            masks.alpha_composite(mask,(int(pressed)*112,row*100))
            entry['masks'].append(base64.b64encode(zlib.compress(coverage.tobytes())).decode())
            if not pressed:
                preview.alpha_composite(sprite,(spec[0]-art.PAD,spec[1]-art.PAD))
        meta['keys'][label]=entry
    atlas.save(dest/'keys.png',optimize=True)
    masks.save(dest/'masks.png',optimize=True)
    (dest/'skin.json').write_text(json.dumps(meta,separators=(',',':'))+'\n')
    d=ImageDraw.Draw(preview);d.rectangle((sx,sy,sx+sw,sy+sh),fill='#b9c6e4')
    d.text((sx+20,sy+60),'A01  TR1',fill='#334577',font=art.font(16,True))
    d.text((sx+20,sy+130),'PANEL PREVIEW',fill='#334577',font=art.font(6))
    out=art.ROOT/'out/ui';out.mkdir(parents=True,exist_ok=True)
    preview.save(out/(product.lower().replace(':','-')+'.png'))
    print(product, 'artwork and preview written')


if __name__ == '__main__':
    for product in ('Model:Samples','Model:Cycles'):
        build(product)
