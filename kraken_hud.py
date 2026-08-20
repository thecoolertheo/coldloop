#!/usr/bin/env python3
"""Live telemetry HUD for the NZXT Kraken 2024 Elite RGB 640x640 circular LCD.

Renders coolant temperature as the hero stat, with CPU/GPU/RAM rings and an
optional FPS readout, then pushes the frame to the LCD via liquidctl.

Every liquidctl invocation used here is documented in VERIFIED_COMMANDS.md and
was checked against the installed driver source and the real device.

Usage:
    kraken_hud.py                          # run forever, pushing to the LCD
    kraken_hud.py --preview                # render one frame to a PNG, no device
    kraken_hud.py --preview --output x.png
    kraken_hud.py --once                   # push exactly one frame, then exit
"""

from __future__ import annotations

import argparse
import colorsys
import contextlib
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import psutil
from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
LIQUIDCTL = str(HERE / "venv" / "bin" / "liquidctl")
if not os.path.exists(LIQUIDCTL):
    LIQUIDCTL = "liquidctl"

MATCH = "Kraken"

# The only cooler this project has been verified against. `MATCH` above is a
# description substring and happily selects any Kraken, which is fine for
# reading status but not for what the service does next: it applies a pump and
# fan curve at boot, before anyone can log in and stop it. Those duties were
# checked against this device's driver table and no other, so `--wait-for-device`
# refuses anything else rather than applying an unverified curve to it.
SUPPORTED_VENDOR_ID = 0x1E71
SUPPORTED_PRODUCT_ID = 0x3012
SUPPORTED_NAME = "NZXT Kraken 2024 Elite RGB (1e71:3012)"

SIZE = 640
CENTER = SIZE / 2.0

# Frames are written to tmpfs, not the SSD: this loop runs forever and would
# otherwise rewrite the same file every couple of seconds for the life of the
# machine.
FRAME_PATH = Path(os.environ.get("KRAKEN_FRAME_PATH", "/dev/shm/kraken_hud_frame.png"))

# Serialises liquidctl access across every process in this suite. The device is
# a HID endpoint with no arbitration: two processes talking to it at once steal
# each other's reply reports, which surfaces as a status read of 0 rpm / 0%
# rather than as an error. The controller GUI takes the same lock.
LOCK_PATH = Path(os.environ.get("KRAKEN_LOCK_PATH", "/dev/shm/kraken_liquidctl.lock"))

# Latest device reading, published for the GUI so it does not need to poll the
# device itself while this service is running.
STATUS_PATH = Path(os.environ.get("KRAKEN_STATUS_PATH", "/dev/shm/kraken_status.json"))

DEFAULT_INTERVAL = 2.0

# If the device stays unreachable for this many consecutive frames, stop
# trying in-process and exit non-zero instead. The unit file's
# StartLimitIntervalSec/Burst and Restart=on-failure exist specifically to
# reapply a clean state after a failure like this ("systemd gives up instead
# of reapplying a bad state forever" -- see liquidctl.service), but that only
# works if the process actually exits when it's stuck. Before this, a
# sustained failure (observed in the wild as `ValueError: The device has no
# langid`, a transient USB-level dropout distinct from the LCD bucket-wrap
# AssertionError below) left run_loop() retrying forever and returning 0,
# so the LCD stayed black until someone noticed and restarted the service
# by hand. 12 frames is a couple minutes of genuine unreachability -- long
# enough that a fresh process (and the ExecStartPre re-initialize) is worth
# trying, short enough that the screen doesn't stay dark all evening.
MAX_CONSECUTIVE_FAILURES = 12

# Coolant range mapped onto the hero gauge sweep and colour ramp. Stays in
# Celsius, matching what the device itself reports -- only the on-screen text
# is converted to Fahrenheit, so this range doesn't need to change with it.
COOLANT_MIN = 20.0
COOLANT_MAX = 55.0


def c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


# FPS is only shown when a provider produced a sample this recently.
FPS_STALE_AFTER = 10.0

# Display-only ceilings for the pump/fan RPM gauges in the "hardware" style.
# These size the gauge fill, nothing else -- they are not sent to the device
# and don't need to match its real limits exactly. Ballparked from this
# Kraken 2024 Elite's observed pump/fan RPM at 100% duty, with headroom.
PUMP_RPM_MAX = 3200.0
FAN_RPM_MAX = 2200.0

# --------------------------------------------------------------------------
# Palette
#
# Ring colours are decorative and deliberately pale (1.2-2.5:1 against white).
# Every colour used for *text* was checked with the WCAG relative-luminance
# formula and clears 3:1 against white for large bold text; the two used for
# small text clear 4.5:1. See contrast_report() below, which recomputes this
# from the palette so the numbers cannot silently drift.
# --------------------------------------------------------------------------

WHITE = (255, 255, 255)

RING_TRACK = "#ddf5f0"  # near-white mint, the unfilled part of every gauge
RING_CPU = "#2dd4bf"  # teal-400
RING_GPU = "#67e8f9"  # cyan-300, clearly bluer than CPU
RING_RAM = "#a7f3d0"  # mint-200, much paler so it never reads as CPU

TEXT_HERO = "#0f766e"  # 5.47:1
TEXT_VALUE = "#115e59"  # 7.58:1
TEXT_LABEL = "#0f766e"  # 5.47:1
TEXT_MUTED = "#134e4a"  # 9.48:1

# Hero ramp: cool -> warm, capped at a light-medium teal. Deliberately never
# goes dark or saturated, even at maximum temperature; the fill length carries
# the same signal so the encoding stays readable without relying on hue.
HERO_RAMP = ((0.0, "#99f6e4"), (0.5, "#2dd4bf"), (1.0, "#14b8a6"))

# Dark palette, used only by the "night" face. Every other style draws dark
# ink on white, so those colours are contrast-checked against white by
# contrast_report(); these are checked against NIGHT_BG instead, since a
# colour that is safely readable on white is usually the opposite here.
NIGHT_BG = "#04211f"  # near-black teal
NIGHT_TRACK = "#0d3b38"  # unfilled gauge, barely above the background
NIGHT_HERO = "#5eead4"  # 11.43:1 on NIGHT_BG
NIGHT_VALUE = "#99f6e4"  # 13.41:1
NIGHT_LABEL = "#2dd4bf"  # 9.08:1
NIGHT_MUTED = "#14b8a6"  # 6.79:1

# --------------------------------------------------------------------------
# Theming
#
# The names above are the *default* teal palette and stay the source of truth
# for it. Everything below lets those module-level names be re-pointed at a
# user-chosen primary/secondary/tertiary, which is why the render functions
# read them as globals rather than capturing them in default arguments -- a
# default argument binds once at import and would silently keep the old
# colour after a theme change (draw_bar's `track` was exactly that bug).
#
# Only the three base colours are user-supplied. Every text shade is
# *derived* from them by moving lightness until it meets a contrast target,
# rather than being taken as given: a hand-picked hex that looks nice as a
# gauge fill is usually unreadable as 18px text, and the whole point of the
# WCAG work in this file is that the HUD cannot become illegible. A user can
# therefore pick any colour they like without being able to render the
# readouts unreadable.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    primary: str
    secondary: str
    tertiary: str


DEFAULT_PALETTE = Palette(primary=RING_CPU, secondary=RING_GPU, tertiary=RING_RAM)

PALETTE_PATH = Path(
    os.environ.get(
        "KRAKEN_PALETTE_PATH",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "coldloop" / "palette.json",
    )
)

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def normalize_hex(value: str) -> str:
    """Validate and canonicalise a user-supplied colour, or raise ValueError."""
    text = value.strip()
    if not _HEX_RE.match(text):
        raise ValueError(f"not a 6-digit hex colour: {value!r}")
    return "#" + text.lstrip("#").lower()


def _rgb_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{min(max(round(c), 0), 255):02x}" for c in rgb)


def _with_lightness(color: str, lightness: float, sat_scale: float = 1.0) -> str:
    r, g, b = (c / 255.0 for c in hex_rgb(color))
    h, _, s = colorsys.rgb_to_hls(r, g, b)
    out = colorsys.hls_to_rgb(h, min(max(lightness, 0.0), 1.0), min(max(s * sat_scale, 0.0), 1.0))
    return _rgb_hex(tuple(c * 255.0 for c in out))


def _shade_for_contrast(color: str, bg: str, target: float) -> str:
    """Keep ``color``'s hue, move its lightness until it clears ``target``.

    Walks toward whichever end of the lightness scale increases contrast with
    ``bg`` and stops at the first shade that clears the target, so the result
    is the most vivid shade that is still legible rather than a needlessly
    washed-out or nearly-black one. Falls back to the extreme if even that
    cannot reach the target (possible for a mid-grey on a mid-grey ground).
    """
    r, g, b = (c / 255.0 for c in hex_rgb(color))
    _, current, _ = colorsys.rgb_to_hls(r, g, b)
    # Darken against a light background, lighten against a dark one.
    toward_dark = relative_luminance(bg) > 0.18
    steps = 60
    for i in range(steps + 1):
        t = i / steps
        lightness = current * (1.0 - t) if toward_dark else current + (1.0 - current) * t
        candidate = _with_lightness(color, lightness)
        if contrast_ratio(candidate, bg) >= target:
            return candidate
    return _with_lightness(color, 0.0 if toward_dark else 1.0)


# The role -> colour mapping for a non-default palette. Contrast targets match
# the floors asserted in contrast_report(), with headroom so that rounding to
# 8-bit channels cannot land a shade fractionally under its floor.
def derive_palette(p: Palette) -> dict[str, object]:
    if p == DEFAULT_PALETTE:
        # The shipped teal was hand-tuned (see the comments on each constant);
        # regenerating it mechanically would produce something close but not
        # identical, so "reset to default" returns the real original rather
        # than an approximation of it.
        return {
            "RING_TRACK": "#ddf5f0",
            "RING_CPU": "#2dd4bf",
            "RING_GPU": "#67e8f9",
            "RING_RAM": "#a7f3d0",
            "TEXT_HERO": "#0f766e",
            "TEXT_VALUE": "#115e59",
            "TEXT_LABEL": "#0f766e",
            "TEXT_MUTED": "#134e4a",
            "HERO_RAMP": ((0.0, "#99f6e4"), (0.5, "#2dd4bf"), (1.0, "#14b8a6")),
            "NIGHT_BG": "#04211f",
            "NIGHT_TRACK": "#0d3b38",
            "NIGHT_HERO": "#5eead4",
            "NIGHT_VALUE": "#99f6e4",
            "NIGHT_LABEL": "#2dd4bf",
            "NIGHT_MUTED": "#14b8a6",
        }

    night_bg = _with_lightness(p.primary, 0.075, sat_scale=0.85)
    return {
        "RING_TRACK": _with_lightness(p.primary, 0.91, sat_scale=0.55),
        "RING_CPU": p.primary,
        "RING_GPU": p.secondary,
        "RING_RAM": p.tertiary,
        "TEXT_HERO": _shade_for_contrast(p.primary, "#ffffff", 4.6),
        "TEXT_VALUE": _shade_for_contrast(p.primary, "#ffffff", 7.0),
        "TEXT_LABEL": _shade_for_contrast(p.primary, "#ffffff", 4.6),
        "TEXT_MUTED": _shade_for_contrast(p.primary, "#ffffff", 9.0),
        "HERO_RAMP": (
            (0.0, _with_lightness(p.primary, 0.78)),
            (0.5, p.primary),
            (1.0, _with_lightness(p.primary, 0.40)),
        ),
        "NIGHT_BG": night_bg,
        "NIGHT_TRACK": _with_lightness(p.primary, 0.14, sat_scale=0.8),
        "NIGHT_HERO": _shade_for_contrast(p.primary, night_bg, 7.0),
        "NIGHT_VALUE": _shade_for_contrast(p.secondary, night_bg, 7.0),
        "NIGHT_LABEL": _shade_for_contrast(p.primary, night_bg, 4.6),
        "NIGHT_MUTED": _shade_for_contrast(p.tertiary, night_bg, 4.6),
    }


