# We Have RaceRender At Home

A data overlay tool for AiM data logs (CSV or native `.xrk`). Lay out widgets
(lap timer, numeric readouts, bar graphs, GPS track map) on a canvas, preview
them against your data *and* your actual race footage, dial in sync — or let
the **Data Wizard** find and sync the pairing for you — then export, either
composited directly onto your video or as a green-screen overlay
to key out in another video editor.

## Setup

```
pip install -r requirements.txt
python source/WHRRAH.py
```

Requires Python 3.10+ (uses modern type-hint syntax).

**ffmpeg is optional but recommended** — it's used for three things: generating
a low-lag preview proxy for laggy source footage, muxing audio into a
composited export, and the Data Wizard's gyro-based sync matching for DJI
footage. The app looks for `ffmpeg` on PATH (falls back to a couple of common
Windows install locations). Without it, everything else still works;
composited exports just come out silent, you won't have the low-lag preview
option, and wizard matching falls back to coarse EXIF-timestamp matching.

**`.xrk` support requires `source/xrk_dll/`** (bundled in this repo) — a small
wrapper around AiM's official `MatLabXRK` DLL. This is Windows-only; CSV logs work everywhere.

Export text (Lap Timer, Numeric Display, Bar Graph, Track Map label) is rendered
via Pillow using a monospace TTF (Consolas, falling back to Courier New) rather
than OpenCV's built-in font — it's a much closer visual match to the live
preview's Qt "Monospace" font, and widget box sizes are measured from this same
font so the export and the preview agree.

### Launch arguments

You can skip the manual "Open CSV…" step by passing a data log on the command line:

```
python source/WHRRAH.py path/to/log.csv
python source/WHRRAH.py path/to/log.csv --layout path/to/layout.json
```

`source/default_layout.json` is loaded automatically on startup if it exists.

## Importing data

Use **File → Open Data Log (.csv/.xrk)…** (`Ctrl+O`), or the **Open CSV…** /
**Open XRK…** buttons in the **Data** group in the left sidebar.

- **CSV** — an AiM RS2-exported CSV (see `source/sample_data.csv` for an
  example). The app reads the **header row** (`"Time","GPS Speed",...`) for
  channel names, and the **`Beacon Markers`** metadata row (absolute time in
  seconds of each lap/segment crossing) for lap timing — without it, lap-based
  features fall back to treating the whole log as one lap.
- **XRK** — AiM's native RaceStudio 3 format, read directly via the bundled DLL (see
  [Setup](#setup)).

Once loaded, the **Data** group shows the filename, channel count,
log length as `mm:ss:ms`, total completed laps, and the best lap's number and
time.

## The Data Wizard

Click **📋 Data Wizard…** at the top of the sidebar to point the app at a
folder of AiM sessions and a folder of video files, and have it figure out
which video goes with which session — and how far into the session each video
starts — automatically.

- **Browse…** for the AiM data folder (scans for `.xrk` files) and the video
  folder (`.mp4`/`.mov`/`.avi`/`.mkv`).
- **Find Matches** runs in the background and scores every plausible
  session/video pairing:
  - **Gyro correlation** When the video files have gyro data embedded, this function
    cross-correlates the video's gyro signal against the
    AiM's gyro signal, and syncs the video to the session using this signal. A confidence score
    is displayed showing how good the match is.
	-DJI Osmo Action cameras that I have tested this with will record gyro data when the FoV is set to Wide, 
	 and internal video stabilization features are disabled.
  - **EXIF `creation_time`** as a fallback when gyro data isn't available.
- The results table shows `AiM Session | Video | Laps | Best Lap | Confidence | [Load]`
  (lap count and best lap match what the Data group shows once that session is
  loaded) — click **Load** to load both into the app with the offset already
  dialed in, and the preview/scrubber automatically clamped to just the range
  the video actually covers.
- Results are cached per folder pair — reopening the wizard with the same AiM/
  video folders restores the table instantly instead of re-parsing gyro
  telemetry for every pairing again. Click **Find Matches** again if you've
  added/changed files in either folder.

This can take a while on a large folder — gyro correlation means parsing the
full telemetry stream for every video and every session it's still a
candidate for. If you already know the session and video you want to load I recommend 
moving them to their own folder and just using the wizard to sync footage to data.

## Loading video

Click **Open Video…** in the **Video** group to load your race footage. Once
loaded, the canvas preview shows the video behind your widgets.

- **Offset** — the video's own start, expressed as an absolute position within
  the session (e.g. `227.2` means the video begins 227.2s into the log).
  This stays constant no matter which lap you select afterward — switching
  laps doesn't require re-tuning it. Use it to line up GPS-derived widgets
  against the footage, and the per-widget **Time Offset** (below) for channels
  that drift relative to that sync. Before the video's own start (e.g.
  scrubbing earlier than the offset allows), the preview shows the green
  screen instead of freezing on the first frame.
