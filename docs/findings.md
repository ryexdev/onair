# Review findings

**Where this stands — 11 done, 16 open.** Ranked by how much it affects what
you actually see, against what it costs to fix. That is deliberately NOT the
same axis the sections below use: those are grouped by KIND of wrongness, which
is why an item can sit in P1 and still be worth skipping. Trust this table for
what to do next; use the sections for the evidence behind each item.

| # | item | cost |
|---|------|------|
| 1 | Pagers read `voice` — the bursty clock test fails (152.2100) | medium, needs clip validation |
| 2 | Real voice reads `digital` on an exact 8 kHz line — whisper masks it | unknown, cause not identified |
| 3 | Prominence guard still in BINS not Hz — depresses genuine narrowband | faithful port |
| 4 | Anything over ~16 kHz unclassifiable — 25 kHz FM, TETRA, trunked | large |
| 5 | `spurs_rsp.json` cannot be produced — nothing writes it | small + antenna-off run |
| 6 | Band mode still slices at 1.9 MHz — sweep-only, band mode is unused | one number |
| 7 | Dense rasters merge — two channels 12.5 kHz apart read as one | medium |
| 8 | CFAR annulus sits on neighbours at 200 kHz — 2/9 found vs 8/9 at 400 kHz | one constant |
| 9 | Signals on under 15% of a capture score below `SCORE_MIN` | narrow class |
| 10 | RTL-only: `SPUR_TOL_HZ` blanks 10.9 MHz — not active on this hardware | n/a |
| 11-16 | Six SUSPECTED gain/timing items — measure before changing anything | measure first |

Only #1 affects the board day to day. #3 and #8 are cheap and would find more
channels. #4 and below are large, irrelevant to the sweep, or need measurement.

**Corrected, no action:** a false-positive `voice` CAN be retired — see the note
in P3. **Do not chase:** 406.125 rhythm shape, 161.6875. Both have the evidence
written up at the bottom.

---
Three independent reviews of the base logic (coverage, fingerprinting,
hardware) after the RSP1B port, 2026-07-31. Ranked by mission impact:
*find frequencies carrying information someone could actually use*.

`V` = verified by measurement or bench. `S` = suspected, not confirmed.
`[x] DONE` items are fixed and pushed.

Not from the reviews, found while working and fixed: the scanner exited with
"No radio found" when SDRconnect lost the device, and `reopen()` closed the old
radio before a single rebuild attempt, so one failure left the sweep holding a
dead socket and killed the board with a Bad-file-descriptor traceback. Both now
recover.

---

## P1 — wrong answers about real traffic

- [x] DONE — **V — AM voice can never read `voice`.** `syllabic()` prove.py:180 and
  `kind_of()` prove.py:209 use the FM discriminator only. The same NOAA audio
  AM-modulated scores rhythm **0.00** and reads `data`. Airband 118-137 and mil
  air 225-400 are structurally incapable of being called voice.
  *Fix:* take the max of the FM-discriminator and `np.abs(y)` envelopes.
  `metrics()` already tries both demodulators (prove.py:115-128) — apply the
  same rule one function down. No new threshold.

- [x] DONE — **V — ~5.5% of verdicts are computed on the wrong spectrum.**
  `verify_slice` scan.py:946-952 walks `tune_at` **monotonically +500 kHz** to
  dodge clocks; `channelize`'s `idx % n` (prove.py:75) then wraps past Nyquist
  **silently**. Over 908 slice centres: 89 shift twice, putting the slice
  centre itself past Nyquist. No error, no log — a confident verdict about a
  different frequency.
  *Fix:* use the `(off, -off, 2*off, -2*off)` search `prove.safe_offset`
  already implements (prove.py:42-47), and bail in `judge()` when
  `abs(off_f) > RATE*0.48`.

