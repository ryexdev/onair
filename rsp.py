#!/usr/bin/env python3
"""SDRplay RSP1B, behind the same interface as rtl.py.

    r = Rsp()                      # starts/attaches to the SDRconnect server
    r.tune(162.550e6)
    x = r.read(n)                  # n complex64 samples, DC-centred
    r.close()

WHY THIS IS NOT A CTYPES WRAPPER LIKE rtl.py
============================================
librtlsdr is open source and speaks USB directly. The RSP1B does not have that
option here: SDRplay's own API daemon (3.15.1) cannot initialise this hardware
revision on macOS 26 — it selects configuration 1 and then times out on
endpoint 0 partway through the firmware upload, every time, on every cable that
works elsewhere. See docs/rsp1b-macos.md for the full diagnosis.

SDRconnect, SDRplay's own application, embeds its own copy of that stack and
DOES drive the device. It also exposes a documented WebSocket API (1.0.3) for
exactly this purpose. So the path is:

    scan.py  ->  rsp.py  ->  websocket  ->  SDRconnect_headless  ->  RSP1B

The WebSocket client here is written from scratch against the spec because the
alternative was a dependency, and the whole project is numpy-only on purpose.

WHAT WAS MEASURED, AND WHY THE CONSTANTS ARE WHAT THEY ARE
==========================================================
Every number below was measured on this hardware, not taken from the datasheet.

RATE = 6 Msps, not the 10 Msps the device will happily accept. The ADC changes
resolution with sample rate: 14-bit native only up to 6.048 MSPS, 12-bit to
8.064, 10-bit to 9.216, 8-bit above. Confirmed by capturing at both rates and
counting distinct sample values: 5480 at 6 Msps against 2046 at 10 Msps — 2046
being 2^11 almost exactly. Running at 10 Msps trades ~1.4 bits, roughly 8.5 dB
of dynamic range, for 4 MHz of bandwidth. The dynamic range is the entire
reason this hardware is better than an RTL-SDR, so it is not a trade worth
making.

USABLE = 0.85. Measured IF response at 6 Msps, referenced to the middle 30%:

    keep 70%  (4.20 MHz)  -0.96 dB
    keep 80%  (4.80 MHz)  -1.05 dB
    keep 85%  (5.10 MHz)  -1.25 dB     <- here
    keep 90%  (5.40 MHz)  -5.80 dB     <- filter corner
    keep 95%  (5.70 MHz) -21.41 dB

5.1 MHz usable against the RTL-SDR's 1.92, at comparable flatness.

Retune measured at a 2.9 ms median (min 2.7, max 13.3) over eight tunes
spanning 144 MHz to 1090 MHz — an order of magnitude faster than the ~28 ms
the RTL-SDR needs, which is what makes a per-step retune cheap.

Streaming was verified clean at 6, 8 and 10 Msps: 57 million samples in 30 s at
a flat 2,000,160/second with zero dropouts. An earlier test that appeared to
show the hardware dropping out after 7 seconds was a slow reader in this file's
first draft — Python re-slicing a growing buffer at 8 MB/s. The fix is the
bytearray + del pattern in _fill(), and it matters: get it wrong and the
symptom looks exactly like failing hardware.

WHAT THE WEBSOCKET API DOES NOT EXPOSE
======================================
Checked by probing: no IF bandwidth selection, no notch filters (the device has
FM/MW/DAB notches), no bias-T, no decimation, no ppm trim. Only centre
frequency, sample rate and LNA state are settable. Two useful things it does
expose that the RTL-SDR never could: `overload`, a hardware overload flag we
previously had to infer from sample statistics, and `signal_power`, a
calibrated absolute measurement in dBm.
"""
import base64
import json
import os
import socket
import struct
import subprocess
import time

import numpy as np

WS_PORT = 9002
SERVER = "/Applications/SDRconnect.app/Contents/MacOS/SDRconnect_headless"

RATE = 6_000_000            # 14-bit native ceiling; see module docstring
USABLE = 0.85               # 5.1 MHz, measured -1.25 dB at the edge
F_MIN, F_MAX = 1_000, 2_000_000_000
LNA_MIN, LNA_MAX = 0, 8     # reported by the device


