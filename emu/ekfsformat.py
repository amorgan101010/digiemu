"""The +Drive's ekFS: format a card image and write files into it, offline.

This is the library half of tools/ekfsadd.py, moved here so the portable
app's first run (emu/bootstrap.py) can format a fresh card without tools/ on
sys.path -- the frozen app does not ship tools/. The CLI is unchanged and is
now a thin wrapper over this module. Formatting BEFORE the cold boot matters:
the firmware identifies the card (<60M instructions) and mounts it (<280M)
during the cold boot, and whatever the card holds then is frozen into every
snapshot built from that boot. A card laid down by format_image() is the
firmware's own format byte for byte, and a first boot on it runs to a settled
user interface (measured: settled at 1050M instructions).

tools/ekfs.py reads this filesystem; nothing wrote to it, and the open list
in docs/mk1/10-plusdrive.md called the file inode format unknown. It is not
unknown any more. Everything below was recovered from the firmware's own code
with Ghidra and then checked against an image the firmware itself formatted.

WHAT WAS ESTABLISHED, and how

  The superblock checksum is Bob Jenkins' lookup3, big-endian variant.
  FUN_400d16d6(buf, len, seed) seeds three state words with
  `seed + 0xdeadbeef` -- the firmware writes `seed - 0x21524111`, which is
  the same thing -- streams through FUN_400d1544 and finalises in
  FUN_400d1200. Update rotations 4, 6, 8, 16, 19, 4; final rotations
  14, 11, 25, 16, 4, 14, 24; tail masks 0xff000000 / 0xffff0000 /
  0xffffff00, which is what makes it hashbig rather than hashlittle. It
  differs from the reference in one way: the length is NOT folded into the
  seed. Checked by recomputing a formatted image's stored 0xda3629df.

  FUN_400d0f9c, the mount, refuses the volume unless that checksum matches,
  so it has to be right.

  The inode is 128 bytes:
      +0x00 u8   type, 1 = directory
      +0x01 u8   2, written by the allocator FUN_400d2290
      +0x02 u16  LINK count (see below -- this was misread as an entry count)
      +0x04 u32  size in bytes
      +0x10 u32  serial, from a counter that only ever goes up
      +0x1e u16  extent count -- FUN_400cea9a returns *(u16*)(inode+0x1e)
      +0x20      extents, 12 bytes each:
                 u32 first logical block, u32 count, u32 first physical block

  The extent reading is the correction that made this possible.
  docs/mk1/10-plusdrive.md read +0x2c as "second fork, size = 0x20000".
  0x20000 is not a size: FUN_400cecfc converts a byte range to block indices
  with `offset >> 14`, and a directory's initialiser asks for the ranges
  (0, 0x4000) and (0x80000000, 0xc000). 0x80000000 >> 14 is 0x20000. It is a
  logical block number, and both root and /incoming read cleanly as two
  extents once you look at them that way.

  The bitmaps are plain bit arrays over big-endian 32-bit words: object n is
  bit n & 31 of word n >> 5, which is exactly how FUN_400d2290 scans them.
  Checked against the formatted image, where the inode bitmap holds 0 and 1
  reserved plus root and /incoming, and the block bitmap holds 0..103 with
  96..103 being the two directories' extents.

  Directory entries are u32 inode, u16 record length, u8 name length,
  u8 type, then the name; the last entry's record length runs to the end of
  the 16 KB block. That much was already in the document and is unchanged.

DIRECTORY INDEXES -- what this tool used to get wrong

  A directory's second extent (logical 0x20000, three blocks) is not spare
  space. It holds three sorted indexes over the entries, and the firmware
  lists and searches directories through them, never by scanning the entry
  block. This tool wrote them as zeros, so on a card it formatted or added to,
  the firmware saw every directory as empty: SETTINGS > SAMPLES opened on a
  blank list. Recovered from the firmware's own insert, FUN_400cd04c, and
  checked against a card the firmware formatted itself:

      logical   key                              order
      0x20000   FUN_400cccb6(name) -- ext3's     ascending, unsigned
                legacy dx_hack_hash
      0x20001   first 4 bytes of the name        '.' and '..' first, then
                                                 directories, then files,
                                                 each by FUN_400e8606
      0x20002   the entry's inode number         ascending

  Each index is one block: u16 count at +0, then 8-byte records from +8,
  (u32 key, u32 location) with location = logical_block << 14 | offset of
  the entry. Ties go after existing equal entries. FUN_400cdce4, the
  iterator the UI lists with, walks the 0x20001 index and uses only the
  location word. The firmware refuses a 2001st entry.

  FUN_400e8606 is a natural-order, case-insensitive compare (digit runs by
  value, then by leading-zero count; other bytes by toupper). It has a quirk
  the index has to reproduce: after an equal digit run it skips the next
  character on both sides unread, so 'a1b' and 'a1c' compare equal. That was
  measured by running the firmware's own routine under Unicorn, not inferred.
  Because a compare with that quirk is not guaranteed transitive, the name
  index here is built by replaying the firmware's insertion loop one entry at
  a time, in entry order, rather than by sorting.

  The '.' and '..' name keys are 0x2e00534f and 0x2e2e0053: the firmware
  reads four bytes from a pointer to the literal "." or ".." and runs past
  the NUL into whatever string follows it. They are constants, reproduced.
  A name shorter than four bytes is zero-padded here; the firmware would
  pick up whatever followed it in its buffer. Nothing found reads that key
  -- the iterator and the insert both use the location -- so it cannot
  change what the device shows.

  Inode +0x02 is a link count, not an entry count. The firmware's insert
  never touches the directory's own +0x02; it bumps the TARGET's when a
  directory is linked in. So a directory counts its own '.', its entry in its
  parent and each child directory's '..' -- root 3, an empty /incoming 2 --
  and adding a file leaves it alone. This tool used to bump it once per file.
"""
import collections
import os
import struct

