#!/usr/bin/env python3
"""Live signal meter — move the antenna, watch the number.

    python3 meter.py            NOAA 162.550, the best reference there is
    python3 meter.py 146.94     any frequency

    -> http://127.0.0.1:8704/

Updates about 8 times a second, because tuning an antenna by moving it and
waiting is hopeless — you need to see the effect while your hand is still on it.

NOAA is the default deliberately: it transmits continuously and never moves, so
every change in its level is something YOU did. A repeater or a station can
vary on its own and will send you chasing ghosts.
"""
import http.server, json, sys, threading, time
import numpy as np
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scan, rtl

PORT = 8704
BLOCK = 0.05                  # 50 ms per reading
HIST = 400                    # ~50 s of history

state = {"snr": 0.0, "peak": 0.0, "floor": 0.0, "freq": 0.0,
         "hist": [], "best": 0.0, "n": 0}
lock = threading.Lock()


def measure(freq_mhz):
    r = rtl.Rtl(rtl.find("R828D") or 0, scan.RATE, scan.GAIN_LADDER[-2])
    nfft = 1024
    n = int(BLOCK * scan.RATE) // nfft * nfft
    off = 300_000
    r.tune(freq_mhz * 1e6 - off)
    r.flush()
    bin_f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / scan.RATE))
    on = np.abs(bin_f - off) < 8000                 # the channel
    near = (np.abs(bin_f - off) > 40_000) & (np.abs(bin_f) < scan.RATE * 0.4)
    try:
        while True:
            x = r.read(n)
            X = np.fft.fftshift(np.fft.fft(x.reshape(-1, nfft)
                                           * np.hanning(nfft), axis=1), axes=1)
            P = (np.abs(X) ** 2).mean(axis=0) + 1e-20
            db = 10 * np.log10(P)
            pk = float(db[on].max())
            fl = float(np.median(db[near]))
            snr = pk - fl
            with lock:
                state["snr"] = round(snr, 1)
                state["peak"] = round(pk, 1)
                state["floor"] = round(fl, 1)
                state["freq"] = freq_mhz
                state["n"] += 1
                state["best"] = round(max(state["best"], snr), 1)
                h = state["hist"]
                h.append(round(snr, 1))
                del h[:-HIST]
    finally:
        r.close()


PAGE = """<!doctype html><meta charset=utf-8><title>signal</title>
<style>
body{font:14px -apple-system,system-ui,sans-serif;background:#0d0f12;color:#e6eaef;
     margin:0;padding:26px;max-width:640px}
h1{font-size:15px;margin:0 0 2px;font-weight:600}
.sub{color:#6b7480;font-size:11px;margin-bottom:22px}
.big{font:700 76px/1 ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums;
     letter-spacing:-2px}
.u{font-size:19px;color:#6b7480;font-weight:400;letter-spacing:0}
.bar{height:30px;background:#171c22;border-radius:6px;overflow:hidden;margin:14px 0 6px;
     position:relative}
.fill{height:100%;transition:width .09s linear;border-radius:6px}
.mark{position:absolute;top:0;bottom:0;width:2px;background:#6d7683}
.meta{color:#7d8794;font-size:11px;font-variant-numeric:tabular-nums}
svg{display:block;margin-top:16px;background:#12161b;border-radius:6px;width:100%}
.d{font-size:13px;margin-top:12px;color:#8b95a3}
.d b{font-size:15px}
.up{color:#4ec27a}.dn{color:#c9776f}
</style>
<h1 id=ttl>signal</h1>
<div class=sub>move the antenna &mdash; the number follows immediately. best so far marked on the bar.</div>
<div class=big id=v>--<span class=u> dB SNR</span></div>
<div class=bar><div class=fill id=f></div><div class=mark id=m style=left:0></div></div>
<div class=meta id=meta></div>
<svg id=g height=90 viewBox="0 0 400 90" preserveAspectRatio=none></svg>
<div class=d id=d></div>
<script>
function col(s){return s>=25?'#3ddc84':s>=15?'#9ec24e':s>=8?'#c2a24e':'#c2704e'}
async function tick(){
  var d=await(await fetch('/m')).json();
  document.getElementById('ttl').textContent=d.freq.toFixed(4)+' MHz';
  document.getElementById('v').innerHTML=d.snr.toFixed(1)+'<span class=u> dB SNR</span>';
  var pc=Math.max(0,Math.min(100,d.snr/40*100));
  var f=document.getElementById('f');
  f.style.width=pc+'%'; f.style.background=col(d.snr);
  document.getElementById('m').style.left=Math.min(100,d.best/40*100)+'%';
  document.getElementById('meta').textContent=
    'peak '+d.peak+' dB   floor '+d.floor+' dB   best '+d.best+' dB';
  var h=d.hist;
  if(h.length>2){
    var mx=Math.max(40,Math.max.apply(null,h)),n=h.length,p='';
    for(var i=0;i<n;i++)p+=(i*(400/(n-1))).toFixed(1)+','+(90-h[i]/mx*90).toFixed(1)+' ';
    document.getElementById('g').innerHTML=
      '<polyline fill=none stroke="'+col(h[h.length-1])+'" stroke-width=1.5 points="'+p+'"/>';
    var recent=h.slice(-12).reduce(function(a,b){return a+b},0)/Math.min(12,h.length);
    var older=h.slice(-60,-12);
    if(older.length>4){
      var o=older.reduce(function(a,b){return a+b},0)/older.length, dd=recent-o;
      document.getElementById('d').innerHTML= Math.abs(dd)<0.8
        ? 'holding steady'
        : (dd>0?'<b class=up>+'+dd.toFixed(1)+' dB better</b> than a moment ago'
               :'<b class=dn>'+dd.toFixed(1)+' dB worse</b> than a moment ago');
    }
  }
}
tick();setInterval(tick,120);
</script>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/m"):
            with lock:
                body, ct = json.dumps(state).encode(), "application/json"
        else:
            body, ct = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    f = float(sys.argv[1]) if len(sys.argv) > 1 else 162.5500
    threading.Thread(target=measure, args=(f,), daemon=True).start()
    print(f"signal meter on {f:.4f} MHz -> http://127.0.0.1:{PORT}/  (Ctrl-C stops)")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
