#!/usr/bin/env python3
"""Watch a running step_iv_multi.py scan for sudden jumps and make it ramp down

    python3 iv_guard.py iv-20261102/iv100.log --ref iv-20261102/iv_l23

Reads V, I and the set point of each channel every --poll s and sends SIGTERM to
step_iv_multi.py (which then ramps all channels to 0 together) on:
  flat jump   at a fixed voltage, I above the median of the last ~30 s by more
              than max(--jump, 30%), twice in a row
  ramp spike  I above the last settled current + max(--ramp-excess, 60%), twice
  V dip       measured V falls by more than --vdip between polls while the set
              point is not below it (a discharge)
  knee        settled I more than doubles from the previous step (above 40 V),
              or more than 2x + 0.1 uA the --ref scan at the same voltage
  not stable  a step above 40 V that never settles
Thresholds from the 2026-11-02 morning scan: flat excess <= 94 nA, ramps <= 0.3 uA
above the previous step, V drop <= 0.06 V per poll.
"""

import argparse
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time

IP = os.environ.get('MPOD_CRATE_IP', '192.168.10.50')
SNMP = os.environ.get('NET_SNMP_INSTALL', '/u1/ldmx/net-snmp-5.9.4/install') + '/bin/snmpget'

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument('log', help='step_iv_multi.py output log')
p.add_argument('--channels', nargs='+', default=['u302', 'u304'])
p.add_argument('--ref', default=None, help='JSON prefix of an earlier scan, <prefix>_<ch>.json')
p.add_argument('--poll', type=float, default=1.0)
p.add_argument('--jump', type=float, default=0.3e-6, help='flat jump threshold in A')
p.add_argument('--ramp-excess', type=float, default=1.0e-6, help='ramp threshold above settled I in A')
p.add_argument('--first-ceiling', type=float, default=2.0e-6, help='ramp ceiling before the first settled step')
p.add_argument('--vdip', type=float, default=0.3, help='V drop per poll in V')
p.add_argument('--knee-from', type=float, default=40.0, help='step-to-step knee check above this V')
p.add_argument('--dry-run', action='store_true', help='report, never signal')
p.add_argument('--pid', type=int, default=None, help='step_iv_multi.py PID to guard (default: the first one)')
args = p.parse_args()
chs = args.channels


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def read(ch):
    out = subprocess.run([SNMP, '-v', '2c', '-m', '+WIENER-CRATE-MIB', '-c', 'public', '-Oqv', '-Op', '+.12', IP,
                          f'outputMeasurementTerminalVoltage.{ch}', f'outputMeasurementCurrent.{ch}',
                          f'outputVoltage.{ch}'], capture_output=True, text=True, timeout=20).stdout.split('\n')
    return [float(x.split()[0]) for x in out[:3]]


def scan_pid():
    # one given scan when several run
    if args.pid:
        return args.pid if os.path.exists(f'/proc/{args.pid}') else None
    out = subprocess.run(['pgrep', '-f', '^python3 step_iv_multi.py'], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


ref = {}
if args.ref:
    for ch in chs:
        ref[ch] = {s['v_set']: s['i_mean'] for s in json.load(open(f'{args.ref}_{ch}.json'))['steps']}

pid = scan_pid()
if pid is None:
    sys.exit('no step_iv_multi.py running')
log(f'guarding pid {pid}, channels {chs}, ref {args.ref}')

settled = {ch: [] for ch in chs}  # (v_set, I)
buf = {ch: [] for ch in chs}      # recent (V, I)
strikes = {ch: {'flat': 0, 'ramp': 0} for ch in chs}
logpos = 0
step_v = None
done = False


def abort(why):
    global done
    log(f'ABORT: {why}')
    if not args.dry_run:
        os.kill(pid, signal.SIGTERM)
        log(f'sent SIGTERM to {pid}, the scan ramps all channels down')
    done = True


def parse_log():
    global logpos, step_v
    with open(args.log) as f:
        f.seek(logpos)
        lines = f.readlines()
        logpos = f.tell()
    for ln in lines:
        m = re.search(r'--- ([\d.]+) V', ln)
        if m:
            step_v = float(m.group(1))
        if 'not stable after' in ln and step_v is not None and step_v > args.knee_from:
            abort(f'{step_v} V step did not settle')
        m = re.search(r'RESULT (\S+) ([\d.]+) V: V=\S+ V  I=(\S+)', ln)
        if m and m.group(1) in chs:
            ch, v, i = m.group(1), float(m.group(2)), float(m.group(3))
            if settled[ch]:
                v0, i0 = settled[ch][-1]
                if v > args.knee_from and i > 2 * i0 and i - i0 > 0.3e-6:
                    abort(f'{ch} knee: {i:.3e} A at {v} V vs {i0:.3e} A at {v0} V')
            if v in ref.get(ch, {}) and i > 2 * ref[ch][v] + 0.1e-6:
                abort(f'{ch} at {v} V: {i:.3e} A vs {ref[ch][v]:.3e} A in the reference scan')
            settled[ch].append((v, i))
            log(f'settled {ch} {v} V {i:.3e} A')
        if 'ramping all to 0 V' in ln or 'switched off' in ln:
            log('scan is ramping down or done')
            return True
    return False


while not done:
    if scan_pid() != pid:
        log('scan process gone')
        break
    try:
        if parse_log():
            break
    except Exception as e:
        log(f'log read failed: {e}')
    if done:
        break
    for ch in chs:
        try:
            v, i, vset = read(ch)
        except Exception as e:
            log(f'{ch} read failed: {e}')
            continue
        b = buf[ch]
        # V dip: discharge, not a commanded ramp-down
        if b and b[-1][0] - v > args.vdip and vset >= b[-1][0] - 0.5:
            abort(f'{ch} V dropped {b[-1][0]:.2f} -> {v:.2f} V (set {vset:.2f})')
            break
        b.append((v, i))
        del b[:-16]
        # flat jump: V steady over the buffer
        if len(b) >= 12 and max(x[0] for x in b) - min(x[0] for x in b) < 0.05:
            med = statistics.median(x[1] for x in b[:-1])
            hit = i - med > max(args.jump, 0.3 * abs(med))
            strikes[ch]['flat'] = strikes[ch]['flat'] + 1 if hit else 0
            if strikes[ch]['flat'] >= 2:
                abort(f'{ch} jump at {v:.2f} V: {i:.3e} A vs median {med:.3e} A')
                break
        else:
            strikes[ch]['flat'] = 0
        # ramp ceiling from the last settled current
        if settled[ch]:
            i0 = settled[ch][-1][1]
            ceil = i0 + max(args.ramp_excess, 0.6 * i0)
        elif ref.get(ch):
            # before the first step: reference current at the next step up
            up = [s for s in sorted(ref[ch]) if s >= v - 0.5]
            i0 = ref[ch][up[0] if up else max(ref[ch])]
            ceil = i0 + max(args.ramp_excess, 0.6 * i0)
        else:
            ceil = args.first_ceiling
        strikes[ch]['ramp'] = strikes[ch]['ramp'] + 1 if i > ceil else 0
        if strikes[ch]['ramp'] >= 2:
            abort(f'{ch} {i:.3e} A at {v:.2f} V above {ceil:.3e} A')
            break
    time.sleep(args.poll)
log('guard exits')
