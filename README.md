# onair

Sweep the radio spectrum with an RTL-SDR, find every channel that is actually
in use, and say what kind of thing is on it — **without decoding anything**.

```
python3 scan.py full --web     ->  http://127.0.0.1:8701/
```

Most SDR software either draws you a power heatmap, or tunes a list of
frequencies you already knew about. This does neither. It discovers channels it
was never told about, then answers a different question:

> Is a human talking here? Is this machine data? Or is this a transmitter with
> nothing on it?

It answers that structurally. Noise has no structure. A bare carrier has
structure that never changes. **Information is structure that changes.** Every
measurement in here is a way of quantifying that last sentence.

## What the CARRYING column means

| verdict   | meaning |
|-----------|---------|
| `voice`   | someone talking |
| `digital` | machine data with a stable symbol clock — P25, pager, packet |
| `data`    | carrying something, kind unclear |
| `burst`   | a real carrier too short to characterise — pagers, TETRA, telemetry, key fobs |
| `carrier` | transmitter on, nothing modulated onto it |
| `tone`    | a steady tone: structured, but carries no information |
| `noise`   | nothing coherent |
| `quiet`   | nothing on air when we listened (often a bursty channel, not a dead one) |
| `?`       | not judged yet |

## How it works

**Sweep.** 908 steps of 1.92 MHz usable bandwidth, ~20 ms each. Every step is
scored on whether a peak *persists* across frames, how sharply it stands out of
its surroundings, and how steady its level is. Noise wanders; a transmission
stays put. Steps that produced something recently are revisited every lap;
quiet ones one lap in six, so nothing is ever permanently ignored.

**Judge.** A 1.2 s capture of one 1.92 MHz slice already contains ~40 channels,
so they are all extracted from that single capture and classified in parallel.
Each channel is FM-discriminated and measured for spectral flatness, loudness
dynamics, 3–6 Hz syllabic rhythm, carrier presence, and symbol-clock
stability. One function, `classify()`, makes the decision — the sweep and the
band monitor both call it, so they cannot drift apart.

**Listen (optional).** If a whisper.cpp build is present, a background thread
transcribes audio that was *already captured* and upgrades a channel to `voice`
when it hears real words. It never touches the radio, nothing waits on it, and
a failure can only leave a label unconfirmed — never erase one. It exists
because morse code, courtesy beeps and squelch tails all modulate at 3–6 Hz,
which is the band that defines speech rhythm, so the structural test cannot
separate them from speech at any threshold. Whisper can.

## Requirements

- An RTL-SDR. Developed against an RTL-SDR Blog V4 (R828D tuner).
- `librtlsdr` (`brew install librtlsdr`)
- Python 3 and numpy. That is the whole dependency list — no scipy, no torch,
  no GNU Radio.
