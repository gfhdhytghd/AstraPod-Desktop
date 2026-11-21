"""Material 3 colour: HCT, tonal palettes and the light/dark role schemes.

A compact, Qt-free port of the parts of Material Color Utilities the desktop
needs. HCT is CAM16 hue and chroma with CIE L* as tone; `hct_to_argb` finds
the in-gamut colour nearest the requested chroma at the exact hue and tone.
`scheme(seed, dark)` follows the tonal-spot scheme Material You uses by
default, and `seed_from_pixels` picks a source colour from album art.
"""
from __future__ import annotations

from functools import lru_cache
import math

_M16 = ((0.401288, 0.650173, -0.051461),
        (-0.250268, 1.204414, 0.045854),
        (-0.002079, 0.048952, 0.953127))
_M16_INV = ((1.8620678, -1.0112547, 0.14918678),
            (0.38752654, 0.62144744, -0.00897398),
            (-0.01584150, -0.03412294, 1.0499644))
_SRGB_TO_XYZ = ((0.41233895, 0.35762064, 0.18051042),
                (0.2126, 0.7152, 0.0722),
                (0.01932141, 0.11916382, 0.95034478))
_XYZ_TO_SRGB = ((3.2413774792388685, -1.5376652402851851, -0.49885366846268053),
                (-0.9691452513005321, 1.8758853451067872, 0.04156585616912061),
                (0.05562093689691305, -0.20395524564742123, 1.0571799111220335))
_WHITE = (95.047, 100.0, 108.883)


def _mul(m, v):
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))


def _linearize(channel: int) -> float:
    c = channel / 255.0
    return (c / 12.92 if c <= 0.040449936 else ((c + 0.055) / 1.055) ** 2.4) * 100.0


def _delinearize(value: float) -> int:
    c = value / 100.0
    c = c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return max(0, min(255, round(c * 255.0)))


def lstar_from_y(y: float) -> float:
    y /= 100.0
    f = y ** (1 / 3) if y > 216 / 24389 else (24389 / 27 * y + 16) / 116
    return 116 * f - 16


def y_from_lstar(lstar: float) -> float:
    ft = (lstar + 16) / 116
    ft3 = ft ** 3
    return 100.0 * (ft3 if ft3 > 216 / 24389 else (116 * ft - 16) / (24389 / 27))


def argb_to_rgb(argb: int) -> tuple[int, int, int]:
    return (argb >> 16) & 255, (argb >> 8) & 255, argb & 255


def rgb_to_argb(r: int, g: int, b: int) -> int:
    return 0xFF000000 | (r << 16) | (g << 8) | b


def hex_to_argb(text: str) -> int:
    text = text.lstrip("#")
    return 0xFF000000 | int(text[-6:], 16)


def argb_to_hex(argb: int) -> str:
    return "#%06X" % (argb & 0xFFFFFF)


def tone_of(argb: int) -> float:
    return lstar_from_y(_mul(_SRGB_TO_XYZ, [_linearize(c) for c in argb_to_rgb(argb)])[1])


class _Viewing:
    """CAM16 viewing conditions Material uses: D65, L* 50 background."""

    def __init__(self):
        adapting = 200 / math.pi * y_from_lstar(50) / 100
        rgb_w = _mul(_M16, _WHITE)
        f = 1.0
        self.c = 0.69
        d = max(0.0, min(1.0, f * (1 - 1 / 3.6 * math.exp((-adapting - 42) / 92))))
        self.nc = f
        self.rgb_d = tuple(d * (100 / w) + 1 - d for w in rgb_w)
        k = 1 / (5 * adapting + 1)
        k4 = k ** 4
        self.fl = k4 * adapting + 0.1 * (1 - k4) ** 2 * (5 * adapting) ** (1 / 3)
        self.n = y_from_lstar(50) / _WHITE[1]
        self.z = 1.48 + math.sqrt(self.n)
        self.nbb = 0.725 / self.n ** 0.2
        factors = [(self.fl * self.rgb_d[i] * rgb_w[i] / 100) ** 0.42 for i in range(3)]
        rgb_a = [400 * f / (f + 27.13) for f in factors]
        self.aw = (2 * rgb_a[0] + rgb_a[1] + 0.05 * rgb_a[2]) * self.nbb


_VC = _Viewing()


