# Research rounds — agent findings, nothing implemented

Bar for "worth it": the last real find was prove.channelize() recomputing a
2.88M-point FFT once per channel instead of once per capture — 23x, output
bit-identical. Micro-optimisation does not count.

## Round 1 (10:37)

### Prior art: patents, SIGINT, non-English, radio-astronomy RFI
1. **Occupied bandwidth measured BEFORE classification** (ITU-R SM.443-4 /
   SM.1600-3). The standards prescribe measuring the externals — centre
   frequency, occupied bandwidth, burst timing — and only then looking at
   waveform internals. We do the reverse. OBW is ~8 lines on the PSD metrics()
   already computes (cumsum, take the span holding the central 99% of power).
   A hard physical constraint that would gate classify() before any threshold
   fires: 2.5 kHz cannot be P25, 16 kHz cannot be a bare carrier. Would also
   settle the 50-54 MHz cluster, whose open question is literally a bandwidth
   question we have no number for.
2. **Generalized spectral kurtosis** (Nita & Gary): SK = [(M+1)/(M-1)]
   (M*S2/S1^2 - 1) over the per-window FFT array metrics() already has. Null
   distribution known — expected 1 for Gaussian noise, variance 4/M — so the
   threshold is k*sqrt(4/M) rather than a tuned constant. Weak on ~50%-duty
   signals, so it supplements syllabic() rather than replacing it.
3. **Coincidence + frequency-stability across sweeps** (Breakthrough Listen
   principle, different mechanism). Channels whose onset/offset coincide
   within one lap are ONE event, not N — exactly the 31-phantom-channel
   splatter case, currently unguarded. And near-zero peak-frequency variance
   across many sweeps fingerprints a local clock harmonic, which would retire
   the hardcoded 12/27/28.8 MHz comb list that NOTES.md admits must be
   re-derived at every location.

Where we look naive: every threshold is a fixed constant, while the patent
literature universally sets thresholds relative to a per-capture noise floor
(median + k*MAD). Across a sweep with per-channel SNR varying by 40 dB, that
may be worth more than any new feature. Also: every feature we compute lives
downstream of demodulation; we compute nothing on the raw complex IQ.

Correctly rejected: cyclostationary FAM/SCF, OFDM cyclic-prefix correlation,
Taylor-tree deDoppler, CASA rflag, emitter fingerprinting, CNN-based AMC.

### What the system is blind to (synthetic signals, verdicts measured)
Agent findings, the two biggest INDEPENDENTLY REPRODUCED by me afterwards.
(My first attempt disagreed because I put the test tone on DC, which is
notched — the harness was wrong, not the code. Corrected below.)

1. **Burst wall (~100 ms).** metrics() cuts the capture into 85 ms windows,
   marks one active only if pres > 6 dB, and bails if fewer than 2 are active;
   classify then wants pres95 >= 8. Measured on a strong synthetic burst in a
   1.2 s capture: 20 ms -> quiet, 85 ms -> noise, 170/250/340/500 ms -> voice.
   Anything under ~a tenth of a second is diluted below the floor and reported
   quiet FOREVER, even though analyse() detects it fine (a 14 ms synthetic
   TETRA burst produced 18 hits). Covers TETRA, pager preambles, TPMS, key
   fobs, wM-Bus, LoRa, burst FSK telemetry. bursts() only spans 10-400 us, so
   it rescues ADS-B and nothing else.

2. **Dense occupancy is invisible.** Contiguous hot bins merge into ONE group
   with one peak, and MAX_WIDTH_BINS=128 (~300 kHz) drops anything wider.
   Measured, adjacent 12.5 kHz channels -> hits:
       1 -> 1    4 -> 1    10 -> 1    30 -> 0    60 -> 0
   A busy trunked site reports as one channel; a dense land-mobile block, FM
   broadcast, cellular downlink report as NOTHING. Density, not weakness, is
   the killer — a 10.9 dB single channel still scores 0.78.

3. **Anything wider than ~24 kHz degrades to quiet/noise.** presence compares
   the middle eighth of the 48 kHz extraction against its edges, so a signal
   filling the channel raises both. Measured OBW -> verdict: 3k data, 6k tone,
   10-12k carrier, 16-24k noise, 30-46k quiet. Ties directly to the
   round-1 prior-art finding that we should measure occupied bandwidth FIRST.

