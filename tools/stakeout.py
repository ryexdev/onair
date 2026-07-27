#!/usr/bin/env python3
"""Sit on one channel all night and save every transmission separately.

    python3 stakeout.py 147.4375              stop after 3 transmissions
    python3 stakeout.py 147.4375 5 30        stop after 5, or 30 minutes

Writes stakeout/<freq>_<time>.wav per transmission plus stakeout/log.jsonl with
the measurements. STOPS on its own once it has enough, or when the time limit
runs out — it is not a recorder, it is here to collect a handful of examples.

Why this exists: 147.4375 measured syllabic 16.6 dB — stronger speech rhythm
than NOAA — while someone was talking, and 1.6 dB a few minutes later when the
repeater sat idle. Every attempt to fix its classification was tuned against
whatever happened to be on air at that second, and broke other channels. The
only way out is a pile of captures of the SAME channel, some with speech and
some without, so a threshold can be chosen against evidence instead of luck.
"""
import json, os, sys, time
import numpy as np
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scan, rtl, prove

OUT = "stakeout"
BLOCK_S = 0.25
HANG_S = 1.5
MIN_S = 0.6              # ignore blips shorter than this
MAX_S = 8.0             # cap one recording


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    f = float(argv[1])
    want = int(argv[2]) if len(argv) > 2 else 3
    limit_min = float(argv[3]) if len(argv) > 3 else 60.0
    deadline = time.time() + limit_min * 60
    os.makedirs(OUT, exist_ok=True)
    r = rtl.Rtl(rtl.find("R828D") or 0, scan.RATE, scan.GAIN_LADDER[-2])
    n = int(BLOCK_S * scan.RATE)
    rate = prove.CHAN_RATE
    print(f"stakeout on {f:.4f} MHz -> {OUT}/")
    print(f"  stops after {want} transmissions, or {limit_min:.0f} min, "
          f"whichever comes first")
    print(f"  {'when':>8} {'secs':>5} {'snr':>6} {'syl':>6} {'flat':>6} "
          f"{'dyn':>5}  verdict")

    r.tune(f * 1e6 - prove.OFFSET)
    r.flush()
    active, last, started, n_saved = [], 0.0, 0.0, 0
    try:
        while time.time() < deadline:
            y = prove.channelize(r.read(n), scan.RATE, prove.OFFSET, rate)
            # is the channel up in this block?
            win = 4096
            nw = max(len(y) // win, 1)
            z = y[:nw * win].reshape(nw, win)
            P = np.abs(np.fft.fftshift(np.fft.fft(z * np.hanning(win), axis=1),
                                       axes=1)) ** 2
            fr = np.fft.fftshift(np.fft.fftfreq(win, 1 / rate))
            mid, out = np.abs(fr) < rate / 8, (np.abs(fr) > rate / 4) & (np.abs(fr) < rate * .46)
            pres = 10 * np.log10(P[:, mid].mean(axis=1) /
                                 (P[:, out].mean(axis=1) + 1e-20))
            now = time.time()
            up = (pres > 6.0).any()
            if up:
                if not active:
                    started = now
                active.append(y)
                last = now
            # Save when the channel drops OR when we already have enough audio.
            # Waiting only for the drop meant a continuous conversation was
            # buffered forever and never written — the exact case we are here
            # to capture.
            if active and (len(active) * BLOCK_S >= MAX_S
                           or (not up and now - last > HANG_S)):
                dur = len(active) * BLOCK_S
                if dur >= MIN_S:
                    aud = np.concatenate(active)[:int(MAX_S * rate)]
                    _, wander, flat, dyn, p95 = prove.metrics(aud, rate)
                    syl = prove.syllabic(aud, rate)
                    kind = prove.kind_of(aud, rate)
                    stamp = time.strftime("%H%M%S")
                    name = f"{f:.4f}".replace(".", "_") + f"_{stamp}.wav"
                    prove.wav(os.path.join(OUT, name), aud, rate)
                    rec = {"freq": f, "file": name, "when": stamp,
                           "secs": round(dur, 2), "snr": round(p95, 1),
                           "syllabic": round(syl, 1),
                           "flat": None if np.isnan(flat) else round(flat, 3),
                           "dyn": None if np.isnan(dyn) else round(dyn, 2),
                           "wander": None if np.isnan(wander) else round(wander),
                           "kind": kind}
                    with open(os.path.join(OUT, "log.jsonl"), "a") as fh:
                        fh.write(json.dumps(rec) + "\n")
                    n_saved += 1
                    print(f"  {stamp:>8} {dur:5.1f} {p95:6.1f} {syl:6.1f} "
                          f"{rec['flat'] if rec['flat'] is not None else 0:6.3f} "
                          f"{rec['dyn'] if rec['dyn'] is not None else 0:5.2f}  {kind}")
                    if n_saved >= want:
                        print(f"\n{n_saved} transmissions captured — done.")
                        break
                active = []
                if up:            # still talking: start the next chunk now
                    active, last, started = [y], now, now
        else:
            print(f"\ntime limit reached, {n_saved} captured.")
    except KeyboardInterrupt:
        print(f"\n{n_saved} transmissions saved to {OUT}/")
    finally:
        r.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
