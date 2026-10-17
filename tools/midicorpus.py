#!/usr/bin/env python3
"""Run a bounded, byte-level mk1 MIDI DIN corpus against a settled snapshot.

The report is deliberately an emulator-path observation, not a physical-port
claim.  It records the host payload, channel-36 ring deliveries, vector-156
entries and the firmware-driven UART9 OUT model.  ``--candidate-transparent-
out-model`` additionally attaches an explicit raw MIDI OUT/SYNC A model at
the receive-to-parser boundary.  That is an exact-byte model of the proposed
route, not evidence that stock firmware or a physical unit implements it.

Example (use paths belonging to a private firmware setup)::

    uv run python tools/midicorpus.py --syx firmware/Digitakt_OS1.53.syx \
      --snapshot snapshots/Digitakt_OS1.53/gui.snap --out out/midi/corpus.json \
      --limit 120000000
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unicorn import UC_HOOK_CODE

from emu import config, longrun, midi, symbols
from emu.dtim import Dtims, Timers
from emu.harness import VBR
from emu.pit import Pits


def channel(status, ch, *data):
    return bytes([status | ch, *data])


def corpus():
    """Named streams covering every MIDI 1.0 class this investigation needs."""
    notes = bytearray()
    poly = bytearray()
    controls = bytearray()
    programs = bytearray()
    pressure = bytearray()
    bend = bytearray()
    for ch in range(16):
        notes += channel(0x90, ch, 0, 1) + channel(0x80, ch, 127, 0)
        notes += channel(0x90, ch, 64, 0)          # velocity-zero Note Off
        poly += channel(0xA0, ch, 0, 0) + channel(0xA0, ch, 127, 127)
        programs += channel(0xC0, ch, 0) + channel(0xC0, ch, 127)
        pressure += channel(0xD0, ch, 0) + channel(0xD0, ch, 127)
        # MIDI's 14-bit bend values: 0, centre and maximum.
        bend += (channel(0xE0, ch, 0, 0) + channel(0xE0, ch, 0, 64)
                 + channel(0xE0, ch, 127, 127))
        for cc in range(128):
            controls += channel(0xB0, ch, cc, (cc * 17 + ch) & 0x7F)

    # CC 2 is breath controller; retain it as an independently named case.
    breath = b''.join(channel(0xB0, ch, 2, v)
                      for ch in range(16) for v in (0, 1, 64, 127))
    sys_common = bytes([0xF1, 0x00, 0xF2, 0x00, 0x00, 0xF3, 0x7F, 0xF6])
    sysex = (b'\xF0\x7D\x00\xF7', b'\xF0\x7D' + bytes(range(16)) + b'\xF7',
             b'\xF0\x7D' + bytes(i & 0x7F for i in range(256)) + b'\xF7')
    realtime = bytes([0xF8, 0xFA, 0xFB, 0xFC, 0xFE])
    interleaved = bytes([0x90, 60, 0xF8, 100, 0xB0, 2, 0xFE, 127,
                         0xF0, 0x7D, 1, 0xFA, 2, 0xF7, 0x80, 60, 0, 0xFC])
    running = bytes([0x90, 60, 1, 61, 2, 62, 0, 0xB0, 2, 0, 2, 127,
                     0xE0, 0, 0, 0, 64, 127, 127])
    rapid = b''.join(channel(0x90, ch, n, 127) + channel(0x80, ch, n, 0)
                     for n in range(128) for ch in range(16))
    # One second of 31,250-baud serial payload, in complete CC messages.
    wire = (b'\xB0\x02\x00' * (31_250 // 3)) + b'\xB0'
    return [
        ('notes_all_channels', bytes(notes)),
        ('poly_aftertouch_all_channels', bytes(poly)),
        ('cc_0_127_all_channels', bytes(controls)),
        ('breath_cc2_all_channels', breath),
        ('program_change_all_channels', bytes(programs)),
        ('channel_aftertouch_all_channels', bytes(pressure)),
        ('pitch_bend_full_range_all_channels', bytes(bend)),
        ('system_common', sys_common),
        ('sysex_4_bytes', sysex[0]),
        ('sysex_20_bytes', sysex[1]),
        ('sysex_260_bytes', sysex[2]),
        ('realtime_clock_transport_active_sense', realtime),
        ('realtime_interleaved', interleaved),
        ('running_status', running),
        ('rapid_back_to_back', rapid),
        ('wire_rate_1_second', wire),
    ]


def u32(machine, address):
    return struct.unpack('>I', bytes(machine.uc.mem_read(address, 4)))[0]


def run_case(args, name, incoming):
    """Run one independent stream, bounded by args.limit instructions."""
    with open(config.main_image(), 'rb') as image:
        profile = symbols.resolve(image.read())
    unblock_except = ((profile.frame_sem,) if profile.frame_sem is not None
                      else ())
    m, ev, _st, pc, _inq, at = longrun.build(
        args.snapshot, syx=args.syx, unblock=True, softfloat=True, bitmap=True,
        dsp=True, unblock_except=unblock_except,
        deferred_components=('timers',))
    restore = ev.get('restore_checkpoint_timers')
    pits = restore() if restore is not None else Timers(
        Pits(m, hold=False), Dtims(m, channels=(3,), hold=False))
    ips = pits.sources[0].ips
    ring = []
    out = []
    candidate_out = []
    firmware_receive_entries = [0]
    vector_entries = [0]
    handler = u32(m, VBR + midi.RX_VECTOR * 4)
    m.uc.hook_add(UC_HOOK_CODE,
                  lambda _uc, _a, _s, _d: vector_entries.__setitem__(
                      0, vector_entries[0] + 1), begin=handler, end=handler)
    firmware_receive = u32(m, 0x4060E80C)
    m.uc.hook_add(UC_HOOK_CODE,
                  lambda _uc, _a, _s, _d: firmware_receive_entries.__setitem__(
                      0, firmware_receive_entries[0] + 1),
                  begin=firmware_receive, end=firmware_receive)
    original_feed = midi.feed

    def traced_feed(machine, data):
        before = u32(machine, midi.TCD36 + midi.TCD_DADDR)
        original_feed(machine, data)
        ring.append({'daddr_before': '0x%08x' % before, 'bytes': bytes(data)})

    midi.feed = traced_feed
    mi = midi.MidiIn()
    mo = midi.MidiOut(out.append)
    candidate = midi.MidiOut(candidate_out.append)
    try:
        mo.install(m)
        mi.attach(m, ips)
        mo.attach(m, ips)
        if args.candidate_transparent_out_model:
            # This is intentionally outside the stock image.  It represents
            # the proposed shared receive hook: after eDMA writes the bytes,
            # before vector 156 lets the stock parser handle them.
            mi.add_pre_parser_tap(candidate.route_raw)
        # This is after HostMidi's byte-preserving callback.  HostMidi's
        # active-sensing regression test guards the OS-facing ingress setup.
        mi.put(incoming)
        done = 0
        # Input is paced by MidiIn at DIN byte time; add 50 ms for its margin
        # and for the firmware to drain the last vector.
        need = int((0.050 + len(incoming) * midi.MidiIn.BYTE_S) * ips)
        while done < min(args.limit, need) and not getattr(m, 'halt_vec', None):
            step = min(1_000_000, args.limit - done)
            pc, used, why = longrun.spin(m, pc, step, pits=pits, fast=True,
                                          async_events=(mi, mo))
            done += used
            if why != 'limit':
                return {'name': name, 'incoming': incoming, 'ring': ring,
                        'firmware_receive_entries': firmware_receive_entries[0],
                        'uart9_out': b''.join(out),
                        'candidate_out': b''.join(candidate_out),
                        'stop': why,
                        'instructions': done, 'vector_entries': vector_entries[0]}
        return {'name': name, 'incoming': incoming, 'ring': ring,
                'firmware_receive_entries': firmware_receive_entries[0],
                'uart9_out': b''.join(out),
                'candidate_out': b''.join(candidate_out),
                'stop': 'limit' if done >= args.limit
                else 'settled', 'instructions': done,
                'vector_entries': vector_entries[0], 'mi': mi, 'mo': mo}
    finally:
        midi.feed = original_feed
        card = ev.get('esdhc', None)
        if card is not None and getattr(card, 'card', None) is not None:
            card.card.close()


def summarise(result, candidate_transparent_out_model=False):
    incoming = result.pop('incoming')
    ring = b''.join(item['bytes'] for item in result.pop('ring'))
    uart9_out = result.pop('uart9_out')
    candidate_out = result.pop('candidate_out')
    mi = result.pop('mi', None)
    mo = result.pop('mo', None)
    return {
        **result,
        'input_bytes': len(incoming),
        'input_sha256': hashlib.sha256(incoming).hexdigest(),
        'ring_bytes': len(ring),
        'ring_matches_input': ring == incoming,
        'firmware_receive_entries': result.pop('firmware_receive_entries'),
        'firmware_rx_vectors': None if mi is None else mi.raised,
        'uart9_out_bytes': len(uart9_out),
        'uart9_out_hex': uart9_out.hex(),
        'uart9_dma_runs': None if mo is None else mo.dma_runs,
        'candidate_out_model_bytes': len(candidate_out),
        'candidate_out_model_matches_input': candidate_out == incoming,
        'candidate_out_model': ('candidate pre-parser MIDI OUT route; not a '
                                'stock/physical/timing claim'
                                if candidate_transparent_out_model else
                                'no candidate route installed'),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--syx', required=True)
    ap.add_argument('--snapshot', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--limit', type=lambda s: int(s, 0), default=120_000_000,
                    help='maximum guest instructions per independent case')
    ap.add_argument('--candidate-transparent-out-model', action='store_true',
                    help='model the proposed raw pre-parser MIDI OUT route; this is '
                         'not a firmware patch, timing model, or hardware result')
    args = ap.parse_args()
    if args.limit <= 0:
        ap.error('--limit must be positive')
    with open(args.syx, 'rb') as source:
        syx_sha256 = hashlib.file_digest(source, 'sha256').hexdigest()
    report = {'syx': str(Path(args.syx).resolve()), 'syx_sha256': syx_sha256,
              'snapshot': args.snapshot, 'limit_per_case': args.limit,
              'cases': []}
    for name, stream in corpus():
        result = run_case(args, name, stream)
        report['cases'].append(summarise(
            result, args.candidate_transparent_out_model))
        print('%-38s %6d -> ring=%s out=%d stop=%s' % (
            name, len(stream), report['cases'][-1]['ring_matches_input'],
            report['cases'][-1]['uart9_out_bytes'], report['cases'][-1]['stop']),
            flush=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