4. **CW reads as voice** (synthetic keyed CW -> voice, syllabic 25.1). Arrived
   at from synthesis alone, and it matches exactly what the user's ear found
   on the 146.7000 morse clips the same morning. Two unrelated methods, same
   conclusion. AM airband voice is unstable (voice 1 of 3 trials, data 2).
   Weak SSB -> quiet on the presence floor.

5. **Hoppers never confirm.** TRACK_TOL 8 kHz + CONFIRM_LAPS 3 require the same
   bin three laps running. Bluetooth, ISM hoppers, per-slot TDMA never do.
   LoRa gave 0 narrow hits and 0 bursts — invisible at every stage.

6. **Schedule duty cycle.** 908 steps x 20.48 ms: a hot step is observed 0.13%
   of the time, a cold step 0.02%. One hit buys HOT_MEMORY=25 laps (~7 min)
   and confirmation needs 3 laps, so a transmission shorter than ~50 s that
   does not RECUR within 7 minutes is seen once, never confirmed, expired.
   That is the shape of real dispatch, GMRS, marine and repeater traffic.

7. **Bug, unrelated:** prove.py:445 evaluates syllabic(y, rate) on `y` from the
   last loop iteration, so every printed row after the first uses the wrong
   signal's rhythm.

### Performance archaeology — no second 23x, but three real wins
1. **max_workers=8 is past the optimum** (scan.py:1563). numpy does release
   the GIL, but classify() is ~40 SMALL numpy calls on 14x4096 arrays, so
   per-call handoff dominates and 8 workers thrash. Agent measured
   517 -> 236 ms (2.2x) going 8 -> 3. I reproduced the DIRECTION on my own
   synthetic but a smaller size: pool(8) 25 ms vs pool(4) 17 ms, 1.4x.
   Absolute numbers depend on how many channels early-exit as quiet. Both
   runs agree the optimum is 3-4 and that 8 is wrong. One integer, zero risk.

2. **publish() runs once per STEP, not once per lap** (scan.py:1639 —
   structurally confirmed by me, cost curve not independently measured). It
   walks every track, rebuilds every row, sorts and recounts tags, 908 times
   per lap, while the browser polls a few times a second. Agent's measured
   cost: 0.68 s/lap at 500 tracks, 1.10 at 2000, 2.67 at 6000, 4.65 at 12000 —
   so it degrades the longer the scanner runs. Same O(n)-per-hit class of bug
   the Tracker.BUCKET comment says was already fixed once elsewhere.

3. **The FM discriminator is computed 3x per channel** and syllabic() 4x:
   once in prove.metrics, again in classify's syllabic call, again inside
   kind_of. abs(Z) is recomputed where |Z|^2 is already in hand (argmax is
   identical on either). Agent's patched version: 26.1 -> 19.8 ms over a
   7-channel corpus, 1.33x, verdicts identical on all seven classes.
   Moderately invasive — changes kind_of/syllabic signatures.

   Stacked: pool(8)+classify 539 ms -> pool(3)+classify2 164 ms = 3.3x, about
   1.9 s/lap of radio-idle CPU at 5 slices/lap.

Clean elsewhere: the sweep step is correctly radio-bound (~1.6 ms CPU against
20.5 ms of air). spectrum() amortisation is already optimal — the FFT is now
only 18% of verify compute and classify is 82%, so polyphase channelizer work
would be optimising the wrong 18%. rtl.read's complex128 intermediates cost
~15 ms/lap, not worth the churn. Band mode already shares one spectrum().

## Round 2 (10:48)

### Threshold audit against the labelled sets
Agent found night/evalset.json (38 rows: flat/dyn/pres/syl + detector verdict
+ whisper label) is the best evaluation set in the repo — better than
stakeout/, whose wavs are demodulated audio and cannot feed metrics().

Sensitivity, verdict flips at +/-30% of each constant:
    pres < 8.0     14/66 and 6/38   <- highest leverage in the codebase
    flat < 0.80     2/66 and 5/38
    dyn >= 0.70     1/66 and 4/38
    flat < 0.90     2/66 and 4/38
    syllabic > 6.0  0/66 and 0/38   (inert to perturbation - see below)
    frac < 0.02, flat < 0.20: inert everywhere

