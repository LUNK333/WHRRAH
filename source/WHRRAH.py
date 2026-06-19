"""
We Have RaceRender At Home
A DIY data overlay tool for AiM Solo 2 DL data logs.
Renders a green-screen overlay video to composite in DaVinci Resolve.
"""

import sys
import os
import csv
import json
from pathlib import Path

import numpy as np
import cv2

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QListWidget, QListWidgetItem,
    QComboBox, QSlider, QSpinBox, QGroupBox, QSplitter, QScrollArea,
    QDoubleSpinBox, QCheckBox, QStatusBar, QToolBar, QFrame,
    QDialog, QDialogButtonBox, QFormLayout, QProgressDialog,
    QMessageBox, QColorDialog
)
from PyQt6.QtCore import Qt, QRect, QPoint, QSize, QTimer, pyqtSignal, QThread, pyqtSignal as Signal
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QFontMetrics, QPixmap, QImage,
    QAction, QCursor
)


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

    def channel_range(self, channel: str) -> tuple[float, float]:
        if channel not in self.channels:
            return (0.0, 1.0)
        vals = self.channels[channel]
        return (min(vals), max(vals))


# ---------------------------------------------------------------------------
# Widget definitions
# ---------------------------------------------------------------------------

WIDGET_TYPES = ["Lap Timer", "Numeric Display", "Bar Graph", "Track Map"]

# Unscaled (scale=1.0) size per widget type — actual w/h/font_size are this times .scale.
BASE_SIZES = {
    "Lap Timer": (220, 70),
    "Numeric Display": (200, 60),
    "Bar Graph": (200, 60),
    "Track Map": (220, 220),
}
BASE_FONT_SIZE = 24


class OverlayWidget:
    """Represents a single draggable widget on the overlay canvas.

    Size is driven entirely by `scale` (same factor on x and y) so resizing
    grows the box and its text/graphics together, instead of just the border.
    """

    def __init__(self, widget_type: str, x=100, y=100, scale=1.0):
        self.widget_type = widget_type
        self.x = x
        self.y = y
        self.scale = scale
        self.channel: str = ""
        self.label: str = widget_type
        self.min_val: float = 0.0
        self.max_val: float = 100.0
        self.color: tuple = (0, 255, 0)  # BGR for OpenCV
        self.selected: bool = False

    @property
    def base_size(self) -> tuple[int, int]:
        return BASE_SIZES.get(self.widget_type, (200, 60))

    @property
    def w(self) -> int:
        if self.widget_type == "Lap Timer":
            # "Time: 00:00:000" is the longest line the widget ever renders
            # (fixed-width clock format) — size the box to it, same font/scale/
            # thickness as the actual draw call, so width tracks text width
            # instead of growing arbitrarily with scale.
            cv_scale = self.font_size / 28.0
            (text_w, _), _ = cv2.getTextSize("Time: 00:00:000", cv2.FONT_HERSHEY_SIMPLEX, cv_scale, 2)
            padding = 16 + int(self.font_size * 0.3)
            return text_w + padding
        return max(1, int(self.base_size[0] * self.scale))

    @property
    def h(self) -> int:
        if self.widget_type == "Lap Timer":
            # Box height tracks actual text height (which grows sub-linearly via
            # _lap_timer_line_gap), not the scale factor directly, so the box
            # doesn't end up with a lot of empty space below the two lines.
            padding = 8 + int(self.font_size * 0.35)
            return _lap_timer_line_gap(self.font_size) * 2 + padding
        return max(1, int(self.base_size[1] * self.scale))

    @property
    def font_size(self) -> int:
        return max(6, int(BASE_FONT_SIZE * self.scale))

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
        x, y, w, h = self.x, self.y, self.w, self.h
        color_bgr = self.color  # already BGR

        val = data_log.value_at(self.channel, time_sec) if self.channel else 0.0

        if self.widget_type == "Numeric Display":
            _draw_numeric(frame, x, y, w, h, self.label, val, self.font_size, color_bgr)

        elif self.widget_type == "Bar Graph":
            lo, hi = self.min_val, self.max_val
            pct = np.clip((val - lo) / (hi - lo) if hi != lo else 0, 0, 1)
            _draw_bar(frame, x, y, w, h, self.label, val, pct, color_bgr)

        elif self.widget_type == "Lap Timer":
            _draw_lap_timer(frame, x, y, w, h, data_log.lap_time_at(time_sec),
                             data_log.lap_number_at(time_sec), self.font_size, color_bgr)

        elif self.widget_type == "Track Map":
            if "GPS Latitude" in data_log.channels and "GPS Longitude" in data_log.channels:
                _draw_track_map(frame, x, y, w, h, data_log, time_sec)