from emu import sparse


SECTOR = 512
REGION = 0x1C0000
INODE_SIZE = 0x80
INODES_PER_CHUNK = 128
CHUNK_SECTORS = 0x20
BLOCK_BYTES = CHUNK_SECTORS * SECTOR          # 16 KB
LOG_BLOCK_SHIFT = 14
SEED = 0x31323334
ROOT_INODE = 2
RAM_INODE = 0x01000000    # at or above this an inode never lives on the card
TYPE_DIR = 1
TYPE_FILE = 0   # open() allocates an inode and never writes a type byte,
                # and FUN_400d2290 zeroes the whole 128 bytes first, so a
                # file is type 0 and only a directory gets 1
MAX_INLINE_EXTENTS = (INODE_SIZE - 0x20) // 12
M = 0xFFFFFFFF


# ---------------------------------------------------------------- checksum
def _rot(x, k):
    return ((x << k) | (x >> (32 - k))) & M


def _mix(a, b, c):
    a = (a - c) & M; a ^= _rot(c, 4);  c = (c + b) & M
    b = (b - a) & M; b ^= _rot(a, 6);  a = (a + c) & M
    c = (c - b) & M; c ^= _rot(b, 8);  b = (b + a) & M
    a = (a - c) & M; a ^= _rot(c, 16); c = (c + b) & M
    b = (b - a) & M; b ^= _rot(a, 19); a = (a + c) & M
    c = (c - b) & M; c ^= _rot(b, 4);  b = (b + a) & M
    return a, b, c


def _final(a, b, c):
    c ^= b; c = (c - _rot(b, 14)) & M
    a ^= c; a = (a - _rot(c, 11)) & M
    b ^= a; b = (b - _rot(a, 25)) & M
    c ^= b; c = (c - _rot(b, 16)) & M
    a ^= c; a = (a - _rot(c, 4)) & M
    b ^= a; b = (b - _rot(a, 14)) & M
    c ^= b; c = (c - _rot(b, 24)) & M
    return a, b, c


_TAIL = {
    12: (None, None, None), 11: (None, None, 0xFFFFFF00),
    10: (None, None, 0xFFFF0000), 9: (None, None, 0xFF000000),
    8: (None, None, 0), 7: (None, 0xFFFFFF00, 0),
    6: (None, 0xFFFF0000, 0), 5: (None, 0xFF000000, 0),
    4: (None, 0, 0), 3: (0xFFFFFF00, 0, 0),
    2: (0xFFFF0000, 0, 0), 1: (0xFF000000, 0, 0),
}


def ekfs_hash(data, initval=SEED):
    """lookup3 hashbig, seeded the way this firmware seeds it."""
    a = b = c = (0xDEADBEEF + initval) & M
    i, n = 0, len(data)
    while n - i > 12:
        k0, k1, k2 = struct.unpack_from('>III', data, i)
        a = (a + k0) & M
        b = (b + k1) & M
        c = (c + k2) & M
        a, b, c = _mix(a, b, c)
        i += 12
    left = n - i
    if left == 0:
        return c
    pad = bytes(data[i:]) + b'\x00' * 12
    k0, k1, k2 = struct.unpack_from('>III', pad, 0)
    m0, m1, m2 = _TAIL[left]
    a = (a + (k0 if m0 is None else k0 & m0)) & M
    if m1 != 0:
        b = (b + (k1 if m1 is None else k1 & m1)) & M
    if m2 != 0:
        c = (c + (k2 if m2 is None else k2 & m2)) & M
    a, b, c = _final(a, b, c)
    return c


# ---------------------------------------------------------------- indexes
INDEX_LOGICAL = (0x20000, 0x20001, 0x20002)    # hash, name, inode
MAX_DIR_ENTRIES = 2000                         # FUN_400cd04c's limit
INDEX_HEADER = 8
DOT_KEYS = {b'.': 0x2E00534F, b'..': 0x2E2E0053}


def dx_hack_hash(name):
    """FUN_400cccb6: ext3's legacy dx_hack_hash. Checked against the
    firmware's own routine under Unicorn on 20 names, 0 mismatches."""
    h0, h1 = 0x12A3FE2D, 0x37ABE8F9
    for c in name:
        h = (h1 + (h0 ^ ((c * 0x6D22F5) & M))) & M
        if h & 0x80000000:
            h = (h + 0x80000001) & M
        h1, h0 = h0, h
    return (h0 << 1) & M


def _toupper(c):
    return c - 0x20 if 0x61 <= c <= 0x7A else c


def _signed(c):
    return c - 256 if c >= 128 else c


