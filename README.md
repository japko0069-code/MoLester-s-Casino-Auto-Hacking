# GTA Online Diamond Casino Heist - Fingerprint Hack Solver

Automates the fingerprint hacking minigame in **GTA Online's Diamond Casino Heist**. The solver is designed specifically for the **fingerprint scanner hack performed inside the casino vault**. It detects the fingerprint interface, identifies which 4 of the 8 displayed fragments match the large target fingerprint using multi-scale template matching, navigates the selector using keyboard input, and handles chained fingerprint puzzles automatically.

---

## Requirements

- Windows 10 / 11 (64-bit)
- Python 3.9 or newer → https://www.python.org/downloads/
- **GTA Online** running on any configured monitor

Everything else (Python packages, Tesseract OCR) is installed automatically by `setup.py`.

---

## Installation

Download latest release and open `setup.py`

`setup.py` will:
1. Install all required Python packages
2. Download and install Tesseract OCR if not already present
3. Ask which monitor your game runs on (shows your actual detected monitors)
4. Ask you to press the key you want to use as the trigger
5. Write `config.json` with your settings

You only need to run setup once. Re-run it any time you change monitors or want a different trigger key.

---

## Usage

Open `fingerprint_solver.py`

1. Enter the **Diamond Casino Heist vault** and open the fingerprint scanner hacking interface
2. **Press your trigger key** when the fingerprint hacking screen opens — timing does not need to be exact because the solver waits for the puzzle to appear
3. The solver detects the fragments, marks the 4 correct ones, and submits
4. If multiple puzzles chain together, it handles them automatically — no second keypress needed
5. It stops and returns to idle the moment the fingerprint hacking sequence ends
6. **F8** to quit the solver

### Console output

```
[CHECK] Polling for puzzle (up to 5s)...
[CHECK] Puzzle #1 detected.
[SOLVE] Matching took 0.27s (rank4/rank5 margin: 0.082)
[SOLVE] Targets (optimal order, 5 moves): [(0,0), (0,1), (2,1), (3,0)]
[SOLVE] Marked (0,0)
...
[SOLVE] Submitted.
[CHECK] Processing/result overlay detected, waiting for it to clear...
[CHECK] Overlay cleared after 4.01s.
[CHECK] Hacking UI no longer active after 0.30s - stopping poll early.
[CHECK] Nothing detected. Back to idle - awaiting trigger.
```

The `rank4/rank5 margin` value shows how confidently the correct fragments were identified. Values above ~0.05 are comfortable; lower values still work but indicate a harder-to-distinguish puzzle layout.

---

## Tuning

### Key delay (input speed)

The default `key_delay` of `0.02s` controls how quickly the selector is moved through the GTA Online fingerprint grid. If you notice the selector skipping cells or marks not registering, increase it:

1.Edit `config.json` and raise `key_delay` (e.g. `0.03` or `0.04`)


### Presence window

`presence_window` in `config.json` controls how long (in seconds) the solver waits for a puzzle to appear after you press the trigger. Default is `5.0`. Increase it if the puzzle sometimes loads slowly and the solver gives up before it appears.

### Recalibrating for a different setup

If the solver is not detecting the GTA Online fingerprint interface or exits too early, re-run `setup.py` to regenerate `config.json` for your monitor configuration.

---

## GTA Online Diamond Casino Heist Context

This project is specifically for the **fingerprint hacking minigame inside the Diamond Casino Heist vault in GTA Online**.

The program is intended to solve the actual on-screen fingerprint interface. It does not assume that a fragment remains visually identical or in the same position across puzzle transitions. Each newly displayed fingerprint puzzle is analyzed from the current screen state.

The large fingerprint is the reference image. The smaller fingerprint fragments are candidates, and the solver determines which four correspond to the required sections before navigating the selector to them.

The solver uses screen capture and keyboard input rather than directly manipulating the player's position or GTA Online's game state.

---

## How it works

- **Detection**: samples the fingerprint fragment area for visual texture and uses it to determine whether the GTA Online fingerprint hacking interface is currently present.
- **Matching**: performs multi-scale template matching between all 8 displayed fragment tiles and the large fingerprint reference. The four highest-scoring matches are treated as the required fragments.
- **Navigation**: evaluates all 24 possible visiting orders for the four required fragments and selects the route requiring the fewest W/A/S/D selector movements.
- **Chain detection**: monitors the processing/result transition after each submission and waits for it to clear before looking for the next fingerprint puzzle. The CONNECTION TIMEOUT display is used to detect when the fingerprint hacking session has ended.
- **Input lock**: a low-level Windows keyboard hook blocks W/A/S/D/Enter input while the solver is controlling the fingerprint selector, preventing manual input from interfering.

---

## Files

| File | Purpose |
|---|---|
| `fingerprint_solver.py` | Main GTA Online Diamond Casino Heist fingerprint solver |
| `setup.py` | First-time setup — installs dependencies, writes config |
| `config.json` | Your settings (generated by setup.py, gitignored) |
| `requirements.txt` | Python package list |
