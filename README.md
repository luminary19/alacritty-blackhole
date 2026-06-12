# alacritty-blackhole

[s0xDk/ghostty-blackhole](https://github.com/s0xDk/ghostty-blackhole) — a
black hole that floats over your terminal, gravitationally lenses the text,
and doubles as a pomodoro break reminder — ported to **Alacritty on Windows**.

Alacritty has no custom-shader support, so this recreates Ghostty's shader
pipeline externally:

```
Alacritty window ──(Windows.Graphics.Capture, by HWND)──► texture (iChannel0)
                                                              │
        blackhole.glsl (verbatim upstream, GLSL 3.3 harness)  ▼
Alacritty window ◄──(click-through, no-activate, topmost GL overlay)── render
```

- **overlay.py** — finds the Alacritty window, captures it live, renders
  `blackhole.glsl` over it in a borderless click-through window that follows
  the terminal, hides whenever Alacritty isn't foreground, and exits when
  Alacritty exits. Single-instance (named mutex), so launching it per
  terminal window is safe.
- **blackhole.glsl** — byte-identical to upstream; the Shadertoy/Ghostty
  uniform harness is prepended at load time. Tune the `const float` block at
  the top; the overlay hot-reloads on save.
- **tune.py** — upstream's interactive tuner ported to Windows (`msvcrt`
  instead of termios; reload happens via the overlay's file-watch instead of
  SIGUSR2). Run `python tune.py` in any pane and nudge values live.
- **test_smoke.py / test_screenshot.py** — sanity checks: headless shader
  compile + capture probe; WGC monitor screenshot (overlays included).

## Uniform emulation

| Ghostty uniform     | This port                                            |
|---------------------|------------------------------------------------------|
| `iChannel0`         | live WGC capture of the Alacritty window             |
| `iResolution/iTime` | overlay framebuffer size / monotonic clock           |
| `iDate`             | wall clock                                           |
| `iTimeCursorChange` | `GetLastInputInfo` — system-wide last keyboard/mouse |
|                     | activity, closest native analogue to Ghostty's       |
|                     | typing detector (idle ⇒ the hole fades away)         |
| `iWorkSeconds`      | **new** — continuous work seconds this session,      |
|                     | tracked by the overlay (shaders are stateless),      |
|                     | persisted to `%LOCALAPPDATA%\alacritty-blackhole-`   |
|                     | `session.json` across restarts; a ≥ `IDLE_RESET_MIN` |
|                     | pause zeroes it                                      |

## Schedule (replaces upstream's 55+5 pomodoro)

The hole is invisible until **4 hours** of continuous work
(`GROW_AFTER_MIN`), then quickly grows to full size over **5 minutes**
(`GROW_RAMP_MIN`) and stays. A **10-minute** typing pause
(`IDLE_RESET_MIN`) fades it to invisible (the last `IDLE_FADE_SEC` of the
pause is the fade) and resets the session — the next 4-hour cycle starts
when you resume. Short pauses under 10 minutes don't reset anything.

## Install / run

```powershell
python -m pip install -r requirements.txt
pythonw overlay.py                  # normal: waits for Alacritty, runs until it exits
python overlay.py --debug           # forced mid-cycle hole, always visible
python overlay.py --debug --duration 15
```

Auto-start is wired into `%APPDATA%\alacritty\alacritty.toml`: the shell
command `Start-Process`es `pythonw overlay.py` before attaching zellij, and
the mutex keeps extra windows from spawning duplicates.

## Tuning

`python tune.py` (same keys as upstream: j/k select, h/l nudge, H/L coarse,
s set exact, q quit). Notable knobs in `blackhole.glsl`:

- `HOLE_RADIUS`, `LENS_STRENGTH`, `DISK_GAIN`, `DISK_TILT`, `DRIFT_SPEED`
- `WORK_AREA` — bottom screen fraction kept undistorted
- `GROW_AFTER_MIN` / `GROW_RAMP_MIN` — hours-until-visible / growth ramp
- `IDLE_RESET_MIN` / `IDLE_FADE_SEC` — pause that resets the session / fade length
- `TIME_SCALE` — set to e.g. 100 to fast-forward the session for testing

The overlay re-reads these consts on every hot-reload, so `IDLE_RESET_MIN`
changes take effect in the session tracker too. Note: a `tune.py` started
before a manual edit to the shader will clobber that edit on its next save
(it rewrites the file from memory) — restart the tuner after hand-edits.

## Troubleshooting

- Crashes from the windowless `pythonw` run land in
  `%TEMP%\alacritty-blackhole-crash.log`.
- The overlay only shows while Alacritty is the foreground window; alt-tab
  away and it hides.
- If the hole is invisible: you probably haven't hit `GROW_AFTER_MIN`
  (4 h) of continuous work yet, or a ≥ 10-minute pause reset the session.
  Check progress in `%LOCALAPPDATA%\alacritty-blackhole-session.json`
  (`start_epoch` is when the current session began).
