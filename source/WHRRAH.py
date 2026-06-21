"""
We Have RaceRender At Home
A DIY data overlay tool for AiM Solo 2 DL data logs.
Renders a green-screen overlay video to composite in DaVinci Resolve.
"""

import sys
import argparse
import os
import csv
import ctypes
import json
import math
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QListWidget, QListWidgetItem,
    QComboBox, QSlider, QSpinBox, QGroupBox, QSplitter, QScrollArea,
    QDoubleSpinBox, QCheckBox, QStatusBar, QToolBar, QFrame,
    QDialog, QDialogButtonBox, QFormLayout, QProgressDialog,
    QMessageBox, QColorDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView
)
from PyQt6.QtCore import Qt, QRect, QPoint, QSize, QTimer, QUrl, QSettings, pyqtSignal, QThread, pyqtSignal as Signal
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QFontMetrics, QPixmap, QImage,
    QAction, QCursor
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput


# ---------------------------------------------------------------------------
# XRK reading (via AiM's official MatLabXRK DLL — the format is proprietary
# and undocumented, so this wraps AiM's own access library rather than
# parsing the binary directly. DLL + dependencies live in source/xrk_dll/.)
# ---------------------------------------------------------------------------

_XRK_DLL_DIR = str(Path(__file__).parent / "xrk_dll")
_XRK_DLL_NAME = "MatLabXRK-2022-64-ReleaseU.dll"

# AiM's CSV export and this DLL disagree on units for these — confirmed
# empirically (matching a CSV export's numbers at several timestamps) rather
# than documented anywhere. GPS Altitude is labeled "ft" in AiM's CSV export
# but is actually still meters; the rest are real metric->imperial
# conversions to match what that CSV export otherwise used.
_XRK_NO_CONVERT = {"GPS Altitude"}
_XRK_UNIT_CONVERSIONS = {
    "km/h": lambda v: v / 1.609344,
    "m/s": lambda v: v * 2.236936,
    "m": lambda v: v * 3.280840,
    "cm": lambda v: v * 0.0328084,
    "C": lambda v: v * 9.0 / 5.0 + 32.0,
}


def find_xrk_dll() -> str | None:
    path = os.path.join(_XRK_DLL_DIR, _XRK_DLL_NAME)
    return path if os.path.isfile(path) else None


class XrkReader:
    """Thin ctypes wrapper around the functions in MatLabXRK.h that we need."""

    def __init__(self, dll_path: str):
        os.add_dll_directory(os.path.dirname(dll_path))
        self.lib = ctypes.CDLL(dll_path)
        self._bind()

    def _bind(self):
        lib = self.lib
        c_int, c_char_p, c_double = ctypes.c_int, ctypes.c_char_p, ctypes.c_double
        dptr = ctypes.POINTER(c_double)

        lib.open_file.argtypes = [c_char_p]; lib.open_file.restype = c_int
        lib.get_last_open_error.restype = c_char_p
        lib.close_file_i.argtypes = [c_int]; lib.close_file_i.restype = c_int

        lib.get_vehicle_name.argtypes = [c_int]; lib.get_vehicle_name.restype = c_char_p
        lib.get_track_name.argtypes = [c_int]; lib.get_track_name.restype = c_char_p
        lib.get_racer_name.argtypes = [c_int]; lib.get_racer_name.restype = c_char_p

        lib.get_laps_count.argtypes = [c_int]; lib.get_laps_count.restype = c_int
        lib.get_lap_info.argtypes = [c_int, c_int, dptr, dptr]; lib.get_lap_info.restype = c_int
        lib.get_session_duration.argtypes = [c_int, dptr]; lib.get_session_duration.restype = c_int

        lib.get_channels_count.argtypes = [c_int]; lib.get_channels_count.restype = c_int
        lib.get_channel_name.argtypes = [c_int, c_int]; lib.get_channel_name.restype = c_char_p
        lib.get_channel_units.argtypes = [c_int, c_int]; lib.get_channel_units.restype = c_char_p
        lib.get_channel_samples_count.argtypes = [c_int, c_int]; lib.get_channel_samples_count.restype = c_int
        lib.get_channel_samples.argtypes = [c_int, c_int, dptr, dptr, c_int]
        lib.get_channel_samples.restype = c_int

        lib.get_GPS_channels_count.argtypes = [c_int]; lib.get_GPS_channels_count.restype = c_int
        lib.get_GPS_channel_name.argtypes = [c_int, c_int]; lib.get_GPS_channel_name.restype = c_char_p
        lib.get_GPS_channel_units.argtypes = [c_int, c_int]; lib.get_GPS_channel_units.restype = c_char_p
        lib.get_GPS_channel_samples_count.argtypes = [c_int, c_int]; lib.get_GPS_channel_samples_count.restype = c_int
        lib.get_GPS_channel_samples.argtypes = [c_int, c_int, dptr, dptr, c_int]
        lib.get_GPS_channel_samples.restype = c_int

    def open(self, path: str) -> int:
        idxf = self.lib.open_file(path.encode("mbcs"))
        if idxf <= 0:
            err = self.lib.get_last_open_error()
            raise IOError(f"Could not open {path}: {err.decode() if err else 'unknown error'}")
        return idxf

    def close(self, idxf: int):
        self.lib.close_file_i(idxf)

    def channel_list(self, idxf: int, gps: bool) -> list[str]:
        count_fn = self.lib.get_GPS_channels_count if gps else self.lib.get_channels_count
        name_fn = self.lib.get_GPS_channel_name if gps else self.lib.get_channel_name
        return [name_fn(idxf, i).decode() for i in range(count_fn(idxf))]

    def channel_units(self, idxf: int, idxc: int, gps: bool) -> str:
        fn = self.lib.get_GPS_channel_units if gps else self.lib.get_channel_units
        return fn(idxf, idxc).decode()

    def channel_samples(self, idxf: int, idxc: int, gps: bool) -> tuple[list[float], list[float]]:
        # Quirk: get_GPS_channel_samples returns times in milliseconds while
        # get_channel_samples (regular channels) returns seconds — confirmed
        # empirically against a known session length.
        count_fn = self.lib.get_GPS_channel_samples_count if gps else self.lib.get_channel_samples_count
        samples_fn = self.lib.get_GPS_channel_samples if gps else self.lib.get_channel_samples
        cnt = count_fn(idxf, idxc)
        if cnt <= 0:
            return [], []
        times = (ctypes.c_double * cnt)()
        values = (ctypes.c_double * cnt)()
        ret = samples_fn(idxf, idxc, times, values, cnt)
        if ret <= 0:
            return [], []
        out_times = [t / 1000.0 for t in times] if gps else list(times)
        return out_times, list(values)

    def laps(self, idxf: int) -> list[tuple[float, float]]:
        """[(start, duration), ...] for every lap, in order."""
        out = []
        for i in range(self.lib.get_laps_count(idxf)):
            start, dur = ctypes.c_double(), ctypes.c_double()
            self.lib.get_lap_info(idxf, i, ctypes.byref(start), ctypes.byref(dur))
            out.append((start.value, dur.value))
        return out


def _resample_to_grid(native_t: list[float], native_v: list[float], grid: list[float]) -> list[float]:
    """Linear-interpolates (native_t, native_v) onto `grid`, clamping at the
    ends — same semantics as DataLog.value_at, precomputed for the whole grid.
    Needed because, unlike the CSV (every channel sampled at one shared rate),
    XRK channels each have their own native sample rate."""
    n = len(native_t)
    if n == 1:
        return [native_v[0]] * len(grid)

    out = []
    lo = 0
    for t in grid:
        if t <= native_t[0]:
            out.append(native_v[0])
            continue
        if t >= native_t[-1]:
            out.append(native_v[-1])
            continue
        while lo + 1 < n - 1 and native_t[lo + 1] <= t:
            lo += 1
        hi = lo + 1
        span = native_t[hi] - native_t[lo]
        alpha = (t - native_t[lo]) / span if span > 0 else 0.0
        out.append(native_v[lo] + alpha * (native_v[hi] - native_v[lo]))
    return out


# ---------------------------------------------------------------------------
# Video/log matching (the "Data Wizard") — pairs up AiM .xrk sessions with
# video files by cross-correlating each video's embedded gyro orientation
# (DJI djmd stream) against the session's YawRate/RollRate/PitchRate.
# Confirmed empirically: a real match gives a sharp correlation peak
# (~0.9-0.98); EXIF creation_time is only used as a coarse pre-filter since
# camera clocks can be wrong by exactly an hour (DST misconfig) or more.
# ---------------------------------------------------------------------------

def video_duration_sec(path: str) -> float | None:
    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        return frame_count / fps if fps > 0 and frame_count > 0 else None
    finally:
        cap.release()


def read_mp4_creation_time(path: str) -> "datetime.datetime | None":
    """Reads the moov/mvhd box's creation_time directly (seconds since
    1904-01-01, per the MP4/QuickTime spec) — no ffmpeg dependency, and
    cheap even on huge files since we only read box headers and seek past
    everything else (moov is sometimes at the very end of camera-recorded
    files, but we never read the multi-GB mdat payload to get there)."""
    import datetime
    EPOCH_1904 = datetime.datetime(1904, 1, 1)
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            f.seek(0)

            def find_box(start: int, end: int, box_type: bytes) -> tuple[int, int] | None:
                pos = start
                while pos < end - 8:
                    f.seek(pos)
                    header = f.read(8)
                    if len(header) < 8:
                        return None
                    size = int.from_bytes(header[0:4], "big")
                    btype = header[4:8]
                    box_start = pos
                    if size == 1:
                        size = int.from_bytes(f.read(8), "big")
                        header_len = 16
                    elif size == 0:
                        size = end - pos
                        header_len = 8
                    else:
                        header_len = 8
                    if btype == box_type:
                        return box_start + header_len, box_start + size
                    pos += size if size > 0 else 8
                return None

            moov = find_box(0, file_size, b"moov")
            if not moov:
                return None
            mvhd = find_box(moov[0], moov[1], b"mvhd")
            if not mvhd:
                return None
            f.seek(mvhd[0])
            version = f.read(1)[0]
            f.seek(mvhd[0] + 4)  # skip version(1)+flags(3)
            if version == 1:
                creation_time = int.from_bytes(f.read(8), "big")
            else:
                creation_time = int.from_bytes(f.read(4), "big")
            return EPOCH_1904 + datetime.timedelta(seconds=creation_time)
    except (OSError, IndexError):
        return None


def xrk_session_info(path: str) -> dict | None:
    """Lightweight session metadata (no channel data) — start time, duration,
    lap count — for use during matching, before committing to a full load."""
    dll_path = find_xrk_dll()
    if not dll_path:
        return None
    xrk = XrkReader(dll_path)
    try:
        idxf = xrk.open(path)
    except IOError:
        return None
    try:
        class _TM(ctypes.Structure):
            _fields_ = [(n, ctypes.c_int) for n in
                        ("tm_sec", "tm_min", "tm_hour", "tm_mday", "tm_mon", "tm_year",
                         "tm_wday", "tm_yday", "tm_isdst")]
        xrk.lib.get_date_and_time.argtypes = [ctypes.c_int]
        xrk.lib.get_date_and_time.restype = ctypes.POINTER(_TM)
        tm = xrk.lib.get_date_and_time(idxf)[0]
        import datetime
        start = datetime.datetime(tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                                   tm.tm_hour, tm.tm_min, tm.tm_sec)
        laps = xrk.laps(idxf)
        duration = sum(d for _, d in laps) if laps else 0.0
        # Matches MainWindow.load_log_file's own convention exactly: that
        # uses lap_number_at(t_end), which counts the trailing beacon
        # marker too since it's built to equal t_end exactly — so the lap
        # total and best-lap search both include every lap the DLL reports,
        # not len(laps) - 1.
        best_lap_num, best_lap_time = None, None
        if laps:
            best_lap_num, best_lap_time = min(enumerate(d for _, d in laps), key=lambda x: x[1])
        return {
            "path": path, "start": start, "duration": duration, "laps": len(laps),
            "completed_laps": len(laps), "best_lap_num": best_lap_num, "best_lap_time": best_lap_time,
        }
    finally:
        xrk.close(idxf)


def xrk_rotation_signal(path: str, grid_rate: float = 20.0) -> tuple[list[float], list[float]] | None:
    """(timestamps, |angular velocity|) from YawRate/RollRate/PitchRate,
    resampled onto a shared grid — cheap, since it skips the other ~43
    channels a full DataLog.load_xrk() would resample."""
    dll_path = find_xrk_dll()
    if not dll_path:
        return None
    xrk = XrkReader(dll_path)
    try:
        idxf = xrk.open(path)
    except IOError:
        return None
    try:
        names = xrk.channel_list(idxf, gps=False)
        wanted = {"YawRate", "RollRate", "PitchRate"}
        if not wanted.issubset(set(names)):
            return None
        laps = xrk.laps(idxf)
        duration = sum(d for _, d in laps) if laps else 0.0
        if duration <= 0:
            return None
        n_samples = max(1, round(duration * grid_rate)) + 1
        grid = [i / grid_rate for i in range(n_samples)]

        channels = {}
        for name in wanted:
            idxc = names.index(name)
            native_t, native_v = xrk.channel_samples(idxf, idxc, gps=False)
            if not native_t:
                return None
            channels[name] = _resample_to_grid(native_t, native_v, grid)

        magnitude = [
            (channels["YawRate"][i] ** 2 + channels["RollRate"][i] ** 2 + channels["PitchRate"][i] ** 2) ** 0.5
            for i in range(len(grid))
        ]
        return grid, magnitude
    finally:
        xrk.close(idxf)


def video_gyro_signal(path: str) -> tuple[list[float], list[float]] | None:
    """(timestamps, |angular velocity|) derived from the DJI djmd stream's
    per-frame orientation quaternions. Returns None if ffmpeg isn't
    available, the video has no djmd stream, or it's not a DJI file with
    this metadata schema."""
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return None

    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS)
    finally:
        cap.release()
    if not fps or fps <= 0:
        return None

    with tempfile.TemporaryDirectory(prefix="whrrah_djmd_") as tmp_dir:
        djmd_path = os.path.join(tmp_dir, "djmd.bin")
        result = subprocess.run(
            [ffmpeg_path, "-y", "-i", path, "-map", "0:2", "-c", "copy", "-f", "data", djmd_path],
            capture_output=True
        )
        if result.returncode != 0 or not os.path.isfile(djmd_path):
            return None
        with open(djmd_path, "rb") as f:
            data = f.read()

    frame_quats = _parse_djmd_frame_quaternions(data)
    if len(frame_quats) < 2:
        return None

    dt = 1.0 / fps
    ang_vel = []
    for k in range(1, len(frame_quats)):
        w1, x1, y1, z1 = frame_quats[k - 1]
        w2, x2, y2, z2 = frame_quats[k]
        rw = w2 * w1 - x2 * (-x1) - y2 * (-y1) - z2 * (-z1)
        rw = max(-1.0, min(1.0, rw))
        angle_deg = math.degrees(2 * math.acos(abs(rw)))
        ang_vel.append(angle_deg / dt)
    timestamps = [(k + 1) / fps for k in range(len(ang_vel))]
    return timestamps, ang_vel


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, i


def _decode_protobuf_fields(buf: bytes, max_items: int = 10**9) -> tuple[list[tuple[int, str, object]], int]:
    """Generic, schema-less protobuf wire-format walker — just enough to
    locate the DJI djmd quaternion bursts without needing their .proto
    definition (which isn't published for this camera's schema version)."""
    i, n, count, out = 0, len(buf), 0, []
    while i < n and count < max_items:
        try:
            tag, i = _read_varint(buf, i)
        except IndexError:
            break
        field_num, wire_type = tag >> 3, tag & 0x7
        if wire_type == 0:
            val, i = _read_varint(buf, i)
            out.append((field_num, "varint", val))
        elif wire_type == 1:
            if i + 8 > n:
                break
            out.append((field_num, "double", struct.unpack("<d", buf[i:i + 8])[0]))
            i += 8
        elif wire_type == 2:
            length, i = _read_varint(buf, i)
            if i + length > n:
                break
            out.append((field_num, "bytes", buf[i:i + length]))
            i += length
        elif wire_type == 5:
            if i + 4 > n:
                break
            out.append((field_num, "float", struct.unpack("<f", buf[i:i + 4])[0]))
            i += 4
        else:
            break
        count += 1
    return out, i


def _decode_4_floats(buf: bytes) -> list[float]:
    vals = []
    i = 0
    while i < len(buf):
        i += 1  # skip the per-float protobuf tag byte
        vals.append(struct.unpack("<f", buf[i:i + 4])[0])
        i += 4
    return vals


def _parse_djmd_frame_quaternions(data: bytes) -> list[tuple[float, float, float, float]]:
    """One quaternion (the last sample of that frame's IMU burst) per video
    frame, in order — confirmed empirically to land one record per frame."""
    i, n = 0, len(data)
    frame_quats = []
    while i < n:
        try:
            tag, i = _read_varint(data, i)
        except IndexError:
            break
        field_num, wire_type = tag >> 3, tag & 0x7
        if wire_type != 2:
            break
        length, i = _read_varint(data, i)
        chunk = data[i:i + length]
        i += length
        if field_num != 3:
            continue
        sub = _decode_protobuf_fields(chunk, max_items=20)[0]
        imu_list = [f[2] for f in sub if f[0] == 3 and f[1] == "bytes"]
        if not imu_list:
            continue
        imu_sub = _decode_protobuf_fields(imu_list[0], max_items=20)[0]
        inner_list = [f[2] for f in imu_sub if f[1] == "bytes"]
        if not inner_list:
            continue
        inner = _decode_protobuf_fields(inner_list[0], max_items=2000)[0]
        quads = [_decode_4_floats(f[2]) for f in inner if f[1] == "bytes" and len(f[2]) == 20]
        if quads:
            frame_quats.append(tuple(quads[-1]))
    return frame_quats