def natcmp(a, b):
    """FUN_400e8606, including its skipped-character quirk. Checked against
    the firmware's own routine under Unicorn on 47 pairs, 0 mismatches."""
    i = j = 0
    while True:
        if i >= len(a):
            return -1 if j < len(b) else 0
        if j >= len(b):
            return 1
        ca, cb = a[i], b[j]
        if 0x30 <= ca <= 0x39 and 0x30 <= cb <= 0x39:
            i0, j0 = i, j
            while i < len(a) and a[i] == 0x30:
                i += 1
            while j < len(b) and b[j] == 0x30:
                j += 1
            za, zb = i - i0, j - j0
            ie, je = i, j
            while ie < len(a) and 0x30 <= a[ie] <= 0x39:
                ie += 1
            while je < len(b) and 0x30 <= b[je] <= 0x39:
                je += 1
            if (ie - i) != (je - j):
                return 1 if (ie - i) > (je - j) else -1
            while i < ie:
                if a[i] != b[j]:
                    return -1 if a[i] < b[j] else 1
                i += 1
                j += 1
            if za != zb:
                return 1 if za > zb else -1
            i += 1      # the quirk: the byte after a number is never read
            j += 1
            continue
        ua, ub = _signed(_toupper(ca)), _signed(_toupper(cb))
        if ua != ub:
            return -1 if ua < ub else 1
        i += 1
        j += 1


def name_key(name):
    if name in DOT_KEYS:
        return DOT_KEYS[name]
    return struct.unpack('>I', (name + b'\x00' * 4)[:4])[0]


def build_indexes(entries):
    """entries: [(location, inode, name_bytes, type)] in entry order.

    -> three BLOCK_BYTES buffers, hash / name / inode, built by replaying
    FUN_400cd04c's insertion loop one entry at a time.
    """
    if len(entries) > MAX_DIR_ENTRIES:
        raise Error('%d entries; the firmware allows %d per directory'
                    % (len(entries), MAX_DIR_ENTRIES))
    by_hash, by_name, by_inode = [], [], []
    for loc, ino, name, typ in entries:
        h = dx_hack_hash(name)
        i = 0
        while i < len(by_hash) and by_hash[i][0] <= h:
            i += 1
        by_hash.insert(i, (h, loc))

        i = 0 if len(by_name) < 2 else 2
        while i < len(by_name):
            x_name, x_typ = by_name[i][2], by_name[i][3]
            if x_typ != TYPE_DIR:
                if typ == TYPE_DIR:
                    break                   # directories go before files
            elif typ != TYPE_DIR:
                i += 1                      # a file goes after directories
                continue
            if natcmp(x_name, name) > 0:
                break
            i += 1
        by_name.insert(i, (name_key(name), loc, name, typ))

        i = 0
        while i < len(by_inode) and by_inode[i][0] <= ino:
            i += 1
        by_inode.insert(i, (ino, loc))

    out = []
    for recs in (by_hash, [(k, loc) for k, loc, _n, _t in by_name], by_inode):
        buf = bytearray(BLOCK_BYTES)
        struct.pack_into('>H', buf, 0, len(recs))
        for n, (key, loc) in enumerate(recs):
            struct.pack_into('>II', buf, INDEX_HEADER + 8 * n, key, loc)
        out.append(buf)
    return out


# ---------------------------------------------------------------- samples
# A sample on the +Drive is not a WAV. FUN_400eb666, the firmware's own
# sample-file writer, lays one down as
#     64-byte header | big-endian 16-bit mono PCM | 16 zero bytes
# with header +0x04 = PCM byte length, +0x08 = sample rate, +0x0c = 0,
# +0x10 = 0, +0x14 = 0x7f and everything else zero -- the values its
# recorder path (FUN_400eb922) passes. The trailer is copied from a BSS
# buffer nothing writes, so it is 16 zeros: padding for the interpolator.
# The loader (FUN_400ec6d2) reads the file verbatim into sample RAM and hands
# +0x04, +0x08 and the PCM at +0x40 to the engine (FUN_400763b4, which takes
# a rate of 0 as 48000).
SAMPLE_HEADER = 0x40
SAMPLE_TRAILER = 0x10
SAMPLE_BYTE_14 = 0x7F
MAX_SAMPLE_PCM = 0x4000000             # the loader's (len + 0x50) <= 0x4000050
AUDIO_EXTENSIONS = ('.wav', '.wave')

# Every file the firmware finishes writing is hashed by FUN_400d1a2a: lookup3
# (the same hashbig as the superblock) over the whole file, seeded with
# 0x654c654b -- ASCII "eLeK". The result | 1 goes into inode +0x0c and into an
# on-card table, block 64 + (inode >> 12), word (inode & 0xfff), which the
# mount loads into a sorted hash -> inode index (FUN_400d1932). A file whose
# +0x0c lacks bit 0 is not a sample to FUN_400d1680, and the loader then gets
# an invalid reference and loads nothing -- "SAMPLE MEMORY FULL", which is
# just what the load-done callback prints when the count is zero.
SAMPLE_HASH_SEED = 0x654C654B
HASH_TABLE_BLOCK = 64


WavInfo = collections.namedtuple('WavInfo', 'tag channels rate bits frames pcm')


