"""Film Lab (https://github.com/thedevmark/film-lab, commit 5df5e06), the parts the Editorial Film
style uses, copied unchanged: tetrahedral 3D-LUT interpolation and measured film grain.

MIT License

Copyright (c) 2026 Mark Koellmann

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


from __future__ import annotations

import math

import numpy as np


LUMA = (0.2126, 0.7152, 0.0722)


def _luminance(rgb):
    return (rgb[:, :, 0] * np.float32(LUMA[0])
            + rgb[:, :, 1] * np.float32(LUMA[1])
            + rgb[:, :, 2] * np.float32(LUMA[2]))



# Amplitude relative to the midtone peak, at the ends of the tone scale.
GRAIN_TONE_FLOOR = 0.31
# Shape of the hump between those ends. The skew pulls the peak below mid-grey
# (measured ~0.40); the exponent sharpens the shoulders. Fitted jointly, mean
# absolute error 0.021 against the nine measured tone bins.
GRAIN_TONE_EXPONENT = 1.65
GRAIN_TONE_SKEW = 0.80

# Fraction of each channel's field shared with the other two.
GRAIN_CHROMA_SHARE = 0.80
# Per-channel amplitude. Red sits in the fastest, coarsest-grained layer.
GRAIN_CHANNEL_GAIN = (1.20, 1.00, 1.05)

# The band-pass: subtract a blur this many times wider than the grain itself.
# This is what puts the negative lobe in the autocorrelation and keeps the field
# from drifting into low-frequency blotching.
GRAIN_BANDPASS_RATIO = 4.0
# Below about a third of a pixel a Gaussian is indistinguishable from a delta,
# and the band-pass subtraction would cancel to zero. Grain finer than the
# sampling grid is white noise, which is the correct answer anyway.
GRAIN_MIN_SIGMA = 0.30


def _box_blur_axis(arr, radius, axis):
    """One box-blur pass along an axis, O(n) in the image and independent of radius."""
    if radius < 1:
        return arr

    moved = np.moveaxis(arr, axis, 0)
    n = moved.shape[0]
    padded = np.pad(moved, [(radius, radius)] + [(0, 0)] * (moved.ndim - 1), mode="edge")
    cumulative = np.cumsum(padded, axis=0, dtype=np.float32)
    zero = np.zeros((1,) + cumulative.shape[1:], dtype=np.float32)
    cumulative = np.concatenate([zero, cumulative], axis=0)

    window = 2 * radius + 1
    out = (cumulative[window:window + n] - cumulative[:n]) / np.float32(window)
    return np.moveaxis(out, 0, axis)


def box_blur(arr, sigma):
    """A SINGLE box pass per axis, sized to match `sigma`'s second moment.

    One box is a poor Gaussian — its frequency response rings. That matters when
    the blur is the visible output (halation) and does not when the blur is only
    being subtracted to mark a low-frequency cutoff (grain's band-pass), where
    everything it gets wrong lives below the band anyone can see. Three times
    cheaper than gaussian_blur, which is what keeps grain off the critical path
    of a batch export.
    """
    if sigma <= 0:
        return arr

    # Second moment of a (2r+1)-wide box is ((2r+1)^2 - 1) / 12.
    radius = max(1, int(round((math.sqrt(12.0 * sigma * sigma + 1.0) - 1.0) / 2.0)))
    out = _box_blur_axis(arr.astype(np.float32, copy=True), radius, axis=0)
    return _box_blur_axis(out, radius, axis=1)


def _fine_gaussian(field, sigma: float):
    """Separable Gaussian with a TRUE float sigma, sub-pixel included.

    gaussian_blur is three box passes with an integer radius floored at 1, so
    every sigma below ~1.5 collapses onto the same ~2px kernel. Halation does
    not care — its sigmas are tens of pixels. Grain lives entirely inside that
    dead zone, so it needs a real kernel. Sigma is small here (a 3-sigma radius
    of 1–3 taps at the sizes grain actually uses), so an explicit FIR is cheap.
    """
    if sigma <= 0:
        return field

    radius = max(1, int(math.ceil(3.0 * sigma)))
    offsets = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-(offsets * offsets) / np.float32(2.0 * sigma * sigma))
    kernel /= kernel.sum()

    out = field
    for axis in (0, 1):
        padding = [(0, 0), (0, 0)]
        padding[axis] = (radius, radius)
        padded = np.pad(out, padding, mode="reflect")
        accumulated = np.zeros_like(out)
        for index, coefficient in enumerate(kernel):
            window = [slice(None), slice(None)]
            window[axis] = slice(index, index + out.shape[axis])
            accumulated += np.float32(coefficient) * padded[tuple(window)]
        out = accumulated
    return out


def _shape_grain(field, sigma: float):
    """White noise -> a band-pass field with the measured correlation length.

    Two different blurs on purpose. The INNER one sets the grain's own size and
    is sub-pixel, so it has to be the exact FIR. The OUTER one only marks where
    the band-pass rolls off underneath the grain — a few percent of error in its
    width moves nothing visible — so it uses the O(n) box-blur, which is what
    keeps a 24MP export in seconds rather than minutes.
    """
    field = _fine_gaussian(field, sigma)
    return field - box_blur(field, sigma * GRAIN_BANDPASS_RATIO)


def _tone_weight(luma):
    """Grain amplitude as a function of lightness.

    A hump that peaks a little below mid-grey and settles onto GRAIN_TONE_FLOOR
    at both ends rather than reaching zero. See the fit note above.
    """
    skewed = np.power(np.clip(luma, 0.0, 1.0), np.float32(GRAIN_TONE_SKEW))
    hump = np.clip(np.float32(4.0) * skewed * (np.float32(1.0) - skewed), 0.0, 1.0)
    hump = np.power(hump, np.float32(GRAIN_TONE_EXPONENT))
    return (np.float32(GRAIN_TONE_FLOOR)
            + np.float32(1.0 - GRAIN_TONE_FLOOR) * hump).astype(np.float32)


def add_grain(rgb, intensity: float, size: float, seed: int = 0):
    """Film grain, in display (sRGB-encoded) space.

    `size` is a fraction of the short edge, so the look survives a resize:
    grain is fixed to the frame, as it is on the negative, not to the pixel
    grid. `intensity` is the standard deviation of the perturbation at the
    midtone peak, in display units — 0.032 reproduces the reference scans.
    """
    rgb = np.asarray(rgb, dtype=np.float32)
    if intensity <= 0 or size <= 0:
        return rgb

    height, width = rgb.shape[:2]
    sigma = float(size) * min(height, width)
    # Clamp: a sigma larger than the frame leaves nothing for the band-pass to
    # subtract, and the renormalisation below would then divide by ~0.
    sigma = min(max(sigma, GRAIN_MIN_SIGMA), min(height, width) / 8.0)

    rng = np.random.default_rng(seed)
    shared_field = rng.standard_normal((height, width), dtype=np.float32)
    shared = np.float32(math.sqrt(GRAIN_CHROMA_SHARE))
    private = np.float32(math.sqrt(1.0 - GRAIN_CHROMA_SHARE))

    weight = _tone_weight(_luminance(rgb)) * np.float32(intensity)

    out = rgb.copy()
    scale = None
    for channel, gain in enumerate(GRAIN_CHANNEL_GAIN):
        # Each channel is mostly the shared field plus a little of its own. The
        # sqrt weights keep the sum at unit variance, so the channels differ in
        # correlation without differing in amplitude.
        field = shared_field * shared
        field += rng.standard_normal((height, width), dtype=np.float32) * private
        field = _shape_grain(field, sigma)

        # Band-passing costs variance. Restore it — once, from the first
        # channel, so the per-channel gains below survive rather than being
        # normalised away.
        if scale is None:
            deviation = float(field.std())
            scale = np.float32(1.0 / deviation if deviation > 1e-6 else 0.0)

        out[:, :, channel] += field * scale * np.float32(gain) * weight

    return np.clip(out, 0.0, 1.0).astype(np.float32)


def apply_lut(rgb, cube, strength: float = 1.0):
    """Apply a 3D LUT with tetrahedral interpolation.

    Input and output are sRGB-encoded [0,1]. Values outside the range are
    clipped, not wrapped — the LUT is only defined on the unit cube.

    Tetrahedral rather than trilinear because the neutral diagonal is a shared
    edge of all six tetrahedra: an R==G==B input therefore interpolates only
    between lattice nodes that are themselves neutral, so greys stay grey by
    construction. Trilinear weights all eight corners, most of them off-axis,
    and tints what should be a pure neutral. It is also cheaper: four fetches
    instead of eight.
    """
    rgb = np.asarray(rgb, dtype=np.float32)
    if strength <= 0.0:
        return rgb.copy()

    size = cube.shape[0]
    clipped = np.clip(rgb, 0.0, 1.0)

    scaled = clipped * np.float32(size - 1)
    base = np.clip(np.floor(scaled), 0, size - 2).astype(np.int32)
    frac = (scaled - base).astype(np.float32)

    ir, ig, ib = base[..., 0], base[..., 1], base[..., 2]
    dr, dg, db = frac[..., 0], frac[..., 1], frac[..., 2]

    def node(orr, og, ob):
        return cube[ir + orr, ig + og, ib + ob]

    c000 = node(0, 0, 0)
    c111 = node(1, 1, 1)

    # Six tetrahedra, selected by the ordering of (dr, dg, db). Each case is a
    # weighted sum of four nodes: c000, c111, and two edge-adjacent corners.
    #
    # cond_rg / cond_gb / cond_rb only reach six of the eight possible
    # (T/F, T/F, T/F) combinations: (T,T,F) and (F,F,T) are algebraically
    # impossible (dr>dg>db>dr is inconsistent, and its mirror). The six masks
    # below are the six reachable combinations, each attached to the formula
    # for its matching ordering:
    #
    #   (T,T,T) dr>dg>db | (T,F,T) dr>db>dg | (T,F,F) db>dr>dg
    #   (F,F,F) db>dg>dr | (F,T,F) dg>db>dr | (F,T,T) dg>dr>db
    #
    # Getting a mask's booleans out of order silently sends pixels through the
    # wrong formula (or lets two masks overlap, or leaves a gap) without
    # raising anything -- see test_lut.py's TestApplyLutSixTetrahedra for the
    # verification that these six partition every pixel exactly once.
    out = np.empty(rgb.shape, dtype=np.float32)

    w = lambda x: x[..., None]  # noqa: E731 - broadcast a weight over RGB

    cond_rg = dr > dg
    cond_gb = dg > db
    cond_rb = dr > db

    # dr > dg > db  ->  (T,T,T)
    m = cond_rg & cond_gb
    out = np.where(w(m),
                   w(1 - dr) * c000 + w(dr - dg) * node(1, 0, 0)
                   + w(dg - db) * node(1, 1, 0) + w(db) * c111,
                   0.0).astype(np.float32)

    # dr > db > dg  ->  (T,F,T)
    m = cond_rg & ~cond_gb & cond_rb
    out += np.where(w(m),
                    w(1 - dr) * c000 + w(dr - db) * node(1, 0, 0)
                    + w(db - dg) * node(1, 0, 1) + w(dg) * c111,
                    0.0).astype(np.float32)

    # db > dr > dg  ->  (T,F,F)
    m = cond_rg & ~cond_gb & ~cond_rb
    out += np.where(w(m),
                    w(1 - db) * c000 + w(db - dr) * node(0, 0, 1)
                    + w(dr - dg) * node(1, 0, 1) + w(dg) * c111,
                    0.0).astype(np.float32)

    # db > dg > dr  ->  (F,F,F)  (also where dr == dg == db lands: all three
    # comparisons are False when the deltas are equal, and this formula then
    # collapses to (1-d)*c000 + d*c111 -- the neutral-axis guarantee.)
    m = ~cond_rg & ~cond_gb & ~cond_rb
    out += np.where(w(m),
                    w(1 - db) * c000 + w(db - dg) * node(0, 0, 1)
                    + w(dg - dr) * node(0, 1, 1) + w(dr) * c111,
                    0.0).astype(np.float32)

    # dg > db > dr  ->  (F,T,F)
    m = ~cond_rg & cond_gb & ~cond_rb
    out += np.where(w(m),
                    w(1 - dg) * c000 + w(dg - db) * node(0, 1, 0)
                    + w(db - dr) * node(0, 1, 1) + w(dr) * c111,
                    0.0).astype(np.float32)

    # dg > dr > db  ->  (F,T,T)
    m = ~cond_rg & cond_gb & cond_rb
    out += np.where(w(m),
                    w(1 - dg) * c000 + w(dg - dr) * node(0, 1, 0)
                    + w(dr - db) * node(1, 1, 0) + w(db) * c111,
                    0.0).astype(np.float32)

    if strength >= 1.0:
        return out
    s = np.float32(strength)
    return (s * out + (np.float32(1.0) - s) * rgb).astype(np.float32)
