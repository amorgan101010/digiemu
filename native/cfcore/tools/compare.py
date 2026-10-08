"""Join cfbench and uctime JSON lines and print the comparison."""
import json
import sys


def rows(path):
    out = {}
    for line in open(path):
        line = line.strip()
        if line.startswith('{'):
            r = json.loads(line)
            out[r['name']] = r
    return out


rust, uc = rows(sys.argv[1]), rows(sys.argv[2])
held = set(sys.argv[3].split(',')) if len(sys.argv) > 3 else set()
tot = {'insns': 0, 'uc': 0, 'aot': 0, 'int': 0, 'miss': 0}
part = {'seen': dict(tot), 'held-out': dict(tot)}
bad = []
print('%-8s %7s %9s %9s %9s %6s %6s %s' % ('name', 'insns', 'uc us', 'aot us',
                                          'interp us', 'uc/aot', 'miss%', 'ok'))
for name in sorted(rust):
    r, u = rust[name], uc[name]
    ucns = u['uc_ns_med'] - u['uc_call_ns']
    ok = r['interp_ok'] and r['aot_ok'] and u['uc_ok']
    if not ok:
        bad.append(name)
    print('%-8s %7d %9.1f %9.1f %9.1f %6.2f %6.2f %s'
          % (name, r['insns'], ucns / 1e3, r['aot_ns_med'] / 1e3,
             r['interp_ns_med'] / 1e3, ucns / r['aot_ns_med'],
             100.0 * r['interp_insns_in_mixed'] / r['insns'], 'ok' if ok else 'BAD'))
    for t in (tot, part['held-out' if name in held else 'seen']):
        t['insns'] += r['insns']
        t['uc'] += ucns
        t['aot'] += r['aot_ns_med']
        t['int'] += r['interp_ns_med']
        t['miss'] += r['interp_insns_in_mixed']
for label, t in [('all', tot)] + sorted(part.items()):
    if not t['insns']:
        continue
    print('%-9s %d insns: unicorn %.1f MIPS, translated %.1f MIPS (%.2fx), '
          'interpreter %.1f MIPS (%.2fx), not translated %.2f%%'
          % (label, t['insns'], 1e3 * t['insns'] / t['uc'],
             1e3 * t['insns'] / t['aot'], t['uc'] / t['aot'],
             1e3 * t['insns'] / t['int'], t['uc'] / t['int'],
             100.0 * t['miss'] / t['insns']))
print('mismatches:', bad or 'none')
