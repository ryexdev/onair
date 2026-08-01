# Getting an SDRplay RSP1B working on macOS (Apple Silicon)

Written while doing it, including the parts that did not work. The scanner
itself does not support this device yet — this is the driver groundwork.

## Why none of this is like the RTL-SDR

`rtl.py` is a ctypes wrapper around `librtlsdr`, which is open source and in
Homebrew. The RSP1B shares none of that. It uses SDRplay's own closed-source
API, which runs as a **root launch daemon** that clients talk to over IPC; the
daemon is the only thing that touches USB. So the port is not "swap a library",
it is a different driver model.

## What has to be installed, in order

1. **SDRplay API** — from https://www.sdrplay.com/downloads/ . V3.14 or later
   is required for the RSP1B specifically; 3.15.1 was used here. It is a `.pkg`
   that needs admin rights, installs to `/Library/SDRplayAPI/<version>/`, drops
   headers in `/usr/local/include/` and `libsdrplay_api.dylib` in
   `/usr/local/lib/`, and registers `/Library/LaunchDaemons/com.sdrplay.service.plist`.
   There is no Homebrew formula. Checked: no `sdrplay` in any tap.

2. **SoapySDRPlay3** — the SoapySDR plugin. Also not in Homebrew (`soapyrtlsdr`
   exists, `soapysdrplay` does not). Build it:

   ```
   git clone --depth 1 https://github.com/pothosware/SoapySDRPlay3.git
   cd SoapySDRPlay3 && mkdir build && cd build
   cmake .. -DCMAKE_BUILD_TYPE=Release && make -j8
   cp libsdrPlaySupport.so /opt/homebrew/lib/SoapySDR/modules0.8/
   ```

   It finds the API automatically if step 1 is done. Version built here: 0.5.2.

## Checking each layer separately

Do not debug this end-to-end; the layers fail differently and the errors are
misleading. Check them in order.

**Is the device on USB at all?** It publishes *no product name*, so looking for
"SDRplay" or "RSP" in `system_profiler` finds nothing and looks like a dead
device. Search by vendor ID instead — `0x1DF7` is 7671 decimal:

    ioreg -p IOUSB -w0 -l | grep '"idVendor" = 7671'

**Is the API installed and matching?** Compile this against the headers; it
answers "is the library there, does its version match, does it see a device"
in one shot, without SoapySDR in the way:

```c
#include <stdio.h>
#include "sdrplay_api.h"
int main(void){
    float ver=0.0f;
    printf("Open: %s\n", sdrplay_api_GetErrorString(sdrplay_api_Open()));
    sdrplay_api_ApiVersion(&ver);
    printf("version %.2f (header %.2f)\n", ver, SDRPLAY_API_VERSION);
    sdrplay_api_DeviceT devs[6]; unsigned int n=0;
    sdrplay_api_LockDeviceApi();
    sdrplay_api_ErrT e = sdrplay_api_GetDevices(devs, &n, 6);
    printf("GetDevices: %s count=%u\n", sdrplay_api_GetErrorString(e), n);
    sdrplay_api_UnlockDeviceApi(); sdrplay_api_Close(); return 0;
}
```

Link it against the dylib directly — the shipped library has no `LC_RPATH`, so
`-lsdrplay_api` builds but fails at runtime with
`Library not loaded: @rpath/libsdrplay_api.so.3`:

    clang -I/usr/local/include probe.c /usr/local/lib/libsdrplay_api.dylib \
          -Wl,-rpath,/usr/local/lib -o probe

**Is the plugin loaded?** `SoapySDRUtil --probe="driver=sdrplay"`. If the
module were missing you would get "no driver"; if it says *"no available RSP
devices found"* the plugin is fine and the problem is below it.

## THE LOG THAT ACTUALLY TELLS YOU ANYTHING

    /Library/Logs/sdrplayservice_err.log

Nothing else reports the real error. The API returns a bare
`sdrplay_api_Fail`, SoapySDR says "No devices found", and neither mentions the
cause. The daemon's log had:

    libusb: warning [darwin_open] USBDeviceOpen: another process has device
            opened for exclusive access
    libusb: error [darwin_claim_interface] USBInterfaceOpen: another process
            has device opened for exclusive access
    libusb: warning [libusb_exit] application left some devices open

On macOS, libusb opens a device **exclusively** — two processes cannot hold it
even on different interfaces. Watch the file size while you probe; if it grows
on each attempt, the failure is live rather than historical.

## Things that were checked and were NOT the problem

* **Architecture.** All three binaries (`libsdrplay_api.dylib`,
  `sdrplay_apiService`, its bundled `libusb`) are universal x86_64 + arm64 on
  an arm64 host.
* **API version.** 3.15 both in the library and the header, above the 3.14
  the RSP1B needs.
* **The daemon not running.** `launchctl print system/com.sdrplay.service`
  showed `state = running` with `last exit code = (never exited)`.
* **A leaked handle from the daemon's own retries.** Suspected, because
  `libusb_exit` was leaving devices open, but a freshly restarted daemon
  (new PID, 25 s old) failed identically.