- Optional: [whisper.cpp](https://github.com/ggerganov/whisper.cpp) and a model,
  for speech confirmation.

```bash
export WHISPER_BIN=/opt/homebrew/bin/whisper-cli
export WHISPER_MODEL=/path/to/ggml-small.en.bin
```

Without them the scanner prints `whisper not found — structural classification
only` and everything else behaves identically.

## Using the board

Sweep the whole spectrum, click **watch** on a band chip to monitor that band
continuously, **back to full sweep** to return. One radio, so one mode at a time.

Untick a band to stop spending radio time on it. Click the ★ on any row to
bookmark it — bookmarks pin to the top, carry an editable note, and stay
visible even when the channel is silent. The **bf** column marks whether a
common handheld (136–174 / 400–520 MHz, FM only) can receive it.

State lives in `muted.json`, `spurs.json` and `bookmarks.json`, all created on
demand. None of them ship, and the scanner runs fine without them.

## Tools

Most need the radio to themselves, so stop `scan.py` first. Run them from the
repo root; they write to `clips/`, `stakeout/` and `night/` there regardless of
where you invoke them from.

```
python3 tools/meter.py 162.5500     live signal meter, 8 updates/sec, for aiming an antenna
python3 tools/stakeout.py 147.4350  park on one channel, save every transmission
python3 tools/label.py grab 146.52 147.435
                                    record clips, then `ui` to label them by ear
python3 tools/review.py stakeout    listen to clips and label them one by one
python3 tools/transcribe.py stakeout   whisper-label a directory of recordings (offline)
python3 tools/evalset.py            grow a labelled evaluation set
                                    (this one takes and returns the radio itself —
                                     leave scan.py running)
python3 prove.py 146.52             one channel, in detail

python3 tools/tune.py noise         synthetic Gaussian — every hit is a false
                                    positive by construction
python3 tools/tune.py off           same, with the antenna unplugged, so real
                                    hardware artefacts are included
python3 tools/tune.py sweep         false positives vs detections across the
                                    detection thresholds
python3 tools/tune.py repeat        do the same channels reappear in a later
                                    session? A coin-flip detector scores ~0%
```

## Known limits

**HF below 24 MHz is unavailable.** The V4 can receive it — it has a built-in
upconverter and a triplexer splitting off HF 0–28 MHz — but that path needs the
rtlsdr-blog fork of librtlsdr, and Homebrew ships the osmocom one. That is also
why `pyrtlsdr` will not import against a Homebrew build: it wants
`rtlsdr_set_dithering`, a symbol the osmocom version does not export. This
project talks to the library through a small ctypes wrapper for that reason.

**Local interference travels with the hardware.** Three clock combs are
excluded: 28.8 MHz (the dongle's own reference), 12 MHz (USB) and 27 MHz. The
last two are radiated, so an antenna-off calibration cannot find them. A
consequence: any real channel landing on an exact harmonic is invisible —
144.000, 432.000, 120.000, 132.000 and 456/459/468 among them.

**Dense occupancy collapses.** Adjacent occupied channels merge into one
detection; more than ~300 kHz of contiguous occupancy is dropped entirely. FM
broadcast, cellular and busy trunked sites are effectively invisible.

**Signals shorter than ~100 ms** are detected but reported as `burst`, not
characterised.

**Band labels are US allocations.** Display only — nothing in the detector
reads them.

**`digital` is far less verified than `voice`.** Voice has been checked against
a human ear and against transcription many times over. The digital half rests
on a handful of cases — a pager confirmed by ear, and P25 symbol clocks that
reproduce across runs. Treat it as the weaker claim.


**Two more things that are known and unfixed.** About twenty weak signals
across 50–54 MHz are detected but unidentified — tested and ruled out: a
regular comb (no grid fits) and a pure tone (energy is spread, not
concentrated). They are real and reproducible across tuner centres; their
origin is not known. And a strong nearby transmitter splatters the whole
slice: 5 W at arm's length manufactured 31 phantom `data` channels, all
starting and stopping in the same instant. Not guarded against.

## How any of this was checked

Detection was verified four ways that do not depend on each other: signals
vanish when the antenna is disconnected; a controlled transmission on 146.520
was found at exactly 146.5200; ADS-B measures a 56.7 µs median burst against a
56 µs spec the code knows nothing about; and P25 at 858.2125 reads a 4800 Hz
symbol clock on four consecutive runs.

Voice was checked against a human ear and against whisper transcribing actual
words — including a GMRS conversation where the callsign in the transcript
matched the licence format for that service. Those two ground truths disagree
often enough to be worth keeping separate: whisper is a reliable positive and
an unreliable negative, since it misses quiet speech but does not invent it
above about 14 dB.

Frequency accuracy, against transmitters on exactly known channels: NOAA
162.5500 reads 162.5498, and 147.0000 reads 146.9998.

`docs/research.md` records the rest: measured evidence behind every threshold,
and a long list of things that were tried and did not work.

## A note on method

Almost every threshold in this project was first set from a handful of
measurements, and most of them later broke on a case they had not seen. The
comments record what each number was measured against, and `docs/research.md`
records the ideas that were tested and rejected — voiced-frame ratio, a
retuned syllabic threshold, striking the radio the instant a channel is seen.
They are written down because a rejected idea with evidence is worth more than
an untested one that sounds good.