**Agent recommended moving syllabic 6.0 -> 13.0. I tested it and it is WRONG.**
It was fitted to whisper labels, and whisper only transcribes loud clear
speech, so "speech has syllabic >= 13" is selection bias. Measured against the
11 EAR-labelled clips:
    threshold  6.0: ear-voice 4/4 caught, morse+beeps 4/4 wrongly voice
    threshold 11.0: ear-voice 2/4,        morse+beeps 1/4
    threshold 13.0: ear-voice 1/4,        morse+beeps 0/4
The real result is stronger than the agent's: ear-voice measures 6.3, 7.1,
11.5, 13.2 and morse/beeps measure 9.3, 9.3, 10.5, 11.1 — FULLY INTERLEAVED.
No threshold on syllabic separates them. It is not mis-tuned, it is the wrong
feature for that distinction. Same conclusion the ear labels reached this
morning, arrived at independently.

**Correction to an earlier claim of mine.** I justified the 8 dB presence
floor by saying every genuine signal cleared it by >10 dB. evalset contains a
whisper-confirmed SPEECH row at 7.3 dB pres, and ear-labelled real signals at
6.1-7.8 dB. The floor is discarding real signals, and it is simultaneously the
highest-leverage constant in the project. Both facts were unknown this morning.

**Scale of the problem:** ~40 decision constants exist. Only SIX can be tested
against anything on disk, and only because three JSON files happen to cache
IQ-derived metrics. The other ~34 — SNR_MIN, MIN/MAX_WIDTH_BINS, SCORE_MIN,
the _score weights, CONFIRM_LAPS, TRACK_TOL, COLD_EVERY, HOT_MEMORY, FORGET_S,
VERDICT_HOLD_S, REVERIFY_S, BAND_SNR_MIN, BURST_MIN, WIDE_MIN_DB, the 18.0 dB
and 150 Hz clock gates, the 6.0 activity floor — cannot be evaluated by any
file we have, because that needs raw IQ (detection stage) or multi-lap
timelines (scheduler stage), and we record neither. Every sweep-stage constant
traces to the first commit and has never been revised.

Implication worth acting on later: we should be RECORDING raw IQ snippets and
lap timelines, or none of the detection-side numbers can ever be validated.

### Gain, dynamic range and the spur model
Headline: **~45 dB of usable simultaneous dynamic range per 1.92 MHz slice**,
against ~72 dB the 8-bit ADC could deliver after NFFT=1024 processing gain
(+28.3 dB net). The gap is ANALOG noise, not arithmetic — so the 8-bit ADC is
not the limitation people assume it is.

Gain policy is mostly fine: adapt() is a peak servo, not a detection
maximiser, and measured weak-signal SNR is FLAT from 32.8 dB gain upward
(signal and analog noise scale together). Two real hazards:
  * **latch-down.** Hysteresis is 10.6 dB, wider than every ladder step, so it
    never oscillates — but also never climbs back while a strong resident
    signal holds peak > 0.25. A broadcast carrier can park a step at 16.6 dB
    gain permanently.
  * **clipping is measured wrong.** VERIFIED BY ME: adapt() uses
    np.percentile(np.abs(iq.real), 99.9) — I only, Q ignored entirely, and
    structurally blind to clipping below 0.1% of samples. A capture already
    clipping at 0.005% reads "hold".

**Spur model — agent's proposed fix TESTED AND REJECTED.** It claimed
tightening SPUR_TOL_HZ 20k -> 5k would cut collateral 4x and lose nothing.
Measured: exclusion goes 1.184% -> 1.049% of 24-1766 MHz, not 4x, and every
named casualty stays excluded because they land on EXACT clock multiples:
    144.000 = 5x28.8   432.000 = 15x28.8   120.000 = 10x12
    132.000 = 11x12    456.000 = 38x12     468.000 = 39x12   459.000 = 17x27
So we are permanently blind at 2m ham 144.000, 70cm 432.000, ATC 120.000 and
132.000, and business 456/459/468. Tolerance cannot fix this — it needs a
different discriminator (does the energy survive with the antenna
disconnected? does its level track the antenna?). Real problem, wrong fix.

Images are defeated by accident: the +/-100 kHz per-lap jitter moves a mirror
image up to 400 kHz while TRACK_TOL is 8 kHz, so an image can never reach
CONFIRM_LAPS=3. But IMD products (2f1-f2) are anchored in absolute frequency,
survive the jitter, and are neither guarded nor recognisable — that is the
real phantom-channel path.

