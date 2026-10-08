"""Silent: record a continuous run of the playing Model:Cycles for cfreplay.

    cd COPY_OF/portable/firmware/mc-1.13-44fe5862
    DT2_DEVICES=REPO/devices DT2_PLUSDRIVE='' DIGIEMU_MUTED=1 \
      python REPO/native/cfcore/tools/record.py SNAPSHOT TRACE [STEPS]

The trace is the machine at the start and then, in order, everything the
engine did (each block it entered, its state at the end of each step) and
everything done to it from outside: each hook that fired, and each write the
Python side made to guest memory and registers. It is taken from the real
window's emulator thread by wrapping the Unicorn binding, with the two
shortcuts that write behind the binding's back switched off (fastuc, and the
native software eDMA, which then runs in Python). Needs the Unicorn with the
EMAC accessor (LIBUNICORN_PATH), see the README.
"""
import ctypes
import os
import struct
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
SNAP, OUT = sys.argv[1], sys.argv[2]
STEPS = int(sys.argv[3]) if len(sys.argv) > 3 else 2000


def main():
    import unicorn
    from unicorn import unicorn_const as K
    import unicorn.m68k_const as C
    from unicorn.unicorn_py3.unicorn import uclib
    from emu import edma_sw, fastuc, gui, longrun
    import livecheck

    uclib.uc_scratch_m68k_mac_ptr.restype = ctypes.c_void_p
    uclib.uc_scratch_m68k_mac_ptr.argtypes = [ctypes.c_void_p]

    class Mac(ctypes.Structure):
        _fields_ = [('acc', ctypes.c_uint64 * 4), ('macsr', ctypes.c_uint32),
                    ('mask', ctypes.c_uint32)]

    REGS = ([C.UC_M68K_REG_D0 + i for i in range(8)]
            + [C.UC_M68K_REG_A0 + i for i in range(8)] + [C.UC_M68K_REG_SR])
    REGIDX = {r: i for i, r in enumerate(REGS)}
    REGIDX[C.UC_M68K_REG_PC] = 17
    MEM = K.UC_HOOK_MEM_READ | K.UC_HOOK_MEM_WRITE
    pack = struct.pack

    st = {'on': False, 'arm': False, 'uc': None, 'last': 0, 'steps': 0, 'srcs': {},
          'clock': 0, 'tbs': 0, 'budget': None, 'fh': None, 'odd': [],
          'raised': 0, 'regions': []}
    buf = bytearray()
    done = threading.Event()
    hooks = []                       # (uc, type, begin, end)

    orig_add = unicorn.Uc.hook_add
    orig_mw = unicorn.Uc.mem_write
    orig_mr = unicorn.Uc.mem_read
    orig_rw = unicorn.Uc.reg_write
    orig_rr = unicorn.Uc.reg_read
    orig_stop = unicorn.Uc.emu_stop

    def hook_add(self, htype, callback, user_data=None, begin=1, end=0, *a, **k):
        hooks.append((self, htype, begin, end))
        name = getattr(callback, '__name__', '')
        if name == '_on_force_rte':
            st['force_rte'] = begin
        elif name in ('do_halt', 'satisfy', 'posted') and htype == K.UC_HOOK_CODE:
            st.setdefault(name, []).append(begin)
        cb = callback
        if htype == K.UC_HOOK_CODE:
            def cb(uc, address, size, ud):
                if st['on'] and uc is st['uc']:
                    buf.extend(pack('<BI', 3, address))
                return callback(uc, address, size, ud)
        elif htype & MEM and not htype & ~MEM:
            def cb(uc, access, address, size, value, ud):
                if st['on'] and uc is st['uc']:
                    buf.extend(pack('<BBBII', 4, access == K.UC_MEM_WRITE, size,
                                    address, value & ((1 << (8 * size)) - 1)
                                    & 0xFFFFFFFF))
                return callback(uc, access, address, size, value, ud)
        elif htype == K.UC_HOOK_INTR:
            def cb(uc, intno, ud):
                if st['on'] and uc is st['uc']:
                    buf.extend(pack('<BI', 5, intno))
                return callback(uc, intno, ud)
        return orig_add(self, htype, cb, user_data, begin, end, *a, **k)

    def mem_write(self, address, data):
        if st['on'] and self is st['uc']:
            data = bytes(data)
            buf.extend(pack('<BII', 6, address, len(data)))
            buf.extend(data)
        return orig_mw(self, address, data)

    def mem_read(self, address, size):
        data = orig_mr(self, address, size)
        if st['on'] and self is st['uc']:
            buf.extend(pack('<BII', 12, address, len(data)))
            buf.extend(data)
        return data

    def reg_write(self, reg_id, value, *a):
        if st['on'] and self is st['uc']:
            idx = REGIDX.get(reg_id)
            if idx is None:
                st['odd'].append('reg_write %r' % (reg_id,))
            else:
                buf.extend(pack('<BBI', 7, idx, value & 0xFFFFFFFF))
        return orig_rw(self, reg_id, value, *a)

    def emu_stop(self):
        if st['on'] and self is st['uc']:
            buf.append(8)
        return orig_stop(self)

    unicorn.Uc.hook_add = hook_add
    unicorn.Uc.mem_write = mem_write
    unicorn.Uc.mem_read = mem_read
    from emu import harness
    orig_raise = harness.Machine.raise_vector

    def raise_vector(self, *a, **k):
        taken = orig_raise(self, *a, **k)
        if taken and st['on'] and self.uc is st['uc']:
            st['raised'] += 1
        return taken

    harness.Machine.raise_vector = raise_vector
    for name in ('mem_map', 'mem_map_ptr'):
        def mapper(self, *a, _orig=getattr(unicorn.Uc, name), _name=name, **k):
            if st['on'] and self is st['uc']:
                st['odd'].append('%s%r during the recording' % (_name, a[:2]))
            return _orig(self, *a, **k)
        setattr(unicorn.Uc, name, mapper)
    unicorn.Uc.reg_write = reg_write
    unicorn.Uc.emu_stop = emu_stop
    fastuc.install = lambda uc: False
    orig_bank = edma_sw.SoftwareBank.__init__

    def bank_init(self, machine, *a, **k):
        k['native'] = False
        return orig_bank(self, machine, *a, **k)

    edma_sw.SoftwareBank.__init__ = bank_init

    def cpu_state(uc):
        mac = Mac.from_address(uclib.uc_scratch_m68k_mac_ptr(uc._uch))
        return (pack('<17I', *[orig_rr(uc, r) & 0xFFFFFFFF for r in REGS])
                + pack('<4Q', *[int(v) for v in mac.acc])
                + pack('<2I', int(mac.macsr), int(mac.mask)))

    def on_block(uc, address, size, ud):
        # The budget hook runs first: it counted this block unless the step
        # is over, and then the block does not run.
        if st['on']:
            blocks = st['budget'].blocks
            if blocks != st['last']:
                st['last'] = blocks
                st['tbs'] += 1
                buf.extend(pack('<BII', 2, address, size))

    def begin(stepper):
        uc = stepper.m.uc
        st['uc'] = uc
        st['budget'] = stepper._native.state
        mine = [(t, b, e) for u, t, b, e in hooks if u is uc]
        head = bytearray(b'CFTR2\0\0\0')
        head += cpu_state(uc)
        regions = [(a, b) for a, b, _p in uc.mem_regions()]
        head += pack('<I', len(regions))
        # The DDR repeats through its window: a megabyte that decodes to the
        # same physical memory as one already written is a view of that one.
        owner = {}
        for a, b in regions:
            device = any(t & MEM and not t & ~MEM and bb <= ee and bb <= b and ee >= a
                         for t, bb, ee in mine)
            phys = stepper.m.ddr_physical(a)
            if phys is not None and phys in owner:
                # Probe rather than trust the model: a byte written through
                # one view must show in the other.
                first = owner[phys]
                was = bytes(orig_mr(uc, a, 1))
                orig_mw(uc, a, bytes([was[0] ^ 0xFF]))
                same = bytes(orig_mr(uc, first, 1)) == bytes([was[0] ^ 0xFF])
                orig_mw(uc, a, was)
                st['views'] = st.get('views', 0) + (1 if same else 0)
                st['copies'] = st.get('copies', 0) + (0 if same else 1)
                if same:
                    head += pack('<4I', a, b - a + 1, 2, first)
                    continue
            if phys is not None:
                owner[phys] = a
            head += pack('<3I', a, b - a + 1, 1 if device else 0)
            head += bytes(orig_mr(uc, a, b - a + 1))
            st['regions'].append((a, b))
        code = set()
        for t, bb, ee in mine:
            if t == K.UC_HOOK_CODE:
                if not bb <= ee or ee - bb > 64:
                    st['odd'].append('code hook over %#x..%#x' % (bb, ee))
                else:
                    code.update(range(bb, ee + 1, 2))
        head += pack('<I', len(code)) + pack('<%dI' % len(code), *sorted(code))
        # The timer models' state: they are stepped just before the engine
        # runs, so by now each has been seen.
        srcs = [(k, st['srcs'][k]) for k in sorted(st['srcs'])]
        ev = st.get('ev') or {}
        idle = st.get('do_halt', [])
        pend = st.get('satisfy', [])
        head += pack('<I', len(srcs) + bool(idle) + bool(pend))
        if idle:
            head += pack('<BQQI', 7, ev.get('idle_spins', {}).get('n', 0),
                         st.get('idle_yield', 20000), len(idle))
            head += pack('<%dI' % len(idle), *idle)
        if pend:
            skip = sorted(ev.get('unblock_skip', ()))
            callers = sorted(ev.get('unblock_skip_callers', ()))
            post = st.get('posted', [0])[0]
            head += pack('<BI', 8, len(pend)) + pack('<%dI' % len(pend), *pend)
            head += pack('<II', post, len(skip)) + pack('<%dI' % len(skip), *skip)
            head += pack('<I', len(callers)) + pack('<%dI' % len(callers), *callers)
        for kind, src in srcs:
            if kind == 9:
                dev = src.devices[0x1A]
                head += pack('<B9B', kind, src.cr, src.sr, bool(src.busy),
                             bool(src.expect_address), src.target is not None,
                             bool(src.reading), src.rx, dev.pointer, bool(dev._fresh))
                head += bytes(dev.regs)
                continue
            if kind == 6:
                if src._key_want or src._pad_want or any(src._turns):
                    st['odd'].append('panel input was waiting when the recording began')
                head += pack('<BBq', kind, src.select, src.frames)
                head += bytes(src.keys) + bytes(src.phase)
                head += pack('<6H', *src.pads) + bytes(src.led_rows)
                continue
            if kind == 5:
                head += pack('<BQ', kind, sum(1 << c for c in src.claimed))
                for ch in range(64):
                    c = src.channels.get(ch)
                    done = getattr(c, '_pending', None)
                    head += pack('<BH', (1 if getattr(c, '_deferred', False) else 0)
                                 | (2 if done is not None else 0), done or 0)
                head += pack('<B', len(src.pending))
                head += bytes(c.channel for c in src.pending)
                continue
            if kind == 4:
                m = stepper.m
                nxt = src.next
                due = getattr(src, '_due', None)
                ipl = getattr(m, 'render_ipl', None)
                head += pack('<BIIIqqq', kind, src.p.tx_chan, src.p.tx_vector,
                             st.get('force_rte', 0), src.request_hz, src.ips, src.now)
                head += pack('<Bqq', nxt is not None,
                             nxt.numerator if nxt is not None else 0,
                             nxt.denominator if nxt is not None else 1)
                head += pack('<BBq', bool(src.batch), due is not None, due or 0)
                head += pack('<5B', src.p.tx_chan in src.enabled,
                             bool(src.int50_asserted), bool(src.int50_delivered),
                             bool(src.force_asserted), bool(src.force_delivered))
                head += pack('<BqB', 0xFF if ipl is None else ipl,
                             int(getattr(m, 'render_since', 0) or 0),
                             bool(getattr(m, 'render_waiting', False)))
                # The samples it hands on, in the order it hands them.
                sink = src.sink

                def tap(data, _sink=sink):
                    if st['on']:
                        buf.extend(pack('<BI', 25, len(data)))
                        buf.extend(data)
                    if _sink is not None:
                        _sink(data)

                src.sink = tap
                continue
            if kind >= 2:
                head += pack('<BIIQQQ', kind, src.base, src.first_vector,
                             sum(1 << s for s in src.sources),
                             sum(1 << s for s in src.asserted),
                             sum(1 << s for s in src.delivered))
                continue
            head += pack('<BB', kind, len(src.channels)) + bytes(src.channels)
            head += pack('<d', float(src.ips))
            for n in src.next:
                head += pack('<Bd', n is not None, float(n or 0))
            head += pack('<BBBq', sum(1 << c for c in src.pending),
                         sum(1 << c for c in getattr(src, 'arm', ())),
                         bool(src.held), int(getattr(src, 'clock', 0)))
        st['fh'] = open(OUT, 'wb')
        st['fh'].write(head)
        orig_add(uc, K.UC_HOOK_BLOCK, on_block)
        uc.ctl_flush_tb()
        st['on'] = True

    orig_run = longrun._FastStepper.run
    orig_skip = longrun._FastStepper.idle_skip

    def run(self, pc, step):
        if st['arm'] and not st['on'] and not done.is_set():
            begin(self)
        if not st['on']:
            return orig_run(self, pc, step)
        buf.extend(pack('<BI', 1, pc))
        st['last'] = 0
        out = orig_run(self, pc, step)
        uc = self.m.uc
        buf.append(9)
        buf.extend(cpu_state(uc))
        buf.extend(pack('<I', orig_rr(uc, C.UC_M68K_REG_PC) & 0xFFFFFFFF))
        st['clock'] += out
        st['steps'] += 1
        if len(buf) > 1 << 20:
            st['fh'].write(buf)
            del buf[:]
        if st['steps'] >= STEPS:
            st['on'] = False
            buf.append(11)
            buf.extend(pack('<QQ', int(st['clock'] * 1e9 / self.m_ips), st['raised']))
            buf.append(13)
            buf.extend(pack('<I', len(st['regions'])))
            st['fh'].write(buf)
            del buf[:]
            for a, b in st['regions']:
                st['fh'].write(pack('<2I', a, b - a + 1))
                st['fh'].write(bytes(orig_mr(uc, a, b - a + 1)))
            st['fh'].close()
            done.set()
        return out

    def idle_skip(self, blocks):
        if st['on']:
            buf.extend(pack('<Bq', 27, blocks))
        orig_skip(self, blocks)
        st['last'] = self.blocks

    longrun._FastStepper.run = run

    # The timer models: when each is stepped and serviced, with what count
    # and what answer, so a port of them can be checked call by call.
    from emu import dtim, pit

    def watch(cls, kind):
        orig_step, orig_service = cls.step, cls.service

        def step(self, done, remaining=None):
            k = kind(self) if callable(kind) else kind
            st['srcs'][k] = self
            if not st['on']:
                return orig_step(self, done, remaining)
            buf.extend(pack('<BBq', 20, k, done))
            out = orig_step(self, done, remaining)
            buf.extend(pack('<BBq', 21, k, -1 if out is None else out))
            return out

        def service(self, done):
            if not st['on']:
                return orig_service(self, done)
            k = kind(self) if callable(kind) else kind
            buf.extend(pack('<BBq', 22, k, done))
            out = orig_service(self, done)
            buf.extend(pack('<BB', 23, k))
            return out

        cls.step, cls.service = step, service

    watch(pit.Pits, 0)
    watch(dtim.Dtims, 1)
    orig_holds = pit.render_holds

    def render_holds(m, done, level):
        out = orig_holds(m, done, level)
        if st['on']:
            buf.extend(pack('<BBB', 24, bool(out), 0xFF if level is None else level))
        return out

    from emu import intfrc
    for mod in (pit, dtim, intfrc):
        mod.render_holds = render_holds
    # The front panel: which one it is, and the input the window gives it.
    from emu import modelboard
    orig_read = modelboard.ModelPanel.on_data_read

    def on_data_read(self):
        st['srcs'][6] = self
        return orig_read(self)

    modelboard.ModelPanel.on_data_read = on_data_read

    def tap_input(name, kind):
        orig = getattr(modelboard.ModelPanel, name)

        def call(self, a, b, c=0):
            if st['on']:
                buf.extend(pack('<BBiii', 26, kind, int(a), int(b), int(c)))
            return orig(self, a, b, c) if name == 'key' else orig(self, a, b)

        setattr(modelboard.ModelPanel, name, call)

    from emu import i2c
    orig_i2c = i2c.I2cBus.install

    def i2c_install(self, m):
        st['srcs'][9] = self
        return orig_i2c(self, m)

    i2c.I2cBus.install = i2c_install
    tap_input('key', 0)
    tap_input('pad', 1)
    tap_input('turn', 2)
    from emu import ssi
    watch(ssi.Ssi0Dma, 4)
    watch(edma_sw.SoftwareBank, 5)
    # The forced-interrupt models: INTC0's is source 2, INTC1's is 3.
    watch(intfrc.ForcedInterrupts, lambda f: 2 if f.base == intfrc.INTC0 else 3)
    longrun._FastStepper.idle_skip = idle_skip
    orig_build = longrun.build

    def build(*a, **k):
        out = orig_build(*a, **k)
        st['ev'] = out[1]
        st['idle_yield'] = k.get('idle_yield', 20000)
        return out

    longrun.build = build
    if getattr(gui, 'build', None) is orig_build:
        gui.build = build

    gui.audioout.WaveOut = livecheck.StubOut
    syx = [f for f in os.listdir('.') if f.endswith('.syx')][0]
    emu = gui.Emulator(os.path.abspath(SNAP), syx=os.path.abspath(syx),
                       realtime=True)
    emu.start()
    if not emu.ready.wait(240):
        print('not ready')
        return 1
    time.sleep(2.0)
    emu.inbox.append(('press', 10, 0))
    time.sleep(0.15)
    emu.inbox.append(('release', 10, 0))
    time.sleep(3.0)
    longrun._FastStepper.m_ips = emu._pits.sources[0].ips
    t0 = time.time()
    st['arm'] = True
    # Some playing on the panel while it records (it runs about 7x slower
    # than real time): a pad, a trig key, a knob both ways.
    if os.environ.get('REC_INPUT', '1') == '1':
        for delay, event in ((2.0, ('press', 33, 110)), (2.0, ('release', 33, 0)),
                             (1.0, ('press', 16, 0)), (1.5, ('release', 16, 0)),
                             (1.0, ('encoder', 3, 4)), (1.5, ('encoder', 3, -2)),
                             (1.0, ('press', 35, 60)), (1.0, ('release', 35, 0))):
            if done.wait(delay):
                break
            emu.inbox.append(event)
    if not done.wait(1800):
        print('recording did not finish: %d steps' % st['steps'])
        return 1
    print('%d steps, %d blocks, %d exception entries, %.3f emulated s in %.1f s, '
          '%d MB; halted: %r; odd: %r'
          % (st['steps'], st['tbs'], st['raised'],
             st['clock'] / emu._pits.sources[0].ips,
             time.time() - t0, os.path.getsize(OUT) >> 20, emu.error,
             st['odd'][:5]), flush=True)
    print('DDR megabytes that repeat an earlier one: %d shared views, %d separate copies'
          % (st.get('views', 0), st.get('copies', 0)), flush=True)
    emu.stop_flag.set()
    emu.join(15)
    return 0


if __name__ == '__main__':
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
