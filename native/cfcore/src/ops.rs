//! What each instruction does. Every handler is `inline(always)` and takes
//! its operands by value, so a translated block (the handlers called with
//! constants) folds down to the arithmetic, and the interpreter calls the
//! very same code with decoded operands.
//!
//! Semantics follow the ColdFire Family Programmer's Reference Manual and
//! the MCF5441x Reference Manual's EMAC chapter, and match digiemu's patched
//! Unicorn (patches/README.md), which is what this core is checked against.
//! The EMAC section is a port of QEMU's m68k MAC helpers as patched there,
//! so it is a derivative of that code and carries its licence (GPL-2.0+).
use crate::cpu::{Cpu, Mem};
use crate::insn::{Alu, Ea, Sz};

const MACSR_PAV0: u32 = 0x100;
const MACSR_OMC: u32 = 0x080;
const MACSR_SU: u32 = 0x040;
const MACSR_FI: u32 = 0x020;
const MACSR_RT: u32 = 0x010;
const MACSR_N: u32 = 0x008;
const MACSR_Z: u32 = 0x004;
const MACSR_V: u32 = 0x002;
const MACSR_EV: u32 = 0x001;

const SP: usize = 15;

#[inline(always)]
fn sext(v: u32, sz: Sz) -> u32 {
    match sz {
        Sz::B => v as u8 as i8 as i32 as u32,
        Sz::W => v as u16 as i16 as i32 as u32,
        Sz::L => v,
    }
}

#[inline(always)]
fn bytes(sz: Sz) -> u32 {
    match sz {
        Sz::B => 1,
        Sz::W => 2,
        Sz::L => 4,
    }
}

#[inline(always)]
fn ar(n: u8) -> usize {
    8 + (n & 7) as usize
}

#[inline(always)]
fn dr(n: u8) -> usize {
    (n & 7) as usize
}

#[inline(always)]
fn logic(c: &mut Cpu, v: u32) {
    c.cc_n = v;
    c.cc_z = v;
    c.cc_v = 0;
    c.cc_c = 0;
}

#[derive(Clone, Copy)]
enum Loc {
    R(usize),
    M(u32),
}

#[inline(always)]
fn index(c: &Cpu, xr: u8, shift: u8, d: i8) -> u32 {
    (c.r[(xr & 15) as usize] << (shift & 3)).wrapping_add(d as i32 as u32)
}

/// The address a memory operand names, for the modes with no side effect.
#[inline(always)]
fn addr_of(c: &Cpu, ea: Ea) -> u32 {
    match ea {
        Ea::Ind(a) => c.r[ar(a)],
        Ea::Disp(a, d) => c.r[ar(a)].wrapping_add(d as i32 as u32),
        Ea::Idx(a, x, s, d) => c.r[ar(a)].wrapping_add(index(c, x, s, d)),
        Ea::Abs(v) => v,
        Ea::PcIdx(base, x, s, d) => base.wrapping_add(index(c, x, s, d)),
        _ => 0,
    }
}

#[inline(always)]
fn resolve(c: &mut Cpu, ea: Ea, sz: Sz) -> Loc {
    match ea {
        Ea::D(n) => Loc::R(dr(n)),
        Ea::A(n) => Loc::R(ar(n)),
        Ea::Inc(a) => {
            let v = c.r[ar(a)];
            c.r[ar(a)] = v.wrapping_add(bytes(sz));
            Loc::M(v)
        }
        Ea::Dec(a) => {
            let v = c.r[ar(a)].wrapping_sub(bytes(sz));
            c.r[ar(a)] = v;
            Loc::M(v)
        }
        other => Loc::M(addr_of(c, other)),
    }
}

#[inline(always)]
fn load(c: &Cpu, m: &mut Mem, loc: Loc, sz: Sz) -> u32 {
    match (loc, sz) {
        (Loc::R(i), Sz::B) => c.r[i] & 0xff,
        (Loc::R(i), Sz::W) => c.r[i] & 0xffff,
        (Loc::R(i), Sz::L) => c.r[i],
        (Loc::M(a), Sz::B) => m.r8(a),
        (Loc::M(a), Sz::W) => m.r16(a),
        (Loc::M(a), Sz::L) => m.r32(a),
    }
}

#[inline(always)]
fn store(c: &mut Cpu, m: &mut Mem, loc: Loc, sz: Sz, v: u32) {
    match (loc, sz) {
        (Loc::R(i), Sz::B) => c.r[i] = (c.r[i] & 0xffff_ff00) | (v & 0xff),
        (Loc::R(i), Sz::W) => c.r[i] = (c.r[i] & 0xffff_0000) | (v & 0xffff),
        (Loc::R(i), Sz::L) => c.r[i] = v,
        (Loc::M(a), Sz::B) => m.w8(a, v),
        (Loc::M(a), Sz::W) => m.w16(a, v),
        (Loc::M(a), Sz::L) => m.w32(a, v),
    }
}

/// Read an operand, zero-extended, with its side effect.
#[inline(always)]
fn rd(c: &mut Cpu, m: &mut Mem, ea: Ea, sz: Sz) -> u32 {
    if let Ea::Imm(v) = ea {
        return v;
    }
    let loc = resolve(c, ea, sz);
    load(c, m, loc, sz)
}

