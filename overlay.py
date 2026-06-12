#!/usr/bin/env python3
"""Black hole overlay for Alacritty on Windows.

Alacritty has no custom-shader support, so this recreates Ghostty's
custom-shader pipeline externally:

  * finds the Alacritty window and captures it with Windows.Graphics.Capture
    (by HWND — survives title changes, never sees this overlay)
  * renders blackhole.glsl over the terminal in a borderless, click-through,
    never-focused, always-on-top window aligned to the terminal
  * feeds the shader the Shadertoy/Ghostty uniforms it expects:
      iChannel0          — live capture of the terminal
      iResolution, iTime — overlay size / monotonic clock
      iDate              — wall clock
      iTimeCursorChange  — emulated with GetLastInputInfo (system-wide
                           "last keyboard/mouse activity"), the closest
                           native analogue to Ghostty's typing detector
      iWorkSeconds       — continuous seconds of work in the current session
                           (shaders are stateless, so the session lives here:
                           a typing pause >= IDLE_RESET_MIN zeroes it, and the
                           shader keeps the hole hidden until GROW_AFTER_MIN).
                           Persisted to disk so an overlay restart doesn't
                           forfeit hours of session progress.
  * hot-reloads blackhole.glsl whenever the file changes (tune.py just saves)

The overlay hides itself whenever Alacritty is not the foreground window and
exits when Alacritty exits. A named mutex keeps it single-instance, so it is
safe to launch from the Alacritty shell command on every new window.

Usage:
    pythonw overlay.py            # normal (waits for Alacritty, runs forever)
    python  overlay.py --debug    # always visible, synthetic mid-cycle
                                  # pomodoro state, no single-instance guard
    python  overlay.py --debug --duration 15   # self-exit after 15 s
"""

import argparse
import ctypes
import datetime
import json
import os
import re
import sys
import threading
import time
from ctypes import wintypes

# ---------------------------------------------------------------- win32 ----

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183

# explicit signatures — bare ctypes truncates 64-bit handles / sign-extends
# wrongly on x64, which makes calls like SetWindowPos fail silently
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

SHADER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blackhole.glsl")
MUTEX_NAME = "Local\\AlacrittyBlackholeOverlay"
SESSION_FILE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.dirname(SHADER)),
                            "alacritty-blackhole-session.json")


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwTime", wintypes.DWORD)]