- [ ] **V — anything wider than ~16 kHz is unclassifiable.** `CHAN_RATE =
  48_000` is hardcoded at every call site (scan.py:966, 1012, 2117) and
  `m["width"]` is tracked at scan.py:1150 and never used. Measured: 12 kHz →
  `carrier`, 16 kHz → `noise`, 25 kHz → `noise`, 48-200 kHz → `quiet`. The WIDE
  detector finds these and the classifier then answers `quiet`. Costs 25 kHz
  analog FM, TETRA, trunked control, wideband links.
  *Fix:* pick the channelizer rate from `m["width"]` (`max(48_000, 4*width)`).
  `classify`/`metrics`/`syllabic` are already rate-parameterised.

- [x] DONE (by the wrong-frequency fix) — **V — continuous data reads `voice`.**
  507.3625 was labelled `voice` on the board. Re-measured after the Nyquist-wrap
  fix, three captures, and it reads **`digital` every time** — a textbook
  control-channel clock at 533 Hz, both halves agreeing at 28-30 dB. The board's
  `voice` was one of the ~5.5% of verdicts computed on the WRONG FREQUENCY.
  No classifier change was needed or made.

- [ ] **V — the clock test fails on BURSTY signals, so pagers read `voice`.**
  `kind_of()` masks down to the loud blocks, then throws that mask away when it
  covers under half a second (`idx.sum() > rate // 2`, prove.py) — which is
  exactly when the signal is bursty and the mask is needed. The clock is then
  measured across bursts AND the silence between them, which smears it into
  nothing. It also splices the loud blocks together and runs `np.diff` straight
  across every join, adding more garbage.

  Confirmed live on **152.2100**, a VHF pager, four captures:

      flat 0.187  c1 12.6 @ 8001 Hz   c2 13.3 @  917 Hz  -> voice
      flat 0.183  c1 12.0 @ 2977 Hz   c2 11.5 @ 2025 Hz  -> voice
      flat 0.181  c1 13.2 @ 4087 Hz   c2 13.1 @ 1590 Hz  -> data
      flat 0.180  c1 12.0 @  505 Hz   c2 12.1 @ 1343 Hz  -> data

  Flatness pinned at 0.18 — a machine, nothing like the 0.5-0.9 of real voice.
  Both halves land at 12-13 dB against an 18 dB gate and disagree wildly on
  frequency, so `digital` is ruled out and it falls through to the rhythm
  score, which flaps around 6.0 and lands on `voice`.

  Supersedes the earlier SUSPECTED note on 929.6125, which was resynthesis;
  this is the same defect measured live on a second channel.
  *Fix:* measure the clock on the longest CONTIGUOUS loud run — no splicing, no
  half-second minimum. Check against the recorded clips before shipping.

- [ ] **V — real VOICE intermittently reads `digital` on an exact ~8 kHz line.**
  The opposite defect, found while checking the above. Three captures each:

      161.6850 voice   digital / digital / digital   c1=c2 at 8000-8001 Hz, 21-24 dB
      162.5500 NOAA    voice   / voice   / digital   c1=c2 at 8001 Hz, 18.8 dB
      507.4350 data    tone    / tone    / digital   flat 0.019, dyn 0.08

  Two unrelated transmitters landing on the SAME exact frequency points at our
  chain, not theirs — but synthetic controls do not reproduce it: pure noise
  through the real channelizer gives 10.7-11.9 dB at random frequencies, and a
  synthetic FM voice gives 11.5-13.2 dB, all far below the 18 dB gate. So it is
  real in the signal and not a generic artifact.
  *Do not fix blind.* This is the same both-halves-agree weakness as the mains
  hum item below — a stable tone is precisely what agrees across halves.
  Needs: identify the 8 kHz line before touching `clock()`.

- [x] DONE — **V — idle carriers read `digital`.** `clock()` prove.py:212-220 has no
  mains-hum notch, while `metrics()` notches 60 Hz harmonics 12 lines earlier
  (prove.py:140-143) for exactly this reason. 268.2 — the channel that motivated
  the whole carrying test — measures `c1=23.3`, `c2=22.0`, both at **359.9 Hz =
  6 x 60**. The "both halves must agree" defence is backwards: a hum tone is
  precisely what agrees.
  *Fix:* reuse the existing `hum` mask in `clock()`. One line.