#[inline(always)]
fn push(c: &mut Cpu, m: &mut Mem, v: u32) {
    let sp = c.r[SP].wrapping_sub(4);
    c.r[SP] = sp;
    m.w32(sp, v);
}

#[inline(always)]
fn alu(c: &mut Cpu, op: Alu, s: u32, d: u32) -> u32 {
    match op {
        Alu::Add => {
            let r = d.wrapping_add(s);
            c.cc_c = (r < s) as u32;
            c.cc_x = c.cc_c;
            c.cc_v = (r ^ s) & !(d ^ s);
            c.cc_n = r;
            c.cc_z = r;
            r
        }
        Alu::Sub => {
            let r = d.wrapping_sub(s);
            c.cc_c = (d < s) as u32;
            c.cc_x = c.cc_c;
            c.cc_v = (s ^ d) & (r ^ d);
            c.cc_n = r;
            c.cc_z = r;
            r
        }
        Alu::And => {
            let r = d & s;
            logic(c, r);
            r
        }
        Alu::Or => {
            let r = d | s;
            logic(c, r);
            r
        }
        Alu::Eor => {
            let r = d ^ s;
            logic(c, r);
            r
        }
    }
}

/// Is condition `cc` (the four bits of Bcc and Scc) true?
#[inline(always)]
pub fn cond(c: &Cpu, cc: u8) -> bool {
    let n = c.cc_n >> 31 != 0;
    let z = c.cc_z == 0;
    let v = c.cc_v >> 31 != 0;
    let cy = c.cc_c != 0;
    match cc & 15 {
        0 => true,
        1 => false,
        2 => !cy && !z,
        3 => cy || z,
        4 => !cy,
        5 => cy,
        6 => !z,
        7 => z,
        8 => !v,
        9 => v,
        10 => !n,
        11 => n,
        12 => n == v,
        13 => n != v,
        14 => !z && n == v,
        _ => z || n != v,
    }
}

#[inline(always)]
pub fn mov(c: &mut Cpu, m: &mut Mem, sz: Sz, src: Ea, dst: Ea) {
    let v = rd(c, m, src, sz);
    let loc = resolve(c, dst, sz);
    store(c, m, loc, sz, v);
    logic(c, sext(v, sz));
}

#[inline(always)]
pub fn movea(c: &mut Cpu, m: &mut Mem, sz: Sz, src: Ea, an: u8) {
    let v = rd(c, m, src, sz);
    c.r[ar(an)] = sext(v, sz);
}

#[inline(always)]
pub fn moveq(c: &mut Cpu, _m: &mut Mem, v: u32, dn: u8) {
    c.r[dr(dn)] = v;
    logic(c, v);
}

#[inline(always)]
pub fn mvs(c: &mut Cpu, m: &mut Mem, sz: Sz, src: Ea, dn: u8) {
    let v = sext(rd(c, m, src, sz), sz);
    c.r[dr(dn)] = v;
    logic(c, v);
}

#[inline(always)]
pub fn mvz(c: &mut Cpu, m: &mut Mem, sz: Sz, src: Ea, dn: u8) {
    let v = rd(c, m, src, sz);
    c.r[dr(dn)] = v;
    // The reference takes N from the source operand at its own size, so a
    // byte of 0x80 or more sets it. The ColdFire manual says MVZ clears N.
    // This follows the reference, which is what the core is checked against.
    logic(c, sext(v, sz));
}

#[inline(always)]
pub fn mov3q(c: &mut Cpu, m: &mut Mem, v: u32, dst: Ea) {
    let loc = resolve(c, dst, Sz::L);
    store(c, m, loc, Sz::L, v);
    logic(c, v);
}

#[inline(always)]
pub fn lea(c: &mut Cpu, _m: &mut Mem, src: Ea, an: u8) {
    c.r[ar(an)] = addr_of(c, src);
}

#[inline(always)]
pub fn pea(c: &mut Cpu, m: &mut Mem, src: Ea) {
    let a = addr_of(c, src);
    push(c, m, a);
}

#[inline(always)]
pub fn clr(c: &mut Cpu, m: &mut Mem, sz: Sz, dst: Ea) {
    let loc = resolve(c, dst, sz);
    store(c, m, loc, sz, 0);
    logic(c, 0);
}

#[inline(always)]
pub fn tst(c: &mut Cpu, m: &mut Mem, sz: Sz, src: Ea) {
    let v = rd(c, m, src, sz);
    logic(c, sext(v, sz));
}

#[inline(always)]
pub fn alu_to_reg(c: &mut Cpu, m: &mut Mem, op: Alu, src: Ea, dn: u8) {
    let s = rd(c, m, src, Sz::L);
    let d = c.r[dr(dn)];
    c.r[dr(dn)] = alu(c, op, s, d);
}

#[inline(always)]
pub fn alu_to_mem(c: &mut Cpu, m: &mut Mem, op: Alu, dn: u8, dst: Ea) {
    let loc = resolve(c, dst, Sz::L);
    let d = load(c, m, loc, Sz::L);
    let s = c.r[dr(dn)];
    let r = alu(c, op, s, d);
    store(c, m, loc, Sz::L, r);
}

