# Overnight surface-measurement collection

Turning the `buckets3d.png` insight into numbers. See `docs/shapes.md` for why
every single-capture feature failed and why this is measured across laps.

What is being collected, automatically, on every channel the scanner already
confirms by an independent means:

  * `above`  how far the peak stands over the surrounding floor (dB)
  * `width`  the -3 dB width of that peak (Hz)  <- the candidate
  * `peak`   where the peak sits (Hz)
  * `live`   above >= 8 dB AND peak < 8 kHz, i.e. not hiss wearing a label

Labels come from whisper returning real words (voice) or a symbol clock over
24 dB (digital). Neither looks at the features being tested.

## The claim under test

One measurement, verified-live channels only:

    NOAA voice    above 21.5 dB   width 562 Hz
    diesel        above 37.5      width 188
    P25           above 11.8      width 188
    T 482.9000    above 47.5      width  94
    carrier       above 20.5      width  94

Voice 3-6x WIDER than every machine, no overlap. **One voice sample.** That is
the entire reason for this run.

## Log

- cycle 1 — collection started, scanner up, 0 rows so far

- **cycle 2 — the width claim is DEAD, and the first measurement was an artifact.**

  Two things found:

  1. The first two rows collected both read `peak: 0 Hz, width: 94`. The FM
     discriminator carries a DC offset that towers over everything, so
     `surface_stats` was reporting the width of the DC spike on every channel.
     `clock()` has skipped below 200 Hz all along for exactly this reason.
     Fixed: the measurement now ignores below 200 Hz.

  2. With DC excluded, 90 fresh reference rows over 10 laps:

         voice     live  8/30   width  94..281   median 234
         digital   live 22/40   width  94..375   median 234
         carrier   live  6/10   width 281..469   median 281
         noise     live  0/10   nothing passed the live gate

     Voice and digital have the SAME median and overlapping ranges. The split
     that motivated this whole run — voice 562 Hz against machines 94-188 Hz —
     was the DC artifact. It does not exist.

  What DID survive: `is_really_live()` rejected all 10 noise samples and 22 of
  30 "voice" attempts, which is the contamination filter working exactly as
  intended. Keep it. That is the only piece of tonight's surface work that has
  earned its place.

  Still collecting, because a null result on 8 live voice samples is not proof
  either. But do not build anything on peak width.

  Also noted: whisper ran 31 times this run and returned no words at all
  (gated 5196), so the automatic voice side is contributing almost nothing.
  The voice rows above came from direct capture, not from the harvester.

- **cycle 3 — harvesters alive, and all three surface numbers are near chance.**

  The automatic collection is working now: 52 rows arrived unattended since the
  last cycle (21 voice via whisper, 31 digital via clock). 144 total.

  On the 48 rows that pass the live gate — 20 voice, 28 digital:

        width   voice med  281 [ 94.. 562]   digital med  281 [ 94.. 375]   60%
        above   voice med   19 [  8..  46]   digital med   18 [  8..  36]   69%
        peak    voice med  516 [281..2625]   digital med 1406 [  0..7969]   65%

  Always guessing "digital" scores 58%. So width at 60% is nothing at all,
  and above/peak at 65-69% are barely above the floor. All three surface
  measurements are effectively dead on this data.

  `peak` is the least bad — voice concentrates near 516 Hz, digital spreads to
  8 kHz — but a 7-point edge over guessing is not a feature.

  Honest position after three cycles: the 3D surfaces are genuinely readable by
  eye, but every scalar taken off them so far behaves like noise. Either the
  information is in something these three do not capture, or the sample is
  still too small at 20 voice. Keep collecting; do not build.

- **cycle 4 — `peak` is the only number showing anything, and the sample is
  badly imbalanced.**

  175 rows. Restricting to HARVESTED live rows only (no hand-picked reference
  captures, which bias toward channels I chose): 31 voice, 7 digital.

        width  voice 375  digital 281   84%
        above  voice  14  digital  11   82%
        peak   voice 562  digital 4781  92%
        floor (always guess voice)      82%

  Voice concentrates its peak near 560 Hz; digital sits up around 4.8 kHz. On
  harvested rows that is a 10-point edge over guessing.

  BUT: 7 digital rows, and the class balance flipped from the last cycle
  (28 digital / 20 voice then, 7 / 31 now), which by itself moves the floor
  from 58% to 82%. On ALL live rows including the direct captures, the same
  `peak` measure scored 65%. Two very different answers from the same feature
  depending on which rows are included is the signature of a sample too small
  to mean anything.

  Not a result. Recorded because `peak` is the only one of the three that has
  ever looked non-random, and it is worth re-checking when digital rows
  accumulate.
