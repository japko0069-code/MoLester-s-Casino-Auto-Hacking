"""
Fingerprint puzzle solver.

Flow: press the configured trigger key when the hacking screen opens ->
polls the top-left fragment box for up to PRESENCE_WINDOW seconds ->
the instant a puzzle is detected, solves it (matches & marks the 4
correct fragments, presses Tab to submit) -> waits for the
PROCESSING/SIGNAL MATCH overlay to appear and then fully clear (so the
still-visible old fragments never get mistaken for a new layout) ->
polls for a chained puzzle -> repeats until a poll window elapses with
nothing detected -> back to idle, awaiting the next trigger press.

Press F8 to quit.

NOTE: assumes the selector always starts on the top-left square when a
puzzle opens (no homing step).

Run setup.py first to generate config.json.
"""

import os
import sys
import json
import time
import threading
import itertools
import concurrent.futures
import ctypes
from ctypes import wintypes
import mss
import numpy as np
import cv2
from pynput.keyboard import Key, Listener

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------
if not os.path.exists(CONFIG_PATH):
    print("ERROR: config.json not found.")
    print("Please run setup.py first:")
    print("    python setup.py")
    input("\nPress Enter to close...")
    sys.exit(1)

with open(CONFIG_PATH, encoding="utf-8") as _f:
    _cfg = json.load(_f)

GAME_MONITOR_INDEX  = _cfg["game_monitor_index"]
TRIGGER_KEY_VK      = _cfg["trigger_key_vk"]
TRIGGER_KEY_NAME    = _cfg.get("trigger_key_name", f"VK {TRIGGER_KEY_VK}")
KEY_DELAY           = _cfg.get("key_delay", 0.02)
HOLD_DURATION       = _cfg.get("hold_duration", 0.04)
PRESENCE_WINDOW     = _cfg.get("presence_window", 5.0)

# ---------------------------------------------------------------------------
# Fixed constants (calibrated from real captured frames - resolution-
# independent since they are stored as fractions of screen width/height)
# ---------------------------------------------------------------------------
CLONE_TARGET_FRAC = (0.4508, 0.1333, 0.7461, 0.7014)
GRID_COLS_FRAC = [(0.2477, 0.3102), (0.3227, 0.3852)]
GRID_ROWS_FRAC = [(0.2500, 0.3611), (0.3833, 0.4944), (0.5167, 0.6306), (0.6528, 0.7639)]

BIG_DOWNSCALE = 0.5
SCALE_LO = 0.4
SCALE_HI = 2.0
COARSE_N = 18
FINE_N = 16
FINE_WINDOW_STEPS = 2

PRESENCE_STD_THRESHOLD = 15.0
PRESENCE_POLL_INTERVAL = 0.1
PRESENCE_DEBOUNCE_COUNT = 2
PRESENCE_INITIAL_SKIP = 0.2

CHECK_OVERLAY_FRAC = (995 / 2560, 640 / 1440, 1565 / 2560, 795 / 1440)
CHECK_BUSY_STD_THRESHOLD = 40.0
CHECK_SETTLE_POLL_INTERVAL = 0.15
CHECK_BUSY_APPEAR_TIMEOUT = 3.0
CHECK_BUSY_CLEAR_TIMEOUT = 15.0

CONNECTION_TIMEOUT_DIGITS_FRAC = (400 / 2560, 150 / 1440, 900 / 2560, 270 / 1440)
HACK_ACTIVE_COLOR_FRAC_THRESHOLD = 0.02

# --- Input lock (blocks real W/A/S/D/Enter presses while a solve is running) ---
# Windows can't cleanly tell "real" and "synthetic" keypresses apart on its
# own, so we tag every keystroke WE send with this marker via SendInput's
# extra-info field, and a low-level keyboard hook lets anything carrying the
# marker through while swallowing everything else on the locked keys.
BOT_INPUT_MARKER = 0xABCDEF01

VK_W, VK_A, VK_S, VK_D = 0x57, 0x41, 0x53, 0x44
VK_RETURN = 0x0D
VK_TAB = 0x09
LOCKED_VKS = {VK_W, VK_A, VK_S, VK_D, VK_RETURN}  # Tab intentionally left unlocked

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

