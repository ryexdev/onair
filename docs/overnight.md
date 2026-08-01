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

- **cycle 5 — `peak` scored honestly: 64% against a 55% floor.**

  184 rows, 71 pass the live gate (42 voice, 29 digital). This time the
  threshold was fitted with LEAVE-ONE-OUT, so it never sees the row it is
  scoring — which is what the previous cycles' 92% and 69% were missing.

        peak, leave-one-out   64%
        floor                 55%

  A 9-point edge over guessing. Real, but small, and nowhere near the 76% the
  rhythm test already manages or the 90% flatness reached on clips before it
  failed in the field.

  So the surface work has produced exactly one durable thing: `is_really_live()`.
  Everything else — width, above-floor, peak — is at or near chance once
  measured without fitting the threshold to the answer.

  This is worth saying plainly for the next session: the 3D surfaces ARE
  readable by eye, and four separate attempts to reduce them to a number have
  now failed. The next attempt should not be a fifth scalar. Either compare
  whole surfaces to each other, or accept that the eye is using something we
  have not identified and go back to looking rather than measuring.

- **cycle 6 — all four scorings, honestly, and the verdict is settled.**

  193 rows, 76 live (47 voice, 29 digital). Everything leave-one-out:

        peak                66%
        width               56%
        above               57%
        all three together  63%
        floor               57%

  `width` and `above` are exactly the floor — they contribute nothing at all.
  Combining all three is WORSE than `peak` alone, which is what happens when
  two noise features dilute one weak one. `peak` holds a 9-point edge across
  two cycles now (64% then 66%), so it is real, but 66% is not usable: the
  rhythm test already in the code manages 76%.

  ## Conclusion of the surface run

  Six cycles, ~200 measurements, four scalars. Result:

    KEEP    `is_really_live()` — above >= 8 dB AND peak < 8 kHz. It rejected
            every noise sample tested and 22 of 30 bogus "voice" references.
            This is a genuine contamination filter and it should guard any
            future dataset.

    DROP    peak width, height above floor, and peak frequency as classifiers.
            All at or barely above chance when scored without fitting the
            threshold to the answer.

  The buckets3d.png surfaces really are readable by eye — the three shapes are
  obvious and the picture caught two board mislabels instantly. Five separate
  attempts to turn that into one number have now failed (rhythm, flatness,
  dynamics, bandwidth, and these three). The pattern is consistent enough to
  be the finding itself: the difference is not a scalar.

  Next attempt should compare whole surfaces to each other rather than
  summarise them, or stop measuring and go back to looking.

- **cycle 7 — THE ANSWER. Compare whole spectra, do not summarise them: 93%.**

  Cycle 6 concluded the difference is not a scalar and the next attempt should
  compare whole surfaces. Doing exactly that:

        method: nearest NEIGHBOUR over the full-resolution demodulated
                spectrum, 228 bins, 200 Hz to rate*0.45, DC excluded,
                mean-removed and unit-normalised. No binning, no averaging
                into templates, no thresholds.

        426 clips over 132 distinct channels (121 voice, 304 digital)

        leave-one-CHANNEL-out   93%     <- never compared to its own channel
        leave-one-out (leaky)   94%
        floor                   71%
        rhythm test in the code 76%

  The channel-held-out number is the one that matters: 93% against 71%, and it
  is only 1 point below the leaky version, so it is NOT recognising individual
  channels. It generalises to channels it has never seen.

  ## Why this works where five scalars failed

  Every previous attempt collapsed the spectrum to one number, which throws
  away the shape. The 24-band centroid fingerprint scored 76% for two reasons,
  both fixed here:

    * binning into 24 wide bands smoothed away the fine structure
    * a centroid averages ALL digital types into one blob, but P25, FLEX and a
      stuck carrier have genuinely different shapes. Nearest NEIGHBOUR lets
      each keep its own, which is the "more than 3 buckets" point made
      repeatedly by the operator and confirmed by clustering at cycle 0.

  ## Before anyone ships this

  Not implemented — this run is data collection only, and the flatness change
  that was shipped and reverted the same night is the reason for that rule.

  Open questions for the next session:

    1. Cost. 426 stored spectra x 228 bins is one matrix multiply per channel;
       needs timing against the sweep budget, though it should be trivial.
    2. The labels are still whisper-for-voice and clock-for-digital, both
       biased toward clear-cut cases. 93% on easy cases may be less in the wild.
    3. It needs the live gate in front of it. Untested on noise, which is a
       third class this two-way test never sees.
    4. Test it against the operator's ear on 506.4125 and 152.2100 — the two
       channels every previous attempt got wrong. That is the real exam.

