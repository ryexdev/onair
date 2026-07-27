#!/usr/bin/env python3
"""Does the noise-vs-data theory actually hold? Measure it, don't assume it.

We have no labelled truth, so these tests avoid needing any:

    python3 tools/tune.py noise      synthetic Gaussian noise -> every hit is a FALSE
                               POSITIVE by construction. Measures FP rate.
    python3 tools/tune.py off        capture with the antenna unplugged. Same idea
                               but with real hardware artefacts included.
    python3 tools/tune.py live BAND  real air, for comparison.
    python3 tools/tune.py sweep      how thresholds trade FP against detections.

Everything reports the SAME metrics, so the distributions can be compared.
"""
import os
import sys
import numpy as np
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import scan, rtl

CAPS = 80                      # captures per condition


def relax():
    """Collect every candidate with its metrics, gates wide open, so the
    thresholds can be swept afterwards instead of baked in."""
    scan.SNR_MIN = 3.0
    scan.MIN_WIDTH_BINS = 1
    scan.MAX_WIDTH_BINS = 512


def synth(n):
    """Complex Gaussian noise: flat, structureless, no signals. The null."""
    return (np.random.normal(0, 0.05, n) +
            1j * np.random.normal(0, 0.05, n)).astype(np.complex64)


def collect_synth(caps=CAPS):
    n = scan.FRAMES * scan.NFFT
    out = []
    for i in range(caps):
        out += scan.analyse(synth(n), 160e6)
    return out


def collect_live(low, high, caps=CAPS, r=None):
    close = r is None
    if r is None:
        idx = rtl.find("R828D")
        r = rtl.Rtl(idx, scan.RATE, scan.GAIN_DB)
    span = scan.RATE * scan.USABLE
    n = scan.FRAMES * scan.NFFT
    out = []
    try:
        k = 0
        while k < caps:
            center = low * 1e6 + span * ((k % max(1, int((high - low) * 1e6 // span))) + 0.5)
            r.tune(center)
            r.flush()
            out += scan.analyse(r.read(n), center)
            k += 1
    finally:
        if close:
            r.close()
    return out


def describe(name, hits, caps=CAPS):
    print(f"\n=== {name} ===")
    print(f"  {len(hits)} candidates over {caps} captures "
          f"({len(hits)/caps:.2f} per capture)")
    if not hits:
        return
    for k in ("snr", "persist", "prom", "stab", "score"):
        v = np.array([h[k] for h in hits])
        print(f"  {k:8} min {v.min():6.2f}  p50 {np.median(v):6.2f}  "
              f"p90 {np.percentile(v,90):6.2f}  max {v.max():6.2f}")
    w = np.array([h["width"] for h in hits]) / 1000
    print(f"  {'width_k':8} min {w.min():6.2f}  p50 {np.median(w):6.2f}  "
          f"p90 {np.percentile(w,90):6.2f}  max {w.max():6.2f}")


def sweep(noise_hits, live_hits, caps=CAPS):
    print("\n=== threshold sweep ===")
    print("  per capture: how many candidates survive")
    print(f"  {'snr':>5} {'score':>6} {'wmin':>5} | {'NOISE(fp)':>10} "
          f"{'LIVE':>8}  {'ratio':>7}")
    for snr_min in (4.0, 5.0, 6.0, 8.0, 10.0):
        for score_min in (0.5, 0.6, 0.7):
            for wmin in (2,):
                def keep(hs):
                    return sum(1 for h in hs
                               if h["snr"] >= snr_min
                               and h["score"] >= score_min
                               and h["width"] >= wmin * scan.BIN_HZ)
                fp = keep(noise_hits) / caps
                lv = keep(live_hits) / caps
                ratio = (lv / fp) if fp > 0 else float("inf")
                print(f"  {snr_min:5.1f} {score_min:6.2f} {wmin:5d} | "
                      f"{fp:10.3f} {lv:8.2f}  "
                      f"{'inf' if ratio == float('inf') else f'{ratio:7.1f}'}")


def pass_over(r, lo, hi, laps, tag):
    """One independent observing session -> the set of confirmed channels.
    Uses the real tracker, so a channel must survive CONFIRM_LAPS to count."""
    import time
    span = scan.RATE * scan.USABLE
    n = scan.FRAMES * scan.NFFT
    steps = max(1, int(np.ceil((hi - lo) * 1e6 / span)))
    tr = scan.Tracker()
    for lap in range(1, laps + 1):
        for k in range(steps):
            r.tune(lo * 1e6 + span * (k + 0.5))
            r.flush()
            hits = scan.analyse(r.read(n), lo * 1e6 + span * (k + 0.5))
            list(tr.update(hits, lap, time.time()))
    out = {}
    for m in tr.live():
        key = int(round(m["freq"] / scan.SNAP_HZ) * scan.SNAP_HZ)
        d = scan.duty(m, laps)
        out[key] = "continuous" if d > 0.9 else "bursty"
    print(f"  {tag}: {len(out)} confirmed channels over {laps} laps")
    return out


def repeat(band, laps=14, gap=45):
    """Same band, two sessions separated in time. Noise cannot repeat itself;
    transmitters can. No ground truth needed — that is the point."""
    import time
    lo, hi, _ = scan.BANDS[band]
    idx = rtl.find("R828D")
    r = rtl.Rtl(idx, scan.RATE, scan.GAIN_DB)
    try:
        print(f"\n=== reproducibility, {band} {lo}-{hi} MHz ===")
        a = pass_over(r, lo, hi, laps, "session A")
        print(f"  waiting {gap}s ...")
        time.sleep(gap)
        b = pass_over(r, lo, hi, laps, "session B")
    finally:
        r.close()

    ka, kb = set(a), set(b)
    both = ka & kb
    print(f"\n  A only {len(ka-kb):4d}   BOTH {len(both):4d}   B only {len(kb-ka):4d}")
    print(f"  overlap (Jaccard)      {len(both)/max(len(ka|kb),1):6.1%}")
    print(f"  of A, seen again in B  {len(both)/max(len(ka),1):6.1%}")
    for kind in ("continuous", "bursty"):
        sub = {k for k in ka if a[k] == kind}
        if sub:
            print(f"    {kind:11} in A: {len(sub):4d}  -> reappeared "
                  f"{len(sub & kb)/len(sub):6.1%}")
    print("\n  A coin-flip detector would show near-0% here. Continuous\n"
          "  transmitters should approach 100%; bursty ones cannot, because a\n"
          "  real bursty channel is genuinely silent much of the time.")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "noise"

    if cmd == "repeat":
        # production thresholds on purpose — this measures the real detector
        repeat(argv[2] if len(argv) > 2 else "vhf")
        return 0

    relax()

    if cmd == "noise":
        describe("SYNTHETIC NOISE (all hits are false positives)",
                 collect_synth())

    elif cmd == "off":
        print("Antenna should be DISCONNECTED from the V4 now.")
        describe("ANTENNA OFF (real hardware, no signals)",
                 collect_live(160.0, 162.0))

    elif cmd == "live":
        band = argv[2] if len(argv) > 2 else "vhf"
        lo, hi, _ = scan.BANDS[band]
        describe(f"LIVE {band} {lo}-{hi} MHz", collect_live(lo, hi))

    elif cmd == "sweep":
        band = argv[2] if len(argv) > 2 else "vhf"
        lo, hi, _ = scan.BANDS[band]
        nh = collect_synth()
        lh = collect_live(lo, hi)
        describe("SYNTHETIC NOISE", nh)
        describe(f"LIVE {band}", lh)
        sweep(nh, lh)

    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