input_locked = False


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class _INPUT_union(ctypes.Union):
    # All three real members must be present, not just `ki` - the union's
    # size has to match Windows' actual INPUT struct (determined by its
    # largest member, MOUSEINPUT) or SendInput silently rejects every call
    # because the cbSize we pass won't match what it expects.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_union)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MAPVK_VK_TO_VSC = 0

user32.SendInput.restype = wintypes.UINT
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)


def _send_key(vk, key_up=False):
    """Sends a single synthetic keydown/keyup for vk, tagged with
    BOT_INPUT_MARKER so the input-lock hook lets it through even while real
    presses on the same key are being swallowed. Includes the real scan
    code (like pynput's backend does) - some UIs silently ignore SendInput
    events that carry a vk code but no scan code."""
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_KEYUP if key_up else 0
    ki = KEYBDINPUT(vk, scan, flags, 0, ctypes.c_void_p(BOT_INPUT_MARKER))
    inp = INPUT(INPUT_KEYBOARD, _INPUT_union(ki=ki))
    sent = user32.SendInput(1, ctypes.pointer(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        print(f"[WARN] SendInput reported {sent} events injected (expected 1) "
              f"for vk={vk:#x} key_up={key_up} - keystroke likely did not register.")


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
_KEY_MSGS = (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP)

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# Explicit argtypes/restype for every WinAPI call the hook uses - without
# these, ctypes has to guess the calling convention for the HOOKPROC
# callback and can throw a confusing ArgumentError on some Python builds.
# Both restypes below use c_ssize_t (pointer-width) rather than c_long,
# since the real Windows type is LRESULT - on 64-bit, declaring it too
# narrow can leave garbage in the returned value's upper bits, which
# Windows can misread as "block this event" and silently eat all keyboard
# input system-wide.
HHOOK = wintypes.HANDLE
user32.SetWindowsHookExW.restype = HHOOK
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = (HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = (HHOOK,)
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint)
user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)


def _low_level_keyboard_proc(nCode, wParam, lParam):
    if nCode == 0 and input_locked and wParam in _KEY_MSGS:
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        if kb.vkCode in LOCKED_VKS and kb.dwExtraInfo != BOT_INPUT_MARKER:
            return 1  # swallow: real human press on a locked key, no marker
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


_hook_proc_ref = HOOKPROC(_low_level_keyboard_proc)  # keep alive - GC'd callback = crash
_hook_handle = None


def _hook_thread_main():
    global _hook_handle
    _hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_proc_ref,
                                             kernel32.GetModuleHandleW(None), 0)
    if not _hook_handle:
        print("[WARN] Failed to install input-lock hook - solves will run unprotected "
              "against your own keypresses.")
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


playing = False


def frac_box_to_px(frac_box, width, height):
    x1, y1, x2, y2 = frac_box
    return int(x1 * width), int(y1 * height), int(x2 * width), int(y2 * height)


def get_grid_boxes(width, height):
    boxes = []
    for r, (y1f, y2f) in enumerate(GRID_ROWS_FRAC):
        for c, (x1f, x2f) in enumerate(GRID_COLS_FRAC):
            x1, y1, x2, y2 = int(x1f * width), int(y1f * height), int(x2f * width), int(y2f * height)
            boxes.append((r, c, x1, y1, x2, y2))
    return boxes


def capture_frame(sct):
    mon = sct.monitors[GAME_MONITOR_INDEX]
    shot = sct.grab(mon)
    return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)


def fragments_present_in_frame(frame):
    """Cheap presence probe: checks only the top-left fragment box (row 0,
    col 0) for texture, rather than analyzing the whole frame."""
    h, w = frame.shape[:2]
    x1f, x2f = GRID_COLS_FRAC[0]
    y1f, y2f = GRID_ROWS_FRAC[0]
    x1, y1, x2, y2 = int(x1f * w), int(y1f * h), int(x2f * w), int(y2f * h)
    crop = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    return float(crop.std()) > PRESENCE_STD_THRESHOLD


