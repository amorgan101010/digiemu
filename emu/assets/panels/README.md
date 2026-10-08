# Original panel skins

Procedurally drawn artwork for digiemu. No photographs, vendor wordmarks,
product logos, firmware or firmware screenshots are included. Material,
control and light layers are shared by the desktop and browser interfaces.

Regenerate from the repository root on a system with fontconfig and
Liberation Sans installed:

```sh
uv run --with pillow python tools/render_panel.py
uv run --with pillow python tools/render_models.py
```

Pillow and the font are build tools only. Runtime uses Tk and Python's
standard library; the portable app bundles the generated assets.

- `plate.png`: powder coat, fasteners, knobs, narrow display recess and legends.
- `keys.png`: normal/pressed cap atlas; columns are the two states.
- `masks.png`: alpha coverage for browser illumination in firmware RGB.
- `skin.json`: atlas rows and compressed alpha masks for the desktop renderer.

`emu/mdpanel.py` owns the Model layouts, with three rows of knobs, six
velocity pads, and a single step row on a compact molded case.
`emu/panellayout.py` owns the Digitakt and Digitone coordinates. The two knob rows are shared between
models; the sampler has a wider step grid, while the FM panel reserves
space for its four track keys. The OLED uses an enlarged integer zoom.
Generated design previews in `out/ui/` use an explicitly labelled sample
screen and are not bundled.
