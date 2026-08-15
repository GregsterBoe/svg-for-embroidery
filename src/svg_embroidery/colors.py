"""Colour parsing and normalisation.

Colours are normalised to ``#rrggbb`` so that ``white``, ``#FFF``, ``#ffffff``
and ``rgb(255,255,255)`` all count as one colour when rules count them.
"""

from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, Optional, Tuple

# Values that mean "no paint" and therefore never count as a colour.
NO_PAINT = {"none", "transparent", "currentcolor", "inherit", "context-fill", "context-stroke"}

CSS_NAMED_COLORS = {
    "aliceblue": "#f0f8ff", "antiquewhite": "#faebd7", "aqua": "#00ffff",
    "aquamarine": "#7fffd4", "azure": "#f0ffff", "beige": "#f5f5dc",
    "bisque": "#ffe4c4", "black": "#000000", "blanchedalmond": "#ffebcd",
    "blue": "#0000ff", "blueviolet": "#8a2be2", "brown": "#a52a2a",
    "burlywood": "#deb887", "cadetblue": "#5f9ea0", "chartreuse": "#7fff00",
    "chocolate": "#d2691e", "coral": "#ff7f50", "cornflowerblue": "#6495ed",
    "cornsilk": "#fff8dc", "crimson": "#dc143c", "cyan": "#00ffff",
    "darkblue": "#00008b", "darkcyan": "#008b8b", "darkgoldenrod": "#b8860b",
    "darkgray": "#a9a9a9", "darkgreen": "#006400", "darkgrey": "#a9a9a9",
    "darkkhaki": "#bdb76b", "darkmagenta": "#8b008b", "darkolivegreen": "#556b2f",
    "darkorange": "#ff8c00", "darkorchid": "#9932cc", "darkred": "#8b0000",
    "darksalmon": "#e9967a", "darkseagreen": "#8fbc8f", "darkslateblue": "#483d8b",
    "darkslategray": "#2f4f4f", "darkslategrey": "#2f4f4f", "darkturquoise": "#00ced1",
    "darkviolet": "#9400d3", "deeppink": "#ff1493", "deepskyblue": "#00bfff",
    "dimgray": "#696969", "dimgrey": "#696969", "dodgerblue": "#1e90ff",
    "firebrick": "#b22222", "floralwhite": "#fffaf0", "forestgreen": "#228b22",
    "fuchsia": "#ff00ff", "gainsboro": "#dcdcdc", "ghostwhite": "#f8f8ff",
    "gold": "#ffd700", "goldenrod": "#daa520", "gray": "#808080",
    "green": "#008000", "greenyellow": "#adff2f", "grey": "#808080",
    "honeydew": "#f0fff0", "hotpink": "#ff69b4", "indianred": "#cd5c5c",
    "indigo": "#4b0082", "ivory": "#fffff0", "khaki": "#f0e68c",
    "lavender": "#e6e6fa", "lavenderblush": "#fff0f5", "lawngreen": "#7cfc00",
    "lemonchiffon": "#fffacd", "lightblue": "#add8e6", "lightcoral": "#f08080",
    "lightcyan": "#e0ffff", "lightgoldenrodyellow": "#fafad2", "lightgray": "#d3d3d3",
    "lightgreen": "#90ee90", "lightgrey": "#d3d3d3", "lightpink": "#ffb6c1",
    "lightsalmon": "#ffa07a", "lightseagreen": "#20b2aa", "lightskyblue": "#87cefa",
    "lightslategray": "#778899", "lightslategrey": "#778899", "lightsteelblue": "#b0c4de",
    "lightyellow": "#ffffe0", "lime": "#00ff00", "limegreen": "#32cd32",
    "linen": "#faf0e6", "magenta": "#ff00ff", "maroon": "#800000",
    "mediumaquamarine": "#66cdaa", "mediumblue": "#0000cd", "mediumorchid": "#ba55d3",
    "mediumpurple": "#9370db", "mediumseagreen": "#3cb371", "mediumslateblue": "#7b68ee",
    "mediumspringgreen": "#00fa9a", "mediumturquoise": "#48d1cc",
    "mediumvioletred": "#c71585", "midnightblue": "#191970", "mintcream": "#f5fffa",
    "mistyrose": "#ffe4e1", "moccasin": "#ffe4b5", "navajowhite": "#ffdead",
    "navy": "#000080", "oldlace": "#fdf5e6", "olive": "#808000",
    "olivedrab": "#6b8e23", "orange": "#ffa500", "orangered": "#ff4500",
    "orchid": "#da70d6", "palegoldenrod": "#eee8aa", "palegreen": "#98fb98",
    "paleturquoise": "#afeeee", "palevioletred": "#db7093", "papayawhip": "#ffefd5",
    "peachpuff": "#ffdab9", "peru": "#cd853f", "pink": "#ffc0cb",
    "plum": "#dda0dd", "powderblue": "#b0e0e6", "purple": "#800080",
    "rebeccapurple": "#663399", "red": "#ff0000", "rosybrown": "#bc8f8f",
    "royalblue": "#4169e1", "saddlebrown": "#8b4513", "salmon": "#fa8072",
    "sandybrown": "#f4a460", "seagreen": "#2e8b57", "seashell": "#fff5ee",
    "sienna": "#a0522d", "silver": "#c0c0c0", "skyblue": "#87ceeb",
    "slateblue": "#6a5acd", "slategray": "#708090", "slategrey": "#708090",
    "snow": "#fffafa", "springgreen": "#00ff7f", "steelblue": "#4682b4",
    "tan": "#d2b48c", "teal": "#008080", "thistle": "#d8bfd8",
    "tomato": "#ff6347", "turquoise": "#40e0d0", "violet": "#ee82ee",
    "wheat": "#f5deb3", "white": "#ffffff", "whitesmoke": "#f5f5f5",
    "yellow": "#ffff00", "yellowgreen": "#9acd32",
}

