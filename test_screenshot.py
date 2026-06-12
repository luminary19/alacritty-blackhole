"""Grab one monitor frame via WGC and save it as PNG (includes overlays)."""
import sys
import threading

from windows_capture import WindowsCapture

out = sys.argv[1] if len(sys.argv) > 1 else "screen.png"
done = threading.Event()
cap = WindowsCapture(cursor_capture=False, monitor_index=1)


@cap.event
def on_frame_arrived(frame, ctl):
    if not done.is_set():
        frame.save_as_image(out)
        done.set()
        ctl.stop()


@cap.event
def on_closed():
    done.set()


ctl = cap.start_free_threaded()
if not done.wait(timeout=10):
    ctl.stop()
    sys.exit("no frame")
print("saved", out)
