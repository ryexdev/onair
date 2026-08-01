#!/usr/bin/env python3
"""Fast spectrum scan that separates real transmissions from noise by SHAPE.

Nothing here decodes anything. The question is only "is this coherent, and is
somebody actually using it" — answered with cheap statistics.

Why this works: noise is structureless. It is spread out, and it is random from
one moment to the next, so a noise peak is somewhere else 20 ms later. Anything
transmitted is the opposite: narrow, sharp-edged, and it STAYS PUT. Measuring
that needs no idea what the signal means.

    python3 scan.py                  # default slice (vhf)
    python3 scan.py vhf uhf --web    # several bands + the board
    python3 scan.py 144 166          # any raw MHz range
    python3 scan.py all

Thresholds here are measured, not guessed — see tune.py.
"""
import http.server, json, math, os, random, sys, threading, time
import numpy as np

# --- what to sweep --------------------------------------------------------
# Just ranges. There is deliberately NO channel plan anywhere in this program:
# every frequency reported is discovered from the spectrum itself. Add a line
# here and it is scanned; nothing else needs to change.
BANDS = {
    "vhf":    (144.0, 166.0, "2m ham, railroad, marine, NOAA"),
    "air":    (118.0, 137.0, "airband"),
    "pub":    (150.0, 174.0, "public service / business VHF"),
    "uhf":    (440.0, 470.0, "70cm ham, GMRS, business"),
    "p800":   (851.0, 869.0, "800 MHz public safety"),
    "ism900": (902.0, 928.0, "900 MHz ISM — sensors, telemetry"),
    "pager":  (929.0, 932.0, "pagers"),
    "hf":     (0.5, 30.0, "HF — shortwave, ham, utility (RSP1B only)"),
    "lband":  (1766.0, 2000.0, "L-band above the RTL-SDR ceiling (RSP1B only)"),
    # everything the tuner reaches. The span depends on which radio is
    # attached: the RTL-SDR V4 stops at 1766 MHz and cannot do HF without the
    # rtlsdr-blog driver fork, while the RSP1B covers 1 kHz - 2 GHz with no
    # gaps. FULL_SPAN below picks the right one.
    "full":   (24.0, 1766.0, "the whole tunable spectrum"),
}
DEFAULT_BANDS = ["vhf"]

# --- radio ------------------------------------------------------------------
#
# Two backends, same interface (tune/read/flush/close/set_gain). The RSP1B is
# preferred when present because it is better on every axis we measured, all
# on the same antenna within minutes of each other:
#
#                     RTL-SDR V4      RSP1B
#   usable per tune   1.92 MHz        5.10 MHz
#   coverage          24-1766 MHz     0.1-2000 MHz
#   retune            ~28 ms          2.9 ms
#   ADC               8-bit           14-bit (at 6 Msps)
#   NOAA 162.55 SNR   38.7 dB         43.0 dB
#
# Every constant below that depends on the radio is set from the backend
# rather than hardcoded, because none of them transfer.
try:
    import rsp as _rsp
except Exception:
    _rsp = None
import rtl as _rtl


def _pick_backend():
    if os.environ.get("ONAIR_RADIO", "").lower() == "rtl":
        return "rtl"
    if _rsp is not None and _rsp.find() is not None:
        return "rsp"
    return "rtl"


BACKEND = _pick_backend()

if BACKEND == "rsp":
    radio = _rsp
    RATE   = _rsp.RATE            # 6 Msps: the 14-bit native ceiling. Above
    USABLE = _rsp.USABLE          # 6.048 Msps the ADC drops to 12/10/8 bits.
    NFFT   = 4096                 # -> 1.46 kHz bins, finer than the RTL path
    FRAMES = 24                   # 4096*24/6e6 = 16 ms, same dwell as before
    GAIN_DB = 2.0                 # LNA state; 2 measured best on NOAA
    DC_NOTCH = 12_000
    # 1 kHz - 2 GHz, no gaps, no upconverter needed.
    FULL_SPAN = (0.5, 2000.0)
else:
    radio = _rtl
    RATE      = 2_400_000
    USABLE    = 0.80          # tuner rolls off at the edges of each capture
    NFFT      = 1024          # -> 2.34 kHz bins
    FRAMES    = 48            # -> ~20 ms dwell. Cost is the retune, not this.
    GAIN_DB   = 40.0
    DC_NOTCH  = 12_000        # the tuner leaks a spike at exactly centre
    FULL_SPAN = (24.0, 1766.0)

BANDS["full"] = (FULL_SPAN[0], FULL_SPAN[1], "the whole tunable spectrum")

# --- what counts as a signal ------------------------------------------------
# Measured, not guessed: with the antenna disconnected AND on synthetic noise,
# this detector produced 0 candidates in 80 captures with the gate at 3.0 dB.
# 6.0 was costing ~9% of real detections for no measured benefit. (0/80 is not
# proof of zero — 95% upper bound is ~0.04/capture — but CONFIRM_LAPS is what
# actually rejects those: a fluke does not repeat on the same channel.)
SNR_MIN        = 4.0          # dB above the local floor to even look at
# Width limits are expressed in HERTZ and converted to bins below. They used to
# be bin counts, which silently meant different bandwidths on different
# hardware: 128 bins is 300 kHz at the RTL-SDR's 2.34 kHz bins but only 187 kHz
# at the RSP1B's 1.46 kHz bins, so the same number would have quietly narrowed
# what counts as a channel.
MIN_WIDTH_HZ   = 4_000        # narrower than this is a spur or a noise spike
MAX_WIDTH_HZ   = 300_000      # wider is broadband junk, not a channel
SCORE_MIN      = 0.50
CONFIRM_LAPS   = 2            # must reappear this many laps to be called real
# 3 was costing the channels this program exists to find. A step is sampled for
# 16 ms once per ~39 s lap, so P(sighting per lap) is roughly the duty cycle,
# and needing THREE sightings before UNCONFIRMED_S expires is a compound bet
# against anything intermittent. Monte-Carlo of the real Schedule/Tracker rules
# on a cold step, median time to confirm and the share confirmed within 43 h:
#
#     duty    600s/3        3600s/3       3600s/2
#      5%     187 min        fast          fast
#      2%      18 h (60%)    9.5 h (98%)   5.5 h (100%)
#      1%      37 h (18%)     27 h (56%)    15 h ( 95%)
#    0.5%        - ( 2%)        - ( 9%)      27 h ( 56%)
#
# That band — a channel used a couple of times an hour — is most business,
# ham, GMRS and fire/EMS tactical traffic. It was effectively invisible.
# The false positives 3 was guarding against are already handled better by the
# per-lap +/-100 kHz jitter, which moves a tuner spur and leaves a real signal
# where it is; persistence was never the thing catching those.
TRACK_TOL      = 8_000        # Hz; same signal seen again
FORGET_S       = 3_600.0      # confirmed signals: forgotten an hour after
                              # they were last heard
UNCONFIRMED_S  = 3_600.0      # a candidate that appeared once and never came
                              # back within an HOUR was noise. Dropping these
                              # keeps memory flat: they are ~60% of all tracks
                              # and none of them ever become anything.
                              # Was 600 s, which is only ~15 laps — a channel
                              # used twice an hour had its count reset before
                              # it could ever reach CONFIRM_LAPS. See the
                              # table above: this is the other half of that
                              # change and the two only work together.
FADE_S         = 300.0        # green -> grey over 5 minutes
# 2.5 kHz, not 12.5. Every raster actually in use divides evenly into 2.5 —
# 5 (ham tuning steps), 12.5 and 6.25 (land mobile, GMRS), 15 and 20 (2 m
# repeaters), 25 (70 cm, marine, airband). A 12.5 kHz grid cannot represent a
# 15 kHz raster at all: 147.435 became 147.4375, 145.220 became 145.2250,
# 446.640 became 446.6375. Those numbers match nothing in RepeaterBook, which
# is exactly the frequency you need to key into a radio.
SNAP_HZ        = 2_500        # channel grid. Our bins are 2.34 kHz
                              # wide, so a measured peak lands within ~1 kHz of
                              # truth; snapping puts it on the real channel.
WEB_PORT       = 8701

BIN_HZ = RATE / NFFT
MIN_WIDTH_BINS = max(2, int(round(MIN_WIDTH_HZ / BIN_HZ)))
MAX_WIDTH_BINS = max(8, int(round(MAX_WIDTH_HZ / BIN_HZ)))

# The local-floor window is also in bins, and must stay wider than the widest
# signal we accept or a wide signal sits in its own reference window and raises
# its own floor. See local_floor().
CFAR_GUARD_HZ = 169_000   # 72 bins on the RTL-SDR, the value validated there

# --- spurs the dongle generates itself --------------------------------------
# Measured with the antenna DISCONNECTED, so nothing here can be a real signal.
# Almost all are harmonics of the 28.8 MHz reference oscillator, plus the USB
# clock at 120 MHz. They matter because they defeat every other defence in this
# program: they are strong (up to 29 dB), perfectly stable, and sit at a fixed
# ABSOLUTE frequency — which is exactly what a genuine transmitter looks like.
# The per-lap jitter cannot help, because that only moves spurs anchored to the
# tuner. The only honest fix is to measure them once and exclude them.
#
# Recalibrate after changing dongle or cabling:
#     (unplug antenna)  python3 scan.py full --web    -> rewrite spurs.json
SPUR_TOL_HZ = 20_000


# Spur lists are PER RADIO. The 30 measured spurs in spurs.json and the clock
# combs below were measured on one specific RTL-SDR dongle; applying them to
# the RSP1B would blind us at 144.000, 432.000, 120.000, 132.000 and
# 456/459/468 for no reason at all, since that hardware has different clocks.
SPURS_FILE = "spurs.json" if BACKEND != "rsp" else "spurs_rsp.json"


def load_spurs(path=None):
    path = path or SPURS_FILE
    try:
        with open(path) as f:
            return [v * 1e6 for v in json.load(f)["freqs_mhz"]]
    except Exception:
        return []


# Clock fundamentals whose harmonics are ours, not the air:
#   28.8 MHz  the dongle's own reference, measured with the antenna OFF
#   12.0 MHz  the USB clock, radiated by the computer and picked up BY the
#             antenna. Measured: every multiple 24/36/48/60/72/84/96 runs
#             7-31 dB SNR while 25/37/49/61/73 sit at 0.1-3.5. Critically this
#             one CANNOT be found by the antenna-off calibration — unplug the
#             antenna and it disappears, because the antenna is what hears it.
#             48.0000 was being reported as carrying data; it is the laptop.
#   27.0 MHz  a third comb, found while verifying "data" hits. 13 of 22
#             harmonics run 9-27 dB while controls 250 kHz away sit at ~0.
#             It had put 405.0 (as "wx balloon"), 459.0 ("business"), 297.0 and
#             162.0 on the board as CARRYING DATA. Also radiated — invisible
#             with the antenna off.
# 28.8 MHz is the RTL-SDR's own reference; 12 and 27 MHz are the computer's,
# radiated and picked up by the antenna. The RSP1B has a different reference,
# so its comb list starts EMPTY and is filled by an antenna-off calibration
# run rather than inherited. An inherited list is worse than no list: it
# silently deletes real channels.
if BACKEND == "rsp":
    # 12 MHz carries over, and ONLY 12 MHz. It is not the dongle's reference —
    # it is the computer's, radiated and picked up by whatever antenna is
    # attached, so changing the radio does not change it. Measured on the
    # RSP1B board: 60 of 405 strong hits were exact multiples of 12.000000 MHz,
    # and the comb was being believed:
    #
    #     360.0000 =  30 x 12 MHz   digital  53.9 dB
    #    1920.0025 = 160 x 12 MHz   voice    41.9
    #    1824.0000 = 152 x 12 MHz   voice    41.8
    #    1680.0000 = 140 x 12 MHz   data     41.5
    #      36.0000 =   3 x 12 MHz   voice    34.5
    #
    # 1536.0000 (= 128 x 12) was briefly mistaken for an Inmarsat downlink,
    # which is what a 38 dB carrier in the L-band looks like until you divide.
    #
    # 28.8 is NOT carried over: that is the RTL-SDR's own reference and this
    # hardware does not have it. 27.0 is not carried over either — it was
    # measured through the RTL and has not been re-measured here. An inherited
    # comb is worse than no comb, because it silently deletes real channels.
    CLOCKS_HZ = (12_000_000.0,)
else:
    CLOCKS_HZ = (28_800_000.0, 12_000_000.0, 27_000_000.0)


BOOKMARKS = "bookmarks.json"


def load_marks():
    """{freq_mhz: note}. Tolerates the old list-of-floats format."""
    try:
        d = json.load(open(BOOKMARKS))
    except Exception:
        return {}
    if isinstance(d, list):
        return {round(float(f), 4): "" for f in d}
    return {round(float(k), 4): str(v) for k, v in d.items()}


def save_marks(m):
    try:
        json.dump({f"{k:.4f}": v for k, v in sorted(m.items())},
                  open(BOOKMARKS, "w"), indent=1)
    except OSError:
        pass


marks = load_marks()


def is_spur(hz, spurs):
    """Measured spurs, plus any harmonic of a known local clock."""
    for s in spurs:
        if abs(hz - s) <= SPUR_TOL_HZ:
            return True
    for c in CLOCKS_HZ:
        h = hz / c
        if h >= 1 and abs(h - round(h)) * c <= SPUR_TOL_HZ:
            return True
    return False


