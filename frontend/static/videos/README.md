# Product tour reel

The `step*.mp4` files are raw screen recordings. They aren't shown on the site
directly: `scripts/build_showcase_video.py` crops, trims, speeds up, and
crossfades them into `showcase.mp4` (plus the `showcase.jpg` poster), which plays
in the "See it in action" section of the landing page (between Philosophy and
Pricing).

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
1440x1260 recordings with browser chrome at the top; new recordings at a
different size need a new crop.

`step1-guided-setup.mp4` is not in the reel — it's a light-theme 1280x640 capture
that doesn't match the rest.