# --- Hacking-UI-active detection (measured from real captured frames) ---
# The CONNECTION TIMEOUT digits are part of the hacking chrome itself -
# present through every phase of an active session (idle-puzzle,
# PROCESSING, SIGNAL MATCH) and gone entirely the instant you're back in
# the game. Detected by color (coral-red digits, ~[R,G,B]=[216,114,123])
# rather than generic brightness/texture, since that's far less likely to
# accidentally match arbitrary game-world content at this same screen
# position than a texture-only check would be.


def hack_ui_active(frame):
    """True if the hacking UI chrome is currently on screen at all."""
    h, w = frame.shape[:2]
    x1f, y1f, x2f, y2f = CONNECTION_TIMEOUT_DIGITS_FRAC
    x1, y1, x2, y2 = int(x1f * w), int(y1f * h), int(x2f * w), int(y2f * h)
    crop = frame[y1:y2, x1:x2].astype(np.float64)
    b, g, r = crop[:, :, 0], crop[:, :, 1], crop[:, :, 2]
    mask = (r > 120) & (r > g * 1.3) & (r > b * 1.3)
    return (mask.sum() / mask.size) > HACK_ACTIVE_COLOR_FRAC_THRESHOLD


def poll_for_puzzle(timeout=PRESENCE_WINDOW, require_active_hack_ui=False):
    """Polls for up to `timeout` seconds, returning True the moment a
    puzzle is confirmed (debounced), or False if the window elapses with
    nothing detected.

    require_active_hack_ui: when True, exits immediately (returns False)
    the moment the hacking UI chrome is confirmed absent, instead of
    waiting out the rest of the window - used for chain-continuation
    polls, where the session was definitely already open and its
    disappearance means the hack has actually ended (this is what stops
    an exited session from still being read as "maybe another puzzle").
    Left False for the very first poll after a trigger, since the hacking
    screen may not have visually opened yet at that point.
    """
    start = time.time()
    consecutive_hits = 0
    with mss.mss() as sct:
        while time.time() - start < timeout:
            elapsed = time.time() - start
            frame = capture_frame(sct)

            if require_active_hack_ui and elapsed >= PRESENCE_INITIAL_SKIP and not hack_ui_active(frame):
                print(f"[CHECK] Hacking UI no longer active after {elapsed:.2f}s - stopping poll early.")
                return False

            if elapsed >= PRESENCE_INITIAL_SKIP:
                if fragments_present_in_frame(frame):
                    consecutive_hits += 1
                    if consecutive_hits >= PRESENCE_DEBOUNCE_COUNT:
                        return True
                else:
                    consecutive_hits = 0
            time.sleep(PRESENCE_POLL_INTERVAL)
    return False


def check_overlay_busy(sct):
    """True if the PROCESSING/SIGNAL MATCH overlay is currently showing."""
    frame = capture_frame(sct)
    h, w = frame.shape[:2]
    x1f, y1f, x2f, y2f = CHECK_OVERLAY_FRAC
    x1, y1, x2, y2 = int(x1f * w), int(y1f * h), int(x2f * w), int(y2f * h)
    crop = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    return float(crop.std()) > CHECK_BUSY_STD_THRESHOLD


def wait_for_check_to_settle():
    """Call right after submitting (Tab). Waits for the PROCESSING/SIGNAL
    MATCH overlay to appear (confirms the submit registered) and then waits
    for it to disappear again (the whole check+result cycle is done) before
    we go back to looking at the fragment panel. This is what prevents the
    still-visible old fragments from being mistaken for a new puzzle while
    a check is still running."""
    with mss.mss() as sct:
        start = time.time()
        appeared = False
        while time.time() - start < CHECK_BUSY_APPEAR_TIMEOUT:
            if check_overlay_busy(sct):
                appeared = True
                break
            time.sleep(CHECK_SETTLE_POLL_INTERVAL)

        if not appeared:
            print("[CHECK] WARNING: no processing overlay seen after submit - "
                  "Tab press may not have registered. Proceeding anyway.")
            return

        print("[CHECK] Processing/result overlay detected, waiting for it to clear...")
        start = time.time()
        while time.time() - start < CHECK_BUSY_CLEAR_TIMEOUT:
            if not check_overlay_busy(sct):
                print(f"[CHECK] Overlay cleared after {time.time() - start:.2f}s.")
                return
            time.sleep(CHECK_SETTLE_POLL_INTERVAL)

        print("[CHECK] WARNING: overlay still busy after timeout - proceeding anyway.")


