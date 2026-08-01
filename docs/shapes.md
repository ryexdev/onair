# The real finding: a machine draws a ridge, a person draws hills

2026-08-01. This supersedes every single-number attempt at telling voice from
data. Read this before touching the classifier again.

## What was tried and failed

All scored against the same labelled clips. All are summaries of ONE capture:

| feature | accuracy | why it fails |
|---|---|---|
| rhythm (`syllabic`), what ships today | 76% | a data frame repeating a few times a second IS a 3-6 Hz rhythm. Voice -1.8..21.0, digital -1.6..22.6 — the ranges are the same. It calls HALF of all digital "voice". |
| flatness | 90% on clips, **wrong in the field** | shipped and reverted the same night: 506.4125, confirmed by ear as a data stream, measures flatness 0.087 — *more* voice-like than real voice |
| dynamics | 66% | separates voice from an idle carrier well, but not from digital |
| energy above 3.5 kHz | 83% | dominated by FM hiss in the silence |
| 24-band shape fingerprint, nearest template | 76% | binning into wide bands smooths away the fine peakiness that matters |
| shape + all three | 86% | nearest-centroid dilutes the one good axis with weak ones |

Every one of these collapses the time axis. That is why they all plateau
around 76-90% and none survives contact with a real channel.

## What actually separates them

Take 8 sweep samples of the same channel, one per lap, and stitch them.
Plot frequency x time x power. Three shapes appear, and they are unmistakable:

    RIDGE running along time   -> a machine (any machine)
    HILLS that move            -> a person
    FLAT ROUGH PLATEAU         -> nothing

Measured across 8 real laps, "how similar is each sample's spectrum to the
others" (1.0 = never changes):

    506.4125  diesel, by ear     0.639     <- machine
    134.1000  carrier            0.400
    162.5500  NOAA, voice        0.317     <- person
    1250      empty band        -0.008     <- nothing

Correct ordering, clean gaps. This is the axis to build on.

It also explains the board's mistakes at a glance. 482.9000 was labelled
`digital`; its surface is IDENTICAL to the empty band, so it is noise.
483.2725 was labelled `quiet`; it shows a ridge for four laps and then falls
off a cliff, so it was transmitting and stopped.

## The trap that nearly fooled me

Running the same "stays put" measure over the 272 labelled clips gives the
OPPOSITE answer — voice 0.348, digital 0.056, an apparently respectable 78%
split. It is meaningless. Those clips are single 1.2 s recordings, so chunks
16 ms apart sit inside the same vowel and voice looks perfectly stable. Real
laps are ~26 s apart, which is a different conversation entirely.

**The measure only works ACROSS LAPS. No existing clip set can test it.**
Trust the 8-lap numbers above, not a score computed inside one capture.

## What to build next

The scanner already visits every channel every lap, and `_accumulate_shape`
in scan.py already keeps a running fingerprint per channel. What it stores is
the MEAN. It needs to also keep how much the per-lap fingerprints DIFFER from
each other — that difference is the whole signal.

Roughly: keep the last N per-lap fingerprints per channel, and record the mean
pairwise similarity. High = machine, middling = voice, near zero = noise.

Nothing else needs to change. The sweep already captures the data and throws
it away.

## Ground truth

`clips/ear.json` — 272 entries, and note the two label sources:

  * `by: whisper` — real words came back, so it is analog voice
  * `by: clock`   — a symbol clock over 24 dB, so it is digital
  * `by: ear`     — the operator listened. Only three, and they are the most
    valuable: 152.2100 and 506.4125 described as "an idling diesel,
    electronic, fast", which is a data stream through an FM discriminator.

Both automatic sources are biased toward clear-cut cases by construction, and
five frequencies are labelled BOTH ways because trunked channels genuinely
carry both at different times.

## Reverted

The flatness change (commit 689db2e) was shipped and reverted the same night.
It scored 94% on clips and then called the operator's known data channel
"voice". Clip accuracy is not field accuracy.