def correlate_signals(
    sig_a: tuple[list[float], list[float]], sig_b: tuple[list[float], list[float]],
    search_range: tuple[float, float], grid_rate: float = 20.0, step: float = 0.25,
) -> tuple[float, float]:
    """Brute-force normalized cross-correlation of two (t, value) signals.
    Returns (best_offset, best_correlation), where best_offset is how far
    sig_b's own timeline must be shifted forward to align with sig_a's."""
    a_t, a_v = np.array(sig_a[0]), np.array(sig_a[1])
    b_t, b_v = np.array(sig_b[0]), np.array(sig_b[1])
    if len(a_t) < 2 or len(b_t) < 2:
        return 0.0, 0.0

    margin = 2.0
    b_dur = b_t[-1] - b_t[0]
    local_grid = np.arange(margin, b_dur - margin, 1.0 / grid_rate)
    if len(local_grid) < grid_rate:  # need at least ~1s of signal
        return 0.0, 0.0
    bg = np.interp(local_grid, b_t, b_v)
    bg = (bg - bg.mean()) / (bg.std() + 1e-9)

    best_corr, best_offset = -1e9, 0.0
    offset = search_range[0]
    while offset <= search_range[1]:
        grid = local_grid + offset
        if grid[0] < a_t[0] or grid[-1] > a_t[-1]:
            offset += step
            continue
        ag = np.interp(grid, a_t, a_v)
        ag = (ag - ag.mean()) / (ag.std() + 1e-9)
        c = float(np.dot(ag, bg) / len(bg))
        if c > best_corr:
            best_corr, best_offset = c, offset
        offset += step
    return best_offset, max(best_corr, 0.0)


def match_video_to_session(video_path: str, video_duration: float, session_info: dict,
                            session_rotation, video_gyro) -> tuple[float, float, str]:
    """Returns (video_start_offset_into_session, confidence_0_to_1, method).
    session_rotation/video_gyro are precomputed signals (or None), passed in
    so callers can compute each session's/video's signal once and reuse it
    across every pair, instead of recomputing it for every combination."""
    session_duration = session_info["duration"]
    slack = 2.0
    if video_duration > session_duration + slack:
        return 0.0, 0.0, "duration-mismatch"

    if session_rotation and video_gyro:
        max_offset = max(0.0, session_duration - video_duration)
        offset, corr = correlate_signals(session_rotation, video_gyro, (-5.0, max_offset + 5.0))
        return offset, corr, "gyro"

    # Fall back to EXIF creation_time, capped well below gyro-confirmed
    # confidence since camera clocks have been observed off by exactly an
    # hour (DST misconfig) — this is a coarse hint, not a real measurement.
    creation_time = read_mp4_creation_time(video_path)
    if creation_time is None:
        return 0.0, 0.10, "duration-only"
    video_start_naive = creation_time  # camera local time, tz-unaware
    offset = (video_start_naive - session_info["start"]).total_seconds()
    if -slack <= offset <= session_duration - video_duration + slack:
        return max(0.0, offset), 0.55, "exif"
    return 0.0, 0.15, "exif-no-overlap"


def find_video_log_matches(aim_folder: str, video_folder: str,
                            progress_cb=None) -> list[dict]:
    """Pairs every .xrk in aim_folder with its best-matching video in
    video_folder. Returns a list of dicts: {xrk_path, video_path or None,
    confidence (0-1), offset_sec, method}, one per .xrk found."""
    xrk_paths = sorted(Path(aim_folder).glob("*.xrk"))
    video_exts = (".mp4", ".mov", ".avi", ".mkv")
    video_paths = sorted(p for p in Path(video_folder).iterdir()
                          if p.suffix.lower() in video_exts)

    sessions = []
    for p in xrk_paths:
        info = xrk_session_info(str(p))
        if info:
            sessions.append(info)

    video_durations = {}
    for p in video_paths:
        d = video_duration_sec(str(p))
        if d:
            video_durations[str(p)] = d

    # Each session's rotation signal and each video's gyro signal is the
    # expensive part (parsing the whole djmd stream / resampling channels) —
    # compute each exactly once and reuse across every pair it's part of,
    # rather than once per (session, video) combination.
    rotation_cache = {s["path"]: xrk_rotation_signal(s["path"]) for s in sessions}
    gyro_cache = {v: video_gyro_signal(v) for v in video_durations}

    total_pairs = max(1, len(sessions) * len(video_durations))
    done = 0
    candidates = []  # (confidence, session_info, video_path, offset, method)
    for session in sessions:
        for video_path, video_dur in video_durations.items():
            offset, conf, method = match_video_to_session(
                video_path, video_dur, session,
                rotation_cache.get(session["path"]), gyro_cache.get(video_path)
            )
            candidates.append((conf, session, video_path, offset, method))
            done += 1
            if progress_cb:
                progress_cb(int(done / total_pairs * 100))

    # Greedy global assignment: strongest matches claimed first, each
    # session and each video used at most once.
    candidates.sort(key=lambda c: -c[0])
    used_sessions, used_videos = set(), set()
    assignment = {}
    for conf, session, video_path, offset, method in candidates:
        key = session["path"]
        if key in used_sessions or video_path in used_videos:
            continue
        if conf <= 0:
            continue
        used_sessions.add(key)
        used_videos.add(video_path)
        assignment[key] = {"xrk_path": key, "video_path": video_path,
                            "confidence": conf, "offset_sec": offset, "method": method}

    results = [
        assignment.get(s["path"], {"xrk_path": s["path"], "video_path": None,
                                    "confidence": 0.0, "offset_sec": 0.0, "method": "none"})
        for s in sessions
    ]
    for result, session in zip(results, sessions):
        result["completed_laps"] = session["completed_laps"]
        result["best_lap_num"] = session["best_lap_num"]
        result["best_lap_time"] = session["best_lap_time"]
    return results


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

def _smooth(values: list[float], window: int = 5) -> list[float]:
    """Simple centered moving average, used to take GPS jitter out of the track outline."""
    n = len(values)
    if n < window:
        return list(values)
    arr = np.array(values)
    kernel = np.ones(window) / window
    smoothed = np.convolve(arr, kernel, mode="same")
    # convolve's edges are biased toward zero-padding — keep the raw values there
    half = window // 2
    smoothed[:half] = arr[:half]
    smoothed[-half:] = arr[-half:]
    return smoothed.tolist()


class DataLog:
    """Parses an AiM RS2-exported CSV and holds all channels."""

    def __init__(self):
        self.channels: dict[str, list[float]] = {}
        self.timestamps: list[float] = []
        self.sample_rate: float = 25.0  # Hz, detected from data
        self.duration: float = 0.0
        self.filepath: str = ""
        self.beacon_markers: list[float] = []  # absolute time_sec of each lap/split crossing
        self.track_lat: list[float] = []  # reference track outline, from lap 1
        self.track_lon: list[float] = []

    def load(self, filepath: str) -> list[str]:
        """Load CSV, return list of detected channel names."""
        self.filepath = filepath
        self.channels = {}
        self.timestamps = []
        self.beacon_markers = []
        self.track_lat = []
        self.track_lon = []

        with open(filepath, newline="", encoding="utf-8-sig") as f:
            # AiM RS2 CSVs have several metadata rows ("Format", "Session", ...)
            # before the real header row, then a units row, then a blank line.
            lines = f.readlines()

        # The metadata block also has a "Time" row (time-of-day, e.g. "11:21 AM"),
        # so the real header is the "Time" row immediately followed by a units
        # row starting with "s" (seconds).
        header_row = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            fields = next(csv.reader([stripped]), [])
            if not fields or fields[0] != "Time":
                continue
            next_fields = next(csv.reader([lines[i + 1]]), []) if i + 1 < len(lines) else []
            if next_fields and next_fields[0] == "s":
                header_row = i
                break

        if header_row is None:
            raise ValueError("Could not find header row (no 'Time' column found).")

        # "Beacon Markers" is a metadata row giving the absolute time_sec of
        # each beacon crossing (start/finish + splits) — used to compute lap time.
        for line in lines[:header_row]:
            fields = next(csv.reader([line.strip()]), [])
            if fields and fields[0] == "Beacon Markers":
                for f in fields[1:]:
                    try:
                        self.beacon_markers.append(float(f))
                    except ValueError:
                        pass
                break

        fieldnames = next(csv.reader([lines[header_row]]))

        # Skip the units row (e.g. "s","mph",...) and any blank line after it
        data_start = header_row + 2
        while data_start < len(lines) and lines[data_start].strip() == "":
            data_start += 1

        reader = csv.DictReader(lines[data_start:], fieldnames=fieldnames)
        rows = list(reader)

        if not rows:
            raise ValueError("No data rows found in CSV.")

        # Try common timestamp column names
        time_keys = ["Time", "time", "Timestamp", "timestamp", "T"]
        time_key = next((k for k in time_keys if k in rows[0]), None)

        for row in rows:
            if time_key:
                try:
                    self.timestamps.append(float(row[time_key]))
                except (ValueError, KeyError):
                    pass

            for col, val in row.items():
                try:
                    self.channels.setdefault(col, []).append(float(val))
                except (ValueError, TypeError):
                    # Non-numeric channel — skip
                    pass

        if self.timestamps:
            self.duration = self.timestamps[-1] - self.timestamps[0]
            if len(self.timestamps) > 1:
                avg_dt = self.duration / (len(self.timestamps) - 1)
                self.sample_rate = 1.0 / avg_dt if avg_dt > 0 else 25.0

        self._build_track_reference()

        return list(self.channels.keys())

    def load_xrk(self, filepath: str, sample_rate_hz: float = 20.0) -> list[str]:
        """Load an AiM .xrk via the bundled MatLabXRK DLL, return list of
        detected channel names. Resamples every channel (each has its own
        native rate in the XRK) onto one shared `sample_rate_hz` grid, so
        the result has the exact same shape as load()'s CSV parse."""
        dll_path = find_xrk_dll()
        if not dll_path:
            raise FileNotFoundError(
                f"XRK support requires {_XRK_DLL_NAME} in {_XRK_DLL_DIR}, which wasn't found.")

        self.filepath = filepath
        self.channels = {}
        self.timestamps = []
        self.beacon_markers = []
        self.track_lat = []
        self.track_lon = []

        xrk = XrkReader(dll_path)
        idxf = xrk.open(filepath)
        try:
            laps = xrk.laps(idxf)
            if laps:
                last_start, last_dur = laps[-1]
                duration = last_start + last_dur
            else:
                dur_ptr = ctypes.c_double()
                xrk.lib.get_session_duration(idxf, ctypes.byref(dur_ptr))
                duration = dur_ptr.value

            n_samples = max(1, round(duration * sample_rate_hz)) + 1
            self.timestamps = [i / sample_rate_hz for i in range(n_samples)]
            self.duration = duration
            self.sample_rate = sample_rate_hz

            for gps in (False, True):
                for idxc, name in enumerate(xrk.channel_list(idxf, gps)):
                    native_t, native_v = xrk.channel_samples(idxf, idxc, gps)
                    if not native_t:
                        continue
                    if name not in _XRK_NO_CONVERT:
                        conv = _XRK_UNIT_CONVERSIONS.get(xrk.channel_units(idxf, idxc, gps))
                        if conv:
                            native_v = [conv(v) for v in native_v]
                    self.channels[name] = _resample_to_grid(native_t, native_v, self.timestamps)

            # Matches the CSV's "Beacon Markers" convention: start time of
            # every lap after the first, then the session end time.
            self.beacon_markers = [start for start, _dur in laps[1:]]
            if laps:
                self.beacon_markers.append(duration)
        finally:
            xrk.close(idxf)

        self._build_track_reference()
        return list(self.channels.keys())

    def _build_track_reference(self):
        """Extract the GPS path for lap 1 to use as the track map's reference line."""
        lats = self.channels.get("GPS Latitude", [])
        lons = self.channels.get("GPS Longitude", [])
        if not lats or not lons or not self.timestamps:
            return

        start, end = self._lap_bounds(1)
        lat_pts = [lat for t, lat in zip(self.timestamps, lats) if start <= t < end]
        lon_pts = [lon for t, lon in zip(self.timestamps, lons) if start <= t < end]

        if not lat_pts:
            # No clean lap 1 (e.g. log doesn't span a full lap) — fall back to all points
            lat_pts, lon_pts = lats, lons

        self.track_lat = _smooth(lat_pts)
        self.track_lon = _smooth(lon_pts)

    def _lap_bounds(self, lap_number: int) -> tuple[float, float]:
        """Start/end time_sec for a given lap number, based on beacon crossings."""
        t0 = self.timestamps[0] if self.timestamps else 0.0
        t_end = self.timestamps[-1] if self.timestamps else 0.0
        start = self.beacon_markers[lap_number - 1] if 0 < lap_number <= len(self.beacon_markers) else t0
        end = self.beacon_markers[lap_number] if lap_number < len(self.beacon_markers) else t_end
        return start, end

    def value_at(self, channel: str, time_sec: float) -> float:
        """Interpolate channel value at a given timestamp."""
        if channel not in self.channels or not self.timestamps:
            return 0.0
        vals = self.channels[channel]
        t = self.timestamps

        # Clamp
        if time_sec <= t[0]:
            return vals[0]
        if time_sec >= t[-1]:
            return vals[-1]

        # Binary search
        lo, hi = 0, len(t) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if t[mid] <= time_sec:
                lo = mid
            else:
                hi = mid

        alpha = (time_sec - t[lo]) / (t[hi] - t[lo]) if t[hi] != t[lo] else 0
        return vals[lo] + alpha * (vals[hi] - vals[lo])

    def lap_time_at(self, time_sec: float) -> float:
        """Seconds elapsed since the most recent beacon crossing at or before time_sec."""
        if not self.beacon_markers:
            return time_sec - (self.timestamps[0] if self.timestamps else 0.0)
        lap_start = self.timestamps[0] if self.timestamps else 0.0
        for marker in self.beacon_markers:
            if marker <= time_sec:
                lap_start = marker
            else:
                break
        return time_sec - lap_start

    def lap_number_at(self, time_sec: float) -> int:
        """Lap/segment number for time_sec, based on beacon crossings. Lap 0 is the outlap."""
        lap = 0
        for marker in self.beacon_markers:
            if marker <= time_sec:
                lap += 1
            else:
                break
        return lap

    def lap_duration(self, lap_number: int) -> float | None:
        """Duration of a completed lap, or None if it hasn't finished (or doesn't exist)."""
        if lap_number == 0:
            if not self.beacon_markers or not self.timestamps:
                return None
            return self.beacon_markers[0] - self.timestamps[0]
        if 0 < lap_number <= len(self.beacon_markers) - 1:
            return self.beacon_markers[lap_number] - self.beacon_markers[lap_number - 1]
        return None

    def last_lap_time_at(self, time_sec: float) -> float | None:
        """Duration of the most recently completed lap as of time_sec, or None if no lap has finished yet."""
        current_lap = self.lap_number_at(time_sec)
        if current_lap == 0:
            return None
        return self.lap_duration(current_lap - 1)

    def best_lap_time_at(self, time_sec: float) -> float | None:
        """Fastest completed lap so far as of time_sec, or None if no lap has finished yet."""
        current_lap = self.lap_number_at(time_sec)
        durations = [d for n in range(current_lap) if (d := self.lap_duration(n)) is not None]
        return min(durations) if durations else None

    def channel_range(self, channel: str) -> tuple[float, float]:
        if channel not in self.channels:
            return (0.0, 1.0)
        vals = self.channels[channel]
        return (min(vals), max(vals))


# ---------------------------------------------------------------------------
# Widget definitions
# ---------------------------------------------------------------------------

WIDGET_TYPES = ["Lap Timer", "Numeric Display", "Bar Graph", "Track Map"]

# Unscaled (scale=1.0) size per widget type — actual w/h/font_size are this times .scale_x/.scale_y.
BASE_SIZES = {
    "Lap Timer": (220, 70),
    "Numeric Display": (200, 60),
    "Bar Graph": (200, 60),
    "Track Map": (220, 220),
}
BASE_FONT_SIZE = 24

# Export text used to render with cv2's Hershey Simplex font, which looks
# noticeably different from (and ~2x wider than, at matching heights) the
# Qt "Monospace" font the live preview uses — Consolas is a close visual
# match for what Qt's generic Monospace family actually resolves to on
# Windows, and using the same font for both means the box-size math (which
# measures this font) actually matches what gets exported.
_MONOSPACE_FONT_CANDIDATES = {
    False: [r"C:\Windows\Fonts\consola.ttf", r"C:\Windows\Fonts\cour.ttf"],
    True: [r"C:\Windows\Fonts\consolab.ttf", r"C:\Windows\Fonts\courbd.ttf"],
}
_pil_font_cache: dict[tuple[int, bool], "ImageFont.FreeTypeFont"] = {}


def _get_pil_font(px_size: int, bold: bool = False):
    px_size = max(1, int(px_size))
    key = (px_size, bold)
    font = _pil_font_cache.get(key)
    if font is not None:
        return font
    for path in _MONOSPACE_FONT_CANDIDATES[bold]:
        if os.path.isfile(path):
            font = ImageFont.truetype(path, px_size)
            break
    if font is None:
        try:
            font = ImageFont.load_default(px_size)  # Pillow >= 10.1
        except TypeError:
            font = ImageFont.load_default()
    _pil_font_cache[key] = font
    return font


