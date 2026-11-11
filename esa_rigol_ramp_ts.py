"""Slowly ramp one Rigol supply channel to a final voltage, with the supply's
over-current protection (OCP) armed as the current limit.

The voltage changes in steps of --step volts at --rate V/s (default 0.1 V every
1 s). After each step the measured voltage and current are printed. Before the
ramp, OCP is set to --ocp amps (default 0.005 A) and enabled, so the supply turns
the output off by itself if the current reaches the limit. If the output is found
off (OCP trip), or the measured current reaches the limit, the ramp stops.

Example:
    python esa_rigol_ramp.py -ch 1 2.0
"""
import argparse
import sys
import time

import vxi11

from esa_rigol_power import (check_voltage, check_current, set_voltage,
                             check_set_voltage, check_set_current,
                             set_ocp_level, check_ocp_level,
                             enable_ocp, check_ocp_enabled)

parser = argparse.ArgumentParser(
    prog='esa_rigol_ramp.py',
    description='Ramp a Rigol supply channel to a final voltage with OCP armed.')
parser.add_argument('final_voltage', type=float,
                    help='Final voltage to ramp to [V]')
parser.add_argument('-ch', '--channel', type=int, required=True,
                    help='Channel number')
parser.add_argument('--rate', type=float, default=0.1,
                    help='Ramp rate [V/s] (default 0.1)')
parser.add_argument('--step', type=float, default=0.1,
                    help='Voltage step [V] (default 0.1); step period = step/rate')
parser.add_argument('--ocp', type=float, default=0.005,
                    help='OCP trip current [A] (default 0.005)')
parser.add_argument('--ip', type=str, default='192.168.10.103',
                    help='Supply IP address (default 192.168.10.103)')


def output_state(instr, ch):
    return instr.ask(f":OUTP? CH{ch}").strip().upper()


def read_vi(instr, ch):
    return float(check_voltage(instr, ch)), float(check_current(instr, ch))


def print_status(instr, ch):
    v, i = read_vi(instr, ch)
    print(f"  Measured:    {v:.4f} V   {i:.6f} A")
    print(f"  Set points:  {check_set_voltage(instr, ch).strip()} V   "
          f"{check_set_current(instr, ch).strip()} A")
    print(f"  Output:      {output_state(instr, ch)}")
    print(f"  OCP level:   {check_ocp_level(instr, ch).strip()} A   "
          f"OCP enabled: {check_ocp_enabled(instr, ch).strip()}")


def setpoints(v_start, v_final, step):
    """Setpoints from v_start (exclusive) to v_final (inclusive) in |step| increments."""
    n = int(abs(v_final - v_start) // step)
    sign = 1.0 if v_final >= v_start else -1.0
    pts = [round(v_start + sign * step * k, 3) for k in range(1, n + 1)]
    if not pts or abs(pts[-1] - v_final) > 1e-6:
        pts.append(round(v_final, 3))
    return pts


def main():
    args = parser.parse_args()
    ch = args.channel

    if args.final_voltage < 0:
        sys.exit("Final voltage must be >= 0 V.")
    if args.step <= 0 or args.rate <= 0 or args.ocp <= 0:
        sys.exit("--step, --rate and --ocp must all be > 0.")
    period = args.step / args.rate

    instr = vxi11.Instrument(args.ip)
    print(instr.ask("*IDN?").strip())

    print(f"Channel {ch} before ramp:")
    print_status(instr, ch)

    if output_state(instr, ch) != 'ON':
        sys.exit(f"Channel {ch} output is OFF; nothing to ramp. Turn it on first with "
                 f"'python esa_rigol_power.py -ch {ch} -on'.")

    v_start = float(check_set_voltage(instr, ch))
    pts = setpoints(v_start, args.final_voltage, args.step)
    print(f"\nRamp channel {ch}: {v_start:.3f} V -> {args.final_voltage:.3f} V, "
          f"{args.step} V every {period:.2f} s ({args.rate} V/s), {len(pts)} steps, "
          f"~{len(pts) * period:.0f} s.")
    print(f"OCP will be set to {args.ocp} A and enabled before ramping.")
    confirm = input("Confirm [y/n] : ")
    if confirm != 'y':
        return

    # Arm the hardware current limit
    set_ocp_level(instr, ch, args.ocp)
    enable_ocp(instr, ch)
    ocp_level = float(check_ocp_level(instr, ch))
    ocp_state = check_ocp_enabled(instr, ch).strip().upper()
    print(f"OCP level read back: {ocp_level} A, enabled: {ocp_state}")
    if abs(ocp_level - args.ocp) > 1e-6 or ocp_state not in ('ON', '1'):
        sys.exit("OCP readback does not match the request; not ramping.")

    print(f"\n{'step':>4}  {'t[s]':>6}  {'Vset[V]':>8}  {'Vmeas[V]':>9}  {'Imeas[A]':>9}")
    t0 = time.time()
    try:
        for k, v_set in enumerate(pts, start=1):
            # Stop if the supply tripped (output off) since the last step
            if output_state(instr, ch) != 'ON':
                print(f"\nOutput is OFF (OCP trip) before step {k}; last Vset = "
                      f"{pts[k - 2] if k > 1 else v_start:.3f} V. Stopping.")
                print_status(instr, ch)
                sys.exit(1)

            set_voltage(instr, ch, v_set)
            time.sleep(period)
            v, i = read_vi(instr, ch)
            print(f"{k:4d}  {time.time() - t0:6.1f}  {v_set:8.3f}  {v:9.4f}  {i:9.6f}",
                  flush=True)

            if i >= ocp_level or output_state(instr, ch) != 'ON':
                print(f"\nCurrent limit / OCP trip reached at Vset = {v_set:.3f} V. Stopping.")
                print_status(instr, ch)
                sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted: ramp stopped, holding at the present setpoint.")
        print_status(instr, ch)
        return

    print(f"\nRamp complete. Channel {ch}:")
    print_status(instr, ch)


if __name__ == '__main__':
    main()
