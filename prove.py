#!/usr/bin/env python3
"""Is a given channel REALLY a transmission, or did the scanner fool itself?

    python3 prove.py 162.5500 462.6000 ...
    python3 prove.py --wav 162.5500        also write a listenable file

No decoding. Three things are measured, and noise cannot fake any of them:

  CARRIER   a transmitter sits on one frequency. Measured as the wander (Hz)
            of the peak across short windows. Noise has no carrier, so its
            "peak" jumps around the channel at random.
  FLATNESS  the decisive one, and deliberately MODULATION-AGNOSTIC. It asks
            only "is this spectrum featureless?" Demodulated noise is smooth
            and shapeless. Anything carrying information — voice, Morse,
            telemetry, packet data, a digital burst — has to impose structure
            to carry it, and structure shows up as peaks and troughs. Low
            flatness = information present. It never asks what the data says,
            and it does not assume the data is speech.
  DYNAMICS  information varies over time; a noise floor sits still.

Every run also measures synthetic noise as a reference row, so the numbers are
read as a comparison rather than against thresholds pulled from the air.
"""
import struct, sys
import numpy as np
import scan, rtl

SECS      = 10.0
OFFSET    = 300_000       # tune off-centre so the DC spike is never the signal


def safe_offset(f_hz, off=OFFSET, clocks=None):
    """Pick a tuner offset that does not park the centre on a local clock.

    The centre is where the DC spike and the strongest local leakage sit. With
    a fixed 300 kHz offset, a signal at 29.1 MHz puts the centre on 28.8 MHz —
    the dongle's own reference — and the measurement collapses. Measured across
    three offsets: 29.1 read -0.7 / -0.1 / +9.3 dB, a 10 dB spread from nothing
    but where the tuner was pointed, while NOAA read 37.9 / 37.9 / 37.8.
    Flipping the offset when the centre lands near a clock costs nothing and
    removes a whole class of false readings.

    `clocks` defaults to the RTL-SDR's set. Pass the caller's own list — the
    RSP1B does not have a 28.8 MHz reference and avoiding it there displaces
    the capture for nothing. That matters beyond tidiness: the candidates are
    tried smallest-first, and every needless rejection pushes the search onto
    the ±2x offset, which is the only one wide enough to reach past Nyquist.
    """
    if clocks is None:
        clocks = (28_800_000.0, 12_000_000.0, 27_000_000.0)
    for cand in (off, -off, off * 2, -off * 2):
        c = f_hz - cand
        if all(abs(c - round(c / clk) * clk) > 400_000
               for clk in clocks) and c > 1e6:
            return cand
    return off
CHAN_RATE = 48_000        # baseband rate; +/-24 kHz covers any NFM channel


def spectrum(x):
    """The one expensive step in channelize, done once for a whole capture.

    Extracting a channel means transforming the capture to the frequency
    domain and cutting out the bins that belong to that channel. The transform
    costs ~37 ms on a 1.2 s capture; the cut costs almost nothing. Doing it
    inside channelize meant a 40-channel slice transformed the SAME array 40
    times - 1500 ms where 64 ms would do, with the thread pool faithfully
    parallelising duplicated work. Pass the result to channelize as `pre`.
    """
    return np.fft.fft(x)


