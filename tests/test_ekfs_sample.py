"""A sample on the +Drive must be in the format the firmware writes, and must
carry the content hash the firmware's loader resolves it by.

Without the hash (inode +0x0c, bit 0 set) FUN_400d1680 treats the file as no
sample at all, the loader gets an invalid reference, loads nothing, and the
UI says "SAMPLE MEMORY FULL" -- the text its load-done callback shows for a
count of zero. Without the format the engine is handed RIFF bytes as a
header. Expected values are firmware-derived, not produced by the code under
test:

- FW_FILE_HASH: FUN_400d1a2a's hash, measured by running the firmware's own
  FUN_400d1544 / FUN_400d1200 under Unicorn, streamed in 16 KB chunks exactly
  as FUN_400d1a2a feeds them, over bytes((i*131 + 7) & 0xff for i in range(n)).
- The header layout is FUN_400eb666 (the firmware's sample-file writer) as
  its recorder caller FUN_400eb922 drives it: +0x04 PCM bytes, +0x08 48000,
  +0x0c 0, +0x10 0, +0x14 0x7f, zeros elsewhere; then PCM; then a 16-byte
  trailer copied from a BSS buffer nothing writes.
"""

import os
import struct
import tempfile
import unittest

FW_FILE_HASH = {0: 0x43FA243A, 1: 0x2EDC4716, 11: 0xA60F96B9, 12: 0x98B5CD9F,
                13: 0x3B7C486C, 16383: 0x384A80E3, 16384: 0x616A5837,
                16385: 0xBD4DED78, 32775: 0xFCBD3B56, 100003: 0xF338BEA3}


def _load():
    # The library, not the CLI: tools/ekfsadd.py is now a thin wrapper
    # over emu/ekfsformat.py, which the portable app imports directly.
    from emu import ekfsformat
    return ekfsformat


def _wav(frames, rate=48000, chans=1, bits=16, tag=1):
    """A RIFF/WAVE with `frames` = list of per-frame channel tuples."""
    width = bits // 8
    body = bytearray()
    for fr in frames:
        for v in fr:
            if tag == 3:
                body += struct.pack("<f", v)
            elif width == 1:
                body += bytes([v & 0xFF])
            elif width == 2:
                body += struct.pack("<h", v)
            elif width == 3:
                body += (v & 0xFFFFFF).to_bytes(3, "little")
            else:
                body += struct.pack("<i", v)
    fmt = struct.pack("<HHIIHH", tag, chans, rate, rate * chans * width,
                      chans * width, bits)
    riff = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    riff += b"data" + struct.pack("<I", len(body)) + bytes(body)
    return b"RIFF" + struct.pack("<I", len(riff)) + riff


class EkfsSampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ek = _load()

    def _fresh(self):
        fd, path = tempfile.mkstemp(suffix=".img")
        os.close(fd)
        self.addCleanup(os.remove, path)
        self.ek.format_image(path, base=0)
        fs = self.ek.Ekfs(path, base=0, write=True)
        self.addCleanup(fs.close)
        return fs

    def test_file_hash_matches_firmware(self):
        for n, want in FW_FILE_HASH.items():
            data = bytes((i * 131 + 7) & 0xFF for i in range(n))
            self.assertEqual(self.ek.ekfs_hash(data, self.ek.SAMPLE_HASH_SEED),
                             want, "length %d" % n)

    def test_header_pcm_and_trailer(self):
        content, rate, frames = self.ek.wav_to_sample(
            _wav([(0x1234,), (-2,), (0,)]))
        self.assertEqual((rate, frames), (48000, 3))
        head = content[:0x40]
        self.assertEqual(struct.unpack(">III", head[4:16]), (6, 48000, 0))
        self.assertEqual(struct.unpack(">I", head[16:20])[0], 0)
        self.assertEqual(head[0x14], 0x7F)
        self.assertEqual(head[:4] + head[0x15:], bytes(4 + 0x2B))
        # big-endian 16-bit, the ColdFire's own byte order
        self.assertEqual(content[0x40:0x46], b"\x12\x34\xff\xfe\x00\x00")
        self.assertEqual(content[0x46:], bytes(16))

    def test_stereo_24bit_is_averaged_to_mono_16bit(self):
        content, rate, frames = self.ek.wav_to_sample(
            _wav([(0x400000, 0x200000)], rate=44100, chans=2, bits=24))
        self.assertEqual((rate, frames), (44100, 1))
        # (0.5 + 0.25) / 2 = 0.375 of full scale
        self.assertEqual(struct.unpack(">h", content[0x40:0x42])[0],
                         round(0.375 * 32767))

    def test_float_and_8bit(self):
        c, _r, _f = self.ek.wav_to_sample(_wav([(1.0,), (-1.0,)], bits=32,
                                               tag=3))
        self.assertEqual(struct.unpack(">hh", c[0x40:0x44]), (32767, -32767))
        c, _r, _f = self.ek.wav_to_sample(_wav([(128,), (255,)], bits=8))
        self.assertEqual(struct.unpack(">h", c[0x40:0x42])[0], 0)

    def test_rejects_non_wav(self):
        with self.assertRaises(self.ek.Error):
            self.ek.wav_to_sample(b"not a wave file at all")

    def test_added_sample_resolves_like_a_firmware_file(self):
        fs = self._fresh()
        inc = self.ek.find_dir(fs, "incoming")
        ino, name, _r, _f = fs.add_sample(inc, "/x/Kick 01.WAV",
                                          _wav([(100,), (200,)]))
        self.assertEqual(name, "Kick 01")
        raw = bytes(fs.inode(ino))
        content, _r, _f = self.ek.wav_to_sample(_wav([(100,), (200,)]))
        h = self.ek.ekfs_hash(content, self.ek.SAMPLE_HASH_SEED) | 1
        self.assertEqual(struct.unpack(">I", raw[0x0C:0x10])[0], h)
        self.assertEqual(struct.unpack(">I", raw[0x08:0x0C])[0], inc)
        self.assertEqual(struct.unpack(">I", raw[0x04:0x08])[0], len(content))
        table = bytes(fs.block(self.ek.HASH_TABLE_BLOCK + (ino >> 12)))
        self.assertEqual(
            struct.unpack(">I", table[(ino & 0xFFF) * 4:(ino & 0xFFF) * 4 + 4])[0],
            h)
        names = [n for _l, _i, n, _t in fs.dir_entries(inc)]
        self.assertIn(b"Kick 01", names)

    def test_add_dir_matches_the_formatted_incoming(self):
        """A directory made by add_dir is laid out as format_image lays out
        /incoming, nests, takes samples, and counts links like the device:
        '.', its entry in its parent, and one per child directory."""
        fs = self._fresh()
        root, inc = 2, self.ek.find_dir(fs, "incoming")
        pack = fs.add_dir(root, "Concrete Ocean")
        cat = fs.add_dir(pack, "Percussion")
        ino, _n, _r, _f = fs.add_sample(cat, "Shaker 1.wav", _wav([(1,)]))

        def links(n):
            return struct.unpack(">H", bytes(fs.inode(n))[0x02:0x04])[0]
        self.assertEqual((links(root), links(pack), links(cat)), (4, 3, 2))

        def shape(n):
            # Everything but the serial and where the blocks landed.
            raw = bytearray(fs.inode(n))
            raw[0x10:0x14] = bytes(4)
            ext = self.ek.Ekfs.extents(raw)
            first = ext[0][2]
            self.assertEqual(ext[1][2], first + 1)
            raw[0x28:0x2C] = raw[0x34:0x38] = bytes(4)
            return bytes(raw)
        self.assertEqual(shape(pack), shape(inc)[:0x02] + b"\x00\x03"
                         + shape(inc)[0x04:])
        self.assertEqual(shape(cat), shape(inc))

        def entries(n):
            return [(i, name) for _l, i, name, _t in fs.dir_entries(n)]
        self.assertIn((pack, b"Concrete Ocean"), entries(root))
        self.assertEqual(entries(pack),
                         [(pack, b"."), (root, b".."), (cat, b"Percussion")])
        self.assertEqual(entries(cat),
                         [(cat, b"."), (pack, b".."), (ino, b"Shaker 1")])
        self.assertTrue(fs.checksum_ok())

    def test_format_clears_the_hash_table(self):
        """Stale words with bit 0 set there would be phantom files in the
        mount's hash index; the firmware's format leaves blocks 0..95 zero."""
        fd, path = tempfile.mkstemp(suffix=".img")
        os.close(fd)
        self.addCleanup(os.remove, path)
        self.ek.format_image(path, base=0)
        fs = self.ek.Ekfs(path, base=0, write=True)
        fs.put_block(self.ek.HASH_TABLE_BLOCK, b"\xff" * self.ek.BLOCK_BYTES)
        fs.close()
        self.ek.format_image(path, base=0)
        fs = self.ek.Ekfs(path, base=0)
        self.addCleanup(fs.close)
        self.assertFalse(any(fs.block(self.ek.HASH_TABLE_BLOCK)))


if __name__ == "__main__":
    unittest.main()
