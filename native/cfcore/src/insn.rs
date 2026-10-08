//! Decoded ColdFire (ISA_C + EMAC) instructions.
//!
//! One table names every instruction, its operands and its handler in
//! `ops`. From it come the enum, the interpreter's dispatch and the text
//! the generator writes, so translated code and the interpreter cannot
//! drift apart: both are the same handler calls.
use crate::cpu::{Cpu, Mem};
use crate::ops;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sz {
    B,
    W,
    L,
}

/// An operand. Register numbers in `Idx`/`PcIdx` index D0..A7 as 0..15.
/// PC-relative displacements are already absolute addresses (`Abs`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ea {
    D(u8),
    A(u8),
    Ind(u8),
    Inc(u8),
    Dec(u8),
    Disp(u8, i16),
    Idx(u8, u8, u8, i8),
    Abs(u32),
    PcIdx(u32, u8, u8, i8),
    Imm(u32),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Alu {
    Add,
    Sub,
    And,
    Or,
    Eor,
}

macro_rules! insns {
    ($( $V:ident => $f:ident ( $($a:ident : $t:ty),* ); )*) => {
        #[derive(Clone, Copy, Debug, PartialEq)]
        pub enum Insn { $( $V($($t),*), )* }

        /// Run one decoded instruction. Control flow sets `cpu.pc`.
        #[inline(always)]
        pub fn exec(c: &mut Cpu, m: &mut Mem, i: Insn) {
            match i { $( Insn::$V($($a),*) => ops::$f(c, m, $($a),*), )* }
        }

        /// The Rust statement that runs `i`: its handler with constants.
        pub fn emit(i: &Insn) -> String {
            match i { $( Insn::$V($($a),*) => {
                let args: Vec<String> = vec![$(format!("{:?}", $a)),*];
                format!("ops::{}(c, m, {});", stringify!($f), args.join(", "))
            } )* }
        }
    };
}

insns! {
    Move => mov(sz: Sz, src: Ea, dst: Ea);
    Movea => movea(sz: Sz, src: Ea, an: u8);
    Moveq => moveq(v: u32, dn: u8);
    Mvs => mvs(sz: Sz, src: Ea, dn: u8);
    Mvz => mvz(sz: Sz, src: Ea, dn: u8);
    Mov3q => mov3q(v: u32, dst: Ea);
    Lea => lea(src: Ea, an: u8);
    Pea => pea(src: Ea);
    Clr => clr(sz: Sz, dst: Ea);
    Tst => tst(sz: Sz, src: Ea);
    AluToReg => alu_to_reg(op: Alu, src: Ea, dn: u8);
    AluToMem => alu_to_mem(op: Alu, dn: u8, dst: Ea);
    Addq => addq(v: u32, dst: Ea, sub: bool);
    Cmp => cmp(sz: Sz, src: Ea, dn: u8);
    Cmpa => cmpa(sz: Sz, src: Ea, an: u8);
    Adda => adda(src: Ea, an: u8, sub: bool);
    Addx => addx(ry: u8, rx: u8, sub: bool);
    Neg => neg(dn: u8);
    Negx => negx(dn: u8);
    Not => not(dn: u8);
    Ext => ext(kind: u8, dn: u8);
    Swap => swap(dn: u8);
    Mull => mull(src: Ea, dn: u8, signed: bool);
    Mulw => mulw(src: Ea, dn: u8, signed: bool);
    Divl => divl(src: Ea, dq: u8, dr: u8, signed: bool, pc: u32);
    Divw => divw(src: Ea, dn: u8, signed: bool, pc: u32);
    ShiftI => shift_i(left: bool, logical: bool, count: u8, dn: u8);
    ShiftR => shift_r(left: bool, logical: bool, cr: u8, dn: u8);
    BitOp => bit_op(kind: u8, bit: Ea, dst: Ea);
    Scc => scc(cc: u8, dn: u8);
    Bcc => bcc(cc: u8, target: u32, next: u32);
    Bra => bra(target: u32);
    Bsr => bsr(target: u32, next: u32);
    Jmp => jmp(dst: Ea);
    Jsr => jsr(dst: Ea, next: u32);
    Rts => rts();
    Rte => rte();
    Trap => trap(n: u8, pc: u32);
    Nop => nop();
    Link => link(an: u8, disp: i16);
    Unlk => unlk(an: u8);
    Movem => movem(to_mem: bool, ea: Ea, mask: u16);
    MoveFromSr => move_from_sr(dn: u8);
    MoveFromCcr => move_from_ccr(dn: u8);
    MoveToCcr => move_to_ccr(src: Ea);
    MoveToSr => move_to_sr(src: Ea);
    Ff1 => ff1(dn: u8);
    Byterev => byterev(dn: u8);
    Bitrev => bitrev(dn: u8);
    Sats => sats(dn: u8);
    Mac => mac(acc: u8, rx: u8, ry: u8, fl: u8, scale: u8, lmode: u8, an: u8, disp: i16, masked: bool, rw: u8);
    FromMac => from_mac(acc: u8, reg: u8, clear: bool);
    ToMac => to_mac(acc: u8, src: Ea);
    FromMacsr => from_macsr(reg: u8);
    ToMacsr => to_macsr(src: Ea);
    FromMask => from_mask(reg: u8);
    ToMask => to_mask(src: Ea);
    FromMext => from_mext(hi: bool, reg: u8);
    ToMext => to_mext(hi: bool, src: Ea);
    Unsupported => unsupported(op: u32, pc: u32);
}

