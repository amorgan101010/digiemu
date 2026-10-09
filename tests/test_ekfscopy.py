"""tools/ekfscopy.py: one card's sample tree made on another."""
import importlib.util
import os
import struct
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import ekfscopy                                   # noqa: E402
from emu import ekfsformat as ek                  # noqa: E402
from test_ekfs_sample import _wav                 # noqa: E402

HAVE_SCIPY = all(importlib.util.find_spec(m) for m in ("numpy", "scipy"))


def tree(fs, at=ek.ROOT_INODE, path=""):
    """-> {path: file bytes, or None for a directory} below `at`."""
    out = {}
    for _loc, ino, name, typ in fs.dir_entries(at):
        name = name.decode("latin-1")
        if name in (".", "..") or ino >= ekfscopy.RAM_ONLY:
            continue
        here = path + "/" + name
        if typ == ek.TYPE_DIR:
            out[here] = None
            out.update(tree(fs, ino, here))
        else:
            out[here] = ekfscopy.file_bytes(fs, ino)
    return out


class EkfsCopyTest(unittest.TestCase):
    def card(self):
        fd, path = tempfile.mkstemp(suffix=".img")
        os.close(fd)
        self.addCleanup(os.remove, path)
        ek.format_image(path, base=0)
        return path

    def source(self):
        """A card with /Pack/Kick (48 kHz), /Pack/Deep/Snare (44.1 kHz) and
        /incoming/Hat."""
        path = self.card()
        fs = ek.Ekfs(path, base=0, write=True)
        pack = fs.add_dir(ek.ROOT_INODE, "Pack")
        deep = fs.add_dir(pack, "Deep")
        ramp = [(i * 50 - 5000,) for i in range(200)]
        fs.add_sample(pack, "Kick.wav", _wav(ramp))
        fs.add_sample(deep, "Snare.wav", _wav(ramp + ramp, rate=44100))
        fs.add_sample(ek.find_dir(fs, "incoming"), "Hat.wav", _wav(ramp[:40]))
        fs.close()
        return path

    def read(self, path):
        fs = ek.Ekfs(path, base=0)
        try:
            return tree(fs)
        finally:
            fs.close()

    def test_the_tree_arrives_as_it_was(self):
        src, dst = self.source(), self.card()
        tally = ekfscopy.copy(src, dst, base=0)
        self.assertEqual((tally["dirs"], tally["files"], tally["kept"]), (3, 3, 0))
        self.assertEqual(self.read(dst), self.read(src))
        fs = ek.Ekfs(dst, base=0)
        self.addCleanup(fs.close)
        self.assertTrue(fs.checksum_ok())

    def test_a_second_copy_adds_nothing(self):
        src, dst = self.source(), self.card()
        ekfscopy.copy(src, dst, base=0)
        before = self.read(dst)
        tally = ekfscopy.copy(src, dst, base=0)
        self.assertEqual((tally["files"], tally["kept"]), (0, 3))
        self.assertEqual(self.read(dst), before)

    def test_a_dry_run_writes_nothing(self):
        src, dst = self.source(), self.card()
        before = self.read(dst)
        tally = ekfscopy.copy(src, dst, dry=True, base=0)
        self.assertEqual(tally["files"], 3)
        self.assertEqual(self.read(dst), before)

    @unittest.skipUnless(HAVE_SCIPY, "resampling needs numpy and scipy")
    def test_another_rate_is_resampled(self):
        src, dst = self.source(), self.card()
        tally = ekfscopy.copy(src, dst, rate=48000, base=0)
        self.assertEqual(tally["resampled"], 1)
        got, was = self.read(dst), self.read(src)
        self.assertEqual(got["/Pack/Kick"], was["/Pack/Kick"])
        snare = got["/Pack/Deep/Snare"]
        pcm, rate = struct.unpack_from(">II", snare, 0x04)
        self.assertEqual(rate, 48000)
        # 400 frames at 44.1 kHz are 436 at 48 kHz (435.4, rounded up).
        self.assertEqual(pcm, 2 * 436)
        self.assertEqual(len(snare), ek.SAMPLE_HEADER + pcm + ek.SAMPLE_TRAILER)


if __name__ == "__main__":
    unittest.main()