class Rsp:
    """Same shape as rtl.Rtl: tune / read / flush / close."""

    def __init__(self, index=0, rate=RATE, gain_db=None, port=WS_PORT,
                 start_server=True):
        self.rate = int(rate)
        self.port = port
        self._buf = bytearray()          # raw websocket bytes
        self._iq = bytearray()           # decoded int16 IQ awaiting read()
        self._srv = None
        if start_server and not _server_up(port):
            self._srv = _start_server(port)
        self._connect()
        self._select_device(index)
        # SDRconnect is a full receiver: without this it demodulates and plays
        # audio out of the speakers while we sweep.
        self._send("audio_stream_enable", "", "false")
        self._set("audio_mute", "true")
        self._set("audio_volume_percent", "0")
        self._set("device_sample_rate", str(self.rate))
        if gain_db is not None:
            self.set_gain(gain_db)
        self._send("device_stream_enable", "", "true")
        self._send("iq_stream_enable", "", "true")
        self._settle()

    # ---- websocket plumbing ------------------------------------------------

    def _connect(self):
        self.s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            d = self.s.recv(4096)
            if not d:
                raise RuntimeError("SDRconnect closed during handshake")
            head += d
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError("websocket handshake refused")
        self._buf.extend(head.split(b"\r\n\r\n", 1)[1])

    def _fill(self, n):
        # bytearray + del, never buf = buf[n:]. Re-slicing a growing buffer is
        # O(n^2) and at 24 MB/s it silently falls behind, which stalls the
        # server and shows up as USB transaction errors — i.e. it looks like a
        # hardware fault.
        while len(self._buf) < n:
            d = self.s.recv(1 << 18)
            if not d:
                raise RuntimeError("SDRconnect closed the stream")
            self._buf.extend(d)

    def _take(self, n):
        self._fill(n)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _frame(self):
        h = self._take(2)
        op, ln = h[0] & 0x0F, h[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._take(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._take(8))[0]
        return op, self._take(ln)

    def _send(self, event, prop, value, device="primary"):
        msg = json.dumps({"event_type": event, "property": prop,
                          "device": device, "value": str(value)}).encode()
        n, mask = len(msg), os.urandom(4)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        hdr += mask
        self.s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4]
                                          for i, b in enumerate(msg)))

    def _set(self, prop, value):
        self._send("set_property", prop, value)

    def get(self, prop, timeout=2.0):
        """Read a property. Ignores property_changed pushes, which carry the
        same property name and would otherwise return a stale queued value."""
        self._send("get_property", prop, "")
        end = time.time() + timeout
        while time.time() < end:
            self.s.settimeout(max(0.05, end - time.time()))
            try:
                op, pay = self._frame()
            except (socket.timeout, RuntimeError):
                return None
            if op == 0x1:
                try:
                    m = json.loads(pay)
                except Exception:
                    continue
                if (m.get("event_type") == "get_property_response"
                        and m.get("property") == prop):
                    return m.get("value")
            elif op == 0x2:
                self._stash(pay)
        return None

    # ---- device ------------------------------------------------------------

    def _select_device(self, index):
        names = self.get("valid_devices") or ""
        if "RSP" not in names:
            raise RuntimeError(
                f"SDRconnect sees no RSP (valid_devices={names!r}). "
                "Another app holding the device, or it needs a replug.")
        self._send("selected_device", "", str(index))
        time.sleep(1.2)
        self.name = self.get("active_device")

    def set_gain(self, gain_db):
        """Mapped onto the 9 LNA states. Kept named set_gain so callers written
        against rtl.py do not have to care which radio they are talking to."""
        st = int(round(max(LNA_MIN, min(LNA_MAX, gain_db))))
        self._set("lna_state", str(st))

    def tune(self, hz):
        hz = int(hz)
        if not (F_MIN <= hz <= F_MAX):
            raise ValueError(f"{hz/1e6:.3f} MHz outside 0.001-2000 MHz")
        self._set("device_center_frequency", str(hz))

    def overloaded(self):
        return (self.get("overload") or "false") == "true"

    def power_dbm(self):
        """Calibrated absolute power. The RTL-SDR path has no equivalent."""
        v = self.get("signal_power")
        return float(v) if v is not None else float("nan")

    # ---- sample flow -------------------------------------------------------

    def _stash(self, pay):
        if len(pay) >= 2 and struct.unpack("<H", pay[:2])[0] == 2:
            self._iq.extend(pay[2:])          # int16 interleaved, primary

    def _settle(self, secs=1.0):
        end = time.time() + secs
        while time.time() < end:
            self.s.settimeout(max(0.05, end - time.time()))
            try:
                op, pay = self._frame()
            except Exception:
                break
            if op == 0x2:
                self._stash(pay)
        self._iq.clear()

    def flush(self):
        """Drop everything captured before now.

        The RTL-SDR version discards a queued USB buffer. Here the stream is
        free-running, so after a tune the pipe still holds samples from the
        OLD frequency — dropping them is what makes tune() mean anything.
        """
        self._iq.clear()
        self.s.settimeout(0.0)
        try:
            while True:
                d = self.s.recv(1 << 18)
                if not d:
                    break
                self._buf.extend(d)
        except (BlockingIOError, socket.error):
            pass
        # keep frame alignment: decode what arrived, then throw it away
        self.s.settimeout(0.2)
        try:
            while len(self._buf) >= 2:
                op, pay = self._frame()
                if op == 0x2:
                    self._stash(pay)
        except Exception:
            pass
        self._iq.clear()

    def read(self, n_samples):
        """n complex64 samples, scaled to roughly +/-1 like rtl.py's read()."""
        need = int(n_samples) * 4                 # 2 ch x int16
        self.s.settimeout(5.0)
        while len(self._iq) < need:
            op, pay = self._frame()
            if op == 0x8:
                raise RuntimeError("SDRconnect closed the stream")
            if op == 0x2:
                self._stash(pay)
        raw = np.frombuffer(bytes(self._iq[:need]), dtype="<i2")
        del self._iq[:need]
        v = raw.astype(np.float32) / 32768.0
        return (v[0::2] + 1j * v[1::2]).astype(np.complex64)

    def close(self):
        try:
            self._send("iq_stream_enable", "", "false")
            self._send("device_stream_enable", "", "false")
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass
        if self._srv is not None:
            self._srv.terminate()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ---- server management ------------------------------------------------------

