import cv2
import mss
import numpy as np
import threading
import time
import math
import os
from collections import deque
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

try:
    from pynput import keyboard
except Exception:
    keyboard = None

try:
    from screeninfo import get_monitors
except Exception:
    get_monitors = None


# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

CAPTURE_MONITOR_INDEX = 1   # mss monitor 1 = first physical monitor
CAPTURE_FPS = 30
ROLLING_SECONDS = 5
CLIP_WIDTH = 1280
ANALYSIS_WIDTH = 480
JPEG_QUALITY = 75

# F8 marks the end of an engagement and analyzes the recent buffer.
MARK_FIGHT_KEY = "f8"

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)


# ------------------------------------------------------------
# SHARED STATE
# ------------------------------------------------------------

state_lock = threading.Lock()
running = True

live_state = {
    "fps": 0.0,
    "dx": 0.0,
    "dy": 0.0,
    "speed": 0.0,
    "stability": 100.0,
    "jitter": 0.0,
    "message": "Starting capture...",
    "last_fight": None,
}

# Each item:
# (timestamp, jpeg_bytes, dx, dy, speed)
frame_buffer = deque(maxlen=CAPTURE_FPS * ROLLING_SECONDS)

# Recent motion history for live metrics.
motion_history = deque(maxlen=CAPTURE_FPS * 2)


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

def resize_keep_aspect(frame, target_width):
    h, w = frame.shape[:2]
    if w <= target_width:
        return frame
    scale = target_width / float(w)
    return cv2.resize(frame, (target_width, int(h * scale)), interpolation=cv2.INTER_AREA)


def preprocess_for_motion(frame):
    small = resize_keep_aspect(frame, ANALYSIS_WIDTH)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    # Remove HUD-heavy outer edges so camera motion dominates more.
    h, w = gray.shape
    x1, x2 = int(w * 0.18), int(w * 0.82)
    y1, y2 = int(h * 0.18), int(h * 0.82)
    crop = gray[y1:y2, x1:x2]

    return np.float32(crop)


def estimate_motion(prev_gray, curr_gray):
    if prev_gray is None or curr_gray is None:
        return 0.0, 0.0, 0.0

    # phaseCorrelate estimates the translation between images.
    # Scene motion is used here as a proxy for camera/aim movement.
    try:
        (shift_x, shift_y), response = cv2.phaseCorrelate(prev_gray, curr_gray)
        if not np.isfinite(shift_x) or not np.isfinite(shift_y):
            return 0.0, 0.0, 0.0
        if response < 0.03:
            return 0.0, 0.0, response
        return float(shift_x), float(shift_y), float(response)
    except cv2.error:
        return 0.0, 0.0, 0.0


def compute_live_metrics():
    with state_lock:
        hist = list(motion_history)

    if len(hist) < 5:
        return 100.0, 0.0

    speeds = np.array([h[2] for h in hist], dtype=np.float32)
    dxs = np.array([h[0] for h in hist], dtype=np.float32)
    dys = np.array([h[1] for h in hist], dtype=np.float32)

    recent = speeds[-min(len(speeds), CAPTURE_FPS):]

    mean_speed = float(np.mean(recent))
    speed_jitter = float(np.std(recent))

    # Direction-change jitter: frequent sign changes imply micro-corrections.
    def sign_changes(arr):
        arr = arr[np.abs(arr) > 0.08]
        if len(arr) < 2:
            return 0
        s = np.sign(arr)
        return int(np.sum(s[1:] != s[:-1]))

    reversals = sign_changes(dxs[-CAPTURE_FPS:]) + sign_changes(dys[-CAPTURE_FPS:])
    jitter = speed_jitter + reversals * 0.15

    # 100 = very steady, 0 = highly active/jittery.
    stability = 100.0 - (mean_speed * 10.0 + jitter * 7.0)
    stability = max(0.0, min(100.0, stability))

    return stability, jitter


def make_coaching_message(speed, stability, jitter):
    # This deliberately coaches general camera/aim control only.
    if speed > 4.5:
        return "Large camera movement — settle before taking the shot."
    if stability < 40:
        return "Aim is very active — reduce unnecessary micro-corrections."
    if jitter > 1.8:
        return "Frequent reversals detected — try one clean correction, then settle."
    if stability > 82 and speed < 1.2:
        return "Stable aim — good time to focus on deliberate first-shot timing."
    if speed < 2.0:
        return "Controlled movement — keep transitions smooth and stop cleanly."
    return "Moderate movement — avoid correcting back and forth."


