# Product tour reel

The `step*.mp4` files are raw screen recordings. They aren't shown on the site
directly: `scripts/build_showcase_video.py` crops, trims, speeds up, and
crossfades them into `showcase.mp4` (plus the `showcase.jpg` poster), which plays
in the hero of the landing page, under the tagline.

## Rebuilding

```sh
python scripts/build_showcase_video.py
```

Requires `ffmpeg`. The script prints the reel's length and each chapter's start
time; copy those into `data-duration` on `#reel` and the `data-start` attributes
on the `.reel__chapters` buttons in `frontend/templates/index.html`.

## Changing the cut

Edit `SEGMENTS` in the script: each entry is the source file, the trim start and
end (seconds), a playback speed, and the chapter title. `CROP` assumes the
2000x1282 recordings with the window's title bar at the top; new recordings at a
different size need a new crop.

## Recording

The recordings are a 1600x1000 page (check with `innerWidth + "x" + innerHeight`
in the console) at 125% browser zoom, in a Chrome app window
(`--app=http://127.0.0.1:5002`), captured with an OBS Window Capture at 30 fps,
H.264. Keep 16:10 and the same window size for every clip, since one `CROP`
covers them all.