_HEX_RE = re.compile(r"^#([0-9a-f]{3,8})$")
_RGB_RE = re.compile(
    r"^rgba?\(\s*([^,\s/]+)[\s,]+([^,\s/]+)[\s,]+([^,\s/)]+)"
    r"(?:[\s,/]+([^)\s]+))?\s*\)$"
)
_URL_RE = re.compile(r"^url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)")


def is_paint_reference(value: str) -> Optional[str]:
    """Return the referenced id for paints like ``url(#grad1)``, else ``None``."""
    match = _URL_RE.match(value.strip().lower())
    return match.group(1) if match else None


def _channel(raw: str) -> Optional[int]:
    raw = raw.strip()
    try:
        if raw.endswith("%"):
            return max(0, min(255, round(float(raw[:-1]) * 255 / 100)))
        return max(0, min(255, round(float(raw))))
    except ValueError:
        return None


def normalize_color(value: Optional[str]) -> Optional[str]:
    """Normalise a colour to ``#rrggbb``.

    Returns ``None`` for anything that is not a concrete colour: ``none``,
    gradient references, unknown keywords, or malformed values.
    """
    if value is None:
        return None
    raw = value.strip().lower()
    if not raw or raw in NO_PAINT:
        return None

    if raw in CSS_NAMED_COLORS:
        return CSS_NAMED_COLORS[raw]

    hex_match = _HEX_RE.match(raw)
    if hex_match:
        digits = hex_match.group(1)
        if len(digits) in (3, 4):  # #rgb / #rgba -> drop alpha, expand
            digits = "".join(ch * 2 for ch in digits[:3])
        elif len(digits) in (6, 8):
            digits = digits[:6]
        else:
            return None
        return "#" + digits

    rgb_match = _RGB_RE.match(raw)
    if rgb_match:
        channels = [_channel(c) for c in rgb_match.groups()[:3]]
        if any(c is None for c in channels):
            return None
        return "#{:02x}{:02x}{:02x}".format(*channels)

    return None


