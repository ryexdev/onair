#!/usr/bin/env python3
"""Record clips, then let a human say which ones are data.

    python3 label.py grab            record clips from what is on air now
    python3 label.py grab 146.52 ... record these specific channels
    python3 label.py ui              listen and label them
                                     -> http://127.0.0.1:8703/

Every threshold in this project was set by staring at signals nobody had
labelled. Three of them turned out wrong, and each time a human ear settled it
in seconds. This builds the labelled set that should have existed first.

Clips are demodulated BOTH ways (FM and AM) and the audible one is saved, since
airband and military air are AM and sound like nothing through an FM
discriminator. Our own metrics are recorded alongside but NOT shown while
labelling — seeing the machine's guess first would bias the answer.
"""
import json, os, struct, sys, time
import numpy as np
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scan, rtl, prove

# Which set of clips to label. `grab` always writes to clips/, but the UI can
# be pointed at any directory holding wavs plus a clips.json manifest — the
# stakeout recordings are the bigger set and were previously unreachable:
#     python3 label.py ui stakeout
CLIPS = "clips"
META = "clips/clips.json"
LABELS = "clips/labels.json"


def use_dir(d):
    global CLIPS, META, LABELS
    CLIPS, META, LABELS = d, f"{d}/clips.json", f"{d}/labels.json"
SECS = 15.0
PORT = 8703