* **Another SDR application holding it.** Nothing else was running.
* **A kernel driver claiming it.** No `IOUSBHostInterface` children under the
  device node. I first read that as "its configuration was never set", which
  was wrong — `kUSBCurrentConfiguration = 1`, so the device IS configured.
  Something opened it and set the configuration, then did not release it.
* **macOS 26 (Tahoe) USB restrictions.** Tahoe added USB Restricted Mode for
  accessories connected during boot, and desktop Macs have no "Allow
  accessories to connect" setting to check. But nothing was logged: no denial,
  no "accessory not enabled", nothing matching the vendor ID in the USB
  subsystem log. Not the cause here, worth ruling out on a laptop.

## Restarting the daemon — SIGTERM, not a hard kill

    sudo killall -TERM sdrplay_apiService

This is the fix reported by the SDRplay community, and the signal matters.
`launchctl kickstart -k` can SIGKILL the daemon, which gives libusb no chance
to release the device — so it stays claimed by a process that no longer
exists, and the freshly restarted daemon hits the same
"another process has device opened for exclusive access". A restart that
appears to change nothing is the symptom of using the wrong signal.
`KeepAlive = true` brings it back a second later either way.

If a probe still fails afterwards, the order matters — the daemon enumerates
on hotplug, so it must be running and clean *before* the device appears:

1. unplug the RSP1B
2. `sudo killall -TERM sdrplay_apiService`
3. wait ~5 s for KeepAlive to bring it back
4. plug it back in
5. probe

## Notes for when the port happens

`Rtl(index, rate, gain)` with `.tune()`, `.read()`, `.flush()`, `.close()` is a
small enough interface that a SoapySDR-backed class can sit behind it and leave
`scan.py` and `prove.py` untouched. What will NOT carry over:

* `spurs.json` — 30 spurs measured on one specific RTL dongle
* the 28.8 / 12 / 27 MHz clock combs — different hardware, different clocks,
  so the frequencies we are blind at will move
* `GAIN_LADDER` — the RSP uses LNA state plus IF gain reduction, not a single
  tuner gain in tenths of a dB
* `RATE`, `USABLE`, `NFFT` and the 2.34 kHz bin size — with ~8 MHz usable the
  sweep geometry changes and 908 steps becomes roughly 220
* every threshold in `classify()` — they were measured through an 8-bit ADC.
  A 14-bit ADC has a different noise floor, so `SNR_MIN`, the presence floor
  and the flatness gates all need re-checking against known channels rather
  than assumed to transfer.

## Outcome as of API 3.15.1 on macOS 26.3.1 (Tahoe): NOT WORKING

Every layer verified correct, and it still does not enumerate:

* API opens, `sdrplay_api_ApiVersion` returns 3.15 matching the header, so the
  client-to-daemon IPC is fine
* `sdrplay_api_GetDevices` returns `sdrplay_api_Fail`, count 0
* the daemon log shows libusb unable to claim the device, every attempt
* device is on the bus, configured (`kUSBCurrentConfiguration = 1`)
* all binaries universal arm64, daemon running, plugin loading

Tried and did not help: restarting the daemon with `launchctl kickstart -k`;
restarting it with SIGTERM; unplug then SIGTERM then replug, in that order, so
nothing could be holding the device while the daemon came back. A daemon 19
seconds old fails identically to one that has been up for an hour, which rules
out a leaked handle from its own retries.

Not yet tried: **a reboot with the device attached.** This is worth doing
before concluding anything, because Apple's macOS 26 security notes describe
USB Restricted Mode applying to accessories connected *during boot* — a
different path from hot-plug.

If a reboot does not fix it, the likely answer is that API 3.15.1 predates
macOS 26 and does not support it. 3.15 is the newest on SDRplay's download
page, and no report of an RSP working on Tahoe could be found. That is
SDRplay's to fix.

One honest note on method: a dozen probe runs were made while diagnosing, each
of which opens the API. If any leaked a claim, the debugging was contributing
to the symptom. Probe once after a clean sequence, not repeatedly.

## Root cause found: the firmware upload times out on endpoint 0

After a reboot the daemon log changed character — `transfer error: timed out`
instead of exclusive-access. Streaming the kernel log during one attempt
(`/usr/bin/log stream`, and note `log` is a zsh builtin so the full path is
required) produced the two lines that explain everything:

    IOUSBHostDevice::setConfigurationGated: pid 1860, sdrplay_apiService
        selected configuration 1
    AppleUSBIORequest::complete: device 1 endpoint 0x00:
        status 0xe00002d6 (timeout): 1920 bytes transferred

The daemon opens the device and sets configuration fine, then the firmware
upload — a control transfer on endpoint 0 — dies after 1920 bytes. The device
stops responding mid-upload. Everything downstream (`GetDevices` count 0, the
exclusive-access spam, the self-deadlock) is fallout from the daemon's broken
error path after this one failure.

The self-deadlock mechanism, confirmed by the kernel log naming the pid: the
daemon's first failed attempt leaves the device claimed (libusb warns
"application left some devices open"), the process keeps running, so every
later attempt collides with its own zombie claim — reported misleadingly as
"another process".

