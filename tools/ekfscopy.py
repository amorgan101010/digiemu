#!/usr/bin/env python3
"""Copy one +Drive's samples onto another, folder for folder.

    python tools/ekfscopy.py FROM TO [--rate 48000] [--dry] [--base N]

FROM and TO are raw card images with an ekFS volume (emu/ekfsformat.py).
Every directory and sample below FROM's root is made under TO's root, except
what TO already has by that name. With --rate, a sample stored at another
rate is resampled to it: the Digitakt plays a sample at whatever rate its
header gives, but a firmware that asks for 48 kHz turns the others away, and
emu/samples.py stores a WAV at its own rate. Resampling needs numpy and
scipy, which nothing else here does:

    uv run --with numpy --with scipy python tools/ekfscopy.py FROM TO --rate 48000

Write to TO only while no emulator has it open, and rebuild its snapshots
afterwards: the firmware indexes the card at its cold boot.
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emu import ekfsformat as ek                  # noqa: E402

RAM_ONLY = 0x1000000        # inodes from here up exist only in the firmware


def file_bytes(fs, ino):
    """-> the whole of file `ino`."""
    raw = fs.inode(ino)
    size = struct.unpack_from('>I', raw, 0x04)[0]
    out = bytearray()
    for lb in range((size + ek.BLOCK_BYTES - 1) // ek.BLOCK_BYTES):
        phys = ek.Ekfs.physical(raw, lb)
        if phys is None:
            raise ek.Error('inode %d: no block for logical %d' % (ino, lb))
        out += fs.block(phys)
    return bytes(out[:size])


def resampled(data, rate):
    """-> sample file `data` at `rate`, or `data` if it is already."""
    pcm_len, was = struct.unpack_from('>II', data, 0x04)
    was = was or 48000
    if was == rate or pcm_len < 4:
        return data
    try:
        import numpy as np
        from scipy.signal import resample_poly
    except ImportError as exc:
        raise SystemExit('--rate needs numpy and scipy (%s): run with '
                         'uv run --with numpy --with scipy' % exc)
    from math import gcd
    pcm = np.frombuffer(data, dtype='>i2', count=pcm_len // 2,
                        offset=ek.SAMPLE_HEADER).astype(np.float64)
    g = gcd(rate, was)
    out = resample_poly(pcm, rate // g, was // g)
    out = np.clip(np.rint(out), -32768, 32767).astype('>i2').tobytes()
    head = bytearray(data[:ek.SAMPLE_HEADER])
    struct.pack_into('>II', head, 0x04, len(out), rate)
    return bytes(head) + out + bytes(ek.SAMPLE_TRAILER)


def copy_tree(src, dst, sdir, ddir, path, rate, dry, tally):
    have = {name.decode('latin-1').lower(): (ino, typ)
            for _loc, ino, name, typ in (dst.dir_entries(ddir) if ddir else [])}
    for _loc, ino, name, typ in src.dir_entries(sdir):
        name = name.decode('latin-1')
        if name in ('.', '..') or ino >= RAM_ONLY:
            continue
        here = path + '/' + name
        if typ == ek.TYPE_DIR:
            if name.lower() in have:
                target = have[name.lower()][0]
            elif dry:
                target = None
            else:
                target = dst.add_dir(ddir, name)
            tally['dirs'] += 1
            copy_tree(src, dst, ino, target, here, rate, dry, tally)
            continue
        if name.lower() in have:
            tally['kept'] += 1
            continue
        data = file_bytes(src, ino)
        if rate and len(data) >= ek.SAMPLE_HEADER:
            new = resampled(data, rate)
            tally['resampled'] += new is not data
            data = new
        tally['files'] += 1
        tally['bytes'] += len(data)
        if not dry:
            dst.add_file(ddir, name, data)
        if tally['files'] % 100 == 0:
            print('  %d files, %d MB: %s' % (tally['files'],
                                             tally['bytes'] >> 20, here),
                  flush=True)


def copy(src_path, dst_path, rate=0, dry=False, base=ek.REGION):
    """Copy the tree. -> counts: dirs, files, kept (already there),
    resampled, bytes."""
    src = ek.Ekfs(src_path, base)
    dst = ek.Ekfs(dst_path, base, write=not dry)
    tally = dict(dirs=0, files=0, kept=0, resampled=0, bytes=0)
    try:
        copy_tree(src, dst, ek.ROOT_INODE, ek.ROOT_INODE, '', rate, dry, tally)
    finally:
        dst.close()
        src.close()
    return tally


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--rate', type=int, default=0,
                    help='resample samples stored at another rate to this')
    ap.add_argument('--dry', action='store_true',
                    help='read and convert, write nothing')
    ap.add_argument('--base', type=lambda s: int(s, 0), default=ek.REGION,
                    help='sector the volume starts at, in both images')
    args = ap.parse_args()
    tally = copy(args.src, args.dst, args.rate, args.dry, args.base)
    print('%(dirs)d folders, %(files)d files copied (%(resampled)d resampled), '
          '%(kept)d already there' % tally)
    print('%d MB of samples' % (tally['bytes'] >> 20))


if __name__ == '__main__':
    main()