- **cycle 8 — the 93% FAILS the only test with human ground truth.**

  Held out the whole channel and asked the cycle-7 method about the two
  channels the operator identified by ear as a data stream ("an idling diesel,
  electronic, fast"):

        506.4125   ->  VOICE     all 5 nearest neighbours voice, sim 0.965
        506.9875   ->  DIGITAL   sim 0.175, i.e. no real match at all

  506.4125 is wrong, unanimously and with high confidence. Its nearest match at
  0.965 is 470.7 MHz — another T-band channel carrying a `voice` label from
  whisper.

  ## What this means

  The library is contaminated. Whisper sometimes returns words on T-band
  digital — a brief analog moment on a mixed system, or an outright
  hallucination — so digital channels sit in the library labelled voice.
  506.4125 then matches them *correctly*. The method is doing its job; the
  labels are wrong.

  **So the 93% is agreement with a contaminated label set, not accuracy.** It
  cannot be trusted, and neither can any number computed from `ear.json` as it
  stands. That includes several earlier cycles.

  Note also that `ear.json` is keyed by frequency, so a later harvest silently
  OVERWRITES an earlier label for the same channel. The operator's own ear
  labels for 506.4125 and 152.2100 were lost that way — they had to be
  recovered from a separate recording to run this test at all. Human labels
  must never be overwritable by automatic ones.

  ## What the next session should do first

    1. Make `by: ear` labels immutable — automatic harvests must not overwrite
       them, and ideally each capture gets its own row rather than one row per
       frequency.
    2. Audit the whisper-labelled T-band rows specifically. If whisper is
       transcribing P25, every whisper label on a trunked band is suspect.
    3. Re-score cycle 7 only against ear labels and clock labels, dropping
       whisper for anything in 470-512, and see what survives.

  The one thing still standing after eight cycles remains `is_really_live()`.

- **cycle 9 — dropping the suspect labels does NOT rescue the 93%.**

  Removed all 41 whisper labels inside 470-512 (the T-band, where whisper is
  demonstrably transcribing digital) and re-scored:

        all labels                  450 clips   93%   floor 68%
        whisper dropped in T-band   409 clips   94%   floor 75%

  The score barely moves, but the FLOOR rises 68 -> 75, so the real edge shrank
  from 25 points to 19. And the cycle-8 exam failure stands regardless: the
  contamination is not confined to the band I could identify.

  Fixed two data-integrity bugs in the harvester, both mine:

    * human `by: ear` labels can no longer be overwritten by an automatic one
    * the label file is now written temp-then-rename; two writers racing on
      `open(p,"w")` had already cut it from 119 entries to 13 once

  ## Standing back after nine cycles

  Nothing here is safe to build on yet, and the reason is not the features —
  it is the labels. Every accuracy number tonight was computed against a set
  where an unknown fraction of "voice" is actually digital. The 93% may be
  excellent or may be worthless and there is currently no way to tell.

  The bottleneck is no longer measurement. It is ground truth. Twenty channels
  labelled by ear would settle more than another thousand auto-labelled clips.

- **cycle 10 — the whisper labels are probably RIGHT, which reframes cycle 8.**

  Looked at what whisper actually transcribed on the 51 suspect trunked-band
  channels:

        470.7118  "copy so far"
        482.9125  "10-4, thank you"
        482.9875  "Was there a second unit to start?"
        483.1376  "1-12, family to sh-"
        472.8392  "I'm gonna read code right now."

  That is real dispatch traffic, not hallucination. So T-band carries a great
  deal of genuine ANALOG voice alongside its P25, and those whisper labels are
  most likely correct. The "contaminated library" conclusion from cycle 8 was
  too quick.

  Which leaves a better explanation for the exam failure: **506.4125 is a
  trunked channel that carries voice at some moments and data at others.** The
  operator heard diesel on it; the scanner recorded voice on it minutes later;
  both are true. Its nearest neighbour at 0.965 was 470.7118, a channel with a
  genuine dispatch transcript — so the match was correct for that capture.

  ## The real lesson

  A per-CHANNEL label is meaningless on a trunked system. `ear.json` is keyed
  by frequency and stores one class per channel, which cannot represent
  "carries both". Every accuracy number tonight inherits that flaw.

  Labels must be per-CAPTURE, tied to the clip, not to the frequency.

  ## For the morning: a listening set

  Eight clips written to `~/Desktop/onair_listen/` with `index.json`:

        506.4125  LIVE   scanner says voice   <- operator's ear said diesel
        152.2100  LIVE   scanner says voice   <- operator's ear said diesel
        507.3125  LIVE   scanner says voice   <- P25 clock measured at 4804 Hz
        470.7118  idle   (whisper heard "copy so far" earlier)
        472.1128  idle
        482.8625  idle
        482.9875  idle
        483.1375  idle

  The three LIVE ones are the ones worth an ear. If 506.4125 and 152.2100 sound
  like voice right now and diesel at other times, that confirms the per-capture
  point and closes out the confusion of the last three cycles.

- **cycle 11 — per-CAPTURE labels: 94% on a BALANCED set, floor 51%.**

  Cycle 10 concluded labels must be per-capture, not per-channel. Every clip
  file carries its own label in its filename, so the per-capture set was
  rebuilt from disk rather than from `ear.json`:

        869 clips, 144 channels, 439 voice / 430 digital  (balanced)

        channel-held-out nearest neighbour   94%
        floor                                51%

  A 43-point edge over chance, on a balanced set, never comparing a clip to
  another clip from its own channel. The earlier 93% was against a 71% floor
  on a set half the size that could not represent a channel carrying both.

  **And the trunked hypothesis is confirmed directly: 11 channels are labelled
  BOTH ways at different moments** — 147.2, 471.4, 482.8, 482.9, 483.2, 483.3,
  483.5, 484.0 and others, almost all T-band. Those channels genuinely carry
  voice sometimes and data other times, exactly as the operator's ear and the
  scanner's readings both indicated. Neither was wrong.

  That closes out cycles 8-10. There was no contamination; there was a data
  model that could not express the truth.

  ## Where this leaves the project

  The approach that works: compare the whole demodulated spectrum at full
  resolution against a library of labelled captures, nearest neighbour, and
  label per capture rather than per channel. 94% against a 51% floor, versus
  76% for the rhythm test currently shipping.

  Still NOT implemented, deliberately. Before it ships:

    1. Time it inside the sweep budget — 869 x 228 floats is one matrix
       multiply, but measure it.
    2. Put `is_really_live()` in front. Noise is a third class this two-way
       test has never been shown.
    3. Decide how the library is stored, capped and aged — it grows forever
       as written.
    4. Have the operator listen to `~/Desktop/onair_listen/` and confirm the
       per-capture story on 506.4125 and 152.2100.
