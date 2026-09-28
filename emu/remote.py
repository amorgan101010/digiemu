"""The front panel in a browser on the local network: an iPad, typically.

The panel window (emu/dtpanel.py, emu/dnpanel.py) starts this when its REMOTE
button is on. A browser that opens the address it shows gets the same
faceplate, drawn from the same layout the window placed, with the live OLED
and LEDs, and plays the panel with multitouch: hold a trig and turn an
encoder for a parameter lock, or FUNC plus a key, with fingers rather than
shift-click latches.

The protocol follows Gearmulator's Machinedrum/Monomachine remote panel in
shape only; nothing is taken from it.

    GET /       the page (PAGE, below: one self-contained HTML file, so a
                frozen build needs no data files for it)
    GET /ws     WebSocket. The server sends
                  'L <json>'      the layout, once the controls are named
                  binary 'S', 1024 bytes of OLED (128x64, 1 bit a pixel,
                  row-major, MSB first), then r, g, b for each LED in the
                  layout's `leds` order (0, 0, 0 when dark or unknown)
                  'A <rate>'      the sample rate of the sound it can
                                  stream; 0 while there is none
                  binary 'A', then 16-bit LE stereo PCM: the emulator's
                                  output, to a page that asked for it
                The page sends text:
                  hello            send the layout and state now
                  b <code> <1|0>   press / release a key
                  e <code> <n>     turn an encoder n detents (signed)
                  r                release every key this page holds
                  a <1|0>          stream the sound to this page, or stop

Sound goes only to pages that ask, and only while the emulator plays live
(accelerated): it is taken before the window's mute, so the PC can be
silenced while an iPad plays. The page schedules each block ahead of its
own clock (Web Audio's AudioBufferSourceNode: an AudioWorklet needs HTTPS)
and keeps a cushion against Wi-Fi's jitter, ?cushion=<ms> in the address.

Input goes straight onto the emulator's inbox, as the window's own does. Each
connection remembers what it holds and lets go of it when the connection
ends, and a page that stops answering pings (an iPad put to sleep holds the
socket half open) is dropped the same way, so a key is never left stuck
down. The emulator's key state is one bit per key, not a count: a remote
release also lets go of the same key held in the window.

There is no authentication: anyone who can reach the port can play the
panel. It starts with the window unless REMOTE was switched off, and is
meant for a trusted home network.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

W, H = 128, 64
SCREEN_BYTES = W * H // 8
_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'   # RFC 6455
_BITS = bytes([0x30] + [0x31] * 255)              # pixel -> '0' / '1'

PORTS_TRIED = 10          # the first port and the next nine
FRAME_S = 1 / 30          # publish at most this often
PING_S = 4.0              # ping an idle page this often
DEAD_S = 12.0             # no reply for this long: the page is gone
MAX_DETENTS = 96          # one message's turn, before splitting
SOUND_S = 0.02            # send the sound this often
SOUND_KEEP_S = 0.5        # most sound held for a page, before the oldest goes


def accept_key(key):
    """-> Sec-WebSocket-Accept for a client's Sec-WebSocket-Key."""
    digest = hashlib.sha1(key.strip().encode('ascii') + _GUID).digest()
    return base64.b64encode(digest).decode('ascii')


def encode_frame(opcode, payload):
    """-> one unmasked, unfragmented server frame."""
    n = len(payload)
    if n < 126:
        head = struct.pack('!BB', 0x80 | opcode, n)
    elif n < 1 << 16:
        head = struct.pack('!BBH', 0x80 | opcode, 126, n)
    else:
        head = struct.pack('!BBQ', 0x80 | opcode, 127, n)
    return head + payload


def pack_screen(fb):
    """-> the OLED as SCREEN_BYTES bytes, 1 bit a pixel; blank without one."""
    if not fb or len(fb) != W * H:
        return bytes(SCREEN_BYTES)
    return int(bytes(fb).translate(_BITS), 2).to_bytes(SCREEN_BYTES, 'big')


def state_frame(fb, leds, order):
    """-> the binary state message: 'S', the OLED, each LED of `order`."""
    out = bytearray(b'S')
    out += pack_screen(fb)
    for led in order:
        rgb = leds.get(led) if leds else None
        out += bytes(rgb[:3]) if rgb else b'\0\0\0'
    return bytes(out)


