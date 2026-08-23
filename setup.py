"""
Hruder Simulator Fingerprint Solver - Setup

Run this once before using fingerprint_solver.py.
It will:
  1. Install all required Python packages
  2. Download and install Tesseract OCR (if not already present)
  3. Ask which monitor your game runs on
  4. Ask you to press the key you want to use as the solver trigger
  5. Write config.json that the solver reads at startup

Run with: python setup.py
"""

import os
import sys
import json
import subprocess
import urllib.request
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
TESSERACT_DEFAULT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSERACT_INSTALLER_URL = (
    "https://github.com/UB-Mannheim/tesseract/releases/download/"
    "v5.3.3.20231005/tesseract-ocr-w64-setup-5.3.3.20231005.exe"
)
TESSERACT_INSTALLER_LOCAL = os.path.join(SCRIPT_DIR, "tesseract_installer.exe")

REQUIRED_PACKAGES = ["mss", "opencv-python", "numpy", "pynput"]


def hr(char="-", width=60):
    print(char * width)


def step(n, label):
    hr()
    print(f"  Step {n}: {label}")
    hr()


# ---------------------------------------------------------------------------
# Step 1 - Python packages
# ---------------------------------------------------------------------------
def install_packages():
    step(1, "Installing Python packages")
    for pkg in REQUIRED_PACKAGES:
        print(f"  Installing {pkg}...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  [ERROR] Failed to install {pkg}:\n{result.stderr}")
            sys.exit(1)
        print(f"  {pkg} OK")


# ---------------------------------------------------------------------------
# Step 2 - Tesseract
# ---------------------------------------------------------------------------
def install_tesseract():
    step(2, "Checking Tesseract OCR")
    if os.path.exists(TESSERACT_DEFAULT_PATH):
        print(f"  Tesseract already installed at:\n  {TESSERACT_DEFAULT_PATH}")
        return TESSERACT_DEFAULT_PATH

    print("  Tesseract not found. Downloading installer...")
    print(f"  URL: {TESSERACT_INSTALLER_URL}")
    try:
        def _progress(count, block_size, total_size):
            pct = int(count * block_size * 100 / total_size)
            print(f"\r  Downloading... {pct}%", end="", flush=True)
        urllib.request.urlretrieve(TESSERACT_INSTALLER_URL, TESSERACT_INSTALLER_LOCAL, _progress)
        print()
    except Exception as e:
        print(f"\n  [ERROR] Download failed: {e}")
        print("  Please install Tesseract manually from:")
        print("  https://github.com/UB-Mannheim/tesseract/wiki")
        sys.exit(1)

    print("  Running installer (a window will open - click through it, keep default path)...")
    result = subprocess.run([TESSERACT_INSTALLER_LOCAL], capture_output=False)
    if result.returncode not in (0, 1):  # 1 = cancelled by user
        print(f"  [ERROR] Installer exited with code {result.returncode}")
        sys.exit(1)

    # Clean up installer
    try:
        os.remove(TESSERACT_INSTALLER_LOCAL)
    except OSError:
        pass

    if os.path.exists(TESSERACT_DEFAULT_PATH):
        print(f"  Tesseract installed at: {TESSERACT_DEFAULT_PATH}")
        return TESSERACT_DEFAULT_PATH

    # Non-default path - ask user
    print("  Tesseract doesn't appear to be at the default location.")
    custom = input("  Enter the full path to tesseract.exe: ").strip().strip('"')
    if not os.path.exists(custom):
        print(f"  [ERROR] File not found: {custom}")
        sys.exit(1)
    return custom


# ---------------------------------------------------------------------------
# Step 3 - Monitor selection
# ---------------------------------------------------------------------------
def select_monitor():
    step(3, "Monitor selection")
    try:
        import mss
    except ImportError:
        print("  [ERROR] mss not available - did Step 1 succeed?")
        sys.exit(1)

    with mss.mss() as sct:
        monitors = sct.monitors[1:]  # index 0 is the combined virtual screen

    print("  Detected monitors:")
    for i, m in enumerate(monitors, start=1):
        primary = " (primary)" if m.get("is_primary") else ""
        print(f"    [{i}] {m['width']}x{m['height']} at ({m['left']},{m['top']}){primary}")

    while True:
        try:
            choice = int(input("\n  Which monitor does Hruder Simulator run on? Enter number: "))
            if 1 <= choice <= len(monitors):
                # mss index 0 = combined, so real monitors start at 1
                monitor_index = choice
                print(f"  Selected monitor {choice}: "
                      f"{monitors[choice - 1]['width']}x{monitors[choice - 1]['height']}")
                return monitor_index
            print(f"  Please enter a number between 1 and {len(monitors)}.")
        except ValueError:
            print("  Please enter a number.")


# ---------------------------------------------------------------------------
# Step 4 - Trigger key
# ---------------------------------------------------------------------------
def capture_trigger_key():
    step(4, "Trigger key setup")
    print("  This is the key you press to start the solver when the hacking screen opens.")
    print("  Press any key now (Numpad +, F9, or whatever you prefer)...")
    print()

    try:
        from pynput import keyboard
    except ImportError:
        print("  [ERROR] pynput not available - did Step 1 succeed?")
        sys.exit(1)

    captured = {}

    def on_press(key):
        vk = getattr(key, "vk", None)
        name = getattr(key, "char", None) or str(key)
        captured["vk"] = vk
        captured["name"] = name
        return False  # stop listener

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()

    vk = captured.get("vk")
    name = captured.get("name", "unknown")

    if vk is None:
        print(f"  Could not read virtual key code for '{name}'.")
        print("  Some keys (like media keys) aren't detectable this way.")
        print("  Defaulting to Numpad + (VK 107). Edit config.json later if needed.")
        vk = 107
        name = "Numpad +"

    print(f"  Captured: {name!r} (VK code: {vk})")
    confirm = input("  Use this key? [Y/n]: ").strip().lower()
    if confirm == "n":
        print("  Re-run setup.py to try again.")
        sys.exit(0)
    return vk, name


# ---------------------------------------------------------------------------
# Write config
# ---------------------------------------------------------------------------
def write_config(monitor_index, tesseract_path, trigger_vk, trigger_name):
    step(5, "Writing config.json")
    config = {
        "_comment": "Generated by setup.py - edit manually if needed",
        "game_monitor_index": monitor_index,
        "tesseract_path": tesseract_path,
        "trigger_key_vk": trigger_vk,
        "trigger_key_name": trigger_name,
        "key_delay": 0.02,
        "hold_duration": 0.04,
        "presence_window": 5.0,
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    print(f"  Config written to: {CONFIG_PATH}")
    print()
    print("  Contents:")
    for k, v in config.items():
        if not k.startswith("_"):
            print(f"    {k}: {v}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print()
    print("  Hruder Simulator Fingerprint Solver - Setup")
    print()

    if not sys.platform.startswith("win"):
        print("[ERROR] This solver is Windows-only (it uses Windows raw input APIs).")
        sys.exit(1)

    install_packages()
    tesseract_path = install_tesseract()
    monitor_index = select_monitor()
    trigger_vk, trigger_name = capture_trigger_key()
    write_config(monitor_index, tesseract_path, trigger_vk, trigger_name)

    hr("=")
    print("  Setup complete! Run the solver with:")
    print(f"    python fingerprint_solver.py")
    print(f"  Press {trigger_name!r} when the hacking screen opens.")
    hr("=")
    print()
    input("Press Enter to close...")


if __name__ == "__main__":
    main()