---

## P2 — coverage we are paying for and not getting

*(band mode's 1.9 MHz item is last here on purpose: it affects band
mode only, and the sweep is what actually gets used.)*

- [ ] **V — band mode still slices at the RTL-SDR's 1.9 MHz.** scan.py:2038
  hardcodes `1_900_000.0` while the sweep correctly uses `RATE * USABLE`.
  vhf 144-166 uses 12 slices where 5 would do; 8.3% duty against 20%. Adjacent
  captures overlap by 3.2 MHz — the radio re-observes the same spectrum instead
  of moving on. The printed "watched X% of the time" and the `hang`/`live_hold`
  derived from `n_sl` are both wrong.
  *Fix:* `1_900_000.0` → `RATE * USABLE`. One number.

- [ ] **V — channels under ~2% airtime never confirm.** 16 ms once per ~39 s
  lap, `CONFIRM_LAPS = 3` (scan.py:113) needs three sightings and
  `UNCONFIRMED_S = 600.0` (scan.py:117) resets the count first. Monte-Carlo of
  the real rules: 5% duty confirms in 187 min, 2% in 18 h (60% of the time),
  1% in 25 h (13%), 0.5% essentially never. That is most business, ham, GMRS
  and fire/EMS tactical traffic.
  *Fix:* `UNCONFIRMED_S: 600 → 3600` and `CONFIRM_LAPS: 3 → 2` gives 2% in
  5.5 h (100%) and 1% in 15 h (95%). The per-lap jitter at scan.py:2281
  already kills the false positives that motivated 3.

- [ ] **V — ~50 kHz blind zone at every step centre.** `DC_NOTCH` marks
  ±12 kHz invalid, then scan.py:458 requires `valid[i-1] and valid[j+1]`, so a
  group merely *adjacent* to the notch is discarded as edge-clipped. Bench:
  0/5 detected out to 22 kHz off centre, 5/5 from 26 kHz.
  *Fix:* test `usable[]` not `valid[]` at scan.py:458 — DC is a notch, not an
  edge. Verified patched: blind zone 50 kHz → 15 kHz.

- [ ] **V — step seams have zero overlap.** `span` is used both as the usable
  half-width (scan.py:440) and the step spacing (scan.py:2285), so windows abut
  exactly. The comment at scan.py:456 claims "the neighbouring step covers it"
  — it does not; the channel is edge-clipped in **both** and dropped by both.
  392 seams. Bench: 5/5 detected at 40 kHz inside the edge, 0/5 at 10 kHz.
  *Fix:* space steps at `span - 60e3` (keep `span` as the mask). 393 → 398
  steps, ~0.5 s per lap.

---

## P3 — footguns and small correctness

- [x] DONE — **V — importing `scan.py` kills the running scanner's stream.**
  `_pick_backend()` runs at import (scan.py:73) → `rsp.find()` builds a full
  `Rsp`, enables both streams, then `close()` sends `iq_stream_enable=false` to
  the *shared* server. Six modules import scan: prove.py, tools/meter.py,
  tools/tune.py, tools/label.py, tools/evalset.py, tools/stakeout.py.
  *Fix:* `find()` should connect, `get("valid_devices")`, close. Never touch
  `selected_device`, `device_sample_rate` or the stream enables.

- [x] DONE — **V — a `read()` larger than the ring is an infinite restart loop, not an
  error.** rsp.py:429-443: if `need > _RING_BYTES` the condition is
  unsatisfiable, so it raises the *misleading* "no IQ for 5 s", scan.py:2296
  calls `reopen()`, `close()` terminates SDRconnect, and the new `Rsp()` waits
  25 s — permanently, every step. The 16→96 MB change moved the cliff, it did
  not remove it.
  *Fix:* two lines at the top of `read()` raising `ValueError` when
  `need > _RING_BYTES`.

