#!/usr/bin/env python3
"""Turn off the black hole overlay for the current session.

The overlay (overlay.py) runs as a windowless ``pythonw`` process kept
single-instance by a named mutex. Its render loop exits cleanly when the
GLFW window titled "blackhole-overlay" receives WM_CLOSE -- that path stops
the Windows.Graphics.Capture thread and tears down GLFW. So we ask nicely
first (WM_CLOSE), then fall back to terminating any leftover overlay.py
process (covers the brief pre-window "waiting for Alacritty" phase, or a
hung overlay that ignored the close).

This only stops the *currently running* overlay. Alacritty's auto-start
command will launch a fresh one the next time you open a terminal window;
to disable it permanently, remove that Start-Process line from
%APPDATA%\\alacritty\\alacritty.toml.

Usage:
    python quit.py
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes

OVERLAY_TITLE = "blackhole-overlay"
WM_CLOSE = 0x0010
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.IsWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def overlay_hwnd():
    hwnd = user32.FindWindowW(None, OVERLAY_TITLE)
    return hwnd if hwnd else None


def pid_for_hwnd(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


def terminate_pid(pid):
    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    ok = bool(kernel32.TerminateProcess(h, 0))
    kernel32.CloseHandle(h)
    return ok


def kill_overlay_processes():
    """Fallback: terminate any python(w) process running overlay.py.

    Uses a CIM query because a process's command line isn't available from
    the ToolHelp snapshot. Catches the overlay before it has created a
    window, or one that ignored WM_CLOSE.
    """
    import subprocess
    ps = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='python.exe' OR Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -match 'overlay\\.py' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    return len([line for line in out.stdout.splitlines() if line.strip()])


def main():
    # 1) Graceful: ask the overlay window to close (its normal exit path).
    closed_any = False
    hwnd = overlay_hwnd()
    pid = pid_for_hwnd(hwnd) if hwnd else None
    while hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        closed_any = True
        nxt = overlay_hwnd()
        if nxt == hwnd:  # same window still up; stop spamming, wait it out
            break
        hwnd = nxt

    if closed_any:
        # Give the render loop time to process WM_CLOSE and tear down.
        for _ in range(30):  # up to ~3 s
            if not overlay_hwnd():
                break
            time.sleep(0.1)

    # 2) Fallback: if the window lingered, hard-kill its process; also sweep
    #    for any overlay process with no window yet (pre-Alacritty wait).
    if overlay_hwnd() and pid:
        terminate_pid(pid)
        time.sleep(0.2)

    killed = kill_overlay_processes()

    if closed_any or killed:
        print("Black hole overlay stopped for this session.")
        print("It will start again the next time you open Alacritty.")
    else:
        print("No running black hole overlay found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