def cam16(argb: int) -> tuple[float, float, float]:
    """(hue, chroma, J) of an sRGB colour."""
    xyz = _mul(_SRGB_TO_XYZ, [_linearize(c) for c in argb_to_rgb(argb)])
    rgb_c = _mul(_M16, xyz)
    rgb_a = []
    for i in range(3):
        d = _VC.rgb_d[i] * rgb_c[i]
        af = (_VC.fl * abs(d) / 100) ** 0.42
        rgb_a.append(math.copysign(400 * af / (af + 27.13), d))
    ra, ga, ba = rgb_a
    a = (11 * ra - 12 * ga + ba) / 11
    b = (ra + ga - 2 * ba) / 9
    u = (20 * ra + 20 * ga + 21 * ba) / 20
    p2 = (40 * ra + 20 * ga + ba) / 20
    hue = math.degrees(math.atan2(b, a)) % 360
    ac = p2 * _VC.nbb
    j = 100 * (ac / _VC.aw) ** (_VC.c * _VC.z) if ac > 0 else 0.0
    hue_prime = hue + 360 if hue < 20.14 else hue
    e_hue = 0.25 * (math.cos(math.radians(hue_prime) + 2) + 3.8)
    p1 = 50000 / 13 * e_hue * _VC.nc * _VC.nbb
    t = p1 * math.hypot(a, b) / (u + 0.305)
    alpha = t ** 0.9 * (1.64 - 0.29 ** _VC.n) ** 0.73
    return hue, alpha * math.sqrt(j / 100), j


def _cam16_to_linear_rgb(j: float, chroma: float, hue: float):
    alpha = 0.0 if chroma == 0 or j == 0 else chroma / math.sqrt(j / 100)
    t = (alpha / (1.64 - 0.29 ** _VC.n) ** 0.73) ** (1 / 0.9)
    h = math.radians(hue)
    e_hue = 0.25 * (math.cos(h + 2) + 3.8)
    ac = _VC.aw * (j / 100) ** (1 / _VC.c / _VC.z)
    p1 = e_hue * (50000 / 13) * _VC.nc * _VC.nbb
    p2 = ac / _VC.nbb
    sin, cos = math.sin(h), math.cos(h)
    gamma = 23 * (p2 + 0.305) * t / (23 * p1 + 11 * t * cos + 108 * t * sin)
    a, b = gamma * cos, gamma * sin
    rgb_a = ((460 * p2 + 451 * a + 288 * b) / 1403,
             (460 * p2 - 891 * a - 261 * b) / 1403,
             (460 * p2 - 220 * a - 6300 * b) / 1403)
    rgb_f = []
    for i, value in enumerate(rgb_a):
        base = max(0.0, 27.13 * abs(value) / (400 - abs(value)))
        rgb_f.append(math.copysign(100 / _VC.fl * base ** (1 / 0.42), value) / _VC.rgb_d[i])
    return _mul(_XYZ_TO_SRGB, _mul(_M16_INV, rgb_f))


def _fit(hue: float, chroma: float, y: float):
    """Linear sRGB at this hue and chroma whose Y is y, or None outside gamut."""
    j = math.sqrt(lstar_from_y(y)) * 11
    rgb = None
    for _ in range(8):
        rgb = _cam16_to_linear_rgb(j, chroma, hue)
        fnj = _SRGB_TO_XYZ[1][0] * rgb[0] + _SRGB_TO_XYZ[1][1] * rgb[1] + _SRGB_TO_XYZ[1][2] * rgb[2]
        if fnj <= 0:
            return None
        if abs(fnj - y) < 0.002:
            break
        j -= (fnj - y) * j / (2 * fnj)
        if j <= 0 or j > 120:
            return None
    if all(-0.01 <= v <= 100.01 for v in rgb):
        return rgb
    return None


@lru_cache(maxsize=4096)
def hct_to_argb(hue: float, chroma: float, tone: float) -> int:
    if tone <= 0.0001:
        return 0xFF000000
    if tone >= 99.9999:
        return 0xFFFFFFFF
    y = y_from_lstar(tone)
    hue %= 360
    rgb = _fit(hue, chroma, y) if chroma >= 0.5 else None
    if rgb is None:
        low, high = 0.0, chroma
        rgb = _fit(hue, 0.0, y) or (y, y, y)
        for _ in range(14):
            middle = (low + high) / 2
            found = _fit(hue, middle, y)
            if found is None:
                high = middle
            else:
                rgb, low = found, middle
    return rgb_to_argb(*(_delinearize(max(0.0, min(100.0, v))) for v in rgb))


class TonalPalette:
    def __init__(self, hue: float, chroma: float):
        self.hue = hue % 360
        self.chroma = chroma

    def tone(self, tone: float) -> int:
        return hct_to_argb(round(self.hue, 3), round(self.chroma, 3), tone)


def harmonize(hue: float, toward: float) -> float:
    """Rotate a fixed hue up to 15 degrees toward the seed's, as M3 does."""
    diff = (toward - hue + 180) % 360 - 180
    return (hue + math.copysign(min(abs(diff) * 0.5, 15.0), diff)) % 360


