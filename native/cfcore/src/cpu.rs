//! CPU state and guest memory.

/// ColdFire V4e state. Condition codes are kept apart, as the reference
/// engine keeps them: N is the sign of `cc_n`, Z is `cc_z == 0`, V is the
/// sign of `cc_v`, C and X are 0 or 1.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Cpu {
    /// D0..D7 then A0..A7.
    pub r: [u32; 16],
    pub pc: u32,
    pub cc_n: u32,
    pub cc_z: u32,
    pub cc_v: u32,
    pub cc_c: u32,
    pub cc_x: u32,
    /// The status register without the condition codes.
    pub sr: u32,
    pub macc: [u64; 4],
    pub macsr: u32,
    pub mac_mask: u32,
    /// Set by an instruction this core does not implement: its address + 1.
    pub halted: u32,
    /// The vector base register: where the exception vector table is.
    pub vbr: u32,
}

impl Cpu {
    #[inline(always)]
    pub fn ccr(&self) -> u32 {
        (self.cc_x << 4)
            | ((self.cc_n >> 31) << 3)
            | (((self.cc_z == 0) as u32) << 2)
            | ((self.cc_v >> 31) << 1)
            | self.cc_c
    }

    #[inline(always)]
    pub fn set_ccr(&mut self, v: u32) {
        self.cc_c = v & 1;
        self.cc_v = ((v >> 1) & 1) << 31;
        self.cc_z = ((v >> 2) & 1) ^ 1;
        self.cc_n = ((v >> 3) & 1) << 31;
        self.cc_x = (v >> 4) & 1;
    }

    pub fn full_sr(&self) -> u32 {
        self.sr | self.ccr()
    }

    #[inline(always)]
    pub fn set_sr(&mut self, v: u32) {
        self.sr = v & 0xffe0;
        self.set_ccr(v);
    }
}


const CHUNK_BITS: u32 = 20;
const CHUNK: usize = 1 << CHUNK_BITS;

/// What sits behind device memory. `access` is called before the guest's
/// read or write of a device megabyte happens, as a Unicorn memory hook is:
/// a read then returns whatever the memory holds once this returns, and a
/// write lands in it afterwards. `m` has no bus while this runs.
pub trait Bus {
    fn access(&mut self, m: &mut Mem, write: bool, addr: u32, size: u32, value: u32);
}

/// Guest memory as 1 MB chunks behind a table of host pointers, each entry
/// already offset by its guest base. RAM is in `table` and is accessed
/// inline. Device megabytes are in `devices`: also plain memory, but every
/// guest access to one goes to the slow path, which calls the bus first.
/// An address in neither records a fault.
pub struct Mem {
    table: Box<[*mut u8; 4096]>,
    devices: Box<[*mut u8; 4096]>,
    chunks: Vec<Box<[u8]>>,
    pub bus: Option<Box<dyn Bus>>,
    /// First address that had no memory behind it, + 1.
    pub fault: u64,
}

impl Default for Mem {
    fn default() -> Self {
        Self::new()
    }
}

impl Mem {
    pub fn new() -> Self {
        Mem {
            table: Box::new([core::ptr::null_mut(); 4096]),
            devices: Box::new([core::ptr::null_mut(); 4096]),
            chunks: Vec::new(),
            bus: None,
            fault: 0,
        }
    }

    fn chunk(&mut self, i: usize) -> *mut u8 {
        // Padding so an unaligned access at the very end stays inside.
        let mut chunk = vec![0u8; CHUNK + 8].into_boxed_slice();
        let p = chunk.as_mut_ptr().wrapping_sub(i << CHUNK_BITS);
        self.chunks.push(chunk);
        p
    }

    /// Back the megabyte holding `addr` with zeroed RAM, unless it is
    /// already RAM or a device.
    pub fn map(&mut self, addr: u32) {
        let i = (addr >> CHUNK_BITS) as usize;
        if self.table[i].is_null() && self.devices[i].is_null() {
            self.table[i] = self.chunk(i);
        }
    }

    /// Make the megabyte holding `addr` device memory.
    pub fn map_device(&mut self, addr: u32) {
        let i = (addr >> CHUNK_BITS) as usize;
        if self.devices[i].is_null() {
            self.devices[i] = if self.table[i].is_null() { self.chunk(i) } else { self.table[i] };
            self.table[i] = core::ptr::null_mut();
        }
    }

    /// Make the megabyte holding `addr` another view of the RAM megabyte
    /// holding `target`, as the board's DDR repeats through its window.
    pub fn alias(&mut self, addr: u32, target: u32) {
        self.map(target);
        let (i, j) = ((addr >> CHUNK_BITS) as usize, (target >> CHUNK_BITS) as usize);
        self.table[i] = self.table[j].wrapping_add(j << CHUNK_BITS).wrapping_sub(i << CHUNK_BITS);
    }