ACTIVE_PALETTE = DEFAULT_PALETTE


def apply_palette(p: Palette) -> None:
    """Re-point the module-level colour names at ``p``'s derived roles."""
    global ACTIVE_PALETTE
    ACTIVE_PALETTE = p
    globals().update(derive_palette(p))


def load_palette() -> Palette:
    """Read the saved palette, falling back to the default on any problem.

    A corrupt or hand-edited config must never take the HUD down: the service
    runs unattended, and a bad colour is worth a warning and a default frame,
    not a crash loop.
    """
    try:
        raw = json.loads(PALETTE_PATH.read_text())
        return Palette(
            primary=normalize_hex(raw["primary"]),
            secondary=normalize_hex(raw["secondary"]),
            tertiary=normalize_hex(raw["tertiary"]),
        )
    except FileNotFoundError:
        return DEFAULT_PALETTE
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"[hud] ignoring unreadable palette {PALETTE_PATH}: {exc}", file=sys.stderr)
        return DEFAULT_PALETTE


def save_palette(p: Palette) -> None:
    PALETTE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PALETTE_PATH.with_suffix(".tmp.json")
    tmp.write_text(
        json.dumps({"primary": p.primary, "secondary": p.secondary, "tertiary": p.tertiary}, indent=2)
        + "\n"
    )
    os.replace(tmp, PALETTE_PATH)  # atomic, so a reader never sees a half-written file


def reset_palette() -> None:
    """Forget any saved palette and go back to the shipped teal."""
    PALETTE_PATH.unlink(missing_ok=True)
    apply_palette(DEFAULT_PALETTE)


FACE_PATH = PALETTE_PATH.with_name("face.json")


def load_face() -> tuple[str, str]:
    """The face the service should show, and how to transition to it."""
    try:
        raw = json.loads(FACE_PATH.read_text())
    except FileNotFoundError:
        return DEFAULT_STYLE, DEFAULT_TRANSITION
    except (ValueError, TypeError, OSError) as exc:
        print(f"[hud] ignoring unreadable face {FACE_PATH}: {exc}", file=sys.stderr)
        return DEFAULT_STYLE, DEFAULT_TRANSITION

    style = raw.get("style") if isinstance(raw, dict) else None
    # resolve_style also covers custom:<name>, including a face deleted while
    # it was the selected one.
    style = resolve_style(style) if isinstance(style, str) else DEFAULT_STYLE

    transition = raw.get("transition", DEFAULT_TRANSITION) if isinstance(raw, dict) else None
    if transition not in TRANSITIONS:
        transition = DEFAULT_TRANSITION
    return style, transition


def save_face(style: str, transition: str | None = None) -> None:
    """Write the face config, preserving the transition unless one is given."""
    if transition is None:
        _, transition = load_face()
    FACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FACE_PATH.with_suffix(".tmp.json")
    tmp.write_text(json.dumps({"style": style, "transition": transition}, indent=2) + "\n")
    os.replace(tmp, FACE_PATH)


def _config_mtimes() -> tuple[float | None, float | None]:
    """Modification times of the palette and face files.

    Deleting either (a reset) has to register as a change too, which is why a
    missing file is None rather than 0.0.
    """
    stamps = []
    for path in (PALETTE_PATH, FACE_PATH):
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            stamps.append(None)
    return stamps[0], stamps[1]



# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

HERO_R, HERO_TH = 278.0, 30.0
ZONE_R, ZONE_TH = 240.0, 22.0
HERO_SWEEP = 270.0  # bottom gap
ZONE_SPAN = 100.0  # each of CPU/GPU/RAM, with 20 degree gaps between

# Hero number size. At the diagonal CPU/RAM positions, the zone text block
# below sits close underneath the hero number's bounding box; this size and
# ZONE_TEXT_R/ZONE_R below were solved together (not eyeballed) by measuring
# actual PIL textbbox() extents for the worst case -- "130.8" hero text at
# black weight against "CPU / 100% / 212°" zone text -- so that both the
# hero-to-zone gap and the zone-text-to-ring gap stay >= 10px. Re-solve rather
# than eyeball it if either the font or these strings change.
HERO_SIZE = 126

# Radius for the CPU/GPU/RAM label/value/sub-value text blocks.
ZONE_TEXT_R = 170.0
ZONE_LABEL_SIZE = 18
ZONE_VALUE_SIZE = 34
ZONE_SUB_SIZE = 19

# Content is kept inside this radius; beyond it the frame fades to white so the
# bezel of the 2.36" circular panel never clips anything meaningful.
VIGNETTE_START = 296.0
VIGNETTE_END = 320.0

# "dial" face: the scale arc, and the needle's centre hub.
DIAL_R = 272.0
DIAL_HUB_R = 15.0

# "orbit" face: three nested full circles. The outermost stays inside
# VIGNETTE_START, and the innermost clears the centred hero number.
ORBIT_R0, ORBIT_R1, ORBIT_R2 = 282.0, 250.0, 218.0
ORBIT_TH = 20.0

# "trend" face: the smallest temperature span the graph will scale to, in
# degrees C. Without a floor, an idle machine whose coolant wanders by 0.1 C
# would render that noise as a full-height mountain range.
TREND_MIN_SPAN = 3.0


def hex_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(float(int(value[i : i + 2], 16)) for i in (0, 2, 4))  # type: ignore[return-value]


def _srgb_to_linear(channel: float) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    r, g, b = hex_rgb(color)
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def contrast_ratio(color: str, other: str = "#ffffff") -> float:
    a, b = relative_luminance(color), relative_luminance(other)
    if a < b:
        a, b = b, a
    return (a + 0.05) / (b + 0.05)


def contrast_report() -> list[tuple[str, str, float, bool]]:
    """Recompute text contrast from the palette. Used by --check-contrast.

    Each entry carries the background it is actually drawn on: the light
    faces are ink on white, the "night" face inverts that, and checking its
    colours against white would report a comfortable pass for text that is
    in fact invisible on the dark ground it really uses.
    """
    checks = [
        ("hero number", TEXT_HERO, "#ffffff", 3.0),
        ("stat values", TEXT_VALUE, "#ffffff", 4.5),
        ("stat labels", TEXT_LABEL, "#ffffff", 4.5),
        ("muted text", TEXT_MUTED, "#ffffff", 4.5),
        ("night hero", NIGHT_HERO, NIGHT_BG, 3.0),
        ("night values", NIGHT_VALUE, NIGHT_BG, 4.5),
        ("night labels", NIGHT_LABEL, NIGHT_BG, 4.5),
        ("night muted", NIGHT_MUTED, NIGHT_BG, 4.5),
    ]
    return [
        (name, color, contrast_ratio(color, bg), contrast_ratio(color, bg) >= floor)
        for name, color, bg, floor in checks
    ]


def ramp_color(ramp, t: float) -> tuple[float, float, float]:
    t = min(max(t, 0.0), 1.0)
    for i in range(len(ramp) - 1):
        t0, c0 = ramp[i]
        t1, c1 = ramp[i + 1]
        if t0 <= t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            a, b = hex_rgb(c0), hex_rgb(c1)
            return tuple(a[k] + (b[k] - a[k]) * f for k in range(3))  # type: ignore[return-value]
    return hex_rgb(ramp[-1][1])


# --------------------------------------------------------------------------
# Fonts
#
# Font files live at different paths on different distros. A hardcoded path
# that does not exist makes PIL silently fall back to a ~10px bitmap font, so
# the HUD renders but is unreadable. fontconfig is the source of truth here;
# a hardcoded path is only ever used as a fast path after confirming it
# actually exists, never as the sole mechanism.
#
# Preference order: SF Pro (installed on this machine, and the clean
# geometric look the ring design was already modelled on -- see the Apple
# Watch reference in the design brief), then Montserrat, then Liberation
# Sans, then whatever fontconfig calls generic sans-serif. Any of these may
# be absent on a given system; the chain is walked at render time.
# --------------------------------------------------------------------------

FONT_FAMILIES = ("SF Pro", "Montserrat", "Liberation Sans", "sans-serif")

# SF Pro ships here as a single variable-weight TTF. PIL loads a variable font
# at its default instance (weight axis default = 400, i.e. Regular) unless a
# named instance is selected explicitly, and fc-match's choice of style is not
# consulted for that -- it always resolves to the same physical file
# regardless of weight. These are SF Pro's own unprefixed weight instance
# names, confirmed via font.get_variation_names() on this machine.
_VARIABLE_WEIGHT_INSTANCES = {
    "regular": b"Regular",
    "medium": b"Medium",
    "semibold": b"Semibold",
    "bold": b"Bold",
    "black": b"Black",
}