def wav_bytes(a, rate=16000):
    a = a / (np.abs(a).max() + 1e-9) * 0.7
    pcm = (a * 32767).astype("<i2").tobytes()
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def grab(freqs):
    os.makedirs(CLIPS, exist_ok=True)
    meta = json.load(open(META)) if os.path.exists(META) else []
    have = {m["freq"] for m in meta}
    r = rtl.Rtl(rtl.find("R828D") or 0, scan.RATE, scan.GAIN_LADDER[-2])
    n = int(SECS * scan.RATE)
    try:
        for f in freqs:
            r.tune(f * 1e6 - prove.OFFSET)
            r.flush()
            y = prove.channelize(r.read(n), scan.RATE, prove.OFFSET,
                                 prove.CHAN_RATE)
            frac, wander, flat, dyn, pres = prove.metrics(y, prove.CHAN_RATE)

            fm = np.angle(y[1:] * np.conj(y[:-1]))
            am = np.abs(y) - np.abs(y).mean()
            # keep whichever demodulator carries more, so an AM channel is not
            # judged through an FM discriminator (it would sound like noise)
            # pick by which demodulator produces the more STRUCTURED audio,
            # the same test metrics() uses. The previous version compared a
            # phase quantity against an amplitude one and always chose FM,
            # which makes every AM channel sound like hiss.
            def _flat(sig):
                k = 4096
                w = sig[:len(sig) // k * k].reshape(-1, k)
                w = w - w.mean(axis=1, keepdims=True)
                D = (np.abs(np.fft.rfft(w * np.hanning(k), axis=1)) ** 2).mean(axis=0)
                fa = np.fft.rfftfreq(k, 1 / prove.CHAN_RATE)
                Db = D[(fa >= 300) & (fa <= 6000)] + 1e-20
                return float(np.exp(np.mean(np.log(Db))) / np.mean(Db))
            pick, how = (fm, "fm") if _flat(fm) <= _flat(am) else (am, "am")
            step = max(int(round(prove.CHAN_RATE / 16000)), 1)
            name = f"{f:.4f}".replace(".", "_") + f"_{int(time.time())}.wav"
            open(os.path.join(CLIPS, name), "wb").write(
                wav_bytes(pick[::step]))
            meta = [m for m in meta if m["freq"] != f]
            meta.append({"freq": f, "file": name, "demod": how,
                         "tag": scan.label_for(f), "secs": SECS,
                         "when": time.strftime("%H:%M"),
                         "flat": None if np.isnan(flat) else round(flat, 3),
                         "dyn": None if np.isnan(dyn) else round(dyn, 2),
                         "pres": round(pres, 1), "onair": round(frac, 2)})
            print(f"  {f:10.4f}  {how}  pres {pres:5.1f} dB  -> {name}")
    finally:
        r.close()
    json.dump(meta, open(META, "w"), indent=1)
    print(f"\n{len(meta)} clips in {CLIPS}/  — now run: python3 label.py ui")


PAGE = """<!doctype html><meta charset=utf-8><title>is this data?</title>
<style>
body{font:14px/1.5 -apple-system,system-ui,sans-serif;background:#0d0f12;
     color:#dfe3e8;margin:0;padding:22px;max-width:680px}
h1{font-size:16px;margin:0 0 3px}
.sub{color:#6b7480;font-size:12px;margin-bottom:20px}
.c{border:1px solid #1e242c;border-radius:8px;padding:13px 15px;margin-bottom:11px;
   background:#11151a}
.c.done{opacity:.5}
.row{display:flex;align-items:center;gap:12px;margin-bottom:9px}
.f{font:600 18px ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.t{color:#8b96a6;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
audio{width:100%;height:34px;margin:4px 0 9px}
button{font:inherit;font-size:12px;padding:6px 15px;border-radius:6px;
       border:1px solid #2a323c;background:#171c22;color:#cfd6df;cursor:pointer;
       margin-right:6px}
button:hover{border-color:#3d4753}
button.y{color:#4ec27a}button.n{color:#c9776f}button.u{color:#9aa4b2}
button.sel{background:#1d2c22;border-color:#2f6b45;font-weight:600}
button.n.sel{background:#2c1d1d;border-color:#6b3535}
button.u.sel{background:#22262c;border-color:#4a545f}
.done .f{color:#7f8896}
.hint{color:#5a6470;font-size:11px;margin-top:5px}
</style>
<h1>Is this data?</h1>
<div class=sub id=sub>loading&hellip;</div>
<div id=out></div>
<script>
var L={};
async function load(){
  var m=await(await fetch('/meta')).json();
  L=await(await fetch('/labels')).json();
  var h='',n=0;
  for(var i=0;i<m.length;i++){var c=m[i],lab=L[c.freq];
    if(lab)n++;
    h+='<div class="c '+(lab?'done':'')+'" id="c'+i+'">'+
       '<div class=row><span class=f>'+c.freq.toFixed(4)+'</span>'+
       '<span class=t>'+(c.tag||'')+' &middot; '+c.demod+' &middot; '+c.when+'</span></div>'+
       '<audio controls preload=none src="/clip/'+encodeURIComponent(c.file)+'"></audio>'+
       '<div>'+
       '<button class="y'+(lab=='data'?' sel':'')+'" onclick="mark('+c.freq+',\\'data\\','+i+')">voice / data</button>'+
       '<button class="n'+(lab=='none'?' sel':'')+'" onclick="mark('+c.freq+',\\'none\\','+i+')">nothing</button>'+
       '<button class="u'+(lab=='unsure'?' sel':'')+'" onclick="mark('+c.freq+',\\'unsure\\','+i+')">can\\'t tell</button>'+
       '</div></div>';
  }
  document.getElementById('out').innerHTML=h||'<p>No clips yet. Run: python3 label.py grab</p>';
  document.getElementById('sub').textContent=
    n+' of '+m.length+' labelled \\u00b7 mark "nothing" for hiss or a dead carrier \\u00b7 '+
    '"can\\'t tell" is a real answer, use it freely';
}
async function mark(f,v,i){
  L[f]=v;
  await fetch('/label?f='+f+'&v='+v);
  var el=document.getElementById('c'+i);
  el.classList.add('done');
  el.querySelectorAll('button').forEach(function(b){b.classList.remove('sel')});
  var k={data:0,none:1,unsure:2}[v];
  el.querySelectorAll('button')[k].classList.add('sel');
  var m=await(await fetch('/meta')).json();
  document.getElementById('sub').textContent=
    Object.keys(L).length+' of '+m.length+' labelled';
}
load();
</script>"""


def ui():
    import http.server
    import urllib.parse

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ct):
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = urllib.parse.urlparse(self.path)
            if p.path == "/meta":
                d = json.load(open(META)) if os.path.exists(META) else []
                self._send(json.dumps(d).encode(), "application/json")
            elif p.path == "/labels":
                d = json.load(open(LABELS)) if os.path.exists(LABELS) else {}
                self._send(json.dumps(d).encode(), "application/json")
            elif p.path == "/label":
                q = urllib.parse.parse_qs(p.query)
                d = json.load(open(LABELS)) if os.path.exists(LABELS) else {}
                d[q["f"][0]] = q["v"][0]
                json.dump(d, open(LABELS, "w"), indent=1)
                self._send(b'{"ok":1}', "application/json")
            elif p.path.startswith("/clip/"):
                fn = urllib.parse.unquote(p.path[6:])
                try:
                    self._send(open(os.path.join(CLIPS, fn), "rb").read(),
                               "audio/wav")
                except OSError:
                    self.send_error(404)
            else:
                self._send(PAGE.encode(), "text/html; charset=utf-8")

    print(f"listen and label -> http://127.0.0.1:{PORT}/")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