#[inline(always)]
pub fn addq(c: &mut Cpu, m: &mut Mem, v: u32, dst: Ea, sub: bool) {
    if let Ea::A(n) = dst {
        let d = c.r[ar(n)];
        c.r[ar(n)] = if sub { d.wrapping_sub(v) } else { d.wrapping_add(v) };
        return;
    }
    let loc = resolve(c, dst, Sz::L);
    let d = load(c, m, loc, Sz::L);
    let r = alu(c, if sub { Alu::Sub } else { Alu::Add }, v, d);
    store(c, m, loc, Sz::L, r);
}

#[inline(always)]
fn compare(c: &mut Cpu, s: u32, d: u32, sz: Sz) {
    let r = sext(d.wrapping_sub(s), sz);
    c.cc_c = (d < s) as u32;
    c.cc_v = (s ^ d) & (r ^ d);
    c.cc_n = r;
    c.cc_z = r;
}

#[inline(always)]
pub fn cmp(c: &mut Cpu, m: &mut Mem, sz: Sz, src: Ea, dn: u8) {
    let s = sext(rd(c, m, src, sz), sz);
    let d = sext(c.r[dr(dn)], sz);
    compare(c, s, d, sz);
}

#[inline(always)]
pub fn cmpa(c: &mut Cpu, m: &mut Mem, sz: Sz, src: Ea, an: u8) {
    let s = sext(rd(c, m, src, sz), sz);
    let d = c.r[ar(an)];
    compare(c, s, d, Sz::L);
}

#[inline(always)]
pub fn adda(c: &mut Cpu, m: &mut Mem, src: Ea, an: u8, sub: bool) {
    let s = rd(c, m, src, Sz::L);
    let d = c.r[ar(an)];
    c.r[ar(an)] = if sub { d.wrapping_sub(s) } else { d.wrapping_add(s) };
}

#[inline(always)]
pub fn addx(c: &mut Cpu, _m: &mut Mem, ry: u8, rx: u8, sub: bool) {
    let s = c.r[dr(ry)];
    let d = c.r[dr(rx)];
    let (r, x, v);
    if sub {
        let t = (d as u64).wrapping_sub(s as u64 + c.cc_x as u64);
        r = t as u32;
        x = ((t >> 32) & 1) as u32;
        v = (r ^ d) & (d ^ s);
    } else {
        let t = d as u64 + s as u64 + c.cc_x as u64;
        r = t as u32;
        x = ((t >> 32) & 1) as u32;
        v = (r ^ s) & !(d ^ s);
    }
    c.r[dr(rx)] = r;
    c.cc_n = r;
    c.cc_z |= r;
    c.cc_v = v;
    c.cc_c = x;
    c.cc_x = x;
}

#[inline(always)]
pub fn neg(c: &mut Cpu, _m: &mut Mem, dn: u8) {
    let d = c.r[dr(dn)];
    let r = 0u32.wrapping_sub(d);
    c.r[dr(dn)] = r;
    c.cc_c = (d != 0) as u32;
    c.cc_x = c.cc_c;
    c.cc_v = d & r;
    c.cc_n = r;
    c.cc_z = r;
}

#[inline(always)]
pub fn negx(c: &mut Cpu, _m: &mut Mem, dn: u8) {
    let d = c.r[dr(dn)];
    let t = 0u64.wrapping_sub(d as u64 + c.cc_x as u64);
    let r = t as u32;
    let x = ((t >> 32) & 1) as u32;
    c.r[dr(dn)] = r;
    c.cc_n = r;
    c.cc_z |= r;
    c.cc_v = r & d;
    c.cc_c = x;
    c.cc_x = x;
}

#[inline(always)]
pub fn not(c: &mut Cpu, _m: &mut Mem, dn: u8) {
    let r = !c.r[dr(dn)];
    c.r[dr(dn)] = r;
    logic(c, r);
}

#[inline(always)]
pub fn ext(c: &mut Cpu, _m: &mut Mem, kind: u8, dn: u8) {
    let d = c.r[dr(dn)];
    let r = match kind {
        0 => {
            let v = sext(d, Sz::B);
            c.r[dr(dn)] = (d & 0xffff_0000) | (v & 0xffff);
            v
        }
        1 => {
            let v = sext(d, Sz::W);
            c.r[dr(dn)] = v;
            v
        }
        _ => {
            let v = sext(d, Sz::B);
            c.r[dr(dn)] = v;
            v
        }
    };
    logic(c, r);
}

#[inline(always)]
pub fn swap(c: &mut Cpu, _m: &mut Mem, dn: u8) {
    let r = c.r[dr(dn)].rotate_left(16);
    c.r[dr(dn)] = r;
    logic(c, r);
}

#[inline(always)]
pub fn mull(c: &mut Cpu, m: &mut Mem, src: Ea, dn: u8, _signed: bool) {
    let s = rd(c, m, src, Sz::L);
    let r = s.wrapping_mul(c.r[dr(dn)]);
    c.r[dr(dn)] = r;
    logic(c, r);
}

