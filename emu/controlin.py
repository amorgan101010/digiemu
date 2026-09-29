"""A local control input: a program on this computer turns the panel's encoders.

The keyboard holds one knob at a time (emu/panelkeys.py), which suits hands on
a computer keyboard but not a MIDI controller with a row of knobs turning at
once. This takes encoder turns from other programs on the same computer, such
as the EasyControl.9 bridge (~/Documents/worlde_setup/worlde_bridge.py), and
puts them on the emulator's inbox as the window's own turns are.

    $XDG_RUNTIME_DIR/elektremu/<product>.sock    (digitakt.sock, digitone.sock)

A Unix datagram socket, readable and writable by this user only. Each
datagram is one line of text:

    turn <label> <n>    turn the encoder labelled A-H or LEVEL/DATA n detents
                        (signed), as emu/remote.py's 'e' does
    leds                reply, to the sender's bound address, with one line:
                        'leds ' and a JSON object of key label -> [r, g, b]
                        for every key with a light ([0, 0, 0] when dark), so
                        a sender can see a mode the keys show, such as which
                        MUTE mode is open

There is no network side: the remote panel (emu/remote.py) is for tablets.
A second window of the same product finds the socket answered and goes
without, rather than taking it over.
"""
import json
import os
import socket
import threading

from emu import remote

READ_TIMEOUT_S = 0.5      # how often the reader checks for stop()


def socket_path(product):
    base = os.environ.get('XDG_RUNTIME_DIR') or '/tmp'
    return os.path.join(base, 'elektremu', '%s.sock' % product.lower())


class ControlInput:
    """`emu` has .inbox and .device; `enc_codes()` -> label -> encoder code;
    `leds()` -> key label -> (r, g, b) or None."""

    def __init__(self, emu, enc_codes, product, leds=None):
        self.emu = emu
        self.enc_codes = enc_codes
        self.leds = leds
        self.path = socket_path(product)
        self.sock = None
        self.thread = None
        self.error = None

    def start(self):
        """-> True once the socket is bound and read."""
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            if self._answered():
                self.error = 'another window has %s' % self.path
                return False
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            old = os.umask(0o177)
            try:
                sock.bind(self.path)
            finally:
                os.umask(old)
        except OSError as e:
            self.error = str(e)
            return False
        sock.settimeout(READ_TIMEOUT_S)
        self.sock = sock
        self.thread = threading.Thread(target=self._read, name='controlin',
                                       daemon=True)
        self.thread.start()
        return True

    def stop(self):
        sock, self.sock = self.sock, None
        if sock is None:
            return
        if self.thread is not None:
            self.thread.join(timeout=2 * READ_TIMEOUT_S)
        sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _answered(self):
        """-> True if a live window already reads the socket."""
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            probe.connect(self.path)
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _read(self):
        while True:
            sock = self.sock
            if sock is None:
                return
            try:
                data, sender = sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                return
            reply = self.handle(data.decode('utf-8', 'replace'))
            if reply is not None and sender:
                try:
                    sock.sendto(reply.encode(), sender)
                except OSError:
                    pass                    # the sender went away

    def handle(self, text):
        """-> a reply for the sender, or None."""
        parts = text.split()
        if parts == ['leds']:
            lit = (self.leds() if self.leds else None) or {}
            return 'leds ' + json.dumps(
                {label: list(rgb[:3]) if rgb else [0, 0, 0]
                 for label, rgb in lit.items()}, separators=(',', ':'))
        if len(parts) != 3 or parts[0] != 'turn':
            return
        code = (self.enc_codes() or {}).get(parts[1])
        try:
            steps = int(parts[2])
        except ValueError:
            return
        if code is None or not steps:
            return
        steps = max(-remote.MAX_DETENTS, min(remote.MAX_DETENTS, steps))
        dev = getattr(self.emu, 'device', None)
        counts = getattr(dev, 'encoder_counts', 1) or 1
        for n in remote.split_turn(steps, 127 // counts):
            self.emu.inbox.append(('encoder', code, n))
