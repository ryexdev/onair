"""Minimal RTL-SDR access: open the device once, retune, read samples.

Deliberately tiny. No demod, no decoding, no threads. Just IQ in.
"""
import ctypes, ctypes.util
import numpy as np

_paths = ["/opt/homebrew/lib/librtlsdr.dylib", ctypes.util.find_library("rtlsdr")]
for _p in _paths:
    if not _p:
        continue
    try:
        lib = ctypes.CDLL(_p)
        break
    except OSError:
        continue
else:
    raise RuntimeError("librtlsdr not found (brew install librtlsdr)")

_dev_p = ctypes.c_void_p
lib.rtlsdr_open.argtypes = [ctypes.POINTER(_dev_p), ctypes.c_uint32]
lib.rtlsdr_get_device_name.restype = ctypes.c_char_p
lib.rtlsdr_read_sync.argtypes = [_dev_p, ctypes.c_void_p, ctypes.c_int,
                                 ctypes.POINTER(ctypes.c_int)]

TUNERS = {0: "unknown", 1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580",
          5: "R820T", 6: "R828D"}


def devices():
    """[(index, name, tuner_chip)] for every dongle attached."""
    out = []
    for i in range(lib.rtlsdr_get_device_count()):
        name = lib.rtlsdr_get_device_name(i).decode(errors="replace")
        d = _dev_p()
        tuner = "?"
        if lib.rtlsdr_open(ctypes.byref(d), i) == 0:
            tuner = TUNERS.get(lib.rtlsdr_get_tuner_type(d), "?")
            lib.rtlsdr_close(d)
        out.append((i, name, tuner))
    return out


def find(tuner):
    """Index of the first dongle with this tuner chip. The two dongles share
    serial 00000001, so the chip is the only reliable way to tell them apart:
    R828D = Blog V4, R820T = the generic one."""
    for i, _, t in devices():
        if t == tuner:
            return i
    return None


class Rtl:
    def __init__(self, index=0, rate=2_400_000, gain_db=40.0):
        self.dev = _dev_p()
        if lib.rtlsdr_open(ctypes.byref(self.dev), index) != 0:
            raise RuntimeError(f"cannot open rtlsdr #{index} (in use?)")
        self.index = index
        self.tuner = TUNERS.get(lib.rtlsdr_get_tuner_type(self.dev), "?")
        lib.rtlsdr_set_sample_rate(self.dev, int(rate))
        lib.rtlsdr_set_tuner_gain_mode(self.dev, 1)
        lib.rtlsdr_set_tuner_gain(self.dev, int(gain_db * 10))
        lib.rtlsdr_set_agc_mode(self.dev, 0)
        self.gain = gain_db
        self.rate = rate
        lib.rtlsdr_reset_buffer(self.dev)

    def set_gain(self, gain_db):
        """No-op when unchanged — this is called every step."""
        if abs(gain_db - self.gain) < 0.05:
            return
        lib.rtlsdr_set_tuner_gain(self.dev, int(gain_db * 10))
        self.gain = gain_db

    def tune(self, hz):
        lib.rtlsdr_set_center_freq(self.dev, int(hz))
        self.freq = int(hz)

    def read(self, n_samples):
        """n_samples complex samples as complex64, centred on 0.

        librtlsdr wants the byte count to be a multiple of 512, so the request
        is rounded down to the nearest whole USB block rather than failing."""
        n_bytes = max((n_samples * 2) // 512, 1) * 512
        buf = (ctypes.c_ubyte * n_bytes)()
        got = ctypes.c_int()
        if lib.rtlsdr_read_sync(self.dev, buf, n_bytes, ctypes.byref(got)) != 0:
            raise RuntimeError("read_sync failed")
        raw = np.frombuffer(buf, np.uint8, count=got.value).astype(np.float32)
        raw = (raw - 127.5) / 127.5
        return raw[0::2] + 1j * raw[1::2]

    def flush(self):
        """Drop stale samples queued from before the last retune."""
        lib.rtlsdr_reset_buffer(self.dev)
        self.read(4096)

    def close(self):
        if self.dev:
            lib.rtlsdr_close(self.dev)
            self.dev = None