44.5 dB gain is defensible in value (~0.1 dB worse than 49.6 in simulation)
but is hardcoded as a literal in band.py, evalset.py, meter.py, label.py and
stakeout.py while scan.py uses the GAIN_LADDER symbol — those five drift
silently if the ladder ever changes.

Agent's single highest-value change: replace the 99.9-percentile heuristic in
Gains.adapt() with a direct clip-fraction count on BOTH I and Q, descending
only above ~0.01% clipping, plus a forced upward re-probe every N laps. Fixes
the invisible-clipping blind spot and latch-down in one edit.

### Dwell allocation — clean bill of health, LEAVE THE SCHEDULE ALONE
(Simulation only; I did not independently verify this one.)

Channels classified per hour, 908 steps / 60 emitters / 3600 s / 8 seeds:
    current hot-cold recency  44.1   <-- best
    Poisson-rate proportional 35.4
    uniform round-robin       34.0
    Whittle index             28.9
    Thompson sampling         12.4
    oracle (knows the answer) ~59    <-- we capture 76% of the ceiling

Bandits collapse because reward is NOT stationary per arm: once a channel is
classified its value drops to zero but its HIT RATE stays 1.0, so Thompson
locks onto the 12 continuous broadcasts and finds 0 of 15 ham channels.

The current scheme is already a two-state Whittle index — belief in
{hot, cold}, relaxation time HOT_MEMORY, forced exploration floor
1/COLD_EVERY — with the pathological state (infinite dwell on a known
transmitter) removed by construction. The HOT_MEMORY x COLD_EVERY grid
(5-100 x 2-12) spans 34.8-44.1 with sigma 2.9 and 25/6 is the top cell.
Nothing to win by retuning.

20.48 ms dwell is right: the floor is set by persist() needing 48 frames, not
by PLL settling (which flush() absorbs). Longer is clearly worse (-22% at
82 ms). Shorter raises raw confirmations but not classifications, and the sim
does not even model the sensitivity loss from fewer frames.

**CORRECTS round 1.** The "seen once, never confirmed, expired" framing was
pessimistic. Confirmation is not gated by transmission length — one lucky cold
hit promotes the step to hot, and the other two hits then come nearly free.

**The actionable finding: detection is not the binding constraint,
classification is.** Sweeping VER_FRAC 0 -> 45% takes classified/hr from 0 to
44 while confirmed only drops 47.8 -> 45.4; past 45% it is flat. More
channels/hour comes from raising verify_slice's hit rate — catching a channel
while it is ACTUALLY transmitting — not from sweeping differently.

## Round 3 (11:03)

### Is the taxonomy right? — proposal: three axes, not one flat list
The core error: one string encodes three unrelated questions — what is it,
how sure am I, and why did I fail. "quiet" is the failure axis leaking into
the what axis, which is why it means five different things.

  Axis 1 WHAT:     speech | keying | machine | burst | modulated | carrier | tone
  Axis 2 HOW SURE: suffix "?" on one look, bare on two agreeing looks
  Axis 3 WHY-NOT:  too short | too weak | too wide | nothing there | not looked yet

Renames worth having regardless: "data" -> "modulated" (it is kind_of's
fallback for "cannot tell", and the current name reads as a positive ID);
"digital" -> "machine". Drop "noise" as a row — nobody clicks in to listen to
noise; make it a footer count.

**VERIFIED BY ME, and worse than the agent said.** metrics() already computes
the "signal present but too few active windows" state and discards it: it
returns frac and pres95 but NaN for flat/dyn. classify() then maps NaN
flatness to **"noise"** — so a real short burst with a strong carrier is
labelled noise, the class most likely to be filtered away and never looked at.
Matches my synthetic test exactly (85 ms burst -> noise, 20 ms -> quiet).
Splitting that one branch into "too short" needs NO new measurement — the
numbers are already in hand and thrown away. Highest-value single unmasking
on the board, and it is the burst-of-machine-data case: TETRA, pagers, TPMS,
key fobs, meter reads.