def multiscale_match_score(big_gray, fragment_gray, debug=False):
    """Coarse-to-fine scale search.

    Stage 1 (coarse): evenly spaced scales across the full range - cheap,
    just enough to locate roughly where the true best-fit scale lives.

    Stage 2 (fine): a dense scan zoomed into the window immediately around
    the coarse peak (one coarse step on either side) - gives near-continuous
    scale resolution right where it matters, instead of paying that cost
    across the whole range. This is what fixes the near-tie misses: a
    genuine match's true peak can fall *between* coarse steps and get
    understated enough for a texture-similar filler to edge it out.
    """
    bh, bw = big_gray.shape
    fh0, fw0 = fragment_gray.shape

    def score_at(s):
        scale = s * BIG_DOWNSCALE
        fw, fh = max(8, int(fw0 * scale)), max(8, int(fh0 * scale))
        if fw > bw or fh > bh:
            return -1.0
        resized_frag = cv2.resize(fragment_gray, (fw, fh))
        res = cv2.matchTemplate(big_gray, resized_frag, cv2.TM_CCOEFF_NORMED)
        return float(res.max())

    coarse_scales = np.linspace(SCALE_LO, SCALE_HI, COARSE_N)
    coarse_scores = [score_at(s) for s in coarse_scales]
    best_idx = int(np.argmax(coarse_scores))
    best_coarse_scale = coarse_scales[best_idx]
    best_coarse_score = coarse_scores[best_idx]

    step = (SCALE_HI - SCALE_LO) / (COARSE_N - 1)
    fine_lo = max(SCALE_LO, best_coarse_scale - step * FINE_WINDOW_STEPS)
    fine_hi = min(SCALE_HI, best_coarse_scale + step * FINE_WINDOW_STEPS)
    fine_scales = np.linspace(fine_lo, fine_hi, FINE_N)
    fine_scores = [score_at(s) for s in fine_scales]

    best = max(best_coarse_score, max(fine_scores))
    if debug:
        return best, best_coarse_scale
    return best


KEY_TO_VK = {"w": VK_W, "a": VK_A, "s": VK_S, "d": VK_D, Key.enter: VK_RETURN, Key.tab: VK_TAB}


def optimal_visit_order(targets, start=(0, 0)):
    """Finds the movement-tap-minimizing order to visit all 4 target cells
    from `start`. Only 24 possible orderings for 4 targets, so brute-forcing
    every permutation is essentially free and guaranteed optimal - no need
    for a heuristic here."""
    best_order, best_cost = None, None
    for perm in itertools.permutations(targets):
        cost = 0
        cur = start
        for t in perm:
            cost += abs(t["row"] - cur[0]) + abs(t["col"] - cur[1])
            cur = (t["row"], t["col"])
        if best_cost is None or cost < best_cost:
            best_cost, best_order = cost, perm
    return list(best_order), best_cost


def tap(key):
    vk = KEY_TO_VK[key]
    _send_key(vk, key_up=False)
    time.sleep(0.04)
    _send_key(vk, key_up=True)
    time.sleep(KEY_DELAY)


