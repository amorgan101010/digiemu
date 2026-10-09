"""A Digitakt-shaped front panel, drawn to look like the hardware.

emu/gui.py lays controls out automatically from the device file's groups,
which draws any product correctly but looks like a debugger. This draws the
mk1's actual faceplate: the OLED at its real aspect, eight encoders in two
rows, the page column down the right, the function column down the left and
sixteen trig keys across the bottom.

Positions live here because they are a fact about the plastic, not about the
image. Everything else still comes from the firmware: a control is placed by
looking up its own NAME in the image's control table, so a code that moves
between builds follows its label rather than silently landing in the wrong
hole. A label this layout does not mention remains available in Extra controls.

MULTITOUCH. The wire carries an 8-bit STATE BITMASK per channel, not press
and release events, so simultaneous presses are what the hardware natively
expresses -- see emu/panelin.py. A plain click is momentary: press on down,
release on up. Shift-click LATCHES, so the button stays asserted while you
click others, which is how chords like FUNC+SRC, or holding a trig while
turning an encoder, are formed. Latched keys are drawn lit; letting go of
Shift, "clear latched" or Escape releases them all.

KEYBOARD. The computer keyboard plays the panel too, on the Monomachine /
Machinedrum layout Gearmulator uses: see emu/panelkeys.py. Ctrl is FUNC,
Shift latches until it is let go, Delete releases everything. A Keychron
with per-key lights shows the keymap while the window has the focus
(emu/keylight.py).

AUDIO. With the accelerated Unicorn (patches/README.md) the firmware's
render runs faster than real time and plays LIVE (MUTE silences it). Without
it the emulator records at a slow audio clock and PLAY plays the recording
back at 48 kHz afterwards. Either way the output is recorded: PLAY replays
it (silent ends trimmed), SAVE WAV writes it out. --no-audio skips the audio
model.

SESSIONS. --save-on-exit PATH saves the machine to PATH when the window is
closed: the emulator stops at a step boundary, the +Drive image is flushed,
and the snapshot is written to PATH.tmp and renamed over PATH. Closing waits
for that however long it takes; a save abandoned half way is a lost session.

SAMPLES. LOAD SAMPLES opens a file dialog for one or more WAV files and puts
them in the +Drive's /incoming (emu/samples.py). The firmware indexes the card
only when it boots, so the running session cannot see them: once the files
check out, the session is saved and closed, the samples are written, and the
panel returns SAMPLES_ADDED. Under the portable app (--app) the app then
rebuilds from the cold boot (~15 s) and reopens the panel; run on its own,
the snapshots have to be rebuilt by hand.

MIDI. The DIN ports (emu/midi.py) appear as a virtual input and output named
after the product, for a DAW. The MIDI button picks a device to take input
from and one to send to as well; the choice is kept in midi.json in the
firmware's folder and made again on the next start.

REMOTE serves the panel to a browser on the local network (emu/remote.py):
the layout this window placed, the live screen and the LEDs, played with
multitouch, and the sound too once the page's sound button is tapped (while
audio is live; the window's mute leaves it playing). It starts with the
window; clicking it turns it off, and that
choice is kept in remote.json beside midi.json until it is clicked on
again. The address is shown under the button.

FAILURES are shown, not swallowed: a snapshot that will not open, an
unrecognised firmware, a halt -- the emulator's `error` is drawn over the
screen and on the status line, and main() says so in its return code. A
snapshot made by a different build (its build manifest does not match) is
its own case, INCOMPATIBLE: nothing failed, the snapshot has to be made
again, and the portable app offers that instead of reporting a crash.

    uv run python -m emu.dtpanel [snapshot] [--syx PATH]
                                 [--save-on-exit PATH] [--no-audio] [--app]
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import tkinter as tk

from emu import (audioout, config, controlin, keylight, panelkeys, remote, panellayout,
                 panelskin)
from emu.gui import Emulator, H, W

ON, OFF = bytes.fromhex("e6ed78"), bytes.fromhex("080b04")

# RGB for each framebuffer byte value: zero is off, anything else on.
_PIXEL = [ON if v else OFF for v in range(256)]


def _midi_settings_path(snapshot, name='midi.json'):
    """-> where the MIDI menu's choice (or another panel setting, `name`)
    is kept: the firmware's folder under the portable app
    (firmware/<name>/snapshots/<build>/x.snap), else next to the
    snapshot."""
    if not snapshot:
        return None
    here = os.path.dirname(os.path.abspath(snapshot))
    folder = os.path.dirname(os.path.dirname(here))
    if os.path.isfile(os.path.join(folder, 'firmware.json')):
        return os.path.join(folder, name)
    return os.path.join(here, name)

_MONITOR = re.compile(r'^\s*\d+:\s*\+?(\*?)\S+\s+(\d+)/\d+x(\d+)/\d+'
                      r'\+(-?\d+)\+(-?\d+)')
def _monitors(text):
    """-> [(primary, x, y, w, h)] from `xrandr --listmonitors` output."""
    found = []
    for line in text.splitlines():
        m = _MONITOR.match(line)
        if m:
            w, h, x, y = (int(g) for g in m.groups()[1:])
            found.append((m.group(1) == '*', x, y, w, h))
    return found
def _home_monitor(pointer=None):
    """-> (x, y, w, h) of the monitor to open on: the primary one, else the
    one under the pointer; None where xrandr is absent (Windows, macOS) or
    says nothing useful."""
    try:
        text = subprocess.run(['xrandr', '--listmonitors'],
                              capture_output=True, text=True,
                              timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    mons = _monitors(text)
    for primary, x, y, w, h in mons:
        if primary:
            return x, y, w, h
    if pointer:
        px, py = pointer
        for _, x, y, w, h in mons:
            if x <= px < x + w and y <= py < y + h:
                return x, y, w, h
    return None
SCALE = panellayout.SCALE
BG, FACE, EDGE = '#111214', '#242629', '#414449'
TEXT, DIM, AMBER = '#e0e2e5', '#899097', '#ffc23d'
LIT, REC_C, PLAY_C = '#3f4b5c', '#e2483d', '#3fbf6a'
ERR = '#ff8f8f'                   # emu/gui.py's App uses the same for errors
# The OLED's top-left on the canvas, right of the Master Volume and
# LEVEL/DATA knobs.
SCREEN_X, SCREEN_Y = panellayout.SCREEN[:2]

# main()'s return codes, besides 0 (closed cleanly, session saved if asked),
# 1 (the emulator failed or halted) and 2 (the session was not saved).
# 4: the snapshot was made by a different build of the emulator or firmware
# (emu.gui's Emulator.incompatible) -- rebuild needed, nothing is broken.
INCOMPATIBLE = 4
# 5: the snapshot (or what it needs: the firmware, the device files) could
# not be opened at all, for another reason -- a truncated or damaged file,
# say. Distinct from 1 so a launcher can stop offering that snapshot.
LOAD_FAILED = 5
# 6: LOAD SAMPLES wrote samples to the card, so every snapshot predates it:
# rebuild from the cold boot (the portable app does, then reopens).
SAMPLES_ADDED = 6
# For arguments it cannot parse. 2 is argparse's usual choice, but here 2
# already means "the session was not saved".
USAGE_ERROR = 64


def _first_line(text, limit=160):
    """The first non-blank line of `text`, cut to `limit` characters."""
    line = next((s.strip() for s in str(text).splitlines() if s.strip()), '')
    return line if len(line) <= limit else line[:limit - 3] + '...'

# Very dim firmware RGB values are the unlit backlight state.
LED_DARK = 24


def _hex(rgb):
    return '#%02x%02x%02x' % tuple(rgb)


# One geometry source drives the native UI, generated artwork and browser.
TRIG_POSITIONS = panellayout.trig_positions('Digitakt')
BUTTONS = panellayout.buttons('Digitakt')
ENCODERS = panellayout.ENCODERS
MASTER_VOLUME = panellayout.MASTER_VOLUME
PANEL_W, PANEL_H = panellayout.WIDTH, panellayout.HEIGHT


def master_volume_angle(value, top):
    """-> the Master Volume indicator's angle in radians, clockwise from 12
    o'clock, for a gain of `value` on a knob that reaches `top`. The whole
    300-degree sweep is used: 0 points at 7 o'clock and `top` at 5 o'clock,
    so every position past unity still reads as louder."""
    value = max(0.0, min(top, value))
    return -5 * math.pi / 6 + (value / top) * (5 * math.pi / 3)


class DigitaktPanel(tk.Tk):
    # How long closing waits for the emulator to reach a step boundary. A
    # step is well under a second, so this only ever expires on a worker
    # stuck inside Unicorn. It does NOT bound a flush or save in progress,
    # nor a snapshot still loading (which goes straight on to them): see
    # _wait_for_worker.
    STOP_TIMEOUT = 5.0

    # What makes this the Digitakt's window rather than another product's.
    # emu/dnpanel.py's DigitonePanel overrides these and nothing else of the
    # machinery: the emulator thread, input, LEDs, audio and shutdown are
    # shared.
    PRODUCT = 'Digitakt'
    TITLE = 'digiemu — Sampler'
    SUBTITLE = 'Drum computer & sampler · mk1 emulator'
    BUTTONS = BUTTONS
    ENCODERS = ENCODERS
    PANEL_W, PANEL_H = PANEL_W, PANEL_H
    SCREEN_X, SCREEN_Y = SCREEN_X, SCREEN_Y
    SAMPLES = True                       # the LOAD SAMPLES button
    KEYS = panelkeys.DIGITAKT            # computer key -> panel label
    KNOB_KEYS = panelkeys.KNOBS          # computer key -> encoder label
    FUNC_KEY = 'FUNC'                    # the label of the key Ctrl holds
    REMOTE_PORT = 8794                   # REMOTE's first port (emu/remote.py)
    LEGEND = AMBER
    SCREEN_LEGEND = '8 Voice Digital Drum Computer & Sampler'

    def __init__(self, snapshot, syx=None, audio=True, save_on_exit=None,
                 app=False):
        super().__init__()
        self.app = app                   # run by the portable app
        self.samples_added = []          # names LOAD SAMPLES wrote
        self.title(self.TITLE)
        self.configure(bg=BG)
        self.canvas = tk.Canvas(self, width=self.PANEL_W,
                                height=self.PANEL_H, bg=BG,
                                highlightthickness=0)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self._vscroll = tk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self._hscroll = tk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(scrollregion=(0, 0, self.PANEL_W, self.PANEL_H),
                              xscrollcommand=self._hscroll.set,
                              yscrollcommand=self._vscroll.set)
        self.bind('<Configure>', self._fit_scrollbars)

        self.emu = Emulator(snapshot, syx=syx, audio=audio,
                            save_on_exit=save_on_exit,
                            midi_name='%s (digiemu)' % self.PRODUCT)
        self._midi_json = _midi_settings_path(snapshot)
        self._midi_win = None
        self._apply_saved_midi()
        self._error_shown = None          # the failure currently drawn
        self.player = audioout.Player()
        self._audio_note = ('', 0.0)     # (message, shown until)
        self.held = set()        # codes currently asserted
        self.latched = set()     # subset of held that survives mouse-up
        self.codes = {}          # firmware label -> button code
        self.enc_codes = {}      # firmware label -> encoder code
        self.items = {}          # code -> (cap sprite, light sprite)
        self.enc_items = {}      # code -> [ring, mark, angle]
        self._named = False
        self.led_of = {}         # button code -> LED id
        self.page_items = []     # (LED id, oval) for the pattern-page LEDs
        self._leds = {}          # LED id -> (r, g, b), as last drawn
        self.layout = []         # every control placed, for emu/remote.py
        self.remote_layout = None
        self.remote = None       # emu.remote.RemotePanel while REMOTE is on
        self._remote_json = _midi_settings_path(snapshot, 'remote.json')
        self._led_version = -1

        self.screen = tk.PhotoImage(width=W, height=H)
        self.big = tk.PhotoImage(width=W * SCALE, height=H * SCALE)
        self._draw_chrome()
        self.draw_screen(bytearray(W * H))

        self.bind('<Escape>', lambda _e: self.clear_latched())
        self.keyboard = panelkeys.Keyboard(self, self.KEYS, self.KNOB_KEYS,
                                           self.FUNC_KEY)
        self.bind('<KeyPress>', lambda e: self._key(e, True))
        self.bind('<KeyRelease>', lambda e: self._key(e, False))
        self.bind('<FocusOut>',
                  lambda _e: self.after_idle(self._focus_check))
        # A keyboard with per-key lights shows the keymap while the window
        # has the focus (emu/keylight.py).
        self.keylight = keylight.KeyLight()
        self.bind('<FocusIn>',
                  lambda _e: self.keylight.show(self.KEYS, self.KNOB_KEYS))

        # Closing the window has to stop the worker BEFORE the interpreter
        # tears down. The worker sits inside uc_emu_start; if the main thread
        # exits first, weakref finalizers call Unicorn's release_handle and
        # free the handle out from under the running thread, and the process
        # dies -- SIGSEGV here, 'malloc(): unsorted double linked list
        # corrupted' on a longer run. Both arrived after billions of
        # instructions of clean operation, so they read as random instability
        # rather than as a shutdown bug. emu/gui.py's quit_all has carried
        # this fix for the other GUI all along; this one never got it.
        self.protocol('WM_DELETE_WINDOW', self.quit_all)

        # Ask for the front. A window manager opens a new window BEHIND the
        # focused one, so launched from a maximised editor this window is
        # created, mapped and on-screen but never seen -- which is
        # indistinguishable from "it didn't launch". Topmost is dropped again
        # straight away so the window behaves normally once it has been seen.
        # Place the window explicitly. Left to itself, Weston (WSLg) has put
        # it at +4368+1022 on a 5120x1440 desktop -- 1160x790 from there hangs
        # off the bottom-right corner, leaving a sliver under the taskbar --
        # and at +4082+215 on the launch before that. A window you cannot find
        # is indistinguishable from one that never opened, which is exactly
        # how this was reported. Clamp it fully on-screen.
        # On X11 the "screen" is the whole desktop across every monitor, so
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        pw, ph = self.PANEL_W, self.PANEL_H
        mon = _home_monitor(self.winfo_pointerxy())
        if mon:
            mx, my, mw, mh = mon
            pw, ph = min(pw, mw - 32), min(ph, mh - 80)
            x = mx + max(0, (mw - pw) // 2)
            y = my + max(0, (mh - ph) // 2)
        else:
            pw, ph = min(pw, sw - 32), min(ph, sh - 80)
            x = max(0, min(80, sw - pw))
            y = max(0, min(60, sh - ph))
        self.geometry('%dx%d+%d+%d' % (pw, ph, x, y))
        print('[dtpanel] %s window %dx%d at +%d+%d on a %dx%d desktop'
              % (self.PRODUCT, pw, ph, x, y, sw, sh), flush=True)

        self.lift()
        self.attributes('-topmost', True)
        self.after(500, lambda: self.attributes('-topmost', False))
        try:
            self.focus_force()
        except tk.TclError:          # no WM, or focus refused: not fatal
            pass

        self.emu.start()
        # On unless switched off: as Gearmulator's MD/MM remote panel is.
        if self._remote_saved().get('enabled', True):
            self.remote_toggle(save=False)
        self.control = controlin.ControlInput(
            self.emu, lambda: self.enc_codes, self.PRODUCT,
            leds=self._key_lights)
        if not self.control.start():
            print('[control] no control input: %s' % self.control.error,
                  flush=True)
            self.control = None
        self.after(50, self.tick)

    def _fit_scrollbars(self, event):
        """Keep every control reachable on smaller desktops or resized windows."""
        if event.widget is not self:
            return
        width, height = event.width, event.height
        vertical, horizontal = height < self.PANEL_H, width < self.PANEL_W
        horizontal |= vertical and width - self._vscroll.winfo_reqwidth() < self.PANEL_W
        vertical |= horizontal and height - self._hscroll.winfo_reqheight() < self.PANEL_H
        if vertical:
            self._vscroll.grid(row=0, column=1, sticky='ns')
        else:
            self._vscroll.grid_remove()
            self.canvas.yview_moveto(0)
        if horizontal:
            self._hscroll.grid(row=1, column=0, sticky='ew')
        else:
            self._hscroll.grid_remove()
            self.canvas.xview_moveto(0)

    # ---------------------------------------------------------------- chrome
    def _rr(self, x, y, w, h, r, **kw):
        """A rounded rectangle; Tk's canvas has no primitive for one."""
        pts = [x + r, y, x + w - r, y, x + w, y, x + w, y + r,
               x + w, y + h - r, x + w, y + h, x + w - r, y + h, x + r, y + h,
               x, y + h, x, y + h - r, x, y + r, x, y]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _draw_chrome(self):
        c = self.canvas
        self.skin = panelskin.Skin(self, self.PRODUCT)
        c.create_image(0, 0, image=self.skin.plate, anchor='nw')
        self.cap_items = {}
        self.key_labels = {}
        self.enc_push_items = {}
        self.extra_items = {}
        pad = self.skin.meta['pad']
        for label in self.skin.meta['keys']:
            if label not in self.BUTTONS:
                continue                 # a key only the app's panel has (PUSH)
            x, y = self.BUTTONS[label][:2]
            cap = c.create_image(x - pad, y - pad,
                                 image=self.skin.cap(label), anchor='nw')
            light = c.create_image(x - pad, y - pad,
                                   image=self.skin.blank, anchor='nw')
            self.cap_items[label] = (cap, light)
        c.create_text(24, 21, text='digiemu', fill=TEXT,
                      font=('Helvetica', -20, 'bold'), anchor='w')
        c.create_text(24, 48, text=self.SUBTITLE, fill=DIM,
                      font=('Helvetica', -10), anchor='w')
        sx, sy = self.SCREEN_X, self.SCREEN_Y
        c.create_image(sx, sy, image=self.big, anchor='nw')
        self.err_box = c.create_rectangle(
            sx, sy, sx + W * SCALE, sy + H * SCALE,
            fill='#080b04', outline='', state='hidden')
        self.err_text = c.create_text(
            sx + 14, sy + 14, text='', fill=ERR,
            font=('Helvetica', -12), anchor='nw',
            width=W * SCALE - 28, state='hidden')
        self.status = c.create_text(24, self.PANEL_H - 16, text='Starting…', fill=DIM,
                                     font=('Helvetica', -11), anchor='w', width=790)
        clear = c.create_text(self.PANEL_W - 24, self.PANEL_H - 16, text='clear latched', fill=DIM,
                               font=('Helvetica', -11), anchor='e')
        c.tag_bind(clear, '<Button-1>', lambda _e: self.clear_latched())
        c.bind('<ButtonRelease-1>', self._release_pointer)
        self._draw_audio_controls()

    def _draw_audio_controls(self):
        c = self.canvas
        self.audio_text = c.create_text(132, 22, text='AUDIO  starting',
                                        fill=DIM, font=('Helvetica', -11),
                                        anchor='w', width=304)
        self.audio_btns = {}
        actions = [('MUTE', 58, self.audio_toggle_mute),
                   ('PLAY', 48, self.audio_play),
                   ('CLEAR', 50, self.audio_clear),
                   ('SAVE WAV', 70, self.audio_save),
                   ('MIDI', 46, self.midi_menu),
                   ('REMOTE', 64, self.remote_toggle)]
        if self.SAMPLES:
            actions.append(('LOAD SAMPLES', 96, self.load_samples))
        x = 450
        for name, w, fn in actions:
            rect = self._rr(x, 10, w, 26, 5, fill=FACE, outline=EDGE)
            txt = c.create_text(x + w / 2, 23, text=name, fill=TEXT,
                                font=('Helvetica', -10, 'bold'))
            for item in (rect, txt):
                c.tag_bind(item, '<Button-1>', lambda _e, f=fn: f())
            self.audio_btns[name] = (rect, txt)
            x += w + 6
        self.remote_btn = self.audio_btns['REMOTE']
        self.remote_text = c.create_text(976, 48, text='', fill=AMBER,
                                         font=('Helvetica', -10), anchor='e')
        self._draw_master_volume()

    def _draw_master_volume(self):
        """Master Volume: the top-left knob. The hardware's volume pot is
        analog (not in the firmware's code table), so the emulator applies
        the knob position as a software gain before the samples reach the
        host device."""
        c = self.canvas
        mv_x, mv_y, mv_r = MASTER_VOLUME
        self._mv_oval = self._knob_target(mv_x, mv_y, mv_r)
        self._mv_mark = c.create_line(
            mv_x, mv_y - mv_r + 11, mv_x, mv_y - mv_r + 7,
            fill='#f1f2f3', width=4, capstyle='round')
        # 0.0 (silent) to 1.0 (unity); the knob goes a bit past unity with
        # clipping at the host.
        self._mv_value = 1.0
        self._paint_master_volume()
        for item in (self._mv_oval, self._mv_mark):
            try:
                c.tag_bind(item, '<MouseWheel>',
                           lambda e: self._turn_master_volume(
                               1 if e.delta > 0 else -1, e))
            except tk.TclError:
                pass
            c.tag_bind(item, '<Button-4>',
                       lambda e: self._turn_master_volume(1, e))
            c.tag_bind(item, '<Button-5>',
                       lambda e: self._turn_master_volume(-1, e))
            c.tag_bind(item, '<ButtonPress-1>',
                       lambda e: self._mv_drag_start(e))
            c.tag_bind(item, '<B1-Motion>',
                       lambda e: self._mv_drag(e))

    def _note(self, msg, secs=4.0):
        self._audio_note = (msg, time.time() + secs)

    # -- MIDI devices ------------------------------------------------------
    def _apply_saved_midi(self):
        """Connect the devices chosen last time, if they are plugged in."""
        host = self.emu.midi_host
        if host is None or not self._midi_json:
            return
        try:
            with open(self._midi_json) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        for key, pick in (('input', host.set_input),
                          ('output', host.set_output)):
            name = saved.get(key)
            if name:
                try:
                    pick(name)
                    print('[midi] %s: %s' % (key, name), flush=True)
                except OSError as exc:
                    print('[midi] %s %r: %s' % (key, name, exc), flush=True)

    def _save_midi(self):
        host = self.emu.midi_host
        if host is None or not self._midi_json:
            return
        try:
            with open(self._midi_json, 'w') as f:
                json.dump({'input': host.input, 'output': host.output}, f)
        except OSError as exc:
            self._note('MIDI choice not saved: %s' % exc)

    def midi_menu(self):
        """MIDI: a small window to pick the device input and output."""
        host = self.emu.midi_host
        if host is None:
            self._note('MIDI is not available: %s'
                       % (self.emu.midi_error or 'no MIDI ports'), 6.0)
            return
        if self._midi_win is not None and self._midi_win.winfo_exists():
            self._midi_win.lift()
            return
        from tkinter import ttk
        win = self._midi_win = tk.Toplevel(self)
        win.title('%s MIDI' % self.PRODUCT)
        win.configure(bg=BG, padx=14, pady=12)
        win.transient(self)
        win.resizable(False, False)
        none = '(none)'
        rows = {}

        def refresh():
            for key, (box, ports, current) in rows.items():
                values = [none] + ports()
                name = current()
                if name and name not in values:
                    values.append(name)             # chosen, now unplugged
                box['values'] = values
                box.set(name or none)

        def chosen(key, pick):
            box = rows[key][0]
            name = box.get()
            try:
                pick(None if name == none else name)
            except OSError as exc:
                self._note('MIDI %s: %s' % (key, exc), 6.0)
            refresh()
            self._save_midi()

        for row, (key, label, ports, pick, current) in enumerate((
                ('input', 'MIDI IN from', host.inputs, host.set_input,
                 lambda: host.input),
                ('output', 'MIDI OUT to', host.outputs, host.set_output,
                 lambda: host.output))):
            tk.Label(win, text=label, bg=BG, fg=TEXT,
                     font=('Helvetica', 10)).grid(row=row, column=0,
                                                  sticky='w', pady=4)
            box = ttk.Combobox(win, state='readonly', width=40)
            box.grid(row=row, column=1, padx=(10, 0), pady=4)
            box.bind('<<ComboboxSelected>>',
                     lambda _e, k=key, p=pick: chosen(k, p))
            rows[key] = (box, ports, current)
        if host.virtual:
            hint = ("Also always there: the virtual ports '%s', for a DAW."
                    % host.name)
        else:
            # Windows: RtMidi's WinMM backend has no virtual ports.
            hint = ('There are no virtual MIDI ports here (Windows has none). '
                    'To reach a DAW, make a loopback port, with loopMIDI for '
                    'example, press Refresh and pick it above.')
        tk.Label(win, text=hint, bg=BG, fg=DIM, font=('Helvetica', 9),
                 justify='left', wraplength=420).grid(
            row=2, column=0, columnspan=2, sticky='w', pady=(8, 0))
        buttons = tk.Frame(win, bg=BG)
        buttons.grid(row=3, column=0, columnspan=2, sticky='e', pady=(10, 0))
        ttk.Button(buttons, text='Refresh', command=refresh).pack(
            side='left', padx=(0, 6))
        ttk.Button(buttons, text='Close', command=win.destroy).pack(
            side='left')
        refresh()

    def _recording(self):
        """The recording with its silent ends trimmed, or b''."""
        emu = self.emu
        return audioout.trim_silence(emu.audio_take(),
                                     rate=emu.audio_cfg['rate'])

    def audio_play(self):
        emu = self.emu
        if not emu.audio_on:
            self._note('audio is off')
            return
        if self.player.playing:
            self.player.stop()
            self._note('stopped')
            return
        pcm = self._recording()
        if not pcm:
            self._note('only silence recorded so far')
            return
        rate = emu.audio_cfg['rate']
        if self.player.rate != rate:
            self.player = audioout.Player(rate=rate)
        self.player.gain = getattr(self, '_mv_value',
                                   getattr(emu, '_volume', 1.0))
        self.player.play(pcm)
        self._note('playing %.2f s' % (len(pcm) / 4 / rate), 1.0)

    def audio_clear(self):
        self.player.stop()
        self.emu.audio_clear()
        self._note('recording cleared')

    def audio_toggle_mute(self):
        emu = self.emu
        if not emu.audio_live:
            self._note('no live audio: PLAY plays the recording')
            return
        emu.audio_mute(not emu.audio_muted)
        self._note('muted' if emu.audio_muted else 'live')

    def audio_save(self):
        emu = self.emu
        if not emu.audio_on:
            self._note('audio is off')
            return
        pcm = self._recording()
        if not pcm:
            self._note('only silence recorded so far')
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension='.wav',
            filetypes=[('WAV audio', '*.wav')],
            initialfile=time.strftime(self.PRODUCT.lower()
                                      + '-%Y%m%d-%H%M%S.wav'))
        if not path:
            return
        audioout.write_wav(path, pcm, rate=emu.audio_cfg['rate'])
        self._note('saved %s' % os.path.basename(path))

    def _draw_audio_status(self):
        emu = self.emu
        playing = self.player.playing
        msg, until = self._audio_note
        if msg and time.time() < until:
            text = 'AUDIO  ' + msg
        elif playing:
            text = 'AUDIO  playing  (PLAY again stops)'
        elif emu.audio_on and emu.audio_live:
            if emu.audio_muted:
                text = 'AUDIO  LIVE, muted'
            elif emu._live_error:
                text = 'AUDIO  no output device'
            else:
                text = 'AUDIO  LIVE  ·  %d ms buffered' % (
                    emu.live_latency_ms())
                if emu.live_underruns:
                    text += '  ·  %d dropouts' % emu.live_underruns
        elif emu.audio_on:
            text = 'AUDIO  %.2f s recorded  ·  rendering at %.0f%%' % (
                emu.audio_seconds(), emu.audio_speed * 100)
        elif emu.audio_error:
            text = 'AUDIO  off: not set up in this snapshot'
        elif emu.ready.is_set():
            text = 'AUDIO  off'
        else:
            text = 'AUDIO  starting'
        if self.player.error:
            text = 'AUDIO  no output device: %s' % self.player.error
        self.canvas.itemconfigure(self.audio_text, text=text)
        self.canvas.itemconfigure(self.audio_btns['PLAY'][1],
                                  text='STOP' if playing else 'PLAY')
        self.canvas.itemconfigure(self.audio_btns['MUTE'][1],
                                  text='UNMUTE' if emu.audio_muted else 'MUTE')

    def draw_screen(self, fb):
        # Unchanged since the last refresh: nothing to do. Building the image
        # is a Python loop over every pixel, run on this thread while the
        # emulator thread waits for the interpreter lock, so skipping it on a
        # still screen buys emulation time.
        frame = bytes(fb)
        if frame == getattr(self, '_last_frame', None):
            return
        self._last_frame = frame
        body = b''.join(map(_PIXEL.__getitem__, frame))
        self.screen.put(b'P6\n%d %d\n255\n' % (W, H) + body, to=(0, 0, W, H))
        # copy -zoom writes into the existing image; PhotoImage.zoom would
        # allocate a new one on every refresh.
        self.tk.call(self.big, 'copy', self.screen, '-zoom', SCALE, SCALE)

    # --------------------------------------------------------------- widgets
    def _bind_button(self, item, code):
        self.canvas.tag_bind(item, '<ButtonPress-1>', self._press_at)
        self.canvas.tag_bind(item, '<ButtonRelease-1>', self._release_pointer)

    def _press_at(self, event):
        # Sprite shadows overlap neighboring hit areas. Resolve the actual cap,
        # not the rectangular bounds of its transparent image tile.
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for code, label in self.key_labels.items():
            bx, by, w, h = self.BUTTONS[label][:4]
            if bx <= x < bx + w and by <= y < by + h:
                self._pointer_code = code
                self.press(code, event)
                break

    def _release_pointer(self, _event=None):
        code = getattr(self, '_pointer_code', None)
        if code is not None:
            self.release(code)
            self._pointer_code = None

    def _knob_target(self, x, y, r):
        # A plate crop makes the entire knob clickable without flattening its art.
        image = self.skin.crop(x - r, y - r - 3, 2 * r, 2 * r + 8)
        return self.canvas.create_image(x - r, y - r - 3, image=image, anchor='nw')

    def _build_controls(self):
        """Attach firmware codes to the shared skin's physical controls."""
        c = self.canvas
        overflow = []
        for label, code in sorted(self.codes.items()):
            if label in self.ENCODERS:
                continue
            if label not in self.cap_items:
                overflow.append((label, code))
                continue
            self.items[code] = self.cap_items[label]
            self.key_labels[code] = label
            x, y, w, h, sub, tint = self.BUTTONS[label]
            self.layout.append({'k': 'b', 'code': code, 'label': label,
                                'x': x, 'y': y, 'w': w, 'h': h, 'sub': sub,
                                'tint': tint, 'face': panellayout.face(self.PRODUCT, label)})
            for item in self.items[code]:
                self._bind_button(item, code)
        if overflow:
            link = c.create_text(460, 48, text='Extra controls', fill=DIM,
                                  font=('Helvetica', -10), anchor='w')
            c.tag_bind(link, '<Button-1>', lambda _e: self._extra_controls(overflow))
            for i, (label, code) in enumerate(overflow):
                self.layout.append({'k': 'b', 'code': code, 'label': label,
                                    'x': 40 + (i % 10) * 92,
                                    'y': 868 + (i // 10) * 40,
                                    'w': 84, 'h': 30, 'sub': None, 'tint': None})
        dev = getattr(self.emu, 'device', None)
        self.led_of = {code: led for led, code in
                       (getattr(dev, 'leds', None) or {}).items()}
        page_leds = getattr(dev, 'page_leds', ()) or ()
        positions = panellayout.page_lights(self.BUTTONS['PAGE'], len(page_leds))
        for led, (cx, cy) in zip(page_leds, positions):
            dot = c.create_oval(cx - 2.5, cy - 2.5, cx + 2.5, cy + 2.5,
                                fill='#242316', outline='')
            self.page_items.append((led, dot))
            self.layout.append({'k': 'd', 'led': led, 'x': cx, 'y': cy})
        # The lens beside each of a Model's parameter knobs.
        code_label = {code: label for label, code in self.enc_codes.items()}
        for led, code in sorted((getattr(dev, 'knob_leds', None) or {}).items()):
            spec = self.ENCODERS.get(code_label.get(code))
            if spec is None:
                continue
            cx, cy = panellayout.knob_light(*spec)
            dot = c.create_rectangle(cx - 4.5, cy - 4.5, cx + 4.5, cy + 4.5,
                                     fill='#242316', outline='')
            self.page_items.append((led, dot))
            self.layout.append({'k': 'd', 'led': led, 'x': cx, 'y': cy})
        self._led_version = -1
        for label, code in self.enc_codes.items():
            spec = self.ENCODERS.get(label)
            if spec is None:
                continue
            x, y, r = spec
            target = self._knob_target(x, y, r)
            ring = c.create_oval(x - r, y - r, x + r, y + r,
                                  outline=self.LEGEND, width=1, state='hidden')
            mark = c.create_line(x, y - r + 11, x, y - r + 7,
                                 fill='#f1f2f3', width=3, capstyle='round', state='hidden')
            push = self.codes.get(label)
            if push is not None:
                self.enc_push_items[push] = ring
            self.enc_items[code] = [ring, mark, 0.0]
            self.layout.append({'k': 'e', 'code': code, 'label': label,
                                'x': x, 'y': y, 'r': r})
            for item in (target, ring, mark):
                try:
                    c.tag_bind(item, '<MouseWheel>',
                               lambda e, k=code: self.turn(k, 1 if e.delta > 0 else -1, e))
                except tk.TclError:
                    pass
                c.tag_bind(item, '<Button-4>', lambda e, k=code: self.turn(k, 1, e))
                c.tag_bind(item, '<Button-5>', lambda e, k=code: self.turn(k, -1, e))
                c.tag_bind(item, '<ButtonPress-1>', lambda e, k=code: self._drag_start(k, e))
                c.tag_bind(item, '<B1-Motion>', lambda e, k=code: self._drag(k, e))
                c.tag_bind(item, '<ButtonRelease-1>',
                           lambda e, k=code, name=label: self._drag_end(k, name, e))
        self.remote_layout = self._remote_layout()

    def _extra_controls(self, controls):
        if getattr(self, '_extra_win', None) is not None and self._extra_win.winfo_exists():
            self._extra_win.lift()
            return
        win = self._extra_win = tk.Toplevel(self)
        win.title('Extra controls')
        win.configure(bg=BG, padx=12, pady=12)
        win.transient(self)
        for i, (label, code) in enumerate(controls):
            button = tk.Label(win, text=label, bg=FACE, fg=TEXT, padx=12, pady=8)
            button.grid(row=i // 4, column=i % 4, padx=3, pady=3)
            button.bind('<ButtonPress-1>', lambda e, k=code: self.press(k, e))
            button.bind('<ButtonRelease-1>', lambda _e, k=code: self.release(k))
            self.extra_items[code] = button
            self._paint(code)

    def _remote_layout(self):
        """-> what emu/remote.py serves: every control as placed above, the
        OLED's place, and the LEDs in the order its state frames carry
        them. A key's 'led' is an index into that order."""
        leds = sorted(set(self.led_of.values())
                      | {c['led'] for c in self.layout if c['k'] == 'd'})
        index = {led: i for i, led in enumerate(leds)}
        controls = []
        for c in self.layout:
            c = dict(c)
            if c['k'] == 'b':
                c['led'] = index.get(self.led_of.get(c['code']))
            elif c['k'] == 'd':
                c['led'] = index[c['led']]
            else:                    # tap a knob on the page to push it
                c['push'] = self.codes.get(c['label'])
                spec = self.BUTTONS.get(c['label'])
                c['sub'] = spec[4] if spec else None
            controls.append(c)
        sx, sy = self.SCREEN_X, self.SCREEN_Y
        x0, y0, width, height = panellayout.BODY
        bottom = max([y0 + height] + [c['y'] + c['h'] + 12
                     for c in controls if c['k'] == 'b'])
        return {'product': self.PRODUCT, 'screen_legend': self.SCREEN_LEGEND,
                'controls': controls, 'leds': leds,
                'screen': {'x': sx, 'y': sy, 'w': W * SCALE, 'h': H * SCALE},
                'skin': self.skin.public_meta(),
                'canvas': [panellayout.WIDTH, panellayout.HEIGHT],
                'master': list(MASTER_VOLUME),
                'bounds': [x0, y0, width, bottom - y0]}

    def _remote_saved(self):
        """-> remote.json's settings: {'enabled': bool, 'port': int}."""
        try:
            with open(self._remote_json) as f:
                saved = json.load(f)
            return saved if isinstance(saved, dict) else {}
        except (OSError, TypeError, ValueError):
            return {}
    def remote_toggle(self, save=True):
        """REMOTE: serve the panel to browsers on the network, or stop."""
        saved = self._remote_saved()
        if self.remote is not None:
            self.remote.stop()
            self.remote = None
            print('[remote] stopped', flush=True)
        else:
            port = saved.get('port', self.REMOTE_PORT)
            server = remote.RemotePanel(self.emu, lambda: self.remote_layout,
                                        port if isinstance(port, int)
                                        else self.REMOTE_PORT, self.PRODUCT)
            if server.start():
                self.remote = server
                print('[remote] %s panel at %s' % (self.PRODUCT, server.url),
                      flush=True)
            else:
                # Short: the status line runs toward the REMOTE button.
                print('[remote] no free port from %d: %s'
                      % (server.first_port, server.error), flush=True)
                self._note('REMOTE: ports %d-%d busy' % (
                    server.first_port,
                    server.first_port + remote.PORTS_TRIED - 1), 8.0)
        self._paint_remote()
        if save and self._remote_json:
            saved['enabled'] = self.remote is not None
            try:
                with open(self._remote_json, 'w') as f:
                    json.dump(saved, f)
            except OSError as exc:
                self._note('REMOTE setting not saved: %s' % exc)
    def _paint_remote(self):
        rect, txt = self.remote_btn
        on = self.remote is not None
        self.canvas.itemconfigure(rect, fill=LIT if on else FACE,
                                  outline=AMBER if on else EDGE)
        self.canvas.itemconfigure(self.remote_text,
                                  text=self.remote.url if on else '')

    def _key_lights(self):
        """-> key label -> the (r, g, b) its light shows, None when dark:
        for the control input (emu/controlin.py). Read off the emulator
        thread's own LED map, so it is current even between repaints."""
        lit = dict(getattr(self.emu, 'leds', None) or {})
        led_of = getattr(self, 'led_of', {})
        return {label: lit.get(led_of[code])
                for label, code in self.codes.items() if code in led_of}

    # ----------------------------------------------------------------- input
    def press(self, code, event=None, latch=None):
        """Hold a key down. A shift-click toggles its latch. `latch` is
        the keyboard's (emu/panelkeys.py), which keeps its own: True draws
        the key latched and holds it until release(code, force=True)."""
        if latch is None and event is not None and event.state & 0x0001:
            if code in self.latched:
                self.latched.discard(code)
                self.held.discard(code)
                self.emu.inbox.append(('release', code, 0))
            else:
                self.latched.add(code)
                self.held.add(code)
                self.emu.inbox.append(('press', code, 0))
        else:
            if latch:
                self.latched.add(code)
            self.held.add(code)
            self.emu.inbox.append(('press', code, 0))
        self._paint(code)

    def release(self, code, force=False):
        if code in self.latched and not force:  # latched: ignore mouse-up
            return
        self.latched.discard(code)
        self.held.discard(code)
        self.emu.inbox.append(('release', code, 0))
        self._paint(code)

    def clear_latched(self, keep=()):
        for code in list(self.latched):
            if code in keep:
                continue
            self.latched.discard(code)
            self.held.discard(code)
            self.emu.inbox.append(('release', code, 0))
            self._paint(code)

    def release_everything(self):
        """Let go of every held and latched key, however it was held."""
        codes = list(self.held)
        self.held.clear()
        self.latched.clear()
        self.emu.inbox.append(('release_all', 0, 0))
        for code in codes:
            self._paint(code)

    def _key(self, event, down):
        if not self._named:
            return None
        used = self.keyboard.key(down, event.keysym, event.keycode,
                                 event.state)
        if not down and event.keysym in panelkeys.SHIFT_KEYS:
            self.clear_latched(keep=self.keyboard.holding())
        return 'break' if used else None

    def _focus_check(self):
        """Focus left the panel window: its key-ups will go elsewhere, so
        let go of what the keyboard holds."""
        try:
            widget = self.focus_get()
            here = widget is not None and widget.winfo_toplevel() is self
        except (KeyError, tk.TclError):
            here = False
        if not here:
            self.keyboard.focus_lost()
            self.keylight.restore()

    def _drag_start(self, code, event):
        self._drag_y = event.y
        self._drag_acc = 0.0
        self._drag_code = code
        self._drag_origin = (event.x, event.y)
        self._drag_moved = False
        self._drag_pending = 0
        self._drag_after = None

    def _flush_drag(self):
        self._drag_after = None
        steps, self._drag_pending = self._drag_pending, 0
        if steps:
            self.turn(self._drag_code, steps)

    def _drag(self, code, event):
        """Vertical drag turns an encoder: up is clockwise, 6px per detent."""
        if code != getattr(self, '_drag_code', None):
            return
        ox, oy = self._drag_origin
        if abs(event.x - ox) > 4 or abs(event.y - oy) > 4:
            self._drag_moved = True
        dy = getattr(self, '_drag_y', event.y) - event.y
        self._drag_y = event.y
        self._drag_acc = getattr(self, '_drag_acc', 0.0) + dy / 6.0
        step = int(self._drag_acc)
        if step:
            self._drag_acc -= step
            if getattr(self, 'DRAG_FLUSH_MS', 0):
                self._drag_pending += step
                if self._drag_after is None:
                    self._drag_after = self.after(self.DRAG_FLUSH_MS,
                                                  self._flush_drag)
            else:
                self.turn(code, step)

    def _drag_end(self, code, label, event):
        """A click pushes an encoder; a drag turns it."""
        if code != getattr(self, '_drag_code', None):
            return
        moved = self._drag_moved
        if self._drag_after is not None:
            self.after_cancel(self._drag_after)
            self._flush_drag()
        self._drag_code = None
        if not moved:
            push = self.codes.get(label)
            if push is not None:
                self.press(push, event)
                self.release(push)

    def turn(self, code, step, event=None):
        if event is not None and event.state & 0x0001:
            step *= 10
        self.emu.inbox.append(('encoder', code, step))
        state = self.enc_items.get(code)
        if not state:
            return
        ring, mark, angle = state
        # A Digitakt encoder has 24 detents per turn: 15 degrees each. The
        # firmware's own step per detent is velocity-scaled, so the ring is
        # a record of detents sent, not of the value.
        state[2] = angle + step * (2 * math.pi / 24)
        self.canvas.itemconfigure(mark, state='normal')
        timers = self.__dict__.setdefault('_encoder_timers', {})
        if code in timers:
            self.after_cancel(timers[code])
        timers[code] = self.after(350, lambda: self.canvas.itemconfigure(mark, state='hidden'))
        x0, y0, x1, y1 = self.canvas.coords(ring)
        cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2
        a = state[2] - math.pi / 2
        self.canvas.coords(mark,
                           cx + math.cos(a) * (r - 11),
                           cy + math.sin(a) * (r - 11),
                           cx + math.cos(a) * (r - 7),
                           cy + math.sin(a) * (r - 7))

    # ------------------------------------------------------ Master Volume knob
    # The hardware's volume pot is analog (not in the firmware's code
    # table), so the knob is purely a panel-side control. Turning it applies
    # a software gain to the live output and to PLAY's replay; SAVE WAV
    # writes the recording as the firmware made it.
    _MV_STEP = 1.0 / 20        # 20 wheel clicks from silent to unity
    _MV_MAX = 1.5              # can push a little past unity; clipped at host

    def _paint_master_volume(self):
        """Redraw the knob's indicator line for the current value."""
        mv_x, mv_y, mv_r = MASTER_VOLUME
        a = master_volume_angle(self._mv_value, self._MV_MAX) - math.pi / 2
        self.canvas.coords(
            self._mv_mark,
            mv_x + math.cos(a) * (mv_r - 11),
            mv_y + math.sin(a) * (mv_r - 11),
            mv_x + math.cos(a) * (mv_r - 7),
            mv_y + math.sin(a) * (mv_r - 7))

    def _turn_master_volume(self, step, event=None):
        if event is not None and event.state & 0x0001:
            step *= 10
        self._mv_value = max(0.0, min(self._MV_MAX,
                                      self._mv_value + step * self._MV_STEP))
        self._paint_master_volume()
        self.emu.set_volume(self._mv_value)
        self.player.gain = self._mv_value

    def _mv_drag_start(self, event):
        self._mv_drag_y = event.y
        self._mv_drag_acc = 0.0

    def _mv_drag(self, event):
        """Vertical drag on the knob: up turns it up, 6px per step."""
        dy = getattr(self, '_mv_drag_y', event.y) - event.y
        self._mv_drag_y = event.y
        self._mv_drag_acc = getattr(self, '_mv_drag_acc', 0.0) + dy / 6.0
        step = int(self._mv_drag_acc)
        if step:
            self._mv_drag_acc -= step
            self._turn_master_volume(step)

    def _led_rgb(self, led):
        """The LED's colour, or None when it is dark (or not defined yet)."""
        rgb = self._leds.get(led)
        if rgb is None or max(rgb) < LED_DARK:
            return None
        return rgb

    def _paint(self, code):
        pressed = code in self.held or code in self.latched
        ring = self.enc_push_items.get(code)
        if ring is not None:
            self.canvas.itemconfigure(ring, state='normal' if pressed else 'hidden')
        extra = self.extra_items.get(code)
        if extra is not None and extra.winfo_exists():
            extra.configure(bg=self.LEGEND if pressed else FACE,
                            fg=BG if pressed else TEXT)
        item = self.items.get(code)
        if item is None:
            return
        cap, light = item
        label = self.key_labels[code]
        rgb = self._led_rgb(self.led_of.get(code))
        if pressed and rgb is None:
            rgb = tuple(int(self.LEGEND[i:i + 2], 16) for i in (1, 3, 5))
        self.canvas.itemconfigure(cap, image=self.skin.cap(label, pressed))
        self.canvas.itemconfigure(light, image=self.skin.light(label, rgb, pressed))

    def _draw_leds(self):
        """Repaint whatever the firmware's LED stream changed."""
        emu = self.emu
        version = getattr(emu, 'led_version', 0)
        if version == self._led_version:
            return
        self._led_version = version
        old, self._leds = self._leds, dict(getattr(emu, 'leds', {}) or {})
        for code, led in self.led_of.items():
            if old.get(led) != self._leds.get(led):
                self._paint(code)
        for led, dot in self.page_items:
            rgb = self._led_rgb(led)
            self.canvas.itemconfigure(
                dot, fill=_hex(rgb) if rgb else '#242316')

    # --------------------------------------------------------------- samples
    def load_samples(self):
        """LOAD SAMPLES: WAV files into the +Drive's /incoming. See SAMPLES
        in the module docstring. Nothing is stopped until the files have
        been checked and the user has confirmed; a file that cannot go on
        the card is named and left out."""
        from tkinter import filedialog, messagebox
        from emu import samples
        title = 'Load samples'
        emu = self.emu
        card = os.environ.get('DT2_PLUSDRIVE')
        if not card:
            messagebox.showinfo(title, 'This session has no +Drive image to '
                                'load samples onto.', parent=self)
            return
        if not emu.ready.is_set() or self._failure() or not emu.is_alive():
            messagebox.showinfo(title, 'Samples can be loaded once the '
                                'Digitakt is running.', parent=self)
            return
        files = filedialog.askopenfilenames(
            parent=self, title='Load samples onto the +Drive',
            filetypes=samples.WAV_TYPES)
        if not files:
            return
        try:
            plan = samples.plan(list(files), card)
        except samples.Error as exc:
            messagebox.showerror(title, 'No samples can go on the +Drive: '
                                 '%s.' % exc, parent=self)
            return
        if not plan.samples:
            messagebox.showerror(title, 'None of these can go on the +Drive:'
                                 '\n\n%s' % _rejections(plan), parent=self)
            return
        if not messagebox.askokcancel(title, load_question(plan, self.app),
                                      parent=self):
            return
        emu.release_card = True
        self._stop_emulator('saving the session before loading the samples')
        if emu.is_alive() or not emu.flushed:
            messagebox.showerror(title, 'The +Drive image could not be '
                                 'written safely (%s), so no samples were '
                                 'loaded.' % (emu.save_error or 'the emulator '
                                              'did not stop'), parent=self)
            self._close_soon()
            return
        try:
            samples.write(plan, progress=lambda s: self._say(
                'loading %s onto the +Drive ...' % s.name, AMBER))
        except Exception as exc:                       # noqa: BLE001
            print('[dtpanel] loading samples failed: %s' % exc, flush=True)
            messagebox.showerror(title, 'Loading stopped: %s. %d of %d '
                                 'samples were loaded.'
                                 % (exc, len(plan.written),
                                    len(plan.samples)), parent=self)
        self.samples_added = [name for name, _ino in plan.written]
        print('[dtpanel] loaded %d sample(s) into /%s on %s: %s'
              % (len(plan.written), samples.INCOMING, card,
                 ', '.join(self.samples_added) or '-'), flush=True)
        self._close_soon()

    def _close_soon(self):
        """Close the window once the current event is over, never from
        inside it. LOAD SAMPLES runs from a canvas item's <Button-1>
        binding; destroying the window there frees the canvas while Tk is
        still dispatching that click, and Tk then walks freed memory: Tcl
        panics with 'alloc: invalid block' and the process dies with
        0x80000003 (measured, frozen and from source)."""
        self.after_idle(self.destroy)

    # -------------------------------------------------------------- shutdown
    def _stop_emulator(self, why=None):
        """Stop the emulator thread at a step boundary and wait for its
        flush (and save, with save_on_exit). -> True once it has ended."""
        emu = getattr(self, 'emu', None)
        if emu is None or not emu.is_alive():
            return True
        emu.stop_flag.set()
        emu.pause.clear()
        if why:
            self._say(why, AMBER)
        return self._wait_for_worker(emu)

    def quit_all(self):
        """Stop the emulator thread, then tear the window down.

        Idempotent: it is called both from WM_DELETE_WINDOW and from the
        `finally` around mainloop, and either may run first.
        """
        player = getattr(self, 'player', None)
        if player is not None:
            player.stop()
        lights = getattr(self, 'keylight', None)
        if lights is not None:
            lights.restore()
        server, self.remote = getattr(self, 'remote', None), None
        if server is not None:
            server.stop()
        control, self.control = getattr(self, 'control', None), None
        if control is not None:
            control.stop()
        emu = getattr(self, 'emu', None)
        self._stop_emulator(
            'saving the session -- this window closes when it is written'
            if getattr(emu, 'save_on_exit', None) else None)
        try:
            self.destroy()
        except tk.TclError:          # already torn down
            pass

    def _say(self, text, fill=DIM):
        """Put `text` on the status line now, even with no mainloop running."""
        try:
            self.canvas.itemconfigure(self.status, text=text, fill=fill)
            self.update_idletasks()
        except (tk.TclError, AttributeError):
            pass

    def _wait_for_worker(self, emu):
        """Join the emulator thread. -> True once it has ended.

        The old fixed 5 s join gave up on a flush (and now a save) that was
        still running; then interpreter teardown freed Unicorn under the
        thread and the card was left half written. So the timeout applies
        only while the worker is stepping, where it can only mean a thread
        stuck inside Unicorn. Loading (which, with stop_flag set, goes
        straight on to the flush and save) and `finishing` are waited out
        in full: both are bounded work, and abandoning either loses the
        session.
        """
        deadline = time.monotonic() + self.STOP_TIMEOUT
        while emu.is_alive():
            emu.join(0.1)
            if emu.finishing.is_set() or not emu.ready.is_set():
                deadline = time.monotonic() + self.STOP_TIMEOUT
                continue
            if time.monotonic() >= deadline:
                print('[dtpanel] the emulator did not stop within %.0f s; '
                      'leaving it' % self.STOP_TIMEOUT, flush=True)
                return False
        return True

    def exit_code(self):
        """-> main()'s return code: 0 clean, 1 emulator failed, 2 not saved,
        4 (INCOMPATIBLE) the snapshot is from another build, 5 (LOAD_FAILED)
        the snapshot could not be opened, 6 (SAMPLES_ADDED) LOAD SAMPLES
        changed the card."""
        # First: whatever else happened, the card now holds files no
        # snapshot knows about, and only a rebuild can show them.
        if getattr(self, 'samples_added', None):
            return SAMPLES_ADDED
        emu = self.emu
        # Before `error`, which is also set: the launcher offers a rebuild
        # for this one rather than reporting a failure.
        if getattr(emu, 'incompatible', False):
            return INCOMPATIBLE
        # The snapshot itself would not open. Not a missing firmware or device
        # file (config.NotFound, device.DeviceError): another snapshot would
        # fail the same way, so those stay plain failures.
        if (emu.error
                and getattr(emu, 'stats', {}).get('status') == 'failed to load'
                and not str(emu.error).startswith(('DeviceError', 'NotFound'))):
            return LOAD_FAILED
        if emu.error or emu.is_alive():
            return 1
        if emu.save_on_exit and not emu.saved:
            return 2
        return 0

    # --------------------------------------------------------------- failure
    def _failure(self):
        """-> why the emulator is not running, or None while it is healthy."""
        emu = self.emu
        if emu.error:
            return emu.error
        if not emu.is_alive() and not emu.stop_flag.is_set():
            return 'the emulator thread ended unexpectedly; see the log'
        return None

    def _show_failure(self, text):
        """Draw the emulator's failure over the screen and on the status line.

        A window that simply stops -- a black screen and 'AUDIO off' -- is
        what an unrecognised firmware, a snapshot from another build or a
        halt all used to look like. The whole message goes over the screen,
        where there is room for it; the status line gets its first line.
        """
        if text == self._error_shown:
            return
        self._error_shown = text
        loading = self.emu.stats.get('status') == 'failed to load'
        incompatible = getattr(self.emu, 'incompatible', False)
        if incompatible:
            head = 'This snapshot was made by a different build.'
        elif loading:
            head = 'The snapshot could not be opened.'
        else:
            head = 'The emulator stopped.'
        body = text if len(text) <= 1500 else text[:1500] + ' ...'
        c = self.canvas
        c.itemconfigure(self.err_text, text='%s\n\n%s' % (head, body),
                        state='normal')
        c.itemconfigure(self.err_box, state='normal')
        c.tag_raise(self.err_box)
        c.tag_raise(self.err_text)
        if incompatible:
            # Not a crash, so not worded or coloured as one: the message
            # leads with its own verdict ('incompatible snapshot: rebuild
            # needed.').
            c.itemconfigure(self.status, fill=AMBER, text=_first_line(text))
        else:
            c.itemconfigure(self.status, fill=ERR,
                            text='EMULATOR STOPPED  ' + _first_line(text))

    # ------------------------------------------------------------------ loop
    def tick(self):
        # The reschedule is in `finally` on purpose. An exception anywhere in
        # here -- a bad bind, a half-built control surface -- otherwise skips
        # the `after` and silently kills the refresh loop for good, leaving a
        # window that looks alive but never updates again.
        try:
            self._tick()
        except Exception as exc:                       # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.canvas.itemconfigure(self.status,
                                      text='tick error: %s' % exc)
        finally:
            self.after(40, self.tick)

    def _names_ready(self):
        """-> True once the controls can be named: the firmware's own table
        has been read, or -- for a product whose image has no static table,
        such as the Digitone -- the device file's measured labels are in
        and the emulator is up."""
        emu = self.emu
        if getattr(emu, 'button_names', None):
            return True
        labels = getattr(getattr(emu, 'device', None), 'labels', None)
        return bool(labels) and emu.ready.is_set()

    def _tick(self):
        emu = self.emu
        if not self._named and self._names_ready():
            # MEASURED names win over the firmware's own table, and must
            # DISPLACE it: this layout places a button by its label, and the
            # panel-test table calls code 25 "PLAY" while the code that
            # actually starts the transport is 10. Letting both claim the
            # label would leave whichever lost sending a trig from the PLAY
            # button, which is the bug this fixes.
            measured = dict(getattr(getattr(emu, 'device', None),
                                    'labels', None) or {})
            taken = set(measured.values())
            names = dict(measured)
            for code, name in (emu.button_names or {}).items():
                if code in names or name in taken:
                    continue
                names[code] = name
            self.codes = {n: c for c, n in names.items()}
            self.enc_codes = {n: c for c, n in
                              (getattr(emu, 'encoder_names', None)
                               or {}).items()}
            # Set before building, not after: if a bind fails the surface is
            # drawn once and imperfect, rather than retried every 40ms for
            # the life of the window.
            self._named = True
            self._build_controls()
        if self._named:
            self._draw_leds()
        self._draw_audio_status()
        fb = getattr(emu, 'fb', None)
        if fb:
            self.draw_screen(fb)
        failure = self._failure()
        if failure:
            self._show_failure(failure)
            return
        held = ', '.join(sorted(self.codes and
                                [n for n, c in self.codes.items()
                                 if c in self.held] or [])) or '-'
        text = ('held: %s      (shift-click latches until Shift is up, '
                'Del releases all)' % held)
        if emu.device_error:
            text += '      no controls: ' + _first_line(emu.device_error, 90)
        self.canvas.itemconfigure(self.status, text=text, fill=DIM)


def _rejections(plan, limit=12):
    lines = ['%s: %s' % (os.path.basename(path), why)
             for path, why in plan.rejected[:limit]]
    if len(plan.rejected) > limit:
        lines.append('... and %d more' % (len(plan.rejected) - limit))
    return '\n'.join(lines)


def load_question(plan, app, limit=12):
    """-> LOAD SAMPLES' confirmation text for `plan` (emu.samples.Plan)."""
    n = len(plan.samples)
    lines = ['Load %d sample%s into /incoming on the +Drive?'
             % (n, '' if n == 1 else 's'), '']
    for s in plan.samples[:limit]:
        lines.append('    %s%s  (%.2f s, %d Hz)'
                     % (s.name, '  [renamed: that name is taken]'
                        if s.renamed else '', s.seconds, s.rate))
    if n > limit:
        lines.append('    ... and %d more' % (n - limit))
    if plan.rejected:
        lines += ['', 'Left out:', _rejections(plan, limit)]
    lines += ['', 'The Digitakt only sees new samples after a restart, so '
              'this session is saved and closed first.']
    if app:
        lines.append('digiemu then rebuilds it with the samples on the '
                     '+Drive and opens it again, in about 15 seconds.')
    else:
        lines.append('The window closes; rebuild the snapshots to see the '
                     'samples.')
    lines.append('Changes to the project that are not saved on the Digitakt '
                 'may be lost.')
    return '\n'.join(lines)


class _Stop(Exception):
    """argparse asked to exit; main() returns `code` instead."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _Parser(argparse.ArgumentParser):
    # argparse exits the process on --help and on a bad argument. main() is
    # also called in-process, by the portable app's worker, which needs a
    # return code rather than a SystemExit; and a windowed build has no
    # stderr for the usage text, so it goes to stdout (the worker's log).
    def exit(self, status=0, message=None):
        if message:
            print(message, end='', flush=True)
        raise _Stop(status)

    def error(self, message):
        self.print_usage(sys.stdout)
        self.exit(USAGE_ERROR, '%s: error: %s\n' % (self.prog, message))


def parse_args(argv, prog='python -m emu.dtpanel',
               description='The Digitakt mk1 front panel.'):
    """-> Namespace(snapshot, syx, save_on_exit, audio, app). Raises
    _Stop."""
    ap = _Parser(prog=prog, description=description)
    ap.add_argument('snapshot', nargs='?',
                    help='the snapshot to open (default: the firmware\'s own, '
                         'from emu.run.paths_for)')
    # The old form, `dtpanel SNAP SYX`, still works.
    ap.add_argument('legacy_syx', nargs='?', help=argparse.SUPPRESS)
    ap.add_argument('--syx', help='the firmware .syx (default: DT2_SYX, or '
                                  'the only .syx in the working directory)')
    ap.add_argument('--save-on-exit', metavar='PATH',
                    help='on a clean close, save the session here (written '
                         'to PATH.tmp, then renamed over PATH)')
    # --no-audio: skip the audio model, for speed or for a snapshot whose
    # audio DMA is not set up (the window says so either way).
    ap.add_argument('--no-audio', dest='audio', action='store_false',
                    help='skip the audio model')
    ap.add_argument('--app', action='store_true',
                    help='run by the portable app, which rebuilds and '
                         'reopens after LOAD SAMPLES')
    args = ap.parse_args(argv)
    if args.legacy_syx:
        if args.syx and args.syx != args.legacy_syx:
            ap.error('two firmware files given: %s and %s'
                     % (args.legacy_syx, args.syx))
        args.syx = args.syx or args.legacy_syx
    del args.legacy_syx
    return args


def main(argv):
    """Run the panel until its window closes. -> the process exit code.

    0 on a clean close -- with --save-on-exit, only once the session is
    written; 1 if the emulator failed while running (its `error`: the run
    halted or crashed); 2 if the session could not be saved; 4
    (INCOMPATIBLE) if the snapshot was made by a different build -- rebuild
    needed; 5 (LOAD_FAILED) if the snapshot, the firmware or the device
    files could not be opened; 6 (SAMPLES_ADDED) if LOAD SAMPLES put samples
    on the card -- rebuild to see them; 64 (USAGE_ERROR) for arguments it
    cannot parse.
    """
    return run(argv, DigitaktPanel)


def run(argv, panel_cls, prog='python -m emu.dtpanel',
        description='The Digitakt mk1 front panel.'):
    """main() for any product's window class (emu/dnpanel.py passes the
    Digitone's). Same arguments and return codes."""
    try:
        args = parse_args(argv, prog, description)
    except _Stop as stop:
        return stop.code
    # The panel is for using the instrument, so its +Drive persists: whatever
    # the firmware writes to the card lands in plusdrive.img in the working
    # directory -- unless DT2_PLUSDRIVE is set, which always wins (even set
    # empty: an in-memory card).
    if 'DT2_PLUSDRIVE' not in os.environ:
        os.environ['DT2_PLUSDRIVE'] = 'plusdrive.img'
    snap = args.snapshot
    if not snap:
        # Only the tested build keeps the historic top-level snapshot path;
        # every other firmware gets its own directory, so ask run.paths_for
        # rather than assuming the flat name and failing on a product whose
        # snapshots are one level down.
        try:
            from emu import run as _run
            snap, _prefix = _run.paths_for(config.firmware(args.syx))
        except SystemExit as exc:    # config.NotFound: no window to show it
            print('[dtpanel] %s' % exc, flush=True)
            return 1
    app = panel_cls(snap, syx=args.syx, audio=args.audio,
                    save_on_exit=args.save_on_exit, app=args.app)
    try:
        app.mainloop()
    finally:
        # Ctrl-C, the X server going away, or an exception out of mainloop
        # all bypass WM_DELETE_WINDOW and would leave the worker inside
        # Unicorn while the interpreter frees it.
        app.quit_all()
    code = app.exit_code()
    if code == SAMPLES_ADDED and not args.app:
        print('[dtpanel] the snapshots predate the new samples: rebuild them '
              'from the cold boot to see them on the %s'
              % getattr(panel_cls, 'PRODUCT', 'Digitakt'), flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
