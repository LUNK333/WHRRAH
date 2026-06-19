# We Have RaceRender At Home

A data overlay tool for AiM Solo2DL data logs. Lay out widgets (lap timer,
numeric readouts, bar graphs, GPS track map) on a canvas, preview them against
your data, then export a green-screen overlay video to composite over your
race footage in DaVinci Resolve (or any editor with chroma key).

## Setup

```
pip install -r requirements.txt
python source/WHRRAH.py
```

Requires Python 3.10+ (uses modern type-hint syntax).

### Launch arguments

You can skip the manual "Open CSV…" step by passing a data log on the command line:

```
python source/WHRRAH.py path/to/log.csv
python source/WHRRAH.py path/to/log.csv --layout path/to/layout.json
```

The CSV argument is optional and positional; `--layout` is optional and also
loads a saved widget layout on launch (see [Saving and loading layouts](#saving-and-loading-layouts)).

## Importing data

Use **File → Open Data Log (.csv)…** (`Ctrl+O`) or the **Open CSV…** button in
the left sidebar, and pick an AiM RS2-exported CSV (see `source/sample_data.csv`
for an example). The app reads:

- The **header row** (`"Time","GPS Speed",...`) and the channel data beneath it —
  every numeric column becomes a selectable channel.
- The **`Beacon Markers`** metadata row, which the log exports with the absolute
  time (in seconds) of each lap/segment crossing. This is what drives the Lap
  Timer and the lap tick marks on the scrubber — without it, lap-based features
  fall back to treating the whole log as one lap.

Once loaded, the sidebar shows the channel count and log duration, and the
scrubber below the canvas covers the full session.

## The canvas and widgets

Add a widget with the buttons under **Add Widget**, then drag it into position
on the green canvas. Click a widget to select it (its properties appear on the
right); drag its bottom-right corner to resize.

### Widget types

- **Lap Timer** — shows any combination of `Time` (elapsed in the current lap),
  `Last` (previous completed lap's time), `Best` (fastest completed lap so
  far), and `Lap` (current lap number, starting at `0` for the out-lap before
  the first beacon crossing). Toggle each line on/off and pick a text color in
  the properties panel. Requires `Beacon Markers` in the log — no channel
  selection needed.
- **Numeric Display** — `label: value` for any selected channel.
- **Bar Graph** — a fill bar for any selected channel, scaled to that channel's
  observed min/max automatically when you pick it (override Min/Max
  afterward if you want a fixed scale). Optionally hide the numeric value and
  just show the bar. Fill color and text color are both editable.
- **Track Map** — draws the GPS path from lap 1 as a reference line, with a dot
  for the current position (held at the start/finish line during the out-lap).
  Requires `GPS Latitude`/`GPS Longitude` channels — no channel selection needed.

All widgets share:
- **Label** — editable text.
- **Scale** — one factor that scales width, height, and font together (Bar
  Graph gets independent **Scale X** / **Scale Y** instead, via its corner-drag
  handle too).
- **X / Y** — position on the overlay canvas.

## Preview and playback

- Drag the **scrubber** to move the preview time. Small tick marks under it
  mark the start of each lap — click one to jump straight there.
- **▶ / ⏸** plays back the log in real time; the speed dropdown next to it
  does 1x/2x/4x.

## Saving and loading layouts

**File → Save Layout…** (`Ctrl+S`) or the **Save Layout…** button in the
sidebar writes every widget's type, position, scale, channel, colors, and
other settings to a JSON file. **Load Layout…** reads one back — handy for
reusing the same overlay arrangement across multiple sessions/logs. You can
also auto-load a layout at launch with `--layout` (see [Launch arguments](#launch-arguments)).

## Exporting

Click **🎬 Export Green Screen Video…** (or **Export → Export Green Screen
Video…**, `Ctrl+E`). If the log has lap data, you'll get a dialog to:

- **Select which laps to include** — each lap is listed with its lap time;
  use Select All/None or check individual laps.
- **Pad start/end of each lap** by a configurable number of seconds (default
  10s) — useful if you edit video and want a few seconds of footage before/after
  the lap itself. Padding is clamped to the actual log bounds (no blank frames
  at the very start/end of the session), and when you select multiple
  consecutive laps, padding only applies to the outside edges of that run, not
  between the laps.
- **Generate a sync `.txt` file** alongside the video, listing the frame number
  and timestamp where each exported lap actually starts — useful for lining
  the overlay up against your raw footage in your video editor.

Pick an output path and the render runs in the background with a progress
dialog; **Cancel** actually stops the render and deletes the partial file.

Export resolution is the bounding box of all your widgets; FPS is set in the
**Export FPS** field in the sidebar.

## Compositing in your editor

The exported video is rendered on a solid green background (BGR `(0, 180, 0)`).
Import it as a layer above your footage and apply a chroma key / green
screen effect (e.g. DaVinci Resolve's Ultra Key) to key it out, leaving just
the overlay graphics.