_FONT_FAST_PATH = {
    "Liberation Sans": (
        "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ),
}


_font_file_cache: dict[tuple[str, str], str] = {}
_font_cache: dict[tuple[int, str, tuple], ImageFont.FreeTypeFont] = {}


def _fc_match(query: str) -> str | None:
    try:
        out = subprocess.run(
            ["fc-match", "--format=%{file}", query], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = out.stdout.strip()
    if out.returncode == 0 and path and os.path.exists(path):
        return path
    return None


def resolve_font_file(family: str, weight: str) -> str | None:
    cache_key = (family, weight)
    if cache_key in _font_file_cache:
        return _font_file_cache[cache_key]

    for candidate in _FONT_FAST_PATH.get(family, ()):
        if os.path.exists(candidate):
            _font_file_cache[cache_key] = candidate
            return candidate

    for query in (f"{family}:weight={weight}", family):
        path = _fc_match(query)
        if path:
            _font_file_cache[cache_key] = path
            return path

    return None


def font(
    size: int, weight: str = "bold", families: tuple[str, ...] = FONT_FAMILIES
) -> ImageFont.FreeTypeFont:
    """Resolve a font at the given pixel size and semantic weight.

    Walks `families` in order and returns the first one fontconfig can
    resolve. For a variable-weight file (SF Pro here) the desired weight is
    applied via PIL's named-instance API, since the file itself doesn't
    change with weight the way separate Bold/Black files do.
    """
    key = (size, weight, families)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached

    for family in families:
        path = resolve_font_file(family, weight)
        if not path:
            continue
        try:
            loaded = ImageFont.truetype(path, size)
        except OSError as exc:
            print(f"[hud] could not load font {path}: {exc}", file=sys.stderr)
            continue

        try:
            names = loaded.get_variation_names()
        except Exception:
            names = None
        if names:
            target = _VARIABLE_WEIGHT_INSTANCES.get(weight, b"Bold")
            if target in names:
                with contextlib.suppress(OSError):
                    loaded.set_variation_by_name(target)

        _font_cache[key] = loaded
        return loaded

    # Loud, because this path means every label renders at bitmap size.
    print(f"[hud] WARNING: no scalable font found in {families}", file=sys.stderr)
    fallback = ImageFont.load_default()
    _font_cache[key] = fallback  # type: ignore[assignment]
    return fallback  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Vectorised polar rendering
#
# The polar grids are built once and reused for every frame. Arcs are drawn as
# distance fields, which gives antialiased edges and true rounded end caps for
# a couple of array ops each -- rather than stepping hundreds of tiny arc
# segments per frame, which is what made the original renderer slow.
# --------------------------------------------------------------------------


class PolarGrid:
    def __init__(self, size: int = SIZE):
        self.size = size
        cx = cy = size / 2.0
        ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
        self.dx = xs - cx + 0.5
        self.dy = ys - cy + 0.5
        self.r = np.hypot(self.dx, self.dy)
        # 0 degrees at 12 o'clock, increasing clockwise, range [-180, 180].
        self.theta = np.degrees(np.arctan2(self.dx, -self.dy)).astype(np.float32)


_grid: PolarGrid | None = None


def grid() -> PolarGrid:
    global _grid
    if _grid is None:
        _grid = PolarGrid()
    return _grid


def polar_xy(angle_deg: float, radius: float) -> tuple[float, float]:
    a = math.radians(angle_deg)
    return CENTER + radius * math.sin(a), CENTER - radius * math.cos(a)


def draw_arc(
    canvas: np.ndarray,
    color: tuple[float, float, float],
    radius: float,
    thickness: float,
    start_deg: float,
    sweep_deg: float,
) -> None:
    """Composite a rounded-cap arc onto ``canvas`` (float32 HxWx3, 0-255)."""
    if sweep_deg <= 0.0:
        return

    g = grid()
    half = thickness / 2.0
    sweep = min(sweep_deg, 360.0)

    # Work inside the annulus bounding box only.
    reach = radius + half + 2.0
    lo = max(int(CENTER - reach), 0)
    hi = min(int(math.ceil(CENTER + reach)), g.size)
    sl = slice(lo, hi)

    r = g.r[sl, sl]
    theta = g.theta[sl, sl]

    if sweep >= 360.0:
        dist = np.abs(r - radius)
    else:
        # Angular offset from the arc start, wrapped into [0, 360).
        delta = np.mod(theta - start_deg, 360.0)
        inside = delta <= sweep

        dist = np.abs(r - radius)
        if not inside.all():
            sx, sy = polar_xy(start_deg, radius)
            ex, ey = polar_xy(start_deg + sweep, radius)
            dxs, dys = g.dx[sl, sl], g.dy[sl, sl]
            cap = np.minimum(
                np.hypot(dxs - (sx - CENTER), dys - (sy - CENTER)),
                np.hypot(dxs - (ex - CENTER), dys - (ey - CENTER)),
            )
            dist = np.where(inside, dist, cap)

    # 1px linear ramp at the boundary gives clean antialiasing.
    alpha = np.clip(half + 0.5 - dist, 0.0, 1.0)
    if not alpha.any():
        return

    a = alpha[..., None]
    region = canvas[sl, sl, :]
    region *= 1.0 - a
    region += a * np.asarray(color, dtype=np.float32)


def apply_vignette(canvas: np.ndarray) -> None:
    g = grid()
    fade = np.clip((VIGNETTE_END - g.r) / (VIGNETTE_END - VIGNETTE_START), 0.0, 1.0)[..., None]
    canvas *= fade
    canvas += (1.0 - fade) * 255.0


def draw_bar(
    draw: ImageDraw.ImageDraw,
    x0: float,
    x1: float,
    y: float,
    height: float,
    frac: float,
    fill: tuple[float, float, float] | str,
    track: str | None = None,
) -> None:
    """A rounded horizontal meter bar, used by the non-circular HUD styles.

    ``track`` defaults to the *current* RING_TRACK, resolved on each call: as
    a default argument it would bind at import and keep the original teal
    after a theme change.
    """
    if track is None:
        track = RING_TRACK
    if not isinstance(fill, str):
        fill = tuple(round(c) for c in fill)  # PIL wants ints, hex_rgb() gives floats
    radius = height / 2.0
    draw.rounded_rectangle([x0, y, x1, y + height], radius=radius, fill=track)
    frac = min(max(frac, 0.0), 1.0)
    if frac <= 0.0:
        return
    # Never let the fill be narrower than its own rounded cap, or the cap
    # geometry itself would clip.
    filled_x1 = max(x0 + (x1 - x0) * frac, x0 + height)
    draw.rounded_rectangle([x0, y, filled_x1, y + height], radius=radius, fill=fill)


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


@dataclass
class Metrics:
    coolant: float | None = None
    cpu_load: float = 0.0
    cpu_temp: float | None = None
    gpu_load: float | None = None
    gpu_temp: float | None = None
    ram_percent: float = 0.0
    ram_used_gb: float = 0.0
    fps: float | None = None
    pump_rpm: int | None = None
    fan_rpm: int | None = None


@contextlib.contextmanager
def device_lock(timeout: float = 20.0):
    """Hold an exclusive lock while talking to the cooler.

    Falls through without the lock rather than failing outright if it cannot be
    acquired -- a missed frame is preferable to a stalled service.
    """
    handle = None
    try:
        handle = open(LOCK_PATH, "w")
    except OSError:
        yield False
        return

    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _run(cmd: list[str], timeout: float = 8.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[hud] command failed {cmd[0]}: {exc}", file=sys.stderr)
        return None


def _run_locked(cmd: list[str], timeout: float = 8.0) -> subprocess.CompletedProcess | None:
    """Run a liquidctl command with exclusive access to the device."""
    with device_lock():
        return _run(cmd, timeout)


def read_cpu_temp() -> float | None:
    """Read package temperature.

    psutil.sensors_temperatures() maps each chip name to a *list* of readings.
    Indexing the list directly (temps[key].current) raises AttributeError, and
    when that happens inside a broad except the temperature silently pins at 0
    forever. Each entry is therefore unpacked explicitly here.
    """
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None

    if not temps:
        return None

    # Preferred chips in order: AMD, Intel, generic ACPI.
    for chip in ("k10temp", "coretemp", "zenpower", "acpitz"):
        entries = temps.get(chip)
        if not entries:
            continue
        # Prefer the package/control label when the chip exposes several.
        for wanted in ("Tctl", "Package id 0", "Tdie"):
            for entry in entries:
                if entry.label == wanted and entry.current:
                    return float(entry.current)
        first = entries[0]
        if first.current:
            return float(first.current)

    for entries in temps.values():
        if entries and entries[0].current:
            return float(entries[0].current)
    return None


def read_gpu() -> tuple[float | None, float | None]:
    """Return (load %, temp C). Prefers NVIDIA, falls back to amdgpu sysfs."""
    proc = _run(
        [
            "nvidia-smi",
            "--query-gpu=utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        timeout=5.0,
    )
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        first = proc.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in first.split(",")]
        if len(parts) >= 2:
            try:
                return float(parts[0]), float(parts[1])
            except ValueError:
                pass

    load = temp = None
    try:
        for busy in sorted(Path("/sys/class/drm").glob("card*/device/gpu_busy_percent")):
            load = float(busy.read_text().strip())
            for hwmon in sorted((busy.parent / "hwmon").glob("hwmon*")):
                probe = hwmon / "temp1_input"
                if probe.exists():
                    temp = float(probe.read_text().strip()) / 1000.0
                    break
            break
    except (OSError, ValueError):
        pass
    return load, temp


def read_fps() -> float | None:
    """Read FPS from a pluggable provider.

    There is no system-wide FPS counter on Linux, so this reads whatever a
    provider last wrote and refuses to report a stale sample. Supported:

      1. A plain-text file (default /dev/shm/kraken_fps, override with
         KRAKEN_FPS_FILE) containing just the current FPS. Anything can write
         it.
      2. MangoHud CSV logs, if MangoHud is configured with an output_folder.

    Returns None when nothing fresh is available, which renders as "--" rather
    than a misleading 0.
    """
    now = time.time()

    path = Path(os.environ.get("KRAKEN_FPS_FILE", "/dev/shm/kraken_fps"))
    try:
        if path.exists() and now - path.stat().st_mtime <= FPS_STALE_AFTER:
            value = float(path.read_text().strip().split()[0])
            if value >= 0:
                return value
    except (OSError, ValueError, IndexError):
        pass

    log_dir = Path(
        os.environ.get("MANGOHUD_OUTPUT_FOLDER", str(Path.home() / ".local/share/MangoHud"))
    )
    try:
        logs = [p for p in log_dir.glob("*.csv") if now - p.stat().st_mtime <= FPS_STALE_AFTER]
        if logs:
            newest = max(logs, key=lambda p: p.stat().st_mtime)
            tail = newest.read_text(errors="ignore").strip().splitlines()
            for line in reversed(tail[-40:]):
                cell = line.split(",")[0].strip()
                if re.fullmatch(r"\d+(\.\d+)?", cell):
                    return float(cell)
    except (OSError, ValueError):
        pass

    return None


def read_device_status() -> tuple[float | None, int | None, int | None]:
    """Return (liquid temp, pump rpm, fan rpm) from liquidctl."""
    proc = _run_locked([LIQUIDCTL, "--match", MATCH, "status", "--json"], timeout=10.0)
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        if proc is not None and proc.stderr.strip():
            print(f"[hud] status failed: {proc.stderr.strip()}", file=sys.stderr)
        return None, None, None

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"[hud] could not parse status json: {exc}", file=sys.stderr)
        return None, None, None

    coolant = pump = fan = pump_duty = fan_duty = None
    for device in payload:
        for item in device.get("status", []):
            key, value = item.get("key", ""), item.get("value")
            if value is None:
                continue
            if key == "Liquid temperature":
                coolant = float(value)
            elif key == "Pump speed":
                pump = int(value)
            elif key == "Fan speed":
                fan = int(value)
            elif key == "Pump duty":
                pump_duty = int(value)
            elif key == "Fan duty":
                fan_duty = int(value)

    publish_status(coolant, pump, fan, pump_duty, fan_duty)
    return coolant, pump, fan


def publish_status(
    coolant: float | None,
    pump: int | None,
    fan: int | None,
    pump_duty: int | None = None,
    fan_duty: int | None = None,
) -> None:
    """Write the latest reading where the GUI can read it without the device.

    This is what keeps the controller from opening a second conversation with
    the cooler while this service is running. pump_duty/fan_duty (percent, as
    opposed to pump/fan which are rpm) are what the controller's sliders sync
    to on load, so they reflect whatever the device is actually doing rather
    than a guessed default.
    """
    payload = {
        "timestamp": time.time(),
        "coolant": coolant,
        "pump_rpm": pump,
        "fan_rpm": fan,
        "pump_duty": pump_duty,
        "fan_duty": fan_duty,
    }
    try:
        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, STATUS_PATH)
    except OSError:
        pass  # publishing is best-effort; the HUD itself does not depend on it


# Coolant samples for the "trend" face, oldest first. At the default 2s
# interval this holds roughly the last six minutes -- long enough to see a
# load spike arrive and dissipate, short enough that the line keeps moving.
HISTORY_LEN = 180
_coolant_history: deque[float] = deque(maxlen=HISTORY_LEN)


def collect() -> Metrics:
    m = Metrics()
    m.cpu_load = psutil.cpu_percent(interval=None)
    m.cpu_temp = read_cpu_temp()
    m.gpu_load, m.gpu_temp = read_gpu()

    mem = psutil.virtual_memory()
    m.ram_percent = mem.percent
    m.ram_used_gb = mem.used / (1024**3)

    m.fps = read_fps()
    # read_device_status() already degrades to (None, None, None) on any
    # failure, so a preview works identically with or without the device
    # connected -- no separate preview-mode path is needed here.
    m.coolant, m.pump_rpm, m.fan_rpm = read_device_status()

    # Recorded here rather than inside the trend face, so history accumulates
    # continuously no matter which face is on screen -- switching to "trend"
    # then shows real backlog instead of starting from an empty graph. A read
    # that failed is skipped rather than stored as a zero, which would draw a
    # cliff in the graph that never happened.
    if m.coolant is not None:
        _coolant_history.append(m.coolant)
    return m


# --------------------------------------------------------------------------
# Frame rendering
# --------------------------------------------------------------------------


_DASH_CHARS = frozenset("-‐‑‒–—")


def _draw_placeholder_bar(
    draw: ImageDraw.ImageDraw, xy: tuple[float, float], size: int, fill: str
) -> None:
    """Draw a plain rounded bar standing in for a bare "no data" dash.

    A text run made up only of dash characters rasterizes as a solid filled
    block on this system: confirmed at the raw font.getmask() bitmap level
    (mask height comes back truncated to a fraction of the font's normal
    glyph height, filled solid), reproducing identically across three
    unrelated font files (SF Pro, Montserrat, Liberation Sans) and with both
    of PIL's layout engines. It isn't a font, shaping, or anchor bug, so no
    font substitution dodges it reliably -- the same string renders fine the
    moment any other glyph shares its run. Drawing a shape instead of text
    sidesteps glyph rasterization for this case entirely.
    """
    height = max(round(size * 0.14), 4)
    width = max(round(size * 0.62), height * 2)
    x, y = xy
    x0, y0 = x - width / 2.0, y - height / 2.0
    draw.rounded_rectangle([x0, y0, x0 + width, y0 + height], radius=height / 2.0, fill=fill)


def _text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    message: str,
    size: int,
    fill: str,
    anchor: str = "mm",
    spacing: float = 0.0,
    weight: str = "bold",
) -> None:
    if message and anchor == "mm" and all(ch in _DASH_CHARS for ch in message):
        _draw_placeholder_bar(draw, xy, size, fill)
        return

    if spacing <= 0:
        draw.text(xy, message, font=font(size, weight), fill=fill, anchor=anchor)
        return

    # Manual letter-spacing for the small caps labels.
    f = font(size, weight)
    widths = [draw.textlength(ch, font=f) for ch in message]
    total = sum(widths) + spacing * (len(message) - 1)
    x = xy[0] - total / 2.0
    for ch, w in zip(message, widths):
        draw.text((x, xy[1]), ch, font=f, fill=fill, anchor="lm")
        x += w + spacing