#[inline(always)]
pub fn mulw(c: &mut Cpu, m: &mut Mem, src: Ea, dn: u8, signed: bool) {
    let s = rd(c, m, src, Sz::W);
    let d = c.r[dr(dn)];
    let r = if signed {
        sext(s, Sz::W).wrapping_mul(sext(d, Sz::W))
    } else {
        (s & 0xffff).wrapping_mul(d & 0xffff)
    };
    c.r[dr(dn)] = r;
    logic(c, r);
}

/// A divide by zero is left to the exception hook, like an instruction
/// this core does not have.
#[inline(always)]
pub fn divl(c: &mut Cpu, m: &mut Mem, src: Ea, dq: u8, dr_: u8, signed: bool, pc: u32) {
    let den = rd(c, m, src, Sz::L);
    if den == 0 {
        return unsupported(c, m, 0, pc);
    }
    let num = c.r[dr(dq)];
    let (quot, rem) = if signed {
        (
            (num as i32).wrapping_div(den as i32) as u32,
            (num as i32).wrapping_rem(den as i32) as u32,
        )
    } else {
        (num / den, num % den)
    };
    logic(c, quot);
    if dq & 7 == dr_ & 7 {
        c.r[dr(dq)] = quot;
    } else {
        c.r[dr(dr_)] = rem;
    }
}

#[inline(always)]
pub fn divw(c: &mut Cpu, m: &mut Mem, src: Ea, dn: u8, signed: bool, pc: u32) {
    let s = rd(c, m, src, Sz::W);
    let num = c.r[dr(dn)];
    let (quot, rem, over);
    if signed {
        let den = sext(s, Sz::W) as i32;
        if den == 0 {
            return unsupported(c, m, 0, pc);
        }
        quot = (num as i32).wrapping_div(den) as u32;
        rem = (num as i32).wrapping_rem(den) as u32;
        over = quot != sext(quot, Sz::W);
    } else {
        if s == 0 {
            return unsupported(c, m, 0, pc);
        }
        quot = num / s;
        rem = num % s;
        over = quot > 0xffff;
    }
    c.cc_c = 0;
    if over {
        c.cc_v = 0x8000_0000;
        c.cc_z = 1;
        return;
    }
    c.r[dr(dn)] = (quot & 0xffff) | (rem << 16);
    c.cc_z = sext(quot, Sz::W);
    c.cc_n = sext(quot, Sz::W);
    c.cc_v = 0;
}

#[inline(always)]
pub fn shift_i(c: &mut Cpu, _m: &mut Mem, left: bool, logical: bool, count: u8, dn: u8) {
    let v = c.r[dr(dn)];
    let n = count as u32;
    let (r, cy);
    if left {
        cy = (v >> (32 - n)) & 1;
        r = v << n;
    } else {
        cy = (v >> (n - 1)) & 1;
        r = if logical { v >> n } else { ((v as i32) >> n) as u32 };
    }
    c.r[dr(dn)] = r;
    c.cc_v = 0;
    c.cc_c = cy;
    c.cc_x = cy;
    c.cc_n = r;
    c.cc_z = r;
}

#[inline(always)]
pub fn shift_r(c: &mut Cpu, _m: &mut Mem, left: bool, logical: bool, cr: u8, dn: u8) {
    let v = c.r[dr(dn)];
    let n = c.r[dr(cr)] & 63;
    let (r, cy);
    if left {
        let t = (v as u64) << n;
        r = t as u32;
        cy = ((t >> 32) & 1) as u32;
    } else {
        let t = (v as u64) << 32;
        let t = if logical { t >> n } else { ((t as i64) >> n) as u64 };
        r = (t >> 32) as u32;
        cy = (t as u32) >> 31;
    }
    c.r[dr(dn)] = r;
    c.cc_v = 0;
    c.cc_c = cy;
    if n != 0 {
        c.cc_x = cy;
    }
    c.cc_n = r;
    c.cc_z = r;
}

#[inline(always)]
pub fn bit_op(c: &mut Cpu, m: &mut Mem, kind: u8, bit: Ea, dst: Ea) {
    let b = match bit {
        Ea::Imm(v) => v,
        Ea::D(n) => c.r[dr(n)],
        _ => 0,
    };
    let (loc, sz, mask) = match dst {
        Ea::D(n) => (Loc::R(dr(n)), Sz::L, 1u32 << (b & 31)),
        other => (resolve(c, other, Sz::B), Sz::B, 1u32 << (b & 7)),
    };
    let v = load(c, m, loc, sz);
    c.cc_z = v & mask;
    match kind & 3 {
        1 => store(c, m, loc, sz, v ^ mask),
        2 => store(c, m, loc, sz, v & !mask),
        3 => store(c, m, loc, sz, v | mask),
        _ => {}
    }
}

#[inline(always)]
pub fn scc(c: &mut Cpu, _m: &mut Mem, cc: u8, dn: u8) {
    let v = if cond(c, cc) { 0xff } else { 0 };
    c.r[dr(dn)] = (c.r[dr(dn)] & 0xffff_ff00) | v;
}

#[inline(always)]
pub fn bcc(c: &mut Cpu, _m: &mut Mem, cc: u8, target: u32, next: u32) {
    c.pc = if cond(c, cc) { target } else { next };
}