def auto(minutes=60, every=90):
    """Collect clips on a loop so a labelled set can actually accumulate.

    Twelve clips is not a dataset. Every threshold in this project was set from
    a handful of examples and every one of them later broke on a case it had
    not seen. The only fix is volume: many clips, across bands and across the
    day, labelled by ear.

    Deliberately grabs a MIX — channels the board thinks are carrying data,
    channels it thinks are bare carriers, and a couple it thinks are nothing.
    Only sampling the confident ones would teach the model its own opinions.
    """
    import random, urllib.request
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{scan.WEB_PORT}/board",
                                        timeout=4) as f:
                rows = json.load(f)["rows"]
        except Exception:
            print("  (no board; start scan.py --web)")
            time.sleep(20)
            continue
        pick = []
        for v, k in (("data", 4), ("carrier", 3), ("tone", 2), ("quiet", 1)):
            c = [r["freq"] for r in rows if r["verdict"] == v]
            random.shuffle(c)
            pick += c[:k]
        if not pick:
            time.sleep(20)
            continue
        print(f"[{time.strftime('%H:%M')}] grabbing {len(pick)}")
        import subprocess
        subprocess.run(["pkill", "-f", "scan.py"], capture_output=True)
        time.sleep(1.0)
        try:
            grab(pick)
        except Exception as e:
            print("  grab failed:", e)
        subprocess.Popen(["python3", "-u", "scan.py", "full", "--web"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(max(every - 30, 10))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "ui"
    if cmd == "ui":
        if len(argv) > 2:
            use_dir(argv[2].rstrip("/"))
        ui()
    elif cmd == "manifest":
        # Build a clips.json for a directory of wavs that were recorded by
        # something else (stakeout.py), so the labelling UI can read them.
        # Deliberately carries NO verdict and NO whisper label — seeing either
        # before answering would bias the ear, which is the whole point of
        # labelling by hand.
        d = argv[2].rstrip("/") if len(argv) > 2 else "stakeout"
        out = []
        # Two naming conventions exist in stakeout/: "146.1600_213639.wav" and
        # "147_4375_212029.wav". Handle both rather than silently dropping 104
        # of 121 files, which is what parsing only the second one did.
        import re
        if not os.path.isdir(d):
            print(f"no such directory: {d}")
            return 1
        for fn in sorted(f for f in os.listdir(d) if f.endswith(".wav")):
            m = re.match(r"^(\d+)[._](\d+)_(\d+)\.wav$", fn)
            if not m:
                print(f"  skipped (unparsed name): {fn}")
                continue
            freq = float(f"{m.group(1)}.{m.group(2)}")
            out.append({"freq": freq, "file": fn, "demod": "fm",
                        "tag": scan.label_for(freq), "when": m.group(3)})
        json.dump(out, open(f"{d}/clips.json", "w"), indent=1)
        print(f"{len(out)} clips -> {d}/clips.json")
        print(f"now run: python3 label.py ui {d}")
    elif cmd == "auto":
        auto(float(argv[2]) if len(argv) > 2 else 60)
    elif cmd == "grab":
        fs = [float(a) for a in argv[2:]]
        if not fs:
            import urllib.request
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{scan.WEB_PORT}/board",
                                            timeout=4) as f:
                    rows = json.load(f)["rows"]
                fs = [r["freq"] for r in rows
                      if r["verdict"] in ("data", "carrier")][:12]
            except Exception:
                print("no board running; pass frequencies explicitly")
                return 1
        grab(fs)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