def seconds_since_last_input():
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    ticks = kernel32.GetTickCount()
    return ((ticks - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def find_alacritty_hwnd(exclude=()):
    """Top-level visible window owned by alacritty.exe (title-agnostic)."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _):
        if hwnd in exclude or not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                if os.path.basename(buf.value).lower() == "alacritty.exe":
                    found.append(hwnd)
            kernel32.CloseHandle(h)
        return not found  # stop at first match
    user32.EnumWindows(enum_proc, 0)
    return found[0] if found else None


def window_rect(hwnd):
    """Extended frame bounds (physical px, excludes the drop shadow)."""
    rect = wintypes.RECT()
    res = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
    if res != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def make_overlay_unobtrusive(hwnd):
    """Never steal focus, never show in alt-tab. Click-through is handled by
    GLFW's MOUSE_PASSTHROUGH hint; topmost by the FLOATING hint."""
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)


# ---------------------------------------------------------------- shader ----

GLSL_HEADER = """#version 330 core
uniform vec3  iResolution;
uniform float iTime;
uniform vec4  iDate;
uniform float iTimeCursorChange;
uniform float iWorkSeconds;
uniform sampler2D iChannel0;
out vec4 _outColor;
#line 1
"""

# Ghostty hands the shader a top-down fragCoord; flip GL's bottom-up one.
GLSL_FOOTER = """
void main() {
    vec2 fc = vec2(gl_FragCoord.x, iResolution.y - gl_FragCoord.y);
    mainImage(_outColor, fc);
    _outColor.a = 1.0;
}
"""

VERTEX = """#version 330 core
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""


def build_program(ctx):
    with open(SHADER, encoding="utf-8") as f:
        body = f.read()
    return ctx.program(vertex_shader=VERTEX,
                       fragment_shader=GLSL_HEADER + body + GLSL_FOOTER)


CONST_RE = re.compile(r"const float\s+(\w+)\s*=\s*(-?\d+(?:\.\d+)?)\s*;")


def shader_consts():
    """The shader's tunables double as the overlay's config (tune.py edits
    them, the overlay re-reads on hot-reload). Session logic needs a few."""
    with open(SHADER, encoding="utf-8") as f:
        src = f.read()
    return {m.group(1): float(m.group(2)) for m in CONST_RE.finditer(src)}


class WorkSession:
    """Continuous-work clock: runs while the user is active, zeroes once a
    pause reaches the reset threshold, survives overlay restarts on disk."""

    def __init__(self, idle_reset_sec):
        self.idle_reset_sec = max(idle_reset_sec, 1.0)
        self.start_epoch = None  # wall-clock time the current session began
        self.last_saved = 0.0
        try:
            with open(SESSION_FILE, encoding="utf-8") as f:
                st = json.load(f)
            # resume only if the overlay wasn't gone long enough to be a break
            if (st["start_epoch"] is not None
                    and time.time() - st["saved_at"] < self.idle_reset_sec):
                self.start_epoch = st["start_epoch"]
        except (OSError, ValueError, KeyError):
            pass

    def update(self, idle_sec):
        now = time.time()
        if idle_sec >= self.idle_reset_sec:
            self.start_epoch = None          # break taken: cycle restarts
        elif self.start_epoch is None:
            self.start_epoch = now - idle_sec  # work resumed idle_sec ago
        work = (now - self.start_epoch) if self.start_epoch is not None else 0.0
        if now - self.last_saved > 15.0:
            self.last_saved = now
            try:
                with open(SESSION_FILE, "w", encoding="utf-8") as f:
                    json.dump({"start_epoch": self.start_epoch,
                               "saved_at": now}, f)
            except OSError:
                pass
        return work


def set_uniform(prog, name, value):
    if name in prog:
        prog[name].value = value


# ----------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--debug", action="store_true",
                    help="always show, synthetic pomodoro state, no mutex")
    ap.add_argument("--duration", type=float, default=0,
                    help="exit after N seconds (0 = run until Alacritty exits)")
    args = ap.parse_args()

    if not args.debug:
        kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return  # another overlay is already running

    # physical-pixel coordinates everywhere
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PMv2
    except Exception:
        pass

    import glfw
    import moderngl
    import numpy as np
    from windows_capture import WindowsCapture

    # -- wait for Alacritty ------------------------------------------------
    deadline = time.monotonic() + (args.duration or 1e18)
    target = None
    while target is None:
        target = find_alacritty_hwnd()
        if target is None:
            if time.monotonic() > deadline:
                return
            time.sleep(1.0)

    x, y, w, h = window_rect(target)
    w, h = max(w, 64), max(h, 64)

    # -- overlay window ----------------------------------------------------
    if not glfw.init():
        raise RuntimeError("glfw init failed")
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
    glfw.window_hint(glfw.DECORATED, False)
    glfw.window_hint(glfw.RESIZABLE, False)
    glfw.window_hint(glfw.FLOATING, True)
    glfw.window_hint(glfw.FOCUSED, False)
    glfw.window_hint(glfw.FOCUS_ON_SHOW, False)
    glfw.window_hint(glfw.VISIBLE, False)
    glfw.window_hint(glfw.SCALE_TO_MONITOR, False)
    glfw.window_hint(glfw.MOUSE_PASSTHROUGH, True)

    win = glfw.create_window(w, h, "blackhole-overlay", None, None)
    if not win:
        glfw.terminate()
        raise RuntimeError("glfw window failed")
    glfw.make_context_current(win)
    glfw.swap_interval(1)
    ctx = moderngl.create_context()

    overlay_hwnd = glfw.get_win32_window(win)
    make_overlay_unobtrusive(overlay_hwnd)
    glfw.set_window_pos(win, x, y)
    glfw.set_window_size(win, w, h)
    glfw.show_window(win)

    prog = build_program(ctx)
    vao = ctx.vertex_array(prog, [])
    shader_mtime = os.path.getmtime(SHADER)
    consts = shader_consts()
    session = WorkSession(consts.get("IDLE_RESET_MIN", 10.0) * 60.0)

    # -- capture thread ----------------------------------------------------
    latest = {"frame": None, "seq": 0, "closed": False}
    frame_lock = threading.Lock()

    capture = WindowsCapture(cursor_capture=False, draw_border=False,
                             window_hwnd=target)

    @capture.event
    def on_frame_arrived(frame, capture_control):
        with frame_lock:
            latest["frame"] = frame.frame_buffer.copy()  # BGRA, row 0 = top
            latest["seq"] += 1

    @capture.event
    def on_closed():
        latest["closed"] = True

    control = capture.start_free_threaded()

    # -- render loop ---------------------------------------------------------
    tex = None
    tex_size = (0, 0)
    seen_seq = -1
    t0 = time.monotonic()
    frames = 0
    hidden = False

    try:
        while not glfw.window_should_close(win):
            now = time.monotonic()
            if args.duration and now - t0 > args.duration:
                break
            if latest["closed"] or not user32.IsWindow(target):
                break

            # follow the terminal window
            if frames % 6 == 0:
                nx, ny, nw, nh = window_rect(target)
                if (nx, ny, nw, nh) != (x, y, w, h) and nw > 0 and nh > 0:
                    x, y, w, h = nx, ny, nw, nh
                    glfw.set_window_pos(win, x, y)
                    glfw.set_window_size(win, w, h)

                # only haunt the terminal while it's actually in front
                fg = user32.GetForegroundWindow()
                want_hidden = (not args.debug and
                               (fg != target or user32.IsIconic(target)))
                if want_hidden != hidden:
                    hidden = want_hidden
                    if hidden:
                        glfw.hide_window(win)
                    else:
                        glfw.show_window(win)

                # hot-reload tuned shader
                try:
                    mt = os.path.getmtime(SHADER)
                    if mt != shader_mtime:
                        shader_mtime = mt
                        new_prog = build_program(ctx)
                        prog.release()
                        prog = new_prog
                        vao = ctx.vertex_array(prog, [])
                        consts = shader_consts()
                        session.idle_reset_sec = max(
                            consts.get("IDLE_RESET_MIN", 10.0) * 60.0, 1.0)
                except Exception as e:
                    print(f"shader reload failed: {e}", file=sys.stderr)

            if hidden:
                glfw.poll_events()
                time.sleep(0.10)
                frames += 1
                continue

            # newest captured frame -> texture
            with frame_lock:
                frame = latest["frame"] if latest["seq"] != seen_seq else None
                seen_seq = latest["seq"]
            if frame is not None:
                fh, fw = frame.shape[:2]
                if (fw, fh) != tex_size:
                    if tex is not None:
                        tex.release()
                    tex = ctx.texture((fw, fh), 4)
                    tex.swizzle = "BGRA"  # capture is BGRA in memory
                    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                    tex_size = (fw, fh)
                tex.write(np.ascontiguousarray(frame))

            if tex is None:
                glfw.poll_events()
                time.sleep(0.01)
                continue

            fbw, fbh = glfw.get_framebuffer_size(win)
            ctx.viewport = (0, 0, fbw, fbh)
            set_uniform(prog, "iResolution", (float(fbw), float(fbh), 1.0))
            t = now - t0
            set_uniform(prog, "iTime", t)
            dt = datetime.datetime.now()
            midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            set_uniform(prog, "iDate", (float(dt.year), float(dt.month),
                                        float(dt.day),
                                        (dt - midnight).total_seconds()))
            if args.debug:
                # synthetic state: 4-hour threshold already passed, growth
                # ramp in progress, never idle — hole visible for testing
                grow_after = consts.get("GROW_AFTER_MIN", 240.0) * 60.0
                # 30x ramp: full size ~10 s in, so short debug runs see it all
                set_uniform(prog, "iWorkSeconds", grow_after + t * 30.0)
                set_uniform(prog, "iTimeCursorChange", t)
            else:
                idle = seconds_since_last_input()
                set_uniform(prog, "iWorkSeconds", session.update(idle))
                set_uniform(prog, "iTimeCursorChange", t - idle)
            tex.use(0)
            set_uniform(prog, "iChannel0", 0)
            vao.render(mode=moderngl.TRIANGLES, vertices=3)
            glfw.swap_buffers(win)
            glfw.poll_events()
            frames += 1
    finally:
        try:
            control.stop()
        except Exception:
            pass
        glfw.terminate()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        crash = os.path.join(os.environ.get("TEMP", "."),
                             "alacritty-blackhole-crash.log")
        with open(crash, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.datetime.now()} ---\n")
            f.write(traceback.format_exc())
        raise
