# We Have RaceRender At Home

A data overlay tool for AiM Solo2DL data logs. Lay out widgets (lap timer,
numeric readouts, bar graphs, GPS track map) on a canvas, preview them
against your data *and* your actual race footage, dial in sync, then export —
either composited directly onto your video (with audio) or as a green-screen
overlay to key out in DaVinci Resolve.

## Setup

```
pip install -r requirements.txt
python source/WHRRAH.py
```

Requires Python 3.10+ (uses modern type-hint syntax).

**ffmpeg is optional but recommended** — it's used for two things: generating
a low-lag preview proxy for laggy source footage, and muxing audio into a
composited export. The app looks for `ffmpeg` on PATH (falls back to a couple
of common Windows install locations). Without it, everything else still
works; composited exports just come out silent and you won't have the
low-lag preview option.

### Launch arguments

You can skip the manual "Open CSV…" step by passing a data log on the command line:

```
python source/WHRRAH.py path/to/log.csv
python source/WHRRAH.py path/to/log.csv --layout path/to/layout.json
```

The CSV argument is optional and positional; `--layout` is optional and also
loads a saved widget layout on launch (see [Saving and loading layouts](#saving-and-loading-layouts)).

If `source/default_layout.json` exists, it's loaded automatically on startup
(before any `--layout` argument, which takes priority over it).

## Importing data

Use **File → Open Data Log (.csv)…** (`Ctrl+O`) or the **Open CSV…** button in
the **Data** group in the left sidebar, and pick an AiM RS2-exported CSV (see
`source/sample_data.csv` for an example). The app reads:

- The **header row** (`"Time","GPS Speed",...`) and the channel data beneath it —
  every numeric column becomes a selectable channel.
- The **`Beacon Markers`** metadata row, which the log exports with the absolute
  time (in seconds) of each lap/segment crossing. This is what drives the Lap
  Timer, lap selection, and the lap tick marks on the scrubber — without it,
  lap-based features fall back to treating the whole log as one lap.

Once loaded, the **Data** group shows the (elided) filename, channel count,
log length as `mm:ss:ms`, total completed laps, and the best lap's number and
time.

## Loading video

Click **Open Video…** in the **Video** group to load your race footage. Once
loaded, the canvas preview shows the actual video frame behind your widgets
instead of a green fill, and audio plays back in sync with the scrubber and
▶/⏸ controls (volume slider next to the scrubber).

- **Offset** — shifts the video relative to the preview timeline (zero-based
  at the start of whatever lap range is selected — see below). Increasing it
  pulls an *earlier* video frame forward to match the current playback
  position; use it to line up GPS-derived widgets against the footage, and
  the per-widget **Time Offset** (below) for channels that drift relative to
  that sync.
- **Make Low-Lag Preview…** — many camera/editor exports use a very long
  GOP (sometimes a single keyframe for the whole clip), which makes seeking
  in-app painfully slow regardless of resolution or bitrate. This transcodes
  a small proxy (lower resolution, frequent keyframes) via ffmpeg and swaps
  it in for preview only — exports always use the original, full-quality
  source. Requires ffmpeg.
- Loading a video also sets **Export FPS** (in the sidebar, near the bottom)
  to the source's exact frame rate, including fractional rates like `59.94` —
  don't round these to `60` if you plan to reimport into an NTSC-rate
  timeline, or the overlay will slowly drift out of sync with your footage.

## The canvas and widgets

Add a widget with the buttons under **Add Widget**, then drag it into position
on the canvas. Click a widget to select it (its properties appear on the
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
- **Time Offset** — shifts *just this widget's* data lookup, in 0.05s steps.
  Useful for channels (e.g. CAN bus data) that lag behind GPS-derived ones —
  dial in a per-widget correction without touching the shared timeline or
  video sync. Positive values sample further ahead in the log.

## Selecting laps

Click **Select Laps…** in the **Data** group to choose which lap(s) the
scrubber, preview, and export should cover — nothing is selected by default.
Pick one or more laps (contiguous picks are grouped into a single padded
range), optionally generate a sync `.txt` file (frame/timestamp at each lap
start), and confirm.

**Pad Start** / **Pad End** (also in the **Data** group) add seconds before/
after the selected range independently, and re-apply live as you change them
— no need to reopen the dialog. If padding would extend past the end of the
loaded video (given the current offset), it's clamped to what footage
actually exists and you'll get a warning showing how much was trimmed,
instead of the export silently truncating itself.

## Preview and playback

- Drag the **scrubber** to move the preview time — its `0.00 s` is the start
  of the selected lap range (or the whole log, if no laps are selected), not
  the start of the data log. Small tick marks under it mark the start of each
  selected lap; click one to jump straight there.
- **▶ / ⏸** plays back the log (and video/audio, if loaded) in real time; the
  speed dropdown next to it does 1x/2x/4x.

## Saving and loading layouts

**File → Save Layout…** (`Ctrl+S`) or the **Save Layout…** button in the
sidebar writes every widget's type, position, scale, channel, colors, time
offset, and other settings to a JSON file. **Load Layout…** reads one back —
handy for reusing the same overlay arrangement across multiple sessions/logs.
You can also auto-load a layout at launch with `--layout`, or by placing a
`default_layout.json` in `source/` (see [Launch arguments](#launch-arguments)).

## Exporting

Click **🎬 Export Video…** (or **Export → Export Video…**, `Ctrl+E`).

- If a video is loaded, the export composites your widgets directly onto the
  original (full-quality) source footage, with matching audio muxed in via
  ffmpeg afterward (cv2's writer can't write audio itself). If ffmpeg isn't
  available, the export still succeeds — silently, with a note in the "Done"
  dialog explaining why.
- If no video is loaded, it falls back to the original green-screen behavior
  (see [Compositing in your editor](#compositing-in-your-editor)).
- If the log has lap markers, you must [select laps](#selecting-laps) first —
  export uses whatever's currently selected in the sidebar rather than
  prompting again.

Pick an output path and the render runs in the background with a progress
dialog; **Cancel** actually stops the render and deletes the partial file.

Export resolution is the **Overlay Resolution** field in the sidebar when
compositing onto video (so widget positions map 1:1 onto the source frame),
or the bounding box of all your widgets when falling back to green screen.

## Compositing in your editor

If you exported without a loaded video, the output is rendered on a solid
green background (BGR `(0, 180, 0)`). Import it as a layer above your footage
and apply a chroma key / green screen effect (e.g. DaVinci Resolve's Ultra
Key) to key it out, leaving just the overlay graphics. If you exported with a
video loaded, the footage and audio are already composited in — no further
keying needed.
