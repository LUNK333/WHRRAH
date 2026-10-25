"""Check osmo_wifi_gyro against telemetry_parser / WHRRAH on local clips.

  py wifi_gyro_sync/validate.py "E:/Videos/Canaan NESVT 10242026"

For each DJI clip: same IMU sample count, per-sample time error vs
telemetry_parser, and WHRRAH's |angular velocity| signal computed both ways.
A local file goes through the same Range-read code path as the camera does.
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import telemetry_parser

sys.path.insert(0, str(Path(__file__).resolve().parent))
import osmo_wifi_gyro as g  # noqa: E402

try:  # the real Data Wizard function, if WHRRAH's deps import cleanly
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
    from WHRRAH import video_gyro_signal  # noqa: E402
except Exception as e:  # noqa: BLE001
    print(f"(WHRRAH import failed: {e!r}; skipping its signal check)")
    video_gyro_signal = None


def ang_vel(samples):
    """WHRRAH.video_gyro_signal()'s math, applied to whrrah_samples()."""
    ts, out = [], []
    for k in range(1, len(samples)):
        t1, *q1 = samples[k - 1]
        t2, *q2 = samples[k]
        dt = t2 - t1
        if dt <= 0:
            continue
        rw = max(-1.0, min(1.0, sum(a * b for a, b in zip(q1, q2))))
        ts.append(t2)
        out.append(math.degrees(2 * math.acos(abs(rw))) / dt)
    return ts, out


def check(clip: Path) -> None:
    print(f"== {clip.name} ({clip.stat().st_size / 1e9:.1f} GB)")
    t0 = time.time()
    tp = [b["Quaternion"]["Data"] for b in telemetry_parser.Parser(str(clip)).telemetry() if b.get("Quaternion")]
    tp_t = [s["t"] for b in tp for s in b]
    t_tp = time.time() - t0

    t0 = time.time()
    r = g.FileRangeReader(str(clip))
    track = g.fetch_gyro(r)
    t_ours = time.time() - t0
    ours = g.samples_1khz(track)
    print(f"  {track.header.model} fw {track.header.firmware}, {track.total_frames} frames, "
          f"fps {track.header.fps:.6f}; read {r.bytes / 1e6:.1f} MB in {r.requests} reads "
          f"({t_ours:.1f}s; telemetry_parser {t_tp:.1f}s)")
    if not tp_t:
        print(f"  telemetry_parser: NO quaternions; ours: {len(ours)} samples")
        return
    n = min(len(ours), len(tp_t))
    terr = max(abs(ours[i][0] - tp_t[i]) for i in range(n))
    print(f"  samples: ours {len(ours)}, telemetry_parser {len(tp_t)}; max time error {terr:.2f} ms")

    if video_gyro_signal:
        wt, wv = video_gyro_signal(str(clip))
        ot, ov = ang_vel(g.whrrah_samples(track))
        m = min(len(wv), len(ov))
        print(f"  WHRRAH signal: {len(wv)} vs {len(ov)} points; max |dt| "
              f"{max(abs(a - b) for a, b in zip(wt[:m], ot[:m])) * 1000:.2f} ms, max |d deg/s| "
              f"{max(abs(a - b) for a, b in zip(wv[:m], ov[:m])):.2e}")

    # a 60 s window from the middle, as a phone would fetch it
    mid = track.total_frames // 2
    win = g.fetch_gyro(g.FileRangeReader(str(clip)), range(mid, mid + 1500))
    ws = g.samples_1khz(win)
    k0 = sum(len(f.quats) for f in track.frames[:mid])
    werr = max(abs(ws[i][0] - tp_t[k0 + i]) for i in range(len(ws)))
    print(f"  60 s window @ frame {mid}: {len(ws)} samples, max time error {werr:.2f} ms")


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    for clip in sorted(folder.glob("DJI_*.MP4")):
        check(clip)
