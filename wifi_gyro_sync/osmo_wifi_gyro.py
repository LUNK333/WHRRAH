"""Read a DJI Osmo clip's gyro/orientation track over the camera's WiFi
without downloading the video.

Standard library only. The same code reads a local file (FileRangeReader) or a
clip on the camera (HttpRangeReader), so the output can be checked against
telemetry_parser on a local copy.

How it works:
  1. Walk the MP4's top-level box headers with tiny Range reads; fetch `moov`
     (~1-2 MB, at the END of DJI files).
  2. Find the `djmd` timed-metadata track. It has one ~1 KB sample per video
     frame, each its own chunk, interleaved with video in `mdat`.
  3. Range-read those samples and decode the protobuf (dvtm_ac203.proto on the
     Osmo Action 4). Each frame holds 40 orientation quaternions = 1 kHz.

Per-frame djmd layout (field numbers; names are inferred, not DJI's):
  1   header (frame 0 only): 1.1 proto name / firmware / model, 1.10.1 IMU Hz,
      1.11 measured fps
  2   stream info (frame 0 only): 2.3 width/height/fps
  3   frame:
      3.1.1  frame index (absent on frame 0 = 0)
      3.1.2  frame timestamp, us, camera clock (+40000 per frame at 25 fps)
      3.2.x  exposure: .2 ISO, .3 shutter denominator, .6 colour temp K,
             .9 frame quaternion, .10 gravity vector (g)
      3.3.2.1 IMU block timestamp (us, a second camera clock)
      3.3.2.2 IMU block counter (+1 per frame)
      3.3.2.3 repeated quaternion {1: w?, 2: x?, 3: y?, 4: z?} - 40 per frame
      3.3.2.4 float, ms; -7.6 .. -8.2, likely IMU-to-frame time offset
"""

from __future__ import annotations

import http.client
import struct
from dataclasses import dataclass, field
from urllib.parse import quote

# -- range readers -----------------------------------------------------------


class FileRangeReader:
    def __init__(self, path: str):
        self._f = open(path, "rb")
        self._f.seek(0, 2)
        self.size = self._f.tell()
        self.requests = self.bytes = 0

    def read(self, offset: int, length: int) -> bytes:
        self._f.seek(offset)
        data = self._f.read(length)
        self.requests += 1
        self.bytes += len(data)
        return data

    def close(self) -> None:
        self._f.close()