#[inline(always)]
pub fn bra(c: &mut Cpu, _m: &mut Mem, target: u32) {
    c.pc = target;
}

#[inline(always)]
pub fn bsr(c: &mut Cpu, m: &mut Mem, target: u32, next: u32) {
    push(c, m, next);
    c.pc = target;
}

#[inline(always)]
pub fn jmp(c: &mut Cpu, _m: &mut Mem, dst: Ea) {
    c.pc = addr_of(c, dst);
}

#[inline(always)]
pub fn jsr(c: &mut Cpu, m: &mut Mem, dst: Ea, next: u32) {
    let a = addr_of(c, dst);
    push(c, m, next);
    c.pc = a;
}

#[inline(always)]
pub fn rts(c: &mut Cpu, m: &mut Mem) {
    let sp = c.r[SP];
    c.pc = m.r32(sp);
    c.r[SP] = sp.wrapping_add(4);
}

#[inline(always)]
pub fn rte(c: &mut Cpu, m: &mut Mem) {
    let sp = c.r[SP];
    let fmt = m.r32(sp);
    c.pc = m.r32(sp.wrapping_add(4));
    c.r[SP] = (sp | ((fmt >> 28) & 3)).wrapping_add(8);
    c.set_sr(fmt);
}

#[inline(always)]
pub fn nop(_c: &mut Cpu, _m: &mut Mem) {}

/// `trap #n`: vector 32 + n, returning to the instruction after it. With
/// an empty vector it is left to the caller, like an instruction this core
/// does not have.
#[inline(always)]
pub fn trap(c: &mut Cpu, m: &mut Mem, n: u8, pc: u32) {
    let vbr = c.vbr;
    if !exception(c, m, 32 + n as u32, None, pc.wrapping_add(2), vbr) {
        unsupported(c, m, 0x4e40 + n as u32, pc);
    }
}

/// Enter exception `vec`: push the 8-byte ColdFire frame (format 4, the
/// vector, the interrupted SR, `return_pc`), set S, raise the interrupt mask
/// to `level` for an interrupt (None for a trap or fault), and jump through
/// the vector table at `vbr`. This is digiemu's `Machine.raise_vector`.
/// -> false, with nothing changed, if the vector is empty.
#[inline]
pub fn exception(
    c: &mut Cpu,
    m: &mut Mem,
    vec: u32,
    level: Option<u32>,
    return_pc: u32,
    vbr: u32,
) -> bool {
    let handler = m.r32(vbr.wrapping_add(vec * 4));
    if handler == 0 {
        return false;
    }
    let sr = c.full_sr() & 0xffff;
    let sp = c.r[SP].wrapping_sub(8);
    m.w16(sp, 0x4000 | ((vec << 2) & 0x0ffc));
    m.w16(sp.wrapping_add(2), sr);
    m.w32(sp.wrapping_add(4), return_pc);
    c.r[SP] = sp;
    let mut live = sr | 0x2000;
    if let Some(level) = level {
        live = (live & !0x0700) | ((level & 7) << 8);
    }
    c.set_sr(live);
    c.pc = handler;
    true
}

#[inline(always)]
pub fn link(c: &mut Cpu, m: &mut Mem, an: u8, disp: i16) {
    let tmp = c.r[SP].wrapping_sub(4);
    m.w32(tmp, c.r[ar(an)]);
    if an & 7 != 7 {
        c.r[ar(an)] = tmp;
    }
    c.r[SP] = tmp.wrapping_add(disp as i32 as u32);
}

#[inline(always)]
pub fn unlk(c: &mut Cpu, m: &mut Mem, an: u8) {
    let a = c.r[ar(an)];
    let v = m.r32(a);
    c.r[ar(an)] = v;
    c.r[SP] = a.wrapping_add(4);
}

#[inline(always)]
pub fn movem(c: &mut Cpu, m: &mut Mem, to_mem: bool, ea: Ea, mask: u16) {
    let mut addr = addr_of(c, ea);
    let mut i = 0;
    while i < 16 {
        if mask & (1 << i) != 0 {
            if to_mem {
                m.w32(addr, c.r[i]);
            } else {
                c.r[i] = m.r32(addr);
            }
            addr = addr.wrapping_add(4);
        }
        i += 1;
    }
}

#[inline(always)]
pub fn move_from_sr(c: &mut Cpu, _m: &mut Mem, dn: u8) {
    c.r[dr(dn)] = (c.r[dr(dn)] & 0xffff_0000) | (c.full_sr() & 0xffff);
}

#[inline(always)]
pub fn move_from_ccr(c: &mut Cpu, _m: &mut Mem, dn: u8) {
    c.r[dr(dn)] = (c.r[dr(dn)] & 0xffff_0000) | c.ccr();
}

#[inline(always)]
pub fn move_to_ccr(c: &mut Cpu, m: &mut Mem, src: Ea) {
    let v = rd(c, m, src, Sz::W);
    c.set_ccr(v);
}

#[inline(always)]
pub fn move_to_sr(c: &mut Cpu, m: &mut Mem, src: Ea) {
    let v = rd(c, m, src, Sz::W);
    c.set_sr(v);
}