Needs new measurement (none require a decoder):
  * speech vs keying — peak concentration, ~5 lines on the P array metrics()
    already has, but n=2 evidence so far (morse 0.28-0.30, voice 0.06-0.12)
  * too wide — occupied bandwidth, ~8 lines, ties to the round-1 ITU finding
  * mid-scale burst 0.4-100 ms — bursts() only covers 10-400 us today

Note the interaction with the round-2 threshold audit: marking labels as
tentative is what makes it SAFE to drop the presence floor from 8.0 to ~5 dB,
where evalset says real speech is being discarded. Eager labels are only
acceptable once they are visibly eager.

### Long-run stability — measured on the live process (lap 295, 42 min up)
RSS flat at 368 MB over 5 min, threads flat at 12, FDs flat at 50, tracks
plateaued and oscillating (expiry works). Nothing is O(uptime); every
structure is bounded by active channels or step count. At 1 year, RSS ~368 MB.

**Direct answer to "will the lap counter break after years": no.** lap is a
Python bignum — no overflow, no float, `lap % COLD_EVERY` stays uniform
forever, `lap - last <= HOT_MEMORY` is exact. 6.8M laps in a year, nothing
degrades.

**Top finding, VERIFIED BY ME: unplugging the dongle kills the process.**
scan.py:1605 `iq = r.read(n_samp)` has no try; rtl.py raises RuntimeError; the
only handler (scan.py:1746) catches KeyboardInterrupt. Process exits with a
traceback, no reconnect logic exists anywhere, whole board lost. The user WILL
unplug it. Smallest fix: catch RuntimeError per step, and after N consecutive
failures re-run rtl.find("R828D") and rebuild Rtl every 2 s. State survives.

**Second, VERIFIED: sleep/wake makes every channel read LIVE.**
scan.py:1669 `lap_times.append(time.time() - lap_start)` is unclamped and
live_hold() uses the mean of the last 3 laps, so a 6-hour lid-close records as
one ~7200 s lap and the entire board reads LIVE for 3 laps. Same failure on an
NTP step backwards. Fix: clamp the append, or use time.monotonic() for the lap
timer. (Everything uses time.time(), never monotonic.) DST is a non-issue.

Location change: the schedule heals in HOT_MEMORY=25 laps ~ 2-6 min, fine. But
FORGET_S=3600 keeps channels from the town you left on the board for an hour,
and they keep WINNING verify_slice radio time as stale re-checks every 240 s.
spurs.json travels with the hardware so it does not need re-measuring, but it
is loaded once at startup and never reloaded.

Also: Baseline is instantiated at scan.py:1559 and never used — dead code.
Band mode's `hist` never expires anything, so the "drop after an hour" rule
FORGET_S implements for the sweep does not apply in band mode.
Disk growth: none from scan.py (only muted.json, overwritten). stdout is the
only unbounded sink and only if redirected.

### *** RAISING THE VERIFY HIT RATE — the biggest actionable finding ***

**The problem, measured.** verify_slice runs in a block AFTER the whole sweep
lap, and worth() scores slices on strongest^2 * waited * len^0.5 where
`waited` is time since last VERIFY (scan.py:1706, confirmed by me) — there is
NO term for time since last SIGHTING. Nothing connects "the sweep just saw
this channel keyed" to "capture it now".

Live: 825-891 channels in ~75 slices, full rotation ~260 s, so the gap between
seeing a channel and capturing it is uniform over ~260 s, median ~130 s.

Measured autocorrelation of channel on/off state (628 bursty channels, 695 s):
    lag  1 s -> 0.93    10 s -> 0.37    20 s -> 0.07    40 s -> -0.01
The information is worthless after ~20 s. We use it at ~130 s. **Verify timing
is currently exactly random.**

CONFIRMED ON THE LIVE BOARD BY ME: 771 of 891 rows = **87% quiet**, and the
median duty of a quiet-verdict channel is **0.098** — those channels are real,
they are just silent whenever we happen to look. (Agent measured 86% / 0.11
independently an hour earlier.)

**The fix: strike while hot.** P(channel is on during capture) vs delay since
sighting, for the 291 low-duty channels: 0.983 at 50 ms, 0.70 at 1 s, 0.50 at
2 s, 0.23 at 5 s, 0.056 random. A 17.7x lift that is gone by 10 s.