- [x] DONE — **V — `classify()` answers `noise` after certifying it is not noise.**
  scan.py:1094 is only reachable once `pres >= 8.0` passed at scan.py:1057. A
  66 dB presence bump is not noise. Continuous digital data lands here, because
  it is flat *and* doesn't vary in level — scan.py:1069 admits this and then
  gates on exactly that.
  *Fix:* terminal branch → `carrier`. Neither is in `CARRYING`, but `noise` is
  the label users filter out first.

- [x] DONE — **V — `burst` is a dead-end verdict.** Added to rescue pagers/TETRA/TPMS
  from `noise`, but it is absent from `CARRYING` (scan.py:837), the UI carrying
  filter (scan.py:1873) and the carrying count (scan.py:1882), and `_RANK`
  gives it specificity 0 (scan.py:848) so it never survives a re-check.
  *Fix:* add to `CARRYING` and `_RANK` at 1.

- [x] DONE — **V — `verify_slice` hardcodes the RTL-SDR's combs.** scan.py:964-970
  walks away from 28.8/12/27 MHz literals while `CLOCKS_HZ` exists for this.
  ~12% of slices get displaced up to 2.3 MHz, pushing channels onto the IF
  filter corner (-5.8 dB at 2.7 MHz) so they return `quiet`.
  *Fix:* `for c in CLOCKS_HZ`. Same literal in prove.py:42.

- [ ] **V — prominence guard still in BINS.** scan.py:500 `i - 12 / j + 13` is
  28 kHz on the RTL-SDR where it was validated, 17.5 kHz here. Same class of
  bug already fixed for `MIN_WIDTH_HZ`/`MAX_WIDTH_HZ`, missed at this site.
  *Fix:* `PROM_GUARD_BINS = max(4, round(28_000 / BIN_HZ))` — a faithful port,
  not a new tuning.

- [x] DONE — **V — `overloaded()` reports False on a failed round trip.** rsp.py:354
  turns `get()`'s `None` timeout into "not overloading", so the gain servo
  silently loses its only real overload signal.
  *Fix:* return `None` on unknown; `Gains.adapt` leaves gain alone.

- [x] DONE (false-NEGATIVE half) — **V — whisper was gated on the verdict.**
  `judge()` skipped `quiet`/`noise`, so a wrong verdict silenced the only thing
  that could revise it. 448.0600, a repeater with people talking non-stop, sat
  as `quiet` with no transcript: presence a steady ~30 dB, but dynamics
  0.21-1.52 under a 0.70 gate left the verdict riding on a rhythm score that
  swung 5.8-22.6 against a threshold of 6.0. Now gated on level alone;
  `LISTEN_MIN_PRES` already did that job. It read `voice` within one lap.

- [~] OVERSTATED — **a false-positive `voice` can never be retired.** Read the
  code: `apply_verdicts` protects a more specific verdict only while
  `now - vpos < VERDICT_HOLD_S` (600 s), after which a less specific re-check
  DOES replace it. `apply_heard` writes onto display rows, not tracks, and is
  re-derived each pass from `heard_recently`, which expires with
  `WHISPER_HOLD_S`. So both overrides are bounded and neither is permanent.
  406.125 persists because the classifier keeps genuinely answering `voice`
  every time it is asked — that is the rhythm defect in P1, not a stuck
  verdict. No change made; do not "fix" this without new evidence.

---

## P4 — low value, or blocked on evidence

- [ ] **V — dense rasters merge.** Two active channels 12.5 kHz apart report as
  one hit at the midpoint. At 200 kHz spacing the CFAR annulus (fixed at
  168-192 kHz, scan.py:384) sits on the neighbours: 2/9 carriers found at
  200 kHz vs 8/9 at 400 kHz. *Only worth it if dense FM matters to you.*
- [ ] **V — signals on for <15% of the 16 ms capture score below `SCORE_MIN`.**
  Genuinely narrow class; 900 MHz FHSS hop dwells. Not worth a change alone.
- [ ] **V — `spurs_rsp.json` cannot be produced.** The procedure at scan.py:152
  does not exist — nothing in the tree writes the file. *Fix:* a `--dump-spurs`
  flag, plus a docs correction.