#[inline(always)]
pub fn ff1(c: &mut Cpu, _m: &mut Mem, dn: u8) {
    let d = c.r[dr(dn)];
    logic(c, d);
    c.r[dr(dn)] = d.leading_zeros();
}

#[inline(always)]
pub fn byterev(c: &mut Cpu, _m: &mut Mem, dn: u8) {
    c.r[dr(dn)] = c.r[dr(dn)].swap_bytes();
}

#[inline(always)]
pub fn bitrev(c: &mut Cpu, _m: &mut Mem, dn: u8) {
    c.r[dr(dn)] = c.r[dr(dn)].reverse_bits();
}

/// Saturate after an overflowed add or subtract: the wrapped result has the
/// opposite sign to the true one.
#[inline(always)]
pub fn sats(c: &mut Cpu, _m: &mut Mem, dn: u8) {
    let mut v = c.r[dr(dn)];
    if c.cc_v >> 31 != 0 {
        v = (((v as i32) >> 31) as u32) ^ 0x8000_0000;
    }
    c.r[dr(dn)] = v;
    logic(c, v);
}

#[inline(always)]
pub fn unsupported(c: &mut Cpu, _m: &mut Mem, _op: u32, pc: u32) {
    c.halted = pc.wrapping_add(1);
    c.pc = pc;
}

// ---- EMAC ---------------------------------------------------------------

#[inline(always)]
fn signed_int(macsr: u32) -> bool {
    macsr & (MACSR_FI | MACSR_SU) == 0
}

#[inline(always)]
fn clear_mac_flags(c: &mut Cpu) {
    c.macsr &= !(MACSR_V | MACSR_Z | MACSR_N | MACSR_EV);
}

#[inline(always)]
fn mac_word(macsr: u32, v: u32, upper: bool) -> u32 {
    if macsr & MACSR_FI != 0 {
        if upper {
            v & 0xffff_0000
        } else {
            v << 16
        }
    } else if signed_int(macsr) {
        if upper {
            ((v as i32) >> 16) as u32
        } else {
            v as u16 as i16 as i32 as u32
        }
    } else if upper {
        v >> 16
    } else {
        v & 0xffff
    }
}

#[inline(always)]
fn mul_frac(c: &Cpu, a: u32, b: u32) -> u64 {
    let product = ((a as i32 as i64).wrapping_mul(b as i32 as i64) as u64) << 1;
    let rem = (product & 0xff_ffff) as u32;
    let mut field = product >> 24;
    if c.macsr & MACSR_RT != 0 && (rem > 0x80_0000 || (rem == 0x80_0000 && field & 1 != 0)) {
        field = field.wrapping_add(1);
    }
    field &= (1u64 << 40) - 1;
    if a == 0x8000_0000 && b == 0x8000_0000 {
        return field;
    }
    (((field << 24) as i64) >> 24) as u64
}

#[inline(always)]
fn mul_signed(c: &mut Cpu, a: u32, b: u32) -> u64 {
    let product = (a as i32 as i64).wrapping_mul(b as i32 as i64);
    let mut res = (product << 24) >> 24;
    if res != product {
        c.macsr |= MACSR_V;
        if c.macsr & MACSR_OMC != 0 {
            res = if product < 0 { !(1i64 << 50) } else { 1i64 << 50 };
        }
    }
    res as u64
}

#[inline(always)]
fn mul_unsigned(c: &mut Cpu, a: u32, b: u32) -> u64 {
    let mut product = (a as u64).wrapping_mul(b as u64);
    if product & (0xff_ffffu64 << 40) != 0 {
        c.macsr |= MACSR_V;
        if c.macsr & MACSR_OMC != 0 {
            product = 1u64 << 50;
        } else {
            product &= (1u64 << 40) - 1;
        }
    }
    product
}

#[inline(always)]
fn sat_frac(c: &mut Cpu, acc: usize) {
    let sum = c.macc[acc] as i64;
    let mut result = (sum << 16) >> 16;
    if result != sum {
        c.macsr |= MACSR_V;
    }
    if c.macsr & MACSR_V != 0 {
        c.macsr |= MACSR_PAV0 << acc;
        if c.macsr & MACSR_OMC != 0 {
            result = if sum < 0 { 0xffff_ff80_0000_0000u64 as i64 } else { 0x007f_ffff_ff00 };
        }
    }
    c.macc[acc] = result as u64;
}

#[inline(always)]
fn sat_signed(c: &mut Cpu, acc: usize) {
    let tmp = c.macc[acc] as i64;
    let mut result = (tmp << 16) >> 16;
    if result != tmp {
        c.macsr |= MACSR_V;
    }
    if c.macsr & MACSR_V != 0 {
        c.macsr |= MACSR_PAV0 << acc;
        if c.macsr & MACSR_OMC != 0 {
            result = if tmp < 0 { !0x7fff_ffffi64 } else { 0x7fff_ffff };
        }
    }
    c.macc[acc] = result as u64;
}

