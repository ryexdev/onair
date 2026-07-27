#!/usr/bin/env python3
"""Listen to a SHORT list of clips and say what each one is carrying.

    python3 review.py stakeout            everything in stakeout/review_list.json
    python3 review.py stakeout all        every wav in the directory

    -> http://127.0.0.1:8705/

Different from label.py in three ways that matter:

  * answers are keyed by FILENAME, not by frequency, so ten recordings of the
    same channel get ten answers instead of overwriting each other. label.py
    keyed by frequency and collapsed 121 clips into 22.
  * voice and data are SEPARATE answers. label.py offered "voice / data" as one
    button, which cannot settle any question about telling them apart.
  * there is a comment box, because "voice" and "data" do not cover it - a
    recording can be a repeater tail, a courtesy beep, music, or two of those
    at once, and that detail is worth more than the button.

Answers land in <dir>/review.json and are written on every click, so stopping
half way loses nothing. Our own verdict and whisper's are deliberately NOT
shown: seeing a machine's guess first is how a labelled set gets quietly
bent into agreeing with the thing it was supposed to check.
"""
import http.server
import json
import os
import sys
import urllib.parse

DIR = "stakeout"   # relative to where you run it
PORT = 8705


def clips():
    lst = os.path.join(DIR, "review_list.json")
    if os.path.exists(lst) and "all" not in sys.argv[2:3]:
        names = json.load(open(lst))
    else:
        if not os.path.isdir(DIR):
            print(f"no such directory: {DIR}")
            return []
        names = sorted(f for f in os.listdir(DIR) if f.endswith(".wav"))
    out = []
    for fn in names:
        if not os.path.exists(os.path.join(DIR, fn)):
            continue
        base = fn[:-4].replace("_", ".", 1) if fn[:-4].count("_") > 1 \
            else fn[:-4]
        out.append({"file": fn, "name": base})
    return out


def answers():
    p = os.path.join(DIR, "review.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def save(d):
    json.dump(d, open(os.path.join(DIR, "review.json"), "w"), indent=1)


PAGE = """<!doctype html><meta charset=utf-8><title>what is this carrying?</title>
<style>
body{font:14px/1.55 -apple-system,system-ui,sans-serif;background:#0d0f12;
     color:#dfe3e8;margin:0;padding:24px;max-width:700px}
h1{font-size:16px;margin:0 0 3px}
.sub{color:#6b7480;font-size:12px;margin-bottom:6px}
.prog{color:#6b7480;font-size:12px;margin-bottom:20px}
.prog b{color:#4ec27a}
.c{border:1px solid #1e242c;border-radius:9px;padding:14px 16px;margin-bottom:12px;
   background:#11151a}
.c.done{border-color:#24402f;background:#101711}
.hd{display:flex;align-items:baseline;gap:12px;margin-bottom:8px}
.f{font:600 17px ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.t{color:#8b96a6;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
audio{width:100%;height:34px;margin:2px 0 10px}
.btns{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:9px}
button{font:inherit;font-size:12.5px;padding:6px 14px;border-radius:6px;
       border:1px solid #2a323c;background:#171c22;color:#cfd6df;cursor:pointer}
button:hover{background:#1e242c}
button.sel{color:#0d0f12;font-weight:600;border-color:transparent}
button.voice.sel{background:#4ec27a}
button.data.sel{background:#5fa8d3}
button.both.sel{background:#b98bd6}
button.none.sel{background:#7d8794}
button.unsure.sel{background:#c2a24e}
textarea{width:100%;box-sizing:border-box;background:#0d1116;color:#dfe3e8;
         border:1px solid #232a33;border-radius:6px;padding:7px 9px;font:inherit;
         font-size:12.5px;resize:vertical;min-height:34px}
textarea:focus{outline:none;border-color:#3a4552}
.hint{color:#5c6672;font-size:11px;margin-top:5px}
</style>
<h1>what is this carrying?</h1>
<div class=sub>listen first, then answer. the comment box matters more than the
button when the button does not fit.</div>
<div class=prog id=prog></div>
<div id=list></div>
<script>
var C=[],A={};
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')}
async function mark(fn,v){
  A[fn]=A[fn]||{}; A[fn].verdict=v;
  await fetch('/set?f='+encodeURIComponent(fn)+'&v='+encodeURIComponent(v));
  draw();
}
async function note(fn,el){
  A[fn]=A[fn]||{}; A[fn].note=el.value;
  await fetch('/set?f='+encodeURIComponent(fn)+'&n='+encodeURIComponent(el.value));
  var p=document.getElementById('prog'); p.textContent=p.textContent;
}
function draw(){
  var done=C.filter(function(c){return A[c.file]&&A[c.file].verdict}).length;
  document.getElementById('prog').innerHTML='<b>'+done+'</b> of '+C.length+' answered';
  document.getElementById('list').innerHTML=C.map(function(c){
    var a=A[c.file]||{},v=a.verdict;
    function b(k,t){return '<button class="'+k+(v==k?' sel':'')+
      '" onclick="mark(\\''+c.file+'\\',\\''+k+'\\')">'+t+'</button>'}
    return '<div class="c'+(v?' done':'')+'">'+
      '<div class=hd><span class=f>'+c.name+'</span></div>'+
      '<audio controls preload=none src="/clip/'+encodeURIComponent(c.file)+'"></audio>'+
      '<div class=btns>'+b('voice','voice')+b('data','data')+
        b('both','both')+b('none','nothing')+b('unsure','can\\'t tell')+'</div>'+
      '<textarea placeholder="what did you actually hear? beeps, music, a tail, '+
      'two people, a tone..." onchange="note(\\''+c.file+'\\',this)">'+
      esc(a.note)+'</textarea>'+
    '</div>'}).join('');
}
(async function(){
  C=await (await fetch('/clips')).json();
  A=await (await fetch('/answers')).json();
  draw();
})();
</script>"""


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
        if p.path == "/clips":
            self._send(json.dumps(clips()).encode(), "application/json")
        elif p.path == "/answers":
            self._send(json.dumps(answers()).encode(), "application/json")
        elif p.path == "/set":
            q = urllib.parse.parse_qs(p.query)
            d = answers()
            fn = q["f"][0]
            rec = d.setdefault(fn, {})
            # verdict and note arrive in separate requests; keep whichever the
            # other one already set instead of replacing the whole record
            if "v" in q:
                rec["verdict"] = q["v"][0]
            if "n" in q:
                rec["note"] = q["n"][0]
            save(d)
            self._send(b'{"ok":1}', "application/json")
        elif p.path.startswith("/clip/"):
            fn = urllib.parse.unquote(p.path[6:])
            path = os.path.join(DIR, os.path.basename(fn))
            try:
                self._send(open(path, "rb").read(), "audio/wav")
            except OSError:
                self.send_error(404)
        else:
            self._send(PAGE.encode(), "text/html; charset=utf-8")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        DIR = sys.argv[1].rstrip("/")
    n = len(clips())
    print(f"{n} clips from {DIR}/  ->  http://127.0.0.1:{PORT}/")
    print(f"answers saved to {DIR}/review.json on every click")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
