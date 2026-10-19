"""Just enough SVG to draw the figures, with nothing to install.

matplotlib was not on the machine the figures were first needed on, and SVG
renders in a README as it is. A figure is a canvas with panels; a panel is
an axis with a data range that maps to pixels, and takes lines, bars, cells
and text. One y axis per panel, always.
"""

import html
import math

INK, MUTED, GRID, FAINT = "#333333", "#777777", "#DDDDDD", "#F3F3F3"
FONT = "font-family='Helvetica,Arial,sans-serif'"


def esc(s):
    return html.escape(str(s), quote=True)


class Canvas:
    def __init__(self, width, height, title=None):
        self.w, self.h = width, height
        self.parts = []
        if title:
            self.text(width / 2, 18, title, size=13, anchor="middle", color=INK)

    def text(self, x, y, s, size=10, anchor="start", color=INK, weight="normal", rotate=None, baseline="auto"):
        tr = " transform='rotate({} {} {})'".format(rotate, x, y) if rotate else ""
        self.parts.append(
            "<text x='{:.1f}' y='{:.1f}' {} font-size='{}' text-anchor='{}' fill='{}' font-weight='{}' dominant-baseline='{}'{}>{}</text>".format(
                x, y, FONT, size, anchor, color, weight, baseline, tr, esc(s)
            )
        )

    def rect(self, x, y, w, h, fill, stroke="none", opacity=1.0, title=None, rx=0):
        t = "<title>{}</title>".format(esc(title)) if title else ""
        self.parts.append(
            "<rect x='{:.1f}' y='{:.1f}' width='{:.1f}' height='{:.1f}' fill='{}' stroke='{}' fill-opacity='{}' rx='{}'>{}</rect>".format(
                x, y, max(w, 0), max(h, 0), fill, stroke, opacity, rx, t
            )
        )

    def line(self, x1, y1, x2, y2, color=GRID, width=1.0, dash=None):
        d = " stroke-dasharray='{}'".format(dash) if dash else ""
        self.parts.append(
            "<line x1='{:.1f}' y1='{:.1f}' x2='{:.1f}' y2='{:.1f}' stroke='{}' stroke-width='{}'{}/>".format(x1, y1, x2, y2, color, width, d)
        )

    def polyline(self, pts, color, width=1.6, opacity=1.0):
        if len(pts) < 2:
            return
        self.parts.append(
            "<polyline fill='none' stroke='{}' stroke-width='{}' stroke-opacity='{}' stroke-linejoin='round' points='{}'/>".format(
                color, width, opacity, " ".join("{:.1f},{:.1f}".format(x, y) for x, y in pts)
            )
        )

    def circle(self, x, y, r, fill, opacity=1.0):
        self.parts.append("<circle cx='{:.1f}' cy='{:.1f}' r='{}' fill='{}' fill-opacity='{}'/>".format(x, y, r, fill, opacity))

    def panel(self, x, y, w, h, xlim, ylim):
        return Panel(self, x, y, w, h, xlim, ylim)

    def save(self, path):
        body = "\n".join(self.parts)
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' viewBox='0 0 {w} {h}'>\n"
            "<rect width='100%' height='100%' fill='white'/>\n{body}\n</svg>\n"
        ).format(w=self.w, h=self.h, body=body)
        with open(path, "w") as f:
            f.write(svg)
        return path