#[inline(always)]
fn sat_unsigned(c: &mut Cpu, acc: usize) {
    let mut val = c.macc[acc];
    if val & (0xffffu64 << 48) != 0 {
        c.macsr |= MACSR_V;
    }
    if c.macsr & MACSR_V != 0 {
        c.macsr |= MACSR_PAV0 << acc;
        if c.macsr & MACSR_OMC != 0 {
            val = if val > (1u64 << 53) { 0 } else { (1u64 << 48) - 1 };
        } else {
            val &= (1u64 << 48) - 1;
        }
    }
    c.macc[acc] = val;
}

#[inline(always)]
fn set_mac_flags(c: &mut Cpu, acc: usize) {
    let val = c.macc[acc];
    if val == 0 {
        c.macsr |= MACSR_Z;
    } else if val & (1u64 << 47) != 0 {
        c.macsr |= MACSR_N;
    }
    if c.macsr & (MACSR_PAV0 << acc) != 0 {
        c.macsr |= MACSR_V;
    }
    if c.macsr & MACSR_FI != 0 {
        let t = (val as i64) >> 39;
        if t != 0 && t != -1 {
            c.macsr |= MACSR_EV;
        }
    } else if c.macsr & MACSR_SU == 0 {
        let t = (val as i64) >> 31;
        if t != 0 && t != -1 {
            c.macsr |= MACSR_EV;
        }
    } else if val >> 32 != 0 {
        c.macsr |= MACSR_EV;
    }
}

/// MAC and MSAC, with or without the parallel load. `fl`: bit 0 word
/// operands, bit 1 upper word of Rx, bit 2 upper word of Ry, bit 3 MSAC.
/// `lmode` is the load's addressing mode (2..5), 0 for none.
#[allow(clippy::too_many_arguments)]
#[inline(always)]
pub fn mac(
    c: &mut Cpu,
    m: &mut Mem,
    acc: u8,
    rx: u8,
    ry: u8,
    fl: u8,
    scale: u8,
    lmode: u8,
    an: u8,
    disp: i16,
    masked: bool,
    rw: u8,
) {
    let acc = (acc & 3) as usize;
    let mut addr = 0;
    let mut loadval = 0;
    if lmode != 0 {
        let base = c.r[ar(an)];
        let t = match lmode {
            4 => base.wrapping_sub(4),
            5 => base.wrapping_add(disp as i32 as u32),
            _ => base,
        };
        addr = if masked { t & c.mac_mask } else { t };
        loadval = m.r32(addr);
    }
    let mut x = c.r[(rx & 15) as usize];
    let mut y = c.r[(ry & 15) as usize];
    clear_mac_flags(c);
    if fl & 1 != 0 {
        x = mac_word(c.macsr, x, fl & 2 != 0);
        y = mac_word(c.macsr, y, fl & 4 != 0);
    }
    let frac = c.macsr & MACSR_FI != 0;
    let signed = signed_int(c.macsr);
    let product = if frac {
        mul_frac(c, x, y)
    } else {
        let p = if signed { mul_signed(c, x, y) } else { mul_unsigned(c, x, y) };
        match scale & 3 {
            1 => p << 1,
            3 => {
                if signed {
                    ((p as i64) >> 1) as u64
                } else {
                    p >> 1
                }
            }
            _ => p,
        }
    };
    c.macc[acc] = if fl & 8 != 0 {
        c.macc[acc].wrapping_sub(product)
    } else {
        c.macc[acc].wrapping_add(product)
    };
    if frac {
        sat_frac(c, acc);
    } else if signed {
        sat_signed(c, acc);
    } else {
        sat_unsigned(c, acc);
    }
    set_mac_flags(c, acc);
    if lmode != 0 {
        c.r[(rw & 15) as usize] = loadval;
        match lmode {
            3 => c.r[ar(an)] = addr.wrapping_add(4),
            4 => c.r[ar(an)] = addr,
            _ => {}
        }
    }
}

#[inline(always)]
fn get_frac(c: &Cpu, val: u64) -> u32 {
    let acc = val as i64;
    if c.macsr & MACSR_SU != 0 {
        let rem = (acc & 0xff_ffff) as u32;
        let mut q = acc >> 24;
        if rem > 0x80_0000 || (rem == 0x80_0000 && q & 1 != 0) {
            q += 1;
        }
        if c.macsr & MACSR_OMC != 0 && q != q as i16 as i64 {
            q = if acc < 0 { -0x8000 } else { 0x7fff };
        }
        return (q as u32) & 0xffff;
    }
    let rem = (acc & 0xff) as u32;
    let mut q = acc >> 8;
    if c.macsr & MACSR_RT != 0 && (rem > 0x80 || (rem == 0x80 && q & 1 != 0)) {
        q += 1;
    }
    if c.macsr & MACSR_OMC != 0 && q != q as i32 as i64 {
        q = if acc < 0 { i32::MIN as i64 } else { i32::MAX as i64 };
    }
    q as u32
}

