"""Silent: capture replayable Model:Cycles code segments with Unicorn's result.

Pauses the playing emulator N times; from each paused state an offline engine
runs until the first device access, and the stretch up to a late, unique block
becomes one workload file: entry state, the pages it touches, the expected
final state. Needs the scratch libunicorn (EMAC accessor) via LIBUNICORN_PATH.
"""
import ctypes
import os
import struct
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
SNAP, OUTDIR = sys.argv[1], sys.argv[2]
COUNT = int(sys.argv[3]) if len(sys.argv) > 3 else 40
MAXBLOCKS = 60000
MINBLOCKS = 400
PAGE = 4096


def main():
    from emu import gui
    import livecheck
    from unicorn import (Uc, UcError, UC_ARCH_M68K, UC_MODE_BIG_ENDIAN,
                         UC_HOOK_BLOCK, UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE,
                         UC_PROT_ALL)
    from unicorn.unicorn_py3.unicorn import uclib
    import unicorn.m68k_const as C
    from emu import native
    uclib.uc_scratch_m68k_mac_ptr.restype = ctypes.c_void_p
    uclib.uc_scratch_m68k_mac_ptr.argtypes = [ctypes.c_void_p]

    class Mac(ctypes.Structure):
        _fields_ = [('acc', ctypes.c_uint64 * 4), ('macsr', ctypes.c_uint32),
                    ('mask', ctypes.c_uint32)]

    def mac_of(uc):
        return Mac.from_address(uclib.uc_scratch_m68k_mac_ptr(uc._uch))

    REGS = ([C.UC_M68K_REG_D0 + i for i in range(8)]
            + [C.UC_M68K_REG_A0 + i for i in range(8)] + [C.UC_M68K_REG_SR])

    def state_of(uc):
        m = mac_of(uc)
        return ([uc.reg_read(r) & 0xFFFFFFFF for r in REGS],
                [int(v) for v in m.acc], int(m.macsr), int(m.mask))

    def set_state(uc, st):
        regs, acc, macsr, mask = st
        uc.reg_write(C.UC_M68K_REG_SR, regs[16])
        for r, v in zip(REGS[:16], regs[:16]):
            uc.reg_write(r, v)
        m = mac_of(uc)
        for i in range(4):
            m.acc[i] = acc[i]
        m.macsr, m.mask = macsr, mask

    def pack_state(st):
        regs, acc, macsr, mask = st
        return (struct.pack('<17I', *regs) + struct.pack('<4Q', *acc)
                + struct.pack('<2I', macsr, mask))

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
    live = emu._m.uc
    cpu_model = live.ctl_get_cpu_model()
    os.makedirs(OUTDIR, exist_ok=True)

    STARTS = (0x40058c88, 0x4005698c, 0x40059dae)
    cap = {'left': 0, 'got': None}

    def on_code(u, address, size, data):
        if cap['left'] <= 0:
            return
        cap['left'] -= 1
        if cap['left']:
            return
        regions = [(a, b) for a, b, _p in u.mem_regions() if a < 0x8C000000]
        cap['got'] = (address, state_of(u), regions,
                      {a: bytes(u.mem_read(a, b - a + 1)) for a, b in regions})

    emu.pause.set()
    time.sleep(0.5)
    for addr in STARTS:
        live.hook_add(UC_HOOK_CODE, on_code, begin=addr, end=addr)
    live.ctl_flush_tb()

    made = tries = 0
    while made < COUNT and tries < COUNT * 6:
        tries += 1
        cap['got'] = None
        cap['left'] = 1 + (tries * 37) % 61
        emu.pause.clear()
        deadline = time.time() + 30
        while cap['got'] is None and time.time() < deadline:
            time.sleep(0.01)
        emu.pause.set()
        time.sleep(0.3)
        if cap['got'] is None:
            print('try %d: no capture' % tries, flush=True)
            continue
        pc0, st0, regions, dump = cap['got']
        off = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
        off.ctl_set_cpu_model(cpu_model)
        native.enable_options(off, native.NO_MEM_EXIT | native.NATIVE_RTE)
        for a, b in regions:
            off.mem_map(a, b - a + 1, UC_PROT_ALL)
            off.mem_write(a, dump[a])
        set_state(off, st0)

        seq, sizes, pages = [], {}, set()

        def on_block(u, address, size, data):
            seq.append(address)
            sizes[address] = size
            if len(seq) >= MAXBLOCKS:
                u.emu_stop()

        def on_mem(u, access, address, size, value, data):
            pages.add(address & ~(PAGE - 1))
            pages.add((address + size - 1) & ~(PAGE - 1))

        h1 = off.hook_add(UC_HOOK_BLOCK, on_block)
        h2 = off.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, on_mem)
        try:
            off.emu_start(pc0, 0xFFFFFFFE)
            why = 'limit'
        except UcError as exc:
            why = str(exc)
        off.hook_del(h1)
        off.hook_del(h2)
        if len(seq) < MINBLOCKS:
            print('try %d: pc 0x%08x only %d blocks (%s)' % (tries, pc0, len(seq), why),
                  flush=True)
            continue
        # A late block whose address was never reached before, even mid-block.
        first = {}
        for i, a in enumerate(seq):
            first.setdefault(a, i)
        stop = cut = None
        for a, i in sorted(first.items(), key=lambda kv: -kv[1]):
            if i >= len(seq) - 3:
                continue        # leave the faulting tail out
            if any(b < a < b + sizes[b] for b in set(seq[:i])):
                continue
            stop, cut = a, i
            break
        if stop is None or cut < MINBLOCKS:
            print('try %d: no usable stop' % tries, flush=True)
            continue
        blocks = sorted(set(seq[:cut]) | {stop})
        for a in blocks:
            pages.add(a & ~(PAGE - 1))
            pages.add((a + sizes[a] + 8) & ~(PAGE - 1))
        mapped = lambda p: any(a <= p <= b for a, b in regions)
        pages = sorted(p for p in pages if mapped(p))
        def page_of(p):
            for a, b in regions:
                if a <= p <= b:
                    return dump[a][p - a:p - a + PAGE]
        init = {p: page_of(p) for p in pages}

        def replay():
            for p in pages:
                off.mem_write(p, init[p])
            set_state(off, st0)
            off.ctl_flush_tb()
            off.emu_start(pc0, stop)
            end_pc = off.reg_read(C.UC_M68K_REG_PC) & 0xFFFFFFFF
            return end_pc, state_of(off), {p: bytes(off.mem_read(p, PAGE))
                                           for p in pages}
        try:
            pc_a, st_a, mem_a = replay()
            pc_b, st_b, mem_b = replay()
        except UcError as exc:
            print('try %d: replay failed %s' % (tries, exc), flush=True)
            continue
        if pc_a != stop or (pc_a, st_a, mem_a) != (pc_b, st_b, mem_b):
            print('try %d: replay not repeatable (pc 0x%08x)' % (tries, pc_a),
                  flush=True)
            continue
        dirty = [p for p in pages if mem_a[p] != init[p]]
        out = bytearray(b'CFWL1\0\0\0')
        out += struct.pack('<2I', pc0, stop)
        out += pack_state(st0)
        out += struct.pack('<I', len(pages))
        for p in pages:
            out += struct.pack('<I', p) + init[p]
        out += pack_state(st_a)
        out += struct.pack('<I', len(dirty))
        for p in dirty:
            out += struct.pack('<I', p) + mem_a[p]
        out += struct.pack('<I', len(blocks))
        out += struct.pack('<%dI' % len(blocks), *blocks)
        out += struct.pack('<I', cut)
        path = os.path.join(OUTDIR, 'w%02d.bin' % made)
        with open(path, 'wb') as fh:
            fh.write(out)
        print('w%02d: pc 0x%08x -> 0x%08x  %d blocks (%d distinct), %d pages, '
              '%d dirty, macsr 0x%x, ended by: %s'
              % (made, pc0, stop, cut, len(blocks), len(pages), len(dirty),
                 st0[2], why), flush=True)
        made += 1
    emu.stop_flag.set()
    emu.pause.clear()
    emu.join(15)
    return 0


if __name__ == '__main__':
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