class HttpRangeReader:
    """GET /v2?storage=<0 SD | 1 internal>&path=DCIM/... with Range headers.

    The camera AP is 192.168.2.1, lighttpd/1.4.55, no auth; the PC/phone must
    already be joined to it (see HANDOFF.md for the BLE wake + WiFi join).
    Keeps one keep-alive connection; reconnects on error.
    """

    def __init__(self, path: str, storage: int, host: str = "192.168.2.1", timeout: float = 15.0):
        self.host, self.timeout = host, timeout
        self.url = f"/v2?storage={storage}&path={quote(path, safe='/')}"
        self._conn: http.client.HTTPConnection | None = None
        self.requests = self.bytes = 0
        self.size = self._head_size()

    def _c(self) -> http.client.HTTPConnection:
        if self._conn is None:
            self._conn = http.client.HTTPConnection(self.host, 80, timeout=self.timeout)
        return self._conn

    def _reset(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _head_size(self) -> int:
        conn = self._c()
        conn.request("HEAD", self.url)
        resp = conn.getresponse()
        resp.read()
        if resp.status != 200:
            raise OSError(f"HEAD {self.url} -> HTTP {resp.status}")
        return int(resp.getheader("Content-Length"))

    def read(self, offset: int, length: int) -> bytes:
        for attempt in range(3):
            try:
                conn = self._c()
                conn.request("GET", self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
                resp = conn.getresponse()
                data = resp.read()
                self.requests += 1
                self.bytes += len(data)
                if resp.status == 206:
                    return data
                if resp.status == 200:
                    return data[offset:offset + length]
                raise OSError(f"HTTP {resp.status}")
            except (OSError, http.client.HTTPException):
                # mid-transfer 404/500s are transient on Action bodies
                self._reset()
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._reset()


# -- MP4 box tree ------------------------------------------------------------

_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


@dataclass
class Track:
    handler: str = "?"
    codec: str | None = None
    timescale: int = 0
    sample_sizes: list[int] = field(default_factory=list)
    chunk_offsets: list[int] = field(default_factory=list)
    stsc: list[tuple[int, int]] = field(default_factory=list)  # (first_chunk, per_chunk)

    def sample_ranges(self) -> list[tuple[int, int]]:
        """(offset, size) of every sample, via stsc/stco/stsz."""
        out: list[tuple[int, int]] = []
        k = 0
        for ci, off in enumerate(self.chunk_offsets, start=1):
            per = next((p for first, p in reversed(self.stsc) if first <= ci), 1)
            for _ in range(per):
                if k >= len(self.sample_sizes):
                    return out
                out.append((off, self.sample_sizes[k]))
                off += self.sample_sizes[k]
                k += 1
        return out


def _boxes(buf: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, btype = struct.unpack_from(">I4s", buf, pos)
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack_from(">Q", buf, pos + 8)[0], 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield btype, pos + hdr, pos + size
        pos += size


def read_moov(reader) -> bytes:
    """Top-level header walk (16 B reads) then one read of moov's body."""
    pos = 0
    while pos + 8 <= reader.size:
        head = reader.read(pos, 16)
        size, btype = struct.unpack_from(">I4s", head)
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack_from(">Q", head, 8)[0], 16
        elif size == 0:
            size = reader.size - pos
        if size < hdr:
            break
        if btype == b"moov":
            return reader.read(pos + hdr, size - hdr)
        pos += size
    raise ValueError("no moov box")


def parse_tracks(moov: bytes) -> list[Track]:
    tracks: list[Track] = []

    def walk(s0: int, e0: int, t: Track | None) -> None:
        for bt, s, e in _boxes(moov, s0, e0):
            if bt == b"trak":
                t2 = Track()
                tracks.append(t2)
                walk(s, e, t2)
            elif t is None:
                if bt in _CONTAINERS:
                    walk(s, e, None)
            elif bt == b"hdlr":
                t.handler = moov[s + 8:s + 12].decode("latin-1")
            elif bt == b"mdhd":
                t.timescale = struct.unpack_from(">I", moov, s + (20 if moov[s] == 0 else 28) - 8)[0]
            elif bt == b"stsd":
                t.codec = moov[s + 12:s + 16].decode("latin-1")
            elif bt == b"stsz":
                fixed, n = struct.unpack_from(">II", moov, s + 4)
                t.sample_sizes = [fixed] * n if fixed else list(struct.unpack_from(f">{n}I", moov, s + 12))
            elif bt in (b"stco", b"co64"):
                n = struct.unpack_from(">I", moov, s + 4)[0]
                t.chunk_offsets = list(struct.unpack_from(f">{n}{'I' if bt == b'stco' else 'Q'}", moov, s + 8))
            elif bt == b"stsc":
                n = struct.unpack_from(">I", moov, s + 4)[0]
                t.stsc = [struct.unpack_from(">II", moov, s + 8 + 12 * k) for k in range(n)]
            elif bt in _CONTAINERS:
                walk(s, e, t)

    walk(0, len(moov), None)
    return tracks


# -- protobuf (schema-less) ---------------------------------------------------


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = sh = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << sh
        sh += 7
        if c < 0x80:
            return r, i


def pb_fields(b: bytes) -> list[tuple[int, int, object]]:
    """[(field, wire_type, value)]; value is int (varint), bytes (len/fixed)."""
    out, i = [], 0
    while i < len(b):
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        elif wt == 2:
            n, i = _varint(b, i)
            v, i = b[i:i + n], i + n
        else:
            raise ValueError(f"wire type {wt}")
        out.append((f, wt, v))
    return out


def _get(b: bytes, *path: int):
    """First value at a field path, e.g. _get(frame, 1, 2)."""
    for depth, f in enumerate(path):
        hit = next((v for ff, _, v in pb_fields(b) if ff == f), None)
        if hit is None:
            return None
        if depth == len(path) - 1:
            return hit
        b = hit
    return None


def _all(b: bytes, f: int) -> list:
    return [v for ff, _, v in pb_fields(b) if ff == f]


def _f32(v) -> float:
    return struct.unpack("<f", v)[0]


# -- djmd decode ---------------------------------------------------------------


@dataclass
class GyroFrame:
    index: int
    frame_ts_us: int | None
    imu_ts_us: int | None
    imu_counter: int | None
    imu_offset_ms: float | None
    quats: list[tuple[float, float, float, float]]  # raw fields 1..4, in order
    # one quaternion per frame (3.2.9), present even when the 1 kHz block is
    # not (seen on a 59.94 fps Action 4 clip)
    frame_quat: tuple[float, float, float, float] | None = None


@dataclass
class ClipHeader:
    proto: str | None = None
    model: str | None = None
    firmware: str | None = None
    imu_hz: int | None = None
    fps: float | None = None


def decode_header(sample0: bytes) -> ClipHeader:
    h = ClipHeader()
    info = _get(sample0, 1, 1)
    if info:
        h.proto = (_get(info, 1) or b"").decode("latin-1") or None
        h.firmware = (_get(info, 6) or b"").decode("latin-1") or None
        h.model = (_get(info, 10) or b"").decode("latin-1") or None
    rate = _get(sample0, 1, 10, 1)
    h.imu_hz = rate if isinstance(rate, int) else None
    fps = _get(sample0, 1, 11, 1)
    h.fps = _f32(fps) if isinstance(fps, bytes) and len(fps) == 4 else None
    return h


def decode_frame(sample: bytes) -> GyroFrame | None:
    frame = _get(sample, 3)
    if frame is None:
        return None
    ident = _get(frame, 1) or b""
    idx = next((v for f, _, v in pb_fields(ident) if f == 1), 0)
    ts = next((v for f, _, v in pb_fields(ident) if f == 2), None)
    imu = _get(frame, 3, 2)
    quats: list[tuple[float, float, float, float]] = []
    imu_ts = imu_ctr = None
    off = None
    if imu:
        for f, _, v in pb_fields(imu):
            if f == 1:
                imu_ts = v
            elif f == 2:
                imu_ctr = v
            elif f == 3:
                q = {ff: _f32(vv) for ff, _, vv in pb_fields(v)}
                quats.append((q.get(1, 0.0), q.get(2, 0.0), q.get(3, 0.0), q.get(4, 0.0)))
            elif f == 4:
                off = _f32(v)
    fq = _get(frame, 2, 9)
    frame_quat = None
    if isinstance(fq, bytes):
        q = {ff: _f32(vv) for ff, _, vv in pb_fields(fq) if len(vv) == 4}
        frame_quat = (q.get(1, 0.0), q.get(2, 0.0), q.get(3, 0.0), q.get(4, 0.0))
    return GyroFrame(idx, ts, imu_ts, imu_ctr, off, quats, frame_quat)


@dataclass
class GyroTrack:
    header: ClipHeader
    frames: list[GyroFrame]
    total_frames: int
    imu_ts0_us: int | None  # frame 0's IMU block timestamp: the t = 0 reference
    requests: int
    bytes_read: int


def fetch_gyro(reader, frames: range | None = None, progress=None) -> GyroTrack:
    """Fetch + decode djmd samples. `frames` limits which video frames (e.g.
    a 60 s window is enough for correlation); frame 0 is always read too, for
    the header and the time reference. One Range request per frame: samples
    are interleaved with video, never adjacent."""
    moov = read_moov(reader)
    track = next((t for t in parse_tracks(moov) if t.codec == "djmd"), None)
    if track is None:
        raise ValueError("no djmd track (not a DJI Osmo clip?)")
    ranges = track.sample_ranges()
    first = reader.read(*ranges[0])
    header = decode_header(first)
    f0 = decode_frame(first)
    want = frames if frames is not None else range(len(ranges))
    out: list[GyroFrame] = []
    for n, i in enumerate(want):
        fr = f0 if i == 0 else decode_frame(reader.read(*ranges[i]))
        if fr is not None:
            out.append(fr)
        if progress and n % 250 == 0:
            progress(n, len(want))
    return GyroTrack(header, out, len(ranges), f0.imu_ts_us if f0 else None,
                     reader.requests, reader.bytes)


def samples_1khz(track: GyroTrack) -> list[tuple[float, float, float, float, float]]:
    """(t_ms, w, x, y, z) for every IMU sample, on telemetry_parser's timebase:

        t_ms = ((imu_ts - imu_ts0) / 1000 + j) * fps / 25 - imu_offset_ms

    (j = index within the frame's burst, fps = header's measured rate). Matched
    telemetry_parser within 1.2 ms over a 531 s Action 4 clip, and works for a
    window that does not start at frame 0. Quaternions are the raw stored
    values; telemetry_parser applies a fixed axis remap on top, which leaves
    angular speed unchanged (verified to 1e-10 deg).
    """
    if track.imu_ts0_us is None:
        return []
    scale = (track.header.fps or 25.0) / 25.0
    out = []
    for fr in track.frames:
        if fr.imu_ts_us is None:
            continue
        # imu_ts is a u32 microsecond counter: it wraps every 71.6 min, and
        # did mid-clip on a real Action 4 recording. A single clip is far
        # shorter than that, so the modular difference is the true elapsed time.
        base = ((fr.imu_ts_us - track.imu_ts0_us) % (1 << 32)) / 1000.0
        off = fr.imu_offset_ms or 0.0
        for j, q in enumerate(fr.quats):
            out.append(((base + j) * scale - off, *q))
    return out


def whrrah_samples(track: GyroTrack) -> list[tuple[float, float, float, float, float]]:
    """(t_sec, w, x, y, z), one per frame (last quaternion of each burst): the
    shape WHRRAH's _quaternion_samples_from_blocks() returns, so it drops into
    video_gyro_signal()'s |angular velocity| step and correlate_signals()."""
    all_s = samples_1khz(track)
    out, i = [], 0
    for fr in track.frames:
        if fr.imu_ts_us is None:
            continue
        i += len(fr.quats)
        if fr.quats:
            t, w, x, y, z = all_s[i - 1]
            out.append((t / 1000.0, w, x, y, z))
    return out


def main(argv: list[str] | None = None) -> int:
    """  py osmo_wifi_gyro.py --file E:/Videos/clip.MP4 --csv gyro.csv
         py osmo_wifi_gyro.py --camera DCIM/DJI_001/DJI_..._D.MP4 --storage 0 --frames 0:1500
    --camera needs this machine already joined to the camera's AP."""
    import argparse
    import csv
    import time

    ap = argparse.ArgumentParser(description="Extract a DJI clip's 1 kHz orientation track")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="local MP4")
    src.add_argument("--camera", help="path on the camera, e.g. DCIM/DJI_001/DJI_..._D.MP4")
    ap.add_argument("--storage", type=int, default=0, help="camera store: 0 SD, 1 internal")
    ap.add_argument("--host", default="192.168.2.1")
    ap.add_argument("--frames", help="video frame window a:b (default: whole clip)")
    ap.add_argument("--csv", help="write t_ms,w,x,y,z rows here")
    a = ap.parse_args(argv)

    reader = FileRangeReader(a.file) if a.file else HttpRangeReader(a.camera, a.storage, a.host)
    frames = range(*map(int, a.frames.split(":"))) if a.frames else None
    t0 = time.monotonic()
    track = fetch_gyro(reader, frames, progress=lambda n, tot: print(f"  {n}/{tot} frames", flush=True))
    secs = time.monotonic() - t0
    samples = samples_1khz(track)
    print(f"{track.header.model} fw {track.header.firmware}: {len(track.frames)}/{track.total_frames} "
          f"frames, {len(samples)} samples; {reader.bytes / 1e6:.1f} MB in {reader.requests} "
          f"requests, {secs:.1f}s ({secs / max(reader.requests, 1) * 1000:.1f} ms/request)")
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_ms", "w", "x", "y", "z"])
            w.writerows(samples)
        print(f"wrote {a.csv}")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
