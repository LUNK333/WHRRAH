# WiFi gyro sync: handoff

**Goal (mobile WHRRAH):** line up DJI Osmo video with AiM/other logs by gyro
correlation, using only the camera's motion track pulled over WiFi. No video
download.

**Status:** the data path works and is validated. The orientation track comes
off the camera over WiFi (verified on a real Osmo Action 4). It decodes to the
same data telemetry_parser gives WHRRAH's Data Wizard today (verified on 6
local clips). Not built yet: a mobile app, and a timed full-clip fetch over WiFi.

## What's here

| file | what |
|---|---|
| `osmo_wifi_gyro.py` | Standard library only. Range readers (local file / camera HTTP), MP4 `moov` walk, `djmd` protobuf decode, telemetry_parser-compatible timebase, CLI. ~400 lines, written to be ported. |
| `validate.py` | Compares against telemetry_parser and WHRRAH's own `video_gyro_signal()` on a folder of clips. |

```bash
py wifi_gyro_sync/validate.py "E:/Videos/Canaan NESVT 10242026"
py wifi_gyro_sync/osmo_wifi_gyro.py --file "E:/Videos/.../DJI_..._D.MP4" --frames 0:1500 --csv out.csv
py wifi_gyro_sync/osmo_wifi_gyro.py --camera DCIM/DJI_001/DJI_..._D.MP4 --storage 0 --frames 0:1500
```

## How it plugs into WHRRAH

The Data Wizard (`source/WHRRAH.py`) does this today:
`video_gyro_signal(path)` → telemetry_parser → one quaternion per frame →
|angular velocity| → `correlate_signals()` against the `xrk_rotation_signal()`
YawRate/RollRate/PitchRate magnitude (20 Hz grid, 0.25 s steps).

`whrrah_samples(track)` returns the same `(t_sec, w, x, y, z)` per-frame list
that `_quaternion_samples_from_blocks()` builds. So the remote path replaces
only the telemetry_parser call; the rest of the pipeline stays unchanged.

**Validation (6 clips, 4.1–7.3 GB, Osmo Action 4 fw 10.00.37.91, 25 fps):**
- IMU sample counts are identical to telemetry_parser (531,620 – 940,875 per clip).
- Per-sample time is within 1.2 ms of telemetry_parser.
- WHRRAH's angular-velocity signal has the same point count, with timestamps within 1.2 ms and values within ~1 °/s (from the 1 ms timing difference only).
- A 60 s window fetched from mid-clip has the same timing accuracy as a whole-clip fetch.
- The raw quaternions differ from telemetry_parser's by a fixed axis remap. Angular speed is unaffected (checked to 1e-10°). Port telemetry_parser's remap only if absolute orientation is ever needed.

## Getting the bytes: camera transport

Proven on Windows by OsmoOffload (`C:\Users\Luke\Documents\Python\OsmoOffload`,
a clone of github.com/intermittech/OsmoOffload). The protocol reference is
`osmosis/MEDIA_PROTOCOL.md` (github.com/KonradIT/osmosis), which also has an
Android app that implements all of this.

1. **BLE** (GATT `fff5` write / `fff4` notify, DUML frames): pair with
   `0x07/0x45` (reuse one persisted 32-char identifier, or the camera prompts
   again), then wake (`0x00/0x2b` keepalive ~1 Hz), then read the SSID
   (`0x07/0x07`) and password (`0x07/0x0e`). Code: `OsmoOffload/src/osmooffload/ble/`.
2. **Join the camera AP** (WPA2, 5 GHz, no internet, camera at 192.168.2.1).
3. **HTTP:** `GET http://192.168.2.1/v2?storage=<0 SD|1 internal>&path=DCIM/DJI_001/<name>.MP4`.
   lighttpd, no auth, honours `Range`. The file list (names, store, duration,
   fps, resolution) comes over the UDP datalink (`0x00/0x26`, `camera/session.py`).
   For the HTTP-only path you can also probe the store with HEAD.