# --- labels -----------------------------------------------------------------
# Where a frequency SITS in the band plan. This is context, not identification:
# nothing here is derived from the signal, so a hit at 462.6 is labelled GMRS
# because that is whose spectrum it is, not because anything was decoded.
# Specific entries first — 1090 must win over the general aviation range.
LABELS = [
    # Verified against published allocations rather than guessed. Sources:
    #   amateur band edges  - Wikipedia "Amateur radio frequency allocations",
    #                         which matches FCC Part 97
    #   30-144 MHz          - jneuhaus.com FCC index (cordless phone, model
    #                         control and radio astronomy sub-bands)
    #   328.6-450 MHz       - same, glide path / military / met aids
    #   GMRS                - FCC Part 95, 462.5625-462.7250 and 467.5625-...
    # Anything still uncertain is marked GUESS below. This table only LABELS a
    # frequency for display; nothing in the detector reads it.
    # --- specific, must win over the wider ranges that contain them ---
    (1089.5, 1090.5, "ADS-B"),      (1574.0, 1577.0, "GPS L1"),
    (162.4, 162.56, "NOAA wx"),     (406.0, 406.1, "emergency beacon"),
    (74.8, 75.2, "marker beacon"),  (462.5, 462.75, "GMRS/FRS"),
    (467.5, 467.75, "GMRS/FRS"),
    # MURS - the other unlicensed handheld service, FCC Part 95 subpart J.
    # Five discrete channels, not a range: the VHF business handhelds used by
    # stores, farms and event staff. They were labelled "public svc" because
    # they sit inside 150-156, which hid the most likely source of a hit there.
    (151.81, 151.83, "MURS"),       (151.87, 151.89, "MURS"),
    (151.93, 151.95, "MURS"),       (154.56, 154.58, "MURS"),
    (154.59, 154.61, "MURS"),
    # Part 90 low-power "dot" and "star" channels. These are the type-accepted
    # itinerant channels shipped in off-the-shelf business handhelds, so they
    # are the single most likely source of a hit anywhere in 150-470: retail
    # staff, warehouses, construction, event crews. Naming them is the whole
    # point of this exercise - they were buried in "public svc" and "business",
    # which are 6 and 20 MHz wide and say nothing.
    (151.615, 151.635, "biz Red Dot"),
    (151.945, 151.965, "biz Purple Dot"),
    (464.49, 464.51, "biz Brown Dot"),
    (464.54, 464.56, "biz Yellow Dot"),
    (467.84, 467.94, "biz star ch"),      # Silver/Gold/Red/Blue 467.850-.925
    # Marine channels where the frequency IS the identification. Ch16 is the
    # distress and calling channel and gets its own label for the same reason
    # 121.5 and 243.0 do. Ch70 is DSC - digital selective calling, machine
    # data, never voice. AIS 1/2 are continuous ship position reports, which
    # is about as close to "facts someone could use" as this band gets.
    (156.79, 156.81, "MARINE 16"),  (156.515, 156.535, "marine DSC"),
    (157.09, 157.11, "USCG 22A"),
    (161.965, 161.985, "AIS 1"),    (162.015, 162.035, "AIS 2"),
    # --- radio astronomy, protected and always quiet ---
    (38.0, 38.25, "radio astronomy"),   (73.0, 74.6, "radio astronomy"),
    (608.0, 614.0, "radio astronomy"),  (1400.0, 1427.0, "radio astronomy"),
    (1660.0, 1670.0, "radio astronomy"),
    # --- amateur, exact ---
    (28.0, 29.7, "10m ham"),        (50.0, 54.0, "6m ham"),
    (144.0, 148.0, "2m ham"),       (219.0, 220.0, "1.25m ham"),
    (222.0, 225.0, "1.25m ham"),    (420.0, 450.0, "70cm ham"),
    (902.0, 928.0, "33cm ham/ISM"), (1240.0, 1300.0, "23cm ham"),
    # --- everything else, low to high ---
    (24.0, 26.9, "HF edge"),        (26.9, 27.5, "CB"),
    (27.5, 28.0, "HF edge"),        (29.7, 38.0, "VHF low"),
    (38.25, 43.71, "VHF low"),      (43.71, 50.0, "cordless/VHF low"),
    (54.0, 72.0, "TV ch2-4"),       (72.0, 73.0, "model/land mobile"),
    (74.6, 74.8, "assistive"),      (75.2, 76.0, "model/land mobile"),
    (76.0, 88.0, "TV ch5-6"),       (88.0, 108.0, "FM bcast"),
    # airband split up: 19 MHz was one label, and clicking watch on it gave
    # 10 slices at 10% coverage. Boundaries from the ICAO/FCC plan —
    # 108-117.95 navaids, 117.975-136.975 voice and data on 25 kHz channels in
    # North America, with ARINC/ASRI managing 128.825-132.0 and 136.5-136.975
    # for company and datalink traffic.
    (108.0, 118.0, "air nav"),
    (118.0, 121.4, "tower/ground"),   (121.4, 121.6, "GUARD 121.5"),
    (121.6, 122.0, "ground ctl"),     (122.0, 123.6, "unicom/CTAF"),
    (123.6, 128.825, "approach/ctr"), (128.825, 132.0, "ARINC/company"),
    (132.0, 136.0, "center"),         (136.0, 137.0, "ACARS/datalink"),
    (137.0, 138.0, "wx sat"),       (138.0, 144.0, "federal/mil"),
    (148.0, 150.0, "federal"),      (150.0, 156.0, "public svc"),
    (156.0, 157.5, "marine"),       (157.5, 159.0, "business"),
    # The AAR plan is 15 kHz channels from 159.810 to 161.565 - not 159.0 to
    # 161.5, which is where I had drawn it. The 159.0-159.81 piece that was
    # wrongly called "railroad" is ordinary land mobile.
    (159.0, 159.81, "land mobile"), (159.81, 161.57, "railroad"),
    (161.57, 162.1, "marine"),
    (162.1, 162.4, "marine/gov"),   (162.56, 174.0, "federal"),
    (174.0, 216.0, "TV hi-VHF"),    (216.0, 219.0, "maritime/AMTS"),
    (220.0, 222.0, "land mobile"),
    # 243.0 is the military emergency guard channel, worth its own label
    (225.0, 243.0, "mil air"),      (243.0, 243.1, "MIL GUARD"),
    (243.1, 328.6, "mil air"),
    (328.6, 335.4, "glide path"),   (335.4, 399.9, "mil air"),
    (399.9, 406.0, "met/satellite"),(406.1, 420.0, "federal"),
    (450.0, 470.0, "business"),
    # 470-512 is the T-band. On the generic US plan it is TV channels 14-20,
    # and labelling it "UHF TV" is what this used to do — but those channels
    # were reallocated to LAND MOBILE in 11 metro areas: Los Angeles, New York,
    # Chicago, Philadelphia, Boston, Washington/Baltimore, Dallas, Houston,
    # Miami, Pittsburgh and San Francisco. In those markets it is public safety
    # and business radio, not television.
    #
    # Caught by the board disagreeing with reality: 506.4125, 507.3625,
    # 507.4350, 507.8375, 508.4125 and 508.4900 all came up VOICE or DIGITAL
    # while labelled "UHF TV". Broadcast television does not look like that.
    # They are LA County land mobile, and 483.5625 in the same range is LASD
    # Access on the county mutual-aid plan.
    #
    # The metro list is NOT applied conditionally, because nothing in this
    # program knows where it is and none of it should start guessing. The label
    # names both possibilities and lets the operator settle it.
    (470.0, 512.0, "T-band land mobile / TV 14-20"),
    (512.0, 608.0, "UHF TV"),
    (614.0, 698.0, "TV / 600 cell"),(698.0, 806.0, "700 LTE"),
    # 800 MHz after the 2004 rebanding, downlink side (what we can hear):
    # 851-854 NPSPAC public safety, 854-860 SMR/business, 860-869 ESMR, which
    # is the old Nextel spectrum and is now cellular-style carrier equipment.
    # One 18 MHz "800 pub-safety" label was hiding all three, and only the
    # first is public safety at all.
    (806.0, 809.0, "NPSPAC uplink"),(809.0, 815.0, "800 SMR uplink"),
    (815.0, 824.0, "ESMR uplink"),  (824.0, 851.0, "cell uplink"),
    (851.0, 854.0, "NPSPAC pub-safety"), (854.0, 860.0, "800 SMR"),
    (860.0, 869.0, "800 ESMR"),     (869.0, 894.0, "cell"),
    # 896-901 is the mobile half of the 900 MHz SMR band whose bases sit at
    # 935-940; it was labelled "cell", which hid that the two are one system.
    (894.0, 896.0, "cell"),         (896.0, 901.0, "900 SMR uplink"),
    (901.0, 902.0, "narrowband PCS"),
    (928.0, 929.0, "fixed link"),
    (929.0, 930.0, "pager"),        (930.0, 935.0, "pager/fixed"),
    (935.0, 940.0, "900 SMR"),          (940.0, 960.0, "fixed/cell"),
    (960.0, 1089.5, "aviation"),    (1090.5, 1215.0, "aviation"),
    (1215.0, 1240.0, "radiolocation"), (1300.0, 1400.0, "radar"),  # GUESS
    (1427.0, 1525.0, "telemetry"),  (1525.0, 1559.0, "satellite"),
    (1559.0, 1574.0, "satnav"),     (1577.0, 1610.0, "satnav"),
    (1610.0, 1626.5, "Iridium"),    (1626.5, 1660.0, "satellite"),
    (1670.0, 1710.0, "met/fixed"),  (1710.0, 1766.0, "AWS cell"),
]


# Every bucket we know about, in frequency order. The filter list is built
# from THIS, not from whatever happens to have been detected — so the controls
# are stable from the first second and a count of 0 is itself information.
def _tag_order():
    first = {}
    for lo, _hi, tag in LABELS:          # several tags span more than one
        first[tag] = min(lo, first.get(tag, lo))   # range; sort by the lowest
    return [t for t, _ in sorted(first.items(), key=lambda kv: kv[1])]


ALL_TAGS = _tag_order()

# Unticked on first load ONLY where decoding is cryptographically impossible —
# no antenna, dongle or software can ever recover the payload:
#   cell / 700 LTE / AWS cell   GSM/LTE/5G traffic is encrypted, and in the US
#                               intercepting it is illegal regardless.
# Deliberately NOT here: TV (unencrypted, we simply cannot capture 6 MHz),
# GPS (open civil code, needs a better front end), P25 public safety/federal
# (often in the clear). Those are equipment limits, not impossibilities, so
# they stay on. Every one of these is one click away from coming back.
DEFAULT_OFF = ["700 LTE", "cell", "AWS cell"]


def label_for(mhz):
    for lo, hi, tag in LABELS:
        if lo <= mhz < hi:
            return tag
    return ""


# --- local noise floor -------------------------------------------------------
#
# CFAR_GUARD is measured in BINS and must exceed the widest signal we accept,
# or a wide signal sits in its own reference window and raises its own floor
# until it disappears. Simulated: with a conventional small guard a signal 20
# bins wide masks itself completely, Pd 1.00 -> 0.01.
CFAR_GUARD = max(8, int(round(CFAR_GUARD_HZ / BIN_HZ)))   # bins each side
CFAR_REF   = 32               # reference cells, half each side
# The MEDIAN of the reference cells, not a higher quantile. This is the same
# estimator the old slice-wide floor used, just measured locally — which is why
# it is the principled choice rather than the best-scoring one. Measured: a
# 0.75 quantile (rank 24) found FEWER signals than the old global median in
# dense bands, because with 25 kHz channel spacing most reference cells are
# themselves occupied and the upper quantile lands on a neighbour.
CFAR_RANK  = 16