def render_rings(m: Metrics) -> Image.Image:
    """Coolant hero gauge plus CPU/GPU/RAM zone rings and FPS. The original,
    and still the default -- the other styles below trade some of this
    density for a single-glance focus (minimal), a linear layout that reads
    more like a system monitor (bars), or the two things this device
    actually controls rather than what the rest of the PC is doing
    (hardware)."""
    canvas = np.full((SIZE, SIZE, 3), 255.0, dtype=np.float32)

    # --- hero coolant gauge -------------------------------------------------
    hero_start = -HERO_SWEEP / 2.0
    draw_arc(canvas, hex_rgb(RING_TRACK), HERO_R, HERO_TH, hero_start, HERO_SWEEP)

    if m.coolant is not None:
        frac = (m.coolant - COOLANT_MIN) / (COOLANT_MAX - COOLANT_MIN)
        frac = min(max(frac, 0.0), 1.0)
        if frac > 0.0:
            draw_arc(
                canvas,
                ramp_color(HERO_RAMP, frac),
                HERO_R,
                HERO_TH,
                hero_start,
                max(HERO_SWEEP * frac, 1.5),
            )

    # --- CPU / GPU / RAM zones ---------------------------------------------
    zones = (
        ("CPU", -120.0, RING_CPU, m.cpu_load / 100.0),
        ("GPU", 0.0, RING_GPU, (m.gpu_load or 0.0) / 100.0),
        ("RAM", 120.0, RING_RAM, m.ram_percent / 100.0),
    )
    for _, mid, color, frac in zones:
        start = mid - ZONE_SPAN / 2.0
        draw_arc(canvas, hex_rgb(RING_TRACK), ZONE_R, ZONE_TH, start, ZONE_SPAN)
        frac = min(max(frac, 0.0), 1.0)
        if frac > 0.0:
            draw_arc(canvas, hex_rgb(color), ZONE_R, ZONE_TH, start, max(ZONE_SPAN * frac, 1.5))

    apply_vignette(canvas)

    image = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(image)

    # --- hero readout -------------------------------------------------------
    _text(draw, (CENTER, 246), "COOLANT", 26, TEXT_LABEL, spacing=5.0, weight="semibold")

    if m.coolant is None:
        _text(draw, (CENTER, 318), "--", HERO_SIZE, TEXT_HERO, weight="black")
    else:
        reading = f"{c_to_f(m.coolant):.1f}"
        _text(draw, (CENTER - 14, 318), reading, HERO_SIZE, TEXT_HERO, weight="black")
        width = draw.textlength(reading, font=font(HERO_SIZE, "black"))
        _text(draw, (CENTER - 14 + width / 2 + 28, 274), "°F", 36, TEXT_MUTED, weight="medium")

    # --- zone readouts ------------------------------------------------------
    # Sub-temperatures are Fahrenheit, which can run a digit wider than
    # Celsius (e.g. "212°" vs "99°") -- accounted for in ZONE_TEXT_R above.
    readouts = (
        (
            "CPU",
            -120.0,
            f"{m.cpu_load:.0f}%",
            f"{c_to_f(m.cpu_temp):.0f}°" if m.cpu_temp else "--",
        ),
        (
            "GPU",
            0.0,
            f"{m.gpu_load:.0f}%" if m.gpu_load is not None else "--",
            f"{c_to_f(m.gpu_temp):.0f}°" if m.gpu_temp else "--",
        ),
        ("RAM", 120.0, f"{m.ram_percent:.0f}%", f"{m.ram_used_gb:.1f}G"),
    )
    for label, mid, value, sub in readouts:
        x, y = polar_xy(mid, ZONE_TEXT_R)
        _text(draw, (x, y - 23), label, ZONE_LABEL_SIZE, TEXT_LABEL, spacing=2.2, weight="semibold")
        _text(draw, (x, y + 3), value, ZONE_VALUE_SIZE, TEXT_VALUE, weight="bold")
        _text(draw, (x, y + 29), sub, ZONE_SUB_SIZE, TEXT_MUTED, weight="medium")

    # --- FPS, in the bottom gap --------------------------------------------
    fps_text = "--" if m.fps is None else f"{m.fps:.0f}"
    _text(draw, (CENTER, 470), fps_text, 34, TEXT_VALUE, weight="bold")
    _text(draw, (CENTER, 498), "FPS", 18, TEXT_LABEL, spacing=2.5, weight="semibold")

    return image


def render_minimal(m: Metrics) -> Image.Image:
    """Just the coolant reading and FPS. Reuses render_rings' exact hero
    geometry (proven not to clip against the canvas or the bezel vignette)
    with the CPU/GPU/RAM zones dropped, so there's nothing competing with
    the one number most people actually glance at this screen for."""
    canvas = np.full((SIZE, SIZE, 3), 255.0, dtype=np.float32)

    hero_start = -HERO_SWEEP / 2.0
    draw_arc(canvas, hex_rgb(RING_TRACK), HERO_R, HERO_TH, hero_start, HERO_SWEEP)

    if m.coolant is not None:
        frac = (m.coolant - COOLANT_MIN) / (COOLANT_MAX - COOLANT_MIN)
        frac = min(max(frac, 0.0), 1.0)
        if frac > 0.0:
            draw_arc(
                canvas,
                ramp_color(HERO_RAMP, frac),
                HERO_R,
                HERO_TH,
                hero_start,
                max(HERO_SWEEP * frac, 1.5),
            )

    apply_vignette(canvas)

    image = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(image)

    _text(draw, (CENTER, 246), "COOLANT", 26, TEXT_LABEL, spacing=5.0, weight="semibold")
    if m.coolant is None:
        _text(draw, (CENTER, 318), "--", HERO_SIZE, TEXT_HERO, weight="black")
    else:
        reading = f"{c_to_f(m.coolant):.1f}"
        _text(draw, (CENTER - 14, 318), reading, HERO_SIZE, TEXT_HERO, weight="black")
        width = draw.textlength(reading, font=font(HERO_SIZE, "black"))
        _text(draw, (CENTER - 14 + width / 2 + 28, 274), "°F", 36, TEXT_MUTED, weight="medium")

    fps_text = "--" if m.fps is None else f"{m.fps:.0f}"
    _text(draw, (CENTER, 470), fps_text, 34, TEXT_VALUE, weight="bold")
    _text(draw, (CENTER, 498), "FPS", 18, TEXT_LABEL, spacing=2.5, weight="semibold")

    return image


