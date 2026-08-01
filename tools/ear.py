#!/usr/bin/env python3
"""Listen to captures and label them by ear.  python3 tools/ear.py

The existing tools/label.py cannot help with the one distinction that matters:
its UI offers a single button reading "voice / data", so the two classes are
merged at the point of entry. Every automatic label in this project comes from
whisper (which only fires on analog voice) or a symbol-clock test (which only
fires on strong digital), so both are biased toward easy cases and neither can
adjudicate the hard ones.

Labels here are PER CAPTURE, appended, and never overwritten. A trunked channel
genuinely carries voice at one moment and data at another — 11 channels in the
library are already labelled both ways — so one class per frequency cannot
represent the truth.

Writes clips/ear_labels.jsonl:
    {"clip": "...wav", "mhz": 470.7118, "heard": "machine", "note": "...",
     "was": "voice", "t": 1785...}
"""
import glob
import html
import json
import os
import re
import sys
import time
import http.server
import socketserver
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "clips", "ear_labels.jsonl")
PORT = 8704

# Questions first, then the rest of the library.
DIRS = [os.path.expanduser("~/Desktop/onair_listen"), os.path.join(ROOT, "clips")]

CHOICES = [
    ("voice", "Voice", "a person talking", "#4ec27a"),
    ("machine", "Machine", "data / diesel / warble / hash", "#e0603a"),
    ("noise", "Noise", "static, nothing there", "#8b949e"),
    ("both", "Both", "voice AND data in the same clip", "#c77dff"),
    ("unsure", "Not sure", "", "#6e7681"),
]


_sig_cache = {}