LIGHT_TONES = {
    "primary": ("p", 40), "on_primary": ("p", 100),
    "primary_container": ("p", 90), "on_primary_container": ("p", 10),
    "secondary": ("s", 40), "on_secondary": ("s", 100),
    "secondary_container": ("s", 90), "on_secondary_container": ("s", 10),
    "tertiary": ("t", 40), "on_tertiary": ("t", 100),
    "tertiary_container": ("t", 90), "on_tertiary_container": ("t", 10),
    "error": ("e", 40), "on_error": ("e", 100),
    "error_container": ("e", 90), "on_error_container": ("e", 10),
    "surface": ("n", 98), "on_surface": ("n", 10),
    "surface_variant": ("v", 90), "on_surface_variant": ("v", 30),
    "surface_dim": ("n", 87), "surface_bright": ("n", 98),
    "surface_container_lowest": ("n", 100), "surface_container_low": ("n", 96),
    "surface_container": ("n", 94), "surface_container_high": ("n", 92),
    "surface_container_highest": ("n", 90),
    "outline": ("v", 50), "outline_variant": ("v", 80),
    "inverse_surface": ("n", 20), "inverse_on_surface": ("n", 95),
    "inverse_primary": ("p", 80), "shadow": ("n", 0), "scrim": ("n", 0),
    "success": ("g", 40), "on_success": ("g", 100),
    "success_container": ("g", 90), "on_success_container": ("g", 10),
    "warning": ("w", 40), "warning_container": ("w", 90), "on_warning_container": ("w", 10),
    "info": ("i", 40),
}
DARK_TONES = {
    "primary": ("p", 80), "on_primary": ("p", 20),
    "primary_container": ("p", 30), "on_primary_container": ("p", 90),
    "secondary": ("s", 80), "on_secondary": ("s", 20),
    "secondary_container": ("s", 30), "on_secondary_container": ("s", 90),
    "tertiary": ("t", 80), "on_tertiary": ("t", 20),
    "tertiary_container": ("t", 30), "on_tertiary_container": ("t", 90),
    "error": ("e", 80), "on_error": ("e", 20),
    "error_container": ("e", 30), "on_error_container": ("e", 90),
    "surface": ("n", 6), "on_surface": ("n", 90),
    "surface_variant": ("v", 30), "on_surface_variant": ("v", 80),
    "surface_dim": ("n", 6), "surface_bright": ("n", 24),
    "surface_container_lowest": ("n", 4), "surface_container_low": ("n", 10),
    "surface_container": ("n", 12), "surface_container_high": ("n", 17),
    "surface_container_highest": ("n", 22),
    "outline": ("v", 60), "outline_variant": ("v", 30),
    "inverse_surface": ("n", 90), "inverse_on_surface": ("n", 20),
    "inverse_primary": ("p", 40), "shadow": ("n", 0), "scrim": ("n", 0),
    "success": ("g", 80), "on_success": ("g", 20),
    "success_container": ("g", 30), "on_success_container": ("g", 90),
    "warning": ("w", 80), "warning_container": ("w", 30), "on_warning_container": ("w", 90),
    "info": ("i", 80),
}


def palettes(seed: int) -> dict[str, TonalPalette]:
    hue, chroma, _j = cam16(seed)
    return {
        "p": TonalPalette(hue, max(chroma, 36.0)),
        "s": TonalPalette(hue, 16.0),
        "t": TonalPalette(hue + 60, 24.0),
        "n": TonalPalette(hue, 6.0),
        "v": TonalPalette(hue, 8.0),
        "e": TonalPalette(25.0, 84.0),
        "g": TonalPalette(harmonize(145.0, hue), 40.0),
        "w": TonalPalette(harmonize(70.0, hue), 48.0),
        "i": TonalPalette(harmonize(255.0, hue), 40.0),
    }


@lru_cache(maxsize=64)
def scheme(seed: int, dark: bool) -> dict[str, str]:
    """Role name -> "#RRGGBB" for the tonal-spot scheme of this seed."""
    tones = DARK_TONES if dark else LIGHT_TONES
    found = palettes(seed)
    return {role: argb_to_hex(found[key].tone(tone)) for role, (key, tone) in tones.items()}


def relative_luminance(hex_color: str) -> float:
    return y_from_lstar(tone_of(hex_to_argb(hex_color))) / 100.0


def contrast(a: str, b: str) -> float:
    la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def seed_from_pixels(pixels, fallback: int | None = None) -> int | None:
    """A source colour for a picture's pixels (ARGB ints): the hue that
    holds the most chroma, averaged over its pixels; None if it is grey."""
    bins: dict[int, list] = {}
    total = 0
    for argb in pixels:
        hue, chroma, _j = cam16(argb)
        tone = tone_of(argb)
        if chroma < 12 or not 18 <= tone <= 92:
            continue
        key = int(hue // 15)
        entry = bins.setdefault(key, [0.0, 0, 0, 0, 0])
        entry[0] += chroma
        r, g, b = argb_to_rgb(argb)
        entry[1] += r
        entry[2] += g
        entry[3] += b
        entry[4] += 1
        total += 1
    if not bins or total < max(4, len(pixels) // 50):
        return fallback
    best = max(bins.values(), key=lambda e: e[0])
    count = best[4]
    return rgb_to_argb(round(best[1] / count), round(best[2] / count), round(best[3] / count))
