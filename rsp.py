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
import threading
import time

import numpy as np

WS_PORT = 9002
SERVER = "/Applications/SDRconnect.app/Contents/MacOS/SDRconnect_headless"

RATE = 6_000_000            # 14-bit native ceiling; see module docstring
USABLE = 0.85               # 5.1 MHz, measured -1.25 dB at the edge
F_MIN, F_MAX = 1_000, 2_000_000_000
LNA_MIN, LNA_MAX = 0, 8     # reported by the device
# Must comfortably exceed the LARGEST single read anyone asks for, or that
# read can never be satisfied and simply hangs. The verify path wants
# VERIFY_SECS (1.2 s) in one go, which at 6 Msps is 28.8 MB — a 16 MB ring
# stalled the scanner at lap 4 with no error at all.
_RING_BYTES = 96 << 20        # ~4 s at 6 Msps

# SDRconnect's pipeline holds samples between the tuner and the socket, so a
# frequency change does not appear at our end for a while. Clearing the ring is
# NOT enough: the very next bytes to arrive are still the OLD frequency.
# Measured by alternating a loud FM centre against a dead one and asking how
# often a sweep-sized read (98,304 samples) came back from the wrong place:
#
#     settle   0 ms  ->  8 of 12 reads were the previous frequency
#             20 ms  ->  9 of 12          (still lagging by a whole step)
#             40 ms  ->  9 of 12
#             60 ms  ->  0 of 12          <- locks here
#             80 ms  ->  0 of 12
#            120 ms  ->  0 of 12
#
# 80 ms for margin. This was THE detection bug on this hardware: every sweep
# step was analysing the spectrum of the step before it, so nothing landed on
# the frequency it was attributed to and strong narrow signals — NOAA on
# 162.5500 at 43 dB — were simply never seen.
SETTLE_S = 0.08


def _frame_len(buf):
    """Total byte length of the websocket frame at the head of buf, or None if
    the header itself is not fully present yet."""
    if len(buf) < 2:
        return None
    ln = buf[1] & 0x7F
    off = 2
    if ln == 126:
        if len(buf) < 4:
            return None
        ln = struct.unpack(">H", bytes(buf[2:4]))[0]; off = 4
    elif ln == 127:
        if len(buf) < 10:
            return None
        ln = struct.unpack(">Q", bytes(buf[2:10]))[0]; off = 10
    if buf[1] & 0x80:                      # masked (servers do not mask)
        off += 4
    return off + ln


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
        # A DEDICATED READER. The stream is free-running at ~24 MB/s, so every
        # millisecond the caller spends in analyse() is a millisecond nobody is
        # draining the socket. Left to itself the window fills, the server
        # stalls, and read() eventually times out — which is exactly how this
        # failed: a bare tune/flush/read loop ran all 392 steps happily, and
        # the identical loop with FFTs in between died within a lap.
        # Blocking, no timeout. The 10 s timeout create_connection() left on
        # the socket applies to the reader too, and the gap between enabling
        # the stream and the first frame can exceed it — which killed the
        # reader on startup and forced a needless reopen on every launch.
        self.s.settimeout(None)
        self._lock = threading.Lock()
        self._stop = False
        self._err = None
        self._rx = threading.Thread(target=self._reader, daemon=True)
        self._rx.start()
        # Wait for the stream to actually start rather than assuming a fixed
        # delay is enough. SDRconnect can take several seconds to go from
        # "stream enabled" to the first frame, and a fixed 0.6 s sleep meant
        # the caller's first read() timed out and triggered a pointless
        # reopen on every single launch.
        end = time.time() + 15.0
        while time.time() < end:
            with self._lock:
                if self._iq:
                    break
            if self._err is not None:
                raise RuntimeError(f"stream never started: {self._err!r}")
            time.sleep(0.05)
        self.flush()

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
        if getattr(self, "_rx", None) is not None:
            return self._get_threaded(prop, timeout)
        return self._get_direct(prop, timeout)

    def _get_threaded(self, prop, timeout):
        """Once the reader thread owns the socket, control replies come back
        through it rather than being read here."""
        self._want = (prop, None)
        self._send("get_property", prop, "")
        end = time.time() + timeout
        while time.time() < end:
            if self._want[1] is not None:
                return self._want[1]
            time.sleep(0.005)
        return None

    def _get_direct(self, prop, timeout=2.0):
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
        # Clamp rather than raise. A sweep step is CENTRED on its slice, so the
        # last step of a band legitimately asks for a centre up to half a span
        # past the band edge — 2002 MHz when sweeping to 2000. Refusing that
        # killed the sweep on its first lap; clamping just means the top half
        # slice is slightly narrower, which is what you want.
        hz = int(min(max(int(hz), F_MIN), F_MAX))
        self._set("device_center_frequency", str(hz))
        self._settled_at = time.time() + SETTLE_S

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

    def _reader(self):
        """Drain the socket forever, into a bounded ring of recent IQ.

        Bounded on purpose: if the consumer is slow we want to throw away OLD
        samples and keep current ones, not grow without limit. A sweep only
        ever wants the most recent slice anyway.
        """
        try:
            while not self._stop:
                op, pay = self._frame()
                if op == 0x8:
                    raise RuntimeError("SDRconnect closed the stream")
                if op == 0x1:
                    want = getattr(self, "_want", None)
                    if want and want[1] is None:
                        try:
                            m = json.loads(pay)
                        except Exception:
                            m = {}
                        if (m.get("event_type") == "get_property_response"
                                and m.get("property") == want[0]):
                            self._want = (want[0], m.get("value"))
                    continue
                if op == 0x2 and len(pay) >= 2 and \
                        struct.unpack("<H", pay[:2])[0] == 2:
                    with self._lock:
                        self._iq.extend(pay[2:])
                        if len(self._iq) > _RING_BYTES:
                            del self._iq[:len(self._iq) - _RING_BYTES]
        except Exception as e:
            self._err = e

    def flush(self):
        """Drop everything captured before now.

        The RTL-SDR version discards a queued USB buffer. Here the stream is
        free-running, so after a tune the pipe still holds samples from the OLD
        frequency, and dropping them is what makes tune() mean anything.

        Waits out SETTLE_S first. Callers already do tune/flush/read, so
        putting the wait here means every one of them gets it for free and none
        of them has to know the hardware needs it. Time spent waiting is not
        wasted anyway — the reader thread is filling the ring while we sit.
        """
        left = getattr(self, "_settled_at", 0) - time.time()
        if left > 0:
            time.sleep(left)
        with self._lock:
            self._iq.clear()

    def read(self, n_samples):
        """n complex64 samples, scaled to roughly +/-1 like rtl.py's read()."""
        need = int(n_samples) * 4                 # 2 ch x int16
        end = time.time() + 5.0
        while True:
            if self._err is not None:
                raise RuntimeError(f"stream reader died: {self._err!r}")
            with self._lock:
                if len(self._iq) >= need:
                    raw = np.frombuffer(bytes(self._iq[:need]), dtype="<i2")
                    del self._iq[:need]
                    break
            if time.time() > end:
                raise RuntimeError("no IQ from SDRconnect for 5 s")
            time.sleep(0.001)
        v = raw.astype(np.float32) / 32768.0
        return (v[0::2] + 1j * v[1::2]).astype(np.complex64)

    def close(self):
        self._stop = True
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