def nice_ticks(lo, hi, n=5):
    """Round tick positions covering [lo, hi]."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if (hi - lo) / step <= n + 0.5:
            break
    start = math.ceil(lo / step) * step
    ticks = []
    t = start
    while t <= hi + 1e-9:
        ticks.append(round(t, 10))
        t += step
    return ticks


def fmt_num(v):
    if abs(v) >= 1000:
        return "{:.0f}k".format(v / 1000) if v % 1000 == 0 else "{:.1f}k".format(v / 1000)
    if float(v).is_integer():
        return "{:d}".format(int(v))
    return "{:g}".format(v)


class Panel:
    def __init__(self, c, x, y, w, h, xlim, ylim):
        self.c, self.x, self.y, self.w, self.h = c, x, y, w, h
        self.x0, self.x1 = xlim
        self.y0, self.y1 = ylim
        if self.x1 <= self.x0:
            self.x1 = self.x0 + 1
        if self.y1 <= self.y0:
            self.y1 = self.y0 + 1

    def px(self, v):
        return self.x + (v - self.x0) / (self.x1 - self.x0) * self.w

    def py(self, v):
        return self.y + self.h - (v - self.y0) / (self.y1 - self.y0) * self.h

    def frame(self, xticks=None, yticks=None, xfmt=fmt_num, yfmt=fmt_num, xlabel=None, ylabel=None, grid=True):
        c = self.c
        if yticks is None:
            yticks = nice_ticks(self.y0, self.y1, 4)
        for t in yticks:
            if self.y0 <= t <= self.y1:
                if grid:
                    c.line(self.x, self.py(t), self.x + self.w, self.py(t), GRID, 0.6)
                c.text(self.x - 4, self.py(t), yfmt(t), size=8, anchor="end", color=INK, baseline="middle")
        if xticks is None:
            xticks = nice_ticks(self.x0, self.x1, 6)
        for t in xticks:
            if self.x0 <= t <= self.x1:
                if grid:
                    c.line(self.px(t), self.y, self.px(t), self.y + self.h, GRID, 0.6)
                c.text(self.px(t), self.y + self.h + 11, xfmt(t), size=8, anchor="middle", color=INK)
        c.line(self.x, self.y + self.h, self.x + self.w, self.y + self.h, MUTED, 0.8)
        c.line(self.x, self.y, self.x, self.y + self.h, MUTED, 0.8)
        if xlabel:
            c.text(self.x + self.w / 2, self.y + self.h + 24, xlabel, size=9, anchor="middle", color=INK)
        if ylabel:
            c.text(self.x - 34, self.y + self.h / 2, ylabel, size=9, anchor="middle", color=INK, rotate=-90)

    def step(self, xs, ys, color, width=1.4):
        """A step line. Only the points where the value changes are kept,
        plus the last, which draws the same shape from a fraction of the
        points on a series that is flat most of the time."""
        pts = []
        last = None
        n = len(xs)
        for k, (x, y) in enumerate(zip(xs, ys)):
            if y is None:
                last = None
                continue
            Y = self.py(min(max(y, self.y0), self.y1))
            if last is not None and Y == last and k < n - 1:
                continue
            X = self.px(x)
            if last is not None:
                pts.append((X, last))
            pts.append((X, Y))
            last = Y
        self.c.polyline(pts, color, width)

    def line(self, xs, ys, color, width=1.6):
        pts = [(self.px(x), self.py(y)) for x, y in zip(xs, ys) if y is not None]
        self.c.polyline(pts, color, width)

    def bar(self, x, y, color, width_units, title=None):
        w = width_units / (self.x1 - self.x0) * self.w
        top = self.py(y)
        self.c.rect(self.px(x) - w / 2, top, w, self.py(self.y0) - top, color, title=title, rx=2)

    def hbar(self, y_index, length, color, height_px, title=None, label=None):
        """A horizontal bar at a category row, length in x units."""
        Y = self.y + y_index * height_px
        self.c.rect(self.x, Y + 2, (length - self.x0) / (self.x1 - self.x0) * self.w, height_px - 4, color, title=title, rx=2)
        if label is not None:
            self.c.text(self.px(length) + 4, Y + height_px / 2, label, size=8, color=INK, baseline="middle")

    def cell(self, x_from, x_to, y_index, height_px, color, title=None, opacity=1.0):
        Y = self.y + y_index * height_px
        self.c.rect(self.px(x_from), Y, self.px(x_to) - self.px(x_from), height_px, color, title=title, opacity=opacity)

    def label(self, s, dx=4, dy=12, color=INK, size=9):
        self.c.text(self.x + dx, self.y + dy, s, size=size, color=color)

    def hline(self, y, color=MUTED, dash="3,3"):
        self.c.line(self.x, self.py(y), self.x + self.w, self.py(y), color, 0.8, dash)

    def vline(self, x, color=MUTED, dash="3,3", width=0.8):
        self.c.line(self.px(x), self.y, self.px(x), self.y + self.h, color, width, dash)


def sequential(frac, hue=(0, 114, 178)):
    """One hue, light to dark, for a magnitude in [0, 1]."""
    frac = 0 if frac is None else min(max(frac, 0.0), 1.0)
    r, g, b = hue
    # from near white to the full hue
    return "#{:02x}{:02x}{:02x}".format(int(245 + (r - 245) * frac), int(245 + (g - 245) * frac), int(248 + (b - 248) * frac))