impl Insn {
    /// Does this instruction choose the next PC itself?
    pub fn is_flow(&self) -> bool {
        matches!(
            self,
            Insn::Bcc(..)
                | Insn::Bra(..)
                | Insn::Bsr(..)
                | Insn::Jmp(..)
                | Insn::Jsr(..)
                | Insn::Rts()
                | Insn::Rte()
                | Insn::Trap(..)
                | Insn::Unsupported(..)
        )
    }
}

struct Fetch<'a> {
    get: &'a mut dyn FnMut(u32) -> u16,
    pc: u32,
}

impl Fetch<'_> {
    fn w(&mut self) -> u16 {
        let v = (self.get)(self.pc);
        self.pc = self.pc.wrapping_add(2);
        v
    }

    fn l(&mut self) -> u32 {
        let hi = self.w() as u32;
        (hi << 16) | self.w() as u32
    }
}

fn ea(f: &mut Fetch, mode: u16, reg: u16, sz: Sz) -> Option<Ea> {
    let r = reg as u8;
    Some(match mode {
        0 => Ea::D(r),
        1 => Ea::A(r),
        2 => Ea::Ind(r),
        3 => Ea::Inc(r),
        4 => Ea::Dec(r),
        5 => Ea::Disp(r, f.w() as i16),
        6 => {
            let x = f.w();
            if x & 0x0100 != 0 {
                return None;
            }
            Ea::Idx(r, (x >> 12) as u8, ((x >> 9) & 3) as u8, x as u8 as i8)
        }
        _ => match reg {
            0 => Ea::Abs(f.w() as i16 as i32 as u32),
            1 => Ea::Abs(f.l()),
            2 => {
                let base = f.pc;
                Ea::Abs(base.wrapping_add(f.w() as i16 as i32 as u32))
            }
            3 => {
                let base = f.pc;
                let x = f.w();
                if x & 0x0100 != 0 {
                    return None;
                }
                Ea::PcIdx(base, (x >> 12) as u8, ((x >> 9) & 3) as u8, x as u8 as i8)
            }
            4 => Ea::Imm(match sz {
                Sz::B => (f.w() & 0xff) as u32,
                Sz::W => f.w() as u32,
                Sz::L => f.l(),
            }),
            _ => return None,
        },
    })
}

/// An operand that can be written: not immediate, not PC-relative.
fn alterable(f: &mut Fetch, mode: u16, reg: u16, sz: Sz) -> Option<Ea> {
    if mode == 7 && reg > 1 {
        return None;
    }
    ea(f, mode, reg, sz)
}

/// An operand that is an address in memory (for lea, pea, jmp, jsr).
fn control(f: &mut Fetch, mode: u16, reg: u16) -> Option<Ea> {
    if matches!(mode, 0 | 1 | 3 | 4) || (mode == 7 && reg == 4) {
        return None;
    }
    ea(f, mode, reg, Sz::L)
}

fn size(bits: u16) -> Option<Sz> {
    match bits & 3 {
        0 => Some(Sz::B),
        1 => Some(Sz::W),
        2 => Some(Sz::L),
        _ => None,
    }
}

