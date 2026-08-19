# Valorant Live Aim Coach (Safe MVP)

This is a local Windows dashboard designed to run on a second monitor while you play Valorant.

It gives **general camera / aim-control feedback** from your screen capture:
- estimated horizontal and vertical camera movement
- movement speed
- stability score
- micro-correction / reversal tendency
- immediate post-fight movement summary
- rolling 5-second clip saved when you mark an engagement

It does **not** detect enemy heads live, identify enemy positions, or tell you where an enemy is during an active fight.

## Requirements

- Windows 10/11
- Python 3.10+
- Two monitors recommended
- Valorant should run on monitor 1 by default

## Install

Double-click:

`setup.bat`

Or open PowerShell / Command Prompt in this folder and run:

```bat
python -m venv venv
venv\Scripts\python -m pip install --upgrade pip
venv\Scripts\pip install -r requirements.txt
```

## Run

Double-click:

`run.bat`

Or:

```bat
venv\Scripts\python app.py
```

The dashboard should automatically open on your second monitor if one is detected.

## During a match

Keep Valorant on your main monitor and the dashboard on monitor 2.

The dashboard updates continuously.

Immediately after an engagement ends, press:

`F8`

The app will:
1. analyze the recent rolling buffer
2. show a movement summary
3. save the recent clip in `output/`
4. save motion data as a CSV file

You can also click **Mark Fight (F8)** on the dashboard.

## What the live metrics mean

### X motion
Estimated horizontal camera movement between frames.

### Y motion
Estimated vertical camera movement between frames.

### Speed
Combined camera movement magnitude.

### Aim stability
A 0-100 heuristic score.

Higher means the camera is relatively steady.
Lower means the image is moving or reversing direction frequently.

This is not a measure of whether your crosshair is on an enemy.

### Reversals
The number of horizontal/vertical direction changes during the recently marked engagement.

A large number can indicate excessive micro-correction.

## Output clips

Every time you press F8, the recent rolling buffer is written to:

`output/fight_YYYYMMDD_HHMMSS.mp4`

The video includes:
- a center-screen reference cross
- a camera-motion arrow
- motion values

The arrow represents estimated camera motion only. It is not an opponent direction indicator.

The matching CSV contains:

```text
timestamp,dx,dy,speed
```

## Change which monitor is captured

Open `app.py`.

Near the top, change:

```python
CAPTURE_MONITOR_INDEX = 1
```

`1` is normally the first physical monitor.

If Valorant is on a different monitor, try `2`.

## Performance settings

If the game or dashboard stutters, reduce:

```python
CAPTURE_FPS = 30
```

to:

```python
CAPTURE_FPS = 20
```

You can also reduce:

```python
CLIP_WIDTH = 1280
```

to:

```python
CLIP_WIDTH = 960
```

## Borderless Windowed is recommended

Screen capture is generally easier and more reliable with Valorant in Borderless Windowed mode than exclusive fullscreen.

## Limitations

This MVP estimates camera motion using frame-to-frame image translation. Player movement, recoil animation, screen effects, and camera shake can also affect the measurement.

The next safe upgrade would be:
- automatic shot/fight boundary detection
- session trend charts
- map/weapon tagging
- post-engagement analysis from the saved rolling buffer
- per-session CSV summaries