def local_floor(avg_db):
    """Noise floor per bin, as an order statistic of nearby bins.

    A single median across the slice assumes the response is flat, and it is
    not: the tuner tilts about 6 dB across 1.92 MHz. Measured consequence, on
    that tilt, for a 5 dB signal: found 100% of the time at slice centre and
    63% at the edge — and the +/-100 kHz per-lap jitter decides which you get.

    A RANK (not a mean) is what makes this work with neighbours present. With
    three 25 dB signals inside the reference window, the order statistic held
    Pd=1.00 on a 6 dB target while a mean-based floor scored 0.000. Several
    strong signals inside 200 kHz is ordinary in a land-mobile band.

    Measured on air, three independent runs over FM broadcast, 800 MHz
    trunked, 2 m, 70 cm and two protected radio-astronomy bands:

        slice median   85    88    89   real signals
        local median   89    96    99
        protected       0/0   1/0   0/0  false positives (old/new)

    The gain is smaller than the theory suggests because USABLE = 0.80 already
    crops the steep part of the IF response: the tilt inside the part we keep
    is 2.6 dB, not the 6 dB across a full capture.
    """
    n = len(avg_db)
    off = np.r_[np.arange(-CFAR_REF // 2 - CFAR_GUARD, -CFAR_GUARD),
                np.arange(CFAR_GUARD + 1, CFAR_REF // 2 + CFAR_GUARD + 1)]
    idx = np.arange(n)[:, None] + off[None, :]
    # clip rather than wrap: the spectrum is not circular, and wrapping would
    # let one edge of the capture set the floor at the other.
    ref = avg_db[np.clip(idx, 0, n - 1)]
    return np.partition(ref, CFAR_RANK - 1, axis=1)[:, CFAR_RANK - 1]


def analyse(iq, center):
    """One capture -> list of candidate signals with a structure score."""
    n = (len(iq) // NFFT) * NFFT
    x = iq[:n].reshape(-1, NFFT) * np.hanning(NFFT)
    X = np.fft.fftshift(np.fft.fft(x, axis=1), axes=1)
    P = (X.real ** 2 + X.imag ** 2) + 1e-20          # frames x bins, linear

    bin_f = np.fft.fftshift(np.fft.fftfreq(NFFT, 1 / RATE))
    avg_db = 10 * np.log10(P.mean(axis=0))
    frame_db = 10 * np.log10(P)

    usable = np.abs(bin_f) <= (RATE * USABLE) / 2
    dc = np.abs(bin_f) < DC_NOTCH
    valid = usable & ~dc
    floor_b = local_floor(avg_db)                 # per bin, follows the IF tilt
    floor = float(np.median(avg_db[valid]))       # slice-wide, for reporting

    hot = valid & (avg_db > floor_b + SNR_MIN)
    hits, i, n_bins = [], 0, NFFT
    while i < n_bins:
        if not hot[i]:
            i += 1
            continue
        j = i
        while j + 1 < n_bins and hot[j + 1]:
            j += 1
        width = j - i + 1
        # A group touching the usable EDGE is probably clipped, so let the
        # neighbouring step have it. Test `usable`, not `valid` — `valid` also
        # excludes the DC notch, so a group merely ADJACENT to the notch was
        # being thrown away as if it were clipped at the capture edge. It is
        # not: DC is a hole in the middle, with good spectrum on both sides.
        #
        # The notch is 24 kHz wide; that mistake made the blind zone 50 kHz.
        # Bench, a strong NBFM carrier stepped away from the centre, 5 trials:
        #
        #     offset kHz   0    5   10   14   18   22   26   30
        #     before      0/5  0/5  0/5  0/5  0/5  0/5  5/5  5/5
        #     after       0/5  0/5  5/5  5/5  5/5  5/5  5/5  5/5
        #
        # ~1% of the spectrum was invisible on any given lap, and any channel
        # within 125 kHz of a step centre lost roughly a quarter of its laps,
        # since the per-lap jitter is only +/-100 kHz.
        if usable[max(i - 1, 0)] and usable[min(j + 1, n_bins - 1)]:
            peak = i + int(np.argmax(avg_db[i:j + 1]))
            hits.append(_score(peak, i, j, width, avg_db, frame_db,
                               float(floor_b[peak]), bin_f, center, valid))
        i = j + 1
    return [h for h in hits if h]


def _score(peak, i, j, width, avg_db, frame_db, floor, bin_f, center, valid):
    # `floor` here is the LOCAL floor at this peak, not the slice median, so a
    # signal near the tilted edge of the capture is measured against the noise
    # that is actually next to it.
    snr = float(avg_db[peak] - floor)
    # A single-bin spike is not a channel. Real narrowband voice is ~12 kHz and
    # covers several bins; one lone bin is a spur or a noise fluctuation.
    if snr < SNR_MIN or not (MIN_WIDTH_BINS <= width <= MAX_WIDTH_BINS):
        return None

    # 1. PERSISTENCE — is it there in every frame, or is it a random spike?
    #    This is the big one. Noise wanders; a transmission stays.
    persist = float(np.mean(frame_db[:, peak] > floor + 3.0))

    # 2. PROMINENCE — how sharply it stands out of its own surroundings.
    #    Real channels have steep skirts; noise humps blend in.
    lo, hi = max(i - 12, 0), min(j + 13, NFFT)
    guard = np.concatenate([avg_db[lo:i], avg_db[j + 1:hi]])
    prom = float(avg_db[peak] - np.median(guard)) if guard.size else 0.0

    # 3. STABILITY — a transmitter holds a steady level frame to frame.
    #    Noise power fluctuates a lot (~5.6 dB for pure thermal noise).
    stab = float(np.std(frame_db[:, peak]))

    s = (0.45 * persist
         + 0.25 * min(max(prom / 15.0, 0.0), 1.0)
         + 0.20 * min(max((8.0 - stab) / 6.0, 0.0), 1.0)
         + 0.10 * (1.0 if MIN_WIDTH_BINS <= width <= MAX_WIDTH_BINS else 0.2))

    # Sub-bin peak position by 3-point parabolic interpolation in dB — the
    # standard estimator. Taking the peak BIN alone quantises every frequency
    # we report to 2.34 kHz, which is coarser than the 2.5 kHz channel grid we
    # then snap to, so a channel could land a whole step off. Measured on 300
    # synthetic tones: median error 609 Hz -> 25 Hz, worst case 1170 -> 40 Hz.
    f_hz = float(bin_f[peak])
    if 0 < peak < NFFT - 1:
        a, b, c = (avg_db[peak - 1], avg_db[peak], avg_db[peak + 1])
        denom = a - 2.0 * b + c
        if denom < -1e-9:                 # a real maximum, not a flat or a dip
            delta = 0.5 * (a - c) / denom
            if abs(delta) <= 1.0:
                f_hz += float(delta) * BIN_HZ

    return {"freq": float(center + f_hz), "snr": snr,
            "width": width * BIN_HZ, "persist": persist, "prom": prom,
            "stab": stab, "score": s}


# --- the other two scales ---------------------------------------------------
# analyse() above finds NARROW signals: it compares each bin against the median
# of its own capture. That is blind to two whole classes of data.
#
#   WIDE  — a signal broader than our 1.92 MHz window has no "outside" left to
#           compare against; it BECOMES the median, so it hides perfectly. The
#           fix is to compare a step against its NEIGHBOURING frequencies
#           instead (see the end of the lap). Comparing against the step's own
#           history does not work: an always-on transmitter is its own history
#           and would stay invisible forever. Catches TV, cellular, wide links.
#   BURST — a 56 us pulse averaged over a 20 ms capture is diluted ~350x into
#           nothing. It has to be caught in the time domain, before any FFT.
#           Catches ADS-B, radar, telemetry, packet bursts.

BURST_MIN   = 2               # pulses in one capture before it counts
# A burst hit has no meaningful per-bin SNR — it is found in the time domain,
# before any FFT. It still needs a number in `snr` because worth() ranks verify
# time by it. This places a burst mid-pack among real signals instead of
# letting a pulse count masquerade as a huge SNR.
BURST_SNR_DB = 12.0
WIDE_MIN_DB = 4.0             # dB over the median of neighbouring steps


def level_db(iq, gain_db=0.0):
    """Overall power of a capture — the number the WIDE test watches.

    Gain-corrected, because WIDE compares one step against its NEIGHBOURS and
    every step carries its own gain from the ladder. WIDE_MIN_DB is 4 dB and a
    single ladder step is 4-9 dB, so without this subtraction the test was
    largely detecting which step happened to latch a higher gain rather than
    which step was occupied.
    """
    return float(10 * np.log10(np.mean(iq.real**2 + iq.imag**2) + 1e-20)) - gain_db


# --- whisper, as a CONFIRMER only -------------------------------------------
#
# Never in the detection path and never on the radio's critical path. The
# structural classifier decides what it decides; a background thread listens to
# audio we already captured and, if it hears actual words, upgrades the channel
# to voice. If whisper is slow, wrong, missing or hung, the worst it can do is
# fail to upgrade something. Nothing waits for it.
#
# Why it earns its place: morse, courtesy beeps and squelch tails all modulate
# at 3-6 Hz, which is the band that defines speech rhythm, so syllabic() cannot
# separate them from voice at ANY threshold — measured against ear labels, real
# voice scored 6.3/7.1/11.5/13.2 and morse+beeps scored 9.3/9.3/10.5/11.1,
# fully interleaved. Whisper returns "(static)" for the morse clip and words
# for the speech. It is the only thing we have that tells those apart.
#
# It speaks only to VOICE. It says nothing about digital, data or burst.
# Point these at your own whisper.cpp build. Absent or unset, the scanner
# simply does not listen — everything else works exactly the same.
WHISPER_BIN = os.environ.get("WHISPER_BIN", "/opt/homebrew/bin/whisper-cli")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "models/ggml-small.en.bin")
# A confirmation stands until we LISTEN AGAIN and hear nothing. A pure timer
# was wrong: transcripts expired after 10 minutes while a channel's next
# listen was up to 30 minutes away, so every confirmed channel spent 20
# minutes with nothing to show and whisper had no chance to renew it. This is
# only a backstop for a channel we never manage to hear again — it matches
# FORGET_S, the point at which the track itself is dropped.
WHISPER_HOLD_S = 3600.0
WHISPER_QUEUE = 16            # bounded: drop the oldest rather than lag behind
# Whisper's own non-speech annotations, plus the music marker. "\u266a The car
# is not a sweater \u266a" came back from a 4.8 dB channel that is silent on a
# handheld — the note characters were not in the original pattern, so it passed.
_NONSPEECH = __import__("re").compile(
    r"^[\s]*[\(\[\*\u266a\u266b].*[\)\]\*\u266a\u266b][\s]*$")
_BLANK = __import__("re").compile(r"blank_?audio|inaudible|silence|\u266a|\u266b",
                                  __import__("re").I)
# Below this, whisper is being asked to transcribe noise, and it obliges.
# Every correct confirmation so far came from 17-18 dB; every hallucination
# ("We also have that", "So a lot of people", the sweater one) came from
# 4.7-11.1 dB. Not a fitted threshold - it is the presence floor classify()
# already uses to decide a carrier exists at all, plus the same margin.
LISTEN_MIN_PRES = 14.0
# Two words, because a 1.2 s capture of real speech CONTAINS two to four words.
# Measured directly on NOAA — a continuous, unambiguous voice transmission at
# 34.3 dB presence — whisper returned "as well.", which a three-word minimum
# threw away. Roughly half of genuine fragments were being discarded that way.
#
# Word count was never the defence. The hallucinations that prompted all this
# ("We also have that", "So a lot of people") were four words and would have
# passed any of these limits; what removes them is LISTEN_MIN_PRES, since they
# all came from 4.7-11.1 dB. This threshold only exists to drop one-word noise.
LISTEN_MIN_WORDS = 2

heard = {}                    # freq_hz -> (when, text)
heard_lock = threading.Lock()
_wq = __import__("collections").deque(maxlen=WHISPER_QUEUE)
_wq_lock = threading.Lock()


def whisper_ok():
    import os
    return os.path.exists(WHISPER_BIN) and os.path.exists(WHISPER_MODEL)


def said_words(text):
    """Is this a transcript, or whisper telling us there was nothing?"""
    import re
    t = text.strip()
    if not t or _NONSPEECH.match(t) or _BLANK.search(t):
        return False
    words = [w for w in re.findall(r"[A-Za-z']+", t) if len(w) > 1]
    if len(words) < LISTEN_MIN_WORDS:
        return False
    # Self-repeating output is the classic hallucination ("you you you you").
    # Test for REPETITION, not for a minimum vocabulary: the old rule demanded
    # three distinct words no matter what, so a genuine two-word fragment like
    # "as well." was rejected as if it were a loop.
    uniq = len(set(w.lower() for w in words))
    return uniq * 2 >= len(words)


listen_stats = {"queued": 0, "dropped": 0, "gated": 0, "ran": 0}


def queue_listen(freq_hz, audio, rate):
    """Called from the judging threads. Must be instant and must never raise."""
    try:
        with _wq_lock:
            if len(_wq) >= WHISPER_QUEUE:
                listen_stats["dropped"] += 1
            _wq.append((freq_hz, audio, rate))
            listen_stats["queued"] += 1
    except Exception:
        pass


def whisper_worker():
    import os, subprocess, tempfile
    from prove import wav
    tmp = os.path.join(tempfile.gettempdir(), "scan_listen.wav")
    while True:
        item = None
        with _wq_lock:
            if _wq:
                item = _wq.popleft()
        if item is None:
            time.sleep(0.4)
            continue
        freq_hz, audio, rate = item
        listen_stats["ran"] += 1
        try:
            wav(tmp, audio, rate)
            out = subprocess.run([WHISPER_BIN, "-m", WHISPER_MODEL, "-f", tmp,
                                  "-nt", "-np"], capture_output=True,
                                 text=True, timeout=60).stdout
            txt = " ".join(out.split())
            if said_words(txt):
                with heard_lock:
                    heard[round(freq_hz)] = (time.time(), txt[:120])
                print(f"  [heard] {freq_hz/1e6:10.4f}  {txt[:60]}", flush=True)
            else:
                # Listened and heard nothing. THAT is what retires a
                # transcript — evidence, not the passage of time.
                with heard_lock:
                    for k in [k for k in heard
                              if abs(k - freq_hz) <= HEARD_TOL_HZ]:
                        del heard[k]
        except Exception:
            pass


HEARD_TOL_HZ = 4000.0


def heard_recently(freq_hz, tol_hz=HEARD_TOL_HZ):
    now = time.time()
    with heard_lock:
        for f, (when, txt) in heard.items():
            if abs(f - freq_hz) <= tol_hz and now - when < WHISPER_HOLD_S:
                return txt
    return None


def apply_verdicts(res, by_freq, t0, tag=""):
    """Write verify results onto tracks. Shared by the deferred pass and the
    mid-lap strike so the two cannot drift apart."""
    for f, v in res:
        m = by_freq.get(f)
        if m is None:
            continue
        was = m.get("verdict")
        now2 = time.time()
        # keep the more specific answer while it is still fresh
        if (specificity(v) < specificity(was)
                and now2 - m.get("vpos", 0.0) < VERDICT_HOLD_S):
            m["vt"] = now2
            continue
        m["verdict"], m["vt"] = v, now2
        if v in CARRYING:
            m["vpos"] = now2
        if v != was:
            print(f"[{time.time()-t0:6.1f}s] {v.upper():9}{tag} "
                  f"{f/1e6:10.4f} MHz  {label_for(f/1e6)}", flush=True)


def reopen(old, rate, gain, tries=6):
    """Rebuild the radio after a USB failure. Returns a new radio, or None.

    Unplugging the dongle used to kill the process outright: rtl.read raises
    RuntimeError, nothing catches it, and the whole board is lost. Replugging
    is a thing people do, so recover instead of dying.

    KEEP TRYING. One attempt was not enough, and the failure was ugly: the old
    radio is closed FIRST, so a single failed rebuild returned None while the
    caller still held a shut socket, and the next step died on
    `OSError: [Errno 9] Bad file descriptor` — a traceback that names
    set_gain and says nothing about the radio having gone. Observed killing
    the board outright.

    On the RSP1B one attempt is especially not enough, because the rebuild is
    exactly where the device gets recovered: Rsp() restarts SDRconnect when it
    has lost the hardware, and that takes ~20 s. Backing off and retrying
    turns a fatal crash into a pause.
    """
    try:
        # Do NOT terminate the server here. Rsp() decides whether SDRconnect
        # needs recycling; killing it blind just adds a 25 s cold start to
        # every attempt, including the attempts that were going to work.
        old._srv = None
    except Exception:
        pass
    try:
        old.close()
    except Exception:
        pass
    for i in range(tries):
        try:
            if BACKEND == "rsp":
                return radio.Rsp(0, rate, gain)
            return radio.Rtl(radio.find("R828D") or 0, rate, gain)
        except Exception as e:
            if i == tries - 1:
                print(f"  [radio] rebuild failed after {tries} tries: {e!r}",
                      flush=True)
                return None
            time.sleep(min(3.0 * (i + 1), 15.0))
    return None


def bursts(iq, lo_us=10.0, hi_us=400.0):
    """Count fast on/off pulses in the envelope. Returns (count, median us).

    No FFT: this is deliberately upstream of everything else, because the
    averaging that makes narrowband detection work is exactly what destroys a
    microsecond pulse. Noise crosses a threshold constantly and at random
    lengths — never repeatedly at one consistent duration."""
    env = np.abs(iq)
    env = np.convolve(env, np.ones(4) / 4, mode="same")
    med = np.median(env)
    if med <= 0:
        return 0, 0.0
    hot = env > med * 3.0
    d = np.diff(hot.astype(np.int8))
    st = np.flatnonzero(d == 1) + 1
    en = np.flatnonzero(d == -1) + 1
    if hot[0]:
        st = np.r_[0, st]
    if hot[-1]:
        en = np.r_[en, len(hot)]
    n = min(len(st), len(en))
    if n == 0:
        return 0, 0.0
    us = (en[:n] - st[:n]) / RATE * 1e6
    ok = us[(us >= lo_us) & (us <= hi_us)]
    return int(ok.size), float(np.median(ok)) if ok.size else 0.0


COLD_EVERY = 6                # a step with nothing recent: one lap in six
HOT_MEMORY = 25               # laps a step stays hot after its last hit.
                              # This is the self-healing knob: after a move or
                              # an antenna change, stale hot steps fall away in
                              # this many laps (~6 min at 14 s/lap).


class Schedule:
    """Not every part of the spectrum deserves equal time.

    Measured here: 72% of a full 24-1766 MHz lap went on steps that had never
    produced anything, making the lap 44 s — and a 44 s lap has only ~11% chance
    of catching a 5-second transmission. Skipping most of the dead air shortens
    the lap, and a shorter lap is the only thing that raises those odds.

    RECENCY, not history. A step is hot because it produced something in the
    last HOT_MEMORY laps, not because it ever did. That matters because the
    setup is not fixed: change the antenna, or drive somewhere else, and the
    map of what is worth listening to changes completely. Old hot steps go
    quiet, cool off on their own, and stop being visited; wherever you are now
    heats up the moment it produces a hit. No reset, no reconfiguration.

    Two safeguards against the obvious failure — a scheduler that hides the
    intermittent signals it never gets around to looking for:
      * every cold step is still visited once every COLD_EVERY laps, so nothing
        is ever locked out, only delayed
      * hot is granted on ANY hit, instantly, from a single cold visit
    """

    def __init__(self):
        self.last_hit = {}

    def mark(self, key, lap):
        self.last_hit[key] = lap

    def due(self, key, lap):
        last = self.last_hit.get(key)
        if last is not None:
            if lap - last <= HOT_MEMORY:
                return True
            del self.last_hit[key]        # gone quiet — let it cool
        # spread the cold steps over the cycle instead of bunching them
        return (lap % COLD_EVERY) == (hash(key) % COLD_EVERY)

    def hot_count(self):
        return len(self.last_hit)


# --- the mission test -------------------------------------------------------
# The sweep finds CARRIERS. A carrier is not the point — a stuck transmitter
# with nothing on it is coherent, stable and persistent, which is every
# property the sweep scores highest. 268.2 MHz sat at the top of the board
# carrying nothing at all.
#
# So each confirmed channel gets a second look: one full second, long enough to
# see whether the modulation actually CHANGES. That is what separates
# information from a bare carrier, and 20 ms cannot do it — FM is
# constant-envelope by design, so real FM voice looks identical to a dead
# carrier in a short capture. Measured: NOAA voice varies 0.18 dB in 20 ms
# (LESS than an idle carrier) but 4.2 dB over a second.
VERIFY_SECS   = 1.2
VERIFY_SLICES_PER_LAP = 5     # captures per lap, NOT channels — one capture
                              # judges every confirmed channel inside it
# A channel that has NEVER been judged is worth far more than re-checking one
# already answered: unknown is the only state that tells you nothing. Verdicts
# are also fairly stable — something carrying voice today is likely to again —
# so re-checking is a background chore, not a priority. Never-judged channels
# jump the queue; answered ones are revisited only when nothing is waiting.
# A verdict is the best answer seen recently, not the most recent answer.
# Band mode already kept the best; the sweep overwrote with the latest, so a
# channel could read "voice" in one and "carrier" in the other AT THE SAME
# MOMENT, from the same code — the sweep had simply re-checked it while nobody
# was talking. Every ham repeater is bursty, so the latest answer is mostly a
# statement about timing.
# Best-ever would be a lie an hour later, so it decays: a positive verdict
# holds for VERDICT_HOLD_S and reverts if re-checks keep coming back idle.
VERDICT_HOLD_S = 600.0        # 10 min
# "burst" belongs here. It was added to rescue pagers, TETRA, TPMS and key fobs
# from being called "noise" — everything under ~a tenth of a second — and then
# left out of CARRYING, out of the UI's carrying filter and out of the carrying
# count, with specificity 0 so it never survived a re-check either. That moved
# them from one label users filter away to another. A burst IS information;
# being brief is not the same as being empty.
CARRYING = ("voice", "digital", "data", "burst")
# How SPECIFIC a verdict is, which is not the same as how confident it is.
# "voice" and "digital" are identifications; "data" is kind_of's fallback for
# "something is here and I cannot tell what". A re-check that comes back less
# specific has not discovered anything - it has failed to see what it saw
# before - so while a specific answer is still fresh it must not be replaced
# by a vaguer one. Measured on 445.1375 while it was unambiguously in use:
# 6 of 8 consecutive 1.2 s looks said voice, 2 did not, and one of the misses
# was enough to relabel a live conversation "data" and hide it from the voice
# filter. The misses are sampling noise - at 2 s windows the same capture was
# voice 5 times out of 5.
_RANK = {"voice": 2, "digital": 2, "data": 1, "burst": 1}


def specificity(v):
    return _RANK.get(v, 0)


REVERIFY_S    = 1800.0        # 30 min before an answered channel is re-asked
REVERIFY_IDLE_S = 240.0       # ...or 4 min, if there is spare capacity

# Ordered LEAST to MOST sensitive, whatever the radio calls its settings, so
# the adapt() logic below ("index up = more gain") is the same either way.
#
# The RSP1B does not have a gain in dB: it has 9 LNA states where a HIGHER
# state means MORE gain reduction, i.e. less sensitivity. Feeding those indices
# straight into a ladder written for the RTL-SDR would have driven the gain the
# wrong way on every adaptation. Reversed here instead, once.
#
# Measured on NOAA 162.5500, same antenna: state 0 is starved (0.4 dB SNR),
# state 2 peaks at 43.0 dB, and it falls off steadily above that as gain
# reduction trades sensitivity for headroom:
#     state  0     1     2     3     4     5     6     7     8
#     SNR   0.4  36.8  43.0  41.1  37.3  32.0  26.5  21.4  13.3
if BACKEND == "rsp":
    GAIN_LADDER = [8, 7, 6, 5, 4, 3, 2]      # least -> most sensitive
    GAIN_START  = 6                          # index of state 2, the measured best
else:
    GAIN_LADDER = [16.6, 25.4, 32.8, 37.2, 40.2, 44.5, 49.6]
    GAIN_START  = 4

# Peak above which it is worth spending a round trip asking the hardware
# whether it is overloading. Set below the 0.415 measured at the last CLEAN
# state and well below the 0.523 measured while overloading, so the question
# gets asked in the gap between "obviously fine" and "the samples finally
# admit it" — which is the whole range the peak test was blind to.
OVERLOAD_PEEK = 0.35


class Gains:
    """One gain for the whole spectrum cannot work. Measured on this hardware:
    FM broadcast reads -3 dB while 1090 MHz reads -41 dB — a 38 dB spread. At a
    fixed 40 dB the strong end overloads and the weak end is starved (ADS-B
    detections went 3 -> 23 just by moving to max gain). So each step keeps its
    own gain and walks toward the right one."""

    def __init__(self, start=40.2):
        self.i = {}
        self.start = GAIN_LADDER.index(start) if start in GAIN_LADDER else GAIN_START

    def for_step(self, key):
        return GAIN_LADDER[self.i.setdefault(key, self.start)]

    def adapt(self, key, iq, ask_overload=None):
        """Aim for a healthy peak without clipping. Returns True if changed.

        ask_overload, where the radio offers one, is the hardware's own
        front-end overload flag. The RSP1B overloads well before its samples
        show it — measured 0.00% clipped at a peak of 0.52 while the flag read
        True — so the peak test alone runs the front end into compression and
        never notices. Asking costs a 6.3 ms round trip, so it is only asked
        when the peak is high enough that the answer could plausibly be yes.
        rtl.Rtl has no such flag, passes None, and keeps its old behaviour.
        """
        peak = float(np.percentile(np.abs(iq.real), 99.9))
        i = self.i[key]
        hot = peak > 0.85
        if not hot and peak > OVERLOAD_PEEK and ask_overload is not None:
            try:
                answer = ask_overload()
            except Exception:
                answer = None
            if answer is None:
                # Could not ask. Leave the gain exactly where it is rather
                # than guessing: treating unknown as "clean" would let the
                # front end sit in compression whenever the link is unhealthy,
                # which is precisely when we can least afford to be wrong.
                return False
            hot = answer
        if hot and i > 0:                               # overloading
            self.i[key] = i - 1
        elif peak < 0.25 and i < len(GAIN_LADDER) - 1:  # starved
            self.i[key] = i + 1
        return self.i[key] != i


def verify_slice(r, center_hz, freqs, pool, also_listen=()):
    """Judge EVERY confirmed channel inside one capture, not one at a time.

    A 2.4 MHz capture already contains ~150 channels — the same fact band mode
    is built on, which the per-channel version ignored. It tuned once per
    channel and spent 1.2 s of radio time for a single verdict; three per lap
    meant the backlog took ~10 minutes to clear and the CARRYING column was
    meaningless until then.

    Here one capture serves the whole slice. Extracting and judging a channel
    costs ~0.2 s, so 40 of them serially would be slower than the capture and
    become the new bottleneck — hence the thread pool. numpy releases the GIL
    for FFTs, so this genuinely uses the cores.
    """
    from prove import channelize, spectrum, metrics, safe_offset, CHAN_RATE
    # Do not let the capture centre land on a local clock. Slice centres are a
    # multiple of the span, and for a channel at 28.7625 that multiple IS
    # 28.8 MHz — the dongle's reference, which measures 64 dB. The channel read
    # 46 dB on the board and 0.6 dB when tuned properly.
    #
    # This used to walk +500 kHz up to four times, and TWO things were wrong
    # with that. It only ever moved UP, so the offset grew without bound
    # instead of flipping sign; and it tested the RTL-SDR's clocks as literals
    # while CLOCKS_HZ exists for exactly this and is empty on the RSP1B, so
    # ~12% of slices were displaced for no reason at all.
    #
    # The damage was silent. channelize's `idx % n` (prove.py:75) WRAPS: ask
    # for a channel past Nyquist and it returns a different one, with no error.
    # Over 908 slice centres, 89 walked twice — putting the slice centre itself
    # past Nyquist — and every channel in them got a confident verdict about
    # the wrong frequency.
    #
    # safe_offset() in prove.py has searched (off, -off, 2*off, -2*off) for a
    # long time. The scanner kept a sign-locked copy. Use the real one.
    off = safe_offset(center_hz, clocks=CLOCKS_HZ)
    tune_at = center_hz - off
    r.tune(tune_at)
    r.flush()
    listening = whisper_ok()
    iq = r.read(int(VERIFY_SECS * RATE))
    # ONE transform for the whole capture, shared by every channel in it.
    # channelize used to do this itself, so a 40-channel slice transformed the
    # same 2.88M samples 40 times: 1500 ms instead of 64 ms, for output that is
    # bit-for-bit identical.
    spec = spectrum(iq)

    def judge(f):
        try:
            off_f = off + (f - center_hz)
            # channelize wraps rather than complaining: `idx % n` at
            # prove.py:75 turns a request past Nyquist into a DIFFERENT
            # channel, silently, and classify then reports a verdict for a
            # frequency nobody asked about. Refuse instead of guessing.
            if abs(off_f) > RATE * 0.48:
                return f, None
            y = channelize(iq, RATE, off_f, CHAN_RATE, pre=spec)
            v = classify(y, CHAN_RATE)
            # LEVEL decides whether to listen, never the verdict.
            #
            # This used to skip "quiet" and "noise" on the reasoning that they
            # have nothing to listen to. That is circular, and it locks in the
            # exact mistakes whisper exists to catch: the verdict silences the
            # only thing that could revise it. 448.0600 is a 70cm repeater with
            # people talking on it continuously, and it sat on the board as
            # quiet with no transcript for hours.
            #
            # It is a repeater, so its carrier is up constantly and its
            # loudness barely varies — dynamics measured 0.21-1.52 against a
            # 0.70 gate — which leaves the verdict riding entirely on the
            # rhythm score. Measured across four captures of the same channel
            # in the same minute, presence a steady ~30 dB throughout:
            #
            #     dyn 0.65  syllabic 19.9  -> voice
            #     dyn 0.43  syllabic  5.8  -> carrier
            #     dyn 0.21  syllabic  6.2  -> voice
            #     dyn 0.43  syllabic 22.6  -> voice
            #
            # 5.8 against a threshold of 6.0. One unlucky capture and whisper
            # was switched off for that channel permanently.
            #
            # The presence floor below already does the job the verdict test
            # was pretending to do. Only ask whisper about signals strong
            # enough to actually be speech: every hallucination so far ("We
            # also have that", "So a lot of people", a musical-note one) came
            # from 4.7-11.1 dB, and every correct confirmation from 17-18 dB.
            # Word count cannot separate them — real transmissions are short
            # too — but level can. 448.0600 clears it at 30 dB.
            if listening:
                pres = metrics(y, CHAN_RATE)[4]
                if not np.isnan(pres) and pres >= LISTEN_MIN_PRES:
                    queue_listen(f, y, CHAN_RATE)
                else:
                    listen_stats["gated"] += 1
            return f, v
        except Exception:
            # Swallowing this hid a real crash for a long time: kind_of raised
            # IndexError on any capture that was not a whole number of 0.1 s
            # blocks, and the channel just never got a verdict. Log it now.
            import traceback
            print(f"  [judge {f/1e6:.4f}] {traceback.format_exc().splitlines()[-1]}",
                  flush=True)
            return f, None

    res = [x for x in pool.map(judge, freqs) if x[1]]

    # LISTEN to every other confirmed channel in this capture as well.
    #
    # The capture already contains all ~40 channels in the slice; we paid the
    # radio time once. Extracting one costs 1.6 ms and a presence check a few
    # more, so listening to the rest is CPU only — no extra dwell, no extra
    # lap time. Without this, a channel was only ever heard when it happened
    # to be DUE for re-verification, which with a full board is every 30
    # minutes, so whisper sat 99.6% idle (281 runs in 9.5 hours) while
    # transcripts expired for want of anything to listen to.
    #
    # These channels get NO verdict from this pass — only whisper can act on
    # them, via apply_heard. We are buying audio, not opinions.
    def listen_only(f):
        try:
            y = channelize(iq, RATE, off + (f - center_hz), CHAN_RATE, pre=spec)
            pres = metrics(y, CHAN_RATE)[4]
            if not np.isnan(pres) and pres >= LISTEN_MIN_PRES:
                queue_listen(f, y, CHAN_RATE)
            else:
                listen_stats["gated"] += 1
        except Exception:
            pass

    if listening and also_listen:
        list(pool.map(listen_only, also_listen))
    return res


def syllabic(y, rate):
    from prove import syllabic as _s
    return _s(y, rate)


def classify(y, rate):
    """Is INFORMATION being carried here?

    -> voice | digital | data | tone | carrier | noise | quiet

    THE ONLY COPY OF THIS DECISION. Both the sweep (verify_slice) and band
    mode call it. It used to be pasted in both, and they drifted: the presence
    floor was added to the sweep and not to band mode, so the same channel got
    two different answers depending on which page you were looking at. If a
    threshold changes here it changes everywhere, which is the point.

    Deliberately not a decoder. It asks only whether the modulation changes
    over time, which is what carrying information requires and what a bare
    carrier cannot fake.
    """
    from prove import metrics, kind_of
    frac, wander, flat, dyn, pres = metrics(y, rate)
    if frac < 0.02:
        return "quiet"                    # nothing on air during the look
    # No carrier means nothing to classify. 136.1000 read "voice" with a
    # presence of 1.0 dB — a few noise windows crossed the activity threshold
    # and the metrics were computed on those, and noise happily produces
    # dynamics 7.88 and flatness 0.440. The floor is not a tuned number: every
    # confirmed voice channel measured 33-39 dB, and the weakest REAL signal
    # referenced (an idle carrier) measured 18 dB, so 8 dB clears everything
    # genuine by more than 10 dB while removing verdicts computed on noise.
    if pres < 8.0:
        return "quiet"
    if np.isnan(flat):
        # metrics() returns NaN for flat/dyn when fewer than 2 of its 85 ms
        # windows were active — it HAS a carrier and it computed pres95, it
        # just could not characterise the modulation. Calling that "noise" sent
        # every short burst to the one label most likely to be filtered away.
        # Measured on synthetic bursts in a 1.2 s capture: 20 ms -> quiet,
        # 85 ms -> noise, 170 ms+ -> voice. Everything under ~a tenth of a
        # second was being buried: pagers, TETRA, TPMS, key fobs, meter reads.
        # No new measurement — the numbers were already in hand and discarded.
        return "burst" if frac > 0.0 else "noise"
    # Flatness alone. Gates on dynamics or carrier wander looked reasonable but
    # rejected real data: continuous digital streams do not vary like speech,
    # and FSK shifts its carrier on purpose. Measured: data 0.10-0.29, bare
    # carriers 0.80-0.87, noise ~1.0.
    # A pure TONE is maximally structured and carries nothing, so flatness
    # alone is not enough — the 50-54 MHz cluster scored flatness 0.08 with
    # dynamics 0.18, less variation than a dead carrier, and was reported as
    # data. Information has to CHANGE: ear-verified data measures 3.1-4.9,
    # everything carrying nothing measures 0.17-0.41.
    # Dynamics, not flatness. Flatness cannot tell an ear-verified data
    # channel (450.7250, flat 0.79) from an ear-verified idle carrier
    # (268.2, flat 0.82); requiring low flatness discarded real data.
    # Information CHANGES: data measures 1.1-4.9, non-data 0.18-0.40.
    # A weak signal's noise fluctuates too, so require it to be measurably
    # non-flat as well: 327.1500 passed on dynamics alone at flatness 0.933,
    # which is indistinguishable from noise.
    if (dyn >= 0.70 and flat < 0.90) or syllabic(y, rate) > 6.0:
        # rhythm alone qualifies: two ear-confirmed 2m voice channels had
        # featureless spectra (flatness 0.96 and 0.67) and the flatness gate
        # would have thrown both away
        return kind_of(y, rate)           # voice | digital | data
    if flat < 0.20:
        return "tone"                     # structured but static: a spur
    # Everything reaching here ALREADY cleared pres >= 8.0 above, so it has a
    # measured carrier — often tens of dB of one. "noise" was self-contradictory
    # in this branch, and it was where continuous digital data landed: a
    # well-designed digital signal demodulates to something flat AND does not
    # vary in level, so it misses the dynamics gate too. Twelve lines up the
    # code already admits "continuous digital streams do not vary like speech"
    # and then gates on exactly that.
    # "carrier" is the honest answer — a real transmitter we could not
    # characterise — and unlike "noise" it is not the label users filter away
    # first. Neither is in CARRYING, so nothing is promoted by this.
    return "carrier"                      # real transmitter, nothing on it





class Tracker:
    """Remembers signals across laps. Confirmation and 'is anyone actually
    using it' both live here, because both are questions about TIME."""

    # Tracks are indexed into frequency buckets. The obvious version — scan
    # every track to find the nearest — is O(n) per hit, which is invisible at
    # 200 tracks and fatal at 10,000: measured 0.5 s vs 17.6 s of CPU per lap.
    # A day's run accumulates that many easily, so the sweep would grind to a
    # halt after hours. Bucketing makes it O(1) regardless of how long it runs.
    BUCKET = 16_000                       # must be >= TRACK_TOL

    def __init__(self):
        self.t = []
        self.idx = {}

    def _bkt(self, f):
        return int(f // self.BUCKET)

    def _index(self, m):
        m["_b"] = self._bkt(m["freq"])
        self.idx.setdefault(m["_b"], []).append(m)

    def _reindex(self, m):
        b = self._bkt(m["freq"])
        if b != m.get("_b"):
            old = self.idx.get(m["_b"])
            if old and m in old:
                old.remove(m)
            self._index(m)

    def _nearest(self, f):
        b, best, bd = self._bkt(f), None, float("inf")
        for k in (b - 1, b, b + 1):        # a hit near an edge may belong next door
            for m in self.idx.get(k, ()):
                d = abs(m["freq"] - f)
                if d < bd:
                    best, bd = m, d
        return best, bd

    def update(self, hits, lap, now):
        for h in hits:
            m, dist = self._nearest(h["freq"])
            if m and dist <= TRACK_TOL:
                m["freq"] = 0.7 * m["freq"] + 0.3 * h["freq"]
                self._reindex(m)
                m["laps"] += 1
                m["last"] = now
                m["last_lap"] = lap
                m["score"] = max(m["score"], h["score"])
                m["snr"] = h["snr"]
                m["width"] = h["width"]
                # A frequency can legitimately show up more than one way —
                # ADS-B is a burst AND a wide lift. Overwriting made the label
                # flip lap to lap, so collect them instead.
                m.setdefault("kinds", set()).add(h.get("kind", "narrow"))
                if not m["announced"] and m["laps"] >= CONFIRM_LAPS \
                        and m["score"] >= SCORE_MIN:
                    m["announced"] = True
                    yield "NEW", m
            else:
                m = {**h, "kinds": {h.get("kind", "narrow")},
                     "first": now, "last": now, "since": now,
                     "laps": 1, "first_lap": lap, "last_lap": lap,
                     "announced": False}
                self.t.append(m)
                self._index(m)

    def expire(self, now):
        keep = []
        for m in self.t:
            ttl = FORGET_S if m["announced"] else UNCONFIRMED_S
            if now - m["last"] > ttl:
                if m["announced"]:
                    yield "GONE", m
            else:
                keep.append(m)
        if len(keep) != len(self.t):
            self.t = keep
            self.idx = {}
            for m in keep:
                self._index(m)

    def live(self):
        return [m for m in self.t if m["announced"]]


DUTY_MIN_LAPS = 20            # below this the duty figure is meaningless


def duty(m, lap):
    """Fraction of laps this has been present for since first seen.

    This is a DESCRIPTION of behaviour, not a judgement of worth. ~1.0 means a
    continuous transmitter; lower means it comes and goes."""
    span = max(m["last_lap"] - m["first_lap"] + 1, 1)
    return m["laps"] / span


def pattern(m, lap):
    """continuous / bursty / new.

    The reproducibility test caught this: over a short window the label is
    unreliable — a channel seen 3 times in its first 3 laps looks 100%
    'continuous' purely because we have not watched it long enough. Say 'new'
    until there is a real baseline rather than assert something we cannot know.
    """
    span = m["last_lap"] - m["first_lap"] + 1
    if span < DUTY_MIN_LAPS:
        return "new"
    return "continuous" if duty(m, lap) > 0.9 else "bursty"


def targets_from(argv):
    """-> [(name, low_mhz, high_mhz)].  Accepts band names, 'all', or a raw
    'LOW HIGH' pair in MHz."""
    args = [a for a in argv if not a.startswith("-")]
    if len(args) == 2 and all(_isnum(a) for a in args):
        return [("custom", float(args[0]), float(args[1]))]
    names = args or DEFAULT_BANDS
    if "all" in names:
        names = list(BANDS)
    out = []
    for n in names:
        if n not in BANDS:
            return None
        lo, hi, _ = BANDS[n]
        out.append((n, lo, hi))
    return out


def _isnum(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


# --- the board -------------------------------------------------------------
# Ordered by FREQUENCY, high to low, and it never reorders. A row means "this
# channel has had data on it"; how long ago is carried by colour alone, green
# fading to grey over five minutes. Nothing is ever removed.

board = {"rows": [], "lap": 0, "elapsed": 0.0, "bands": "", "updated": 0.0}
board_lock = threading.Lock()

# Labels the user has unticked. This does NOT merely hide rows — the sweep
# skips those frequencies altogether, so the lap gets shorter and everything
# still selected gets revisited more often. That is the entire point: spend
# the radio's time where it is wanted.
MUTE_FILE = "muted.json"
muted = set()
muted_lock = threading.Lock()

# --- one radio, two modes ---------------------------------------------------
# SWEEP looks at everything and knows almost nothing about each channel.
# BAND parks on one range and watches every channel in it continuously.
# They cannot run at once: there is one dongle and it can only be in one place.
# The handover is deliberately slow and explicit — the current capture finishes,
# state is cleared, and the new mode starts fresh. Nothing is shared between
# them, so there is no way for one mode's leftovers to appear in the other.
mode = {"mode": "sweep", "lo": 0.0, "hi": 0.0, "tag": "",
        "gen": 0, "since": 0.0}
mode_lock = threading.Lock()


def tag_blocks(tag):
    """CONTIGUOUS blocks for a label, low to high.

    A tag can appear in several places that are nowhere near each other:
    "federal" is 148-150, 162.56-174 AND 406.1-420. Taking min-to-max gave
    148-420 MHz — 144 slices, 1% coverage, useless. Touching ranges merge;
    separated ones stay separate and are watched one at a time."""
    r = sorted((a, b) for a, b, t in LABELS if t == tag)
    if not r:
        return []
    out = [list(r[0])]
    for a, b in r[1:]:
        if a <= out[-1][1] + 0.001:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]


def tag_range(tag, rows=None):
    """The block worth watching: the one with the most channels detected in
    it, so clicking "federal" lands where the traffic actually is."""
    blocks = tag_blocks(tag)
    if not blocks:
        return (None, None)
    if not rows:
        return blocks[0]

    def inside(b):
        return [r["freq"] for r in rows if b[0] <= r["freq"] < b[1]]

    blocks = sorted(blocks, key=lambda b: -len(inside(b)))
    b = blocks[0]
    seen = inside(b)
    # Narrow to where signals ACTUALLY are. Some allocations are huge and do
    # not subdivide honestly — 450-470 is interleaved channel by channel
    # between the business and public safety pools, so any range split would
    # be invented. But watching all 20 MHz costs 11 slices and 9% coverage,
    # when the traffic may sit in 3 MHz of it. Bounding to the detected span
    # (plus a margin, so a new channel just outside is still found) is honest:
    # it claims nothing about allocations, only about where signals were seen.
    if len(seen) >= 3:
        lo2, hi2 = min(seen) - 0.3, max(seen) + 0.3
        if (hi2 - lo2) < (b[1] - b[0]) * 0.75:
            return (max(b[0], lo2), min(b[1], hi2))
    return b


def load_muted():
    """The browser is not the source of truth — it may not even be open. The
    sweep has to honour these from the moment it starts."""
    try:
        with open(MUTE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_muted(tags):
    try:
        with open(MUTE_FILE, "w") as f:
            json.dump(sorted(tags), f)
    except Exception:
        pass


def is_muted(mhz):
    with muted_lock:
        return label_for(mhz) in muted


# Lap durations, most recent last. A lifetime average hides the effect of
# anything you change while it is running — untick a band and the number barely
# moves for an hour. Three laps is short enough to respond, long enough not to
# jump around.
lap_times = []


LIVE_MARGIN = 2.5
LIVE_MIN_S = 60.0


def live_hold(blind_s):
    """How long a channel stays LIVE after its last sighting.

    One rule for both modes: at least as long as we are NOT looking at it,
    plus a margin. The only difference is what "not looking" means — a full lap
    on the sweep, the slice rotation in band mode.

    The floor of 30 s is about conversations, not about the radio. Two people
    going back and forth leave 3-15 s holes between overs, and a 7 s hold sat
    right in the middle of that, so a channel in active use flashed on and off
    with the rhythm of the conversation. LIVE now means "in use" rather than
    "a carrier is up this instant" — a deliberate trade, since the flashing
    made the display unusable."""
    return max(blind_s + LIVE_MARGIN, LIVE_MIN_S)


def apply_heard(rows):
    """Upgrade rows that whisper transcribed real words on.

    Kept separate from attach_bookmarks() because an earlier refactor of the
    bookmark block silently deleted both call sites of this: the log filled
    with confirmations while every row stayed unmarked.
    """
    for r in rows:
        txt = heard_recently(r["freq"] * 1e6)
        if txt:
            r["verdict"], r["said"] = "voice", txt


def attach_bookmarks(rows):
    """Flag bookmarked rows and return which bookmarks were matched.

    Nearest row within a few kHz, not exact equality: a weak FM signal's
    spectral peak wanders a couple of kHz between laps, so 462.7250 (GMRS 22)
    can be reported as 462.7200, and an exact-match bookmark would then read
    "not heard" while the traffic sits on the next row down.
    """
    tol = SNAP_HZ * 1.5 / 1e6
    seen = set()
    for r in rows:
        r["fav"], r["note"] = False, ""
    for k, note in marks.items():
        near = [r for r in rows if abs(r["freq"] - k) <= tol]
        if near:
            best = min(near, key=lambda r: abs(r["freq"] - k))
            best["fav"], best["note"] = True, note
            seen.add(k)
    return seen


def publish(tr, lap, t0, bands):
    now = time.time()
    # Snap to the channel grid, then merge anything landing on the same slot —
    # that is what kills drift artefacts without merging real neighbours.
    merged = {}
    for m in tr.live():
        # STICKY channel. Snapping m["freq"] fresh every lap makes a signal
        # sitting near a grid boundary flip between two channel numbers as
        # measurement noise pushes it back and forth — 462.7216 alternated
        # between 462.7200 and 462.7225, and a bookmark on one of them read
        # "not heard" while the traffic showed up on the other. A track keeps
        # the channel it was first assigned and only moves if it has genuinely
        # walked a whole grid step away, which noise cannot do.
        # 0.6 grid steps of hysteresis. The wide 1.5x version was sized for
        # the 900 Hz spread you get from taking the peak BIN; parabolic
        # interpolation cut that to ~40 Hz, so flapping needs a signal within
        # 40 Hz of a boundary and effectively never happens. Wide hysteresis
        # then does harm instead: it pinned a track to whatever channel its
        # FIRST sighting picked and refused to let better measurements correct
        # it. Narrow enough to self-correct, wide enough not to oscillate.
        key = m.get("chan")
        if key is None or abs(m["freq"] - key) > SNAP_HZ * 0.6:
            key = int(round(m["freq"] / SNAP_HZ) * SNAP_HZ)
            m["chan"] = key
        cur = merged.get(key)
        if cur is None or m["last"] > cur["last"]:
            merged[key] = m

    # Same rule as band mode: a channel is LIVE if it was heard within the
    # time it takes us to come back to it. On the sweep that is one lap, not
    # the slice rotation — but the principle is identical, and without it a
    # channel blinks off every lap for reasons that have nothing to do with
    # the air.
    lap_now = (sum(lap_times[-3:]) / len(lap_times[-3:])) if lap_times else 3.0
    live_for = live_hold(lap_now)
    rows = []
    for key, m in merged.items():
        d = duty(m, lap)
        rows.append({"freq": round(key / 1e6, 4),
                     "meas": round(m["freq"] / 1e6, 4),
                     "band": m.get("band", "?"),
                     "snr": round(m["snr"], 1),
                     "width": round(m["width"] / 1000, 1),
                     "score": round(m["score"], 2),
                     "age": round(now - m["last"], 1),
                     "on": (now - m["last"]) <= live_for,
                     "duty": round(d, 3),
                     "pattern": pattern(m, lap),
                     "tag": label_for(key / 1e6),
                     "verdict": m.get("verdict", "?"),
                     "kind": "+".join(sorted(m.get("kinds",
                                                    {m.get("kind","narrow")})))})
    apply_heard(rows)
    seen = attach_bookmarks(rows)
    # A bookmark must stay on the board even when the channel is silent and its
    # track has expired — otherwise the thing you bookmarked disappears exactly
    # when you want to check on it. Missing ones come back as placeholders.
    for k, note in marks.items():
        if k not in seen:
            # age/snr are None, not 0 or 1e9: this channel has NOT been
            # heard, which is different from "heard at 0 dB a very long time
            # ago". 1e9 rendered as "16666666m40s".
            rows.append({"freq": k, "tag": label_for(k), "snr": None,
                         "verdict": "?", "kind": "narrow", "age": None,
                         "on": False, "fav": True, "note": note,
                         "width": 0.0, "score": 0.0, "duty": 0.0,
                         "pattern": "", "band": "", "quiet": True})
    # Bookmarked channels pin to the top. Everything else stays in frequency
    # order, which is what the user asked for originally.
    rows.sort(key=lambda r: (not r["fav"], r["freq"]))

    counts = {t: 0 for t in ALL_TAGS}
    counts["(none)"] = 0
    for r in rows:
        k = r["tag"] or "(none)"
        counts[k] = counts.get(k, 0) + 1
    with board_lock:
        board["tags"] = counts
        board["default_off"] = DEFAULT_OFF
        with muted_lock:
            board["muted"] = sorted(muted)
        board["rows"] = rows
        board["mode"] = "sweep"
        board["lap"] = lap
        board["elapsed"] = round(now - t0, 1)
        board["lap_s"] = (round(sum(lap_times[-3:]) / len(lap_times[-3:]), 1)
                          if lap_times else None)
        # confirmed rows are the visible ones; total includes every one-off
        # candidate still inside the FORGET_S window
        board["tracks"] = len(tr.t)
        board["bands"] = bands
        board["fade"] = FADE_S
        board["live"] = sum(1 for r in rows if r["on"])
        board["live_for"] = round(live_for, 1)
        board["updated"] = now


PAGE = """<!doctype html><meta charset=utf-8><title>on the air</title>
<style>
*{box-sizing:border-box}
body{font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;background:#0d0f12;
     color:#dfe3e8;margin:0;padding:18px 20px}
h1{font:600 15px/1 -apple-system,system-ui,sans-serif;margin:0 0 3px}
.hdr{color:#6b7480;font-size:11px;margin-bottom:16px}
.key{color:#5a6270;font-size:10px;margin-bottom:14px}
.key i{display:inline-block;width:9px;height:9px;border-radius:2px;
       vertical-align:-1px;margin:0 3px 0 9px}
#filt{display:flex;flex-wrap:wrap;gap:5px 7px;margin:0 0 14px;
      padding-bottom:12px;border-bottom:1px solid #1d2229}
#filt label{display:inline-flex;align-items:center;gap:4px;font-size:11px;
      color:#aeb6c2;background:#171b21;border:1px solid #242b34;border-radius:5px;
      padding:3px 7px;cursor:pointer;user-select:none}
#filt label.off{opacity:.42;text-decoration:line-through}
#filt label.zero{opacity:.5}
#filt input{margin:0;cursor:pointer}
#filt .c{color:#6b7480;font-variant-numeric:tabular-nums}
#filt button#unus{color:#c98f6f;border-color:#3a2e26}
/* column filters: these only HIDE ROWS. The radio keeps scanning everything,
   unlike the band checkboxes above, which actually stop it going there. */
/* Excel-style column filters. These only HIDE ROWS — the radio keeps
   scanning everything, unlike the band checkboxes, which stop it going there. */
thead th.fx{cursor:pointer;user-select:none;position:relative}
thead th.fx:hover{color:#9aa5b3}
thead th .car{font-size:8px;margin-left:4px;opacity:.6}
thead th.act{color:#4ec27a}
.pop{position:absolute;top:100%;left:0;z-index:9;background:#141920;
     border:1px solid #2a323c;border-radius:7px;padding:8px 6px;min-width:150px;
     box-shadow:0 8px 26px #0009;font:12px/1.5 -apple-system,system-ui,sans-serif;
     text-transform:none;letter-spacing:0}
.pop label{display:flex;align-items:center;gap:7px;padding:3px 7px;
     border-radius:4px;cursor:pointer;color:#cfd6df;font-weight:400}
.pop label:hover{background:#1b2129}
.pop input{margin:0}
.pop .n{margin-left:auto;color:#6b7480}
.pop .act{display:flex;gap:6px;border-top:1px solid #232a33;margin-top:6px;
     padding-top:7px}
.pop button{flex:1;font:inherit;font-size:11px;background:#1b222a;color:#cfd6df;
     border:1px solid #2a323c;border-radius:5px;padding:3px 0;cursor:pointer}
.pop .note{color:#5a6470;font-size:10px;padding:5px 7px 0}
#fnote{background:#1d1a12;border:1px solid #3d3520;color:#d8c48a;font-size:11px;
       border-radius:6px;padding:7px 11px;margin-bottom:12px}
#fnote b{color:#f0dca4}
#fnote button{margin-left:10px;font:inherit;font-size:10px;background:#241f14;
       color:#d8c48a;border:1px solid #3d3520;border-radius:4px;padding:2px 8px;
       cursor:pointer}

.watch{cursor:pointer;color:#4a8ac2;font-size:10px;margin-left:5px;
       border:1px solid #24303c;border-radius:4px;padding:0 4px}
.watch:hover{background:#16222e;color:#7ab6e8}
#mode{background:#12211a;border:1px solid #24503a;border-radius:7px;
      padding:10px 14px;margin-bottom:14px;font-size:12px;color:#9fd8b8}
#mode b{color:#4ec27a}
#mode button{margin-left:12px;font:inherit;font-size:11px;color:#cfd6df;
      background:#1b2229;border:1px solid #2c353f;border-radius:5px;
      padding:4px 11px;cursor:pointer}
#filt button{font:inherit;font-size:10px;color:#8b95a3;background:none;
      border:1px solid #242b34;border-radius:5px;padding:3px 8px;cursor:pointer}
thead th{position:sticky;top:0;z-index:2;background:#0d0f12;color:#6b7480;
         font:500 10px/1 -apple-system,system-ui,sans-serif;text-align:left;
         letter-spacing:.08em;text-transform:uppercase;padding:9px 8px 7px 0;
         border-bottom:1px solid #232932}
thead th.r{text-align:left}
table{border-collapse:collapse;width:100%}
td{padding:4px 8px 4px 0;white-space:nowrap;vertical-align:middle}
tr{border-bottom:1px solid #14181d}
/* live: the whole row goes green, matching band mode */
tr.on{background:#0f1a14}
tr.on td{color:#3ddc84}
tr.on .n,tr.on .b,tr.on .k,tr.on .age{color:#3ddc84}
tr.on .f{color:#3ddc84}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;
     background:#232b35;margin-right:7px;vertical-align:1px}
.dot.on{background:#3ddc84;box-shadow:0 0 7px #3ddc84}
.f{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums;width:1%}
.b{color:#8b96a6;font-size:10px;letter-spacing:.04em;white-space:nowrap}
.k{color:#5f6a78;font-size:9px;letter-spacing:.04em}
.bar{width:74px;height:5px;background:#1b2027;border-radius:3px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:3px}
.n{font-variant-numeric:tabular-nums;color:#8b95a3;font-size:11px}
.age{text-align:left;font-variant-numeric:tabular-nums}
.tag{font-size:10px;padding:1px 6px;border-radius:9px;letter-spacing:.03em}
.v{font-size:10px;padding:1px 7px;border-radius:9px;letter-spacing:.03em}
/* a live row turns every cell green; the verdict badge must NOT follow, or
   "tone" and "noise" read as green and look like things worth listening to */
tr.on td .v{color:inherit}
tr td .v-voice{color:#5ee89a}
tr td .v-digital{color:#5fb0e8}
tr td .v-data{color:#4ec27a}
tr td .v-carrier{color:#c39a6a}
tr td .v-burst{color:#d09ae0}
.star{cursor:pointer;color:#2a323c;margin-right:6px;font-size:12px;
      -webkit-user-select:none;user-select:none}
.star:hover{color:#6b7480}
.star.on{color:#e8c35f}
.nw{white-space:nowrap}
.nh{color:#39414c}
/* what the radio actually measured, next to the channel you would key in.
   The channel is snapped to the 2.5 kHz grid; this is the raw reading, and it
   moves by a kHz or so because the FFT bins are 2.34 kHz wide. */
.meas{color:#4a5361;font-size:10px;margin-left:7px}
/* a voice verdict that whisper backed up by transcribing actual words —
   hover to see them. Structural voice verdicts have no mark. */
.v.hrd{box-shadow:inset 0 0 0 1px #4ec27a}
th.hd,td.hd{color:#8fbf9e;font-size:12px;padding-right:18px}
/* Sits between freq and band, so it is padded on both sides now rather than
   pushed off the right edge. width:1px + nowrap makes the table give it the
   minimum a single glyph needs and hand the slack back to band/heard/notes. */
th.bf,td.bf{text-align:center;padding:0 5px;width:1px;white-space:nowrap}
.bfm{font-size:10px}
.bfm.ok{color:#4ec27a}
.bfm.dg{color:#c2a24e}
.bfm.no{color:#2c333c}
/* Every column except the two text ones shrinks to its content (width:1% with
   nowrap does that in an auto-layout table); heard and notes then split
   whatever is left over. */
th:not(.hd):not(.nt),td:not(.hd):not(.nt){width:1%;white-space:nowrap}
th.hd,td.hd,th.nt,td.nt{width:auto;white-space:normal;text-align:left}
th.nt,td.nt{padding-left:18px}
.note{width:100%;box-sizing:border-box;background:transparent;
      border:1px solid transparent;border-radius:4px;color:#c9b482;
      font:inherit;font-size:12px;padding:3px 6px}
.note::placeholder{color:#39414c}
.note:hover{border-color:#232a33}
.note:focus{outline:none;background:#0d1116;border-color:#e8c35f;color:#e8c35f}
tr.fav td{background:#171307}
tr td .v-tone,tr td .v-noise,tr td .v-quiet{color:#7c8794;background:#1c1f24}
.v-data{background:#123524;color:#4ec27a;font-weight:600}
/* a real carrier we could not characterise because it was too SHORT — not a
   failure worth hiding, it is how pagers, TETRA and telemetry look */
.v-burst{background:#2a1b30;color:#d09ae0;font-weight:600}
.v-voice{background:#123524;color:#5ee89a;font-weight:600}
.v-digital{background:#132a3a;color:#5fb0e8;font-weight:600}
.v-carrier{background:#2b2118;color:#b08a5f}
.v-quiet,.v-noise{background:#1c1f24;color:#5f6a78}
.v-na{background:none;color:#3d444d}
.empty{color:#5a6270;padding:26px 0}
</style>
<h1>on the air</h1>
<div class=hdr id=hdr>starting&hellip;</div>
<div class=key>age<i style="background:#3ddc84"></i>live<i style="background:#7a8a80"></i>1-15m<i style="background:#5a6270"></i>15m+</div>
<div id=mode style=display:none></div>
<div id=filt></div>
<div id=fnote style="display:none"></div>
<div id=out></div>
<script>
var FADE=300, fsig='', saved=localStorage.getItem('off.v2');
// ONE definition of carrying for the whole page. The filter and the count
// each had their own copy of the list, so adding a verdict meant editing two
// places and 'burst' was missed by both.
var CARRYING=new Set(['voice','digital','data','burst']);
var HID={carrying:new Set(),type:new Set(),age:new Set()};
try{var _h=JSON.parse(localStorage.getItem('colhide')||'{}');
    for(var k in HID) (_h[k]||[]).forEach(function(v){HID[k].add(v)});}catch(e){}
function saveHid(){var o={};for(var k in HID)o[k]=[...HID[k]];
  localStorage.setItem('colhide',JSON.stringify(o));}
function ageBucket(r){return r.on?'LIVE':(r.age!=null&&r.age<900)?'1-15m':'over 15m';}
function colVal(r,c){return c==='carrying'?(r.verdict||'?')
  :c==='type'?(r.kind||'narrow'):ageBucket(r);}
function closePops(){document.querySelectorAll('.pop').forEach(function(p){p.remove()});}
var lastHtml='';
function setBody(html){
  // Rebuilding the table every tick closed any open menu and stole clicks
  // mid-press. Skip the rebuild entirely while a menu is open, and otherwise
  // only touch the DOM when something actually changed.
  if(document.querySelector('.pop'))return;
  // Never rebuild out from under someone typing a note — it would drop the
  // keystroke and the caret. Same reason the open-menu check above exists.
  var a=document.activeElement;
  if(a&&a.classList&&a.classList.contains('note'))return;
  if(html===lastHtml)return;
  lastHtml=html;
  document.getElementById('out').innerHTML=html;
  document.querySelectorAll('th.fx').forEach(function(th){
    var c=th.dataset.c;
    if(HID[c].size){th.classList.add('act');
      th.title='hiding: '+[...HID[c]].join(', ')+' \u2014 click to change';}
    else th.removeAttribute('title');
    th.onclick=function(e){
      if(e.target.closest('.pop'))return;
      if(th.querySelector('.pop')){closePops();return;}
      openPop(th,c,window.PRE||[]);};});
}
document.addEventListener('click',function(e){
  if(!e.target.closest('.pop')&&!e.target.closest('th.fx'))closePops();});
function openPop(th,col,rows){
  closePops();
  var vals={};rows.forEach(function(r){var v=colVal(r,col);vals[v]=(vals[v]||0)+1});
  var order=col==='age'?['LIVE','1-15m','over 15m']
    :col==='carrying'?['voice','digital','data','burst','carrier','tone','noise','quiet','?']
    :Object.keys(vals).sort();
  order=order.filter(function(v){return vals[v]!==undefined||HID[col].has(v)});
  var p=document.createElement('div');p.className='pop';
  var h='';
  order.forEach(function(v){
    h+='<label><input type=checkbox '+(HID[col].has(v)?'':'checked')+
       ' data-v="'+v+'"><span>'+(v==='?'?'unknown':v)+'</span>'+
       '<span class=n>'+(vals[v]||0)+'</span></label>';});
  h+='<div class=act><button data-a=all>all</button>'+
     '<button data-a=none>none</button></div>'+
     '<div class=note>display only \u2014 still scanning</div>';
  p.innerHTML=h;
  p.querySelectorAll('input').forEach(function(i){
    i.onchange=function(){i.checked?HID[col].delete(i.dataset.v)
                                   :HID[col].add(i.dataset.v);
      saveHid();tick();};});
  p.querySelector('[data-a=all]').onclick=function(){HID[col].clear();saveHid();closePops();tick();};
  p.querySelector('[data-a=none]').onclick=function(){
    order.forEach(function(v){HID[col].add(v)});saveHid();closePops();tick();};
  th.appendChild(p);
}

window.ONLYD=!!localStorage.getItem('onlyd');
var off=new Set(saved?JSON.parse(saved):null);
var virgin=!saved, pushed=false;
function push(){
  localStorage.setItem('off.v2',JSON.stringify([...off]));
  fetch('/mute?tags='+[...off].map(encodeURIComponent).join(','));
}
function filters(tags){
  var keys=Object.keys(tags);
  var sig=keys.join('|');
  if(sig===fsig){
    var f=document.getElementById('filt');
    keys.forEach(function(t){
      var l=f.querySelector('label[data-t="'+CSS.escape(t)+'"]');
      if(l){l.querySelector('.c').textContent=tags[t];
            l.classList.toggle('zero',!tags[t]);}});
    return;
  }
  fsig=sig;
  var h='';
  for(var i=0;i<keys.length;i++){var t=keys[i],on=!off.has(t);
    h+='<label class="'+(on?'':'off')+(tags[t]?'':' zero')+'" data-t="'+t+'"><input type=checkbox '+
       (on?'checked':'')+'> '+t+' <span class=c>'+tags[t]+'</span>'+
       '<span class=watch title="monitor this band continuously">watch</span>'+
       '</label>';}
  h+='<label id=onlyd class="'+(window.ONLYD?'':'off')+'"><input type=checkbox '+
     (window.ONLYD?'checked':'')+'> only carrying data</label>'+
     '<button id=all>all</button><button id=unus title="untick only the '+
     'encrypted bands \u2014 nothing else changes">unusable</button>'+
     '<button id=none>none</button>';
  var f=document.getElementById('filt'); f.innerHTML=h;
  f.querySelectorAll('.watch').forEach(function(w){
    w.onclick=function(e){e.preventDefault();e.stopPropagation();
      watch(w.parentNode.dataset.t);};});
  f.querySelectorAll('label').forEach(function(l){
    var inp=l.querySelector('input'); if(!inp)return;
    inp.onchange=function(){
      var t=l.dataset.t;
      this.checked?off.delete(t):off.add(t);
      l.className=this.checked?'':'off'; push(); tick();};});
  var od=document.getElementById('onlyd');
  od.querySelector('input').onchange=function(){
    window.ONLYD=this.checked; localStorage.setItem('onlyd',this.checked?'1':'');
    od.className=this.checked?'':'off'; fsig=''; tick();};
  document.getElementById('all').onclick=function(){off.clear();fsig='';push();tick()};
  // adds the never-decodable ones to whatever is already unticked, and
  // deliberately leaves every other choice exactly as the user left it
  document.getElementById('unus').onclick=function(){
    (window.DEF_OFF||[]).forEach(function(t){off.add(t)});
    fsig='';push();tick()};
  document.getElementById('none').onclick=function(){
    keys.forEach(function(t){off.add(t)});fsig='';push();tick()};
}
function ago(s){return s<1?'now':s<60?Math.round(s)+'s':Math.floor(s/60)+'m'+Math.round(s%60)+'s'}
// green -> grey, linear in age, parked at grey past FADE
function age_col(s){
  if(s==null)return '#39414c';        // never heard: not a colour on the fade
  var t=Math.min(s/FADE,1);
  var r=Math.round(0x3d+(0x5a-0x3d)*t),
      g=Math.round(0xdc+(0x62-0xdc)*t),
      b=Math.round(0x84+(0x70-0x84)*t);
  return 'rgb('+r+','+g+','+b+')';
}
function snr_w(s){return Math.min(100,Math.max(4,s/35*100))}
// ONE handler on the container, not one per row.
//
// The table is rebuilt roughly twice a second and has ~1000 rows, so handlers
// bound to individual <span>s are destroyed and recreated constantly. If a
// rebuild lands between mouse-down and mouse-up the element you pressed no
// longer exists and the click is silently lost — which is why starring a
// channel sometimes appeared to do nothing. The container is never replaced,
// so a delegated handler cannot be lost this way.
var tableWired=false;
function wireTable(){
  if(tableWired) return;
  var out=document.getElementById('out');
  if(!out) return;
  tableWired=true;
  out.addEventListener('click',function(e){
    var s=e.target.closest && e.target.closest('.star');
    if(!s) return;
    e.preventDefault(); e.stopPropagation();
    var on=!s.classList.contains('on');
    s.classList.toggle('on', on);
    var tr=s.closest('tr'); if(tr) tr.classList.toggle('fav', on);
    fetch('/fav?f='+s.dataset.f+'&on='+(on?1:0));
    fsig='';                        // rebuild so pinning takes effect
  });
  out.addEventListener('change',function(e){
    var n=e.target.closest && e.target.closest('.note');
    if(!n) return;
    fetch('/fav?f='+n.dataset.f+'&note='+encodeURIComponent(n.value));
  });
  out.addEventListener('focusout',function(e){
    var n=e.target.closest && e.target.closest('.note');
    if(!n) return;
    fetch('/fav?f='+n.dataset.f+'&note='+encodeURIComponent(n.value));
  });
}
function esc(t){return (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
// A UV-5R-class Baofeng covers 136-174 and 400-520 MHz, FM only. Outside that
// it refuses the entry outright — the "cancel" the user saw keying in 250.0125
// and 386.1500. In range but digital (P25/DMR) it receives and you hear hash,
// which is worth marking differently from a plain yes.
function bfMark(r){
  var f=r.freq, inband=(f>=136&&f<=174)||(f>=400&&f<=520);
  if(!inband) return '<span class="bfm no" title="outside 136-174 / 400-520 MHz \u2014 the radio will not accept it">\u00b7</span>';
  if(r.verdict==='digital') return '<span class="bfm dg" title="in range, but digital \u2014 you will hear hash, not speech">\u25cb</span>';
  return '<span class="bfm ok" title="in range and analog \u2014 your Baofeng can hear this">\u25cf</span>';
}
async function watch(tag){await fetch('/mode?m=band&tag='+encodeURIComponent(tag));fsig='';tick()}
async function sweep(){await fetch('/mode?m=sweep');fsig='';tick()}
function bandView(d){
  var h='<table><thead><tr><th>freq MHz</th><th>carrying</th><th>snr</th>'+
        '<th>heard</th><th>airtime</th><th>longest</th><th>last</th>'+
        '</tr></thead><tbody>';
  for(var i=0;i<d.rows.length;i++){var r=d.rows[i];
    h+='<tr'+(r.on?' style="background:#0f1a14"':'')+'>'+
       '<td class=f style="color:'+(r.on?'#3ddc84':'#dfe3e8')+'">'+r.freq.toFixed(4)+'</td>'+
       '<td><span class="v v-'+r.verdict+'">'+r.verdict+'</span></td>'+
       '<td class=n>'+r.snr.toFixed(1)+' dB</td><td class=n>'+r.count+'x</td>'+
       '<td class=n>'+r.airtime.toFixed(1)+'s</td><td class=n>'+r.longest.toFixed(1)+'s</td>'+
       '<td class=n>'+(r.on?'LIVE':ago(r.age))+'</td></tr>';}
  return h+'</tbody></table>';
}
async function tick(){
  var d=await(await fetch('/board')).json();
  var mb=document.getElementById('mode');
  if(d.mode=='band'){
    mb.style.display='';
    mb.innerHTML='watching <b>'+d.band_tag+'</b> '+d.band_lo+'-'+d.band_hi+
      ' MHz &middot; every channel '+d.watch_pct+'% of the time &middot; '+
      d.live+' live now'+
      '<button onclick="sweep()">back to full sweep</button>';
    document.getElementById('filt').style.display='none';

    document.getElementById('hdr').textContent=
      d.rows.length+' channels seen \u00b7 '+Math.round(d.elapsed)+'s';
    document.getElementById('out').innerHTML=
      d.rows.length?bandView(d):'<div class=empty>listening&hellip;</div>';
    return;
  }
  mb.style.display='none';
  document.getElementById('filt').style.display='';

  FADE=d.fade||300;
  window.DEF_OFF=d.default_off||[];
  if(virgin){virgin=false;window.DEF_OFF.forEach(function(t){off.add(t)});}
  // re-assert on every page load: the server may have restarted since, and a
  // filter the scanner does not know about would silently cost sweep time
  if(!pushed){pushed=true;push();}
  filters(d.tags||{});
  var rows=d.rows.filter(function(r){return !off.has(r.tag||'(none)')});
  if(window.ONLYD) rows=rows.filter(function(r){return CARRYING.has(r.verdict)});
  var pre=rows; window.PRE=pre;
  rows=rows.filter(function(r){
    return !HID.carrying.has(colVal(r,'carrying'))
        && !HID.type.has(colVal(r,'type'))
        && !HID.age.has(colVal(r,'age'));});
  document.getElementById('hdr').textContent=
    rows.length+' shown · '+rows.filter(function(r){return r.on}).length+
    ' live now · '+d.rows.filter(function(r){return CARRYING.has(r.verdict)}).length+
    ' carrying data · lap '+d.lap+' · '+
    (d.lap_s!=null?d.lap_s.toFixed(1):'--')+'s per lap (avg of 3)'+
    (off.size?' · '+off.size+' band(s) skipped, not scanned':'');
  var h='<table><thead><tr>'+
        '<th>freq MHz</th>'+
        '<th class=bf title="can your Baofeng receive this?">bf</th>'+
        '<th>band</th><th>signal</th><th>snr</th>'+
        '<th class=fx data-c=carrying>carrying<span class=car>\u25be</span></th>'+
        '<th class=fx data-c=type>shape<span class=car>\u25be</span></th>'+
        '<th class="fx r nw" data-c=age>last seen<span class=car>\u25be</span></th>'+
        '<th class=hd>heard</th>'+
        '<th class=nt>notes</th>'+
        '</tr></thead><tbody>';
  for(var i=0;i<rows.length;i++){var r=rows[i],c=age_col(r.age);
    var cls=(r.on?'on':'')+(r.fav?' fav':'');
    h+='<tr'+(cls?' class="'+cls+'"':'')+'><td class=f'+(r.on?'':' style="color:'+c+'"')+'>'+
       '<span class="star'+(r.fav?' on':'')+'" data-f="'+r.freq.toFixed(4)+'" '+
       'title="bookmark this channel">\u2605</span>'+
       '<span class="dot'+(r.on?' on':'')+'"></span>'+r.freq.toFixed(4)+
       (r.meas!=null&&Math.abs(r.meas-r.freq)>0.0004
          ?'<span class=meas>rx '+r.meas.toFixed(4)+'</span>':'')+'</td>'+
       '<td class=bf>'+bfMark(r)+'</td>'+
       '<td class=b>'+(r.tag||'')+'</td>'+
       '<td><div class=bar>'+(r.snr==null?'':'<i style="width:'+snr_w(r.snr)+'%;background:'+c+'"></i>')+'</div></td>'+
       '<td class=n>'+(r.snr==null?'<span class=nh>\u2014</span>':r.snr.toFixed(1)+' dB')+'</td>'+
       '<td><span class="v v-'+(!r.verdict||r.verdict=='?'?'na':r.verdict)+(r.said?' hrd':'')+'">'+
         (r.verdict=='?'?'\u00b7\u00b7\u00b7':r.verdict)+'</span></td>'+
       '<td class=k>'+(r.kind==='narrow'?'':r.kind)+'</td>'+
       '<td class="n age nw">'+(r.on?'LIVE':r.age==null
          ?'<span class=nh>not heard</span>':ago(Math.round(r.age/5)*5))+'</td>'+
       '<td class=hd>'+(r.said?esc(r.said):'')+'</td>'+
       '<td class=nt>'+(r.fav
         ?'<input class=note data-f="'+r.freq.toFixed(4)+'" placeholder="add a note\u2026" value="'+
          (r.note||'').replace(/"/g,'&quot;')+'">'
         :'')+'</td></tr>';
  }
  if(!rows.length)h+='<tr><td colspan=10 class=empty>'+
    (pre.length?'every row is filtered out \u2014 use a column menu to bring them back'
              :'nothing confirmed yet\u2026')+'</td></tr>';
  setBody(h+'</tbody></table>');
  wireTable();
  var hid=[];
  for(var c in HID) if(HID[c].size) hid.push(c+': '+[...HID[c]].join(', '));
  var fn=document.getElementById('fnote');
  if(hid.length){fn.style.display='';
    fn.innerHTML='<b>'+(pre.length-rows.length)+' rows hidden</b> by column '+
      'filters &mdash; '+hid.join(' &middot; ')+
      '<button id=clrf>clear all filters</button>';
    document.getElementById('clrf').onclick=function(){
      for(var k in HID)HID[k].clear();saveHid();lastHtml='';tick();};}
  else fn.style.display='none';

}
tick();setInterval(tick,1200);
</script>"""


class _H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/mode"):
            import urllib.parse
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                      if "?" in self.path else "")
            want = q.get("m", ["sweep"])[0]
            with mode_lock:
                if want == "band":
                    tag = urllib.parse.unquote_plus(q.get("tag", [""])[0])
                    with board_lock:
                        rows = list(board.get("rows", []))
                    lo, hi = tag_range(tag, rows)
                    if q.get("lo"):
                        lo, hi = float(q["lo"][0]), float(q["hi"][0])
                    if lo is not None:
                        # bump `gen` so a band ALREADY running notices the
                        # range changed. Without this, clicking one band then
                        # another silently kept the first: run_band only read
                        # the range when it started.
                        mode.update({"mode": "band", "lo": lo, "hi": hi,
                                     "tag": tag or f"{lo}-{hi}",
                                     "gen": mode.get("gen", 0) + 1,
                                     "since": time.time()})
                else:
                    mode.update({"mode": "sweep", "since": time.time()})
            body, ctype = b'{"ok":1}', "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/fav"):
            import urllib.parse
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                      if "?" in self.path else "")
            try:
                f = round(float(q["f"][0]), 4)
            except (KeyError, ValueError):
                f = None
            if f is not None:
                if "note" in q:
                    if f in marks:
                        marks[f] = q["note"][0]
                elif q.get("on", ["1"])[0] == "1":
                    marks.setdefault(f, "")
                else:
                    marks.pop(f, None)
                save_marks(marks)
            body, ctype = b'{"ok":1}', "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/mute"):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            val = ""
            for part in q.split("&"):
                if part.startswith("tags="):
                    val = part[5:]
            import urllib.parse
            tags = [urllib.parse.unquote_plus(t)
                    for t in val.split(",") if t]
            with muted_lock:
                muted.clear()
                muted.update(tags)
                save_muted(muted)
            body, ctype = b'{"ok":1}', "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/board"):
            with board_lock:
                body, ctype = json.dumps(board).encode(), "application/json"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# Band mode keys `live` and `hist` directly off the snapped frequency with no