def channelize(x, fs, foff, out_rate, pre=None):
    """Frequency-domain channel extract: pick the bins around foff, move them
    to DC, transform back at a lower rate. numpy only, and fast.

    `pre` is spectrum(x) if you already have it - required to be the transform
    of exactly this x. Callers extracting many channels from one capture should
    compute it once; a lone caller can leave it None and pay for its own.
    """
    n = len(x)
    m = int(round(out_rate / fs * n))
    k0 = int(round(foff / fs * n))
    idx = (np.arange(m) - m // 2 + k0) % n
    X = spectrum(x) if pre is None else pre
    return np.fft.ifft(np.fft.ifftshift(X[idx])) * (m / n)


def metrics(y, rate=CHAN_RATE, force=False):
    """-> (active fraction, carrier wander Hz, flatness, dynamics, presence dB)

    A bursty channel is SILENT most of the time, so measuring the whole capture
    as one lump just averages the transmission away and calls it noise. Instead
    the capture is cut into short windows, each is asked "is a carrier here
    RIGHT NOW", and the modulation is judged only on the windows that say yes.
    """
    win = 4096                                  # ~85 ms
    nw = len(y) // win
    if nw < 4:
        return 0.0, *(float("nan"),) * 4
    z = y[:nw * win].reshape(nw, win)
    Z = np.fft.fftshift(np.fft.fft(z * np.hanning(win), axis=1), axes=1)
    f = np.fft.fftshift(np.fft.fftfreq(win, 1 / rate))
    P = np.abs(Z) ** 2

    # presence: a narrowband channel puts its power in the middle of the span,
    # the edges stay at the noise floor. Noise fills both equally.
    # scale with the channel width, so a WIDE data burst (pagers are ~21 kHz)
    # is not mistaken for absent by a test tuned to narrowband voice
    mid = np.abs(f) < rate / 8
    out = (np.abs(f) > rate / 4) & (np.abs(f) < rate * 0.46)
    pres = 10 * np.log10(P[:, mid].mean(axis=1) / (P[:, out].mean(axis=1) + 1e-20))
    active = pres > 6.0
    frac = float(active.mean())
    if force:                 # noise reference: measure it anyway, so the
        active = np.ones(nw, bool)   # baseline is MEASURED and not invented
    if active.sum() < 2:
        return (frac, float("nan"), float("nan"), float("nan"),
                float(np.percentile(pres, 95)))

    pk = f[np.argmax(np.abs(Z[active]), axis=1)]
    wander = float(np.std(pk))

    # Structure is measured through BOTH demodulators and the better one wins.
    #
    # Assuming FM was a real bug: airband (118-137) and military air (225-400)
    # are AM, and an FM discriminator fed an AM signal produces noise-like
    # rubbish. A perfectly good AM voice channel measured as dynamics 0.4 dB —
    # indistinguishable from a dead band. Since the whole point is to detect
    # data WITHOUT knowing what it is, the detector must not assume how it was
    # modulated either. Trying both costs one extra FFT and removes the
    # assumption entirely.
    fm = np.angle(y[1:] * np.conj(y[:-1]))
    fm = np.r_[fm, fm[-1]]
    am = np.abs(y)
    best = None
    for sig in (fm, am):
        w = sig[:nw * win].reshape(nw, win)[active].astype(np.float64)
        w = w - w.mean(axis=1, keepdims=True)
        D = (np.abs(np.fft.rfft(w * np.hanning(win), axis=1)) ** 2).mean(axis=0)
        fa = np.fft.rfftfreq(win, 1 / rate)
        # Notch our OWN mains hum before judging structure. Measured here:
        # 120 Hz and harmonics run +31 dB over the floor on a strong carrier —
        # and +9 dB on an empty channel, which is how we know it is generated
        # locally rather than transmitted. Envelope demodulation scales that
        # ripple with carrier strength, so a big idle carrier grows a whole
        # harmonic series that looks exactly like modulation and drags flatness
        # under the threshold. An idle carrier was being reported as DATA.
        band = (fa >= 300) & (fa <= rate * 0.31)
        hum = np.zeros_like(fa, bool)
        for k in range(1, int(rate * 0.31 / 60) + 1):
            hum |= np.abs(fa - 60.0 * k) < 15.0
        Db = D[band & ~hum] + 1e-20
        # FLATNESS (Wiener entropy): is this spectrum featureless? Demodulated
        # noise is smooth and shapeless -> ~1. Anything carrying information had
        # to impose structure to carry it -> well below 1.
        f_ = float(np.exp(np.mean(np.log(Db))) / np.mean(Db))
        # Dynamics of the BUSIEST second, not of the whole capture.
        # A carrier that is up the whole time keeps every window "active", so
        # a short transmission gets averaged in with the idle carrier around
        # it. Measured on 147.4550: one second of real modulation followed by
        # seven seconds of dead carrier read as dynamics 0.3 — reported as an
        # idle carrier when a human plainly heard the transmission. Taking the
        # best second finds it; averaging never will. Same failure as ADS-B:
        # anything brief disappears into the mean.
        a = w.reshape(-1)
        m = (len(a) // 512) * 512
        e = 10 * np.log10((a[:m].reshape(-1, 512) ** 2).mean(axis=1) + 1e-20)
        per_sec = max(int(round(rate / 512)), 4)
        if len(e) >= per_sec * 2:
            chunks = [float(np.std(e[i:i + per_sec]))
                      for i in range(0, len(e) - per_sec + 1, per_sec // 2)]
            d_ = float(max(chunks))
        else:
            d_ = float(np.std(e))
        if best is None or f_ < best[0]:      # lower flatness = more structure
            best = (f_, d_)
    return frac, wander, best[0], best[1], float(np.percentile(pres, 95))


def syllabic(y, rate=CHAN_RATE):
    """dB by which 3-6 Hz stands out of the audio's loudness envelope.

    Speech starts and stops at roughly that rate; a steady carrier does not.
    Measured: NOAA 13.7, an ear-verified 2m voice channel 16.3, an idle
    carrier 0.3, a pure tone -2.4.

    BOTH DEMODULATORS, best of the two. This used to run the FM discriminator
    only, which meant AM voice scored exactly 0.00 and read as "data" — and
    since kind_of() asks this function, airband 118-137 and mil air 225-400
    were structurally incapable of ever being called voice. Measured on the
    same NOAA / 450.7250 / 145.2250 audio, AM-modulated instead of FM:
    syllabic 0.00, kind_of "data", while metrics() on the identical signal
    still returned flat 0.71 / dyn 3.53.

    metrics() already learned this lesson (see the both-demodulators comment
    there — "Assuming FM was a real bug: airband and military air are AM").
    It was never applied one function down. Same rule, no new threshold: try
    each envelope, keep the higher score. An FM signal's amplitude envelope is
    flat by construction, so the AM branch cannot inflate an FM reading."""
    if len(y) < rate // 2:
        return 0.0

    def rhythm(env):
        k = max(int(rate // 200), 1)
        e = env[:len(env) // k * k].reshape(-1, k).mean(axis=1)
        e = e - e.mean()
        if e.std() < 1e-15 or len(e) < 32:
            return 0.0
        E = np.abs(np.fft.rfft(e * np.hanning(len(e)))) ** 2
        fa = np.fft.rfftfreq(len(e), 1 / 200.0)
        lo = E[(fa >= 2.5) & (fa <= 7.0)].mean()
        hi = E[(fa >= 25) & (fa <= 90)].mean()
        return float(10 * np.log10(lo / (hi + 1e-30)))

    d = np.angle(y[1:] * np.conj(y[:-1]))
    fm = rhythm(np.abs(d - np.median(d)))
    am = rhythm(np.abs(y[1:]).astype(np.float64))
    return max(fm, am)


def kind_of(y, rate=CHAN_RATE):
    """voice | digital | data — what KIND of information, not what it says.

    Two features, neither of which needs a decoder:
      VOICE    speech starts and stops at roughly 3-6 Hz, so the audio's own
               loudness envelope has a bump there. Measured: NOAA 13.7 dB,
               an idle carrier 0.3.
      DIGITAL  a fixed symbol clock puts a STABLE line in the spectrum of the
               demodulated signal's rate of change. P25 sits at exactly
               4800 Hz on every run; a FLEX pager at 3200. Voice has no such
               line. Stability is the test — a single strong peak can be a
               tone, so it only counts if the two halves of the capture agree.
    """
    if len(y) < rate:
        return "data"
    d = np.angle(y[1:] * np.conj(y[:-1]))
    d = d - np.median(d)

    def clock(seg):
        t = np.abs(np.diff(seg))
        t = t - t.mean()
        T = np.abs(np.fft.rfft(t * np.hanning(len(t)))) ** 2
        fr = np.fft.rfftfreq(len(t), 1 / rate)
        m = (fr > 200) & (fr < 15000)
        T, fr = T[m], fr[m]
        i = int(np.argmax(T))
        return float(10 * np.log10(T[i] / (np.median(T) + 1e-30))), float(fr[i])

    # Compare halves only where the signal is actually PRESENT. Requiring the
    # same symbol rate in both halves stops a pure tone posing as digital, but
    # a bursty transmitter (a pager sends, then idles) fails it for the wrong
    # reason: the silent half has no clock to agree with. 929.6125 is digital
    # by ear and was being called plain "data" for exactly this.
    k = max(int(rate * 0.1), 256)
    lvl = np.abs(y[:len(y) // k * k]).reshape(-1, k).mean(axis=1)
    if len(lvl) >= 4:
        loud = lvl > (np.median(lvl) + lvl.max()) / 2.0
        # np.repeat gives (len(y)//k)*k flags, but d has len(y)-1 samples, so
        # for any capture that is not a whole number of k-sample blocks the
        # mask is SHORT and numpy raises IndexError on the boolean index. That
        # exception is swallowed by verify_slice's except clause, so the
        # channel silently never got a verdict and displayed as a placeholder
        # for as long as it stayed on air. Pad instead: the trailing partial
        # block (under 0.1 s) simply counts as not-loud.
        idx = np.zeros(len(d), bool)
        rep = np.repeat(loud, k)[:len(d)]
        idx[:len(rep)] = rep
        act = d[idx] if idx.sum() > rate // 2 else d
    else:
        act = d
    h = len(act) // 2
    if h > rate // 4:
        c1, f1 = clock(act[:h])
        c2, f2 = clock(act[h:])
        if min(c1, c2) > 18.0 and abs(f1 - f2) < 150.0:
            return "digital"

    if syllabic(y, rate) > 6.0:
        return "voice"
    return "data"


def pulses(x, rate, lo_us=20, hi_us=200):
    """Count short on/off BURSTS in the raw envelope. No channelising, no
    demodulation, no decoding — just "did the power slam on and off again".

    This exists because the rest of this file is blind to fast data. ADS-B
    (1090 MHz) transmits 56 or 112 microsecond pulses; the 85 ms windows used
    elsewhere average them into nothing. Here we look at the envelope at full
    sample rate, where such a burst is a few hundred samples.

    Noise cannot produce these: its envelope crosses any threshold constantly
    and at random lengths, never in a tight band of durations.
    """
    env = np.abs(x)
    k = 4                                     # light smoothing, ~1.7 us
    env = np.convolve(env, np.ones(k) / k, mode="same")
    floor = np.median(env)
    hot = env > floor * 3.0                   # ~9.5 dB over the floor

    # run-length encode the hot regions
    d = np.diff(hot.astype(np.int8))
    starts = np.flatnonzero(d == 1) + 1
    ends = np.flatnonzero(d == -1) + 1
    if hot[0]:
        starts = np.r_[0, starts]
    if hot[-1]:
        ends = np.r_[ends, len(hot)]
    n = min(len(starts), len(ends))
    if n == 0:
        return 0, 0.0, 0.0
    dur_us = (ends[:n] - starts[:n]) / rate * 1e6
    inband = (dur_us >= lo_us) & (dur_us <= hi_us)
    return int(inband.sum()), float(np.median(dur_us[inband])) \
        if inband.any() else 0.0, float(len(dur_us))


def wav(path, d, rate=CHAN_RATE, am=False):
    """Demodulated audio, decimated to ~16 kHz, for a human ear.

    AM matters: airband and military air are AM, not FM. Running an FM
    discriminator over an AM signal gives noise-like garbage and would make a
    perfectly good voice channel look unmodulated."""
    if am:
        a = np.abs(d)
        a = a - a.mean()                      # drop the carrier, keep the audio
    else:
        a = np.angle(d[1:] * np.conj(d[:-1]))
    # De-emphasis (75 us) and a 300-3400 Hz speech band. Raw discriminator
    # output is harsh and hiss-heavy: FM transmitters pre-emphasise the highs,
    # so without undoing it everything above ~2 kHz is exaggerated. Measured
    # A/B on the same 105 recordings, transcribed both ways: 5 improved from
    # "no speech" to real words ("What the heck?", "K4G", "Bye!"), none got
    # worse. Affects what gets written and played, never what gets detected.
    out_rate = 16000
    F = np.fft.rfft(a)
    fr = np.fft.rfftfreq(len(a), 1.0 / rate)
    F /= (1.0 + 1j * 2 * np.pi * fr * 75e-6)        # de-emphasis
    F[(fr < 300) | (fr > 3400)] = 0                 # speech band
    a = np.fft.irfft(F, len(a))
    a = a[::max(int(round(rate / out_rate)), 1)]
    a = a / (np.abs(a).max() + 1e-9) * 0.85
    pcm = (a * 32767).astype("<i2").tobytes()
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
                + b"data" + struct.pack("<I", len(pcm)) + pcm)
    print(f"    wrote {path}")


def pulse_mode(freqs, secs=2.0):
    """Fast-burst test, for data far too quick for the rest of this file."""
    # Max gain, not the sweep default: measured 3 ADS-B detections at 40 dB
    # versus 23 at 49.6 on the same signal. At 1090 MHz nothing clips.
    r = rtl.Rtl(rtl.find("R828D") or 0, scan.RATE, 49.6)
    n = int(secs * scan.RATE)
    rows = []
    try:
        for f in freqs:
            r.tune(f * 1e6)
            r.flush()
            cnt, med, tot = pulses(r.read(n), scan.RATE)
            rows.append((f"{f:.3f}", cnt, med, tot))
    finally:
        r.close()
    nz = (np.random.normal(0, .05, n) +
          1j*np.random.normal(0, .05, n)).astype(np.complex64)
    rows.append(("NOISE ref", *pulses(nz, scan.RATE)))

    print(f"\n  {secs:.0f}s per frequency, envelope at {scan.RATE/1e6:.1f} MS/s")
    print(f"\n  {'freq MHz':>10}  {'bursts':>7}  {'per sec':>8}  "
          f"{'median us':>9}  verdict")
    for name, cnt, med, tot in rows:
        rate_s = cnt / secs
        if name == "NOISE ref":
            v = "(reference)"
        elif cnt >= 10:
            v = "DATA — fast bursts"
        elif cnt > 0:
            v = "a few bursts"
        else:
            v = "nothing"
        print(f"  {name:>10}  {cnt:7d}  {rate_s:8.1f}  {med:9.1f}  {v}")
    print("\n  A burst is the envelope jumping ~10 dB and dropping back within"
          "\n  20-200 us. Noise has no reason to do that at a consistent length.")
    return 0


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    want_wav = "--wav" in argv
    if not args:
        print(__doc__)
        return 1

    rate = CHAN_RATE
    for a in argv[1:]:
        if a.startswith("--bw="):
            rate = int(float(a.split("=", 1)[1]) * 1000)

    if "--pulses" in argv:
        return pulse_mode([float(a) for a in args])
    am = "--am" in argv

    n = int(SECS * scan.RATE)
    r = rtl.Rtl(rtl.find("R828D") or 0, scan.RATE, scan.GAIN_DB)
    rows = []
    audio = {}
    try:
        for a in args:
            fmhz = float(a)
            off = safe_offset(fmhz * 1e6)
            r.tune(fmhz * 1e6 - off)
            r.flush()
            x = r.read(n)
            y = channelize(x, scan.RATE, off, rate)
            # keep the audio alongside its row. The verdict ladder below used
            # to call syllabic(y, ...) on whatever `y` the loop happened to
            # leave behind, so every row after the first was judged on the LAST
            # channel's audio.
            rows.append((f"{fmhz:.4f}", *metrics(y, rate)))
            audio[f"{fmhz:.4f}"] = y
            if want_wav:
                wav(f"proof_{fmhz:.4f}.wav", y, rate, am)
    finally:
        r.close()

    # reference: pure noise through the identical pipeline
    nz = (np.random.normal(0, .05, n) + 1j*np.random.normal(0, .05, n)).astype(np.complex64)
    rows.append(("NOISE ref", *metrics(channelize(nz, scan.RATE, OFFSET, rate),
                                       rate, force=True)))

    # The noise reference usually has NO active window at all — which is itself
    # the point — so fall back to a fixed baseline when it cannot be measured.
    nz_flat = rows[-1][3]
    if np.isnan(nz_flat):
        nz_flat = 0.90
    print(f"\n  {'channel':>10}  {'on-air':>7}  {'carrier':>9}  "
          f"{'flatness':>8}  {'dynamics':>8}  {'presence':>8}   verdict")
    print(f"  {'':>10}  {'%':>7}  {'wander Hz':>9}  {'0..1':>8}  "
          f"{'dB':>8}  {'dB':>8}")
    for name, frac, wander, flat, dyn, pres in rows:
        if name == "NOISE ref":
            v = "(reference)"
        elif frac < 0.02:
            v = "IDLE during capture"
        elif np.isnan(flat):
            v = "too brief to judge"
        # Flatness alone is NOT enough, and believing it was a mistake: a strong
        # carrier's phase noise rises toward low frequencies, so any big carrier
        # looks "structured" whether or not anything is on it. 268.2 MHz — an
        # idle carrier, confirmed by ear — scored 0.775 against a 0.80 gate.
        # What actually separates traffic from a bare carrier is whether the
        # modulation CHANGES: real voice measured 4.5 dB of variation, the idle
        # carrier 0.4, an empty channel 0.2.
        # DYNAMICS decides, not flatness. Flatness measures structure, and
        # both failure modes are structured: a pure tone is maximally so, and
        # a strong carrier's phase noise is non-flat too. Measured, flatness
        # cannot separate 450.7250 (data, confirmed by ear, 0.79) from 268.2
        # (idle carrier, confirmed by ear, 0.82). Requiring flat < 0.50 threw
        # away that ear-verified data channel.
        # Dynamics separates all of it: data 1.1-4.9, everything carrying
        # nothing 0.18-0.40. Flatness is kept only to name WHAT the non-data
        # is (a tone versus a bare carrier).
        # Flatness says "structured". It does NOT say "carrying information",
        # because a PURE TONE is maximally structured and conveys nothing —
        # it never changes. Measured on the 50-54 MHz cluster: flatness 0.08
        # (more peaked than NOAA voice at 0.23) with dynamics 0.17-0.20, which
        # is LESS variation than a dead carrier. Those were being called data.
        # Ear-verified data sits at 3.1-4.9; everything not carrying anything
        # sits at 0.17-0.41.
        # dynamics decides, but a weak signal's noise also fluctuates, so it
        # must also be measurably NON-flat. 327.1500 passed on dynamics 0.8
        # alone while sitting at flatness 0.933 (noise is 0.993) and only
        # 8.5 dB presence — that was noise wobbling, not modulation.
        elif dyn >= 0.70 and flat < 0.90:
            v = "DATA"
        elif name in audio and syllabic(audio[name], rate) > 6.0:
            # Rhythm alone is enough. Measured on 2m and confirmed by ear:
            # 144.3900 (flatness 0.963) and 146.4000 (0.670) are both voice,
            # and the flatness gate would have discarded both. Speech does not
            # have to produce a structured spectrum to be speech.
            v = "DATA"
        elif flat < 0.20:
            v = "tone (structured, static)"
        elif flat < 0.80:
            # carrier is real and rock steady, but nothing is varying. Could be
            # idle, could be a constant low-depth data stream — this method
            # cannot tell those apart, and should not pretend to.
            v = "carrier, steady (idle?)"
        else:
            v = "bare carrier"
        w = "  nan" if np.isnan(wander) else f"{wander:9.0f}"
        b = "     nan" if np.isnan(flat) else f"{flat:8.3f}"
        dd = "     nan" if np.isnan(dyn) else f"{dyn:8.1f}"
        print(f"  {name:>10}  {frac:6.0%}  {w}  {b}  {dd}  {pres:8.1f}   {v}")
    print(f"\n  Flatness baseline for pure noise: {nz_flat:.2f}"
          f" (lower = more structured).\n"
          f"  'IDLE' means nothing was on air during the capture — that is not"
          f" the same\n  as the scanner being wrong; bursty channels are silent"
          f" most of the time.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