# All linear (bar-style) layouts below keep their content within a circle of
# radius ~260 around the canvas centre rather than the full 296px vignette
# safe-zone -- rounded rectangles have square corners, unlike the arcs above,
# so their true worst-case distance from centre is a corner, not an edge.
_BAR_X0, _BAR_X1 = 140.0, 500.0


def render_bars(m: Metrics) -> Image.Image:
    """Coolant up top, CPU/GPU/RAM load as horizontal meters below -- a
    linear system-monitor layout rather than the ring style's circular
    gauges."""
    image = Image.new("RGB", (SIZE, SIZE), WHITE)
    draw = ImageDraw.Draw(image)

    _text(draw, (CENTER, 150), "COOLANT", 26, TEXT_LABEL, spacing=5.0, weight="semibold")
    if m.coolant is None:
        _text(draw, (CENTER, 225), "--", 84, TEXT_HERO, weight="black")
    else:
        reading = f"{c_to_f(m.coolant):.1f}"
        _text(draw, (CENTER - 10, 225), reading, 84, TEXT_HERO, weight="black")
        width = draw.textlength(reading, font=font(84, "black"))
        _text(draw, (CENTER - 10 + width / 2 + 24, 190), "°F", 26, TEXT_MUTED, weight="medium")

    rows = (
        ("CPU", RING_CPU, m.cpu_load / 100.0, f"{m.cpu_load:.0f}%"),
        (
            "GPU",
            RING_GPU,
            (m.gpu_load or 0.0) / 100.0,
            f"{m.gpu_load:.0f}%" if m.gpu_load is not None else "--",
        ),
        ("RAM", RING_RAM, m.ram_percent / 100.0, f"{m.ram_percent:.0f}%"),
    )
    y = 280.0
    row_h = 90.0
    for label, color, frac, value in rows:
        _text(draw, (_BAR_X0, y), label, 20, TEXT_LABEL, anchor="lm", weight="semibold")
        _text(draw, (_BAR_X1, y), value, 22, TEXT_VALUE, anchor="rm", weight="bold")
        draw_bar(draw, _BAR_X0, _BAR_X1, y + 26, 24, frac, hex_rgb(color))
        y += row_h

    return image


def render_hardware(m: Metrics) -> Image.Image:
    """Pump and fan RPM as the headline, coolant as a small line up top.
    Everything else on the ring/bars/minimal styles is *software*
    telemetry (CPU/GPU/RAM/FPS); this is the one style that shows the two
    things this suite actually controls on the cooler itself. The gauge
    ceilings (PUMP_RPM_MAX/FAN_RPM_MAX) are cosmetic fill references only,
    not device limits -- they're never sent to liquidctl."""
    image = Image.new("RGB", (SIZE, SIZE), WHITE)
    draw = ImageDraw.Draw(image)

    coolant_text = "COOLANT --" if m.coolant is None else f"COOLANT {c_to_f(m.coolant):.1f}°F"
    _text(draw, (CENTER, 150), coolant_text, 28, TEXT_LABEL, weight="semibold")

    rows = (
        ("PUMP", RING_CPU, m.pump_rpm, PUMP_RPM_MAX),
        ("FAN", RING_GPU, m.fan_rpm, FAN_RPM_MAX),
    )
    y = 260.0
    row_h = 140.0
    for label, color, rpm, rpm_max in rows:
        value = "--" if rpm is None else f"{rpm} rpm"
        frac = 0.0 if rpm is None else rpm / rpm_max
        _text(draw, (_BAR_X0, y), label, 22, TEXT_LABEL, anchor="lm", spacing=1.5, weight="semibold")
        _text(draw, (_BAR_X1, y), value, 26, TEXT_VALUE, anchor="rm", weight="bold")
        draw_bar(draw, _BAR_X0, _BAR_X1, y + 30, 34, frac, hex_rgb(color))
        y += row_h

    return image


def render_dial(m: Metrics) -> Image.Image:
    """Coolant as a swept needle over a tick scale, read like an analogue
    gauge. Every other face encodes a value as the *length* of something
    filled in; this one encodes it as an angle against fixed graduations, so
    the reading is positional -- you learn where "normal" sits on the dial and
    notice deviation without reading the number at all."""
    canvas = np.full((SIZE, SIZE, 3), 255.0, dtype=np.float32)

    span = 260.0  # leaves a wide gap at the bottom for the readout
    start = -span / 2.0
    draw_arc(canvas, hex_rgb(RING_TRACK), DIAL_R, 14.0, start, span)

    frac = None
    if m.coolant is not None:
        frac = min(max((m.coolant - COOLANT_MIN) / (COOLANT_MAX - COOLANT_MIN), 0.0), 1.0)

    apply_vignette(canvas)
    image = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(image)

    # Graduations: a longer, labelled tick every 5 degrees C, a short one
    # between. Drawn as lines rather than arcs because a tick is radial.
    steps = int(round(COOLANT_MAX - COOLANT_MIN))
    for i in range(steps + 1):
        t = i / steps
        angle = start + span * t
        major = i % 5 == 0
        inner = DIAL_R - (26.0 if major else 14.0)
        x0, y0 = polar_xy(angle, inner)
        x1, y1 = polar_xy(angle, DIAL_R - 9.0)
        draw.line([x0, y0, x1, y1], fill=TEXT_MUTED if major else RING_TRACK, width=3 if major else 2)
        if major:
            lx, ly = polar_xy(angle, inner - 20.0)
            _text(draw, (lx, ly), f"{c_to_f(COOLANT_MIN + (COOLANT_MAX - COOLANT_MIN) * t):.0f}", 17, TEXT_MUTED, weight="medium")

    # Needle: a tapered triangle from a hub, not a plain line, so the pointing
    # end is unambiguous at a glance.
    if frac is not None:
        angle = start + span * frac
        tip = polar_xy(angle, DIAL_R - 34.0)
        left = polar_xy(angle - 90.0, DIAL_HUB_R * 0.75)
        right = polar_xy(angle + 90.0, DIAL_HUB_R * 0.75)
        draw.polygon([tip, left, right], fill=tuple(round(c) for c in ramp_color(HERO_RAMP, frac)))
    draw.ellipse(
        [CENTER - DIAL_HUB_R, CENTER - DIAL_HUB_R, CENTER + DIAL_HUB_R, CENTER + DIAL_HUB_R],
        fill=TEXT_HERO,
    )

    _text(draw, (CENTER, 430), "COOLANT", 22, TEXT_LABEL, spacing=4.0, weight="semibold")
    reading = "--" if m.coolant is None else f"{c_to_f(m.coolant):.1f}°F"
    _text(draw, (CENTER, 478), reading, 52, TEXT_HERO, weight="black")

    return image


def render_trend(m: Metrics) -> Image.Image:
    """Coolant temperature over the last few minutes as a filled line graph.

    The only face that shows history rather than an instant: every other one
    answers "what is it now", this answers "where is it heading", which is the
    question that actually matters when you are watching a cooler under load.
    Samples come from _coolant_history, filled by collect() on every frame."""
    image = Image.new("RGB", (SIZE, SIZE), WHITE)
    draw = ImageDraw.Draw(image)

    _text(draw, (CENTER, 132), "COOLANT TREND", 22, TEXT_LABEL, spacing=4.0, weight="semibold")
    reading = "--" if m.coolant is None else f"{c_to_f(m.coolant):.1f}°F"
    _text(draw, (CENTER, 186), reading, 68, TEXT_HERO, weight="black")

    # Plot box, kept inside the safe circular radius at its corners.
    x0, x1 = 150.0, 490.0
    y0, y1 = 250.0, 430.0

    history = list(_coolant_history)
    if len(history) < 2:
        _text(draw, (CENTER, (y0 + y1) / 2.0), "GATHERING DATA", 22, TEXT_MUTED, spacing=3.0, weight="semibold")
        return image

    # Scale to the observed range, not the full COOLANT_MIN..MAX span: idle
    # coolant barely moves, and a fixed axis would flatten every real change
    # into a dead straight line. A floor on the span stops sensor jitter of a
    # tenth of a degree from being magnified into a dramatic mountain range.
    lo, hi = min(history), max(history)
    if hi - lo < TREND_MIN_SPAN:
        mid = (lo + hi) / 2.0
        lo, hi = mid - TREND_MIN_SPAN / 2.0, mid + TREND_MIN_SPAN / 2.0

    points = []
    for i, value in enumerate(history):
        x = x0 + (x1 - x0) * (i / (len(history) - 1))
        y = y1 - (y1 - y0) * ((value - lo) / (hi - lo))
        points.append((x, y))

    draw.polygon([(x0, y1), *points, (x1, y1)], fill=RING_TRACK)
    draw.line(points, fill=TEXT_HERO, width=4, joint="curve")

    # Axis labels: the range the graph is actually scaled to, so a dramatic
    # looking slope can be read for what it is worth.
    _text(draw, (x0 - 12, y0), f"{c_to_f(hi):.0f}°", 18, TEXT_MUTED, anchor="rm", weight="medium")
    _text(draw, (x0 - 12, y1), f"{c_to_f(lo):.0f}°", 18, TEXT_MUTED, anchor="rm", weight="medium")
    minutes = len(history) * DEFAULT_INTERVAL / 60.0
    _text(draw, (CENTER, 468), f"LAST {minutes:.0f} MIN", 18, TEXT_LABEL, spacing=2.5, weight="semibold")

    return image


def render_orbit(m: Metrics) -> Image.Image:
    """Three nested full-circle rings -- CPU, GPU, RAM -- around a plain
    coolant number. The "rings" face splits its ring into three arc segments
    sitting side by side at one radius; here each stat owns a complete circle
    at its own radius, so they can be compared by how far round each has
    travelled rather than by reading three separate arcs."""
    canvas = np.full((SIZE, SIZE, 3), 255.0, dtype=np.float32)

    tracks = (
        (ORBIT_R0, RING_CPU, m.cpu_load / 100.0),
        (ORBIT_R1, RING_GPU, (m.gpu_load or 0.0) / 100.0),
        (ORBIT_R2, RING_RAM, m.ram_percent / 100.0),
    )
    for radius, color, frac in tracks:
        draw_arc(canvas, hex_rgb(RING_TRACK), radius, ORBIT_TH, 0.0, 360.0)
        frac = min(max(frac, 0.0), 1.0)
        if frac > 0.0:
            # Start at 12 o'clock so all three share a visible origin.
            draw_arc(canvas, hex_rgb(color), radius, ORBIT_TH, 0.0, max(360.0 * frac, 2.0))

    apply_vignette(canvas)
    image = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(image)

    reading = "--" if m.coolant is None else f"{c_to_f(m.coolant):.0f}"
    _text(draw, (CENTER, CENTER - 18), reading, 96, TEXT_HERO, weight="black")
    _text(draw, (CENTER, CENTER + 44), "°F COOLANT", 19, TEXT_LABEL, spacing=2.5, weight="semibold")

    # Legend under the number: which ring is which, with its value.
    legend = (
        ("CPU", RING_CPU, f"{m.cpu_load:.0f}%"),
        ("GPU", RING_GPU, f"{m.gpu_load:.0f}%" if m.gpu_load is not None else "--"),
        ("RAM", RING_RAM, f"{m.ram_percent:.0f}%"),
    )
    y = CENTER + 92
    for i, (label, color, value) in enumerate(legend):
        x = CENTER + (i - 1) * 86.0
        draw.ellipse([x - 26, y - 26, x - 14, y - 14], fill=color)
        _text(draw, (x + 4, y - 20), label, 16, TEXT_LABEL, spacing=1.5, weight="semibold")
        _text(draw, (x, y + 8), value, 24, TEXT_VALUE, weight="bold")

    return image


