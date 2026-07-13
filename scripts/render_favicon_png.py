"""Render frontend/static/favicon.svg to a 1024x1024 PNG.

The SVG is simple (circles + cubic Bezier strokes with round caps), so we
rasterize it directly with Pillow using 4x supersampling for clean edges.
No native libraries (Cairo, etc.) required.
"""
from PIL import Image, ImageDraw

VIEWBOX = 32          # svg viewBox is 0 0 32 32
TARGET = 1024         # output size in px
SS = 4                # supersampling factor
F = TARGET * SS / VIEWBOX   # user-unit -> internal-pixel scale
SIZE = TARGET * SS

C_ACCENT = (56, 189, 248, 255)   # #38bdf8
C_SLATE = (100, 116, 139, 255)   # #64748b


def cubic(p0, p1, p2, p3, steps=800):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def dot(draw, cx, cy, r, color):
    x, y, rr = cx * F, cy * F, r * F
    draw.ellipse([x - rr, y - rr, x + rr, y + rr], fill=color)


def stroke(draw, pts, width, color):
    r = width / 2
    # round cap/join by stamping a filled circle at every sampled point
    for (x, y) in pts:
        dot(draw, x, y, r, color)


img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Origin dot
dot(d, 7, 16, 3, C_ACCENT)

# Spent path + end dot
stroke(d, cubic((7, 16), (12, 16), (14, 8), (21, 8)), 2.5, C_SLATE)
dot(d, 21, 8, 2.5, C_SLATE)

# Future path + end dot
stroke(d, cubic((7, 16), (13, 16), (18, 23), (26, 23)), 3, C_ACCENT)
dot(d, 26, 23, 3, C_ACCENT)

img = img.resize((TARGET, TARGET), Image.LANCZOS)
out = "frontend/static/favicon-1024.png"
img.save(out)
print("wrote", out, img.size)