## The libusb swap: fixed one layer, worth keeping

The daemon bundles a years-old libusb and loads it via @rpath. Replacing it
with Homebrew's 1.0.30 (arm64, same compatibility version 4.0.0):

    sudo mv /Library/SDRplayAPI/3.15.1/bin/libusb-1.0.0.dylib{,.orig}
    sudo cp /opt/homebrew/opt/libusb/lib/libusb-1.0.0.dylib \
            /Library/SDRplayAPI/3.15.1/bin/libusb-1.0.0.dylib
    sudo killall -TERM sdrplay_apiService

Code signing accepted it (the daemon runs). With it, the open/claim layer is
clean — no more exclusive-access errors on a fresh plug — and the failure is
isolated to the endpoint-0 timeout above. The firmware upload still fails, so
this is necessary-but-not-sufficient. Original library kept as `.orig`.

## Where this stands, and the decisive next test

No public report of this exact failure exists as of writing (searched GitHub
issues, sdrplayusers.net, groups.io, the SDRplay forum). Two candidate
explanations remain: macOS 26 changed control-transfer handling in a way that
breaks the upload, or this RSP1B hardware revision (bcdDevice 0x0200) needs a
newer upload sequence than API 3.15.1 has.

**SDRconnect** — SDRplay's own application — talks to RSPs directly without
the API daemon. If it runs the device, the hardware and OS are fine and the
API daemon is the broken piece (report to SDRplay, and try letting SDRconnect
load the firmware then probing the API against the already-programmed
device). If SDRconnect also fails, the RSP1B/Tahoe combination is broken
below anything user-side software can fix.

## Final diagnosis (2026-07-31): the API daemon's init code, not the Mac

With every variable isolated — fresh daemon, verified cable, no competing
process — the daemon fails identically on first contact:

    IOUSBHostDevice::setConfigurationGated: pid 2629, sdrplay_apiService
        selected configuration 1
    endpoint 0x00: status 0xe00002d6 (timeout): 128 bytes transferred

Meanwhile SDRplay's own SDRconnect app, which ships its OWN device code and
does not use the API daemon, drives the same device on the same cable
successfully: firmware loads, the device re-enumerates, and it streams with
zero USB errors. Three-way isolation:

    device  WORKS   (SDRconnect loaded firmware and streamed)
    cable   WORKS   (2000/2000 clean EP0 transfers; SDRconnect session clean)
    daemon  FAILS   (identical EP0 timeout, every attempt, both cables)

The first bad cable muddied everything early on: it produced real transaction
errors (endpoint 0x81, 0xe00002ed) that mimicked and compounded the daemon
bug. Both were true at once — a bad cable AND a broken daemon. Cables were
proven with an EP0 stress tool (1000x full-size GET_DESCRIPTOR reads) armed
behind a hotplug watcher, so each candidate cable got a PASS/FAIL within
seconds of being plugged in.

Secondary trap: every failed daemon attempt LEAKS an exclusive device claim
("libusb_exit: application left some devices open"), which then blocks
SDRconnect too — kernel names the daemon pid as the holder. So a freshly
restarted daemon makes SDRconnect STOP working, and a wedged-idle daemon lets
it work. If SDRconnect mysteriously cannot see the device, the daemon
probably failed first and is squatting on it.

### Working configuration today

Remove the daemon (verify it actually died — one bootout silently failed):

    sudo launchctl bootout system/com.sdrplay.service
    pgrep -fl sdrplay_apiService   # must print nothing

Then SDRconnect owns the device and works. The scanner cannot use the RSP1B
until SDRplay fixes the API; there is no user-side workaround, since the API
is closed-source and SDRconnect bundles no substitute.

### Bug report for SDRplay (paste-ready)

    Hardware: RSP1B, USB descriptor 0x1DF7:0x3050, bcdDevice 0x0200
    Host: MacBook Pro M2 Max, macOS 26.3.1 (Tahoe)
    API: 3.15.1 (sdrplay_apiService via LaunchDaemon)

    sdrplay_api_Open() and ApiVersion() succeed; sdrplay_api_GetDevices()
    returns sdrplay_api_Fail with count 0 on every attempt.

    Kernel trace of each attempt: the service opens the device, selects
    configuration 1, then a control transfer on endpoint 0 times out
    (IOKit status 0xe00002d6) after 128-1920 bytes. The service then exits
    libusb without releasing the device ("application left some devices
    open"), so the process retains an exclusive kernel claim that blocks
    all later attempts, including its own — the kernel reports "provider is
    already opened for exclusive access by pid <service pid>".

    The same device on the same cable works fully under SDRconnect on the
    same machine (firmware upload, re-enumeration, sustained streaming, no
    USB errors), which isolates the failure to the API service's device
    initialization. Also reproduced with the service's bundled libusb
    replaced by 1.0.30 — identical EP0 timeout, minus the self-deadlock.

    Two requests: fix the init exchange for this hardware revision on
    macOS 26, and release the device handle on the failure path so a failed
    init does not brick the device for every other client until the
    process dies.