def wav_info(data):
    """RIFF/WAVE bytes -> WavInfo, with every check wav_to_sample makes.

    Error for anything wav_to_sample would refuse, without converting: a
    caller can vet a batch of files before committing to any of them.
    `pcm` is the data chunk as it is in the file.
    """
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise Error('not a RIFF/WAVE file')
    fmt = pcm = None
    o = 12
    while o + 8 <= len(data):
        cid, size = data[o:o + 4], struct.unpack_from('<I', data, o + 4)[0]
        body = data[o + 8:o + 8 + size]
        if cid == b'fmt ':
            fmt = body
        elif cid == b'data':
            pcm = body
        o += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise Error('WAV has no fmt or data chunk')
    if len(fmt) < 16:
        raise Error('WAV fmt chunk is %d bytes, not 16' % len(fmt))
    tag, chans, rate = struct.unpack_from('<HHI', fmt, 0)
    bits = struct.unpack_from('<H', fmt, 14)[0]
    if tag == 0xFFFE and len(fmt) >= 26:
        tag = struct.unpack_from('<H', fmt, 24)[0]
    if tag not in (1, 3) or chans < 1:
        raise Error('unsupported WAV format tag %d, %d channel(s)'
                    % (tag, chans))
    if tag == 3 and bits != 32 or tag == 1 and bits not in (8, 16, 24, 32):
        raise Error('unsupported %d-bit %s WAV'
                    % (bits, 'float' if tag == 3 else 'integer'))
    frames = len(pcm) // (bits // 8 * chans)
    if frames * 2 > MAX_SAMPLE_PCM:
        raise Error('%d bytes of PCM; the loader takes at most %d'
                    % (frames * 2, MAX_SAMPLE_PCM))
    return WavInfo(tag, chans, rate, bits, frames, pcm)


def wav_to_sample(data):
    """RIFF/WAVE bytes -> (sample file bytes, sample rate, frames).

    Integer PCM of 8/16/24/32 bits and 32-bit float, any channel count
    (averaged to mono -- the mk1 plays mono), WAVE_FORMAT_EXTENSIBLE
    included. The rate is kept rather than resampled: the header has a field
    for it and the engine computes the pitch ratio from it.
    """
    tag, chans, rate, bits, frames, pcm = wav_info(data)
    width = bits // 8
    frame = width * chans
    out = bytearray(frames * 2)
    for i in range(frames):
        acc = 0.0
        base = i * frame
        for ch in range(chans):
            p = base + ch * width
            if tag == 3:
                v = struct.unpack_from('<f', pcm, p)[0]
            elif width == 1:
                v = (pcm[p] - 128) / 128.0
            elif width == 2:
                v = struct.unpack_from('<h', pcm, p)[0] / 32768.0
            elif width == 3:
                v = int.from_bytes(pcm[p:p + 3], 'little', signed=True) \
                    / 8388608.0
            else:
                v = struct.unpack_from('<i', pcm, p)[0] / 2147483648.0
            acc += v
        s = int(round(acc / chans * 32767.0))
        struct.pack_into('>h', out, 2 * i, max(-32768, min(32767, s)))
    head = bytearray(SAMPLE_HEADER)
    struct.pack_into('>III', head, 0x04, len(out), rate, 0)
    struct.pack_into('>I', head, 0x10, 0)
    head[0x14] = SAMPLE_BYTE_14
    return bytes(head) + bytes(out) + bytes(SAMPLE_TRAILER), rate, frames


def sample_name(path):
    """A WAV's name as it goes on the card: no extension, since the
    contents are no longer a WAV (the firmware's recorder writes none)."""
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    return stem if ext.lower() in AUDIO_EXTENSIONS else base


class Error(Exception):
    pass