def _pil_text_size(text: str, px_size: int, bold: bool = False) -> tuple[int, int]:
    """(width, height) text would occupy at this pixel size — used both to
    size widget boxes and (via the same font) to actually draw the export,
    so the two stay in agreement."""
    font = _get_pil_font(px_size, bold)
    left, top, right, bottom = font.getbbox(text)
    return right - left, bottom - top


def _pil_draw_text(draw: "ImageDraw.ImageDraw", xy: tuple[int, int], text: str, font, fill):
    """draw.text(), but compensating for the font's own left/top bearing so
    the glyphs' visible top-left actually lands at `xy` — matching what
    _pil_text_size measured, instead of drifting by each font's bearing."""
    left, top, _, _ = font.getbbox(text)
    draw.text((xy[0] - left, xy[1] - top), text, font=font, fill=fill)


def _render_text_region(frame: np.ndarray, x: int, y: int, w: int, h: int, draw_fn):
    """Lets draw_fn (called with a PIL ImageDraw and the region's own (w, h))
    draw text into just this region of `frame` with Pillow, then blits the
    result back — far cheaper than converting the whole frame for every
    widget's text."""
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(frame.shape[1], x + w), min(frame.shape[0], y + h)
    if x1 <= x0 or y1 <= y0:
        return
    sub = frame[y0:y1, x0:x1]
    img = Image.fromarray(cv2.cvtColor(sub, cv2.COLOR_BGR2RGB))
    draw_fn(ImageDraw.Draw(img), x0 - x, y0 - y, x1 - x0, y1 - y0)
    frame[y0:y1, x0:x1] = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


class OverlayWidget:
    """Represents a single draggable widget on the overlay canvas.

    Size is driven entirely by `scale` (same factor on x and y) so resizing
    grows the box and its text/graphics together, instead of just the border.
    """

    def __init__(self, widget_type: str, x=100, y=100, scale=1.0):
        self.widget_type = widget_type
        self.x = x
        self.y = y
        self.scale_x = scale
        self.scale_y = scale
        self.channel: str = ""
        self.label: str = widget_type
        self.min_val: float = 0.0
        self.max_val: float = 100.0
        self.color: tuple = (0, 255, 0)  # BGR for OpenCV
        self.show_value: bool = True  # Bar Graph: overlay the numeric value on the bar
        self.text_color: tuple = (255, 255, 255)  # BGR, Lap Timer text — default white
        self.show_time: bool = True  # Lap Timer: which lines to render
        self.show_last: bool = True
        self.show_best: bool = True
        self.show_lap: bool = True
        self.selected: bool = False
        # Per-widget data-time offset, to compensate channels (e.g. CAN bus
        # data) that lag behind GPS-derived ones without shifting the whole
        # timeline/video sync. Positive = sample further ahead in the log.
        self.time_offset: float = 0.0
        self.decimals: int = 2  # Numeric Display / Bar Graph value precision
        # Numeric Display: independent absolute font sizes for the value
        # ("XXX") and the label beneath it, rather than tying the label to
        # whatever the corner-drag scale computes for the value.
        self.label_font_size: int = 16

    @property
    def base_size(self) -> tuple[int, int]:
        return BASE_SIZES.get(self.widget_type, (200, 60))

    def lap_timer_lines(self) -> list[str]:
        """Which lines ("Time"/"Last"/"Best"/"Lap") this Lap Timer should show, in order."""
        lines = []
        if self.show_time:
            lines.append("Time")
        if self.show_last:
            lines.append("Last")
        if self.show_best:
            lines.append("Best")
        if self.show_lap:
            lines.append("Lap")
        return lines

    def _numeric_ref_text(self) -> str:
        # "000.00" (or however many decimals) covers triple-digit readings
        # (e.g. mph) — a fixed budget so the box doesn't resize as the live
        # value changes. The label is sized/drawn separately (see w/h below).
        return "000" + (f".{'0' * self.decimals}" if self.decimals > 0 else "")

    @property
    def w(self) -> int:
        if self.widget_type == "Lap Timer":
            # Longest line actually shown determines the width — "Lap: 0" is much
            # narrower than the clock lines, so a Lap-only timer doesn't end up
            # arbitrarily wide.
            lines = self.lap_timer_lines()
            ref_text = "Time: 00:00:000" if any(l in ("Time", "Last", "Best") for l in lines) else "Lap: 0"
            text_w, _ = _pil_text_size(ref_text, _lap_timer_text_px_size(self.font_size), bold=True)
            padding = 16 + int(self.font_size * 0.3)
            return text_w + padding
        if self.widget_type == "Numeric Display":
            value_w, _ = _pil_text_size(self._numeric_ref_text(), self.font_size, bold=True)
            label_w, _ = _pil_text_size(self.label, self.label_font_size)
            padding = 16 + int(self.font_size * 0.3)
            return max(value_w, label_w) + padding
        return max(1, int(self.base_size[0] * self.scale_x))

    @property
    def h(self) -> int:
        if self.widget_type == "Lap Timer":
            # Box height tracks actual text height (which grows sub-linearly via
            # _lap_timer_line_gap), not the scale factor directly, so the box
            # doesn't end up with a lot of empty space below the lines — and it
            # scales with however many of Time/Last/Best/Lap are enabled.
            n_lines = max(1, len(self.lap_timer_lines()))
            padding = 8 + int(self.font_size * 0.35)
            return _lap_timer_line_gap(self.font_size) * n_lines + padding
        if self.widget_type == "Numeric Display":
            # Stacked layout: value on top, label below, each sized by its
            # own independent font setting.
            _, value_h = _pil_text_size(self._numeric_ref_text(), self.font_size, bold=True)
            _, label_h = _pil_text_size(self.label or "A", self.label_font_size)
            padding = 16 + int(self.font_size * 0.2)
            return value_h + 4 + label_h + padding
        return max(1, int(self.base_size[1] * self.scale_y))

    @property
    def font_size(self) -> int:
        return max(6, int(BASE_FONT_SIZE * self.scale_y))

    def rect(self) -> QRect:
        return QRect(self.x, self.y, self.w, self.h)

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "OverlayWidget":
        w = cls(d["widget_type"])
        w.__dict__.update(d)
        return w

    def render_to_frame(self, frame: np.ndarray, data_log: DataLog, time_sec: float):
        """Draw this widget onto a CV2 frame (BGR + alpha channel)."""
        time_sec = time_sec + self.time_offset
        x, y, w, h = self.x, self.y, self.w, self.h
        color_bgr = self.color  # already BGR

        val = data_log.value_at(self.channel, time_sec) if self.channel else 0.0

        if self.widget_type == "Numeric Display":
            _draw_numeric(frame, x, y, w, h, self.label, val, self.font_size, self.label_font_size,
                          self.decimals, self.text_color)

        elif self.widget_type == "Bar Graph":
            lo, hi = self.min_val, self.max_val
            pct = np.clip((val - lo) / (hi - lo) if hi != lo else 0, 0, 1)
            _draw_bar(frame, x, y, w, h, self.label, val, pct, color_bgr, self.text_color, self.show_value,
                      self.decimals)

        elif self.widget_type == "Lap Timer":
            lines = []
            if self.show_time:
                lines.append(("Time: ", _format_lap_clock(data_log.lap_time_at(time_sec))))
            if self.show_last:
                last = data_log.last_lap_time_at(time_sec)
                lines.append(("Last: ", _format_lap_clock(last) if last is not None else "--:--:---"))
            if self.show_best:
                best = data_log.best_lap_time_at(time_sec)
                lines.append(("Best: ", _format_lap_clock(best) if best is not None else "--:--:---"))
            if self.show_lap:
                lines.append(("Lap: ", str(data_log.lap_number_at(time_sec))))
            _draw_lap_timer(frame, x, y, w, h, lines, self.font_size, self.text_color)

        elif self.widget_type == "Track Map":
            if "GPS Latitude" in data_log.channels and "GPS Longitude" in data_log.channels:
                _draw_track_map(frame, x, y, w, h, data_log, time_sec, self.label, self.font_size, self.text_color)


# ---------------------------------------------------------------------------
# OpenCV drawing helpers
# ---------------------------------------------------------------------------

