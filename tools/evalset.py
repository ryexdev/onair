#!/usr/bin/env python3
"""Grow a labelled evaluation set: record channels, let whisper judge them.

    python3 evalset.py            one round
    python3 evalset.py 8          eight channels this round

Appends to night/evalset.json. OFFLINE ONLY — nothing here touches the scanner.

The point: every threshold in this project was chosen from a handful of
measurements and later broke on a case it had not seen. A verdict of "voice"
can only be checked against something independent, and whisper transcribing
actual words is that. One round gives a few samples; a night of rounds gives
enough to choose a number honestly.

Deliberately samples BOTH channels the detector calls voice and channels it
does not. Only sampling the confident ones would measure the detector against
its own opinion.
"""
import json, os, random, re, subprocess, sys, urllib.request
import numpy as np
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import prove, scan, rtl

WHISPER = os.environ.get("WHISPER_BIN", "/opt/homebrew/bin/whisper-cli")
MODEL = os.environ.get("WHISPER_MODEL", "models/ggml-small.en.bin")
STORE = os.path.join(ROOT, "night", "evalset.json")
SECS = 10.0
NONSPEECH = re.compile(r"^[\s]*[\(\[\*].*[\)\]\*][\s]*$")


def board():
    with urllib.request.urlopen(f"http://127.0.0.1:{scan.WEB_PORT}/board", timeout=5) as f:
        return json.load(f)["rows"]


def pick(rows, n):
    # Only sample channels that are LIVE and whose verdict is FRESH. A verdict
    # is held for 10 minutes, so a channel that genuinely carried voice earlier
    # reads "voice" while sitting silent now — sampling it scores a miss
    # against the detector for being right at a different moment. That inflated
    # the error rate in the first rounds and would have driven a bad threshold.
    fresh = [r for r in rows if r.get("on") and r.get("age", 999) < 45]
    pos = [r for r in fresh if r["verdict"] == "voice"]
    neg = [r for r in fresh if r["verdict"] in ("data", "digital", "carrier")]
    random.shuffle(pos)
    random.shuffle(neg)
    half = max(n // 2, 1)
    return pos[:half] + neg[:n - len(pos[:half])]


def listen(text):
    """Is this a transcript, or whisper telling us there was nothing?"""
    t = text.strip()
    if not t or NONSPEECH.match(t):
        return False
    words = [w for w in re.findall(r"[A-Za-z']+", t) if len(w) > 1]
    if len(words) < 3:
        return False
    # self-repeating output is the classic hallucination
    if len(set(w.lower() for w in words)) <= max(2, len(words) // 4):
        return False
    return True


def main(argv):
    n = int(argv[1]) if len(argv) > 1 else 6
    os.makedirs("night", exist_ok=True)
    data = json.load(open(STORE)) if os.path.exists(STORE) else []
    try:
        rows = board()
    except Exception:
        print("  no board running")
        return 1
    targets = pick(rows, n)
    if not targets:
        print("  nothing to sample yet")
        return 0

    subprocess.run(["pkill", "-f", "scan.py"], capture_output=True)
    import time
    time.sleep(1.5)
    r = rtl.Rtl(rtl.find("R828D") or 0, scan.RATE, scan.GAIN_LADDER[-2])
    got = []
    try:
        for t in targets:
            f = t["freq"]
            r.tune(f * 1e6 - prove.OFFSET)
            r.flush()
            y = prove.channelize(r.read(int(SECS * scan.RATE)), scan.RATE,
                                 prove.OFFSET, prove.CHAN_RATE)
            frac, wan, flat, dyn, pres = prove.metrics(y, prove.CHAN_RATE)
            if np.isnan(flat):
                continue
            p = f"night/s_{f:.4f}_{time.strftime('%H%M%S')}.wav"
            prove.wav(p, y, prove.CHAN_RATE)
            got.append({"freq": f, "tag": t.get("tag", ""),
                        "said": t["verdict"], "file": p,
                        "flat": round(float(flat), 3),
                        "dyn": round(float(dyn), 2),
                        "pres": round(float(pres), 1),
                        "syl": round(float(prove.syllabic(y, prove.CHAN_RATE)), 1),
                        "when": time.strftime("%H:%M")})
    finally:
        r.close()
        subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "scan.py"),
                          "full", "--web"],
                         stdout=subprocess.DEVNULL)

    for g in got:
        try:
            out = subprocess.run([WHISPER, "-m", MODEL, "-f", g["file"],
                                  "-nt", "-np"], capture_output=True,
                                 text=True, timeout=180).stdout
            txt = " ".join(out.split())
        except Exception:
            txt = ""
        # Whisper is a reliable POSITIVE and an unreliable negative. Measured
        # against the user's ear on 14 clips: they agreed 10 times, and all 4
        # disagreements were the same way — the ear heard voice, whisper heard
        # nothing. Whisper never claimed speech where the ear heard none. It
        # transcribes from dynamics ~2.6 upward and misses quieter speech.
        # So "speech" is ground truth; "nothing" is UNKNOWN, not "not voice".
        # Scoring them as equivalent is what made a whisper-fitted rule look
        # perfect while missing every marginal channel a human can hear.
        g["heard"] = listen(txt)
        g["label"] = "voice" if g["heard"] else "unknown"
        g["text"] = txt[:120]
        mark = ("OK  " if g["heard"] and g["said"] == "voice"
                else "----" if not g["heard"] else "MISS")
        print(f"  {g['freq']:9.4f} {g['tag']:14} said {g['said']:8} "
              f"heard {'speech' if g['heard'] else 'nothing':7} {mark} "
              f"flat {g['flat']:5.3f} dyn {g['dyn']:5.2f}  {txt[:40]}")
        # a clip with no speech in it is not worth keeping
        if not g["heard"]:
            try:
                os.remove(g["file"])
                g["file"] = None
            except OSError:
                pass
    data += got
    json.dump(data, open(STORE, "w"), indent=1)
    print(f"\n  evaluation set now {len(data)} samples")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
