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
