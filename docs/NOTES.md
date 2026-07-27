# Notes

## Running it

    python3 scan.py full --web      ->  http://127.0.0.1:8701/

Sweep the whole spectrum, click **watch** on any band chip to monitor that band
continuously, **back to full sweep** to return. One radio, so only one mode at
a time.

Other tools, each needs the radio to itself (stop `scan.py` first):

    python3 meter.py 162.5500      live signal meter, 8 updates/sec, for aiming
                                   an antenna while your hand is still on it
    python3 prove.py 146.52 ...    one channel, in detail
    python3 watch.py 146.52        park on a channel until it transmits
    python3 label.py grab / ui     record clips and label them by ear
    python3 band.py 144 148        band monitor, standalone

State on disk: `muted.json` (which bands are skipped), `spurs.json` (this
dongle's measured internal spurs), `clips/` (recordings and labels).

## What the CARRYING column means

    voice     someone talking
    digital   machine data - P25, pager, packet
    data      carrying something, kind unclear
    carrier   transmitter on, nothing modulated onto it
    tone      steady tone: structured, but carries no information
    noise     nothing coherent
    quiet     nothing on air when we listened (often a bursty channel, not dead)
    ?         not judged yet

Detection is instant; judging takes a 1.2 s listen, so `?` clears over the
first few minutes, strongest signals first.

## Known limits

**HF below 24 MHz is not available.** The V4 can receive it — it has a built-in
upconverter and a triplexer splitting HF 0-28 MHz — but that path needs the
rtlsdr-blog fork of librtlsdr, and Homebrew ships the osmocom one (which is
also why `pyrtlsdr` fails to import: it wants `rtlsdr_set_dithering`, a symbol
the osmocom build does not have). Enabling HF means installing the fork; no
code here would need to change beyond selecting the mode. Not done, because HF
has never come up as something worth having.

**The 50-54 MHz cluster is unexplained.** About 20 weak signals across the 6 m
band, currently reported as `tone`. They are detected and displayed like
anything else — nothing is hardcoded or skipped. What is unknown is their
ORIGIN. Tested and ruled out: a regular comb (no grid fits), a pure single
tone (energy is spread, not concentrated). Called them tones once and
disproved it, then a symbol-clock test called them digital and that did not
reproduce either. Left alone deliberately rather than guessing a third time.

**Local interference travels with the hardware.** Three clock combs are
excluded: 28.8 MHz (the dongle's own reference, found with the antenna
disconnected), 12 MHz (the computer's USB clock) and 27 MHz. The last two are
RADIATED — they arrive through the antenna, so the antenna-off calibration
cannot find them, and they were putting strong "data" hits on the board.
A new location brings its own. The signature is always the same: a loud signal
on an exact multiple of some frequency, with empty spectrum 250 kHz either
side.

**A strong nearby transmitter splatters the whole slice.** 5 W at arm's length
manufactured 31 phantom `data` channels, all starting and stopping within the
same instant. Not guarded against; an edge case in practice.

**Band labels are US allocations.** Correct in any state, wrong abroad except
for ham, airband, marine and ADS-B, which are near-universal. Labels are
display only — nothing in the detector reads them.

## What has been verified, and how

Voice detection is 5/5 against a human ear with no false positives. A
controlled transmission (handheld keyed on 146.520) was found at exactly
146.5200. ADS-B measures a 56.7 us median burst against a 56 us spec the code
knows nothing about. P25 at 858.2125 reads a 4800 Hz symbol clock on four
consecutive runs.

Digital detection has one ear-confirmed case (a pager, heard as bursts). That
half is far less verified than voice.

Thresholds rest on roughly nine decisive labelled examples. That is thin, and
every one of them was set from a handful of cases that later broke on a case
they had not seen. `label.py` exists to grow that set.