Lessons from getting step 2 working on Windows (both fixed in the OsmoOffload
clone, uncommitted: `core/connect.py`, `net/wifi.py`):
- **Never send `0x07/0x47` ConnectToWiFi** as a "wake the AP" nudge. It makes
  the camera try to join a network as a client, which drops its own AP (BLE
  went quiet 2 s later).
- The AP comes up within a few seconds, but Windows' cached network list
  (`netsh wlan show networks`) lags ~45 s. Force a scan (WlanScan) instead.
  Mobile OSes have their own version of this (Android `WifiNetworkSpecifier`,
  iOS `NEHotspotConfiguration`); untested here.
- Only one client at a time: the desktop OsmoOffload app holding the camera
  makes everything else fail to associate.

## djmd format (Osmo Action 4, `dvtm_ac203.proto`)

One `djmd` sample per video frame (~1 KB). Each sample is its own chunk,
interleaved with video in `mdat`, so it costs **one Range request per frame**.
`moov` sits at the end of the file (1.3–1.9 MB) and costs 6 requests.
Field meanings below are inferred; the full layout is in the module docstring.

- `3.1.2`: frame timestamp, µs, nominal (+40000/frame).
- `3.3.2.1`: IMU block timestamp, µs. A **u32 that wraps every 71.6 min**, and
  it wrapped mid-clip on clip 0404, so take differences mod 2³².
- `3.3.2.3`: 40 or 41 quaternions per frame = 1 kHz.
- `3.3.2.4`: ms offset, about −7.6 to −8.2.
- Frame 0 header: model, firmware, IMU rate (1000), measured fps (24.999065).

Timebase (matches telemetry_parser):
`t_ms = ((imu_ts − imu_ts0) mod 2³² / 1000 + j) × fps/25 − offset_ms`

## Costs

| | requests | bytes | measured |
|---|---|---|---|
| `moov` (index) | 6 | 1.3–1.9 MB | 0.11–0.21 s over the camera's 5 GHz WiFi |
| full clip gyro (9–16 min) | 13k–24k | 15–26 MB | **not measured over WiFi yet** |
| 60 s window | ~1,500 | ~3 MB | not measured over WiFi yet |

Local disk does ~0.1 ms/request. Over WiFi, per-request latency is the
unknown. If it's ~5–15 ms, a full clip takes 1–6 min and a 60 s window 8–25 s.

## Open questions / next steps

1. **Measure a WiFi fetch.** Join the AP, run the CLI with `--camera … --frames 0:1500`,
   and record ms/request.
2. **Cut the request count.** Options:
   - HTTP multi-range (`Range: bytes=a-b,c-d,…`). lighttpd's static handler supports it, but `/v2` is untested.
   - Pipelining.
   - Several parallel connections.
   - Correlating on windows instead of whole clips. WHRRAH's `correlate_signals` already works on the overlap, so a few 30–60 s windows with motion may be enough.
3. **Clips without the 1 kHz block.** A 59.94 fps Action 4 clip
   (`E:/Videos/BMWCCA 060825/…_0077_D.MP4`) has no `3.3` block, and
   telemetry_parser returns nothing for it either, so today's Data Wizard can't
   gyro-match it. It does carry one quaternion per frame (`3.2.9`, now in
   `GyroFrame.frame_quat`) plus gravity (`3.2.10`). That signal follows the
   1 kHz motion (correlation 0.77–0.88) but **lags by 0.9–1.6 s, not
   constant**, so it may be a smoothed path; I haven't checked. Not usable for
   precise sync as is. What decides whether the 1 kHz block is present
   (fps? stabilization mode?) is unknown.
4. **Other bodies** (Action 5 Pro / 6, Pocket 3/4, Nano) use different
   `dvtm_*.proto` files. Check against telemetry_parser before trusting them.
5. **Coarse pre-filter.** On this camera `mvhd` creation_time is real UTC
   (19:08:32Z for a file named 15:08:31 local, EDT). WHRRAH's
   `read_mp4_creation_time()` treats it as naive local time, so its EXIF
   fallback would be 4 h off for these clips. The WiFi file list's mtime
   (camera local time, 2 s resolution) is another source.
6. **Mobile port:** pick the stack. The decoder is small enough to port
   directly; telemetry_parser (Rust) could also be cross-compiled.