    pub fn is_device(&self, addr: u32) -> bool {
        !self.devices[(addr >> CHUNK_BITS) as usize].is_null()
    }

    pub fn is_mapped(&self, addr: u32) -> bool {
        !self.any(addr).is_null()
    }

    /// The host byte for `addr`, RAM or device, with no bus call.
    #[inline(always)]
    fn any(&self, addr: u32) -> *mut u8 {
        let i = (addr >> CHUNK_BITS) as usize;
        let p = if self.table[i].is_null() { self.devices[i] } else { self.table[i] };
        if p.is_null() {
            p
        } else {
            p.wrapping_add(addr as usize)
        }
    }

    /// Write from the host: no bus call, and missing memory becomes RAM.
    pub fn write_bytes(&mut self, addr: u32, data: &[u8]) {
        for (i, b) in data.iter().enumerate() {
            let a = addr.wrapping_add(i as u32);
            self.map(a);
            unsafe { *self.any(a) = *b }
        }
    }

    /// Read from the host: no bus call, zero where there is no memory.
    pub fn read_bytes(&self, addr: u32, out: &mut [u8]) {
        for (i, b) in out.iter_mut().enumerate() {
            let p = self.any(addr.wrapping_add(i as u32));
            *b = if p.is_null() { 0 } else { unsafe { *p } };
        }
    }

    #[cold]
    #[inline(never)]
    fn slow_read(&mut self, addr: u32, size: u32) -> u32 {
        if self.devices[(addr >> CHUNK_BITS) as usize].is_null() {
            if self.fault == 0 {
                self.fault = addr as u64 + 1;
            }
            return 0;
        }
        if let Some(mut bus) = self.bus.take() {
            bus.access(self, false, addr, size, 0);
            self.bus = Some(bus);
        }
        let mut b = [0u8; 4];
        self.read_bytes(addr, &mut b[4 - size as usize..]);
        u32::from_be_bytes(b)
    }

    #[cold]
    #[inline(never)]
    fn slow_write(&mut self, addr: u32, size: u32, v: u32) {
        if self.devices[(addr >> CHUNK_BITS) as usize].is_null() {
            if self.fault == 0 {
                self.fault = addr as u64 + 1;
            }
            return;
        }
        if let Some(mut bus) = self.bus.take() {
            bus.access(self, true, addr, size, v);
            self.bus = Some(bus);
        }
        let b = v.to_be_bytes();
        self.write_bytes(addr, &b[4 - size as usize..]);
    }

    #[inline(always)]
    fn host(&self, addr: u32) -> *mut u8 {
        let p = self.table[(addr >> CHUNK_BITS) as usize];
        if p.is_null() {
            p
        } else {
            p.wrapping_add(addr as usize)
        }
    }

    #[inline(always)]
    pub fn r8(&mut self, addr: u32) -> u32 {
        let p = self.host(addr);
        if p.is_null() {
            return self.slow_read(addr, 1);
        }
        unsafe { *p as u32 }
    }

    #[inline(always)]
    pub fn r16(&mut self, addr: u32) -> u32 {
        let p = self.host(addr);
        if p.is_null() {
            return self.slow_read(addr, 2);
        }
        unsafe { u16::from_be((p as *const u16).read_unaligned()) as u32 }
    }

    #[inline(always)]
    pub fn r32(&mut self, addr: u32) -> u32 {
        let p = self.host(addr);
        if p.is_null() {
            return self.slow_read(addr, 4);
        }
        unsafe { u32::from_be((p as *const u32).read_unaligned()) }
    }

    #[inline(always)]
    pub fn w8(&mut self, addr: u32, v: u32) {
        let p = self.host(addr);
        if p.is_null() {
            return self.slow_write(addr, 1, v & 0xff);
        }
        unsafe { *p = v as u8 }
    }

    #[inline(always)]
    pub fn w16(&mut self, addr: u32, v: u32) {
        let p = self.host(addr);
        if p.is_null() {
            return self.slow_write(addr, 2, v & 0xffff);
        }
        unsafe { (p as *mut u16).write_unaligned((v as u16).to_be()) }
    }

    #[inline(always)]
    pub fn w32(&mut self, addr: u32, v: u32) {
        let p = self.host(addr);
        if p.is_null() {
            return self.slow_write(addr, 4, v);
        }
        unsafe { (p as *mut u32).write_unaligned(v.to_be()) }
    }
}
