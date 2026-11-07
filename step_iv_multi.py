#!/usr/bin/env python3
"""Step-and-settle IV on several MPOD HV channels in lockstep, for coupled HV lines

    python3 step_iv_multi.py u302 u304 --ilimit 6e-6 3e-6 --voltages 5 10 --vmax 40 --dry-run

All channels switch on at 0 V, then move together in increments of --dv, each
increment waiting until every channel has arrived, so the lines never differ by
more than about --dv. At each step all channels must be stable at the same time,
then --samples readings are taken and --step-cmd runs (e.g. a pedestal) while
the currents are still watched. Any trip, flag, over-current or a voltage
difference above --max-dv ramps all channels to 0 together and switches off.
Writes one JSON per channel in the step_iv.py format (plot_step_iv.py works).
"""

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time

IP = os.environ.get('MPOD_CRATE_IP', '192.168.10.50')
SNMP = os.environ.get('NET_SNMP_INSTALL', '/u1/ldmx/net-snmp-5.9.4/install') + '/bin/snmp'
FAIL_WORDS = ('Failure', 'Kill', 'EmergencyOff', 'Trip', 'BoundsExceeded', 'Inhibit')

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument('channels', nargs='+', help='MPOD channels, e.g. u302 u304')
p.add_argument('--voltages', type=float, nargs='+', default=[1, 2, 3], help='set points in V, in order')
p.add_argument('--ilimit', type=float, nargs='+', default=[10e-6], help='current limit in A, one value or one per channel')
p.add_argument('--rate', type=float, default=0.1, help='rise and fall rate in V/s (default 0.1)')
p.add_argument('--dv', type=float, default=1.0, help='lockstep increment in V (default 1)')
p.add_argument('--max-dv', type=float, default=3.0, help='abort if two channels differ by more than this in V')
p.add_argument('--poll', type=float, default=2.0, help='seconds between readings')
p.add_argument('--window', type=float, default=60.0, help='stability window in s')
p.add_argument('--abs-tol', type=float, default=0.2e-9, help='absolute current drift tolerance in A')
p.add_argument('--rel-tol', type=float, default=0.02, help='relative current drift tolerance')
p.add_argument('--timeout', type=float, default=900.0, help='max seconds to wait for stability after reaching a step')
p.add_argument('--v-tol', type=float, default=0.25, help='max |V_meas - V_set| in V')
p.add_argument('--v-flat', type=float, default=0.01, help='max terminal voltage spread over the window in V')
p.add_argument('--samples', type=int, default=5, help='readings recorded once stable (at least 2)')
p.add_argument('--step-cmd', default=None, help='shell command run at each step after the samples, {v} = set point')
p.add_argument('--output', default=None, help='JSON prefix (default iv_<channels>_<time>), writes <prefix>_<ch>.json')
p.add_argument('--vmax', type=float, default=10.0, help='refuse set points above this (default 10 V, hard cap 200 V)')
p.add_argument('--hold', type=float, default=0.0, help='keep the last voltage this many seconds (still watched) before ramping down')
p.add_argument('--adopt', action='store_true',
               help='take over channels already on at the last set point and go straight to the hold')
p.add_argument('--dry-run', action='store_true', help='only read, print what would be set')
args = p.parse_args()
chs = args.channels
if len(args.ilimit) == 1:
    args.ilimit = args.ilimit * len(chs)
if len(args.ilimit) != len(chs):
    sys.exit('--ilimit needs one value or one per channel')
ilim = dict(zip(chs, args.ilimit))


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def run(kind, *a, community='public', tries=3):
    cmd = [SNMP + kind, '-v', '2c', '-m', '+WIENER-CRATE-MIB', '-c', community] + list(a)
    # a single SNMP timeout must not end a hold (07-Nov 01:31)
    for n in range(tries):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired as e:
            err = e
        else:
            if r.returncode == 0:
                return r.stdout.strip()
            err = RuntimeError(f'{" ".join(cmd)}: {r.stderr.strip()}')
        if n + 1 < tries:
            log(f'SNMP {kind} failed (try {n + 1}), retrying: {err}')
            time.sleep(2)
    raise err


def get(param, ch):
    """float value with full precision"""
    return float(run('get', '-Oqv', '-Op', '+.12', IP, f'{param}.{ch}').split()[0])