def render_night(m: Metrics) -> Image.Image:
    """The same telemetry inverted: light readouts on a near-black ground.

    Not a recolour of another face -- it is laid out for the dark ground it
    runs on. The bright ink is the *data*, the chrome recedes, so at night the
    panel reads as a few floating numbers rather than a lit white disc in a
    dark case. Its colours are contrast-checked against NIGHT_BG in
    contrast_report(), not against white like every other face."""
    image = Image.new("RGB", (SIZE, SIZE), NIGHT_BG)
    draw = ImageDraw.Draw(image)

    reading = "--" if m.coolant is None else f"{c_to_f(m.coolant):.1f}"
    _text(draw, (CENTER, 214), reading, 132, NIGHT_HERO, weight="black")
    _text(draw, (CENTER, 300), "°F COOLANT", 22, NIGHT_LABEL, spacing=5.0, weight="semibold")

    rows = (
        ("CPU", m.cpu_load / 100.0, f"{m.cpu_load:.0f}%"),
        ("GPU", (m.gpu_load or 0.0) / 100.0, f"{m.gpu_load:.0f}%" if m.gpu_load is not None else "--"),
        ("RAM", m.ram_percent / 100.0, f"{m.ram_percent:.0f}%"),
    )
    y = 358.0
    for label, frac, value in rows:
        _text(draw, (_BAR_X0, y), label, 18, NIGHT_LABEL, anchor="lm", spacing=1.5, weight="semibold")
        _text(draw, (_BAR_X1, y), value, 20, NIGHT_VALUE, anchor="rm", weight="bold")
        draw_bar(draw, _BAR_X0, _BAR_X1, y + 16, 10, frac, NIGHT_HERO, track=NIGHT_TRACK)
        y += 52.0

    return image


def render_loading(progress: float, message: str = "STARTING") -> Image.Image:
    """The frame shown while there is nothing real to show yet.

    Deliberately not in STYLES -- it is not a face you can choose, it is what
    covers the genuinely blank moments: the seconds between the device being
    initialized and the first telemetry read completing. Those used to be a
    black screen, which reads as the cooler having failed rather than as it
    starting up.
    """
    canvas = np.full((SIZE, SIZE, 3), 255.0, dtype=np.float32)
    draw_arc(canvas, hex_rgb(RING_TRACK), HERO_R, HERO_TH, 0.0, 360.0)
    # A short arc parked at `progress` around the ring: with frames arriving
    # about a second apart there is no point animating a spinner, so this
    # reads as a progress position rather than pretending to be motion.
    draw_arc(
        canvas,
        ramp_color(HERO_RAMP, progress),
        HERO_R,
        HERO_TH,
        -90.0 + 360.0 * min(max(progress, 0.0), 1.0),
        70.0,
    )
    apply_vignette(canvas)

    image = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(image)
    _text(draw, (CENTER, CENTER - 18), "COLDLOOP", 46, TEXT_HERO, spacing=6.0, weight="black")
    _text(draw, (CENTER, CENTER + 34), message, 22, TEXT_LABEL, spacing=4.0, weight="semibold")
    return image


# How a face change is presented.
#
# This device accepts about 1.8 frames per second (a `set lcd screen static`
# push measures a consistent 0.56s). That rules out a crossfade: a blend needs
# enough frames per second to read as one image dissolving into another, and
# at this rate it is instead a short slideshow of half-transparent
# double-exposures -- both faces legible at once, over roughly two seconds.
# That was tried here and looked like a rendering fault rather than a
# transition, so it is gone.
#
# What is left are the only two honest options at 1.8fps:
#   "instant" -- one push, 0.56s, the floor for this hardware. The old face
#                stays up until the new one lands; there is no black frame.
#   "loading" -- the Coldloop loading frame for one push, then the new face:
#                1.1s, and announces the change rather than popping.
# A *wipe* survives the low frame rate where a blend does not. Every frame of
# it shows a hard boundary with the old face on one side and the new one on
# the other -- each frame is a legible, intentional-looking state on its own,
# so three of them read as a sweep rather than as three broken frames. Alpha
# blending has the opposite property: its intermediate frames are only
# meaningful as part of a smooth sequence, which this device cannot deliver.
# The sweep is radial because the panel is round and the faces are built from
# arcs, so it echoes the geometry already on screen.
TRANSITIONS = ("wipe", "instant", "loading")
DEFAULT_TRANSITION = "wipe"

# Intermediate frames in a wipe, excluding the final full frame. Two puts the
# boundary at 1/3 and 2/3 of the sweep; each costs 0.56s, so this is the knob
# that trades smoothness against how long the switch takes.
WIPE_STEPS = 2


def wipe(writer: "LcdWriter", old: Image.Image, new: Image.Image) -> bool:
    """Sweep ``new`` over ``old`` like a clock hand, then land on ``new``."""
    for step in range(1, WIPE_STEPS + 1):
        if _stop:
            return False
        mask = Image.new("L", (SIZE, SIZE), 0)
        # -90 starts the sweep at 12 o'clock, matching where every gauge on
        # these faces starts filling from.
        ImageDraw.Draw(mask).pieslice(
            [0, 0, SIZE - 1, SIZE - 1],
            -90,
            -90 + 360.0 * step / (WIPE_STEPS + 1),
            fill=255,
        )
        if not writer.push(Image.composite(new, old, mask)):
            return False  # the writer already logged and counted the failure
    # The sweep only ever shows partial frames, so the complete new face has
    # to be pushed to finish: WIPE_STEPS + 1 pushes in total.
    return writer.push(new)


STYLES = {
    "rings": (
        "Telemetry rings",
        "Coolant hero gauge with CPU, GPU and RAM rings, plus FPS.",
        render_rings,
    ),
    "minimal": (
        "Minimal coolant",
        "Just the coolant reading and FPS -- nothing else competing for attention.",
        render_minimal,
    ),
    "bars": (
        "System bars",
        "Coolant reading up top, CPU/GPU/RAM load as bar meters below.",
        render_bars,
    ),
    "hardware": (
        "Pump & fan",
        "Pump and fan RPM as the headline -- what the cooler itself is doing.",
        render_hardware,
    ),
    "dial": (
        "Analogue dial",
        "Coolant as a needle swept over a tick scale, read by position.",
        render_dial,
    ),
    "trend": (
        "Coolant trend",
        "The last few minutes of coolant temperature as a line graph.",
        render_trend,
    ),
    "orbit": (
        "Orbit rings",
        "CPU, GPU and RAM as three nested full circles around the coolant number.",
        render_orbit,
    ),
    "night": (
        "Night mode",
        "Light readouts on a near-black ground, for a dark room or case.",
        render_night,
    ),
}
DEFAULT_STYLE = "rings"


# --------------------------------------------------------------------------
# Custom faces
#
# A custom face is a JSON file listing components with their own position,
# size, colour and metric binding, built in Coldloop's studio. The eight faces
# above stay hand-written Python: they encode judgements about spacing and
# legibility that a generic component renderer cannot make, and they are the
# fallback when a custom face is missing or broken.
#
# Both sides need the same vocabulary of metrics and components, so this
# module owns it and the GUI reads it from here rather than restating it --
# the one place the two files genuinely must agree, since a spec written
# against a metric this renderer does not know produces a blank component.
# --------------------------------------------------------------------------

FACES_DIR = PALETTE_PATH.parent / "faces"
CUSTOM_PREFIX = "custom:"


def _fmt(value: float | None, spec: str, suffix: str = "") -> str:
    return "--" if value is None else format(value, spec) + suffix


# key -> (menu label, text for a readout, 0..1 fill for a gauge)
CUSTOM_METRICS: dict[str, tuple[str, object, object]] = {
    "coolant": (
        "Coolant temperature",
        lambda m: _fmt(None if m.coolant is None else c_to_f(m.coolant), ".1f"),
        lambda m: None
        if m.coolant is None
        else (m.coolant - COOLANT_MIN) / (COOLANT_MAX - COOLANT_MIN),
    ),
    "cpu_load": ("CPU load", lambda m: _fmt(m.cpu_load, ".0f", "%"), lambda m: m.cpu_load / 100.0),
    "gpu_load": (
        "GPU load",
        lambda m: _fmt(m.gpu_load, ".0f", "%"),
        lambda m: None if m.gpu_load is None else m.gpu_load / 100.0,
    ),
    "ram_percent": (
        "RAM used (%)",
        lambda m: _fmt(m.ram_percent, ".0f", "%"),
        lambda m: m.ram_percent / 100.0,
    ),
    "ram_used_gb": (
        "RAM used (GB)",
        lambda m: _fmt(m.ram_used_gb, ".1f", "G"),
        lambda m: m.ram_percent / 100.0,
    ),
    "cpu_temp": (
        "CPU temperature",
        lambda m: _fmt(None if m.cpu_temp is None else c_to_f(m.cpu_temp), ".0f", "°"),
        lambda m: None if m.cpu_temp is None else (m.cpu_temp - 30.0) / 60.0,
    ),
    "gpu_temp": (
        "GPU temperature",
        lambda m: _fmt(None if m.gpu_temp is None else c_to_f(m.gpu_temp), ".0f", "°"),
        lambda m: None if m.gpu_temp is None else (m.gpu_temp - 30.0) / 60.0,
    ),
    "fps": (
        "FPS",
        lambda m: _fmt(m.fps, ".0f"),
        lambda m: None if m.fps is None else m.fps / 240.0,
    ),
    "pump_rpm": (
        "Pump RPM",
        lambda m: _fmt(m.pump_rpm, "d"),
        lambda m: None if m.pump_rpm is None else m.pump_rpm / PUMP_RPM_MAX,
    ),
    "fan_rpm": (
        "Fan RPM",
        lambda m: _fmt(m.fan_rpm, "d"),
        lambda m: None if m.fan_rpm is None else m.fan_rpm / FAN_RPM_MAX,
    ),
}