fn dec(f: &mut Fetch, op: u16, pc: u32) -> Option<Insn> {
    use Insn::*;
    let mode = (op >> 3) & 7;
    let reg = op & 7;
    let r9 = ((op >> 9) & 7) as u8;
    let r0 = reg as u8;
    Some(match op >> 12 {
        0x0 => {
            if op & 0x0100 != 0 {
                if mode == 1 {
                    return None;
                }
                BitOp(((op >> 6) & 3) as u8, Ea::D(r9), alterable(f, mode, reg, Sz::B)?)
            } else if op & 0xff00 == 0x0800 {
                let bit = (f.w() & 0xff) as u32;
                if mode == 1 {
                    return None;
                }
                BitOp(((op >> 6) & 3) as u8, Ea::Imm(bit), alterable(f, mode, reg, Sz::B)?)
            } else {
                match op & 0xfff8 {
                    0x0080 => AluToReg(Alu::Or, Ea::Imm(f.l()), r0),
                    0x0280 => AluToReg(Alu::And, Ea::Imm(f.l()), r0),
                    0x0480 => AluToReg(Alu::Sub, Ea::Imm(f.l()), r0),
                    0x0680 => AluToReg(Alu::Add, Ea::Imm(f.l()), r0),
                    0x0a80 => AluToReg(Alu::Eor, Ea::Imm(f.l()), r0),
                    0x00c0 => Bitrev(r0),
                    0x02c0 => Byterev(r0),
                    0x04c0 => Ff1(r0),
                    0x0c00 => Cmp(Sz::B, Ea::Imm((f.w() & 0xff) as u32), r0),
                    0x0c40 => Cmp(Sz::W, Ea::Imm(f.w() as u32), r0),
                    0x0c80 => Cmp(Sz::L, Ea::Imm(f.l()), r0),
                    _ => return None,
                }
            }
        }
        0x1 | 0x2 | 0x3 => {
            let sz = match op >> 12 {
                1 => Sz::B,
                3 => Sz::W,
                _ => Sz::L,
            };
            let src = ea(f, mode, reg, sz)?;
            let (dm, dr) = ((op >> 6) & 7, (op >> 9) & 7);
            if dm == 1 {
                if sz == Sz::B {
                    return None;
                }
                Movea(sz, src, dr as u8)
            } else {
                Move(sz, src, alterable(f, dm, dr, sz)?)
            }
        }
        0x4 => {
            if op & 0xf1c0 == 0x41c0 {
                return Some(Lea(control(f, mode, reg)?, r9));
            }
            match op & 0xfff8 {
                0x4080 => return Some(Negx(r0)),
                0x40c0 => return Some(MoveFromSr(r0)),
                0x42c0 => return Some(MoveFromCcr(r0)),
                0x4480 => return Some(Neg(r0)),
                0x4680 => return Some(Not(r0)),
                0x4840 => return Some(Swap(r0)),
                0x4880 => return Some(Ext(0, r0)),
                0x48c0 => return Some(Ext(1, r0)),
                0x49c0 => return Some(Ext(2, r0)),
                0x4e50 => return Some(Link(r0, f.w() as i16)),
                0x4e58 => return Some(Unlk(r0)),
                0x4c80 => return Some(Sats(r0)),
                _ => {}
            }
            if op & 0xfff0 == 0x4e40 {
                return Some(Trap((op & 15) as u8, pc));
            }
            match op {
                0x4e71 => return Some(Nop()),
                0x4e73 => return Some(Rte()),
                0x4e75 => return Some(Rts()),
                _ => {}
            }
            match op & 0xffc0 {
                0x4200 | 0x4240 | 0x4280 => Clr(size(op >> 6)?, alterable(f, mode, reg, Sz::L)?),
                0x4a00 | 0x4a40 | 0x4a80 => {
                    let sz = size(op >> 6)?;
                    Tst(sz, ea(f, mode, reg, sz)?)
                }
                0x44c0 => MoveToCcr(ea(f, mode, reg, Sz::W)?),
                0x46c0 => MoveToSr(ea(f, mode, reg, Sz::W)?),
                0x4840 => Pea(control(f, mode, reg)?),
                0x48c0 | 0x4cc0 if mode == 2 || mode == 5 => {
                    let mask = f.w();
                    Movem(op & 0x0400 == 0, ea(f, mode, reg, Sz::L)?, mask)
                }
                0x4c00 => {
                    let x = f.w();
                    if x & 0x0400 != 0 {
                        return None;
                    }
                    Mull(ea(f, mode, reg, Sz::L)?, ((x >> 12) & 7) as u8, x & 0x0800 != 0)
                }
                0x4c40 => {
                    let x = f.w();
                    if x & 0x0400 != 0 {
                        return None;
                    }
                    let src = ea(f, mode, reg, Sz::L)?;
                    Divl(src, ((x >> 12) & 7) as u8, (x & 7) as u8, x & 0x0800 != 0, pc)
                }
                0x4e80 => {
                    let dst = control(f, mode, reg)?;
                    Jsr(dst, f.pc)
                }
                0x4ec0 => Jmp(control(f, mode, reg)?),
                _ => return None,
            }
        }
        0x5 => {
            if op & 0xc0 == 0xc0 {
                if mode == 0 {
                    Scc(((op >> 8) & 15) as u8, r0)
                } else if op & 0xfff8 == 0x51f8 {
                    match reg {
                        2 => {
                            f.w();
                        }
                        3 => {
                            f.l();
                        }
                        4 => {}
                        _ => return None,
                    }
                    Nop()
                } else {
                    return None;
                }
            } else if (op >> 6) & 3 == 2 {
                let v = if r9 == 0 { 8 } else { r9 as u32 };
                Addq(v, alterable(f, mode, reg, Sz::L)?, op & 0x0100 != 0)
            } else {
                return None;
            }
        }
        0x6 => {
            let cc = ((op >> 8) & 15) as u8;
            let base = pc.wrapping_add(2);
            let target = match op as u8 {
                0 => base.wrapping_add(f.w() as i16 as i32 as u32),
                0xff => base.wrapping_add(f.l()),
                d => base.wrapping_add(d as i8 as i32 as u32),
            };
            match cc {
                0 => Bra(target),
                1 => Bsr(target, f.pc),
                _ => Bcc(cc, target, f.pc),
            }
        }
        0x7 => {
            if op & 0x0100 == 0 {
                Moveq(op as u8 as i8 as i32 as u32, r9)
            } else {
                let sz = if op & 0x40 != 0 { Sz::W } else { Sz::B };
                let src = ea(f, mode, reg, sz)?;
                if op & 0x80 != 0 {
                    Mvz(sz, src, r9)
                } else {
                    Mvs(sz, src, r9)
                }
            }
        }
        0x8 | 0xc => {
            let alu = if op >> 12 == 8 { Alu::Or } else { Alu::And };
            if op & 0xc0 == 0xc0 {
                let src = ea(f, mode, reg, Sz::W)?;
                if op >> 12 == 8 {
                    Divw(src, r9, op & 0x0100 != 0, pc)
                } else {
                    Mulw(src, r9, op & 0x0100 != 0)
                }
            } else if (op >> 6) & 3 != 2 {
                return None;
            } else if op & 0x0100 == 0 {
                AluToReg(alu, ea(f, mode, reg, Sz::L)?, r9)
            } else {
                if mode < 2 {
                    return None;
                }
                AluToMem(alu, r9, alterable(f, mode, reg, Sz::L)?)
            }
        }
        0x9 | 0xd => {
            let sub = op >> 12 == 9;
            let alu = if sub { Alu::Sub } else { Alu::Add };
            if op & 0xc0 == 0xc0 {
                if op & 0x0100 == 0 {
                    return None;
                }
                Adda(ea(f, mode, reg, Sz::L)?, r9, sub)
            } else if (op >> 6) & 3 != 2 {
                return None;
            } else if op & 0x0100 == 0 {
                AluToReg(alu, ea(f, mode, reg, Sz::L)?, r9)
            } else if mode == 0 {
                Addx(r0, r9, sub)
            } else if mode == 1 {
                return None;
            } else {
                AluToMem(alu, r9, alterable(f, mode, reg, Sz::L)?)
            }
        }
        0xa => return dec_emac(f, op),
        0xb => {
            let s = (op >> 6) & 3;
            if op & 0x0100 == 0 {
                if s == 3 {
                    Cmpa(Sz::W, ea(f, mode, reg, Sz::W)?, r9)
                } else {
                    let sz = size(s)?;
                    Cmp(sz, ea(f, mode, reg, sz)?, r9)
                }
            } else if s == 3 {
                Cmpa(Sz::L, ea(f, mode, reg, Sz::L)?, r9)
            } else if s == 2 && mode != 1 {
                AluToMem(Alu::Eor, r9, alterable(f, mode, reg, Sz::L)?)
            } else {
                return None;
            }
        }
        0xe => {
            let (left, logical) = (op & 0x0100 != 0, op & 8 != 0);
            match op & 0xf0f0 {
                0xe080 => ShiftI(left, logical, if r9 == 0 { 8 } else { r9 }, r0),
                0xe0a0 => ShiftR(left, logical, r9, r0),
                _ => return None,
            }
        }
        _ => return None,
    })
}

