#!/usr/bin/env python3
"""Label recordings as speech / not, using whisper. OFFLINE ONLY.

    python3 transcribe.py stakeout

Nothing here is wired into the scanner and it never should be — this exists to
build GROUND TRUTH from recordings already on disk, so detector thresholds can
be chosen against evidence instead of against whichever signal happened to be
on air the moment someone looked. Choosing thresholds from single measurements
broke this project three separate times.

Whisper hallucinates, so its output is treated as ONE opinion, not truth:
  * parenthetical output — "(vacuum whirring)", "[BLANK_AUDIO]", "*music*" —
    is whisper's non-speech annotation, so it counts as NOT speech
  * a transcript on a weak signal is suspect and gets flagged, not believed
  * very short or self-repeating output is flagged too
Anything flagged is reported as UNSURE rather than folded into the labels.
"""
import json, os, re, subprocess, sys

WHISPER = os.environ.get("WHISPER_BIN", "/opt/homebrew/bin/whisper-cli")
MODEL = os.environ.get("WHISPER_MODEL", "models/ggml-small.en.bin")
NONSPEECH = re.compile(r"^[\s]*[\(\[\*].*[\)\]\*][\s]*$")
BLANK = re.compile(r"blank_?audio|inaudible|silence", re.I)


def transcribe(path):
    try:
        out = subprocess.run([WHISPER, "-m", MODEL, "-f", path, "-nt", "-np"],
                             capture_output=True, text=True, timeout=180)
        return " ".join(out.stdout.split())
    except Exception as e:
        return f"__ERROR__ {e}"


def judge(text, snr):
    """-> speech | none | unsure, plus why."""
    t = text.strip()
    if t.startswith("__ERROR__"):
        return "unsure", "whisper failed"
    if not t:
        return "none", "no output"
    if NONSPEECH.match(t) or BLANK.search(t):
        return "none", "non-speech annotation"
    words = [w for w in re.findall(r"[A-Za-z']+", t) if len(w) > 1]
    if len(words) < 3:
        return "unsure", "too little text to trust"
    uniq = len(set(w.lower() for w in words))
    if uniq <= max(2, len(words) // 4):
        return "unsure", "self-repeating (classic hallucination)"
    if snr is not None and snr < 8:
        return "unsure", f"transcript on a weak signal ({snr:.0f} dB)"
    return "speech", f"{len(words)} words"


def main(argv):
    d = argv[1] if len(argv) > 1 else "stakeout"
    if not os.path.isdir(d):
        print(f"no such directory: {d}")
        return 1
    files = sorted(f for f in os.listdir(d) if f.endswith(".wav"))
    out = {}
    prev = {}
    if os.path.exists(f"{d}/whisper.json"):
        prev = json.load(open(f"{d}/whisper.json"))
    n_new = 0
    for i, fn in enumerate(files, 1):
        if fn in prev:
            out[fn] = prev[fn]
            continue
        p = os.path.join(d, fn)
        txt = transcribe(p)
        verdict, why = judge(txt, None)
        out[fn] = {"text": txt[:300], "verdict": verdict, "why": why}
        n_new += 1
        print(f"  [{i}/{len(files)}] {fn:34} {verdict:7} {why:34} {txt[:60]}")
        json.dump(out, open(f"{d}/whisper.json", "w"), indent=1)
    tally = {}
    for v in out.values():
        tally[v["verdict"]] = tally.get(v["verdict"], 0) + 1
    print(f"\n  {len(out)} files ({n_new} new): {tally}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