Simulated at IDENTICAL radio time (2 mid-lap strikes, VERIFY_SLICES_PER_LAP
5 -> 3), 3 h x 4 seeds:
    current            962 useful/hr,  466 bursty/hr
    strike x2, spl=3  1729 (+80%),    1114 (+139%)
    strike x3, spl=2  1874 (+95%),    1287 (+176%)
    verify-more control (REVERIFY 240->120)  1170 (+22%), 716 (+54%)
Trigger-channel hit rate under strike: 0.99 vs 0.085. Sweep cost +3% lap time,
and round 2 already showed detection has slack. Holds at L_on = 2, 5 and 15 s.

Rejected with reasons:
  * adaptive capture length — kind_of hard-returns "data" below 1.0 s, so max
    saving is 0.2 s; mean saving measured 18 ms of 1200 (1.5%). Absence can
    never be concluded early.
  * prediction from history — no per-lap timeline is stored, AND the measured
    autocorrelation is statistically zero past 20 s, so there is nothing to
    predict from. The only exploitable structure is the 0-10 s persistence,
    which is exactly what strike consumes.
  * adding a recency term to worth() alone — end-of-lap deferral has already
    burned the correlation. Measured +2-5%.
  * VERIFY_SECS 1.2 is right, for a reason not in the code: it is the smallest
    value above kind_of's 1.0 s floor. 2.0 s buys +8% for +67% radio time, and
    under strike timing it is strictly worse. The earlier 6/8-vs-5/5 accuracy
    anecdote is Fisher p ~ 0.47 — not evidence. (That was my anecdote.)
  * VERIFY_SLICES_PER_LAP=8 changes nothing; the real throttle is
    REVERIFY_IDLE_S=240.

## Round 4 (11:18)

### Second dongle — NOT WORTH IT
Payoff is ~10-15%, not 2x: round 2 showed classified/hr is flat past
VER_FRAC ~45%, and the current config is already at 43-48%. Dongle B would
push effective VER_FRAC to ~200%, but you cannot climb a flat curve. The one
real mechanism is more repeat looks per channel — 1-(1-p)^N — worth maybe
44 -> 48-50 classified/hr. Two radios and doubled failure modes for a tenth.

**Fatal blocker, VERIFIED BY ME: there is zero ppm handling anywhere.** grep
for ppm / freq_correction / set_freq in rtl.py and scan.py returns nothing.
metrics() uses mid = |f| < rate/8 = +/-6 kHz as SIGNAL and 12-22 kHz as NOISE
REFERENCE. A generic R820T is +/-20-50 ppm untrimmed; 30 ppm at 450 MHz is
13.5 kHz, which lands the signal squarely inside the noise reference window.
Presence goes negative and the entire UHF board reads "quiet" — and it would
look like a weak-signal problem, not a calibration one.
Current setup is safe: the V4's 1 ppm TCXO gives 1.77 kHz at 1766 MHz, well
inside the +/-6 kHz window.

Noise floor is NOT the main objection: R820T vs R828D inherent NF differs only
~1-3 dB; the real gap is shielding and IMD, ~3-10 dB effective. But evalset
has real signals at 6.1-7.8 dB presence, so expect 20-35% of currently
classified channels to drop out on B — concentrated on exactly the marginal
ones the pending presence-floor reduction is meant to rescue.
USB is a non-issue: 2 x 4.8 MB/s = ~20% of USB 2.0 practical bulk ceiling.
Spurs are dongle-specific and verify_slice never calls is_spur anyway.

Better uses for dongle B, ranked:
  (a) permanent STAKEOUT on one channel — no shared state, no ppm sensitivity
      if calibrated once, and it dogfoods the burst/duty blind spots
  (b) IQ RECORDER — makes the ~34 untestable constants from round 2 testable.
      Highest research value available.
  (c) SPUR DISCRIMINATOR — same antenna, disconnected, to kill the
      144.000/432.000/120.000 permanent blindness found in round 2
  (d) parallel second band — needs a concurrency model AND ppm
  (e) verify offload — last

### The board — what it shows vs what the system knows
MISLEADING (all four VERIFIED BY ME on the live board, 905 rows):
1. **The SNR column mixes units.** scan.py:1621 sets "snr": float(nb) for
   BURST hits, where nb is a PULSE COUNT, not dB. Narrow hits are dB over the
   local median; WIDE hits are dB over neighbouring steps. Three incompatible
   quantities, one column, one bar scale. Sorting by strength partly sorts by
   pulse count.