fn dec_emac(f: &mut Fetch, op: u16) -> Option<Insn> {
    use Insn::*;
    let mode = (op >> 3) & 7;
    let reg = op & 7;
    let r9 = ((op >> 9) & 7) as u8;
    let wide = |bit: u16| if op & bit != 0 { 8u8 } else { 0 };
    if op & 0xf100 == 0xa000 {
        let x = f.w();
        let mut acc = (((op >> 7) & 1) | ((x >> 3) & 2)) as u8;
        let fl = ((x & 0x0800 == 0) as u8)
            | (((x & 0x80 != 0) as u8) << 1)
            | (((x & 0x40 != 0) as u8) << 2)
            | (((x & 0x0100 != 0) as u8) << 3);
        let scale = ((x >> 9) & 3) as u8;
        if op & 0x30 != 0 {
            if !(2..=5).contains(&mode) {
                return None;
            }
            let disp = if mode == 5 { f.w() as i16 } else { 0 };
            acc ^= 1;
            return Some(Mac(
                acc,
                (x >> 12) as u8,
                (x & 15) as u8,
                fl,
                scale,
                mode as u8,
                reg as u8,
                disp,
                x & 0x20 != 0,
                r9 + wide(0x40),
            ));
        }
        return Some(Mac(acc, r9 + wide(0x40), (op & 15) as u8, fl, scale, 0, 0, 0, false, 0));
    }
    // The reference decoder registers these in an order where the later
    // entry wins an overlap; this is that order, last first.
    let acc = ((op >> 9) & 3) as u8;
    let hi = op & 0x0400 != 0;
    if op & 0xf1c0 == 0xa140 {
        let v = if r9 == 0 { 0xffff_ffff } else { r9 as u32 };
        Some(Mov3q(v, alterable(f, mode, reg, Sz::L)?))
    } else if op & 0xffc0 == 0xad00 {
        Some(ToMask(ea(f, mode, reg, Sz::L)?))
    } else if op & 0xfbc0 == 0xab00 {
        Some(ToMext(hi, ea(f, mode, reg, Sz::L)?))
    } else if op & 0xffc0 == 0xa900 {
        Some(ToMacsr(ea(f, mode, reg, Sz::L)?))
    } else if op & 0xf9c0 == 0xa100 {
        Some(ToMac(acc, ea(f, mode, reg, Sz::L)?))
    } else if op == 0xa9c0 {
        None
    } else if op & 0xfbf0 == 0xab80 {
        Some(FromMext(hi, (op & 15) as u8))
    } else if op & 0xfff0 == 0xad80 {
        Some(FromMask((op & 15) as u8))
    } else if op & 0xf9f0 == 0xa980 {
        Some(FromMacsr((op & 15) as u8))
    } else if op & 0xf9b0 == 0xa180 {
        Some(FromMac(acc, (op & 15) as u8, op & 0x40 != 0))
    } else {
        None
    }
}

/// Decode the instruction at `pc`. -> (instruction, its length in bytes).
/// Anything not implemented comes back as `Unsupported`, length 2.
pub fn decode(get: &mut dyn FnMut(u32) -> u16, pc: u32) -> (Insn, u32) {
    let mut f = Fetch { get, pc };
    let op = f.w();
    match dec(&mut f, op, pc) {
        Some(i) => (i, f.pc.wrapping_sub(pc)),
        None => (Insn::Unsupported(op as u32, pc), 2),
    }
}