#[inline(always)]
pub fn from_mac(c: &mut Cpu, _m: &mut Mem, acc: u8, reg: u8, clear: bool) {
    let a = (acc & 3) as usize;
    let val = c.macc[a];
    let v = if c.macsr & MACSR_FI != 0 {
        get_frac(c, val)
    } else if c.macsr & MACSR_OMC == 0 {
        val as u32
    } else if signed_int(c.macsr) {
        if val == val as i32 as i64 as u64 {
            val as u32
        } else if (val as i64) < 0 {
            0x8000_0000
        } else {
            0x7fff_ffff
        }
    } else if val >> 32 == 0 {
        val as u32
    } else {
        0xffff_ffff
    };
    c.r[(reg & 15) as usize] = v;
    if clear {
        c.macc[a] = 0;
        c.macsr &= !(MACSR_PAV0 << a);
    }
}

#[inline(always)]
pub fn to_mac(c: &mut Cpu, m: &mut Mem, acc: u8, src: Ea) {
    let a = (acc & 3) as usize;
    let v = rd(c, m, src, Sz::L);
    c.macc[a] = if c.macsr & MACSR_FI != 0 {
        ((v as i32 as i64) << 8) as u64
    } else if signed_int(c.macsr) {
        v as i32 as i64 as u64
    } else {
        v as u64
    };
    c.macsr &= !(MACSR_PAV0 << a);
    clear_mac_flags(c);
    set_mac_flags(c, a);
}

#[inline(always)]
pub fn from_macsr(c: &mut Cpu, _m: &mut Mem, reg: u8) {
    c.r[(reg & 15) as usize] = c.macsr;
}

/// A change of FI or S/U repacks each accumulator: the registers are ACCn
/// and its extension bytes, and the mode only says how they make 48 bits.
#[inline(always)]
pub fn to_macsr(c: &mut Cpu, m: &mut Mem, src: Ea) {
    let val = rd(c, m, src, Sz::L);
    if (c.macsr ^ val) & (MACSR_FI | MACSR_SU) != 0 {
        let mut i = 0;
        while i < 4 {
            let regval = c.macc[i];
            let exthigh = (regval >> 40) as u8 as i8;
            let (acc, extlow) = if c.macsr & MACSR_FI != 0 {
                ((regval >> 8) as u32, regval as u8)
            } else {
                (regval as u32, (regval >> 32) as u8)
            };
            c.macc[i] = if val & MACSR_FI != 0 {
                (((acc as u64) << 8) | extlow as u64) | (((exthigh as i64) << 40) as u64)
            } else if val & MACSR_SU == 0 {
                (acc as u64 | ((extlow as u64) << 32)) | (((exthigh as i64) << 40) as u64)
            } else {
                acc as u64 | ((extlow as u64) << 32) | ((exthigh as u8 as u64) << 40)
            };
            i += 1;
        }
    }
    c.macsr = val;
}

#[inline(always)]
pub fn from_mask(c: &mut Cpu, _m: &mut Mem, reg: u8) {
    c.r[(reg & 15) as usize] = c.mac_mask;
}

#[inline(always)]
pub fn to_mask(c: &mut Cpu, m: &mut Mem, src: Ea) {
    c.mac_mask = rd(c, m, src, Sz::L) | 0xffff_0000;
}

#[inline(always)]
pub fn from_mext(c: &mut Cpu, _m: &mut Mem, hi: bool, reg: u8) {
    let a = if hi { 2 } else { 0 };
    let (lo_acc, hi_acc) = (c.macc[a], c.macc[a + 1]);
    let v = if c.macsr & MACSR_FI != 0 {
        ((lo_acc & 0xff) as u32)
            | (((lo_acc >> 32) & 0xff00) as u32)
            | (((hi_acc << 16) & 0x00ff_0000) as u32)
            | (((hi_acc >> 16) & 0xff00_0000) as u32)
    } else {
        (((lo_acc >> 32) & 0xffff) as u32) | (((hi_acc >> 16) & 0xffff_0000) as u32)
    };
    c.r[(reg & 15) as usize] = v;
}

#[inline(always)]
pub fn to_mext(c: &mut Cpu, m: &mut Mem, hi: bool, src: Ea) {
    let a = if hi { 2 } else { 0 };
    let val = rd(c, m, src, Sz::L);
    if c.macsr & MACSR_FI != 0 {
        let mut res = (c.macc[a] & 0xff_ffff_ff00) as i64;
        res |= ((val & 0xff00) as u16 as i16 as i64) << 32;
        res |= (val & 0xff) as i64;
        c.macc[a] = res as u64;
        let mut res = (c.macc[a + 1] & 0xff_ffff_ff00) as i64;
        res |= ((val & 0xff00_0000) as i32 as i64) << 16;
        res |= ((val >> 16) & 0xff) as i64;
        c.macc[a + 1] = res as u64;
    } else if signed_int(c.macsr) {
        let mut res = (c.macc[a] as u32) as i64;
        res |= (val as u16 as i16 as i64) << 32;
        c.macc[a] = res as u64;
        let mut res = (c.macc[a + 1] as u32) as i64;
        res |= ((val & 0xffff_0000) as i32 as i64) << 16;
        c.macc[a + 1] = res as u64;
    } else {
        c.macc[a] = (c.macc[a] as u32) as u64 | (((val & 0xffff) as u64) << 32);
        c.macc[a + 1] = (c.macc[a + 1] as u32) as u64 | (((val & 0xffff_0000) as u64) << 16);
    }
}