def solve_once():
    # Note: `playing` is owned by run_chain() for the duration of the whole
    # trigger-poll-solve-poll chain, not by this function - a solve is just
    # one step inside that chain, and chained puzzles need the guard to
    # stay up across multiple solves.
    global input_locked
    input_locked = True
    try:
        print("\n[SOLVE] Capturing and matching...")
        match_start = time.time()
        with mss.mss() as sct:
            frame = capture_frame(sct)
        height, width = frame.shape[:2]

        cx1, cy1, cx2, cy2 = frac_box_to_px(CLONE_TARGET_FRAC, width, height)
        big_gray = cv2.cvtColor(frame[cy1:cy2, cx1:cx2], cv2.COLOR_BGR2GRAY)
        big_gray = cv2.resize(big_gray, (0, 0), fx=BIG_DOWNSCALE, fy=BIG_DOWNSCALE)

        boxes = get_grid_boxes(width, height)

        def match_one(box):
            r, c, x1, y1, x2, y2 = box
            frag_gray = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
            score = multiscale_match_score(big_gray, frag_gray)
            return {"row": r, "col": c, "score": score}

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(match_one, boxes))

        results.sort(key=lambda d: d["score"], reverse=True)

        # Diagnostic: how close was the cutoff between 4th and 5th place?
        # A small margin here means this frame was a genuine near-tie -
        # useful to know if misses ever come back.
        margin = results[3]["score"] - results[4]["score"]
        print(f"[SOLVE] Matching took {time.time() - match_start:.2f}s "
              f"(rank4/rank5 margin: {margin:.3f})")

        targets = results[:4]
        targets, move_cost = optimal_visit_order(targets, start=(0, 0))
        print(f"[SOLVE] Targets (optimal order, {move_cost} moves): "
              f"{[(t['row'], t['col']) for t in targets]}")

        cur_row, cur_col = 0, 0  # assumed starting position: top-left

        for t in targets:
            dr = t["row"] - cur_row
            dc = t["col"] - cur_col
            step = "s" if dr > 0 else "w"
            for _ in range(abs(dr)):
                tap(step)
            step = "d" if dc > 0 else "a"
            for _ in range(abs(dc)):
                tap(step)
            cur_row, cur_col = t["row"], t["col"]
            tap(Key.enter)
            print(f"[SOLVE] Marked ({t['row']},{t['col']})")

        tap(Key.tab)
        print("[SOLVE] Submitted.")

    except Exception:
        import traceback
        print("\n--- AN ERROR OCCURRED DURING SOLVE ---")
        traceback.print_exc()
    finally:
        input_locked = False  # always release, even on exception - never leave WASD/Enter blocked


NUMPAD_PLUS_VK = 107  # VK_ADD - kept as fallback default if config missing


def run_chain():
    """Triggered by the configured key. Polls for a puzzle for up to
    PRESENCE_WINDOW seconds; if found, solves it and immediately polls
    again (catches chained puzzles with no fixed gap assumed). Drops back
    to idle once a poll window elapses with nothing detected, or (for any
    poll after the first) the instant the hacking UI chrome itself is
    confirmed gone."""
    global playing
    playing = True
    try:
        puzzle_num = 0
        is_first_poll = True
        while True:
            print(f"\n[CHECK] Polling for puzzle (up to {PRESENCE_WINDOW:.0f}s)...")
            if not poll_for_puzzle(PRESENCE_WINDOW, require_active_hack_ui=not is_first_poll):
                print("[CHECK] Nothing detected. Back to idle - awaiting Numpad +.")
                break
            is_first_poll = False
            puzzle_num += 1
            print(f"[CHECK] Puzzle #{puzzle_num} detected.")
            solve_once()
            wait_for_check_to_settle()
    finally:
        playing = False


def on_press(key):
    is_trigger = getattr(key, "vk", None) == TRIGGER_KEY_VK
    if is_trigger and not playing:
        threading.Thread(target=run_chain, daemon=True).start()
    elif key == Key.f8:
        print("Exiting.")
        return False


if __name__ == "__main__":
    try:
        print(f"Fingerprint solver ready.")
        print(f"Press {TRIGGER_KEY_NAME!r} when the hacking screen opens. F8 to quit.")
        threading.Thread(target=_hook_thread_main, daemon=True).start()
        time.sleep(0.3)
        with Listener(on_press=on_press) as l:
            l.join()
    except Exception:
        import traceback
        print("\n--- AN ERROR OCCURRED ---")
        traceback.print_exc()
    finally:
        if _hook_handle:
            user32.UnhookWindowsHookEx(_hook_handle)
        input("\nPress Enter to close this window...")
