"""Time the reference engine (patched Unicorn, JIT) on captured workloads.

Same options as the live emulator: NO_MEM_EXIT, NATIVE_RTE, and the native
block budget hook. The translation cache is warm; restoring state between
runs is not timed. One JSON line per workload.
"""
import ctypes
import json
import os
import struct
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, ROOT)
PAGE = 4096
REPS = int(os.environ.get('REPS', '200'))
BUDGET = os.environ.get('BUDGET', '1') == '1'


def parse(path):
    d = open(path, 'rb').read()
    assert d[:8] == b'CFWL1\0\0\0'
    at = 8

    def take(fmt):
        nonlocal at
        v = struct.unpack_from(fmt, d, at)
        at += struct.calcsize(fmt)
        return v

    def state():
        regs = take('<17I')
        acc = take('<4Q')
        macsr, mask = take('<2I')
        return list(regs), list(acc), macsr, mask

    def pages():
        nonlocal at
        out = []
        for _ in range(take('<I')[0]):
            addr = take('<I')[0]
            out.append((addr, d[at:at + PAGE]))
            at += PAGE
        return out

    entry, stop = take('<2I')
    init = state()
    pgs = pages()
    expect = state()
    dirty = dict(pages())
    return entry, stop, init, pgs, expect, dirty


def main():
    from unicorn import Uc, UC_ARCH_M68K, UC_MODE_BIG_ENDIAN, UC_PROT_ALL
    from unicorn.unicorn_py3.unicorn import uclib
    import unicorn.m68k_const as C
    from emu import native
    uclib.uc_scratch_m68k_mac_ptr.restype = ctypes.c_void_p
    uclib.uc_scratch_m68k_mac_ptr.argtypes = [ctypes.c_void_p]

    class Mac(ctypes.Structure):
        _fields_ = [('acc', ctypes.c_uint64 * 4), ('macsr', ctypes.c_uint32),
                    ('mask', ctypes.c_uint32)]

    REGS = ([C.UC_M68K_REG_D0 + i for i in range(8)]
            + [C.UC_M68K_REG_A0 + i for i in range(8)] + [C.UC_M68K_REG_SR])
    cpu_model = int(os.environ.get('CPU_MODEL', str(C.UC_CPU_M68K_CFV4E)))

    for path in sys.argv[1:]:
        entry, stop, init, pgs, expect, dirty = parse(path)
        uc = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
        uc.ctl_set_cpu_model(cpu_model)
        native.enable_options(uc, native.NO_MEM_EXIT | native.NATIVE_RTE)
        budget = native.NativeBudget(uc) if BUDGET else None
        mac = Mac.from_address(uclib.uc_scratch_m68k_mac_ptr(uc._uch))
        megs = sorted(set(a & ~0xFFFFF for a, _ in pgs))
        for a in megs:
            uc.mem_map(a, 0x100000, UC_PROT_ALL)

        def reset():
            for a, data in pgs:
                uc.mem_write(a, data)
            regs, acc, macsr, mask = init
            uc.reg_write(C.UC_M68K_REG_SR, regs[16])
            for r, v in zip(REGS[:16], regs[:16]):
                uc.reg_write(r, v)
            for i in range(4):
                mac.acc[i] = acc[i]
            mac.macsr, mac.mask = macsr, mask
            if budget is not None:
                budget.state.left = 1 << 40
                budget.state.blocks = 0

        def final():
            return ([uc.reg_read(r) & 0xFFFFFFFF for r in REGS],
                    [int(v) for v in mac.acc], int(mac.macsr), int(mac.mask))

        for _ in range(3):
            reset()
            uc.emu_start(entry, stop)
        got = final()
        ok = (got == expect and all(
            bytes(uc.mem_read(a, PAGE)) == dirty.get(a, data) for a, data in pgs))
        blocks = budget.state.blocks if budget is not None else 0
        times = []
        clock = time.perf_counter_ns
        start = uc.emu_start
        for _ in range(REPS):
            reset()
            t0 = clock()
            start(entry, stop)
            times.append(clock() - t0)
        times.sort()
        # The cost of one call that runs a single instruction.
        uc.mem_write(megs[0], b'\x4e\x71\x4e\x71')
        over = []
        for _ in range(200):
            if budget is not None:
                budget.state.left = 1 << 40
            t0 = clock()
            start(megs[0], megs[0] + 2)
            over.append(clock() - t0)
        over.sort()
        print(json.dumps({'name': os.path.basename(path), 'uc_ok': ok,
                          'uc_blocks': blocks, 'uc_ns_min': times[0],
                          'uc_ns_med': times[len(times) // 2],
                          'uc_call_ns': over[len(over) // 2]}), flush=True)


if __name__ == '__main__':
    main()
