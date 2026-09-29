"""Cut the raw screen recordings into one crossfaded showcase reel.

Each recording is cropped to just the app (the browser tabs and address bar
at the top are removed), trimmed to its useful moment, sped up a little, and
crossfaded into the next. Writes:

  frontend/static/videos/showcase.mp4   the reel (H.264, no audio, faststart)
  frontend/static/videos/showcase.jpg   poster frame

and prints the reel's length and each chapter's start time, for the
data-duration / data-start attributes on the hero reel in index.html.
Requires ffmpeg on PATH.

Run from the repo root:  python scripts/build_showcase_video.py
"""
import subprocess
from pathlib import Path

VIDEOS = Path("frontend/static/videos")
OUT = VIDEOS / "showcase.mp4"
POSTER = VIDEOS / "showcase.jpg"

# The 1440x1260 recordings: drop the browser chrome (top 90px) and the page
# scrollbar on the right edge.
CROP = "crop=1408:1160:6:90"
WIDTH, HEIGHT = 1200, 988
FPS = 30
FADE = 0.5

# (file, trim start, trim end, speed, chapter title)
SEGMENTS = [
    ("step2-accounts.mp4", 0.0, 12.0, 1.5, "All your accounts"),
    ("step2-categorize.mp4", 0.0, 10.1, 1.4, "Categorize"),
    ("step2-split-transactions.mp4", 4.0, 30.0, 2.0, "Split shared costs"),
    ("step2-amortize.mp4", 0.0, 25.0, 2.0, "Spread big purchases"),
    ("step3-forecast.mp4", 0.0, 17.5, 1.6, "Forecast"),
    ("step4-scenarios.mp4", 0.0, 8.9, 1.25, "Compare scenarios"),
    ("step4-budget-vs-actual.mp4", 0.0, 5.7, 1.0, "Budget vs. actual"),
]


def main():
    inputs, chains = [], []
    durations = []
    for i, (name, start, end, speed, _) in enumerate(SEGMENTS):
        inputs += ["-i", str(VIDEOS / name)]
        durations.append((end - start) / speed)
        chains.append(
            f"[{i}:v]trim={start}:{end},setpts=(PTS-STARTPTS)/{speed},"
            f"{CROP},scale={WIDTH}:{HEIGHT}:flags=lanczos,fps={FPS},"
            f"format=yuv420p,settb=AVTB[s{i}]"
        )

    starts, prev, elapsed = [0.0], "s0", durations[0]
    for i in range(1, len(SEGMENTS)):
        offset = elapsed - FADE
        starts.append(offset)
        label = f"x{i}"
        chains.append(
            f"[{prev}][s{i}]xfade=transition=fade:duration={FADE}:offset={offset:.3f}[{label}]"
        )
        prev = label
        elapsed = offset + durations[i]

    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", *inputs,
            "-filter_complex", ";".join(chains),
            "-map", f"[{prev}]", "-an",
            "-c:v", "libx264", "-preset", "slow", "-crf", "24",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(OUT),
        ],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", "0.2", "-i", str(OUT),
         "-frames:v", "1", "-q:v", "3", str(POSTER)],
        check=True,
    )

    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB, {elapsed:.2f}s)")
    for (_, _, _, _, title), start in zip(SEGMENTS, starts):
        print(f"  {start:6.2f}s  {title}")


if __name__ == "__main__":
    main()