def _server_up(port=WS_PORT):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def _start_server(port=WS_PORT, wait=25.0):
    if not os.path.exists(SERVER):
        raise RuntimeError(f"SDRconnect not found at {SERVER}")
    p = subprocess.Popen([SERVER, f"--websocket_port={port}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = time.time() + wait
    while time.time() < end:
        if _server_up(port):
            time.sleep(2.0)               # let it enumerate the device
            return p
        time.sleep(0.5)
    p.terminate()
    raise RuntimeError("SDRconnect_headless did not come up")


def find(_hint=None):
    """Mirrors rtl.find(): returns an index or None."""
    if not _server_up():
        return 0 if os.path.exists(SERVER) else None
    try:
        r = Rsp(start_server=False)
        ok = "RSP" in (r.name or "")
        r.close()
        return 0 if ok else None
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    f = float(sys.argv[1]) * 1e6 if len(sys.argv) > 1 else 162.550e6
    with Rsp() as r:
        print(f"device   {r.name}")
        print(f"rate     {r.rate/1e6:.3f} Msps   usable {r.rate*USABLE/1e6:.3f} MHz")
        r.tune(f)
        r.flush()
        t0 = time.time()
        x = r.read(int(0.2 * r.rate))
        el = time.time() - t0
        p = 10 * np.log10(np.mean(np.abs(x) ** 2) + 1e-20)
        print(f"tuned    {f/1e6:.4f} MHz")
        print(f"read     {len(x)} samples in {el:.3f}s   mean power {p:.1f} dBFS")
        print(f"overload {r.overloaded()}   calibrated {r.power_dbm():.1f} dBm")