- **Make Low-Lag Preview…** — This transcodes a small proxy via ffmpeg and swaps it in for preview only, may help preview playback framerate.
- Loading a video also sets **Export FPS** (in the sidebar, near the bottom)
  to the source's exact frame rate.

## The canvas and widgets

Add a widget with the buttons under **Add Widget**, then drag it into position
on the canvas. Click a widget to select it (its properties appear on the
right); drag its bottom-right corner to resize. Only Bar Graph shows a color
outline by default (it doubles as the fill color) — the rest just show a dark
background, plus a dashed selection border while you're editing them.

### Widget types

- **Lap Timer** — shows any combination of `Time` (elapsed in the current lap),
  `Last` (previous completed lap's time), `Best` (fastest completed lap so
  far), and `Lap` (current lap number, starting at `0` for the out-lap before
  the first beacon crossing). Toggle each line on/off and pick a text color in
  the properties panel.
- **Numeric Display** — the value on top, label centered beneath it, for any
  selected channel. **Decimals** controls how many decimal places the value
  shows; **Label Font Size** sizes the label independently of the value
  (which scales with the widget's own **Scale**).
- **Bar Graph** — a fill bar for any selected channel, scaled to that channel's
  observed min/max automatically when you pick it (override Min/Max
  afterward if you want a fixed scale). Optionally hide the numeric value and
  just show the bar. Fill color and text color are both editable. **Decimals**
  controls the displayed value's precision here too.
- **Track Map** — draws the GPS path from lap 1 as a reference line, with a dot
  for the current position (held at the start/finish line during the out-lap).

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
	-Channels taken from the car ECU such as RPM tend to lag. I manually dial in an offset for these channels by scrubbing to a point in the video where
	 I shift at redline and then adjust the RPM offset until it matches well.

## Selecting laps

Click **Select Laps…** in the **Data** group to choose which lap(s) the
scrubber, preview, and export should cover — nothing is selected by default.
Pick one or more laps (contiguous picks are grouped into a single padded
range), optionally generate a sync `.txt` file (frame/timestamp at each lap
start), and confirm.

**Pad Start** / **Pad End** (also in the **Data** group) add seconds before/
after the selected range independently. If padding would extend past the end of the
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
offset, and other settings — plus the **Overlay Resolution** — to a JSON file.
**Load Layout…** reads one back, restoring the resolution too, and handles
older layout files that don't have it saved (falls back to whatever's
currently set). Handy for reusing the same overlay arrangement across
multiple sessions/logs, especially if different cameras need different
resolutions/aspect ratios. You can also auto-load a layout at launch with
`--layout`, or by placing a `default_layout.json` in `source/` (see
[Launch arguments](#launch-arguments)).

## Exporting

Click **🎬 Export Video…** in the **Video** group (or **Export → Export Video…**, `Ctrl+E`).

- If a video is loaded, the export composites your widgets directly onto the
  original source footage. If ffmpeg isn't available, the export still 
  succeeds — silently, with a note in the "Done" dialog explaining why.
- If no video is loaded, a green screen background is exported.
  (see [Compositing in your editor](#compositing-in-your-editor)).
- If the log has lap markers, you must [select laps](#selecting-laps) first —
  export uses whatever's currently selected in the sidebar rather than
  prompting again.
- The export destination can't be the same file as the currently loaded
  source video (it reads frames from that file while writing the output,
  which would corrupt it) — pick a different filename. If muxing the audio in
  hits a transient file lock (antivirus scan, Explorer thumbnailing, etc.),
  it retries automatically for a few seconds before giving up and noting it
  in the "Done" dialog.

Export resolution is the **Overlay Resolution** field in the sidebar when
compositing onto video (so widget positions map 1:1 onto the source frame),
or the bounding box of all your widgets when falling back to green screen.

## Compositing in your editor

If you exported without a loaded video, the output is rendered on a solid
green background (BGR `(0, 180, 0)`). Import it as a layer above your footage
and apply a chroma key / green screen effect to key it out, leaving just the overlay graphics.