def _has_signal(path):
    """Level test only: is anything actually transmitting in this clip?"""
    if path in _sig_cache:
        return _sig_cache[path]
    ok = False
    try:
        import wave
        import numpy as np
        w = wave.open(path)
        r = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()),
                          dtype=np.int16).astype(np.float64) / 32768.0
        N = 512
        n = (len(a) // N) * N
        if n >= N * 4:
            P = np.abs(np.fft.rfft(a[:n].reshape(-1, N) * np.hanning(N),
                                   axis=1)) ** 2
            M = 10 * np.log10(P.mean(axis=0) + 1e-20)
            fr = np.fft.rfftfreq(N, 1 / r)
            b = (fr >= 200) & (fr < r * 0.45)
            M, fr = M[b], fr[b]
            pk = int(np.argmax(M))
            ok = bool(M[pk] - np.median(M) >= 8.0 and fr[pk] < 8000.0)
    except Exception:
        ok = False
    _sig_cache[path] = ok
    return ok


def done_set():
    s = set()
    try:
        for line in open(OUT):
            s.add(json.loads(line)["clip"])
    except Exception:
        pass
    return s


def playlist():
    """Questions first (they decide the open argument), then everything else."""
    items, seen = [], set()
    qmeta = {}
    qp = os.path.expanduser("~/Desktop/onair_listen/QUESTIONS.json")
    try:
        for q in json.load(open(qp)):
            qmeta[q["file"]] = q
    except Exception:
        pass
    for d in DIRS:
        for p in sorted(glob.glob(os.path.join(d, "*.wav"))):
            b = os.path.basename(p)
            if b in seen:
                continue
            seen.add(b)
            q = qmeta.get(b)
            mhz, was = None, None
            if q:
                mhz, was = q["mhz"], q["library_says"]
            else:
                m = re.match(r"(\d+)_(\d+)_\d+_([vd])\.wav$", b)
                if m:
                    mhz = float(f"{int(m.group(1))}.{m.group(2)}")
                    was = "voice" if m.group(3) == "v" else "digital"
                else:
                    m2 = re.match(r"([\d.]+)", b)
                    if m2:
                        try:
                            mhz = float(m2.group(1).rstrip("."))
                        except ValueError:
                            pass
            items.append({"path": p, "clip": b, "mhz": mhz, "was": was,
                          "isq": bool(q),
                          "sim": q["similarity"] if q else None})
    # DROP anything with nothing on it. Telling static from a data stream by
    # ear is genuinely hard and the operator should not be spending attention
    # on it — the machine already does that part reliably by level alone
    # (is_really_live rejected every noise sample it was shown). The ear is
    # needed for exactly one thing no measurement has managed: voice vs
    # machine. So only clips with a real signal are queued.
    items = [x for x in items if _has_signal(x["path"])]
    # Questions first, then EVERYTHING ELSE SHUFFLED. Sorted order meant a
    # partial pass would be all one part of the band — the operator is not
    # going to label 900 clips, so whatever subset gets done has to be a fair
    # sample of the whole library rather than the alphabetical start of it.
    # Fixed seed so the order is stable across restarts and progress survives.
    import random
    rest = [x for x in items if not x["isq"]]
    random.Random(1).shuffle(rest)
    qs = sorted((x for x in items if x["isq"]), key=lambda x: x["clip"])
    return qs + rest


PAGE = """<!doctype html><meta charset=utf-8><title>ear</title>
<style>
 body{background:#0d1117;color:#e6edf3;font:15px -apple-system,system-ui,sans-serif;
      margin:0;padding:28px;max-width:760px}
 h1{font-size:17px;margin:0 0 4px} .sub{color:#8b949e;font-size:13px;margin-bottom:18px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:20px}
 .f{font-size:30px;font-weight:600;letter-spacing:-.5px}
 .meta{color:#8b949e;font-size:13px;margin:6px 0 16px}
 audio{width:100%;margin:6px 0 18px}
 button{font:600 15px inherit;color:#0d1117;border:0;border-radius:8px;
        padding:13px 18px;margin:0 8px 8px 0;cursor:pointer}
 button:hover{filter:brightness(1.12)}
 input{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;
       border-radius:8px;padding:11px;font:14px inherit;margin-bottom:14px}
 .prog{color:#8b949e;font-size:12px;margin-top:16px}
 kbd{background:#21262d;border:1px solid #30363d;border-radius:4px;padding:1px 6px;
     font:12px ui-monospace;color:#8b949e}
 .q{display:inline-block;background:#3b2f00;color:#ffd54a;border-radius:5px;
    padding:2px 8px;font-size:11px;font-weight:700;margin-left:8px}
</style>
<h1>What do you hear?</h1>
<div class=sub>Per capture, not per channel. A channel can be voice now and data later &mdash; that is expected.</div>
<div class=card id=card></div>
<div class=prog id=prog></div>
<script>
let items=[], i=0;
const CH=%CHOICES%;
async function load(){
  const r=await fetch('/list'); const d=await r.json();
  items=d.items; i=0; show();
}
function show(){
  if(i>=items.length){document.getElementById('card').innerHTML=
     '<div class=f>All done</div><div class=meta>Nothing left to label.</div>';
     document.getElementById('prog').textContent=''; return;}
  const it=items[i];
  let h='<div class=f>'+(it.mhz?it.mhz.toFixed(4)+' MHz':it.clip)+
        (it.isq?'<span class=q>DECIDES IT</span>':'')+'</div>';
  h+='<div class=meta>';
  if(it.was) h+='library currently says <b>'+it.was+'</b>';
  if(it.sim!=null) h+=' &middot; looks '+Math.round(it.sim*100)+'% like the diesel channel';
  h+='</div>';
  h+='<audio controls autoplay src="/wav?c='+encodeURIComponent(it.clip)+'"></audio>';
  h+='<input id=note placeholder="what you heard, optional">';
  CH.forEach(function(c,n){
    h+='<button style="background:'+c[3]+'" onclick="mark(\\''+c[0]+'\\')">'+
       c[1]+' <kbd>'+(n+1)+'</kbd></button>';
  });
  h+='<button style="background:#30363d;color:#e6edf3" onclick="undo()">'+
     '\\u21a9 Undo last <kbd>u</kbd></button>';
  document.getElementById('card').innerHTML=h;
  document.getElementById('prog').textContent=(i+1)+' of '+items.length+
     '  \\u2014  keys 1-'+CH.length+' to label, space replays';
}
async function undo(){
  const r=await fetch('/undo',{method:'POST'});
  const d=await r.json();
  if(d.undone){ i=Math.max(0,i-1); items.splice(i,0,{clip:d.undone.clip,
      mhz:d.undone.mhz,was:d.undone.was,isq:false,sim:null}); }
  show();
}
async function mark(v){
  const it=items[i];
  await fetch('/mark',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({clip:it.clip,mhz:it.mhz,was:it.was,heard:v,
                         note:document.getElementById('note').value})});
  i++; show();
}
document.addEventListener('keydown',function(e){
  if(e.target.tagName==='INPUT'&&e.key!=='Enter')return;
  const n=parseInt(e.key);
  if(n>=1&&n<=CH.length){mark(CH[n-1][0]);e.preventDefault();}
  if(e.key==='u'){undo();e.preventDefault();return;}
  if(e.key===' '){const a=document.querySelector('audio');if(a){a.currentTime=0;a.play();}e.preventDefault();}
});
load();
</script>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/list"):
            done = done_set()
            items = [x for x in playlist() if x["clip"] not in done]
            self._send(json.dumps({"items": items}).encode(),
                       "application/json")
            return
        if self.path.startswith("/wav"):
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            name = q.get("c", [""])[0]
            for it in playlist():
                if it["clip"] == name:
                    self._send(open(it["path"], "rb").read(), "audio/wav")
                    return
            self._send(b"not found", "text/plain", 404)
            return
        page = PAGE.replace("%CHOICES%", json.dumps(CHOICES))
        self._send(page.encode())

    def do_POST(self):
        if self.path.startswith("/undo"):
            # Append-only is right for a label log, but a misclick should not
            # need a text editor. Drop the last line and hand it back so the
            # page can re-queue that clip.
            try:
                lines = open(OUT).readlines()
            except Exception:
                lines = []
            last = json.loads(lines[-1]) if lines else None
            if lines:
                tmp = OUT + ".tmp"
                with open(tmp, "w") as fh:
                    fh.writelines(lines[:-1])
                os.replace(tmp, OUT)
            self._send(json.dumps({"undone": last}).encode(),
                       "application/json")
            return
        n = int(self.headers.get("Content-Length", 0))
        d = json.loads(self.rfile.read(n) or b"{}")
        d["t"] = int(time.time())
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "a") as fh:            # append only, never overwrite
            fh.write(json.dumps(d) + "\n")
        self._send(b'{"ok":1}', "application/json")


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    n = len([x for x in playlist() if x["clip"] not in done_set()])
    print(f"  {n} clips to label  ->  http://127.0.0.1:{PORT}/")
    print(f"  writes {OUT}")
    socketserver.TCPServer(("127.0.0.1", PORT), H).serve_forever()