# tracker merging behind it, so it keeps the coarse grid — a finer one would
# split a wobbling signal across two keys and show it twice.
BAND_SNAP_HZ = 12_500
BAND_BLOCK_S = 0.25
BAND_ROTATE_S = 2.0
BAND_SNR_MIN = 8.0          # parked, the same bin is seen hundreds of times,
                            # so marginal noise fires constantly. Higher bar.


def run_band(r, gains, spurs, t0):
    """Park on one band until the mode changes. Returns when told to stop.

    Everything here is local: the sweep's tracker, baseline and schedule are
    untouched, and this function's state dies when it returns. The two modes
    share the radio and nothing else."""
    with mode_lock:
        lo, hi = mode["lo"] * 1e6, mode["hi"] * 1e6
        tag, gen = mode["tag"], mode.get("gen", 0)
    n_sl = max(1, int(math.ceil((hi - lo) / 1_900_000.0)))
    stepw = (hi - lo) / n_sl
    centers = [lo + stepw * (i + 0.5) for i in range(n_sl)]
    # A channel must stay "live" for at least as long as we are NOT looking at
    # it, or it drops every rotation and the display flickers for reasons that
    # have nothing to do with the air. With N slices we are blind to any given
    # channel for (N-1) x ROTATE seconds. Scales with whatever band is picked.
    hang = live_hold((n_sl - 1) * BAND_ROTATE_S)
    print(f"\n>>> BAND MODE  {tag}  {lo/1e6:.3f}-{hi/1e6:.3f} MHz  "
          f"{n_sl} slice(s), each channel watched {100/n_sl:.0f}% of the time"
          f", holding live for {hang:.1f}s between visits")
    r.set_gain(GAIN_LADDER[-2])
    live, hist, si = {}, {}, 0
    nblk = int(BAND_BLOCK_S * RATE)
    while True:
        with mode_lock:
            if mode["mode"] != "band":
                print(f">>> leaving band mode after "
                      f"{time.time()-mode['since']:.0f}s\n")
                return
            if mode.get("gen", 0) != gen:
                print(">>> band changed, restarting on the new range\n")
                return
        c = centers[si % len(centers)]
        si += 1
        r.tune(c)
        r.flush()
        t_end = time.time() + BAND_ROTATE_S
        while time.time() < t_end:
            with mode_lock:
                if mode["mode"] != "band" or mode.get("gen", 0) != gen:
                    break
            iq = r.read(nblk)
            ts = time.time()
            hits = [h for h in analyse(iq, c)
                    if h["snr"] >= BAND_SNR_MIN
                    and not is_spur(h["freq"], spurs)]
            # One transform per block, shared by every hit in it — the same
            # duplication the sweep path had. Computed lazily because most
            # blocks have no hit that still needs audio.
            spec = None
            for h in hits:
                key = int(round(h["freq"] / BAND_SNAP_HZ) * BAND_SNAP_HZ)
                m = live.get(key)
                if m is None:
                    m = live[key] = {"first": ts, "audio": [], "snr": h["snr"],
                                     "verdict": None, "blocks": 0}
                    st = hist.setdefault(key, {"verdict": None, "count": 0,
                                               "airtime": 0.0, "longest": 0.0,
                                               "snr": h["snr"], "last": ts})
                    st["count"] += 1
                m["last"] = ts
                m["blocks"] += 1
                m["snr"] = max(m["snr"], h["snr"])
                hist[key]["last"] = ts
                hist[key]["snr"] = max(hist[key]["snr"], h["snr"])
                if m["verdict"] is None:
                    try:
                        from prove import channelize, spectrum, CHAN_RATE
                        if spec is None:
                            spec = spectrum(iq)
                        m["audio"].append(channelize(iq, RATE, key - c,
                                                     CHAN_RATE, pre=spec))
                    except Exception:
                        pass
            now = time.time()
            for key, m in list(live.items()):
                from prove import metrics, CHAN_RATE
                got = sum(len(a) for a in m["audio"]) / CHAN_RATE
                if m["verdict"] is None and got >= 1.2:
                    aud = np.concatenate(m["audio"])
                    try:
                        v = classify(aud, CHAN_RATE)   # same call the sweep makes
                    except Exception as e:
                        print(f"  [band {key/1e6:.4f}] {e!r}", flush=True)
                        v = None
                    m["verdict"] = v
                    # v is None only if classify raised. Leave the displayed
                    # verdict alone in that case and let the next 1.2 s of
                    # audio try again — overwriting a good verdict with None
                    # would erase a real answer because of a transient fault.
                    #
                    # Otherwise keep whichever answer is MORE SPECIFIC, the
                    # same rule the sweep uses. This used to read
                    # `!= "data"`, which pinned a channel to "data" forever and
                    # blocked the one upgrade worth having: data -> voice.
                    if v is not None and \
                            specificity(v) >= specificity(hist[key]["verdict"]):
                        hist[key]["verdict"] = v
                    m["audio"] = []
                if now - m["last"] > hang:
                    # airtime counts only blocks actually OBSERVED. Wall-clock
                    # from first to last sighting would include the seconds we
                    # were watching another slice, inflating every number.
                    obs = m["blocks"] * BAND_BLOCK_S
                    hist[key]["airtime"] += obs
                    hist[key]["longest"] = max(hist[key]["longest"],
                                               m["last"] - m["first"])
                    del live[key]
            publish_band(hist, live, centers, tag, lo, hi, t0)