class Ekfs(object):
    def __init__(self, path, base=REGION, write=False):
        self.path, self.base = path, base
        self.f = open(path, 'r+b' if write else 'rb')
        sb = self.sectors(base, 1)
        if sb[:4] != b'ekFS':
            self.f.close()
            raise Error('%s: no ekFS superblock at sector 0x%x' % (path, base))
        self.sb = bytearray(sb)
        u = self._u
        self.version = u(0x04)
        self.bitmap_bytes = u(0x08)
        self.inode_count = u(0x0c)
        self.block_count = u(0x10)
        self.inode_bitmap_off = u(0x14)
        self.block_bitmap_off = u(0x18)
        self.inode_table_off = u(0x1c)
        self.data_off = u(0x20)
        self.stored_checksum = u(0x1fc)

    def _u(self, o):
        return struct.unpack_from('>I', self.sb, o)[0]

    def close(self):
        self.f.close()

    # -- raw ------------------------------------------------------------
    def sectors(self, sector, n):
        self.f.seek(sector * SECTOR)
        got = self.f.read(n * SECTOR)
        if len(got) < n * SECTOR:
            got += b'\x00' * (n * SECTOR - len(got))
        return got

    def put_sectors(self, sector, data):
        if len(data) % SECTOR:
            raise Error('writes must be whole sectors')
        self.f.seek(sector * SECTOR)
        self.f.write(data)

    def checksum_ok(self):
        return ekfs_hash(bytes(self.sb[:0x1fc]), SEED) == self.stored_checksum

    def reseal(self):
        """Recompute the checksum, then write the superblock and its copy.

        The device keeps a byte-identical copy just past the end of the block
        bitmap, so it has to be rewritten whenever the primary changes.
        """
        h = ekfs_hash(bytes(self.sb[:0x1fc]), SEED)
        struct.pack_into('>I', self.sb, 0x1fc, h)
        self.stored_checksum = h
        self.put_sectors(self.base, bytes(self.sb))
        raw = self._bitmap('block')
        raw[SB_BACKUP_OFF:SB_BACKUP_OFF + SECTOR] = self.sb
        self._put_bitmap('block', raw)
        return h

    # -- bitmaps --------------------------------------------------------
    def _bitmap(self, which):
        off = (self.inode_bitmap_off if which == 'inode'
               else self.block_bitmap_off)
        return bytearray(self.sectors(self.base + off,
                                      self.bitmap_bytes // SECTOR))

    def _put_bitmap(self, which, raw):
        off = (self.inode_bitmap_off if which == 'inode'
               else self.block_bitmap_off)
        self.put_sectors(self.base + off, bytes(raw))

    @staticmethod
    def _bit(raw, n):
        w = (n >> 5) * 4
        return struct.unpack_from('>I', raw, w)[0] >> (n & 31) & 1

    @staticmethod
    def _set_bit(raw, n):
        w = (n >> 5) * 4
        v = struct.unpack_from('>I', raw, w)[0] | (1 << (n & 31))
        struct.pack_into('>I', raw, w, v)

    def used(self, which):
        raw = self._bitmap(which)
        return sum(bin(x).count('1') for x in raw)

    def alloc_inode(self):
        raw = self._bitmap('inode')
        for n in range(2, self.inode_count):
            if not self._bit(raw, n):
                self._set_bit(raw, n)
                self._put_bitmap('inode', raw)
                return n
        raise Error('no free inode')

    def alloc_blocks(self, count):
        """-> [(first_physical, run_length)], preferring one run."""
        raw = self._bitmap('block')
        runs, need = [], count
        n = 0
        while need and n < self.block_count:
            if self._bit(raw, n):
                n += 1
                continue
            start, run = n, 0
            while (run < need and n < self.block_count
                   and not self._bit(raw, n)):
                self._set_bit(raw, n)
                n += 1
                run += 1
            runs.append((start, run))
            need -= run
        if need:
            raise Error('no room: %d blocks short' % need)
        self._put_bitmap('block', raw)
        return runs

    # -- inodes ---------------------------------------------------------
    def _inode_loc(self, n):
        chunk = n // INODES_PER_CHUNK
        sector = self.base + self.inode_table_off + chunk * CHUNK_SECTORS
        return sector, (n % INODES_PER_CHUNK) * INODE_SIZE

    def inode(self, n):
        sector, off = self._inode_loc(n)
        chunk = self.sectors(sector, CHUNK_SECTORS)
        return bytearray(chunk[off:off + INODE_SIZE])

    def put_inode(self, n, raw):
        if len(raw) != INODE_SIZE:
            raise Error('an inode is %d bytes' % INODE_SIZE)
        sector, off = self._inode_loc(n)
        chunk = bytearray(self.sectors(sector, CHUNK_SECTORS))
        chunk[off:off + INODE_SIZE] = raw
        self.put_sectors(sector, bytes(chunk))

    @staticmethod
    def extents(raw):
        n = struct.unpack_from('>H', raw, 0x1e)[0]
        out = []
        for i in range(min(n, MAX_INLINE_EXTENTS)):
            o = 0x20 + i * 12
            out.append(struct.unpack_from('>III', raw, o))
        return out

    def next_serial(self):
        best = 0
        raw = self._bitmap('inode')
        for n in range(2, min(self.inode_count, 4096)):
            if self._bit(raw, n):
                serial = struct.unpack_from('>I', self.inode(n), 0x10)[0]
                best = max(best, serial)
        return best + 1

    # -- data blocks ----------------------------------------------------
    def block(self, n):
        return bytearray(self.sectors(self.base + self.data_off +
                                      n * CHUNK_SECTORS, CHUNK_SECTORS))

    def put_block(self, n, data):
        if len(data) != BLOCK_BYTES:
            raise Error('a block is %d bytes' % BLOCK_BYTES)
        self.put_sectors(self.base + self.data_off + n * CHUNK_SECTORS,
                         bytes(data))

    # -- directories ----------------------------------------------------
    @staticmethod
    def parse_dir(buf):
        out, o = [], 0
        while o + 8 <= len(buf):
            ino, rec, nlen, typ = struct.unpack_from('>IHBB', buf, o)
            if rec < 8 or o + rec > len(buf):
                break
            out.append((o, rec, ino, buf[o + 8:o + 8 + nlen].decode('latin-1'),
                        typ))
            o += rec
        return out

    @staticmethod
    def _reclen(name):
        return 8 + (len(name) + 3) // 4 * 4

    @staticmethod
    def physical(raw, logical):
        """-> the physical block holding `logical` in this inode, or None."""
        for first, count, phys in Ekfs.extents(raw):
            if first <= logical < first + count:
                return phys + (logical - first)
        return None

    def dir_entries(self, dir_inode_n):
        """-> [(location, inode, name_bytes, type)] in entry order, the way
        FUN_400cca5c walks them: every data block the size covers, skipping
        records whose inode is 0 (a freed slot)."""
        raw = self.inode(dir_inode_n)
        if raw[0] != TYPE_DIR:
            raise Error('inode %d is not a directory' % dir_inode_n)
        size = struct.unpack_from('>I', raw, 0x04)[0]
        out = []
        for lb in range(max(1, size // BLOCK_BYTES)):
            phys = self.physical(raw, lb)
            if phys is None:
                raise Error('inode %d: no block for logical %d' % (dir_inode_n,
                                                                   lb))
            for off, _rec, ino, name, typ in self.parse_dir(self.block(phys)):
                if ino:
                    out.append(((lb << LOG_BLOCK_SHIFT) | off, ino,
                                name.encode('latin-1'), typ))
        return out

    def rebuild_indexes(self, dir_inode_n):
        """Rewrite a directory's three index blocks from its entries."""
        raw = self.inode(dir_inode_n)
        blocks = [self.physical(raw, lg) for lg in INDEX_LOGICAL]
        if None in blocks:
            raise Error('inode %d has no index extent at logical 0x%x'
                        % (dir_inode_n, INDEX_LOGICAL[blocks.index(None)]))
        for phys, buf in zip(blocks, build_indexes(
                self.dir_entries(dir_inode_n))):
            self.put_block(phys, buf)
        return blocks

    def directories(self):
        """-> every directory inode on the card reachable from the root."""
        seen, todo = [], [ROOT_INODE]
        while todo:
            d = todo.pop(0)
            if d in seen or d >= RAM_INODE:
                continue
            seen.append(d)
            for _loc, ino, name, typ in self.dir_entries(d):
                if (typ == TYPE_DIR and name not in (b'.', b'..')
                        and ino < RAM_INODE):
                    todo.append(ino)
        return seen

    def repair(self):
        """Rebuild every directory's indexes and correct directory link
        counts. -> [(inode, entries, (old_links, new_links))]."""
        dirs = self.directories()
        links = {d: 0 for d in dirs}
        for d in dirs:
            for _loc, ino, _name, _typ in self.dir_entries(d):
                if ino in links:
                    links[ino] += 1
        report = []
        for d in dirs:
            self.rebuild_indexes(d)
            raw = self.inode(d)
            old = struct.unpack_from('>H', raw, 0x02)[0]
            if old != links[d]:
                struct.pack_into('>H', raw, 0x02, links[d])
                self.put_inode(d, raw)
            report.append((d, len(self.dir_entries(d)), (old, links[d])))
        return report

    def add_dir_entry(self, dir_inode_n, name, target, typ):
        """Insert one entry into a directory's first extent block, then
        rebuild its indexes -- without those the firmware never sees it."""
        raw = self.inode(dir_inode_n)
        ext = self.extents(raw)
        if not ext:
            raise Error('inode %d has no extents' % dir_inode_n)
        if len(self.dir_entries(dir_inode_n)) >= MAX_DIR_ENTRIES:
            raise Error('inode %d already holds the %d entries the firmware '
                        'allows' % (dir_inode_n, MAX_DIR_ENTRIES))
        blk = ext[0][2]
        buf = self.block(blk)
        entries = self.parse_dir(buf)
        if any(e[3] == name for e in entries):
            raise Error('%r already exists in inode %d' % (name, dir_inode_n))
        if not entries:
            raise Error('inode %d is not a directory' % dir_inode_n)
        last_off, last_rec, l_ino, l_name, l_typ = entries[-1]
        natural = self._reclen(l_name)
        need = self._reclen(name)
        if last_rec - natural < need:
            raise Error('no room in the directory block for %r' % name)
        struct.pack_into('>H', buf, last_off + 4, natural)
        o = last_off + natural
        struct.pack_into('>IHBB', buf, o, target, len(buf) - o, len(name), typ)
        buf[o + 8:o + 8 + len(name)] = name.encode('latin-1')
        pad = o + 8 + len(name)
        end = o + (len(buf) - o)
        buf[pad:end] = b'\x00' * (end - pad)
        self.put_block(blk, buf)
        # No link-count bump on the directory: FUN_400cd04c never touches
        # the directory's own +0x02, only the target's, and only when a
        # directory is being linked in. See the module docstring.
        self.rebuild_indexes(dir_inode_n)

    def set_file_hash(self, ino, data):
        """What FUN_400d1a2a does when the firmware finishes a file: hash
        the whole content, and store it | 1 both in inode +0x0c and in the
        on-card table the mount builds its hash -> inode index from."""
        h = ekfs_hash(bytes(data), SAMPLE_HASH_SEED) | 1
        blk = HASH_TABLE_BLOCK + (ino >> 12)
        table = self.block(blk)
        struct.pack_into('>I', table, (ino & 0xFFF) * 4, h)
        self.put_block(blk, table)
        raw = self.inode(ino)
        struct.pack_into('>I', raw, 0x0C, h)
        self.put_inode(ino, raw)
        return h

    # -- the point ------------------------------------------------------
    def add_file(self, parent, name, data, typ=TYPE_FILE):
        nblocks = (len(data) + BLOCK_BYTES - 1) // BLOCK_BYTES or 1
        ino = self.alloc_inode()
        runs = self.alloc_blocks(nblocks)
        if len(runs) > MAX_INLINE_EXTENTS:
            raise Error('needs %d extents; only %d fit inline'
                        % (len(runs), MAX_INLINE_EXTENTS))
        padded = bytes(data) + b'\x00' * (nblocks * BLOCK_BYTES - len(data))
        i = 0
        raw = bytearray(INODE_SIZE)
        raw[0x00] = typ
        raw[0x01] = 2
        struct.pack_into('>H', raw, 0x02, 1)
        struct.pack_into('>I', raw, 0x04, len(data))
        # The parent directory: FUN_400d1a2a notifies it through +0x08 when
        # a file is finished, and the RAM factory inodes carry it there too.
        struct.pack_into('>I', raw, 0x08, parent)
        struct.pack_into('>I', raw, 0x10, self.next_serial())
        struct.pack_into('>H', raw, 0x1e, len(runs))
        logical = 0
        for k, (first, run) in enumerate(runs):
            struct.pack_into('>III', raw, 0x20 + k * 12, logical, run, first)
            for j in range(run):
                self.put_block(first + j,
                               padded[i:i + BLOCK_BYTES])
                i += BLOCK_BYTES
            logical += run
        self.put_inode(ino, raw)
        self.set_file_hash(ino, data)
        self.add_dir_entry(parent, name, ino, typ)
        self.reseal()
        return ino

    def add_sample(self, parent, path_or_name, wav_bytes):
        """Convert a WAV to the +Drive's sample format and add it.
        -> (inode, card name, sample rate, frames)."""
        content, rate, frames = wav_to_sample(wav_bytes)
        name = sample_name(path_or_name)
        return self.add_file(parent, name, content), name, rate, frames

    def add_dir(self, parent, name):
        """Make an empty directory `name` in directory `parent`. -> its inode.

        Laid out as format_image lays out /incoming, the only directory
        below the root whose on-card form has been compared with the
        device's. Its entry in the parent and the parent's '..' back-link
        each count once, so the parent's link count goes up by one."""
        ino = self.alloc_inode()
        runs = self.alloc_blocks(4)
        if len(runs) != 1:
            raise Error('no run of 4 free blocks for a directory')
        first = runs[0][0]
        self.put_inode(ino, _dir_inode(parent, first, self.next_serial(), 2))
        _write_dir_blocks(self, first, [(ino, '.', TYPE_DIR),
                                        (parent, '..', TYPE_DIR)])
        self.add_dir_entry(parent, name, ino, TYPE_DIR)
        raw = self.inode(parent)
        links = struct.unpack_from('>H', raw, 0x02)[0]
        struct.pack_into('>H', raw, 0x02, links + 1)
        self.put_inode(parent, raw)
        self.reseal()
        return ino



# ------------------------------------------------------------------ format
# Straight out of FUN_400d0cb8, the firmware's FORMAT +DRIVE.
SB_DEFAULTS = [
    (0x04, 2),           # version
    (0x08, 0x4000),      # bitmap bytes
    (0x0c, 0x10000),     # inodes
    (0x10, 0xefe0),      # blocks
    (0x14, 0x10),        # inode bitmap, in sectors from the region base
    (0x18, 0x30),        # block bitmap
    (0x1c, 0x50),        # inode table
    (0x20, 0x4050),      # data
    (0x24, 0x10),
    (0x28, 0x40),
    (0x2c, 0x40),        # where the 0x40-block reservation starts
    (0x30, 0x50),        # where the 0x10-block one starts
]
# A copy of the superblock lives just past the end of the block bitmap.
SB_BACKUP_OFF = 0x1e00
# The format reserves the first 96 blocks before it allocates anything, which
# is why the firmware's own root lands on block 96. Reproduce that, so an
# image made here and one made by the device agree block for block.
RESERVED_BLOCKS = 96
DIR_EXTENT_LOGICAL_2 = 0x20000     # 0x80000000 >> 14
DIR_SIZE = 0x4000


def _dir_block(entries, size=BLOCK_BYTES):
    """Build a directory block. The last record runs to the end."""
    buf = bytearray(size)
    o = 0
    for k, (ino, name, typ) in enumerate(entries):
        last = k == len(entries) - 1
        rec = (size - o) if last else Ekfs._reclen(name)
        struct.pack_into('>IHBB', buf, o, ino, rec, len(name), typ)
        buf[o + 8:o + 8 + len(name)] = name.encode('latin-1')
        o += rec
    return buf


def _dir_inode(parent, first_block, serial, links):
    """A directory's inode: its entry block at `first_block`, the three
    index blocks right after it."""
    raw = bytearray(INODE_SIZE)
    raw[0x00] = TYPE_DIR
    raw[0x01] = 2
    # A link count: the directory's own '.', its entry in its parent (for
    # root, its own '..') and one per child directory's '..'. The device
    # records 3 for root and 2 for an empty /incoming. This used to be
    # computed as "entries on the card", which happens to give the same
    # two numbers and then diverges the moment a file is added.
    struct.pack_into('>H', raw, 0x02, links)
    struct.pack_into('>I', raw, 0x04, DIR_SIZE)
    struct.pack_into('>I', raw, 0x08, 3)
    struct.pack_into('>I', raw, 0x10, serial)
    struct.pack_into('>I', raw, 0x1c, parent)
    struct.pack_into('>H', raw, 0x1e, 2)
    struct.pack_into('>III', raw, 0x20, 0, 1, first_block)
    struct.pack_into('>III', raw, 0x2c, DIR_EXTENT_LOGICAL_2, 3,
                     first_block + 1)
    return raw


def _write_dir_blocks(fs, first_block, entries):
    """entries: [(inode, name, type)] -> the entry block and its indexes."""
    fs.put_block(first_block, _dir_block(entries))
    # The three indexes, which the firmware lists and searches through.
    # Locations follow _dir_block's layout: every record at its natural
    # length except the last, which runs to the end of the block.
    located, o = [], 0
    for e_ino, e_name, e_typ in entries:
        located.append((o, e_ino, e_name.encode('latin-1'), e_typ))
        o += Ekfs._reclen(e_name)
    for k, buf in enumerate(build_indexes(located)):
        fs.put_block(first_block + 1 + k, buf)


def format_image(path, base=REGION, with_incoming=True):
    """Lay down a fresh ekFS in the sample region of `path`.

    A file this has to create or lengthen is marked sparse first (Windows
    NTFS; see emu/sparse.py) and lengthened without writing zeros, so a new
    ~950 MB card occupies the ~2 MB the format writes rather than all of it.
    """
    need = (base + 0x4050 + 8 * CHUNK_SECTORS) * SECTOR
    with open(path, 'r+b' if os.path.exists(path) else 'w+b') as f:
        f.seek(0, 2)
        if f.tell() < need:
            sparse.make_sparse(f)
            sparse.extend(f, need)

    fs = object.__new__(Ekfs)
    fs.path, fs.base = path, base
    fs.f = open(path, 'r+b')
    fs.sb = bytearray(SECTOR)
    fs.sb[0:4] = b'ekFS'
    for off, val in SB_DEFAULTS:
        struct.pack_into('>I', fs.sb, off, val)
    for name, off in (('version', 0x04), ('bitmap_bytes', 0x08),
                      ('inode_count', 0x0c), ('block_count', 0x10),
                      ('inode_bitmap_off', 0x14), ('block_bitmap_off', 0x18),
                      ('inode_table_off', 0x1c), ('data_off', 0x20)):
        setattr(fs, name, struct.unpack_from('>I', fs.sb, off)[0])
    fs.stored_checksum = 0

    zero_bm = bytearray(fs.bitmap_bytes)
    fs._put_bitmap('inode', zero_bm)
    fs._put_bitmap('block', bytearray(fs.bitmap_bytes))
    blank = bytes(CHUNK_SECTORS * SECTOR)
    for chunk in range(8):
        fs.put_sectors(base + fs.inode_table_off + chunk * CHUNK_SECTORS,
                       blank)

    ib = fs._bitmap('inode')
    fs._set_bit(ib, 0)
    fs._set_bit(ib, 1)
    bb = fs._bitmap('block')
    # The reserved blocks are all zeros on a card the firmware formatted
    # (its FORMAT +DRIVE erases the region). They matter: blocks 64..79 are
    # the file-hash table the mount indexes, and stale words with bit 0 set
    # there would put phantom files in that index.
    zero_block = bytes(BLOCK_BYTES)
    for b in range(RESERVED_BLOCKS):
        fs._set_bit(bb, b)
        fs.put_block(b, zero_block)

    def make_dir(ino, parent, first_block, serial, entries, links):
        for b in range(first_block, first_block + 4):
            fs._set_bit(bb, b)
        fs._set_bit(ib, ino)
        fs.put_inode(ino, _dir_inode(parent, first_block, serial, links))
        _write_dir_blocks(fs, first_block, entries)

    root_entries = [(2, '.', TYPE_DIR), (2, '..', TYPE_DIR),
                    (RAM_INODE, 'factory', TYPE_DIR)]
    if with_incoming:
        root_entries.append((3, 'incoming', TYPE_DIR))
    make_dir(2, 2, RESERVED_BLOCKS, 2, root_entries,
             links=3 if with_incoming else 2)
    if with_incoming:
        make_dir(3, 2, RESERVED_BLOCKS + 4, 3,
                 [(3, '.', TYPE_DIR), (2, '..', TYPE_DIR)], links=2)

    fs._put_bitmap('inode', ib)
    fs._put_bitmap('block', bb)
    fs.reseal()
    fs.f.close()
    return fs


def is_formatted(path, base=REGION):
    """-> True if `path` holds an ekFS whose superblock checksum is right.

    The same test the firmware's mount applies (FUN_400d0f9c refuses a
    volume whose checksum does not match), so True means the device would
    mount it. format_image writes the superblock LAST, in reseal(), so a
    format that was interrupted part way reads as False here and not as a
    half-built filesystem.
    """
    try:
        with open(path, 'rb') as fh:
            fh.seek(base * SECTOR)
            sb = fh.read(SECTOR)
    except OSError:
        return False
    if len(sb) < SECTOR or sb[:4] != b'ekFS':
        return False
    return ekfs_hash(sb[:0x1fc], SEED) == struct.unpack_from('>I', sb, 0x1fc)[0]


def find_dir(fs, name):
    """-> the inode number of a named directory under the root."""
    for n in range(2, 64):
        raw = fs.inode(n)
        if raw[0] != TYPE_DIR:
            continue
        ext = fs.extents(raw)
        if not ext:
            continue
        for _o, _r, ino, nm, _t in fs.parse_dir(fs.block(ext[0][2])):
            if nm == name and ino != n:
                return ino
    return None