2. **SNR is LATEST, not peak** (Tracker.update does m["snr"] = h["snr"] while
   score uses max). Band mode uses max. So the same channel reads a different
   SNR before and after you click into it, and a bursty channel shows whatever
   the last lap caught mid-gap. Peak is never retained anywhere.
3. **The verdict badge has no age.** 48 of 85 carrying rows are not live;
   oldest still-badged voice is 447.3875 at **2655 s = 44 minutes**, visually
   identical to one confirmed 5 s ago. Note this EXCEEDS VERDICT_HOLD_S=600 —
   the hold governs overwriting, not display, so a verdict shows indefinitely
   if the channel is never re-verified. Most likely single cause of the user
   listening to a dead channel.
4. **228 of 905 rows are LIVE and "quiet" at the same time** — green for "in
   use" while the column says nothing is there. Failure-to-measure dressed as
   a measurement.
5. duty/pattern measure the SCHEDULER, not the air: duty = laps/(lap span) but
   cold steps are visited 1 lap in 6. "bursty" is 770/889 — effectively a
   synonym for "heard a while ago". Correctly not displayed; must not be.

HIDDEN BUT KNOWN (would change the next click):
  * Tracker holds laps/first/last/first_lap/last_lap and publish() emits NONE.
    Band mode already shows count/airtime/longest — the "40 x 2 s vs 1 x 80 s"
    distinction — but only AFTER the user commits to clicking in.
  * Repeater vs simplex is computable today with no new measurement: the live
    board contains 4 distinct 2 m +/-600 kHz pairs (146.6125/147.2125,
    147.2375/147.8375, 147.725/148.325, 144.075/144.675) and nothing flags them.
  * No confidence field exists at all — no look count, no agreement count. This
    is the prerequisite for the round-3 "voice?" tier and therefore for safely
    dropping the presence floor.
  * WHY a verdict happened is discarded at classify()'s return: frac, wander,
    flat, dyn, pres, syllabic all thrown away. "carrier" cannot be told from
    "I failed".
  * Sort is frequency-ascending and not re-sortable; column menus only hide.

DEAD WEIGHT: band, width, score, duty, pattern, tracks, live_for, updated are
all published and never rendered (~35% of payload). The shape column is blank
for 857 of 889 rows.

POLLING: 176 KB per poll (VERIFIED), every 1.2 s = 144 KB/s. At 5000 rows that
is ~970 KB/poll. Rows measured byte-identical between consecutive polls
(888/888). Fixes in order: gzip (177 KB -> 14 KB, 12.6x, no Content-Encoding
is sent today), then ETag/304, then derive `age` client-side from one `now`.
Also confirms the round-1 finding: publish() runs ~14x/s, so >90% of the
rebuild+sort+recount work is discarded unseen.

### Professional RF critique — frequency accuracy and measurement hygiene

**Drift does NOT break tracking, and that is worth stating plainly.** A static
offset shifts the track and the new hit identically, so TRACK_TOL never
notices. Splitting a track would need 8 kHz of drift BETWEEN consecutive
visits of one step (~84 s for a cold step) = 3.2 ppm/min; warm-up peaks at
1-2 ppm/min uncompensated and ~0.05 on the V4. So no split, no lost
confirmation. What IS damaged: the reported frequency itself (up to 53 kHz off
at L-band on a generic dongle, printed to four decimals of false precision),
SNAP_HZ snapping to the WRONG slot above ~625 MHz at 10 ppm, and label_for
picking the wrong allocation near a boundary.

Note: the 28.8 MHz comb CANNOT self-calibrate — spur and LO scale by the same
epsilon, so a crystal harmonic always lands at exactly nominal in reported
coordinates. The radiated 12/27 MHz combs are external references and DO walk.

**Self-calibration, concrete and cheap:** the sweep already visits
162.400-162.550 every lap and NOAA runs continuously on exact channels. Every
~20 laps take one 1.2 s capture of the strongest NOAA channel and take the
MEDIAN of the FM discriminator output — that median IS the residual carrier
offset in Hz. Divide by 162.55e6 for ppm, store in cal.json, apply as
hz/(1+ppm*1e-6) inside rtl.tune (NOT rtlsdr_set_freq_correction, which takes
integer ppm and is too coarse). Accuracy ~0.15-0.3 ppm single-shot, ~0.1 ppm
over 10 measurements = 180 Hz at 1766 MHz, sub-bin. Cost ~0.06 s/lap, and
repeating it absorbs warm-up and temperature for free.

