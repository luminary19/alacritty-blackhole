"""Smoke tests: shader compiles headlessly; Alacritty window is capturable."""
import sys
import threading
import time

sys.path.insert(0, r"C:\Users\hoang\Lumicity\Projects\Personal\alacritty-blackhole")
import overlay


def test_shader():
    import moderngl
    ctx = moderngl.create_context(standalone=True)
    prog = overlay.build_program(ctx)
    print("shader compiled OK; active uniforms:", sorted(n for n in prog))
    ctx.release()


def test_capture():
    hwnd = overlay.find_alacritty_hwnd()
    print("alacritty hwnd:", hwnd)
    if not hwnd:
        print("SKIP capture test (no Alacritty window)")
        return
    print("rect:", overlay.window_rect(hwnd))

    from windows_capture import WindowsCapture
    got = {}
    done = threading.Event()
    cap = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)

    @cap.event
    def on_frame_arrived(frame, ctl):
        got["shape"] = frame.frame_buffer.shape
        got["dtype"] = str(frame.frame_buffer.dtype)
        done.set()
        ctl.stop()

    @cap.event
    def on_closed():
        done.set()

    ctl = cap.start_free_threaded()
    if not done.wait(timeout=10):
        ctl.stop()
        raise RuntimeError("no frame within 10 s")
    print("captured frame:", got)


if __name__ == "__main__":
    test_shader()
    test_capture()
    print("ALL OK")