def split_turn(steps, per_event):
    """-> `steps` detents as events of at most `per_event` each, same sign.

    The emulator multiplies a detent by the device's encoder_counts and
    clamps the result to what the wire carries (127), so one large event
    would be cut short rather than turned."""
    per_event = max(1, per_event)
    sign = 1 if steps > 0 else -1
    left = abs(steps)
    out = []
    while left:
        n = min(left, per_event)
        out.append(sign * n)
        left -= n
    return out


def lan_address():
    """-> this machine's address on the local network, as best known.

    Connecting a UDP socket sends nothing; it only makes the OS pick the
    interface it would route by."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


class _Client:
    def __init__(self, sock):
        self.sock = sock
        self.send_lock = threading.Lock()
        self.lock = threading.Lock()
        self.held = set()
        self.ready = False        # has asked (hello) and been sent the layout
        self.last_state = None
        self.rate_sent = 0        # the 'A <rate>' last told it; 0 is unsaid
        self.sound = False        # has asked for the sound (a 1)
        self.heard = time.monotonic()
        self.closed = False


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Rebinding a port in TIME_WAIT after the window reopens needs it; on
    # Windows it would instead let two processes share the port, which
    # defeats trying the next one.
    allow_reuse_address = os.name != 'nt'


class RemotePanel:
    """Serves the panel of `emu` (an emu.gui.Emulator, or anything with its
    inbox, fb, version, leds and led_version) to browsers.

    `layout()` -> the layout dict, or None until the window has named its
    controls: {'controls': [...], 'leds': [led id, ...], ...}; each control
    is a dict with 'k' 'b' (key: code, x, y, w, h), 'e' (encoder: code, x,
    y, r, and push: the code of its push switch, or None) or 'd' (an LED
    with no key). emu/dtpanel.py builds it."""

    def __init__(self, emu, layout, port, product='Digitakt'):
        self.emu = emu
        self.layout = layout
        self.first_port = port
        self.product = product
        self.port = None
        self.error = None
        self._httpd = None
        self._threads = []
        self._stop = threading.Event()
        self._clients = []
        self._lock = threading.Lock()
        self._sent_layout = None
        self._pcm = bytearray()       # sound not yet sent
        self._pcm_lock = threading.Lock()
        self._listening = False       # some page wants the sound

    # ------------------------------------------------------------ lifecycle
    def start(self):
        """Bind the first free port from `first_port` on. -> True if serving;
        False with `error` set if none would bind."""
        remote = self
        ports = ([0] if self.first_port == 0 else
                 range(self.first_port, self.first_port + PORTS_TRIED))
        for port in ports:
            try:
                httpd = _Server(('0.0.0.0', port), _handler(remote))
            except OSError as exc:
                self.error = str(exc)
                continue
            self._httpd = httpd
            self.port = httpd.server_address[1]
            self.error = None
            break
        else:
            return False
        for target in (self._httpd.serve_forever, self._publish,
                       self._stream):
            t = threading.Thread(target=target, daemon=True,
                                 name='remote-panel')
            t.start()
            self._threads.append(t)
        taps = getattr(self.emu, 'audio_taps', None)
        if taps is not None:
            self.emu.audio_taps = taps + (self.feed_sound,)
        return True

    def stop(self):
        """Stop serving and let go of everything any page holds."""
        taps = getattr(self.emu, 'audio_taps', None)
        if taps is not None:
            self.emu.audio_taps = tuple(t for t in taps
                                        if t != self.feed_sound)
        self._stop.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        for client in self.clients():
            self._drop(client)

    @property
    def running(self):
        return self._httpd is not None

    @property
    def url(self):
        if self.port is None:
            return None
        return 'http://%s:%d/' % (lan_address(), self.port)

    def clients(self):
        with self._lock:
            return list(self._clients)

    # ---------------------------------------------------------------- input
    def _codes(self, kind):
        lay = self.layout()
        if not lay:
            return set()
        return {c['code'] for c in lay['controls'] if c['k'] == kind}

    def handle(self, client, text):
        """Act on one text message from a page."""
        parts = text.split()
        if not parts:
            return
        verb, args = parts[0], parts[1:]
        inbox = self.emu.inbox
        try:
            if verb == 'hello':
                client.last_state = None
            elif verb == 'a' and len(args) == 1:
                self._want_sound(client, args[0] == '1')
            elif verb == 'b' and len(args) == 2:
                code, down = int(args[0]), args[1] == '1'
                if code not in self._codes('b'):
                    return
                with client.lock:
                    if client.closed:
                        return
                    if down and code not in client.held:
                        client.held.add(code)
                        inbox.append(('press', code, 0))
                    elif not down and code in client.held:
                        client.held.discard(code)
                        inbox.append(('release', code, 0))
            elif verb == 'e' and len(args) == 2:
                code, steps = int(args[0]), int(args[1])
                if code not in self._codes('e') or not steps:
                    return
                steps = max(-MAX_DETENTS, min(MAX_DETENTS, steps))
                dev = getattr(self.emu, 'device', None)
                counts = getattr(dev, 'encoder_counts', 1) or 1
                for n in split_turn(steps, 127 // counts):
                    inbox.append(('encoder', code, n))
            elif verb == 'r':
                self._release(client)
        except ValueError:
            return

    def _release(self, client):
        with client.lock:
            self._let_go(client)
    def _let_go(self, client):
        """Release what `client` holds. Its lock must be held."""
        for code in sorted(client.held):
            self.emu.inbox.append(('release', code, 0))
        client.held.clear()

    # --------------------------------------------------------------- output
    def _send(self, client, opcode, payload):
        """-> False, and the client dropped, if it could not be sent to."""
        if client.closed:
            return False
        try:
            with client.send_lock:
                client.sock.sendall(encode_frame(opcode, payload))
            return True
        except OSError:
            self._drop(client)
            return False

    def _drop(self, client):
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        with client.lock:
            if client.closed:
                return
            client.closed = True
            self._let_go(client)
        self._want_sound(client, False)
        try:
            client.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _publish(self):
        """Push the layout and state to every page whenever they change.
        An exception is reported and the loop goes on: left to end the
        thread, it would freeze every page with nothing said."""
        last_ping = [time.monotonic()]
        while not self._stop.wait(FRAME_S):
            try:
                self._publish_once(last_ping)
            except Exception as exc:                   # noqa: BLE001
                print('[remote] publishing failed: %r' % exc, flush=True)

    def _publish_once(self, last_ping):
        """One pass of _publish. `last_ping` is a one-item list: when the
        pages were last pinged."""
        lay = self.layout()
        now = time.monotonic()
        ping = now - last_ping[0] >= PING_S
        if ping:
            last_ping[0] = now
        if lay is not None and lay is not self._sent_layout:
            self._sent_layout = lay
            for client in self.clients():
                client.ready = False
        state = None
        rate = self.sound_rate()
        for client in self.clients():
            if now - client.heard > DEAD_S:
                self._drop(client)
                continue
            if ping and not self._send(client, 0x9, b''):
                continue
            if rate != client.rate_sent:
                if not self._send(client, 0x1, b'A %d' % rate):
                    continue
                client.rate_sent = rate
            if lay is None:
                continue
            if not client.ready:
                text = 'L ' + json.dumps(lay, separators=(',', ':'))
                if not self._send(client, 0x1, text.encode()):
                    continue
                client.ready = True
            if state is None:
                emu = self.emu
                state = state_frame(getattr(emu, 'fb', None),
                                    getattr(emu, 'leds', None),
                                    lay['leds'])
            if state != client.last_state:
                if self._send(client, 0x2, state):
                    client.last_state = state

    # ---------------------------------------------------------------- sound
    def sound_rate(self):
        """-> the sample rate of the sound a page can have; 0 if none (the
        emulator is not playing live, or cannot be tapped)."""
        emu = self.emu
        cfg = getattr(emu, 'audio_cfg', None)
        if (not cfg or not getattr(emu, 'audio_live', False)
                or getattr(emu, 'audio_taps', None) is None):
            return 0
        return cfg['rate']

    def _want_sound(self, client, on):
        with self._pcm_lock:
            client.sound = on and not client.closed
            self._listening = any(c.sound for c in self.clients())
            if not self._listening:
                del self._pcm[:]

    def feed_sound(self, pcm):
        """Take a block of live PCM (16-bit LE stereo). Called by the
        emulator's worker thread, so it only queues, and does nothing while
        no page listens."""
        if not self._listening:
            return
        with self._pcm_lock:
            self._pcm += pcm
            cfg = getattr(self.emu, 'audio_cfg', None) or {}
            keep = int(SOUND_KEEP_S * cfg.get('rate', 48000)) * 4
            over = len(self._pcm) - keep
            if over > 0:
                del self._pcm[:over + (-over % 4)]

    def _stream(self):
        """Send the queued sound to every page that asked for it."""
        while not self._stop.wait(SOUND_S):
            try:
                with self._pcm_lock:
                    if not self._pcm:
                        continue
                    frame = b'A' + bytes(self._pcm)
                    del self._pcm[:]
                for client in self.clients():
                    if client.sound:
                        self._send(client, 0x2, frame)
            except Exception as exc:                   # noqa: BLE001
                print('[remote] streaming sound failed: %r' % exc,
                      flush=True)

    # ------------------------------------------------------------ websocket
    def serve_socket(self, sock):
        """Run one page's WebSocket until it closes. Called on its own
        request thread, after the handshake."""
        client = _Client(sock)
        with self._lock:
            self._clients.append(client)
        sock.settimeout(1.0)
        try:
            while not client.closed and not self._stop.is_set():
                frame = _read_frame(sock, lambda: client.closed)
                if frame is None:
                    continue
                fin, opcode, payload = frame
                client.heard = time.monotonic()
                if opcode == 0x8:
                    self._send(client, 0x8, payload[:2])
                    break
                if opcode == 0x9:
                    self._send(client, 0xA, payload)
                elif opcode == 0x1:
                    self.handle(client, payload.decode('utf-8', 'replace'))
        except (OSError, EOFError):
            pass
        finally:
            self._drop(client)


def _recv(sock, n, gone):
    """-> exactly n bytes; waits through timeouts until `gone()`."""
    out = bytearray()
    while len(out) < n:
        try:
            chunk = sock.recv(n - len(out))
        except socket.timeout:
            if gone():
                raise EOFError
            continue
        if not chunk:
            raise EOFError
        out += chunk
    return bytes(out)


def _read_frame(sock, gone):
    """-> (fin, opcode, payload), or None if nothing arrived for a while."""
    try:
        head = sock.recv(2)
    except socket.timeout:
        return None
    if not head:
        raise EOFError
    if len(head) == 1:
        head += _recv(sock, 1, gone)
    b0, b1 = head
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack('!H', _recv(sock, 2, gone))[0]
    elif n == 127:
        n = struct.unpack('!Q', _recv(sock, 8, gone))[0]
    if n > 1 << 16:              # nothing the page sends is near this
        raise EOFError
    mask = _recv(sock, 4, gone) if b1 & 0x80 else None
    payload = _recv(sock, n, gone)
    if mask:
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return bool(b0 & 0x80), b0 & 0x0F, payload


def _handler(remote):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'digiemu-remote'
        # The default, HTTP/1.0, makes the handshake answer 'HTTP/1.0 101':
        # Chromium takes it, but Firefox will not upgrade such a connection
        # and the page reconnects forever.
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):     # quiet: one line per GET
            pass

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path == '/ws':
                return self._websocket()
            if path in ('/', '/index.html'):
                body = PAGE.replace('__PRODUCT__', remote.product).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
            self.send_error(404)
            return None

        def _websocket(self):
            key = self.headers.get('Sec-WebSocket-Key')
            if (not key or 'websocket' not in
                    self.headers.get('Upgrade', '').lower()):
                self.send_error(400)
                return
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept_key(key))
            self.end_headers()
            self.wfile.flush()
            print('[remote] page connected from %s' % self.client_address[0],
                  flush=True)
            remote.serve_socket(self.connection)
            self.close_connection = True

    return Handler


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<title>__PRODUCT__ remote</title>
<style>
:root{--bg:#0b0d10;--face:#1c2027;--edge:#2c323b;--text:#c9d3e0;--dim:#6b7789;
  --amber:#ffb638;--lit:#3f4b5c}
html,body{margin:0;height:100%;background:var(--bg);overflow:hidden;
  touch-action:none;-webkit-user-select:none;user-select:none;
  -webkit-touch-callout:none;-webkit-tap-highlight-color:transparent;
  font-family:-apple-system,Helvetica,Arial,sans-serif}
#stage{position:absolute;left:0;top:0;transform-origin:0 0}
.key{position:absolute;box-sizing:border-box;border:1px solid var(--edge);
  border-radius:7px;background:var(--face);color:var(--text);
  display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:13px;letter-spacing:.02em}
.key.held{background:var(--lit);border-color:#5b6779}
.sub{position:absolute;color:var(--dim);font-size:10px;text-align:center;
  pointer-events:none;white-space:nowrap}
.enc{position:absolute;border-radius:50%;box-sizing:border-box;
  background:#171b21;border:2px solid #3c444f}
.enc.held{border-color:var(--amber)}
.mark{position:absolute;left:50%;top:6px;width:4px;margin-left:-2px;
  background:var(--text);border-radius:2px;transform-origin:50% 100%}
.dot{position:absolute;width:10px;height:10px;border-radius:50%;
  box-sizing:border-box;border:1px solid var(--edge);background:#20252c}
#oled{position:absolute;image-rendering:pixelated;image-rendering:crisp-edges;
  background:#0c0e12;border-radius:2px;box-shadow:0 0 0 14px #05070a,
  0 0 0 15px #39414d}
#status{position:fixed;right:12px;bottom:8px;color:var(--dim);font-size:12px}
#status.bad{color:var(--amber)}
#sound{position:fixed;left:12px;bottom:6px;font:inherit;font-size:12px;
  color:var(--dim);background:var(--face);border:1px solid var(--edge);
  border-radius:6px;padding:4px 10px}
#sound.on{color:var(--amber);border-color:var(--amber)}
#sound[hidden]{display:none}
</style>
</head>
<body>
<div id="stage"></div>
<div id="status">connecting</div>
<button id="sound" hidden>sound off</button>
<script>
'use strict';
const stage = document.getElementById('stage');
const status = document.getElementById('status');
const ON = [0xe8, 0xf6, 0xff], OFF = [0x0c, 0x0e, 0x12];
let ws = null, layout = null, oled = null, octx = null, img = null;
let retry = 500;
const keys = new Map();       // code -> {el, text, led, tint}
const dots = [];              // [el, led index]
const pointers = new Map();   // pointerId -> {kind, code, ...}
let ledRgb = [];

function say(text, bad) { status.textContent = text; status.className = bad ? 'bad' : ''; }
function send(text) { if (ws && ws.readyState === 1) ws.send(text); }

function connect() {
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    retry = 500; say('connected'); send('hello');
    if (soundOn) send('a 1');
  };
  ws.onclose = () => {
    letGoAll(false);
    soundRate(0);
    say('reconnecting', true);
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 4000);
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      if (ev.data.startsWith('L ')) build(JSON.parse(ev.data.slice(2)));
      else if (ev.data.startsWith('A ')) soundRate(+ev.data.slice(2));
    } else {
      const b = new Uint8Array(ev.data);
      if (b[0] === 0x41) sound(ev.data);
      else state(b);
    }
  };
}

function el(cls, x, y, w, h) {
  const d = document.createElement('div');
  d.className = cls;
  Object.assign(d.style, {left: x + 'px', top: y + 'px', width: w + 'px', height: h + 'px'});
  stage.appendChild(d);
  return d;
}

function build(lay) {
  layout = lay;
  stage.textContent = '';
  keys.clear(); dots.length = 0;
  const [bx, by, bw, bh] = lay.bounds;
  stage.style.width = bw + 'px'; stage.style.height = bh + 'px';
  stage.dataset.ox = bx; stage.dataset.oy = by;
  const at = (x, y) => [x - bx, y - by];
  const s = lay.screen;
  oled = document.createElement('canvas');
  oled.id = 'oled'; oled.width = 128; oled.height = 64;
  const [sx, sy] = at(s.x, s.y);
  Object.assign(oled.style, {left: sx + 'px', top: sy + 'px', width: s.w + 'px', height: s.h + 'px'});
  stage.appendChild(oled);
  octx = oled.getContext('2d');
  img = octx.createImageData(128, 64);
  for (const c of lay.controls) {
    if (c.k === 'b') {
      const [x, y] = at(c.x, c.y);
      const k = el('key', x, y, c.w, c.h);
      k.textContent = c.label.slice(0, 13);
      if (c.h < 24) k.style.fontSize = '10px';
      if (c.tint) k.style.color = c.tint;
      if (c.sub) {
        const t = el('sub', x - 20, y + c.h + 3, c.w + 40, 12);
        t.textContent = c.sub;
      }
      keys.set(c.code, {el: k, led: c.led, tint: c.tint || null});
      k.addEventListener('pointerdown', (e) => keyDown(e, c.code));
    } else if (c.k === 'e') {
      const [x, y] = at(c.x - c.r, c.y - c.r);
      const e = el('enc', x, y, 2 * c.r, 2 * c.r);
      const m = document.createElement('div');
      m.className = 'mark'; m.style.height = (c.r - 10) + 'px';
      e.appendChild(m);
      e._angle = 0;
      e.addEventListener('pointerdown', (ev) => encDown(ev, c.code, c.push, e, m));
    } else if (c.k === 'd') {
      const [x, y] = at(c.x - 5, c.y - 5);
      dots.push([el('dot', x, y, 10, 10), c.led]);
    }
  }
  fit();
  paintAll();
}

function fit() {
  if (!layout) return;
  const [, , bw, bh] = layout.bounds;
  const k = Math.min(innerWidth / bw, innerHeight / bh);
  stage.style.transform = 'translate(' + (innerWidth - bw * k) / 2 + 'px,' +
    (innerHeight - bh * k) / 2 + 'px) scale(' + k + ')';
}
addEventListener('resize', fit);

function state(b) {
  if (!layout || b[0] !== 0x53) return;
  const px = img.data;
  for (let i = 0; i < 1024; i++) {
    const v = b[1 + i];
    for (let bit = 0; bit < 8; bit++) {
      const c = (v >> (7 - bit)) & 1 ? ON : OFF, o = (i * 8 + bit) * 4;
      px[o] = c[0]; px[o + 1] = c[1]; px[o + 2] = c[2]; px[o + 3] = 255;
    }
  }
  octx.putImageData(img, 0, 0);
  ledRgb = [];
  for (let i = 0, o = 1025; o + 2 < b.length; i++, o += 3) ledRgb.push([b[o], b[o + 1], b[o + 2]]);
  paintAll();
}

const hex = (c) => '#' + c.map((v) => v.toString(16).padStart(2, '0')).join('');
const lit = (i) => { const c = i == null ? null : ledRgb[i]; return c && Math.max(...c) > 24 ? c : null; };

function paint(code) {
  const k = keys.get(code);
  if (!k) return;
  const s = k.el.style, rgb = lit(k.led);
  let text = k.tint || '';
  if (k.el.classList.contains('held') || !rgb) {
    s.background = ''; s.borderColor = '';
  } else {
    const face = [0x1c, 0x20, 0x27].map((f, j) => Math.round(f + (rgb[j] - f) * 0.6));
    s.background = hex(face); s.borderColor = hex(rgb);
    if ((0.2126 * face[0] + 0.7152 * face[1] + 0.0722 * face[2]) / 255 > 0.45) text = '#0b0d10';
  }
  s.color = text;
}

function paintAll() {
  for (const code of keys.keys()) paint(code);
  for (const [d, led] of dots) { const c = lit(led); d.style.background = c ? hex(c) : ''; }
}

function keyDown(e, code) {
  e.preventDefault();
  e.currentTarget.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, {kind: 'b', code});
  keys.get(code).el.classList.add('held');
  paint(code);
  send('b ' + code + ' 1');
}

function encDown(e, code, push, ring, mark) {
  e.preventDefault();
  ring.setPointerCapture(e.pointerId);
  ring.classList.add('held');
  pointers.set(e.pointerId, {kind: 'e', code, push, ring, mark, x: e.clientX, y: e.clientY,
                             acc: 0, moved: false, t: Date.now()});
}

function move(e) {
  const p = pointers.get(e.pointerId);
  if (!p || p.kind !== 'e') return;
  const k = stage.getBoundingClientRect().width / layout.bounds[2];
  // up or right turns clockwise; 6 panel pixels a detent, as in the window
  p.acc += ((e.clientX - p.x) - (e.clientY - p.y)) / k / 6;
  p.x = e.clientX; p.y = e.clientY;
  const n = Math.trunc(p.acc);
  if (n) {
    p.moved = true;
    p.acc -= n;
    send('e ' + p.code + ' ' + n);
    p.ring._angle += n * 15;
    p.mark.style.transform = 'rotate(' + p.ring._angle + 'deg)';
  }
}

function up(e) {
  const p = pointers.get(e.pointerId);
  if (!p) return;
  pointers.delete(e.pointerId);
  if (p.kind === 'b') {
    keys.get(p.code).el.classList.remove('held');
    paint(p.code);
    send('b ' + p.code + ' 0');
  } else {
    p.ring.classList.remove('held');
    // a tap that turned nothing pushes the knob, as on the hardware
    if (!p.moved && p.push != null && e.type === 'pointerup' && Date.now() - p.t < 400) {
      send('b ' + p.push + ' 1');
      send('b ' + p.push + ' 0');
    }
  }
}

function letGoAll(tell) {
  for (const id of [...pointers.keys()]) up({pointerId: id});
  if (tell) send('r');
}

// ---- sound: the emulator's output, streamed while the page asks for it.
// Each block is scheduled on the page's own clock, `playAt` running on by
// exact block lengths; it restarts a cushion ahead after running dry, and a
// block that would put it more than SLACK past the cushion is dropped
// (Wi-Fi delivers in bursts, and the two clocks drift apart).
const soundBtn = document.getElementById('sound');
const CUSHION = (+new URLSearchParams(location.search).get('cushion') || 150) / 1000;
const SLACK = 0.1;
let actx = null, rate = 0, soundOn = false, playAt = 0, dropouts = 0;

function soundRate(r) {
  rate = r;
  soundBtn.hidden = !r;
  if (actx && r && actx.sampleRate !== r && actx._wanted !== r) { actx.close(); actx = null; }
  showSound();
}

function showSound() {
  const live = actx && actx.state === 'running';
  soundBtn.className = soundOn ? 'on' : '';
  soundBtn.textContent = !soundOn ? 'sound off' : !live ? 'tap for sound'
    : dropouts ? 'sound on · ' + dropouts + ' dropouts' : 'sound on';
}

function context() {
  // At the stream's rate, so the blocks join without resampling seams.
  try { actx = new AudioContext({sampleRate: rate}); }
  catch (e) { actx = new (window.AudioContext || window.webkitAudioContext)(); }
  actx._wanted = rate;
  actx.onstatechange = showSound;
}

// A click, not pointerdown: iOS lets a page start sound only from one.
soundBtn.addEventListener('click', () => {
  if (!rate) return;
  if (navigator.audioSession) {
    try { navigator.audioSession.type = 'playback'; } catch (e) {}  // past the mute switch
  }
  if (!actx) context();
  if (soundOn && actx.state === 'running') {
    soundOn = false;
    send('a 0');
  } else {
    soundOn = true;
    actx.resume();
    const tick = actx.createBufferSource();   // older iOS unlocks on a played buffer
    tick.buffer = actx.createBuffer(1, 1, actx.sampleRate);
    tick.connect(actx.destination);
    tick.start();
    playAt = 0;
    send('a 1');
  }
  showSound();
});

function sound(data) {
  if (!soundOn || !actx || actx.state !== 'running' || !rate) return;
  const pcm = new Int16Array(data.slice(1, 1 + ((data.byteLength - 1) & ~3)));
  const n = pcm.length >> 1;
  if (!n) return;
  const now = actx.currentTime;
  if (playAt < now + 0.005) {
    if (playAt) { dropouts++; showSound(); }
    playAt = now + CUSHION;
  } else if (playAt - now > CUSHION + SLACK) {
    return;
  }
  const buf = actx.createBuffer(2, n, rate);
  const l = buf.getChannelData(0), r = buf.getChannelData(1);
  for (let i = 0, j = 0; i < n; i++, j += 2) { l[i] = pcm[j] / 32768; r[i] = pcm[j + 1] / 32768; }
  const src = actx.createBufferSource();
  src.buffer = buf;
  src.connect(actx.destination);
  src.start(playAt);
  playAt += n / rate;
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && soundOn && actx) { playAt = 0; actx.resume().then(showSound, showSound); }
});

addEventListener('pointermove', move);
addEventListener('pointerup', up);
addEventListener('pointercancel', up);
addEventListener('blur', () => letGoAll(true));
document.addEventListener('visibilitychange', () => { if (document.hidden) letGoAll(true); });
document.addEventListener('gesturestart', (e) => e.preventDefault());
connect();
</script>
</body>
</html>
'''