def get_str(param, ch):
    return run('get', '-Oqv', IP, f'{param}.{ch}')


def status_flags(ch):
    out = run('get', '-Oq', IP, f'outputStatus.{ch}')
    return [w for w in out.split() if '(' in w]


vset_max = 0.0  # highest voltage set this run, for the ramp-down time


def put(param, value, ch, kind='F'):
    global vset_max
    if param == 'outputVoltage':
        vset_max = max(vset_max, abs(value))
    if args.dry_run:
        print(f'  [dry-run] would set {param}.{ch} = {value}')
        return
    run('set', IP, f'{param}.{ch}', kind, str(value), community='guru')


def put_all(param, value, kind='F'):
    for ch in chs:
        put(param, value, ch, kind)


def read():
    t = time.time()
    return {ch: dict(t=t, v=get('outputMeasurementTerminalVoltage', ch), i=get('outputMeasurementCurrent', ch))
            for ch in chs}


def fmt(r):
    return '  '.join(f'{ch} V={r[ch]["v"]:7.3f} I={r[ch]["i"]:+.3e}' for ch in chs)


def check(r):
    for ch in chs:
        bad = [f for f in status_flags(ch) if any(w in f for w in FAIL_WORDS)]
        if bad:
            raise RuntimeError(f'{ch} status {bad}')
        if abs(r[ch]['i']) > 0.8 * ilim[ch]:
            raise RuntimeError(f'{ch} current {r[ch]["i"]:.3e} A above 80% of the {ilim[ch]:.1e} A limit')
    v = [r[ch]['v'] for ch in chs]
    if max(v) - min(v) > args.max_dv:
        raise RuntimeError(f'channels differ by {max(v) - min(v):.2f} V, above --max-dv {args.max_dv:g}')