def contrast_label(hex_color: str) -> str:
    """Rough light/dark label, handy for report output."""
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "light" if luminance > 0.6 else "dark"


# -- perceptual colour space ----------------------------------------------
#
# Colour decisions are made in CIE Lab, not RGB. RGB distance does not match
# what the eye sees: #00ff00 and #00cc00 are far apart numerically and nearly
# indistinguishable, while #000080 and #008000 are the reverse. Reducing a
# palette in RGB merges the wrong colours.

_D65 = (0.95047, 1.0, 1.08883)


def _linearise(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _pivot(value: float) -> float:
    return value ** (1 / 3) if value > (6 / 29) ** 3 else value / (3 * (6 / 29) ** 2) + 4 / 29


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    return (int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16))


def rgb_to_hex(red: int, green: int, blue: int) -> str:
    clamp = lambda value: max(0, min(255, int(round(value))))  # noqa: E731
    return "#{:02x}{:02x}{:02x}".format(clamp(red), clamp(green), clamp(blue))


def srgb_to_lab(hex_color: str) -> Tuple[float, float, float]:
    """Convert ``#rrggbb`` to CIE L*a*b* (D65)."""
    red, green, blue = (_linearise(value / 255) for value in hex_to_rgb(hex_color))
    x = (red * 0.4124 + green * 0.3576 + blue * 0.1805) / _D65[0]
    y = (red * 0.2126 + green * 0.7152 + blue * 0.0722) / _D65[1]
    z = (red * 0.0193 + green * 0.1192 + blue * 0.9505) / _D65[2]
    fx, fy, fz = _pivot(x), _pivot(y), _pivot(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def color_distance(left: str, right: str) -> float:
    """Perceptual distance between two ``#rrggbb`` colours (CIE76)."""
    a = srgb_to_lab(left)
    b = srgb_to_lab(right)
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def nearest_color(target: str, candidates: Iterable[str]) -> Optional[str]:
    """The perceptually closest candidate, ties broken by hex for determinism."""
    options = sorted(set(candidates))
    if not options:
        return None
    return min(options, key=lambda candidate: (color_distance(target, candidate), candidate))


def blend_over(hex_color: str, alpha: float, backdrop: str = "#ffffff") -> str:
    """Composite ``hex_color`` at ``alpha`` onto an opaque backdrop."""
    alpha = max(0.0, min(1.0, alpha))
    front = hex_to_rgb(hex_color)
    back = hex_to_rgb(backdrop)
    return rgb_to_hex(*(f * alpha + b * (1 - alpha) for f, b in zip(front, back)))


def reduce_palette(weights: Dict[str, float], limit: int) -> Dict[str, str]:
    """Merge colours until at most ``limit`` remain; returns old -> new.

    Agglomerative: repeatedly merge the two perceptually closest colours, the
    lighter-used one giving way to the more-used one. Deterministic, unlike
    k-means with random seeding — the same file must always produce the same
    palette, or the fix would not be idempotent.

    Survivors are always colours that were *in the design*. Averaging clusters
    would invent shades that no thread matches.
    """
    colors = sorted(weights)
    if len(colors) <= limit:
        return {color: color for color in colors}

    # Each cluster: representative -> the originals that map to it.
    clusters: Dict[str, List[str]] = {color: [color] for color in colors}
    cluster_weight = dict(weights)

    while len(clusters) > limit:
        representatives = sorted(clusters)
        best = None
        for index, left in enumerate(representatives):
            for right in representatives[index + 1 :]:
                distance = color_distance(left, right)
                if best is None or (distance, left, right) < best[0]:
                    best = ((distance, left, right), left, right)
        assert best is not None
        _, left, right = best

        # The more-used colour survives; ties go to the lower hex value.
        keep, drop = (
            (left, right)
            if (cluster_weight[left], right) >= (cluster_weight[right], left)
            else (right, left)
        )
        clusters[keep].extend(clusters.pop(drop))
        cluster_weight[keep] += cluster_weight.pop(drop)

    return {
        original: representative
        for representative, originals in clusters.items()
        for original in originals
    }