def publish_band(hist, live, centers, tag, lo, hi, t0):
    now = time.time()
    rows = [{"freq": k / 1e6, "tag": label_for(k / 1e6),
             # "?" not "carrier": a channel that never accumulated 1.2 s of
             # audio was never judged, and calling that a carrier is a claim we
             # did not measure. It made band mode look like it disagreed with
             # the sweep on channels it had simply not looked at yet.
             "verdict": s["verdict"] or "?", "snr": round(s["snr"], 1),
             "count": s["count"], "airtime": round(s["airtime"], 1),
             "longest": round(s["longest"], 1),
             "age": round(now - s["last"], 1), "on": k in live,
             "kind": "", "pattern": "", "duty": 0}
            for k, s in hist.items()]
    apply_heard(rows)
    seen = attach_bookmarks(rows)
    # A bookmark must stay on the board even when the channel is silent and its
    # track has expired — otherwise the thing you bookmarked disappears exactly
    # when you want to check on it. Missing ones come back as placeholders.
    for k, note in marks.items():
        if k not in seen:
            # age/snr are None, not 0 or 1e9: this channel has NOT been
            # heard, which is different from "heard at 0 dB a very long time
            # ago". 1e9 rendered as "16666666m40s".
            rows.append({"freq": k, "tag": label_for(k), "snr": None,
                         "verdict": "?", "kind": "narrow", "age": None,
                         "on": False, "fav": True, "note": note,
                         "width": 0.0, "score": 0.0, "duty": 0.0,
                         "pattern": "", "band": "", "quiet": True})
    # Bookmarked channels pin to the top. Everything else stays in frequency
    # order, which is what the user asked for originally.
    rows.sort(key=lambda r: (not r["fav"], r["freq"]))
    with board_lock:
        board["rows"] = rows
        board["mode"] = "band"
        board["band_tag"] = tag
        board["band_lo"] = round(lo / 1e6, 3)
        board["band_hi"] = round(hi / 1e6, 3)
        board["watch_pct"] = round(100.0 / max(len(centers), 1))
        board["live"] = len(live)
        board["elapsed"] = round(now - t0, 1)