def watch(seconds, hist=None):
    """read and check for the given time"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(args.poll)
        r = read()
        check(r)
        if hist is not None:
            hist.append(r)


def move_to(vset, hist):
    """lockstep move of all channels to vset in --dv increments"""
    v_now = statistics.mean(get('outputVoltage', ch) for ch in chs)
    n = max(1, int(abs(vset - v_now) / args.dv + 0.999))
    for k in range(1, n + 1):
        v = v_now + (vset - v_now) * k / n
        put_all('outputVoltage', round(v, 3))
        t0, resent = time.time(), False
        while True:
            time.sleep(args.poll)
            r = read()
            check(r)
            hist.append(r)
            if all(abs(r[ch]['v'] - v) <= args.v_tol for ch in chs):
                break
            # the crate can drop a set: resend once to a lagging channel whose set point is wrong
            if not resent and time.time() - t0 > 60:
                for ch in chs:
                    if abs(r[ch]['v'] - v) > args.v_tol and abs(get('outputVoltage', ch) - v) > 0.01:
                        log(f'  {ch} set point lost, resending {v:.2f} V')
                        put('outputVoltage', round(v, 3), ch)
                resent = True
            # switch-on delay can be ~100 s, after that a lagging channel is a fault
            if time.time() - t0 > 2 * args.dv / args.rate + 180:
                raise RuntimeError(f'not all channels reached {v:.2f} V: {fmt(r)}')
        log(f'  at {v:.2f} V  {fmt(r)}')


def stable_one(hist, ch, vset):
    """step_iv.py stability test on one channel, returns (stable, message)"""
    v_tol = max(args.v_tol, 0.02 * vset)
    t_last = hist[-1][ch]['t']
    win = [h[ch] for h in hist if h[ch]['t'] >= t_last - args.window and abs(h[ch]['v'] - vset) <= v_tol]
    if len(win) < 6 or win[-1]['t'] - win[0]['t'] < 0.9 * args.window:
        return False, 'filling'
    if max(h['v'] for h in win) - min(h['v'] for h in win) > args.v_flat:
        return False, 'V not flat'
    half = len(win) // 2
    a = statistics.mean(h['i'] for h in win[:half])
    b = statistics.mean(h['i'] for h in win[half:])
    tol = max(args.abs_tol, args.rel_tol * abs(b))
    return abs(b - a) < tol, f'drift {b - a:+.2e} (tol {tol:.1e})'


def run_step_cmd(vset):
    cmd = args.step_cmd.replace('{v}', f'{vset:g}')
    log(f'  step-cmd: {cmd}')
    proc = subprocess.Popen(cmd, shell=True, start_new_session=True)
    try:
        while proc.poll() is None:
            time.sleep(args.poll)
            check(read())
    except BaseException:
        os.killpg(proc.pid, signal.SIGTERM)
        raise
    log(f'  step-cmd rc={proc.returncode}')
    return proc.returncode


def shutdown(switched_on):
    if args.dry_run or not switched_on:
        return
    log('ramping all to 0 V together')
    # a second signal must not cut the ramp-down short
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    try:
        put_all('outputVoltage', 0.0)
        t0 = time.time()
        # readback can lag, so wait most of the ramp time and for 3 low readings in a row
        v_ref = max([abs(get('outputMeasurementTerminalVoltage', ch)) for ch in chs] + [vset_max])
        n_low = 0
        while time.time() - t0 < v_ref / args.rate + 60:
            try:
                r = read()
            except Exception as e:
                print(f'  read failed: {e}')
                time.sleep(args.poll)
                continue
            print(f'  {fmt(r)}', flush=True)
            n_low = n_low + 1 if all(abs(r[ch]['v']) < 0.2 for ch in chs) else 0
            if n_low >= 3 and time.time() - t0 >= 0.8 * v_ref / args.rate:
                break
            time.sleep(args.poll)
    finally:
        for ch in chs:
            put('outputSwitch', 0, ch, kind='i')
        log('switched off:', ' '.join(f'{ch}={get_str("outputSwitch", ch)}' for ch in chs))


# --- pre-checks (read only)
for ch in chs:
    state = dict(switch=get_str('outputSwitch', ch), vset=get('outputVoltage', ch),
                 ilim=get('outputCurrent', ch), rise=get('outputVoltageRiseRate', ch),
                 vmeas=get('outputMeasurementTerminalVoltage', ch),
                 imeas=get('outputMeasurementCurrent', ch), status=status_flags(ch))
    print(f'{ch} now: {json.dumps(state)}')
    if args.adopt:
        # on, set and measured at the hold voltage
        if 'on' not in state['switch'] or state['vset'] != args.voltages[-1] \
                or abs(state['vmeas'] - args.voltages[-1]) > 1.0:
            sys.exit(f'{ch} is not on at {args.voltages[-1]:g} V, cannot adopt')
    elif 'on' in state['switch'] or state['vset'] != 0.0:
        sys.exit(f'{ch} is not off at 0 V, refusing to start')
if args.adopt and args.hold <= 0:
    sys.exit('--adopt needs --hold')
if args.vmax > 200:
    sys.exit('--vmax above 200 V is not allowed by this script')
if max(args.voltages) > args.vmax:
    sys.exit(f'set points above --vmax {args.vmax:g} V are not allowed')
print(f'plan: {" ".join(f"{ch} limit {ilim[ch]:.1e} A" for ch in chs)}, rate {args.rate} V/s, '
      f'lockstep {args.dv:g} V (abort above {args.max_dv:g} V apart), steps {args.voltages} V, '
      f'stable over {args.window:.0f} s, {args.samples} samples per step, timeout {args.timeout:.0f} s per step'
      + (f', step-cmd "{args.step_cmd}"' if args.step_cmd else ''), flush=True)


def on_signal(signum, frame):
    # a closed terminal (SIGHUP) or kill (SIGTERM) must still ramp down
    raise KeyboardInterrupt


signal.signal(signal.SIGHUP, on_signal)
signal.signal(signal.SIGTERM, on_signal)
steps = {ch: [] for ch in chs}
switched_on, cur = False, None
try:
    for ch in chs:
        put('outputCurrent', ilim[ch], ch)
    put_all('outputVoltageRiseRate', args.rate)
    put_all('outputVoltageFallRate', args.rate)
    if args.adopt:
        switched_on = True
        log('adopted:', ' '.join(f'{ch}={get_str("outputSwitch", ch)}' for ch in chs))
    else:
        put_all('outputVoltage', 0.0)
    if args.dry_run:
        for v in args.voltages:
            put_all('outputVoltage', v)
        print('dry run done, nothing was changed')
        sys.exit(0)
    if not args.adopt:
        put_all('outputSwitch', 1, kind='i')
        switched_on = True
        log('switched on:', ' '.join(f'{ch}={get_str("outputSwitch", ch)}' for ch in chs))
        watch(30)

    for vset in ([] if args.adopt else args.voltages):
        log(f'--- {vset} V')
        t0, hist = time.time(), []
        cur = dict(v_set=vset, history=hist)
        move_to(vset, hist)
        stable = False
        # timeout counts from arrival, a long ramp must not use it up
        t_arr = time.time()
        while time.time() - t_arr < args.timeout:
            time.sleep(args.poll)
            r = read()
            check(r)
            hist.append(r)
            res = {ch: stable_one(hist, ch, vset) for ch in chs}
            if int(r[chs[0]]['t'] - t0) % 20 < args.poll:
                log(f'  {r[chs[0]]["t"] - t0:5.0f} s  {fmt(r)}  ' + '  '.join(m for _, m in res.values()))
            if all(s for s, _ in res.values()):
                stable = True
                log(f'  stable  {fmt(r)}')
                break
        if not stable:
            log(f'  not stable after {args.timeout:.0f} s, recording anyway')
        samples = []
        for k in range(args.samples):
            time.sleep(args.poll)
            r = read()
            check(r)
            samples.append(r)
        rc = run_step_cmd(vset) if args.step_cmd else None
        for ch in chs:
            i = [s[ch]['i'] for s in samples]
            v = [s[ch]['v'] for s in samples]
            steps[ch].append(dict(v_set=vset, stable=stable, settle_s=round(hist[-1][ch]['t'] - t0, 1),
                                  v_mean=statistics.mean(v), i_mean=statistics.mean(i),
                                  i_std=statistics.stdev(i), step_cmd_rc=rc,
                                  history=[h[ch] for h in hist], samples=[s[ch] for s in samples]))
            log(f'  RESULT {ch} {vset} V: V={steps[ch][-1]["v_mean"]:.4f} V  '
                f'I={steps[ch][-1]["i_mean"]:.4e} +- {steps[ch][-1]["i_std"]:.1e} A')
        cur = None
    if args.hold > 0:
        log(f'holding {args.voltages[-1]:g} V for {args.hold:.0f} s')
        # write a new total hold (s) into this file to extend or shorten
        hold_file = f'{args.output}.hold' if args.output else None
        hold = args.hold
        if hold_file:
            with open(hold_file, 'w') as f:
                f.write(f'{hold:.0f}\n')
            log(f'hold length in {hold_file}')
        t0 = time.time()
        while time.time() - t0 < hold:
            time.sleep(args.poll)
            try:
                new = float(open(hold_file).read()) if hold_file else hold
            except (OSError, ValueError):
                new = hold
            if new != hold:
                log(f'hold changed {hold:.0f} -> {new:.0f} s')
                hold = new
            r = read()
            check(r)
            if int(r[chs[0]]['t'] - t0) % 20 < args.poll:
                log(f'  hold {r[chs[0]]["t"] - t0:5.0f} s  {fmt(r)}')
        log('hold done')
except KeyboardInterrupt:
    log('interrupted, shutting down')
except Exception as e:
    log(f'ABORT: {e}')
    raise
finally:
    shutdown(switched_on)
    if cur is not None and cur['history']:
        # interrupted step, keep what was read
        for ch in chs:
            h = [x[ch] for x in cur['history']]
            steps[ch].append(dict(v_set=cur['v_set'], stable=False, settle_s=round(h[-1]['t'] - h[0]['t'], 1),
                                  v_mean=h[-1]['v'], i_mean=h[-1]['i'], i_std=0.0, interrupted=True, history=h,
                                  samples=[]))
    if any(steps.values()):
        prefix = args.output or time.strftime(f'iv_{"_".join(chs)}_%Y%m%d_%H%M%S')
        for ch in chs:
            out = f'{prefix}_{ch}.json'
            with open(out, 'w') as f:
                json.dump(dict(channel=ch, channels=chs, args=vars(args), steps=steps[ch]), f, indent=1)
            print(f'saved {out}')
            for s in steps[ch]:
                print(f'  {ch} {s["v_set"]:5.2f} V  {s["v_mean"]:.4f} V  {s["i_mean"]:.4e} A  +- {s["i_std"]:.1e}'
                      f'  {"stable" if s["stable"] else "NOT stable"} after {s["settle_s"]:.0f} s')