def annotate_frame(frame, dx, dy, speed):
    out = frame.copy()
    h, w = out.shape[:2]
    cx, cy = w // 2, h // 2

    # Center reference only; no opponent detection.
    cv2.drawMarker(out, (cx, cy), (255, 255, 255),
                   markerType=cv2.MARKER_CROSS, markerSize=24, thickness=2)

    # Motion vector shows camera movement, not target direction.
    scale = 8
    end = (
        int(cx + max(-80, min(80, dx * scale))),
        int(cy + max(-80, min(80, dy * scale)))
    )
    cv2.arrowedLine(out, (cx, cy), end, (255, 255, 255), 2, tipLength=0.25)

    cv2.putText(out, f"camera dx={dx:+.2f} dy={dy:+.2f} speed={speed:.2f}",
                (20, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (255, 255, 255), 2, cv2.LINE_AA)

    return out


def analyze_recent_fight():
    with state_lock:
        items = list(frame_buffer)

    if len(items) < max(15, CAPTURE_FPS):
        with state_lock:
            live_state["last_fight"] = {
                "status": "Not enough buffered footage yet."
            }
        return

    # Focus on last ~2 seconds for movement summary.
    recent = items[-min(len(items), CAPTURE_FPS * 2):]
    dxs = np.array([x[2] for x in recent], dtype=np.float32)
    dys = np.array([x[3] for x in recent], dtype=np.float32)
    speeds = np.array([x[4] for x in recent], dtype=np.float32)

    avg_speed = float(np.mean(speeds))
    peak_speed = float(np.max(speeds))
    final_speed = float(np.mean(speeds[-max(3, CAPTURE_FPS // 5):]))

    def count_reversals(arr):
        arr = arr[np.abs(arr) > 0.10]
        if len(arr) < 2:
            return 0
        s = np.sign(arr)
        return int(np.sum(s[1:] != s[:-1]))

    horizontal_reversals = count_reversals(dxs)
    vertical_reversals = count_reversals(dys)
    total_reversals = horizontal_reversals + vertical_reversals

    # Estimate whether the camera settled in the final 250 ms.
    settle_threshold = 1.2
    settled_frames = 0
    for v in speeds[::-1]:
        if v < settle_threshold:
            settled_frames += 1
        else:
            break

    settle_ms = int((settled_frames / CAPTURE_FPS) * 1000)

    if total_reversals >= 8:
        diagnosis = "Heavy micro-correction: too many direction reversals."
    elif peak_speed > 7.0 and final_speed > 2.0:
        diagnosis = "Large movement did not fully settle."
    elif peak_speed > 7.0 and final_speed <= 1.4:
        diagnosis = "Fast movement with a clean stop."
    elif avg_speed < 1.6:
        diagnosis = "Controlled engagement movement."
    else:
        diagnosis = "Moderate correction pattern."

    fight_summary = {
        "status": "ok",
        "avg_speed": avg_speed,
        "peak_speed": peak_speed,
        "final_speed": final_speed,
        "reversals": total_reversals,
        "horizontal_reversals": horizontal_reversals,
        "vertical_reversals": vertical_reversals,
        "settle_ms": settle_ms,
        "diagnosis": diagnosis,
    }

    with state_lock:
        live_state["last_fight"] = fight_summary

    save_recent_clip(items)


def save_recent_clip(items):
    if not items:
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    video_path = OUTPUT_DIR / f"fight_{stamp}.mp4"
    csv_path = OUTPUT_DIR / f"fight_{stamp}.csv"

    first = cv2.imdecode(np.frombuffer(items[0][1], np.uint8), cv2.IMREAD_COLOR)
    if first is None:
        return

    h, w = first.shape[:2]
    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        CAPTURE_FPS,
        (w, h)
    )

    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("timestamp,dx,dy,speed\n")
        for ts, jpeg_bytes, dx, dy, speed in items:
            frame = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            frame = annotate_frame(frame, dx, dy, speed)
            writer.write(frame)
            f.write(f"{ts:.6f},{dx:.6f},{dy:.6f},{speed:.6f}\n")

    writer.release()


# ------------------------------------------------------------
# CAPTURE THREAD
# ------------------------------------------------------------

def capture_loop():
    global running

    prev_gray = None
    ema_dx = 0.0
    ema_dy = 0.0
    ema_speed = 0.0
    fps_times = deque(maxlen=60)

    with mss.mss() as sct:
        monitors = sct.monitors

        monitor_index = CAPTURE_MONITOR_INDEX
        if monitor_index <= 0 or monitor_index >= len(monitors):
            monitor_index = 1

        mon = monitors[monitor_index]

        frame_period = 1.0 / CAPTURE_FPS

        while running:
            start = time.perf_counter()

            raw = np.array(sct.grab(mon))
            frame = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)

            gray = preprocess_for_motion(frame)
            dx, dy, response = estimate_motion(prev_gray, gray)
            prev_gray = gray

            # Clamp occasional phase-correlation outliers.
            dx = max(-20.0, min(20.0, dx))
            dy = max(-20.0, min(20.0, dy))

            alpha = 0.35
            ema_dx = alpha * dx + (1 - alpha) * ema_dx
            ema_dy = alpha * dy + (1 - alpha) * ema_dy
            speed = math.hypot(ema_dx, ema_dy)
            ema_speed = alpha * speed + (1 - alpha) * ema_speed

            with state_lock:
                motion_history.append((ema_dx, ema_dy, ema_speed))

            stability, jitter = compute_live_metrics()
            message = make_coaching_message(ema_speed, stability, jitter)

            clip_frame = resize_keep_aspect(frame, CLIP_WIDTH)
            ok, encoded = cv2.imencode(
                ".jpg",
                clip_frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
            )

            now = time.perf_counter()
            fps_times.append(now)
            if len(fps_times) >= 2:
                current_fps = (len(fps_times) - 1) / (fps_times[-1] - fps_times[0])
            else:
                current_fps = 0.0

            with state_lock:
                live_state["fps"] = current_fps
                live_state["dx"] = ema_dx
                live_state["dy"] = ema_dy
                live_state["speed"] = ema_speed
                live_state["stability"] = stability
                live_state["jitter"] = jitter
                live_state["message"] = message

                if ok:
                    frame_buffer.append(
                        (time.time(), encoded.tobytes(), ema_dx, ema_dy, ema_speed)
                    )

            elapsed = time.perf_counter() - start
            sleep_for = frame_period - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)


# ------------------------------------------------------------
# GLOBAL HOTKEY
# ------------------------------------------------------------

def start_hotkey_listener():
    if keyboard is None:
        return None

    def on_press(key):
        try:
            if key == keyboard.Key.f8:
                threading.Thread(target=analyze_recent_fight, daemon=True).start()
        except Exception:
            pass

    listener = keyboard.Listener(on_press=on_press)
    listener.daemon = True
    listener.start()
    return listener


# ------------------------------------------------------------
# DASHBOARD
# ------------------------------------------------------------

class Dashboard:
    def __init__(self, root):
        self.root = root
        root.title("Live Aim Coach")
        root.configure(bg="#111318")
        root.minsize(520, 620)

        self.place_on_second_monitor()

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TFrame", background="#111318")
        style.configure("TLabel", background="#111318", foreground="#f4f4f5")
        style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"))
        style.configure("Big.TLabel", font=("Segoe UI", 32, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Muted.TLabel", foreground="#a1a1aa", font=("Segoe UI", 10))
        style.configure("Coach.TLabel", foreground="#ffffff", font=("Segoe UI", 15, "bold"), wraplength=460)
        style.configure("Value.TLabel", font=("Consolas", 15))
        style.configure("TButton", padding=10)

        main = ttk.Frame(root, padding=22)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="LIVE AIM COACH", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            main,
            text="General camera/aim-control feedback only — no live enemy detection.",
            style="Muted.TLabel"
        ).pack(anchor="w", pady=(2, 18))

        self.stability_label = ttk.Label(main, text="--", style="Big.TLabel")
        self.stability_label.pack(anchor="w")
        ttk.Label(main, text="Aim stability (0–100)", style="Muted.TLabel").pack(anchor="w")

        self.progress = ttk.Progressbar(main, maximum=100, length=470)
        self.progress.pack(fill="x", pady=(6, 18))

        metrics = ttk.Frame(main)
        metrics.pack(fill="x", pady=(0, 14))

        self.dx_label = ttk.Label(metrics, text="X: --", style="Value.TLabel")
        self.dx_label.grid(row=0, column=0, sticky="w", padx=(0, 30))
        self.dy_label = ttk.Label(metrics, text="Y: --", style="Value.TLabel")
        self.dy_label.grid(row=0, column=1, sticky="w", padx=(0, 30))
        self.speed_label = ttk.Label(metrics, text="Speed: --", style="Value.TLabel")
        self.speed_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.fps_label = ttk.Label(metrics, text="Capture: -- fps", style="Value.TLabel")
        self.fps_label.grid(row=1, column=1, sticky="w", pady=(8, 0))

        ttk.Separator(main).pack(fill="x", pady=12)

        ttk.Label(main, text="CURRENT COACHING", style="Section.TLabel").pack(anchor="w")
        self.coach_label = ttk.Label(main, text="Starting...", style="Coach.TLabel")
        self.coach_label.pack(anchor="w", pady=(8, 16))

        ttk.Separator(main).pack(fill="x", pady=12)

        ttk.Label(main, text="LAST MARKED ENGAGEMENT", style="Section.TLabel").pack(anchor="w")
        self.fight_label = ttk.Label(
            main,
            text="Press F8 immediately after a fight ends,\nor click “Mark Fight”.",
            style="Value.TLabel",
            justify="left"
        )
        self.fight_label.pack(anchor="w", pady=(8, 16))

        btns = ttk.Frame(main)
        btns.pack(fill="x", pady=(8, 0))

        mark_btn = ttk.Button(btns, text="Mark Fight (F8)", command=self.mark_fight)
        mark_btn.pack(side="left")

        open_btn = ttk.Button(btns, text="Open Output Folder", command=self.open_output)
        open_btn.pack(side="left", padx=(10, 0))

        ttk.Label(
            main,
            text="Tip: put this window on monitor 2 and keep Valorant on monitor 1.",
            style="Muted.TLabel"
        ).pack(anchor="w", pady=(18, 0))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.update_ui()

    def place_on_second_monitor(self):
        if get_monitors is None:
            self.root.geometry("560x680")
            return

        try:
            monitors = get_monitors()
            if len(monitors) >= 2:
                m = monitors[1]
                x = m.x + 40
                y = m.y + 40
                self.root.geometry(f"560x680+{x}+{y}")
            else:
                self.root.geometry("560x680")
        except Exception:
            self.root.geometry("560x680")

    def mark_fight(self):
        threading.Thread(target=analyze_recent_fight, daemon=True).start()

    def open_output(self):
        try:
            os.startfile(OUTPUT_DIR.resolve())
        except Exception as e:
            messagebox.showerror("Could not open folder", str(e))

    def update_ui(self):
        if not running:
            return

        with state_lock:
            s = dict(live_state)

        stability = float(s["stability"])
        self.stability_label.config(text=f"{stability:0.0f}")
        self.progress["value"] = stability
        self.dx_label.config(text=f"X motion: {s['dx']:+.2f}")
        self.dy_label.config(text=f"Y motion: {s['dy']:+.2f}")
        self.speed_label.config(text=f"Speed: {s['speed']:.2f}")
        self.fps_label.config(text=f"Capture: {s['fps']:.0f} fps")
        self.coach_label.config(text=s["message"])

        fight = s.get("last_fight")
        if fight:
            if fight.get("status") != "ok":
                fight_text = fight.get("status", "No result.")
            else:
                fight_text = (
                    f"Avg movement:   {fight['avg_speed']:.2f}\n"
                    f"Peak movement:  {fight['peak_speed']:.2f}\n"
                    f"Final movement: {fight['final_speed']:.2f}\n"
                    f"Reversals:      {fight['reversals']}\n"
                    f"Final settle:   {fight['settle_ms']} ms\n\n"
                    f"{fight['diagnosis']}"
                )
            self.fight_label.config(text=fight_text)

        self.root.after(100, self.update_ui)

    def on_close(self):
        global running
        running = False
        self.root.destroy()


def main():
    global running

    capture_thread = threading.Thread(target=capture_loop, daemon=True)
    capture_thread.start()

    hotkey_listener = start_hotkey_listener()

    root = tk.Tk()
    Dashboard(root)

    try:
        root.mainloop()
    finally:
        running = False
        if hotkey_listener is not None:
            try:
                hotkey_listener.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()