# Component type -> the fields the studio should offer for it. Kept here so
# the studio's property panel and this renderer cannot drift apart.
COMPONENT_FIELDS = {
    "arc": ("metric", "colour", "radius", "thickness", "start", "sweep", "track"),
    "bar": ("metric", "colour", "width", "height", "track"),
    "value": ("metric", "colour", "size", "weight"),
    "label": ("text", "colour", "size", "weight", "spacing"),
}


def metric_text(m: Metrics, key: str) -> str:
    entry = CUSTOM_METRICS.get(key)
    return entry[1](m) if entry else "--"


def metric_fraction(m: Metrics, key: str) -> float:
    entry = CUSTOM_METRICS.get(key)
    value = entry[2](m) if entry else None
    return 0.0 if value is None else min(max(value, 0.0), 1.0)


def _face_file(name: str) -> Path:
    # Names come from a text field, so keep them to something that cannot
    # escape the faces directory or collide with the built-in style names.
    safe = re.sub(r"[^A-Za-z0-9 _-]", "", name).strip()
    return FACES_DIR / f"{safe}.json"


def list_custom_faces() -> list[str]:
    try:
        # Leading-underscore names are the studio's scratch renders: still
        # renderable by name so its preview can use one, but never offered as
        # a selectable face in the gallery or --list-faces.
        return sorted(p.stem for p in FACES_DIR.glob("*.json") if not p.stem.startswith("_"))
    except OSError:
        return []


def load_custom_face(name: str) -> dict | None:
    try:
        spec = json.loads(_face_file(name).read_text())
    except (OSError, ValueError) as exc:
        print(f"[hud] cannot read custom face {name!r}: {exc}", file=sys.stderr)
        return None
    if not isinstance(spec, dict) or not isinstance(spec.get("components"), list):
        print(f"[hud] custom face {name!r} has no component list", file=sys.stderr)
        return None
    return spec


