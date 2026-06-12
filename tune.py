#!/usr/bin/env python3
"""Live tuner for blackhole.glsl — Windows port, run it inside Alacritty.

Parses the `const float NAME = VALUE;` block at the top of the shader,
lets you nudge values with the keyboard and rewrites the file. The overlay
(overlay.py) watches the file's mtime and hot-reloads the shader on save,
so there is no Ghostty-style SIGUSR2 dance — saving is reloading.

Keys:
  up/down or k/j   select parameter
  left/right h/l   nudge value by step
  shift + h/l      nudge by 10x step
  s                type an exact value
  r                force a reload (re-saves the file)
  q / ctrl-c       quit
"""

import ctypes
import math
import msvcrt
import os
import re
import sys

SHADER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blackhole.glsl")
CONST_RE = re.compile(
    r"^(const float\s+)(\w+)(\s*=\s*)(-?\d+\.\d+)(\s*;.*)$"
)


def enable_ansi():
    # zellij/Alacritty are VT-native; this only matters under conhost
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def load():
    params = []  # (name, value, line_index)
    with open(SHADER, encoding="utf-8") as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        m = CONST_RE.match(line)
        if m:
            params.append([m.group(2), float(m.group(4)), i])
    return lines, params


def save(lines, params):
    for name, value, i in params:
        lines[i] = CONST_RE.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{value:.4f}{m.group(5)}",
            lines[i],
        )
    tmp = SHADER + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)
    os.replace(tmp, SHADER)


def step_for(value):
    if value == 0.0:
        return 0.01
    return 10.0 ** (math.floor(math.log10(abs(value))) - 1)


def read_key():
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):  # arrow/function key prefix
        seq = msvcrt.getwch()
        return {"H": "up", "P": "down", "M": "right", "K": "left"}.get(seq, "")
    return ch


def draw(params, sel, status):
    sys.stdout.write("\x1b[2J\x1b[H")
    print("black hole tuner — j/k select, h/l nudge, H/L coarse, s set, r reload, q quit\n")
    for i, (name, value, _) in enumerate(params):
        cursor = "\x1b[7m" if i == sel else ""
        print(f"  {cursor}{name:<16} {value:>10.4f}\x1b[0m   step {step_for(value):g}")
    print(f"\n  {status}")
    sys.stdout.flush()


def prompt_value():
    try:
        raw = input("\n  new value: ")
        return float(raw)
    except ValueError:
        return None


def main():
    enable_ansi()
    lines, params = load()
    if not params:
        sys.exit(f"no `const float` params found in {SHADER}")
    sel, status = 0, f"{len(params)} params from {os.path.basename(SHADER)}"

    while True:
        draw(params, sel, status)
        key = read_key()
        changed = False
        if key in ("q", "\x03"):
            break
        elif key in ("k", "up"):
            sel = (sel - 1) % len(params)
        elif key in ("j", "down"):
            sel = (sel + 1) % len(params)
        elif key in ("h", "l", "H", "L", "left", "right"):
            direction = 1 if key in ("l", "L", "right") else -1
            coarse = 10.0 if key in ("H", "L") else 1.0
            params[sel][1] += direction * coarse * step_for(params[sel][1])
            params[sel][1] = round(params[sel][1], 6)
            changed = True
        elif key == "s":
            v = prompt_value()
            if v is not None:
                params[sel][1] = v
                changed = True
        elif key == "r":
            changed = True

        if changed:
            save(lines, params)
            status = (f"saved {params[sel][0]} = {params[sel][1]:.4f} — "
                      f"overlay hot-reloads automatically")
    print()


if __name__ == "__main__":
    main()