# ---------------------------------------------------------------------------
# OpenCV drawing helpers
# ---------------------------------------------------------------------------

def _draw_numeric(frame, x, y, w, h, label, value, font_size, color):
    cv2.rectangle(frame, (x, y), (x + w, y + h), (20, 20, 20), -1)
    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
    scale = font_size / 30.0
    text = f"{label}: {value:.1f}"
    cv2.putText(frame, text, (x + 8, y + h - 12), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)


def _draw_bar(frame, x, y, w, h, label, value, pct, color):
    cv2.rectangle(frame, (x, y), (x + w, y + h), (20, 20, 20), -1)
    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
    bar_w = int((w - 4) * pct)
    if bar_w > 0:
        cv2.rectangle(frame, (x + 2, y + 2), (x + 2 + bar_w, y + h - 2), color, -1)
    cv2.putText(frame, f"{label}: {value:.1f}", (x + 4, y + h - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1, cv2.LINE_AA)


def _format_lap_clock(seconds: float) -> str:
    """mm:ss:ms, per 'lap timer format.pdf' (Time: XX:XX:XX)."""
    total_ms = round(seconds * 1000)
    mins, rem_ms = divmod(total_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{mins:02d}:{secs:02d}:{ms:03d}"


def _lap_timer_line_gap(font_size: int) -> int:
    """Vertical distance between the two lines. Grows sub-linearly with font_size
    (sqrt) so the lines don't spread far apart at high widget scale."""
    base_gap = BASE_FONT_SIZE * 1.3
    return int(base_gap * (font_size / BASE_FONT_SIZE) ** 0.5)


def _draw_lap_timer(frame, x, y, w, h, seconds, lap_number, font_size, color):
    """Two lines per 'lap timer format.pdf': 'Time: <bold XX:XX:XX>' / 'Lap: <bold X>'."""
    cv2.rectangle(frame, (x, y), (x + w, y + h), (20, 20, 20), -1)
    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
    scale = font_size / 28.0
    font = cv2.FONT_HERSHEY_SIMPLEX
    line_h = _lap_timer_line_gap(font_size)

    def draw_label_value(label, value, ty):
        cv2.putText(frame, label, (x + 8, ty), font, scale, color, 1, cv2.LINE_AA)
        (label_w, _), _ = cv2.getTextSize(label, font, scale, 1)
        cv2.putText(frame, value, (x + 8 + label_w, ty), font, scale, color, 2, cv2.LINE_AA)

    draw_label_value("Time: ", _format_lap_clock(seconds), y + line_h)
    draw_label_value("Lap: ", str(lap_number), y + line_h * 2 + 4)


def _draw_track_map(frame, x, y, w, h, data_log: DataLog, time_sec: float):
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
    cv2.rectangle(frame, (x, y), (x + w, y + h), (100, 100, 100), 2)

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
        self._data_log: DataLog | None = None
        self._preview_pixmap: QPixmap | None = None

    def set_resolution(self, w: int, h: int):
        self.overlay_width = w
        self.overlay_height = h
        self.update()

    def set_data_log(self, dl: DataLog):
        self._data_log = dl

    def set_preview_time(self, t: float):
        self._preview_time = t
        self.update()

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

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background: checkerboard to represent green screen
        s, ox, oy = self._scale()
        ow = self.overlay_width * s
        oh = self.overlay_height * s
        painter.fillRect(0, 0, self.width(), self.height(), QColor(30, 30, 30))
        painter.fillRect(int(ox), int(oy), int(ow), int(oh), QColor(0, 180, 0))

        # Draw each widget as a placeholder rectangle
        for w in self.widgets:
            tl = self._to_canvas(w.x, w.y)
            br = self._to_canvas(w.x + w.w, w.y + w.h)
            rect = QRect(tl, br)

            bg = QColor(20, 20, 20, 200)
            painter.fillRect(rect, bg)

            pen = QPen(QColor(*reversed(w.color)) if len(w.color) == 3 else QColor(0, 255, 0))
            pen.setWidth(2 if not w.selected else 3)
            if w.selected:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(rect)

            if w.widget_type == "Track Map" and self._data_log:
                layout = _track_map_layout(self._data_log, rect, self._preview_time)
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
                val = self._data_log.value_at(w.channel, self._preview_time) if (self._data_log and w.channel) else 0.0
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
                lap = self._data_log.lap_number_at(self._preview_time) if self._data_log else 0
                t = self._data_log.lap_time_at(self._preview_time) if self._data_log else 0.0
                inner = rect.adjusted(8, 4, -4, -4)
                line_h = max(1, int(_lap_timer_line_gap(w.font_size) * s))

                # box width is sized (in OverlayWidget.w) to fit "Time: 00:00:000"
                # rendered with cv2 — Qt's font metrics differ, so pick the Qt
                # point size that actually fills inner.width() with that same
                # reference string, rather than reusing the generic pt_size.
                ref_text = "Time: 00:00:000"
                probe_pt = 20
                probe_font = QFont("Monospace", probe_pt)
                probe_font.setBold(True)
                probe_w = QFontMetrics(probe_font).horizontalAdvance(ref_text)
                fit_pt = max(6, int(probe_pt * inner.width() / max(1, probe_w)))
                font = QFont("Monospace", fit_pt)
                bold_font = QFont(font)
                bold_font.setBold(True)

                def draw_line(top, label, value):
                    line_rect = QRect(inner.x(), top, inner.width(), line_h)
                    painter.setFont(font)
                    painter.drawText(line_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
                    label_w = painter.fontMetrics().horizontalAdvance(label)
                    painter.setFont(bold_font)
                    value_rect = QRect(inner.x() + label_w, top, inner.width() - label_w, line_h)
                    painter.drawText(value_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, value)

                draw_line(inner.y(), "Time: ", _format_lap_clock(t))
                draw_line(inner.y() + line_h, "Lap: ", str(lap))
            elif w.widget_type == "Bar Graph":
                val = self._data_log.value_at(w.channel, self._preview_time) if (self._data_log and w.channel) else 0.0
                painter.setPen(QPen(QColor(230, 230, 230)))
                painter.drawText(rect.adjusted(4, 2, -4, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                                 f"{w.label}: {val:.1f}")
            else:
                val_str = ""
                if self._data_log and w.channel:
                    val = self._data_log.value_at(w.channel, self._preview_time)
                    val_str = f"  [{val:.2f}]"
                painter.drawText(rect.adjusted(4, 4, -4, -4), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 f"{w.widget_type}\n{w.label}{val_str}")

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

    def mouseMoveEvent(self, event):
        if not (self._dragging or self._resize_handle):
            return
        ox, oy = self._to_overlay(event.position().x(), event.position().y())
        if self.selected:
            if self._resize_handle:
                base_w, _ = self.selected.base_size
                new_w = max(20, ox - self.selected.x)
                self.selected.scale = max(0.2, new_w / base_w)
            else:
                self.selected.x = max(0, min(self.overlay_width - self.selected.w, ox - self._drag_offset.x()))
                self.selected.y = max(0, min(self.overlay_height - self.selected.h, oy - self._drag_offset.y()))
            self.update()

    def mouseReleaseEvent(self, event):
        self._dragging = False
        self._resize_handle = False

    def add_widget(self, widget_type: str):
        w = OverlayWidget(widget_type, x=50, y=50 + len(self.widgets) * 70)
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

        self.lbl_no_channel_hint = QLabel()
        self.lbl_no_channel_hint.setWordWrap(True)
        layout.addRow("", self.lbl_no_channel_hint)

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

        self.spn_min = QDoubleSpinBox(); self.spn_min.setRange(-99999, 99999)
        self.spn_max = QDoubleSpinBox(); self.spn_max.setRange(-99999, 99999); self.spn_max.setValue(100)
        self.spn_min.valueChanged.connect(self._on_range)
        self.spn_max.valueChanged.connect(self._on_range)
        layout.addRow("Min:", self.spn_min)
        layout.addRow("Max:", self.spn_max)

        self.lbl_no_channel_hint.setVisible(False)
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
        self.btn_color.setEnabled(w.widget_type == "Bar Graph")
        idx = self.cmb_channel.findText(w.channel)
        self.cmb_channel.setCurrentIndex(idx if idx >= 0 else 0)
        for spn, val in [(self.spn_x, w.x), (self.spn_y, w.y)]:
            spn.blockSignals(True); spn.setValue(val); spn.blockSignals(False)
        self.spn_scale.blockSignals(True); self.spn_scale.setValue(w.scale); self.spn_scale.blockSignals(False)
        self.spn_min.blockSignals(True); self.spn_min.setValue(w.min_val); self.spn_min.blockSignals(False)
        self.spn_max.blockSignals(True); self.spn_max.setValue(w.max_val); self.spn_max.blockSignals(False)

        no_channel_hints = {
            "Lap Timer": "Derived from Beacon Markers in the log — no channel needed.",
            "Track Map": "Uses GPS Latitude/Longitude directly — no channel needed.",
        }
        hint = no_channel_hints.get(w.widget_type)
        self.lbl_no_channel_hint.setText(hint or "")
        self.lbl_no_channel_hint.setVisible(hint is not None)
        self.cmb_channel.setEnabled(hint is None)
        self.spn_min.setEnabled(hint is None)
        self.spn_max.setEnabled(hint is None)

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
            self._widget.scale = self.spn_scale.value()
            self.changed.emit()

    def _on_range(self):
        if self._widget:
            self._widget.min_val = self.spn_min.value()
            self._widget.max_val = self.spn_max.value()
            self.changed.emit()


# ---------------------------------------------------------------------------
# Export thread
# ---------------------------------------------------------------------------

class ExportThread(QThread):
    progress = Signal(int)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, widgets, data_log, fps, out_path):
        super().__init__()
        self.widgets = widgets
        self.data_log = data_log
        self.fps = fps
        self.out_path = out_path

    def run(self):
        try:
            dl = self.data_log
            duration = dl.duration
            total_frames = int(duration * self.fps)
            if total_frames == 0:
                self.error.emit("Data log has zero duration.")
                return

            # Determine resolution from first widget bounds or default
            w_max = max((ww.x + ww.w for ww in self.widgets), default=1920)
            h_max = max((ww.y + ww.h for ww in self.widgets), default=1080)

            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(self.out_path, fourcc, self.fps, (w_max, h_max))

            green = np.zeros((h_max, w_max, 3), dtype=np.uint8)
            green[:, :] = (0, 180, 0)  # pure BGR green screen

            for frame_idx in range(total_frames):
                t = dl.timestamps[0] + frame_idx / self.fps
                frame = green.copy()
                for ww in self.widgets:
                    ww.render_to_frame(frame, dl, t)
                writer.write(frame)
                self.progress.emit(int(frame_idx / total_frames * 100))

            writer.release()
            self.finished.emit(self.out_path)
        except Exception as e:
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
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("We Have RaceRender At Home")
        self.resize(1400, 800)
        self.data_log = DataLog()
        self._build_ui()
        self._build_menu()
        self.statusBar().showMessage("Load a data log to get started.")

    def _build_menu(self):
        mb = self.menuBar()
        file_menu = mb.addMenu("File")

        act_open = QAction("Open Data Log (.csv)…", self)
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
        act_export = QAction("Export Green Screen Video…", self)
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

        grp_log = QGroupBox("Data Log")
        ll = QVBoxLayout(grp_log)
        self.btn_open_log = QPushButton("Open CSV…")
        self.btn_open_log.clicked.connect(self.open_log)
        self.lbl_log = QLabel("No log loaded")
        self.lbl_log.setWordWrap(True)
        ll.addWidget(self.btn_open_log)
        ll.addWidget(self.lbl_log)

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
        self.spn_fps = QSpinBox(); self.spn_fps.setRange(1, 120); self.spn_fps.setValue(30)
        fl.addRow("FPS:", self.spn_fps)

        left_layout.addWidget(grp_log)
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
        ccl.addLayout(scrub_row)

        self.playback_speed = 1.0
        self._current_t = 0.0
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(33)  # ~30 ticks/sec
        self._play_timer.timeout.connect(self._on_play_tick)

        btn_export = QPushButton("🎬  Export Green Screen Video…")
        btn_export.setFixedHeight(40)
        btn_export.clicked.connect(self.export_video)
        ccl.addWidget(btn_export)

        root.addWidget(left)
        root.addWidget(canvas_col, 1)
        root.addWidget(self.props)

    def _on_res_change(self):
        self.canvas.set_resolution(self.spn_res_w.value(), self.spn_res_h.value())

    def _on_selection(self, w):
        self.props.load_widget(w)
        self.btn_delete.setEnabled(w is not None)

    def _on_scrub(self, val):
        if self.data_log.duration > 0:
            t = self.data_log.timestamps[0] + val / 1000.0 * self.data_log.duration
            self._current_t = t
            self.canvas.set_preview_time(t)
            self.lbl_time.setText(f"{t:.2f} s")

    def _on_lap_tick_clicked(self, t: float):
        if self.data_log.duration > 0:
            val = int((t - self.data_log.timestamps[0]) / self.data_log.duration * 1000)
            self.scrubber.setValue(max(0, min(1000, val)))

    def _on_speed_changed(self, text):
        self.playback_speed = float(text.rstrip("x"))

    def _on_play_clicked(self):
        if self._play_timer.isActive():
            self._play_timer.stop()
            self.btn_play.setText("▶")
        else:
            if self.data_log.duration <= 0:
                return
            # Seed the float time tracker from wherever the slider currently is,
            # since the slider's integer resolution is too coarse to advance by
            # for long logs (a single unit can be many real-time seconds).
            self._current_t = self.data_log.timestamps[0] + self.scrubber.value() / 1000.0 * self.data_log.duration
            self._play_timer.start()
            self.btn_play.setText("⏸")

    def _on_play_tick(self):
        if self.data_log.duration <= 0:
            self._play_timer.stop()
            self.btn_play.setText("▶")
            return
        step = self._play_timer.interval() / 1000.0 * self.playback_speed
        t_end = self.data_log.timestamps[-1]
        self._current_t = min(self._current_t + step, t_end)

        val = int((self._current_t - self.data_log.timestamps[0]) / self.data_log.duration * 1000)
        self.scrubber.blockSignals(True)
        self.scrubber.setValue(val)
        self.scrubber.blockSignals(False)
        self.canvas.set_preview_time(self._current_t)
        self.lbl_time.setText(f"{self._current_t:.2f} s")

        if self._current_t >= t_end:
            self._play_timer.stop()
            self.btn_play.setText("▶")

    def canvas_delete(self):
        self.canvas.delete_selected()

    def open_log(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open AiM RS2 CSV", "", "CSV Files (*.csv);;All Files (*)")
        if not path:
            return
        self.load_log_file(path)

    def load_log_file(self, path: str):
        try:
            channels = self.data_log.load(path)
            self.canvas.set_data_log(self.data_log)
            self.lbl_log.setText(f"{Path(path).name}\n{len(channels)} channels\n{self.data_log.duration:.1f}s")
            self.props.set_channels(channels)
            self.props.set_data_log(self.data_log)

            t0 = self.data_log.timestamps[0] if self.data_log.timestamps else 0.0
            t_end = self.data_log.timestamps[-1] if self.data_log.timestamps else 0.0
            lap_starts = [t0] + [m for m in self.data_log.beacon_markers if m < t_end]
            self.lap_ticks.set_markers(lap_starts, t0, self.data_log.duration)

            self.statusBar().showMessage(f"Loaded {len(channels)} channels, {self.data_log.duration:.1f}s duration")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))

    def save_layout(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Layout", "", "JSON (*.json)")
        if not path:
            return
        data = [w.to_dict() for w in self.canvas.widgets]
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        self.statusBar().showMessage(f"Layout saved to {path}")

    def load_layout(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Layout", "", "JSON (*.json)")
        if not path:
            return
        with open(path) as f:
            data = json.load(f)
        self.canvas.widgets = [OverlayWidget.from_dict(d) for d in data]
        self.canvas.selected = None
        self.canvas.update()
        self.statusBar().showMessage(f"Layout loaded from {path}")

    def export_video(self):
        if not self.data_log.timestamps:
            QMessageBox.warning(self, "No Data", "Load a data log before exporting.")
            return
        if not self.canvas.widgets:
            QMessageBox.warning(self, "No Widgets", "Add at least one widget before exporting.")
            return

        path, _ = QFileDialog.getSaveFileName(self, "Export Video", "overlay.mp4", "MP4 Video (*.mp4)")
        if not path:
            return

        progress = QProgressDialog("Rendering overlay video…", "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()

        self._export_thread = ExportThread(
            self.canvas.widgets, self.data_log, self.spn_fps.value(), path
        )
        self._export_thread.progress.connect(progress.setValue)
        self._export_thread.finished.connect(lambda p: (
            progress.close(),
            QMessageBox.information(self, "Done", f"Exported to:\n{p}")
        ))
        self._export_thread.error.connect(lambda e: (
            progress.close(),
            QMessageBox.critical(self, "Export Error", e)
        ))
        self._export_thread.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    if len(sys.argv) > 1:
        window.load_log_file(sys.argv[1])
    sys.exit(app.exec())