def main(argv):
    targets = targets_from(argv[1:])
    if not targets:
        print("usage: scan.py [band ...] | [LOW_MHZ HIGH_MHZ] | all")
        for n, (lo, hi, d) in BANDS.items():
            print(f"  {n:8} {lo:7.1f}-{hi:<7.1f} {d}")
        return 1

    if BACKEND == "rsp":
        try:
            r = radio.Rsp(0, RATE, GAIN_DB)
        except Exception as e:
            print(f"RSP1B present but would not open: {e}")
            print("  Is SDRconnect (the GUI) running? Only one app can hold it.")
            return 1
        print(f"      {r.name}  14-bit  {RATE/1e6:.3f} Msps  "
              f"{RATE*USABLE/1e6:.2f} MHz usable/tune")
    else:
        idx = radio.find("R828D")
        if idx is None:
            print("No radio found. Attached RTL devices:")
            for i, n, t in radio.devices():
                print(f"  #{i} {n} [{t}]")
            print("  (an RSP1B needs SDRconnect installed; see "
                  "docs/rsp1b-macos.md)")
            return 1
        r = radio.Rtl(idx, RATE, GAIN_DB)
    span = RATE * USABLE
    # OVERLAP the steps. `span` is the usable width of one capture, and it was
    # also being used as the step SPACING, so consecutive usable windows abutted
    # exactly with nothing to spare. The comment in analyse() says a group
    # clipped at the edge is fine because "the neighbouring step covers it
    # properly" — with zero overlap that is simply false. A channel sitting on
    # a seam is at the top edge of step k AND the bottom edge of step k+1, and
    # the edge test drops it in BOTH. 392 seams across 0.5-2000 MHz.
    #
    # Bench, strong NBFM stepped in from the usable edge, 5 trials each:
    #
    #     inside edge by   60k   40k   20k   10k    5k    0
    #     detected         5/5   5/5   5/5   0/5   0/5   0/5
    #
    # 60 kHz of overlap covers that with margin. Costs 5 extra steps out of
    # 393, about half a second a lap.
    STEP_OVERLAP = 60_000
    step = span - STEP_OVERLAP
    n_samp = FRAMES * NFFT
    plan = [(n, lo, hi, max(1, math.ceil((hi - lo) * 1e6 / step)))
            for n, lo, hi in targets]
    steps = sum(p[3] for p in plan)
    mhz = sum(hi - lo for _, lo, hi, _ in plan)

    print(f"scan  {mhz:.0f} MHz over {len(plan)} band(s):")
    for n, lo, hi, st in plan:
        print(f"      {n:8} {lo:7.1f}-{hi:<7.1f} {st:3d} steps")
    who = (getattr(r, "name", None) or f"RTL #{getattr(r, 'index', 0)} "
           f"[{getattr(r, 'tuner', '?')}]")
    print(f"      {who}  {steps} steps/lap  "
          f"{BIN_HZ:.0f} Hz bins  {FRAMES*NFFT/RATE*1000:.0f} ms dwell")
    print(f"      confirm after {CONFIRM_LAPS} laps, score >= {SCORE_MIN}")

    web = any(a.startswith("--web") for a in argv[1:])
    if web:
        port = WEB_PORT
        for a in argv[1:]:
            if a.startswith("--web="):
                port = int(a.split("=", 1)[1])
        serve(port)
        print(f"      board -> http://127.0.0.1:{port}/")
    print()

    tr = Tracker()
    gains = Gains(GAIN_DB)
    import concurrent.futures
    # ~0.2 s to judge a channel; a busy slice has dozens. numpy drops the GIL
    # for FFTs, so threads genuinely use the cores here.
    # 4, not 8. classify() is ~40 SMALL numpy calls on 14x4096 arrays, so per-call
    # GIL handoff dominates and extra workers thrash. Measured on a 40-channel
    # slice: serial 34 ms, pool(2) 21, pool(3) 18, pool(4) 17, pool(6) 24,
    # pool(8) 25, pool(12) 26.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    if whisper_ok():
        threading.Thread(target=whisper_worker, daemon=True).start()
        print("      listening for speech in the background (whisper, "
              "off the radio's path)")
    else:
        print("      whisper not found — structural classification only")
    sched = Schedule()
    with muted_lock:
        muted.update(load_muted())
        if muted:
            print(f"      skipping (not scanned): {', '.join(sorted(muted))}")
    spurs = load_spurs()
    if spurs:
        print(f"      excluding {len(spurs)} measured internal spurs")
    t0 = time.time()
    lap = 0
    usb_fail = 0
    last_pub = 0.0
    bands_str = " ".join(n for n, _, _, _ in plan)
    try:
        while True:
            with mode_lock:
                in_band = mode["mode"] == "band"
            if in_band:
                run_band(r, gains, spurs, t0)
                # coming back: the sweep's own state is intact, but the board
                # belonged to band mode, so mark it as sweep again
                with board_lock:
                    board["mode"] = "sweep"
                continue
            lap += 1
            lap_start = time.time()
            # Nudge every lap. A real signal sits at the same absolute
            # frequency regardless; a tuner spur is locked to a fixed OFFSET
            # from centre, so it moves and never confirms. Cheap, and it kills
            # the one false positive that persistence alone cannot.
            jitter = random.uniform(-100e3, 100e3)
            lap_levels = []
            for name, low, high, nsteps in plan:
                for k in range(nsteps):
                    center = low * 1e6 + step * (k + 0.5) + jitter
                    key = (name, k)
                    if is_muted((low * 1e6 + step * (k + 0.5)) / 1e6):
                        continue          # unticked: do not spend radio time
                    if not sched.due(key, lap):
                        continue
                    g_db = gains.for_step(key)
                    try:
                        r.set_gain(g_db)
                        r.tune(center)
                        r.flush()
                        iq = r.read(n_samp)
                    except RuntimeError as e:
                        # USB dropped out — almost always the dongle being
                        # unplugged. Rebuild it rather than letting the
                        # exception end the process and lose the whole board.
                        usb_fail += 1
                        print(f"  [radio] {e} ({usb_fail}) — reopening",
                              flush=True)
                        time.sleep(min(2.0 * usb_fail, 10.0))
                        # Loop until it comes back. `r` is already closed by
                        # now, so falling through with the old object is what
                        # produced the Bad-file-descriptor crash that killed
                        # the board. reopen() backs off internally, so waiting
                        # here for a replug costs nothing.
                        nr = None
                        while nr is None:
                            nr = reopen(r, RATE, g_db)
                        r, usb_fail = nr, 0
                        print("  [radio] back", flush=True)
                        continue
                    usb_fail = 0
                    gains.adapt(key, iq, getattr(r, "overloaded", None))

                    hits = analyse(iq, center)

                    # WIDE is judged after the lap, against NEIGHBOURING
                    # frequencies rather than against this step's own history —
                    # an always-on wideband signal is its own history, so it
                    # would hide from a memory-based test forever.
                    lap_levels.append((center, name, level_db(iq, g_db)))

                    # BURST: too fast for any FFT here. Rare per capture, so it
                    # is confirmed by accumulating over laps like everything
                    # else rather than by one lucky look.
                    nb, us = bursts(iq)
                    if nb >= BURST_MIN:
                        # The pulse COUNT used to be stuffed into "snr". It is
                        # not decibels, but it was displayed in the SNR column
                        # as dB and — worse — squared by worth() to allocate
                        # verify radio time, so a 40-pulse burst outranked a
                        # real 30 dB carrier 1.78 to 1. It gets its own field
                        # now; snr carries the actual level over the floor.
                        hits.append({"freq": round(center / span) * span,
                                     "snr": float(BURST_SNR_DB), "pulses": nb,
                                     "width": span, "persist": 1.0,
                                     "prom": 10.0, "stab": 0.0,
                                     "score": 1.0, "kind": "burst",
                                     "us": us})

                    # drop anything the dongle generates itself
                    hits = [h for h in hits if not is_spur(h["freq"], spurs)]
                    if hits or nb >= BURST_MIN:
                        sched.mark(key, lap)   # worth coming back to, for a while
                    for h in hits:
                        h["band"] = name
                        h.setdefault("kind", "narrow")
                    now = time.time()
                    for kind, m in tr.update(hits, lap, now):
                        print(f"[{now-t0:6.1f}s] NEW   {m['freq']/1e6:10.4f} MHz"
                              f"  {m['band']:7} snr {m['snr']:5.1f}  "
                              f"w {m['width']/1000:5.1f}k  score {m['score']:.2f}")
                    # Rate-limited. This used to run on EVERY step — 908 times
                    # a lap, ~14 times a second — rebuilding every row,
                    # re-sorting and recounting tags, while the browser polls
                    # under once a second. Over 90% of that work was discarded
                    # unseen, and it grows with the track count: 0.68 s/lap at
                    # 500 tracks, 4.65 s/lap at 12000.
                    if web and time.time() - last_pub > 0.5:
                        publish(tr, lap, t0, bands_str)
                        last_pub = time.time()
            # --- WIDE, once per lap -------------------------------------
            # Each step against the median of its neighbours. The tuner and
            # antenna make level drift smoothly with frequency, so a LOCAL
            # median is the honest reference; a global one would just flag
            # whichever end of the spectrum happens to be hotter.
            wide_hits = []
            if len(lap_levels) >= 7:
                lap_levels.sort()
                lv = np.array([x[2] for x in lap_levels])
                for i, (c, nm, v) in enumerate(lap_levels):
                    a, b = max(i - 6, 0), min(i + 7, len(lv))
                    nb = np.concatenate([lv[a:i], lv[i+1:b]])
                    if nb.size < 4:
                        continue
                    lift = v - float(np.median(nb))
                    if lift > WIDE_MIN_DB:
                        wide_hits.append({"freq": round(c / span) * span, "snr": lift,
                                          "width": span, "persist": 1.0,
                                          "prom": lift, "stab": 0.0,
                                          "score": min(lift / 12.0, 1.0),
                                          "kind": "wide", "band": nm})
            if wide_hits:
                now = time.time()
                for kind, m in tr.update(wide_hits, lap, now):
                    print(f"[{now-t0:6.1f}s] NEW   {m['freq']/1e6:10.4f} MHz"
                          f"  {m['band']:7} WIDE  +{m['snr']:.1f} dB over "
                          f"neighbours")

            # Clamped: a laptop sleeping for hours recorded the gap as ONE
            # lap, and live_hold() averages the last 3 laps, so every row on
            # the board read LIVE for the next three laps. Same failure on an
            # NTP step backwards.
            lap_times.append(min(max(time.time() - lap_start, 0.0), 120.0))
            del lap_times[:-10]
            # --- the mission test, one capture per SLICE --------------
            # Group the channels needing a verdict by which 1.9 MHz capture
            # would contain them, then judge each group from a single capture.
            # Radio time is per slice, not per channel, so a slice with 40
            # confirmed channels costs the same 1.2 s as one with a single
            # channel. The backlog clears in about one pass instead of ten
            # minutes of round-robin.
            now = time.time()
            live_now = tr.live()
            fresh = [m for m in live_now if m.get("verdict") is None]
            stale = [m for m in live_now
                     if m.get("verdict") is not None
                     and now - m.get("vt", 0.0) > (REVERIFY_IDLE_S if not fresh
                                                   else REVERIFY_S)]
            due = fresh + stale
            if due:
                # Never-judged channels form the queue on their own whenever
                # any exist; only when the board is fully answered does
                # re-checking get the radio.
                groups = {}
                for m in (fresh if fresh else due):
                    c = round(m["freq"] / span) * span
                    groups.setdefault(c, []).append(m)
                # Busiest-first alone STARVES sparse slices. Measured: NOAA at
                # 41.7 dB sat unjudged for 56 laps because its slice held ~4
                # channels and denser ones always won. Throughput is worthless
                # if the strongest signal on the board never gets looked at.
                #
                # Score each slice by how many channels it would resolve AND
                # how long they have waited, so a starved slice climbs until it
                # wins no matter how thin it is. Never-checked channels count
                # as having waited a long time, so nothing is judged twice
                # before something else is judged once.
                def worth(kv):
                    c, ms = kv
                    waited = min(max(now - m.get("vt", 0.0) for m in ms), 3600.0)
                    strongest = max(m["snr"] for m in ms)
                    # STRENGTH dominates. A 42 dB signal unanswered while a
                    # dense slice of 5 dB channels is judged first is the wrong
                    # order — obvious signals should be resolved while you are
                    # looking at them. Squaring makes 42 dB outweigh 5 dB by
                    # ~70x, so density can no longer bury a strong channel,
                    # while count and waiting still break ties among equals.
                    return (strongest ** 2) * waited * (len(ms) ** 0.5)
                order = sorted(groups.items(), key=worth, reverse=True)
                for center, members in order[:VERIFY_SLICES_PER_LAP]:
                    by_freq = {m["freq"]: m for m in members}
                    # Every OTHER confirmed channel that falls inside this same
                    # capture. They are not due for a verdict, but the audio is
                    # already paid for, so hand it to whisper.
                    half = span / 2
                    extra = [m["freq"] for m in live_now
                             if abs(m["freq"] - center) < half
                             and m["freq"] not in by_freq]
                    try:
                        r.set_gain(GAIN_LADDER[-2])
                        res = verify_slice(r, center, list(by_freq), pool,
                                           also_listen=extra)
                    except Exception:
                        continue
                    apply_verdicts(res, by_freq, t0)

            for kind, m in tr.expire(time.time()):
                print(f"[{time.time()-t0:6.1f}s] GONE  {m['freq']/1e6:10.4f} MHz"
                      f"  after {m['last']-m['first']:.0f}s")
            if lap % 20 == 0:
                report(tr, lap, time.time() - t0)
                if whisper_ok():
                    print(f"  listen: {listen_stats}", flush=True)
                print(f"  scheduler: {sched.hot_count()} hot steps of {steps}"
                      f"  (cold steps still checked 1 lap in {COLD_EVERY})")
    except KeyboardInterrupt:
        print()
        report(tr, lap, time.time() - t0)
    finally:
        r.close()
    return 0


def report(tr, lap, elapsed):
    live = tr.live()
    print(f"\n--- lap {lap}   {elapsed:.0f}s   {len(live)} signals ---")
    if not live:
        print("  (nothing confirmed yet)\n")
        return
    print(f"  {'freq MHz':>10}  {'band':<7} {'snr':>5}  {'width':>7}  "
          f"{'score':>5}  {'on':>4}  {'seen':>6}  pattern")
    for m in sorted(live, key=lambda x: (x.get("band", ""), -x["snr"])):
        d = duty(m, lap)
        pat = pattern(m, lap)
        print(f"  {m['freq']/1e6:10.4f}  {m.get('band','?'):<7} {m['snr']:5.1f}"
              f"  {m['width']/1000:6.1f}k  {m['score']:5.2f}  {d:4.0%}  "
              f"{m['last']-m['first']:5.0f}s  {pat}")
    print()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