def _draw_numeric(frame, x, y, w, h, label, value, font_size, label_font_size, decimals, text_color):
    cv2.rectangle(frame, (x, y), (x + w, y + h), (20, 20, 20), -1)

    value_text = f"{value:.{decimals}f}"
    value_w, value_h = _pil_text_size(value_text, font_size, bold=True)
    label_w, label_h = _pil_text_size(label, label_font_size)
    rgb = tuple(reversed(text_color))

    def draw(d: ImageDraw.ImageDraw, rx, ry, rw, rh):
        value_x = rx + max(0, (w - value_w) // 2)
        _pil_draw_text(d, (value_x, ry + 8), value_text, _get_pil_font(font_size, bold=True), rgb)
        label_x = rx + max(0, (w - label_w) // 2)
        _pil_draw_text(d, (label_x, ry + 8 + value_h + 4), label, _get_pil_font(label_font_size), rgb)

    _render_text_region(frame, x, y, w, h, draw)


def _draw_bar(frame, x, y, w, h, label, value, pct, color, text_color, show_value=True, decimals=1):
    cv2.rectangle(frame, (x, y), (x + w, y + h), (20, 20, 20), -1)
    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
    bar_w = int((w - 4) * pct)
    if bar_w > 0:
        cv2.rectangle(frame, (x + 2, y + 2), (x + 2 + bar_w, y + h - 2), color, -1)
    text = f"{label}: {value:.{decimals}f}" if show_value else label

    # Grow the text to fill the bar's height (clamped so it doesn't overflow
    # the width), instead of a fixed small font regardless of widget scale.
    pad_x, pad_y = 6, 6
    avail_w = max(1, w - pad_x * 2)
    avail_h = max(1, h - pad_y * 2)
    probe_size = 100
    probe_w, probe_h = _pil_text_size(text, probe_size, bold=True)
    px_size = max(6, int(probe_size * min(avail_h / max(1, probe_h), avail_w / max(1, probe_w))))

    rgb = tuple(reversed(text_color))

    def draw(d: ImageDraw.ImageDraw, rx, ry, rw, rh):
        font = _get_pil_font(px_size, bold=True)
        tw, th = _pil_text_size(text, px_size, bold=True)
        ty = max(0, (rh - th) // 2)
        _pil_draw_text(d, (rx + pad_x, ry + ty), text, font, rgb)

    _render_text_region(frame, x, y, w, h, draw)


def _format_lap_clock(seconds: float) -> str:
    """mm:ss:ms, per 'lap timer format.pdf' (Time: XX:XX:XX)."""
    total_ms = round(seconds * 1000)
    mins, rem_ms = divmod(total_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{mins:02d}:{secs:02d}:{ms:03d}"


def _lap_timer_line_gap(font_size: int) -> int:
    """Vertical distance between lines. Grows sub-linearly with font_size
    (sqrt) so the lines don't spread far apart at high widget scale."""
    base_gap = BASE_FONT_SIZE * 1.3
    return int(base_gap * (font_size / BASE_FONT_SIZE) ** 0.5)


def _lap_timer_text_px_size(font_size: int) -> int:
    """The actual pixel size to render Lap Timer text at — derived from
    line_gap's real estate (line_gap grows sub-linearly with font_size by
    design, but font_size itself grows linearly, so using font_size
    directly as the text's pixel size eventually overflows/clips the line).
    Picks the largest size whose real font metrics (ascent+descent) still
    fit within one line_gap."""
    line_h = _lap_timer_line_gap(font_size)
    probe = 100
    ascent, descent = _get_pil_font(probe, bold=True).getmetrics()
    return max(6, int(probe * line_h / max(1, ascent + descent)))


def _draw_lap_timer(frame, x, y, w, h, lines, font_size, text_color):
    """Renders whichever of Time/Last/Best/Lap lines are enabled, per
    'lap timer format v2.pdf'. lines is [(label, value_str), ...] top to bottom."""
    cv2.rectangle(frame, (x, y), (x + w, y + h), (20, 20, 20), -1)
    line_h = _lap_timer_line_gap(font_size)
    text_px = _lap_timer_text_px_size(font_size)
    rgb = tuple(reversed(text_color))
    label_font = _get_pil_font(text_px, bold=False)
    value_font = _get_pil_font(text_px, bold=True)

    def draw(d: ImageDraw.ImageDraw, rx, ry, rw, rh):
        for i, (label, value) in enumerate(lines):
            _, text_h = _pil_text_size(label, text_px)
            top = ry + line_h * i + max(0, (line_h - text_h) // 2)
            _pil_draw_text(d, (rx + 8, top), label, label_font, rgb)
            label_w, _ = _pil_text_size(label, text_px)
            _pil_draw_text(d, (rx + 8 + label_w, top), value, value_font, rgb)

    _render_text_region(frame, x, y, w, h, draw)


def _draw_track_map(frame, x, y, w, h, data_log: DataLog, time_sec: float, label="", font_size=BASE_FONT_SIZE, text_color=(255, 255, 255)):
    lats = data_log.track_lat
    lons = data_log.track_lon
    if not lats or not lons:
        return

    lat_arr = np.array(lats)
    lon_arr = np.array(lons)
    lat_min, lat_max = lat_arr.min(), lat_arr.max()
    lon_min, lon_max = lon_arr.min(), lon_arr.max()
    pad = 10

    def to_px(lat, lon):
        px = int((lon - lon_min) / (lon_max - lon_min + 1e-9) * (w - pad * 2)) + x + pad
        py = int((1 - (lat - lat_min) / (lat_max - lat_min + 1e-9)) * (h - pad * 2)) + y + pad
        return (px, py)

    cv2.rectangle(frame, (x, y), (x + w, y + h), (20, 20, 20), -1)

    if label:
        rgb = tuple(reversed(text_color))

        def draw(d: ImageDraw.ImageDraw, rx, ry, rw, rh):
            _pil_draw_text(d, (rx + 8, ry + 8), label, _get_pil_font(font_size), rgb)

        _render_text_region(frame, x, y, w, h, draw)

    # Draw track outline
    pts = [to_px(lat_arr[i], lon_arr[i]) for i in range(0, len(lat_arr), max(1, len(lat_arr) // 200))]
    for i in range(1, len(pts)):
        cv2.line(frame, pts[i - 1], pts[i], (150, 150, 150), 2, cv2.LINE_AA)

    # Draw current position dot — held at the start/finish line during the
    # outlap (lap 0), since the car hasn't crossed the beacon yet. Red so it
    # survives chroma-keying against the green screen background.
    if data_log.lap_number_at(time_sec) == 0:
        cur_lat, cur_lon = lat_arr[0], lon_arr[0]
    else:
        cur_lat = data_log.value_at("GPS Latitude", time_sec)
        cur_lon = data_log.value_at("GPS Longitude", time_sec)
    dot = to_px(cur_lat, cur_lon)
    cv2.circle(frame, dot, 6, (0, 0, 255), -1, cv2.LINE_AA)


def _track_map_layout(data_log: "DataLog", rect: QRect, time_sec: float):
    """Map the lap-1 reference GPS path + current position into QPoints within rect,
    for use by both the canvas live preview and (in principle) any other Qt-side view."""
    lats = data_log.track_lat
    lons = data_log.track_lon
    if not lats or not lons:
        return None

    lat_arr = np.array(lats)
    lon_arr = np.array(lons)
    lat_min, lat_max = lat_arr.min(), lat_arr.max()
    lon_min, lon_max = lon_arr.min(), lon_arr.max()
    pad = 4
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()

    def to_pt(lat, lon):
        px = (lon - lon_min) / (lon_max - lon_min + 1e-9) * (w - pad * 2) + x + pad
        py = (1 - (lat - lat_min) / (lat_max - lat_min + 1e-9)) * (h - pad * 2) + y + pad
        return QPoint(int(px), int(py))

    pts = [to_pt(lat_arr[i], lon_arr[i]) for i in range(0, len(lat_arr), max(1, len(lat_arr) // 200))]

    if data_log.lap_number_at(time_sec) == 0:
        cur_lat, cur_lon = lat_arr[0], lon_arr[0]
    else:
        cur_lat = data_log.value_at("GPS Latitude", time_sec)
        cur_lon = data_log.value_at("GPS Longitude", time_sec)
    dot = to_pt(cur_lat, cur_lon)

    return pts, dot


def _fit_pt_for_text(text: str, max_w: float, max_h: float, bold: bool = False,
                      family: str = "Monospace", min_pt: float = 6.0) -> float:
    """Largest QFont point size (float — avoids the int-truncation rounding
    that made small font-size adjustments invisible) at which `text` fits
    within max_w x max_h. cv2's Hershey Simplex (used for the actual export
    render) and Qt's Monospace are very different fonts — at the "same"
    pixel size Hershey renders roughly 2x wider for the same height — so
    deriving the preview's point size from font_size alone (matching
    height) left huge dead space, while fitting only to width (the older
    approach) let height drift out of proportion. Fitting both at once and
    taking whichever is the binding constraint avoids both failure modes."""
    probe_pt = 100.0
    font = QFont(family)
    font.setPointSizeF(probe_pt)
    font.setBold(bold)
    metrics = QFontMetrics(font)
    probe_w = max(1, metrics.horizontalAdvance(text))
    probe_h = max(1, metrics.height())
    pt = probe_pt * min(max_w / probe_w, max_h / probe_h)
    return max(min_pt, pt)


# ---------------------------------------------------------------------------
# Canvas widget (the drag/drop overlay editor)
# ---------------------------------------------------------------------------

class OverlayCanvas(QWidget):
    """Interactive canvas where users position overlay widgets."""

    selection_changed = pyqtSignal(object)  # emits selected OverlayWidget or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.overlay_width = 1920
        self.overlay_height = 1080
        self.widgets: list[OverlayWidget] = []
        self.selected: OverlayWidget | None = None
        self._drag_offset = QPoint()
        self._dragging = False
        self._resize_handle = False
        self.setMinimumSize(640, 360)
        self.setMouseTracking(True)
        self._preview_time = 0.0
        self._preview_progress_sec = 0.0
        self._data_log: DataLog | None = None
        self._preview_pixmap: QPixmap | None = None
        self._video_cap: cv2.VideoCapture | None = None
        self._video_offset = 0.0
        self._video_last_msec = -1.0
        self._video_last_frame = None

    def set_resolution(self, w: int, h: int):
        self.overlay_width = w
        self.overlay_height = h
        self.update()

    def set_data_log(self, dl: DataLog):
        self._data_log = dl

    def set_preview_time(self, t: float, progress_sec: float | None = None):
        """t is the absolute data-log time, used to look up widget channel
        values. progress_sec is the zero-based time since the start of the
        selected lap range (or the whole log if no lap is selected) — that's
        the basis the video offset is measured against, so dialing in an
        offset means the same thing regardless of which lap you're viewing."""
        self._preview_time = t
        self._preview_progress_sec = progress_sec if progress_sec is not None else t
        self.update()

    def set_video(self, path: str | None):
        """Loads a source video to preview behind the overlay, replacing the
        green-screen fill, so positioning/offset can be judged against the
        real footage instead of compositing in DaVinci first."""
        if self._video_cap is not None:
            self._video_cap.release()
            self._video_cap = None
        self._video_last_msec = -1.0
        self._video_last_frame = None
        if path:
            cap = cv2.VideoCapture(path)
            if cap.isOpened():
                self._video_cap = cap
        self.update()

    def set_video_offset(self, offset_sec: float):
        """offset_sec is the video's own t=0 expressed as an absolute
        session time: the video frame shown at absolute session time t is
        the one at video time (t - offset_sec). This stays constant
        regardless of which lap is currently selected — selecting a
        different lap doesn't change where the video actually starts."""
        self._video_offset = offset_sec
        self.update()

    def video_time_for(self, absolute_t: float) -> float:
        return absolute_t - self._video_offset

    # How far forward we'll walk via cheap sequential reads before giving up
    # and seeking instead. A keyframe-seek on long-GOP/high-bitrate footage
    # can cost 100ms+ per call (it has to locate the nearest keyframe and
    # decode forward) versus ~a few ms for a plain sequential read, so during
    # normal forward playback/scrubbing we want to avoid re-seeking every frame.
    _MAX_SEQUENTIAL_READS = 30
    _MAX_SEQUENTIAL_GAP_MS = 2000.0
    # How far behind the last decoded frame's timestamp a request can be and
    # still count as "frame rate lower than tick rate" overshoot rather than
    # a real backward jump — generous enough for any sane source frame rate.
    _MAX_OVERSHOOT_MS = 200.0

    def _video_frame_at(self, t: float) -> QImage | None:
        if self._video_cap is None or t < 0:
            # Before the video's own t=0 (e.g. a positive offset shifts the
            # video later than the current preview position) — there's no
            # frame to show yet, so fall back to the green screen rather
            # than clamping to 0 and sitting on the first frame.
            return None
        target_msec = t * 1000.0
        delta = target_msec - self._video_last_msec
        frame = None

        if -self._MAX_OVERSHOOT_MS <= delta < 0:
            # The frame we already have is still at or after the requested
            # time — happens whenever the video's own frame rate is lower
            # than the ~30Hz preview tick rate (e.g. 24fps footage), since
            # the decoded frame's actual timestamp then regularly overshoots
            # the next tick's target by about one frame interval. Reuse it
            # instead of seeking backward for a frame we're already showing.
            # A *large* negative delta is a real backward jump (e.g. lap
            # selection changed, or the user dragged the scrubber back) and
            # must fall through to an actual seek instead.
            frame = self._video_last_frame
        elif 0 <= delta <= self._MAX_SEQUENTIAL_GAP_MS:
            for _ in range(self._MAX_SEQUENTIAL_READS):
                ok, f = self._video_cap.read()
                if not ok:
                    break
                frame = f
                self._video_last_msec = self._video_cap.get(cv2.CAP_PROP_POS_MSEC)
                if self._video_last_msec >= target_msec:
                    break
        else:
            self._video_cap.set(cv2.CAP_PROP_POS_MSEC, target_msec)
            ok, frame = self._video_cap.read()
            if ok:
                self._video_last_msec = self._video_cap.get(cv2.CAP_PROP_POS_MSEC)

        if frame is None:
            return None
        self._video_last_frame = frame
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, _ = frame.shape
        return QImage(frame.data, w, h, frame.strides[0], QImage.Format.Format_RGB888).copy()

    def _scale(self) -> tuple[float, float, float, float]:
        """Returns (scale, offset_x, offset_y, scale) to map overlay coords to canvas."""
        cw, ch = self.width(), self.height()
        ow, oh = self.overlay_width, self.overlay_height
        scale = min(cw / ow, ch / oh)
        ox = (cw - ow * scale) / 2
        oy = (ch - oh * scale) / 2
        return scale, ox, oy

    def _to_canvas(self, x, y) -> QPoint:
        s, ox, oy = self._scale()
        return QPoint(int(x * s + ox), int(y * s + oy))

    def _to_overlay(self, cx, cy) -> tuple[int, int]:
        s, ox, oy = self._scale()
        return int((cx - ox) / s), int((cy - oy) / s)

    @staticmethod
    def _fit_rect(outer: QRect, content_w: int, content_h: int) -> QRect:
        """Largest rect with content's own aspect ratio, centered within
        `outer` — so a 4:3 source doesn't get stretched to fill a 16:9
        overlay canvas."""
        if content_w <= 0 or content_h <= 0:
            return outer
        scale = min(outer.width() / content_w, outer.height() / content_h)
        w, h = int(content_w * scale), int(content_h * scale)
        x = outer.x() + (outer.width() - w) // 2
        y = outer.y() + (outer.height() - h) // 2
        return QRect(x, y, w, h)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background: checkerboard to represent green screen
        s, ox, oy = self._scale()
        ow = self.overlay_width * s
        oh = self.overlay_height * s
        painter.fillRect(0, 0, self.width(), self.height(), QColor(30, 30, 30))
        frame_rect = QRect(int(ox), int(oy), int(ow), int(oh))
        qimg = self._video_frame_at(self.video_time_for(self._preview_time))
        if qimg is not None:
            painter.fillRect(frame_rect, QColor(0, 0, 0))
            painter.drawImage(self._fit_rect(frame_rect, qimg.width(), qimg.height()), qimg)
        else:
            painter.fillRect(frame_rect, QColor(0, 180, 0))

        # Draw each widget as a placeholder rectangle
        for w in self.widgets:
            # Per-widget time offset compensates channels (e.g. CAN bus data)
            # that lag behind GPS-derived ones — shifting this widget's own
            # data lookup forward/back without touching the shared timeline.
            wt = self._preview_time + w.time_offset
            tl = self._to_canvas(w.x, w.y)
            br = self._to_canvas(w.x + w.w, w.y + w.h)
            rect = QRect(tl, br)

            bg = QColor(20, 20, 20, 200)
            painter.fillRect(rect, bg)

            # The widget's own color outline is only meaningful for Bar
            # Graph (it doubles as the fill color); for everything else it
            # was just decorative clutter. Selection still needs a visible
            # border regardless of type, so editing remains usable.
            if w.widget_type == "Bar Graph" or w.selected:
                pen = QPen(QColor(*reversed(w.color)) if len(w.color) == 3 else QColor(0, 255, 0))
                pen.setWidth(2 if not w.selected else 3)
                if w.selected:
                    pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawRect(rect)

            if w.widget_type == "Track Map" and self._data_log:
                layout = _track_map_layout(self._data_log, rect, wt)
                if layout:
                    pts, dot = layout
                    track_pen = QPen(QColor(150, 150, 150))
                    track_pen.setWidth(2)
                    painter.setPen(track_pen)
                    for i in range(1, len(pts)):
                        painter.drawLine(pts[i - 1], pts[i])
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(255, 0, 0))
                    painter.drawEllipse(dot, 5, 5)
                    painter.setBrush(Qt.BrushStyle.NoBrush)

            elif w.widget_type == "Bar Graph":
                val = self._data_log.value_at(w.channel, wt) if (self._data_log and w.channel) else 0.0
                lo, hi = w.min_val, w.max_val
                pct = max(0.0, min(1.0, (val - lo) / (hi - lo))) if hi != lo else 0.0
                inner = rect.adjusted(3, 3, -3, -3)
                painter.fillRect(inner, QColor(15, 15, 15))
                bar_color = QColor(*reversed(w.color)) if len(w.color) == 3 else QColor(0, 255, 0)
                bar_w = int(inner.width() * pct)
                if bar_w > 0:
                    painter.fillRect(QRect(inner.x(), inner.y(), bar_w, inner.height()), bar_color)

            # Label
            painter.setPen(QPen(QColor(220, 220, 220)))
            pt_size = max(6, int(w.font_size / BASE_FONT_SIZE * 10 * s))
            font = QFont("Monospace", pt_size)
            painter.setFont(font)
            if w.widget_type == "Track Map":
                painter.drawText(rect.adjusted(4, 4, -4, -4), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                                 w.label)
            elif w.widget_type == "Lap Timer":
                dl = self._data_log
                t = wt
                lap_lines = []
                if w.show_time:
                    lap_lines.append(("Time: ", _format_lap_clock(dl.lap_time_at(t)) if dl else "00:00:000"))
                if w.show_last:
                    last = dl.last_lap_time_at(t) if dl else None
                    lap_lines.append(("Last: ", _format_lap_clock(last) if last is not None else "--:--:---"))
                if w.show_best:
                    best = dl.best_lap_time_at(t) if dl else None
                    lap_lines.append(("Best: ", _format_lap_clock(best) if best is not None else "--:--:---"))
                if w.show_lap:
                    lap_lines.append(("Lap: ", str(dl.lap_number_at(t)) if dl else "0"))

                inner = rect.adjusted(8, 4, -4, -4)
                line_h = max(1, int(_lap_timer_line_gap(w.font_size) * s))
                text_color = QColor(*reversed(w.text_color)) if len(w.text_color) == 3 else QColor(255, 255, 255)

                # Fit to both the box's width (using the widest line that
                # could appear, so the font doesn't resize as lines change)
                # and line_h — taking whichever is the binding constraint.
                # Hershey Simplex (the export's font) and Qt's Monospace are
                # different enough that fitting only one dimension either
                # left dead space or let lines overlap/drift out of sync
                # with line_h at extreme widget scales.
                ref_text = "Time: 00:00:000" if any(l in ("Time: ", "Last: ", "Best: ") for l, _ in lap_lines) else "Lap: 0"
                fit_pt = _fit_pt_for_text(ref_text, inner.width(), line_h, bold=True)
                font = QFont("Monospace")
                font.setPointSizeF(fit_pt)
                bold_font = QFont(font)
                bold_font.setBold(True)

                def draw_line(top, label, value):
                    line_rect = QRect(inner.x(), top, inner.width(), line_h)
                    painter.setFont(font)
                    painter.setPen(QPen(text_color))
                    painter.drawText(line_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
                    label_w = painter.fontMetrics().horizontalAdvance(label)
                    painter.setFont(bold_font)
                    value_rect = QRect(inner.x() + label_w, top, inner.width() - label_w, line_h)
                    painter.drawText(value_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, value)

                for i, (label, value) in enumerate(lap_lines):
                    draw_line(inner.y() + line_h * i, label, value)
            elif w.widget_type == "Bar Graph":
                val = self._data_log.value_at(w.channel, wt) if (self._data_log and w.channel) else 0.0
                text = f"{w.label}: {val:.{w.decimals}f}" if w.show_value else w.label
                inner = rect.adjusted(6, 6, -6, -6)

                # Grow the text to fill the bar's height (clamped to width),
                # same fit-to-box approach as the cv2 export.
                probe_pt = 20
                probe_metrics = QFontMetrics(QFont("Monospace", probe_pt))
                probe_w = probe_metrics.horizontalAdvance(text)
                probe_h = probe_metrics.height()
                fit_pt = max(6, int(min(probe_pt * inner.width() / max(1, probe_w),
                                         probe_pt * inner.height() / max(1, probe_h))))
                painter.setFont(QFont("Monospace", fit_pt))
                painter.setPen(QPen(QColor(*reversed(w.text_color)) if len(w.text_color) == 3 else QColor(255, 255, 255)))
                painter.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            elif w.widget_type == "Numeric Display":
                val = self._data_log.value_at(w.channel, wt) if (self._data_log and w.channel) else 0.0
                inner = rect.adjusted(8, 4, -4, -4)
                text_color = QColor(*reversed(w.text_color)) if len(w.text_color) == 3 else QColor(255, 255, 255)

                # Split inner's height between value/label in the same
                # proportion OverlayWidget.h used (cv2 metrics) to size the
                # box in the first place, then fit each one's own font to
                # its share of (width, height) — see _fit_pt_for_text for
                # why fitting just one dimension isn't enough.
                cv_value_scale = w.font_size / 30.0
                (_, cv_value_h), cv_value_base = cv2.getTextSize(
                    w._numeric_ref_text(), cv2.FONT_HERSHEY_SIMPLEX, cv_value_scale, 2)
                cv_label_scale = w.label_font_size / 30.0
                (_, cv_label_h), cv_label_base = cv2.getTextSize(
                    w.label or "A", cv2.FONT_HERSHEY_SIMPLEX, cv_label_scale, 1)
                value_share = (cv_value_h + cv_value_base)
                label_share = (cv_label_h + cv_label_base)
                total_share = max(1, value_share + label_share)
                value_h_budget = max(1, int(inner.height() * value_share / total_share))
                label_h_budget = max(1, inner.height() - value_h_budget - 2)

                value_text = f"{val:.{w.decimals}f}"
                # Fit to the same fixed reference text the box width was
                # computed from, not the live value — otherwise a shorter
                # value (e.g. "0.00" vs the "000.00" budget) would render
                # oversized and overflow the box instead of just leaving
                # the same intentional slack the box was sized with.
                value_pt = _fit_pt_for_text(w._numeric_ref_text(), inner.width(), value_h_budget, bold=True)
                value_font = QFont("Monospace")
                value_font.setPointSizeF(value_pt)
                value_font.setBold(True)
                value_rect = QRect(inner.x(), inner.y(), inner.width(), value_h_budget)
                painter.setFont(value_font)
                painter.setPen(QPen(text_color))
                painter.drawText(value_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, value_text)

                label_pt = _fit_pt_for_text(w.label or "A", inner.width(), label_h_budget)
                label_font = QFont("Monospace")
                label_font.setPointSizeF(label_pt)
                label_rect = QRect(inner.x(), value_rect.bottom() + 2, inner.width(), label_h_budget)
                painter.setFont(label_font)
                painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, w.label)
            else:
                painter.drawText(rect.adjusted(4, 4, -4, -4), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 w.label)

            # Resize handle
            if w.selected:
                handle = QRect(br.x() - 8, br.y() - 8, 8, 8)
                painter.fillRect(handle, QColor(255, 255, 0))

        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        ox, oy = self._to_overlay(event.position().x(), event.position().y())

        # Check resize handle first
        if self.selected:
            rx = self.selected.x + self.selected.w
            ry = self.selected.y + self.selected.h
            s, _, _ = self._scale()
            handle_px = 8 / s
            if abs(ox - rx) < handle_px and abs(oy - ry) < handle_px:
                self._resize_handle = True
                self._drag_offset = QPoint(ox, oy)
                return

        # Hit test widgets (reverse order = top-first)
        hit = None
        for w in reversed(self.widgets):
            if w.x <= ox <= w.x + w.w and w.y <= oy <= w.y + w.h:
                hit = w
                break

        for w in self.widgets:
            w.selected = (w is hit)
        self.selected = hit
        self._dragging = hit is not None
        self._resize_handle = False
        if hit:
            self._drag_offset = QPoint(ox - hit.x, oy - hit.y)
        self.selection_changed.emit(hit)
        self.update()

    def _overlaps_others(self, widget: "OverlayWidget", x: int, y: int, w: int, h: int) -> bool:
        for other in self.widgets:
            if other is widget:
                continue
            if x < other.x + other.w and x + w > other.x and y < other.y + other.h and y + h > other.y:
                return True
        return False

    def _find_free_spot(self, widget: "OverlayWidget") -> tuple[int, int]:
        """Find a position for a newly added widget that doesn't overlap existing ones."""
        step = 20
        x, y = 50, 50
        while self._overlaps_others(widget, x, y, widget.w, widget.h):
            y += step
            if y + widget.h > self.overlay_height:
                y = 50
                x += step * 3
            if x + widget.w > self.overlay_width:
                break  # no free space left — place it anyway rather than loop forever
        return x, y

    def mouseMoveEvent(self, event):
        if not (self._dragging or self._resize_handle):
            return
        ox, oy = self._to_overlay(event.position().x(), event.position().y())
        if self.selected:
            if self._resize_handle:
                base_w, base_h = self.selected.base_size
                new_w = max(20, ox - self.selected.x)
                new_h = max(10, oy - self.selected.y)
                prev_sx, prev_sy = self.selected.scale_x, self.selected.scale_y
                if self.selected.widget_type == "Bar Graph":
                    # Bar Graph supports independent x/y scaling via corner drag.
                    self.selected.scale_x = max(0.2, new_w / base_w)
                    self.selected.scale_y = max(0.2, new_h / base_h)
                else:
                    s = max(0.2, new_w / base_w)
                    self.selected.scale_x = s
                    self.selected.scale_y = s
                # Reject resizes that would make the widget overlap a neighbor.
                if self._overlaps_others(self.selected, self.selected.x, self.selected.y,
                                          self.selected.w, self.selected.h):
                    self.selected.scale_x, self.selected.scale_y = prev_sx, prev_sy
            else:
                new_x = max(0, min(self.overlay_width - self.selected.w, ox - self._drag_offset.x()))
                new_y = max(0, min(self.overlay_height - self.selected.h, oy - self._drag_offset.y()))
                w_, h_ = self.selected.w, self.selected.h
                # Try the full move; if it would overlap, slide along whichever
                # single axis still works, so dragging diagonally past another
                # widget slides along its edge instead of just freezing.
                if not self._overlaps_others(self.selected, new_x, new_y, w_, h_):
                    self.selected.x, self.selected.y = new_x, new_y
                elif not self._overlaps_others(self.selected, new_x, self.selected.y, w_, h_):
                    self.selected.x = new_x
                elif not self._overlaps_others(self.selected, self.selected.x, new_y, w_, h_):
                    self.selected.y = new_y
            self.update()

    def mouseReleaseEvent(self, event):
        self._dragging = False
        self._resize_handle = False

    def add_widget(self, widget_type: str):
        w = OverlayWidget(widget_type)
        w.x, w.y = self._find_free_spot(w)
        self.widgets.append(w)
        for ww in self.widgets:
            ww.selected = False
        w.selected = True
        self.selected = w
        self.selection_changed.emit(w)
        self.update()

    def delete_selected(self):
        if self.selected in self.widgets:
            self.widgets.remove(self.selected)
            self.selected = None
            self.selection_changed.emit(None)
            self.update()


# ---------------------------------------------------------------------------
# Widget property panel
# ---------------------------------------------------------------------------

class PropertiesPanel(QWidget):
    """Panel to configure the selected overlay widget."""

    changed = pyqtSignal()

    def __init__(self, channels: list[str], parent=None):
        super().__init__(parent)
        self._widget: OverlayWidget | None = None
        self._channels = channels
        self._data_log: DataLog | None = None
        self._build_ui()

    def set_data_log(self, dl: "DataLog"):
        self._data_log = dl

    def _build_ui(self):
        layout = QFormLayout(self)

        self.lbl_type = QLabel("—")
        layout.addRow("Type:", self.lbl_type)

        self.txt_label = QLineEdit()
        self.txt_label.textChanged.connect(self._on_label)
        layout.addRow("Label:", self.txt_label)

        self.btn_color = QPushButton("Color…")
        self.btn_color.clicked.connect(self._on_pick_color)
        layout.addRow("Color:", self.btn_color)

        self.chk_show_value = QCheckBox("Show numeric value")
        self.chk_show_value.toggled.connect(self._on_show_value)
        layout.addRow("", self.chk_show_value)

        self.btn_text_color = QPushButton("Text Color…")
        self.btn_text_color.clicked.connect(self._on_pick_text_color)
        layout.addRow("Text Color:", self.btn_text_color)

        self.chk_show_time = QCheckBox("Show Time")
        self.chk_show_time.toggled.connect(self._on_lap_lines)
        layout.addRow("", self.chk_show_time)

        self.chk_show_last = QCheckBox("Show Last")
        self.chk_show_last.toggled.connect(self._on_lap_lines)
        layout.addRow("", self.chk_show_last)

        self.chk_show_best = QCheckBox("Show Best")
        self.chk_show_best.toggled.connect(self._on_lap_lines)
        layout.addRow("", self.chk_show_best)

        self.chk_show_lap = QCheckBox("Show Lap")
        self.chk_show_lap.toggled.connect(self._on_lap_lines)
        layout.addRow("", self.chk_show_lap)

        self.cmb_channel = QComboBox()
        self.cmb_channel.addItem("(none)")
        self.cmb_channel.addItems(self._channels)
        self.cmb_channel.currentTextChanged.connect(self._on_channel)
        layout.addRow("Channel:", self.cmb_channel)

        self.spn_x = QSpinBox(); self.spn_x.setRange(0, 3840)
        self.spn_y = QSpinBox(); self.spn_y.setRange(0, 2160)
        for spn in (self.spn_x, self.spn_y):
            spn.valueChanged.connect(self._on_geom)
        layout.addRow("X:", self.spn_x)
        layout.addRow("Y:", self.spn_y)

        self.spn_scale = QDoubleSpinBox(); self.spn_scale.setRange(0.2, 10.0)
        self.spn_scale.setSingleStep(0.1)
        self.spn_scale.setValue(1.0)
        self.spn_scale.valueChanged.connect(self._on_scale)
        layout.addRow("Scale:", self.spn_scale)

        # Bar Graph gets independent x/y scaling instead of the single Scale field.
        self.spn_scale_x = QDoubleSpinBox(); self.spn_scale_x.setRange(0.2, 10.0)
        self.spn_scale_x.setSingleStep(0.1)
        self.spn_scale_x.setValue(1.0)
        self.spn_scale_x.valueChanged.connect(self._on_scale_xy)
        layout.addRow("Scale X:", self.spn_scale_x)

        self.spn_scale_y = QDoubleSpinBox(); self.spn_scale_y.setRange(0.2, 10.0)
        self.spn_scale_y.setSingleStep(0.1)
        self.spn_scale_y.setValue(1.0)
        self.spn_scale_y.valueChanged.connect(self._on_scale_xy)
        layout.addRow("Scale Y:", self.spn_scale_y)

        self.spn_min = QDoubleSpinBox(); self.spn_min.setRange(-99999, 99999)
        self.spn_max = QDoubleSpinBox(); self.spn_max.setRange(-99999, 99999); self.spn_max.setValue(100)
        self.spn_min.valueChanged.connect(self._on_range)
        self.spn_max.valueChanged.connect(self._on_range)
        layout.addRow("Min:", self.spn_min)
        layout.addRow("Max:", self.spn_max)

        # Numeric Display / Bar Graph: how many decimal places the value is shown with.
        self.spn_decimals = QSpinBox()
        self.spn_decimals.setRange(0, 4)
        self.spn_decimals.setValue(2)
        self.spn_decimals.valueChanged.connect(self._on_decimals)
        layout.addRow("Decimals:", self.spn_decimals)

        # Numeric Display: the value ("XXX") uses Scale above; the label
        # beneath it gets its own independent absolute size here.
        self.spn_label_font_size = QSpinBox()
        self.spn_label_font_size.setRange(6, 96)
        self.spn_label_font_size.setValue(16)
        self.spn_label_font_size.valueChanged.connect(self._on_label_font_size)
        layout.addRow("Label Font Size:", self.spn_label_font_size)

        # Shifts just this widget's data lookup — fixes channels (e.g. CAN
        # bus data) that lag behind GPS-derived ones, without touching the
        # shared scrubber timeline or video sync.
        self.spn_time_offset = QDoubleSpinBox()
        self.spn_time_offset.setRange(-10.0, 10.0)
        self.spn_time_offset.setSingleStep(0.05)
        self.spn_time_offset.setDecimals(2)
        self.spn_time_offset.setSuffix(" s")
        self.spn_time_offset.valueChanged.connect(self._on_time_offset)
        layout.addRow("Time Offset:", self.spn_time_offset)

        self.chk_show_value.setVisible(False)
        self._set_row_visible(self.btn_color, False)
        self._set_row_visible(self.btn_text_color, False)
        self._set_row_visible(self.chk_show_time, False)
        self._set_row_visible(self.chk_show_last, False)
        self._set_row_visible(self.chk_show_best, False)
        self._set_row_visible(self.chk_show_lap, False)
        self._set_row_visible(self.spn_scale_x, False)
        self._set_row_visible(self.spn_scale_y, False)
        self._set_row_visible(self.spn_decimals, False)
        self._set_row_visible(self.spn_label_font_size, False)
        self.setEnabled(False)

    def set_channels(self, channels: list[str]):
        self._channels = channels
        cur = self.cmb_channel.currentText()
        self.cmb_channel.clear()
        self.cmb_channel.addItem("(none)")
        self.cmb_channel.addItems(channels)
        idx = self.cmb_channel.findText(cur)
        if idx >= 0:
            self.cmb_channel.setCurrentIndex(idx)

    def load_widget(self, w: OverlayWidget | None):
        self._widget = w
        self.setEnabled(w is not None)
        if w is None:
            return
        self.lbl_type.setText(w.widget_type)
        self.txt_label.blockSignals(True); self.txt_label.setText(w.label); self.txt_label.blockSignals(False)
        self._update_color_swatch(w.color)
        is_bar = w.widget_type == "Bar Graph"
        is_lap_timer = w.widget_type == "Lap Timer"
        has_text_color = w.widget_type in ("Lap Timer", "Bar Graph", "Numeric Display")
        no_channel = is_lap_timer or w.widget_type == "Track Map"
        # Track Map doesn't render anything in widget.color — the generic
        # Color picker is otherwise the fill (Bar Graph) / border (Numeric
        # Display) color; Text Color is separate and used by all three
        # text-bearing widget types.
        self._set_row_visible(self.btn_color, w.widget_type in ("Bar Graph", "Numeric Display"))
        self.chk_show_value.setVisible(is_bar)
        self.chk_show_value.blockSignals(True); self.chk_show_value.setChecked(w.show_value); self.chk_show_value.blockSignals(False)

        self._set_row_visible(self.btn_text_color, has_text_color)
        self._update_text_color_swatch(w.text_color)
        for chk_row, attr in [(self.chk_show_time, "show_time"), (self.chk_show_last, "show_last"),
                               (self.chk_show_best, "show_best"), (self.chk_show_lap, "show_lap")]:
            self._set_row_visible(chk_row, is_lap_timer)
            chk_row.blockSignals(True); chk_row.setChecked(getattr(w, attr)); chk_row.blockSignals(False)

        idx = self.cmb_channel.findText(w.channel)
        self.cmb_channel.setCurrentIndex(idx if idx >= 0 else 0)
        for spn, val in [(self.spn_x, w.x), (self.spn_y, w.y)]:
            spn.blockSignals(True); spn.setValue(val); spn.blockSignals(False)
        self.spn_scale.blockSignals(True); self.spn_scale.setValue(w.scale_x); self.spn_scale.blockSignals(False)
        self.spn_scale_x.blockSignals(True); self.spn_scale_x.setValue(w.scale_x); self.spn_scale_x.blockSignals(False)
        self.spn_scale_y.blockSignals(True); self.spn_scale_y.setValue(w.scale_y); self.spn_scale_y.blockSignals(False)
        self._set_row_visible(self.spn_scale, not is_bar)
        self._set_row_visible(self.spn_scale_x, is_bar)
        self._set_row_visible(self.spn_scale_y, is_bar)
        self.spn_min.blockSignals(True); self.spn_min.setValue(w.min_val); self.spn_min.blockSignals(False)
        self.spn_max.blockSignals(True); self.spn_max.setValue(w.max_val); self.spn_max.blockSignals(False)
        self.spn_time_offset.blockSignals(True); self.spn_time_offset.setValue(w.time_offset); self.spn_time_offset.blockSignals(False)

        has_decimals = w.widget_type in ("Numeric Display", "Bar Graph")
        self._set_row_visible(self.spn_decimals, has_decimals)
        self.spn_decimals.blockSignals(True); self.spn_decimals.setValue(w.decimals); self.spn_decimals.blockSignals(False)

        is_numeric = w.widget_type == "Numeric Display"
        self._set_row_visible(self.spn_label_font_size, is_numeric)
        self.spn_label_font_size.blockSignals(True)
        self.spn_label_font_size.setValue(w.label_font_size)
        self.spn_label_font_size.blockSignals(False)

        self.cmb_channel.setEnabled(not no_channel)
        self.spn_min.setEnabled(not no_channel)
        self.spn_max.setEnabled(not no_channel)

    def _on_show_value(self, checked):
        if self._widget:
            self._widget.show_value = checked
            self.changed.emit()

    def _on_label(self, text):
        if self._widget:
            self._widget.label = text
            self.changed.emit()

    def _update_color_swatch(self, color_bgr: tuple):
        r, g, b = reversed(color_bgr) if len(color_bgr) == 3 else (0, 255, 0)
        self.btn_color.setStyleSheet(f"background-color: rgb({r},{g},{b});")

    def _on_pick_color(self):
        if not self._widget:
            return
        r, g, b = reversed(self._widget.color) if len(self._widget.color) == 3 else (0, 255, 0)
        chosen = QColorDialog.getColor(QColor(r, g, b), self, "Pick Bar Color")
        if chosen.isValid():
            self._widget.color = (chosen.blue(), chosen.green(), chosen.red())  # BGR for OpenCV
            self._update_color_swatch(self._widget.color)
            self.changed.emit()

    def _update_text_color_swatch(self, color_bgr: tuple):
        r, g, b = reversed(color_bgr) if len(color_bgr) == 3 else (255, 255, 255)
        self.btn_text_color.setStyleSheet(f"background-color: rgb({r},{g},{b});")

    def _on_pick_text_color(self):
        if not self._widget:
            return
        r, g, b = reversed(self._widget.text_color) if len(self._widget.text_color) == 3 else (255, 255, 255)
        chosen = QColorDialog.getColor(QColor(r, g, b), self, "Pick Text Color")
        if chosen.isValid():
            self._widget.text_color = (chosen.blue(), chosen.green(), chosen.red())  # BGR for OpenCV
            self._update_text_color_swatch(self._widget.text_color)
            self.changed.emit()

    def _on_lap_lines(self):
        if self._widget:
            self._widget.show_time = self.chk_show_time.isChecked()
            self._widget.show_last = self.chk_show_last.isChecked()
            self._widget.show_best = self.chk_show_best.isChecked()
            self._widget.show_lap = self.chk_show_lap.isChecked()
            self.changed.emit()

    def _on_channel(self, text):
        if self._widget:
            self._widget.channel = text if text != "(none)" else ""
            # Scale the bar off the channel's actual range (e.g. PEDAL POSITION's
            # observed min/max) instead of leaving it at the 0–100 default.
            if self._widget.widget_type == "Bar Graph" and self._widget.channel and self._data_log:
                lo, hi = self._data_log.channel_range(self._widget.channel)
                self._widget.min_val = lo
                self._widget.max_val = hi
                self.spn_min.blockSignals(True); self.spn_min.setValue(lo); self.spn_min.blockSignals(False)
                self.spn_max.blockSignals(True); self.spn_max.setValue(hi); self.spn_max.blockSignals(False)
            self.changed.emit()

    def _on_geom(self):
        if self._widget:
            self._widget.x = self.spn_x.value()
            self._widget.y = self.spn_y.value()
            self.changed.emit()

    def _on_scale(self):
        if self._widget:
            self._widget.scale_x = self.spn_scale.value()
            self._widget.scale_y = self.spn_scale.value()
            self.changed.emit()

    def _on_scale_xy(self):
        if self._widget:
            self._widget.scale_x = self.spn_scale_x.value()
            self._widget.scale_y = self.spn_scale_y.value()
            self.changed.emit()

    def _set_row_visible(self, field_widget: QWidget, visible: bool):
        layout: QFormLayout = self.layout()
        label = layout.labelForField(field_widget)
        if label:
            label.setVisible(visible)
        field_widget.setVisible(visible)

    def _on_range(self):
        if self._widget:
            self._widget.min_val = self.spn_min.value()
            self._widget.max_val = self.spn_max.value()
            self.changed.emit()

    def _on_time_offset(self, value: float):
        if self._widget:
            self._widget.time_offset = value
            self.changed.emit()

    def _on_decimals(self, value: int):
        if self._widget:
            self._widget.decimals = value
            self.changed.emit()

    def _on_label_font_size(self, value: int):
        if self._widget:
            self._widget.label_font_size = value
            self.changed.emit()


# ---------------------------------------------------------------------------
# Preview proxy thread
# ---------------------------------------------------------------------------

def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for candidate in (
        r"C:\Program Files (x86)\MOZA Pit House\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


# Builds (vary quite a bit between bundled ffmpeg distributions — the GPL-only
# libx264 isn't always present) are tried in this order, with the encoder-
# specific flags each one actually understands. -g (GOP size) is the one flag
# that matters for fixing seek lag and is supported across the board.
_ENCODER_PREFERENCE = [
    ("libx264", ["-preset", "veryfast", "-crf", "23", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0"]),
    ("h264_nvenc", ["-preset", "fast", "-b:v", "4M", "-g", "30"]),
    ("h264_amf", ["-b:v", "4M", "-g", "30"]),
    ("h264_qsv", ["-b:v", "4M", "-g", "30"]),
    ("h264_mf", ["-b:v", "4M", "-g", "30"]),
]


def pick_video_encoder(ffmpeg_path: str) -> tuple[str, list[str]]:
    """Picks the first usable H.264 encoder this ffmpeg build actually has."""
    try:
        result = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=10)
        available = result.stdout
    except Exception:
        available = ""
    for name, args in _ENCODER_PREFERENCE:
        if name in available:
            return name, args
    return _ENCODER_PREFERENCE[0]  # fall back and let ffmpeg report the error


class ProxyThread(QThread):
    """Transcodes a low-res, short-GOP copy of a source video for smooth
    in-app preview. Camera/editor exports are often near-single-GOP (one
    keyframe for the whole clip) at very high bitrate — great for quality,
    terrible for scrubbing, since every seek has to decode forward from
    that one keyframe. A small GOP fixes seeking; the lower resolution/
    bitrate just makes each frame cheaper to decode."""

    finished = Signal(str)
    error = Signal(str)

    def __init__(self, ffmpeg_path: str, src_path: str, dst_path: str, height: int = 540):
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.src_path = src_path
        self.dst_path = dst_path
        self.height = height

    def run(self):
        try:
            encoder, encoder_args = pick_video_encoder(self.ffmpeg_path)
            cmd = [
                self.ffmpeg_path, "-y", "-i", self.src_path,
                # format=yuv420p forces 8-bit — some cameras (e.g. DJI Osmo
                # Action 4 in its higher-quality modes) record 10-bit HEVC,
                # which most H.264 encoders (nvenc included) flatly refuse.
                "-vf", f"scale=-2:{self.height},format=yuv420p",
                "-c:v", encoder, *encoder_args,
                "-c:a", "aac", "-b:a", "128k",
                self.dst_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                self.error.emit(result.stderr[-2000:])
                return
            self.finished.emit(self.dst_path)
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Export thread
# ---------------------------------------------------------------------------

class ExportThread(QThread):
    progress = Signal(int)
    finished = Signal(str, str, str)  # (video_path, sync_file_path or "", audio_note or "")
    error = Signal(str)
    canceled = Signal()

    def __init__(self, widgets, data_log, fps, out_path,
                 lap_windows: list[tuple[float, float, list[tuple[int, float]]]] | None = None,
                 write_sync_file: bool = False,
                 video_path: str | None = None, video_offset: float = 0.0,
                 output_size: tuple[int, int] | None = None):
        super().__init__()
        self.widgets = widgets
        self.data_log = data_log
        self.fps = fps
        self.out_path = out_path
        # Each window: (render_start, render_end, markers). render_start/end is
        # the (possibly padded) range to render — contiguous selected laps share
        # one window so padding only lands at the outer edges, not in between.
        # markers is [(lap_number, true_lap_start), ...] for the sync file.
        self.lap_windows = lap_windows or [
            (data_log.timestamps[0], data_log.timestamps[-1], [(0, data_log.timestamps[0])])
        ]
        self.write_sync_file = write_sync_file
        # video_path is the original full-quality source (never the low-lag
        # proxy), composited in place of the green screen. video_offset uses
        # the same zero-based-at-selection-start convention as the live
        # preview, so whatever offset was dialed in there carries over exactly.
        self.video_path = video_path
        self.video_offset = video_offset
        self.output_size = output_size
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    # Mirrors OverlayCanvas._video_frame_at's sequential-read optimization —
    # re-seeking every frame is what made the in-app preview laggy in the
    # first place, and it's just as costly here.
    _MAX_SEQUENTIAL_READS = 30
    _MAX_SEQUENTIAL_GAP_MS = 2000.0
    _MAX_OVERSHOOT_MS = 200.0

    def _video_frame_at(self, cap, last_msec: float, last_frame, target_msec: float, size: tuple[int, int]):
        delta = target_msec - last_msec
        frame = None
        if -self._MAX_OVERSHOOT_MS <= delta < 0:
            # Same overshoot case as OverlayCanvas._video_frame_at — the
            # frame we already have is still at or after the requested
            # time (export fps lower than source fps), so reuse it rather
            # than seeking backward for nothing. A large negative delta is
            # a real backward jump and must fall through to a real seek.
            frame = last_frame
        elif 0 <= delta <= self._MAX_SEQUENTIAL_GAP_MS:
            for _ in range(self._MAX_SEQUENTIAL_READS):
                ok, f = cap.read()
                if not ok:
                    break
                frame = f
                last_msec = cap.get(cv2.CAP_PROP_POS_MSEC)
                if last_msec >= target_msec:
                    break
        else:
            cap.set(cv2.CAP_PROP_POS_MSEC, target_msec)
            ok, frame = cap.read()
            if ok:
                last_msec = cap.get(cv2.CAP_PROP_POS_MSEC)
        if frame is None:
            return None, last_msec, last_frame
        return self._letterbox(frame, size), last_msec, frame

    @staticmethod
    def _letterbox(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
        """Scales `frame` to fit within `size` preserving its own aspect
        ratio, centered on a black canvas of exactly `size` — so a 4:3
        source doesn't get stretched to fill a 16:9 export frame."""
        target_w, target_h = size
        fh, fw = frame.shape[:2]
        if fw <= 0 or fh <= 0:
            return np.zeros((target_h, target_w, 3), dtype=np.uint8)
        scale = min(target_w / fw, target_h / fh)
        new_w, new_h = max(1, int(fw * scale)), max(1, int(fh * scale))
        resized = cv2.resize(frame, (new_w, new_h))
        canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
        x_off, y_off = (target_w - new_w) // 2, (target_h - new_h) // 2
        canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
        return canvas

    def _mux_audio(self, silent_video_path: str) -> str:
        """Pulls the matching audio out of the source video (trimmed/offset
        the same way the video frames were) and muxes it into the rendered
        file. cv2.VideoWriter can't write audio at all, so this is a
        separate ffmpeg pass after the frames are written. Returns a warning
        string if it couldn't be done (export still succeeds, just silent)."""
        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path:
            return "ffmpeg wasn't found, so the export has no audio. Install ffmpeg and put it on PATH to get audio."

        tmp_dir = tempfile.mkdtemp(prefix="whrrah_audio_")
        try:
            segment_paths = []
            for start, end, _markers in self.lap_windows:
                duration = end - start
                # video_offset is the video's t=0 expressed as an absolute
                # session time, so each window's own absolute start maps
                # directly onto a video position — no need to track a
                # separate render-relative running total.
                audio_start = max(0.0, start - self.video_offset)
                seg_path = os.path.join(tmp_dir, f"seg_{len(segment_paths)}.m4a")
                result = subprocess.run(
                    [ffmpeg_path, "-y", "-ss", f"{audio_start}", "-t", f"{duration}",
                     "-i", self.video_path, "-vn", "-c:a", "aac", "-b:a", "192k", seg_path],
                    capture_output=True, text=True
                )
                if result.returncode != 0:
                    return f"Couldn't extract audio for export: {result.stderr[-500:]}"
                segment_paths.append(seg_path)

            if len(segment_paths) == 1:
                audio_path = segment_paths[0]
            else:
                concat_list = os.path.join(tmp_dir, "concat.txt")
                with open(concat_list, "w") as f:
                    for p in segment_paths:
                        f.write(f"file '{p}'\n")
                audio_path = os.path.join(tmp_dir, "audio_concat.m4a")
                result = subprocess.run(
                    [ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", audio_path],
                    capture_output=True, text=True
                )
                if result.returncode != 0:
                    return f"Couldn't join audio segments for export: {result.stderr[-500:]}"

            muxed_path = silent_video_path + ".muxed.mp4"
            result = subprocess.run(
                [ffmpeg_path, "-y", "-i", silent_video_path, "-i", audio_path,
                 "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-shortest", muxed_path],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                return f"Couldn't mux audio into export: {result.stderr[-500:]}"

            # Replacing the output path can transiently fail on Windows if
            # something else (antivirus scan, Explorer thumbnail, a media
            # player, or our own app's preview) briefly has it open — retry
            # a few times before giving up, rather than failing the whole
            # export over a lock that's usually gone within a second.
            last_error = None
            for attempt in range(5):
                try:
                    os.replace(muxed_path, silent_video_path)
                    return ""
                except OSError as e:
                    last_error = e
                    if attempt < 4:
                        time.sleep(0.5)
            try:
                os.remove(muxed_path)
            except OSError:
                pass
            return (f"Couldn't replace the output file with the audio-muxed version "
                    f"(it's the silent version without audio): {last_error}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def run(self):
        writer = None
        video_cap = None
        try:
            dl = self.data_log
            frames_per_window = [max(0, int((end - start) * self.fps)) for start, end, _ in self.lap_windows]
            total_frames = sum(frames_per_window)
            if total_frames == 0:
                self.error.emit("Selected laps have zero duration.")
                return

            if self.video_path:
                video_cap = cv2.VideoCapture(self.video_path)
                if not video_cap.isOpened():
                    self.error.emit(f"Could not open video for compositing:\n{self.video_path}")
                    return
                w_max, h_max = self.output_size or (1920, 1080)
                video_last_msec = -1.0
                video_last_frame = None
            else:
                # No video loaded — fall back to the original green-screen
                # behavior, sized to the widget bounds for chroma-keying.
                w_max = max((ww.x + ww.w for ww in self.widgets), default=1920)
                h_max = max((ww.y + ww.h for ww in self.widgets), default=1080)

            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(self.out_path, fourcc, self.fps, (w_max, h_max))

            # Also used when video_cap is set but a positive offset means
            # the video hasn't actually started yet at this point in the render.
            green = np.zeros((h_max, w_max, 3), dtype=np.uint8)
            green[:, :] = (0, 180, 0)  # pure BGR green screen

            done = 0
            sync_entries = []  # (lap_number, frame_idx, time_sec)
            for (start, _end, markers), n_frames in zip(self.lap_windows, frames_per_window):
                for lap_number, marker_t in markers:
                    marker_frame_in_window = max(0, min(n_frames - 1, int(round((marker_t - start) * self.fps))))
                    sync_entries.append((lap_number, done + marker_frame_in_window,
                                          (done + marker_frame_in_window) / self.fps))

                for frame_idx in range(n_frames):
                    if self._cancel_requested:
                        writer.release()
                        writer = None
                        if os.path.exists(self.out_path):
                            os.remove(self.out_path)
                        self.canceled.emit()
                        return
                    t = start + frame_idx / self.fps

                    if video_cap is not None:
                        # video_offset is the video's own t=0 expressed as an
                        # absolute session time (same basis the live preview
                        # uses), so it's compared against the absolute time
                        # `t`, not the render-relative frame count.
                        video_time = t - self.video_offset
                        if video_time < 0:
                            # Positive offset means the video hasn't started
                            # yet at this point — green screen, not frame 0.
                            frame = green.copy()
                        else:
                            target_msec = video_time * 1000.0
                            frame, video_last_msec, video_last_frame = self._video_frame_at(
                                video_cap, video_last_msec, video_last_frame, target_msec, (w_max, h_max))
                            if frame is None:
                                frame = green.copy()
                    else:
                        frame = green.copy()

                    for ww in self.widgets:
                        ww.render_to_frame(frame, dl, t)
                    writer.write(frame)
                    done += 1
                    self.progress.emit(int(done / total_frames * 100))

            writer.release()
            writer = None
            if video_cap is not None:
                video_cap.release()
                video_cap = None

            audio_note = ""
            if self.video_path:
                audio_note = self._mux_audio(self.out_path)

            sync_path = ""
            if self.write_sync_file:
                sync_path = os.path.splitext(self.out_path)[0] + "_sync.txt"
                with open(sync_path, "w") as f:
                    f.write("Lap sync points — frame numbers and timestamps in the exported video\n\n")
                    for lap_number, frame_idx, time_sec in sync_entries:
                        f.write(f"Lap {lap_number}: frame {frame_idx}, time {_format_lap_clock(time_sec)}\n")

            self.finished.emit(self.out_path, sync_path, audio_note)
        except Exception as e:
            if writer is not None:
                writer.release()
            if video_cap is not None:
                video_cap.release()
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Lap tick bar (clickable lap-start markers under the scrubber)
# ---------------------------------------------------------------------------

class LapTickBar(QWidget):
    """Thin strip under the scrubber with a clickable tick at the start of each lap."""

    lap_start_clicked = pyqtSignal(float)  # emits the time_sec to jump to

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(14)
        self.setMouseTracking(True)
        self._lap_starts: list[float] = []
        self._t0 = 0.0
        self._duration = 0.0
        self._hover_idx = -1

    def set_markers(self, lap_starts: list[float], t0: float, duration: float):
        self._lap_starts = lap_starts
        self._t0 = t0
        self._duration = duration
        self.update()

    def _tick_x(self, t: float) -> int:
        if self._duration <= 0:
            return 0
        frac = (t - self._t0) / self._duration
        return int(frac * self.width())

    def _nearest_tick(self, mouse_x: int) -> int:
        best_idx, best_dist = -1, 1e9
        for i, t in enumerate(self._lap_starts):
            dist = abs(self._tick_x(t) - mouse_x)
            if dist < best_dist:
                best_dist, best_idx = dist, i
        return best_idx if best_dist <= 6 else -1

    def mousePressEvent(self, event):
        idx = self._nearest_tick(int(event.position().x()))
        if idx >= 0:
            self.lap_start_clicked.emit(self._lap_starts[idx])

    def mouseMoveEvent(self, event):
        idx = self._nearest_tick(int(event.position().x()))
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.setCursor(Qt.CursorShape.PointingHandCursor if idx >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for i, t in enumerate(self._lap_starts):
            x = self._tick_x(t)
            hovered = (i == self._hover_idx)
            color = QColor(255, 220, 0) if hovered else QColor(150, 150, 150)
            pen = QPen(color)
            pen.setWidth(2 if hovered else 1)
            painter.setPen(pen)
            painter.drawLine(x, 0, x, self.height())
        painter.end()


# ---------------------------------------------------------------------------
# Lap selection dialog (for export)
# ---------------------------------------------------------------------------

class LapSelectDialog(QDialog):
    """Lets the user pick which laps to include in the exported video."""

    def __init__(self, lap_ranges: list[tuple[int, float, float]], parent=None,
                 preselected: set[int] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Select Laps")
        self._checkboxes: list[QCheckBox] = []
        preselected = preselected or set()

        layout = QVBoxLayout(self)

        btn_row = QHBoxLayout()
        btn_all = QPushButton("Select All")
        btn_none = QPushButton("Select None")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        btn_row.addWidget(btn_all)
        btn_row.addWidget(btn_none)
        layout.addLayout(btn_row)

        list_widget = QListWidget()
        for lap_num, start, end in lap_ranges:
            item = QListWidgetItem(list_widget)
            chk = QCheckBox(f"Lap {lap_num}  ({_format_lap_clock(end - start)})")
            chk.setChecked(lap_num in preselected)
            self._checkboxes.append(chk)
            list_widget.setItemWidget(item, chk)
        layout.addWidget(list_widget)

        self.chk_sync_file = QCheckBox("Generate sync .txt file (frame/timestamp at each lap start)")
        layout.addWidget(self.chk_sync_file)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_all(self, checked: bool):
        for chk in self._checkboxes:
            chk.setChecked(checked)

    def sync_file_enabled(self) -> bool:
        return self.chk_sync_file.isChecked()

    def selected_indices(self) -> list[int]:
        return [i for i, chk in enumerate(self._checkboxes) if chk.isChecked()]


# ---------------------------------------------------------------------------
# Data Wizard — pairs AiM sessions with video files (see find_video_log_matches)
# ---------------------------------------------------------------------------

class MatchThread(QThread):
    progress = Signal(int)
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, aim_folder: str, video_folder: str):
        super().__init__()
        self.aim_folder = aim_folder
        self.video_folder = video_folder

    def run(self):
        try:
            results = find_video_log_matches(
                self.aim_folder, self.video_folder, progress_cb=self.progress.emit)
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))


class DataWizardDialog(QDialog):
    """Point this at where AiM data and video live, and it pairs each
    session with its best-matching video (see find_video_log_matches for the
    confidence methodology — gyro cross-correlation when possible, EXIF
    creation_time as a much weaker fallback)."""

    load_requested = pyqtSignal(str, str, float)  # (xrk_path, video_path, offset_sec)
    matches_cached = pyqtSignal(str, str, list)  # (aim_folder, video_folder, results)

    def __init__(self, parent=None, cache: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Data Wizard")
        self.resize(820, 420)
        self.settings = QSettings("WHRRAH", "WHRRAH")
        self._results: list[dict] = []
        self._match_thread: MatchThread | None = None
        self._cache = cache
        self._build_ui()
        self._load_saved_paths()
        self._maybe_restore_cache()

    def _maybe_restore_cache(self):
        """Reuse the previous run's results if the folders haven't changed,
        so reopening the wizard doesn't mean re-parsing gyro telemetry for
        every session/video pair again."""
        if (self._cache
                and self._cache.get("aim_folder") == self.txt_aim_folder.text()
                and self._cache.get("video_folder") == self.txt_video_folder.text()):
            self._on_matches_found(self._cache["results"], from_cache=True)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        folder_form = QFormLayout()
        self.txt_aim_folder = QLineEdit()
        self.txt_aim_folder.setReadOnly(True)
        btn_aim = QPushButton("Browse…")
        btn_aim.clicked.connect(self._browse_aim_folder)
        aim_row = QHBoxLayout()
        aim_row.addWidget(self.txt_aim_folder, 1)
        aim_row.addWidget(btn_aim)
        folder_form.addRow("AiM Data Folder:", aim_row)

        self.txt_video_folder = QLineEdit()
        self.txt_video_folder.setReadOnly(True)
        btn_video = QPushButton("Browse…")
        btn_video.clicked.connect(self._browse_video_folder)
        video_row = QHBoxLayout()
        video_row.addWidget(self.txt_video_folder, 1)
        video_row.addWidget(btn_video)
        folder_form.addRow("Video Folder:", video_row)
        layout.addLayout(folder_form)

        btn_row = QHBoxLayout()
        self.btn_find_matches = QPushButton("Find Matches")
        self.btn_find_matches.clicked.connect(self._find_matches)
        btn_row.addWidget(self.btn_find_matches)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["AiM Session", "Video", "Laps", "Best Lap", "Confidence", ""])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in (2, 3, 4, 5):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        self.lbl_status = QLabel("")
        layout.addWidget(self.lbl_status)

    def _load_saved_paths(self):
        aim_path = self.settings.value("data_wizard/aim_folder", "")
        video_path = self.settings.value("data_wizard/video_folder", "")
        if aim_path:
            self.txt_aim_folder.setText(aim_path)
        if video_path:
            self.txt_video_folder.setText(video_path)

    def _browse_aim_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select AiM Data Folder", self.txt_aim_folder.text())
        if path:
            self.txt_aim_folder.setText(path)
            self.settings.setValue("data_wizard/aim_folder", path)

    def _browse_video_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select Video Folder", self.txt_video_folder.text())
        if path:
            self.txt_video_folder.setText(path)
            self.settings.setValue("data_wizard/video_folder", path)

    def _find_matches(self):
        aim_folder = self.txt_aim_folder.text()
        video_folder = self.txt_video_folder.text()
        if not aim_folder or not video_folder:
            QMessageBox.warning(self, "Missing Folders", "Select both an AiM data folder and a video folder first.")
            return
        if not os.path.isdir(aim_folder) or not os.path.isdir(video_folder):
            QMessageBox.warning(self, "Folder Not Found", "One of the selected folders doesn't exist anymore.")
            return

        self.btn_find_matches.setEnabled(False)
        self.table.setRowCount(0)
        self.lbl_status.setText("Matching sessions to video… this can take a while (parsing gyro telemetry).")

        self._match_thread = MatchThread(aim_folder, video_folder)
        self._match_thread.progress.connect(lambda p: self.lbl_status.setText(f"Matching… {p}%"))
        self._match_thread.finished.connect(self._on_matches_found)
        self._match_thread.error.connect(self._on_match_error)
        self._match_thread.start()

    def _on_match_error(self, message: str):
        self.btn_find_matches.setEnabled(True)
        self.lbl_status.setText("")
        QMessageBox.critical(self, "Matching Failed", message)

    def _on_matches_found(self, results: list[dict], from_cache: bool = False):
        self.btn_find_matches.setEnabled(True)
        self._results = results
        self.table.setRowCount(len(results))
        for row, r in enumerate(results):
            session_name = Path(r["xrk_path"]).stem
            video_name = Path(r["video_path"]).stem if r["video_path"] else "(no match found)"
            confidence_pct = round(r["confidence"] * 100)
            laps_text = str(r.get("completed_laps", "")) if r.get("completed_laps") is not None else ""
            if r.get("best_lap_num") is not None:
                best_lap_text = f"Lap {r['best_lap_num']} ({_format_lap_clock(r['best_lap_time'])})"
            else:
                best_lap_text = "—"

            self.table.setItem(row, 0, QTableWidgetItem(session_name))
            self.table.setItem(row, 1, QTableWidgetItem(video_name))
            self.table.setItem(row, 2, QTableWidgetItem(laps_text))
            self.table.setItem(row, 3, QTableWidgetItem(best_lap_text))
            self.table.setItem(row, 4, QTableWidgetItem(f"{confidence_pct}%"))

            btn_load = QPushButton("Load")
            btn_load.setEnabled(r["video_path"] is not None)
            btn_load.clicked.connect(lambda checked, rr=r: self._on_load_clicked(rr))
            self.table.setCellWidget(row, 5, btn_load)

        suffix = " (cached)" if from_cache else ""
        self.lbl_status.setText(f"Found {len(results)} session(s).{suffix}")
        if not from_cache:
            self.matches_cached.emit(self.txt_aim_folder.text(), self.txt_video_folder.text(), results)

    def _on_load_clicked(self, result: dict):
        self.load_requested.emit(result["xrk_path"], result["video_path"], result["offset_sec"])
        self.accept()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("We Have RaceRender At Home")
        self.resize(1400, 800)
        self.data_log = DataLog()
        self.lap_starts: list[float] = []
        self._lap_selection_indices: set[int] = set()
        self._lap_pad_start = 0.0
        self._lap_pad_end = 0.0
        self._write_sync_file = False
        self._lap_windows_for_export: list[tuple[float, float, list[tuple[int, float]]]] | None = None
        self._preview_windows: list[tuple[float, float]] = []  # restricted scrub/preview range; empty = full log
        self._preview_progress_sec = 0.0
        self._source_video_path: str | None = None
        self._wizard_cache: dict | None = None  # {"aim_folder", "video_folder", "results"}
        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.7)
        self.media_player.setAudioOutput(self.audio_output)
        self._build_ui()
        self._build_menu()
        self.statusBar().showMessage("Load a data log to get started.")

        default_layout_path = Path(__file__).parent / "default_layout.json"
        if default_layout_path.exists():
            self.load_layout_file(str(default_layout_path))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            focus = QApplication.focusWidget()
            # Don't hijack space while the user is typing/editing a control.
            if not isinstance(focus, (QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox)):
                self._on_play_clicked()
                event.accept()
                return
        super().keyPressEvent(event)

    def _build_menu(self):
        mb = self.menuBar()
        file_menu = mb.addMenu("File")

        act_open = QAction("Open Data Log (.csv/.xrk)…", self)
        act_open.setShortcut("Ctrl+O")
        act_open.triggered.connect(self.open_log)
        file_menu.addAction(act_open)

        act_save = QAction("Save Layout…", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self.save_layout)
        file_menu.addAction(act_save)

        act_load = QAction("Load Layout…", self)
        act_load.triggered.connect(self.load_layout)
        file_menu.addAction(act_load)

        file_menu.addSeparator()
        act_quit = QAction("Quit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        export_menu = mb.addMenu("Export")
        act_export = QAction("Export Video…", self)
        act_export.setShortcut("Ctrl+E")
        act_export.triggered.connect(self.export_video)
        export_menu.addAction(act_export)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # --- Left sidebar ---
        left = QWidget(); left.setFixedWidth(200)
        left_layout = QVBoxLayout(left)

        self.btn_data_wizard = QPushButton("📋  Data Wizard…")
        self.btn_data_wizard.clicked.connect(self.open_data_wizard)
        left_layout.addWidget(self.btn_data_wizard)

        grp_log = QGroupBox("Data")
        ll = QVBoxLayout(grp_log)
        self.btn_open_log = QPushButton("Open CSV…")
        self.btn_open_log.clicked.connect(self.open_log)
        self.btn_open_xrk = QPushButton("Open XRK…")
        self.btn_open_xrk.clicked.connect(self.open_xrk)
        self.lbl_log = QLabel("No log loaded")
        self.lbl_log.setWordWrap(False)
        self.btn_select_laps = QPushButton("Select Laps…")
        self.btn_select_laps.clicked.connect(self.open_lap_select)
        self.lbl_lap_selection = QLabel("No laps selected")
        self.lbl_lap_selection.setWordWrap(True)
        pad_row = QFormLayout()
        self.spn_pad_start = QDoubleSpinBox()
        self.spn_pad_start.setRange(0.0, 120.0)
        self.spn_pad_start.setSingleStep(1.0)
        self.spn_pad_start.setSuffix(" s")
        self.spn_pad_start.valueChanged.connect(self._on_pad_changed)
        self.spn_pad_end = QDoubleSpinBox()
        self.spn_pad_end.setRange(0.0, 120.0)
        self.spn_pad_end.setSingleStep(1.0)
        self.spn_pad_end.setSuffix(" s")
        self.spn_pad_end.valueChanged.connect(self._on_pad_changed)
        pad_row.addRow("Pad Start:", self.spn_pad_start)
        pad_row.addRow("Pad End:", self.spn_pad_end)
        ll.addWidget(self.btn_open_log)
        ll.addWidget(self.btn_open_xrk)
        ll.addWidget(self.lbl_log)
        ll.addWidget(self.btn_select_laps)
        ll.addWidget(self.lbl_lap_selection)
        ll.addLayout(pad_row)

        grp_video = QGroupBox("Video")
        vl = QVBoxLayout(grp_video)
        self.btn_open_video = QPushButton("Open Video…")
        self.btn_open_video.clicked.connect(self.open_video)
        self.lbl_video = QLabel("No video loaded")
        self.lbl_video.setWordWrap(True)
        self.btn_make_proxy = QPushButton("Make Low-Lag Preview…")
        self.btn_make_proxy.setEnabled(False)
        self.btn_make_proxy.clicked.connect(self.make_proxy_video)
        offset_row = QFormLayout()
        self.spn_video_offset = QDoubleSpinBox()
        self.spn_video_offset.setRange(-3600.0, 3600.0)
        self.spn_video_offset.setDecimals(2)
        self.spn_video_offset.setSingleStep(0.1)
        self.spn_video_offset.setSuffix(" s")
        self.spn_video_offset.valueChanged.connect(self._on_video_offset_changed)
        offset_row.addRow("Offset:", self.spn_video_offset)
        vl.addWidget(self.btn_open_video)
        vl.addWidget(self.lbl_video)
        vl.addWidget(self.btn_make_proxy)
        vl.addLayout(offset_row)

        self.btn_export = QPushButton("🎬  Export Video…")
        self.btn_export.clicked.connect(self.export_video)
        vl.addWidget(self.btn_export)

        grp_layout = QGroupBox("Layout")
        lyl = QVBoxLayout(grp_layout)
        self.btn_save_layout = QPushButton("Save Layout…")
        self.btn_save_layout.clicked.connect(self.save_layout)
        self.btn_load_layout = QPushButton("Load Layout…")
        self.btn_load_layout.clicked.connect(self.load_layout)
        lyl.addWidget(self.btn_save_layout)
        lyl.addWidget(self.btn_load_layout)

        grp_widgets = QGroupBox("Add Widget")
        wl = QVBoxLayout(grp_widgets)
        for wtype in WIDGET_TYPES:
            btn = QPushButton(wtype)
            btn.clicked.connect(lambda checked, t=wtype: self.canvas.add_widget(t))
            wl.addWidget(btn)

        self.btn_delete = QPushButton("Delete Selected")
        self.btn_delete.clicked.connect(self.canvas_delete)
        self.btn_delete.setEnabled(False)

        grp_res = QGroupBox("Overlay Resolution")
        rl = QFormLayout(grp_res)
        self.spn_res_w = QSpinBox(); self.spn_res_w.setRange(640, 7680); self.spn_res_w.setValue(1920)
        self.spn_res_h = QSpinBox(); self.spn_res_h.setRange(360, 4320); self.spn_res_h.setValue(1080)
        self.spn_res_w.valueChanged.connect(self._on_res_change)
        self.spn_res_h.valueChanged.connect(self._on_res_change)
        rl.addRow("W:", self.spn_res_w)
        rl.addRow("H:", self.spn_res_h)

        grp_fps = QGroupBox("Export FPS")
        fl = QFormLayout(grp_fps)
        self.spn_fps = QDoubleSpinBox()
        self.spn_fps.setRange(1.0, 120.0)
        self.spn_fps.setDecimals(2)
        self.spn_fps.setValue(30.0)
        fl.addRow("FPS:", self.spn_fps)

        left_layout.addWidget(grp_log)
        left_layout.addWidget(grp_video)
        left_layout.addWidget(grp_layout)
        left_layout.addWidget(grp_widgets)
        left_layout.addWidget(self.btn_delete)
        left_layout.addWidget(grp_res)
        left_layout.addWidget(grp_fps)
        left_layout.addStretch()

        # --- Canvas ---
        self.canvas = OverlayCanvas()
        self.canvas.selection_changed.connect(self._on_selection)

        # --- Right sidebar: properties ---
        self.props = PropertiesPanel([])
        self.props.setFixedWidth(220)
        self.props.changed.connect(self.canvas.update)

        # --- Preview scrubber ---
        canvas_col = QWidget()
        ccl = QVBoxLayout(canvas_col)
        ccl.addWidget(self.canvas, 1)
        scrub_row = QHBoxLayout()
        self.btn_play = QPushButton("▶")
        self.btn_play.setFixedWidth(36)
        self.btn_play.clicked.connect(self._on_play_clicked)
        self.cmb_speed = QComboBox()
        self.cmb_speed.addItems(["1x", "2x", "4x"])
        self.cmb_speed.currentTextChanged.connect(self._on_speed_changed)
        self.lbl_time = QLabel("0.00 s")
        self.scrubber = QSlider(Qt.Orientation.Horizontal)
        self.scrubber.setRange(0, 1000)
        self.scrubber.valueChanged.connect(self._on_scrub)
        self.lap_ticks = LapTickBar()
        self.lap_ticks.lap_start_clicked.connect(self._on_lap_tick_clicked)
        scrub_col = QVBoxLayout()
        scrub_col.setSpacing(0)
        scrub_col.addWidget(self.scrubber)
        scrub_col.addWidget(self.lap_ticks)
        scrub_row.addWidget(self.btn_play)
        scrub_row.addWidget(self.cmb_speed)
        scrub_row.addWidget(QLabel("Preview:"))
        scrub_row.addLayout(scrub_col, 1)
        scrub_row.addWidget(self.lbl_time)
        scrub_row.addWidget(QLabel("🔊"))
        self.sld_volume = QSlider(Qt.Orientation.Horizontal)
        self.sld_volume.setFixedWidth(80)
        self.sld_volume.setRange(0, 100)
        self.sld_volume.setValue(70)
        self.sld_volume.valueChanged.connect(self._on_volume_changed)
        scrub_row.addWidget(self.sld_volume)
        ccl.addLayout(scrub_row)

        self.playback_speed = 1.0
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(33)  # ~30 ticks/sec
        self._play_timer.timeout.connect(self._on_play_tick)

        root.addWidget(left)
        root.addWidget(canvas_col, 1)
        root.addWidget(self.props)

    def _on_res_change(self):
        self.canvas.set_resolution(self.spn_res_w.value(), self.spn_res_h.value())

    def _on_selection(self, w):
        self.props.load_widget(w)
        self.btn_delete.setEnabled(w is not None)

    def _preview_windows_or_full(self) -> list[tuple[float, float]]:
        """The (start, end) ranges the scrubber/preview should move through —
        the lap-selected windows if any, otherwise the whole log."""
        if self._preview_windows:
            return self._preview_windows
        if self.data_log.timestamps:
            return [(self.data_log.timestamps[0], self.data_log.timestamps[-1])]
        return []

    def _preview_total_duration(self) -> float:
        return sum(e - s for s, e in self._preview_windows_or_full())

    def _progress_seconds_to_time(self, progress_sec: float) -> float:
        """Maps seconds-along-the-concatenated-preview-timeline to an absolute
        data-log time, skipping over any gaps between non-adjacent lap windows."""
        windows = self._preview_windows_or_full()
        if not windows:
            return self.data_log.timestamps[0] if self.data_log.timestamps else 0.0
        acc = 0.0
        for s, e in windows:
            seg = e - s
            if progress_sec <= acc + seg or seg <= 0:
                return s + (progress_sec - acc)
            acc += seg
        return windows[-1][1]

    def _time_to_progress_seconds(self, t: float) -> float:
        """Inverse of _progress_seconds_to_time, for placing lap ticks and
        for resuming playback from wherever the scrubber currently sits."""
        windows = self._preview_windows_or_full()
        if not windows:
            return 0.0
        acc = 0.0
        for s, e in windows:
            if s <= t <= e:
                return acc + (t - s)
            acc += e - s
        return 0.0 if t < windows[0][0] else acc

    def _refresh_lap_ticks(self):
        total = self._preview_total_duration()
        windows = self._preview_windows_or_full()
        ticks = [self._time_to_progress_seconds(s) for s in self.lap_starts
                 if any(ws <= s <= we for ws, we in windows)]
        self.lap_ticks.set_markers(ticks, 0.0, total)

    def _on_scrub(self, val):
        total = self._preview_total_duration()
        if total > 0:
            self._preview_progress_sec = val / 1000.0 * total
            t = self._progress_seconds_to_time(self._preview_progress_sec)
            self.canvas.set_preview_time(t, self._preview_progress_sec)
            self.lbl_time.setText(f"{self._preview_progress_sec:.2f} s")
            self._sync_audio_position(t)

    def _on_lap_tick_clicked(self, progress_sec: float):
        total = self._preview_total_duration()
        if total > 0:
            val = int(progress_sec / total * 1000)
            self.scrubber.setValue(max(0, min(1000, val)))

    def _on_speed_changed(self, text):
        self.playback_speed = float(text.rstrip("x"))
        if self._play_timer.isActive():
            self.media_player.setPlaybackRate(self.playback_speed)

    def _sync_audio_position(self, absolute_t: float):
        if self.media_player.source().isEmpty():
            return
        self.media_player.setPosition(int(self.canvas.video_time_for(absolute_t) * 1000))

    def _on_play_clicked(self):
        if self._play_timer.isActive():
            self._play_timer.stop()
            self.btn_play.setText("▶")
            self.media_player.pause()
        else:
            total = self._preview_total_duration()
            if total <= 0:
                return
            # Seed the float progress tracker from wherever the slider currently
            # is, since the slider's integer resolution is too coarse to advance
            # by for long logs (a single unit can be many real-time seconds).
            self._preview_progress_sec = self.scrubber.value() / 1000.0 * total
            self._play_timer.start()
            self.btn_play.setText("⏸")
            if not self.media_player.source().isEmpty():
                self.media_player.setPlaybackRate(self.playback_speed)
                self._sync_audio_position(self._progress_seconds_to_time(self._preview_progress_sec))
                self.media_player.play()

    def _on_play_tick(self):
        total = self._preview_total_duration()
        if total <= 0:
            self._play_timer.stop()
            self.btn_play.setText("▶")
            return
        step = self._play_timer.interval() / 1000.0 * self.playback_speed
        self._preview_progress_sec = min(self._preview_progress_sec + step, total)
        t = self._progress_seconds_to_time(self._preview_progress_sec)

        val = int(self._preview_progress_sec / total * 1000)
        self.scrubber.blockSignals(True)
        self.scrubber.setValue(val)
        self.scrubber.blockSignals(False)
        self.canvas.set_preview_time(t, self._preview_progress_sec)
        self.lbl_time.setText(f"{self._preview_progress_sec:.2f} s")

        # The preview clock and the media player's internal clock drift apart
        # over time (they're independent timers) — nudge the audio back in
        # line once it's noticeably off rather than resyncing every tick.
        if not self.media_player.source().isEmpty():
            expected_ms = self.canvas.video_time_for(t) * 1000
            if abs(self.media_player.position() - expected_ms) > 200:
                self.media_player.setPosition(int(expected_ms))

        if self._preview_progress_sec >= total:
            self._play_timer.stop()
            self.btn_play.setText("▶")
            self.media_player.pause()
            self.media_player.pause()

    def canvas_delete(self):
        self.canvas.delete_selected()

    def open_log(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Data Log", "", "AiM Data Logs (*.csv *.xrk);;CSV Files (*.csv);;XRK Files (*.xrk);;All Files (*)")
        if not path:
            return
        self.load_log_file(path)

    def open_data_wizard(self):
        dialog = DataWizardDialog(self, cache=self._wizard_cache)
        dialog.load_requested.connect(self._on_wizard_load_requested)
        dialog.matches_cached.connect(self._on_wizard_matches_cached)
        dialog.exec()

    def _on_wizard_matches_cached(self, aim_folder: str, video_folder: str, results: list[dict]):
        self._wizard_cache = {"aim_folder": aim_folder, "video_folder": video_folder, "results": results}

    def _on_wizard_load_requested(self, xrk_path: str, video_path: str, offset_sec: float):
        self.load_log_file(xrk_path)
        self._load_video(video_path, is_proxy=False)

        # offset_sec is the video's t=0 expressed as an absolute session
        # time — set it directly; it stays correct no matter which lap is
        # selected afterward (see set_video_offset's docstring).
        self.spn_video_offset.setValue(offset_sec)

        # The wizard matched a specific video to this session, but the video
        # is very likely shorter than the full log — clamp the preview/
        # scrubber to just the range the video actually covers, instead of
        # defaulting to the whole (mostly video-less) session.
        video_dur = video_duration_sec(video_path)
        if video_dur is not None and self.data_log.timestamps:
            t0, t_end = self.data_log.timestamps[0], self.data_log.timestamps[-1]
            full_window = [(t0, t_end, [(0, t0)])]
            clamp_offset = offset_sec - t0
            clamped, _trimmed = self._clamp_windows_to_video(full_window, clamp_offset, video_dur)
            if clamped:
                self._preview_windows = [(s, e) for s, e, _ in clamped]
                self._refresh_lap_ticks()
                self.scrubber.blockSignals(True)
                self.scrubber.setValue(0)
                self.scrubber.blockSignals(False)
                self._on_scrub(0)

        self.statusBar().showMessage(
            f"Loaded {Path(xrk_path).name} + {Path(video_path).name} (offset {offset_sec:.1f}s from Data Wizard)")

    def open_xrk(self):
        if not find_xrk_dll():
            QMessageBox.critical(
                self, "XRK Support Unavailable",
                f"{_XRK_DLL_NAME} wasn't found in {_XRK_DLL_DIR}.\n\n"
                "XRK reading needs AiM's MatLabXRK DLL bundled alongside the app."
            )
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open AiM XRK", "", "XRK Files (*.xrk);;All Files (*)")
        if not path:
            return
        self.load_log_file(path)

    def load_log_file(self, path: str):
        try:
            is_xrk = Path(path).suffix.lower() == ".xrk"
            channels = self.data_log.load_xrk(path) if is_xrk else self.data_log.load(path)
            self.canvas.set_data_log(self.data_log)
            self.props.set_channels(channels)
            self.props.set_data_log(self.data_log)

            t0 = self.data_log.timestamps[0] if self.data_log.timestamps else 0.0
            t_end = self.data_log.timestamps[-1] if self.data_log.timestamps else 0.0
            self.lap_starts = [t0] + [m for m in self.data_log.beacon_markers if m < t_end]

            # Completed laps only — the segment still in progress at t_end (if
            # any) doesn't count toward the lap total or the best-lap search.
            total_laps = self.data_log.lap_number_at(t_end) if self.data_log.timestamps else 0
            lap_durations = [(n, d) for n in range(total_laps)
                              if (d := self.data_log.lap_duration(n)) is not None]
            if lap_durations:
                best_lap_num, best_time = min(lap_durations, key=lambda x: x[1])
                best_str = f"Best: Lap {best_lap_num} ({_format_lap_clock(best_time)})"
            else:
                best_str = "Best: —"

            elided_name = self.lbl_log.fontMetrics().elidedText(
                Path(path).name, Qt.TextElideMode.ElideRight, 170)
            self.lbl_log.setText(
                f"{elided_name}\n"
                f"{len(channels)} channels\n"
                f"Length: {_format_lap_clock(self.data_log.duration)}\n"
                f"Laps: {total_laps}\n"
                f"{best_str}"
            )

            # A new log invalidates any previous lap selection — default back
            # to no laps selected / full-range preview until the user picks again.
            self._lap_selection_indices = set()
            self._lap_windows_for_export = None
            self._preview_windows = []
            self.lbl_lap_selection.setText("No laps selected")
            self._refresh_lap_ticks()
            self.scrubber.blockSignals(True)
            self.scrubber.setValue(0)
            self.scrubber.blockSignals(False)
            self._on_scrub(0)

            self.statusBar().showMessage(f"Loaded {len(channels)} channels, {self.data_log.duration:.1f}s duration")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))

    def _lap_ranges(self) -> list[tuple[int, float, float]]:
        t_end = self.data_log.timestamps[-1] if self.data_log.timestamps else 0.0
        return [
            (i, start, self.lap_starts[i + 1] if i + 1 < len(self.lap_starts) else t_end)
            for i, start in enumerate(self.lap_starts)
        ]

    def open_lap_select(self):
        if not self.lap_starts or self.data_log.duration <= 0:
            QMessageBox.information(self, "No Laps", "Load a data log with lap markers first.")
            return
        lap_ranges = self._lap_ranges()
        dialog = LapSelectDialog(lap_ranges, self, preselected=self._lap_selection_indices)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selected_indices()
        if not selected:
            QMessageBox.warning(self, "No Laps Selected",
                                 "Select at least one lap, or Cancel to leave the current selection unchanged.")
            return
        self._apply_lap_selection(lap_ranges, selected, self.spn_pad_start.value(), self.spn_pad_end.value(),
                                   dialog.sync_file_enabled())

    def _on_pad_changed(self):
        # Live-reapply if a selection is already active, the same way the
        # video offset re-renders the preview without reopening a dialog.
        if not self._lap_selection_indices:
            return
        self._apply_lap_selection(self._lap_ranges(), sorted(self._lap_selection_indices),
                                   self.spn_pad_start.value(), self.spn_pad_end.value(), self._write_sync_file)

    def _video_duration_sec(self) -> float | None:
        """Reads the original source video's duration (never the low-lag
        proxy — it's the same length, but this is the authoritative file
        export actually uses)."""
        if not self._source_video_path:
            return None
        return video_duration_sec(self._source_video_path)

    @staticmethod
    def _clamp_windows_to_video(lap_windows, video_offset: float, video_duration: float):
        """Trims (start, end, markers) windows so the video lookup they imply
        — video_time = progress - offset — never asks for footage outside
        [0, video_duration]. Without this, a lap selection padded past the
        end of the recording silently truncates the exported video to match
        whatever audio ffmpeg could find instead of erroring or warning."""
        progress_min = max(0.0, video_offset)
        progress_max = video_duration + video_offset

        clamped = []
        progress = 0.0
        trimmed = 0.0
        for start, end, markers in lap_windows:
            win_progress_start = progress
            win_progress_end = progress + (end - start)
            progress = win_progress_end

            clipped_start = max(win_progress_start, progress_min)
            clipped_end = min(win_progress_end, progress_max)
            if clipped_end <= clipped_start:
                trimmed += end - start
                continue
            new_start = start + (clipped_start - win_progress_start)
            new_end = start + (clipped_end - win_progress_start)
            trimmed += (end - start) - (new_end - new_start)
            kept_markers = [(n, t) for n, t in markers if new_start <= t <= new_end]
            clamped.append((new_start, new_end, kept_markers))
        return clamped, trimmed

    def _apply_lap_selection(self, lap_ranges, selected: list[int], pad_start: float, pad_end: float,
                              write_sync_file: bool):
        self._lap_selection_indices = set(selected)
        self._lap_pad_start = pad_start
        self._lap_pad_end = pad_end
        self._write_sync_file = write_sync_file

        t0 = self.data_log.timestamps[0]
        t_end = self.data_log.timestamps[-1]

        # Group contiguous selected laps (e.g. 2 and 3) into one window so
        # padding only lands before the first and after the last lap of a
        # run, not in the gap between adjacent laps.
        ordered = sorted(selected)
        groups = [[ordered[0]]]
        for idx in ordered[1:]:
            if idx == groups[-1][-1] + 1:
                groups[-1].append(idx)
            else:
                groups.append([idx])

        lap_windows = []
        for group in groups:
            start = max(t0, lap_ranges[group[0]][1] - pad_start)
            end = min(t_end, lap_ranges[group[-1]][2] + pad_end)
            lap_windows.append((start, end, [(lap_ranges[i][0], lap_ranges[i][1]) for i in group]))

        video_duration = self._video_duration_sec()
        if video_duration is not None:
            # video_offset is the video's t=0 expressed as an absolute
            # session time and stays constant regardless of selection;
            # _clamp_windows_to_video works in progress-space relative to
            # this window's own start, so convert just for that call.
            clamp_offset = self.spn_video_offset.value() - lap_windows[0][0]
            lap_windows, trimmed = self._clamp_windows_to_video(lap_windows, clamp_offset, video_duration)
            if trimmed > 0.05:
                QMessageBox.warning(
                    self, "Selection Extends Past Video",
                    f"The selected laps (with padding) cover {trimmed:.1f}s more than the loaded "
                    "video has footage for, given the current offset. That portion has been trimmed "
                    "from the selection rather than exported with no audio/video."
                )
            if not lap_windows:
                QMessageBox.warning(self, "No Footage In Range",
                                     "None of the selected laps overlap the loaded video at the current offset.")
                return

        self._preview_windows = [(s, e) for s, e, _ in lap_windows]
        self._lap_windows_for_export = lap_windows
        self._refresh_lap_ticks()

        lap_nums = ", ".join(str(lap_ranges[i][0]) for i in ordered)
        self.lbl_lap_selection.setText(f"Laps: {lap_nums}")

        self.scrubber.blockSignals(True)
        self.scrubber.setValue(0)
        self.scrubber.blockSignals(False)
        self._on_scrub(0)

    def open_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Video", "", "Video Files (*.mp4 *.mov *.avi *.mkv);;All Files (*)")
        if not path:
            return
        self._load_video(path, is_proxy=False)

    def _load_video(self, path: str, is_proxy: bool):
        self.canvas.set_video(path)
        if self.canvas._video_cap is None:
            QMessageBox.critical(self, "Load Error", f"Could not open video:\n{path}")
            self.lbl_video.setText("No video loaded")
            self.btn_make_proxy.setEnabled(False)
            return
        if not is_proxy:
            self._source_video_path = path
            video_fps = self.canvas._video_cap.get(cv2.CAP_PROP_FPS)
            if video_fps > 0:
                self.spn_fps.setValue(video_fps)
        suffix = " (low-lag preview)" if is_proxy else ""
        self.lbl_video.setText(Path(path).name + suffix)
        self.canvas.set_video_offset(self.spn_video_offset.value())
        self.media_player.setSource(QUrl.fromLocalFile(path))
        self.btn_make_proxy.setEnabled(not is_proxy)

    def make_proxy_video(self):
        src = self._source_video_path
        if not src:
            return
        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path:
            QMessageBox.critical(
                self, "ffmpeg Not Found",
                "Couldn't find ffmpeg on this machine, so a low-lag preview can't be generated.\n\n"
                "Install ffmpeg and make sure it's on PATH, then try again."
            )
            return

        dst = str(Path(src).with_name(Path(src).stem + "_preview.mp4"))
        progress = QProgressDialog("Generating low-lag preview…", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setCancelButton(None)
        progress.show()

        self._proxy_thread = ProxyThread(ffmpeg_path, src, dst)
        self._proxy_thread.finished.connect(lambda dst_path: (
            progress.close(),
            self._load_video(dst_path, is_proxy=True),
            self.statusBar().showMessage(f"Low-lag preview ready: {dst_path}")
        ))
        self._proxy_thread.error.connect(lambda e: (
            progress.close(),
            QMessageBox.critical(self, "Proxy Generation Failed", e)
        ))
        self._proxy_thread.start()

    def _on_video_offset_changed(self, value: float):
        self.canvas.set_video_offset(value)

    def _on_volume_changed(self, value: int):
        self.audio_output.setVolume(value / 100.0)

    def save_layout(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Layout", "", "JSON (*.json)")
        if not path:
            return
        self.save_layout_file(path)

    def save_layout_file(self, path: str):
        data = {
            "resolution": [self.canvas.overlay_width, self.canvas.overlay_height],
            "widgets": [w.to_dict() for w in self.canvas.widgets],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        self.statusBar().showMessage(f"Layout saved to {path}")

    def load_layout(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Layout", "", "JSON (*.json)")
        if not path:
            return
        self.load_layout_file(path)

    def load_layout_file(self, path: str):
        try:
            with open(path) as f:
                data = json.load(f)
            # Older layout files are just a bare list of widgets with no
            # resolution recorded — fall back to whatever's currently set
            # rather than erroring on them.
            if isinstance(data, list):
                widget_dicts, resolution = data, None
            else:
                widget_dicts = data.get("widgets", [])
                resolution = data.get("resolution")

            self.canvas.widgets = [OverlayWidget.from_dict(d) for d in widget_dicts]
            self.canvas.selected = None
            if resolution:
                res_w, res_h = resolution
                self.spn_res_w.setValue(res_w)
                self.spn_res_h.setValue(res_h)
                self.canvas.set_resolution(res_w, res_h)
            self.canvas.update()
            self.props.load_widget(None)
            self.btn_delete.setEnabled(False)
            self.statusBar().showMessage(f"Layout loaded from {path}")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))

    def export_video(self):
        if not self.data_log.timestamps:
            QMessageBox.warning(self, "No Data", "Load a data log before exporting.")
            return
        if not self.canvas.widgets:
            QMessageBox.warning(self, "No Widgets", "Add at least one widget before exporting.")
            return

        if self.lap_starts and self._lap_windows_for_export is None:
            QMessageBox.warning(self, "No Laps Selected",
                                 "Use the \"Select Laps…\" button in the sidebar to choose which laps to export.")
            return

        path, _ = QFileDialog.getSaveFileName(self, "Export Video", "overlay.mp4", "MP4 Video (*.mp4)")
        if not path:
            return

        # The export reads frames from the compositing source while writing
        # the output — if they're the same file, the writer truncates it out
        # from under the reader mid-export, corrupting the result. Block
        # this rather than let it silently produce garbage.
        if self._source_video_path and os.path.abspath(path) == os.path.abspath(self._source_video_path):
            QMessageBox.warning(
                self, "Can't Overwrite Source Video",
                "The export destination is the same file as the currently loaded source video. "
                "Choose a different output filename."
            )
            return

        progress = QProgressDialog("Rendering overlay video…", "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()

        self._export_thread = ExportThread(
            self.canvas.widgets, self.data_log, self.spn_fps.value(), path,
            self._lap_windows_for_export, self._write_sync_file,
            video_path=self._source_video_path,
            video_offset=self.spn_video_offset.value(),
            output_size=(self.spn_res_w.value(), self.spn_res_h.value()),
        )
        progress.canceled.connect(self._export_thread.cancel)
        self._export_thread.progress.connect(progress.setValue)
        self._export_thread.finished.connect(lambda video_path, sync_path, audio_note: (
            progress.close(),
            QMessageBox.information(
                self, "Done",
                f"Exported to:\n{video_path}"
                + (f"\n\nSync file:\n{sync_path}" if sync_path else "")
                + (f"\n\nNote: {audio_note}" if audio_note else "")
            )
        ))
        self._export_thread.error.connect(lambda e: (
            progress.close(),
            QMessageBox.critical(self, "Export Error", e)
        ))
        self._export_thread.canceled.connect(lambda: (
            progress.close(),
            self.statusBar().showMessage("Export canceled.")
        ))
        self._export_thread.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="We Have RaceRender At Home")
    parser.add_argument("csv", nargs="?", help="AiM RS2 CSV log to auto-load on launch")
    parser.add_argument("--layout", help="Overlay layout JSON file to auto-load on launch")
    args, qt_args = parser.parse_known_args(sys.argv[1:])

    app = QApplication([sys.argv[0]] + qt_args)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    if args.csv:
        window.load_log_file(args.csv)
    if args.layout:
        window.load_layout_file(args.layout)
    sys.exit(app.exec())