def save_custom_face(name: str, spec: dict) -> Path:
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    path = _face_file(name)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(spec, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def delete_custom_face(name: str) -> None:
    _face_file(name).unlink(missing_ok=True)


def render_custom(m: Metrics, spec: dict) -> Image.Image:
    """Draw a face from a component spec.

    Arcs are composited first, on the float canvas the arc drawing needs, and
    the flat components are drawn afterwards on the resulting image. That
    fixes arcs behind text and bars regardless of their order in the file --
    an acceptable limit, since arcs are the background layer in every face
    here, and the alternative is converting the canvas back and forth for
    every component.
    """
    dark = bool(spec.get("dark"))
    ground = hex_rgb(NIGHT_BG) if dark else (255.0, 255.0, 255.0)
    canvas = np.full((SIZE, SIZE, 3), 0.0, dtype=np.float32)
    canvas[:, :] = np.asarray(ground, dtype=np.float32)

    components = spec.get("components", [])
    default_track = NIGHT_TRACK if dark else RING_TRACK

    for c in components:
        if c.get("type") != "arc":
            continue
        radius = float(c.get("radius", 240.0))
        thickness = float(c.get("thickness", 24.0))
        start = float(c.get("start", -135.0))
        sweep = float(c.get("sweep", 270.0))
        if c.get("track", True):
            draw_arc(canvas, hex_rgb(c.get("track_colour", default_track)), radius, thickness, start, sweep)
        frac = metric_fraction(m, c.get("metric", ""))
        if frac > 0.0:
            draw_arc(
                canvas,
                hex_rgb(c.get("colour", RING_CPU)),
                radius,
                thickness,
                start,
                max(sweep * frac, 1.5),
            )

    if not dark:
        # The vignette fades to white, so it only suits the light ground.
        apply_vignette(canvas)

    image = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(image)

    for c in components:
        kind = c.get("type")
        x = float(c.get("x", CENTER))
        y = float(c.get("y", CENTER))
        colour = c.get("colour") or (NIGHT_VALUE if dark else TEXT_VALUE)
        if kind == "bar":
            width = float(c.get("width", 360.0))
            height = float(c.get("height", 24.0))
            draw_bar(
                draw,
                x - width / 2.0,
                x + width / 2.0,
                y - height / 2.0,
                height,
                metric_fraction(m, c.get("metric", "")),
                hex_rgb(colour),
                track=c.get("track_colour", default_track),
            )
        elif kind == "value":
            _text(
                draw,
                (x, y),
                metric_text(m, c.get("metric", "")),
                int(c.get("size", 48)),
                colour,
                weight=c.get("weight", "black"),
            )
        elif kind == "label":
            _text(
                draw,
                (x, y),
                str(c.get("text", "")),
                int(c.get("size", 22)),
                colour,
                spacing=float(c.get("spacing", 0.0)),
                weight=c.get("weight", "semibold"),
            )

    return image


def resolve_style(style: str) -> str:
    """Fall back to the default for a face that no longer exists."""
    if style.startswith(CUSTOM_PREFIX):
        name = style[len(CUSTOM_PREFIX) :]
        if name in list_custom_faces():
            return style
        print(f"[hud] custom face {name!r} is gone, using {DEFAULT_STYLE}", file=sys.stderr)
        return DEFAULT_STYLE
    return style if style in STYLES else DEFAULT_STYLE


def render(m: Metrics, style: str = DEFAULT_STYLE) -> Image.Image:
    if style.startswith(CUSTOM_PREFIX):
        spec = load_custom_face(style[len(CUSTOM_PREFIX) :])
        if spec is not None:
            return render_custom(m, spec)
        style = DEFAULT_STYLE
    _, _, render_fn = STYLES.get(style, STYLES[DEFAULT_STYLE])
    return render_fn(m)


# --------------------------------------------------------------------------
# Device output
# --------------------------------------------------------------------------


class LcdWriter:
    """Pushes frames to the LCD, recovering from the known transfer failures."""

    def __init__(self) -> None:
        self.consecutive_failures = 0
        # Wall-clock time of the first failed push in the current streak, so a
        # recovery can log how long the LCD was actually dark for. The journal
        # only ever showed the moment an error was *printed*, not how long the
        # underlying liquidctl call took to fail or how many loop iterations
        # (each interval-seconds apart) passed before a push finally
        # succeeded again -- this fills that gap for diagnosing "goes black
        # for a while" reports that don't reach MAX_CONSECUTIVE_FAILURES.
        self.failure_since: float | None = None

    def initialize(self) -> bool:
        proc = _run_locked([LIQUIDCTL, "initialize", "all"], timeout=30.0)
        ok = proc is not None and proc.returncode == 0
        if not ok:
            detail = proc.stderr.strip() if proc is not None else "no output"
            print(f"[hud] initialize failed: {detail}", file=sys.stderr)
        return ok

    def push(self, image: Image.Image) -> bool:
        FRAME_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file and rename, so liquidctl can never observe
        # a half-written PNG.
        tmp = FRAME_PATH.with_suffix(".tmp.png")
        image.save(tmp, "PNG", optimize=False, compress_level=1)
        os.replace(tmp, FRAME_PATH)

        # The device's static-image path cycles through 16 internal "buckets"
        # and wraps around by deleting and reusing bucket 0 roughly every 16
        # pushes. When that housekeeping hiccups it surfaces as
        # `AssertionError('reached max bucket')` from the driver, and the LCD
        # shows a black frame until the next push succeeds -- this is the
        # cause of the HUD randomly blinking to black. Retrying immediately
        # within this same call, instead of waiting up to `interval` seconds
        # for the next scheduled frame, keeps that blackout as short as
        # possible; a bucket error additionally gets a re-initialize first,
        # since that's the documented recovery for this device family.
        push_started = time.monotonic()
        attempts = 3
        last_err = "no output"
        for attempt in range(1, attempts + 1):
            if _stop:
                return False

            attempt_started = time.monotonic()
            proc = _run_locked(
                [LIQUIDCTL, "--match", MATCH, "set", "lcd", "screen", "static", str(FRAME_PATH)],
                timeout=30.0,
            )
            call_elapsed = time.monotonic() - attempt_started
            if proc is not None and proc.returncode == 0:
                self.consecutive_failures = 0
                if self.failure_since is not None:
                    print(
                        f"[hud] LCD recovered after {time.monotonic() - self.failure_since:.1f}s dark",
                        file=sys.stderr,
                    )
                    self.failure_since = None
                return True

            last_err = proc.stderr.strip() if proc is not None else "no output"

            if attempt < attempts:
                if _stop:
                    return False  # killed mid-push by shutdown; not a device fault
                print(
                    f"[hud] LCD push attempt {attempt} failed after {call_elapsed:.1f}s, "
                    f"retrying: {last_err}",
                    file=sys.stderr,
                )
                if "bucket" in last_err.lower():
                    self.initialize()
                time.sleep(0.4)

        # systemd signals the whole cgroup on stop, so a push in flight when
        # SIGTERM arrives gets killed with it. That is not a device fault and
        # must not count toward recovery, or shutdown would trigger a pointless
        # re-initialize on the way out.
        if _stop:
            return False

        self.consecutive_failures += 1
        if self.failure_since is None:
            self.failure_since = push_started
        print(
            f"[hud] LCD push failed after {attempts} attempts ({self.consecutive_failures}), "
            f"dark for {time.monotonic() - self.failure_since:.1f}s so far: {last_err}",
            file=sys.stderr,
        )

        # Switching LCD modes is documented as flaky on this device family; a
        # re-initialize is the known recovery. Only retried occasionally so a
        # genuinely absent device does not spin.
        if self.consecutive_failures in (2, 6) or self.consecutive_failures % 15 == 0:
            print("[hud] attempting recovery via initialize", file=sys.stderr)
            self.initialize()
        return False


_stop = False


def _handle_signal(signum, _frame) -> None:
    global _stop
    _stop = True
    print(f"[hud] received signal {signum}, stopping", file=sys.stderr)


def run_loop(interval: float, style: str = DEFAULT_STYLE) -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    writer = LcdWriter()
    writer.initialize()

    # `initialize` above resets the panel, and the first real frame is still a
    # telemetry read and a render away. Covering that gap with the loading
    # frame costs one extra push but replaces several seconds of black screen
    # -- the thing that made a restart look like a failure.
    last_image = render_loading(0.35)
    writer.push(last_image)

    psutil.cpu_percent(interval=None)  # prime the load counter

    # The face is whatever the config says, not a fixed CLI choice: the
    # gallery switches faces by writing that file, so the service stays the
    # only process pushing to the device. Two writers meant a pushed face was
    # overwritten by this loop within one interval.
    transition = DEFAULT_TRANSITION
    if FACE_PATH.exists():
        style, transition = load_face()

    frame_error_streak = 0
    palette_mtime, face_mtime = _config_mtimes()
    while not _stop:
        started = time.monotonic()

        # Pick up colour and face changes without a restart: the GUI writes
        # those files while this loop is running, and restarting the service
        # to apply them would blank the LCD for several seconds.
        current_palette_mtime, current_face_mtime = _config_mtimes()
        changed = False
        if current_palette_mtime != palette_mtime:
            palette_mtime = current_palette_mtime
            apply_palette(load_palette())
            print(f"[hud] palette reloaded: {ACTIVE_PALETTE.primary}", file=sys.stderr)
            changed = True
        if current_face_mtime != face_mtime:
            face_mtime = current_face_mtime
            new_style, transition = load_face()
            if new_style != style:
                style = new_style
                print(f"[hud] face changed: {style} ({transition})", file=sys.stderr)
                changed = True

        try:
            # Only a *face* change gets a transition: a colour change keeps
            # the same layout, so it reads as a recolour on its own.
            if changed and transition == "loading":
                writer.push(render_loading(0.5, "LOADING"))

            image = render(collect(), style)
            if changed and transition == "wipe" and last_image is not None:
                wipe(writer, last_image, image)
            else:
                writer.push(image)
            last_image = image
            frame_error_streak = 0
        except Exception as exc:  # keep the service alive across transient faults...
            print(f"[hud] frame error: {exc!r}", file=sys.stderr)
            frame_error_streak += 1

        # ...but only up to a point. If either the device (push failures) or
        # the render path (exceptions above) has been broken for this many
        # frames in a row, stop retrying in-process; see MAX_CONSECUTIVE_FAILURES.
        if writer.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            print(
                f"[hud] {writer.consecutive_failures} consecutive LCD push failures, "
                "giving up and exiting so systemd can restart us",
                file=sys.stderr,
            )
            return 1
        if frame_error_streak >= MAX_CONSECUTIVE_FAILURES:
            print(
                f"[hud] {frame_error_streak} consecutive frame errors, "
                "giving up and exiting so systemd can restart us",
                file=sys.stderr,
            )
            return 1

        elapsed = time.monotonic() - started
        remaining = interval - elapsed
        # Sleep in slices so SIGTERM is honoured promptly -- and so a face or
        # colour change is noticed while we are idle rather than at the top of
        # the next interval. Waiting out the full interval put up to `interval`
        # seconds of dead time in front of every switch, which dwarfed the
        # 0.56s the push itself costs.
        while remaining > 0 and not _stop:
            if _config_mtimes() != (palette_mtime, face_mtime):
                break
            time.sleep(min(0.25, remaining))
            remaining -= 0.25

    print("[hud] stopped", file=sys.stderr)
    return 0


def wait_for_supported_device(timeout: float) -> int:
    """Block until the verified cooler is on the USB bus. Returns an exit code.

    Two jobs in one, both needed by the service units before they touch a duty:

    Waiting, because starting at boot races USB enumeration in a way starting
    at login never did. Failing instantly there would burn the unit's whole
    restart budget in under a minute and leave it permanently failed.

    Refusing, because the pump and fan curves that run next were verified
    against one device. `--match Kraken` would happily select a Z73 and apply
    them to it, at boot, before anyone could intervene.
    """
    deadline = time.monotonic() + timeout
    seen: set[str] = set()
    while True:
        proc = _run([LIQUIDCTL, "--match", MATCH, "list", "--verbose"], timeout=15.0)
        if proc is not None and proc.returncode == 0:
            text = proc.stdout
            if f"{SUPPORTED_PRODUCT_ID:#06x}" in text and f"{SUPPORTED_VENDOR_ID:#06x}" in text:
                return 0
            # Something answering to "Kraken" is present but is not the device
            # these duties were checked against. Waiting will not change that,
            # so fail now with a message that names what was found.
            for line in text.splitlines():
                if "Product ID" in line or line.startswith("Result"):
                    seen.add(line.strip())
            if seen:
                print(
                    f"[hud] refusing to configure an unverified cooler.\n"
                    f"      supported: {SUPPORTED_NAME}\n"
                    f"      found:     {'; '.join(sorted(seen))}\n"
                    f"      Pump and fan duties in the service unit were checked "
                    f"against the supported device only.",
                    file=sys.stderr,
                )
                return 2
        if time.monotonic() >= deadline:
            print(
                f"[hud] {SUPPORTED_NAME} did not appear on the USB bus "
                f"within {timeout:.0f}s",
                file=sys.stderr,
            )
            return 1
        time.sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Kraken Elite telemetry HUD")
    parser.add_argument(
        "--wait-for-device",
        type=float,
        metavar="SECONDS",
        help="wait for the supported cooler, then exit 0; refuse any other model",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="render a single frame to a PNG and exit without touching the LCD",
    )
    parser.add_argument("--output", default=str(HERE / "hud_preview.png"), help="preview path")
    parser.add_argument("--once", action="store_true", help="push exactly one frame, then exit")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument(
        "--style",
        default=DEFAULT_STYLE,
        # Not `choices`: a custom face is "custom:<name>", and the set of
        # those changes whenever the studio saves one, so it cannot be a
        # fixed list built at parse time.
        help=f"HUD layout: one of {', '.join(sorted(STYLES))}, or custom:<name>",
    )
    parser.add_argument(
        "--check-contrast", action="store_true", help="print WCAG contrast for text colours"
    )
    parser.add_argument(
        "--set-face",
        help="switch the running HUD to this face, built-in or custom:<name>",
    )
    parser.add_argument(
        "--list-faces", action="store_true", help="print every face that can be selected"
    )
    parser.add_argument(
        "--dump-vocab",
        action="store_true",
        help="print the metric/component vocabulary as JSON (used by the studio)",
    )
    parser.add_argument(
        "--transition",
        choices=TRANSITIONS,
        help="how a face change is presented: 'instant' (0.56s) or 'loading' (1.1s)",
    )
    parser.add_argument("--primary", help="primary colour, e.g. '#c084fc' (gauges, hero text)")
    parser.add_argument("--secondary", help="secondary colour (the second gauge of each face)")
    parser.add_argument("--tertiary", help="tertiary colour (the third gauge of each face)")
    parser.add_argument(
        "--save-colours",
        "--save-colors",
        dest="save_colours",
        action="store_true",
        help="persist the given colours so the running HUD picks them up",
    )
    parser.add_argument(
        "--reset-colours",
        "--reset-colors",
        dest="reset_colours",
        action="store_true",
        help="forget any saved colours and go back to the default teal",
    )
    parser.add_argument(
        "--show-colours",
        "--show-colors",
        dest="show_colours",
        action="store_true",
        help="print the active palette and every colour derived from it",
    )
    args = parser.parse_args()

    # Checked before anything else: this is what the service units call to
    # decide whether they are allowed to touch the hardware at all.
    if args.wait_for_device is not None:
        return wait_for_supported_device(args.wait_for_device)

    if args.dump_vocab:
        # The studio builds its metric menus and property panels from this,
        # so the two files cannot drift into offering components or metrics
        # the renderer does not implement.
        json.dump(
            {
                "metrics": {key: entry[0] for key, entry in CUSTOM_METRICS.items()},
                "components": {k: list(v) for k, v in COMPONENT_FIELDS.items()},
                "weights": ["regular", "medium", "semibold", "bold", "black"],
                "size": SIZE,
                "centre": CENTER,
                "palette": {
                    "primary": ACTIVE_PALETTE.primary,
                    "secondary": ACTIVE_PALETTE.secondary,
                    "tertiary": ACTIVE_PALETTE.tertiary,
                    "text": TEXT_VALUE,
                    "label": TEXT_LABEL,
                    "track": RING_TRACK,
                    "night_bg": NIGHT_BG,
                    "night_text": NIGHT_VALUE,
                },
                "faces": list_custom_faces(),
            },
            sys.stdout,
            indent=2,
        )
        return 0

    if args.list_faces:
        for key in sorted(STYLES):
            print(f"{key:24s} {STYLES[key][0]}")
        for name in list_custom_faces():
            print(f"{CUSTOM_PREFIX + name:24s} (custom)")
        return 0

    if args.set_face or args.transition:
        style = args.set_face or load_face()[0]
        if style != resolve_style(style):
            parser.error(f"unknown face {style!r} (see --list-faces)")
        save_face(style, args.transition)
        print(f"[hud] face set to {style} ({load_face()[1]})")
        return 0

    if args.reset_colours:
        reset_palette()
        print(f"[hud] palette reset to default teal ({DEFAULT_PALETTE.primary})")
        return 0

    # A partial override keeps the saved (or default) value for the other two,
    # so "just make the primary purple" does not require restating all three.
    base = load_palette()
    try:
        chosen = Palette(
            primary=normalize_hex(args.primary) if args.primary else base.primary,
            secondary=normalize_hex(args.secondary) if args.secondary else base.secondary,
            tertiary=normalize_hex(args.tertiary) if args.tertiary else base.tertiary,
        )
    except ValueError as exc:
        parser.error(str(exc))
    apply_palette(chosen)

    if args.save_colours:
        save_palette(chosen)
        print(f"[hud] saved palette to {PALETTE_PATH}")
        # Saving is a complete action on its own -- without this, setting
        # colours from the GUI would also start a second rendering loop
        # competing with the service for the device.
        if not (args.preview or args.once or args.check_contrast):
            return 0

    if args.show_colours:
        print(f"primary   {chosen.primary}")
        print(f"secondary {chosen.secondary}")
        print(f"tertiary  {chosen.tertiary}")
        print(f"default   {'yes' if chosen == DEFAULT_PALETTE else 'no'}")
        for role, value in sorted(derive_palette(chosen).items()):
            print(f"  {role:12s} {value}")
        return 0

    if args.check_contrast:
        ok = True
        for name, color, ratio, passed in contrast_report():
            print(f"{name:14s} {color}  {ratio:5.2f}:1  {'PASS' if passed else 'FAIL'}")
            ok = ok and passed
        return 0 if ok else 1

    if args.preview:
        psutil.cpu_percent(interval=None)
        time.sleep(0.15)
        image = render(collect(), args.style)
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        image.save(out, "PNG")
        print(out)
        return 0

    if args.once:
        psutil.cpu_percent(interval=None)
        time.sleep(0.15)
        writer = LcdWriter()
        # No unconditional `initialize` here: it resets the panel, which is a
        # ~1s black screen before every single push, and the device is
        # normally already initialized by the service or its ExecStartPre.
        # If the push does fail, that is when initializing is worth its cost.
        if writer.push(render(collect(), args.style)):
            return 0
        writer.initialize()
        return 0 if writer.push(render(collect(), args.style)) else 1

    return run_loop(args.interval, args.style)


if __name__ == "__main__":
    sys.exit(main())