Ranked flags, VERIFIED BY ME where marked:
1. **The WIDE test compares steps at DIFFERENT GAINS.** VERIFIED: level_db
   (scan.py:355) is raw ADC power with no gain compensation, while every step
   has its own ladder entry spanning 16.6-49.6 dB. WIDE_MIN_DB is 4 dB and one
   ladder step is 4-9 dB, so WIDE is largely detecting which step latched a
   higher gain, not occupancy. Also the +/-6 neighbours span up to 23 MHz over
   which antenna/tuner response genuinely varies ~10 dB, and lap_levels only
   holds steps visited THIS lap, so the baseline is non-stationary. One-line
   fix: subtract gains.for_step(key). Until then treat every WIDE row as
   unverified.
2. **The burst pulse count is SQUARED to allocate radio time.** VERIFIED:
   worth() (scan.py:1707) does strongest**2, and burst hits put a pulse COUNT
   in the snr field. A 40-pulse burst scores 1600 against a real 30 dB
   carrier's 900 — it outranks it 1.78x for verify time. Compounds the SNR
   unit bug from the board audit.
3. **"Dynamics" is an extreme-value statistic** (max over overlapping chunks)
   compared to a FIXED threshold, so dyn >= 0.70 is not the same test in
   prove.py (SECS=10) as in the sweep (1.2 s) as in band mode (concatenated
   non-contiguous audio). prove.py systematically says DATA more often than
   the board on the same signal — and prove.py is the tool the user reaches
   for to adjudicate a disagreement.
4. Noise floor is ONE scalar over 1.92 MHz. Median is the right estimator and
   median-of-dB == dB-of-median, so that part is clean — but the IF response
   is not flat, so the same signal's SNR changes several dB depending on where
   the +/-100 kHz jitter drops it. A running median over ~200 bins is 3 lines.
5. `width` is not a bandwidth: it is the count of bins over threshold,
   convolved with the Hann mainlobe and measured on the skirts, so a STRONGER
   signal measures WIDER. An SNR proxy displayed as kHz. It cannot serve as
   the occupied-bandwidth gate round 1 wants.
6. Burst and WIDE frequencies are quantised to the 1.92 MHz grid, so ADS-B is
   reported at the nearest multiple of 1.92 MHz to 1090, printed to four
   decimals, and labelled from that fake number.
7. publish() silently drops co-slot channels (scan.py:924) — merged[key] keeps
   only the newest track per 12.5 kHz slot, with no counter. The 12.5 kHz grid
   is also wrong for airband (25/8.33), marine (25) and LTE (100 kHz raster).
8. Doppler: a LEO pass is +/-3.5 kHz at 137 MHz (survives TRACK_TOL) but
   +/-38 kHz at 1610 MHz, so Iridium and the 1.5-1.7 GHz satellite labels can
   never reach CONFIRM_LAPS.

**Explicitly cleared as sloppy-but-harmless:** no ENBW/coherent-gain window
correction (every decision is a dB DIFFERENCE within one spectrum, so it
cancels exactly); frame averaging is done in the LINEAR domain before the log
in both scan.py and prove.py, so the classic dB-averaging error is NOT
present; per-bin false alarms are ~1e-11 with 48-frame averaging; and the
total absence of dBm does not matter for "is anyone using this" — except for
item 1, where a bare gain-table subtraction buys most of what absolute
calibration would.

---

## Note on what is in the code and what is not

`strike while hot` was implemented, measured twice against a no-strike
baseline, beat it neither time, and has been REMOVED from the source rather
than left disabled behind a constant. The finding above stands and the
measurement is recorded here; the code was unreachable and would only have
misled a reader. Re-implementing it means re-reading this section, which is
the right amount of friction for an idea that did not work.

Also removed at publication: `band.py` and `watch.py`. Both carried their own
older copies of the classification tree, which is exactly the drift that caused
two separate bugs — the sweep and band mode disagreeing about the same channel,
and a verdict that could never be upgraded. `scan.py` does both jobs from one
`classify()`.