- [ ] **V — RTL path only: `SPUR_TOL_HZ = 20_000` blanks 10.9 MHz** across 273
  harmonics including 144.000, 460.800, 852.000. Not active on RSP.
- [ ] **S — the gain ladder is calibrated at one frequency.** `GAIN_START =
  state 2` comes from a single NOAA 162.55 measurement and is applied 0.5 MHz
  to 2 GHz, but RSP1B LNA states are **band-dependent**. Also **S** — one
  strong emitter drives a whole 5.1 MHz step down the ladder, and the RSP step
  is 2.7x wider than the RTL step this was tuned on. *Check before changing:
  LNA sweep on one known channel per decade.*
- [ ] **S — `r.set_gain(GAIN_LADDER[-2])` at scan.py:2449** discards the
  per-step adapted gain for every verify capture.
- [ ] **S — band mode ring lag.** `read()` takes the OLDEST bytes
  (rsp.py:438) contradicting the reader docstring. Harmless in sweep
  (`flush()` empties first); in band mode timestamps could drift up to the 4 s
  ring depth with no indication. *Fix:* log when depth exceeds ~1 s.
- [ ] **S — SDRconnect's own IF AGC is invisible.** If it moves gain between
  steps, `level_db()` subtracts ours but not theirs, so WIDE measures their AGC.
  docs/research.md:496 records this failure once already on the RTL path.

### Do not chase

- **406.125 false-positive voice.** Rhythm shape does **not** separate beacons
  from speech: 406.125 scores crest 7.1 / 2nd-harmonic 8.5 / autocorr 0.25,
  while ear-confirmed voice at 145.2250 scores 5.6 / 6.2 / **0.93** — *more*
  periodic than the false positive. Confirms scan.py:556-562. The defensible
  change is to stop calling the rhythm-only branch (scan.py:1085) `voice` at
  all and let whisper supply the name; `syllabic > 6.0` is a clean **carrying**
  detector (8/8 on `data`, 0/18 on `none`) but says nothing about *voice*.
- **161.6875.** Its clip is `demod: "fm"`, rhythm 0.15, and the human labelled
  it **"none"** — machine and ear agree. Re-grab a clip while it is known to be
  talking before touching anything.

### Ground-truth warning

`clips/labels.json` has a 3-way vocabulary — `data`(8) / `none`(18) /
`unsure`(39). **It cannot arbitrate voice vs digital vs data.** 406.125 is not
in it at all. 39 batch-1 clips predate commit `4eab3e5`, which rewrote dynamics
to busiest-second, so their journalled `dyn` is not comparable to today's gate.
Any P1 classification fix needs new labelled data first.

---

## Confirmed correct — do not touch

- **The 14-bit range is not thrown away.** `int16 → float32/32768 → complex64 →
  FFT` costs **0.00 dB** vs a full float64 path; the measured slice floor tracks
  bit depth exactly (12 dB per 2 bits).
- **The Hann window is adequate for 14 bits.** −137.2 dBc at the 169 kHz CFAR
  reference distance against a ~−122 dBc floor. Do not swap for Blackman-Harris.
- **Ring and lock mechanics.** No race between `flush()`, `read()` and the
  reader — all three take `_lock`, and `flush()` sleeps out `SETTLE_S` *before*
  clearing. `bytearray + del` at the front is O(1).
- **The burst detector at the new geometry.** 0.00 false pulses per capture at
  both 48x1024/2.4 Msps and 24x4096/6 Msps.
- **`MIN_WIDTH_HZ` does not block narrow signals.** CW −22 dB, SSB −24 dB,
  NXDN −26 dB, NBFM −26 dB minimum detectable.
- **`Schedule` starves nothing.** `mark()` grants hot instantly on any hit;
  `COLD_EVERY` delays first contact, never locks out.
- **`SETTLE_S = 0.08`, `RATE = 6 Msps`, `USABLE = 0.85`, the ladder direction
  reversal, width limits in Hz, `CFAR_REF = 32`, sweep `span` geometry.**
