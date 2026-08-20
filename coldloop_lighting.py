#!/usr/bin/env python3
"""Coldloop lighting: the Kraken's pump ring and its RGB fan chain.

WHY THIS DOES NOT SHELL OUT TO LIQUIDCTL
----------------------------------------
Every other part of Coldloop talks to the cooler by running the `liquidctl`
CLI. This module cannot, because the CLI in `venv/` refuses the commands:

    $ liquidctl --match Kraken set ring color fixed 00ff00
    ERROR: operation not supported by the device

That is not a hardware limit. It is a gap in liquidctl 1.16.0. In
`driver/kraken3.py` the table entry for PID 0x3012 -- this cooler -- points at
`_COLOR_CHANNELS_KRAKEN2023`, which is literally `{}`, so `set_color()` raises
`NotSupportedByDevice` before writing a byte. The LEDs really are on the pump:
the only NZXT device on this machine's USB bus is the Kraken itself, and the
ASUS Aura controller drives motherboard headers, not the cooler.

The protocol is known. liquidctl PR #882 implements it for exactly this PID and
is still unmerged; OpenKraken ships the same thing independently. Both credit
OpenRGB's Hue 2 work. There is no official NZXT spec.

So this module applies PR #882's changes *at runtime*, to a driver instance it
owns, and leaves the installed package alone. Editing files inside `venv/` was
the other option and is worse: `requirements.txt` pins liquidctl because
`VERIFIED_COMMANDS.md` was checked against that exact release, and a patched
copy would make the pin describe something that is not what is installed --
silently, until the next `pip install -r requirements.txt` reverted it and the
lighting stopped working for no visible reason.

WHAT PR #882 CHANGES, AND WHY EACH PIECE IS NEEDED
--------------------------------------------------
1. Colour channels for 0x3012: ring=0b001 (the pump ring), external=0b010 (the
   RGB header the bundled radiator fans chain into), sync=0b111 (both at once).
2. `fixed` on the ring is redirected to `super-fixed`. On this generation the
   firmware's own animation path makes a "fixed" colour blink off periodically;
   per-LED writes hold steady.
3. `super-fixed` is split across two HID reports. The installed version sends
   every LED under sub-command 0x10 and then an empty 0x11, which addresses
   only the first 20 LEDs. A 64-byte HID frame holds a 4-byte header plus 20
   RGB triplets, so LEDs 20-39 need their own report.

CONCURRENCY
-----------
The HUD service pushes an LCD frame to this same device roughly every two
seconds, and each push occupies it for about 0.56s. Lighting writes therefore
take the same `flock` every other Coldloop process takes. The practical
consequence shows up in `stream()`: host-computed effects contend with the HUD
for the device, which is why they are opt-in and rate-capped, and why the modes
that write only on change are the ones to prefer.
"""

from __future__ import annotations

import argparse
import colorsys
import contextlib
import fcntl
import itertools
import json
import math
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Import liquidctl from the venv even when this script is run by a system
# python, so the version in use is the pinned, verified one.
_SITE = HERE / "venv" / "lib"
if _SITE.is_dir():
    for _packages in sorted(_SITE.glob("python3.*/site-packages")):
        sys.path.insert(0, str(_packages))

try:
    from liquidctl.driver.kraken3 import _COLOR_MODES, _SPEED_VALUE, KrakenZ3
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    print(f"cannot import liquidctl: {exc}", file=sys.stderr)
    raise SystemExit(2)

# The cooler this module knows how to light. Deliberately an exact PID rather
# than the HUD's looser MATCH = "Kraken": the ring protocol was verified on
# this device only, and writing Hue 2 reports to a cooler expecting something
# else is not a risk worth taking for a cosmetic feature.
VENDOR_ID = 0x1E71
PRODUCT_ID = 0x3012

# From PR #882. Channel ids are bit masks; "sync" addresses both at once.
COLOR_CHANNELS = {"ring": 0b001, "external": 0b010, "sync": 0b111}

CHANNEL_LABELS = {
    "ring": "pump ring",
    "external": "RGB fan chain",
    "sync": "ring and fans together",
}

# `super-fixed` addresses this many LED slots. The ring itself has fewer
# (sources differ on 24 vs 40 across models); the device ignores slots past its
# own ring, so writing the full complement is correct either way.
RING_LED_SLOTS = _COLOR_MODES["super-fixed"][4]

# Shared with kraken_hud.py and kraken_controller.py: one lock for the device.
LOCK_PATH = Path(os.environ.get("KRAKEN_LOCK_PATH", "/dev/shm/kraken_liquidctl.lock"))

CONFIG_PATH = Path(
    os.environ.get(
        "COLDLOOP_LIGHTING_PATH",
        Path.home() / ".config" / "coldloop" / "lighting.json",
    )
)

# Published by the HUD every frame, so reactive mode learns the temperature
# without opening a second conversation with the cooler.
STATUS_PATH = Path(os.environ.get("KRAKEN_STATUS_PATH", "/dev/shm/kraken_status.json"))

PALETTE_PATH = Path(
    os.environ.get("KRAKEN_PALETTE_PATH", Path.home() / ".config" / "coldloop" / "palette.json")
)

# Modes computed here and streamed as per-LED frames, because this generation's
# firmware rejects its own animation modes.
ANIMATED_MODES = ("breathing", "pulse", "spectrum")

# Every mode except `off` has to be *held*, not written once.
#
# The firmware does not retain per-LED state: measured on the real cooler, a
# solid colour written once starts corrupting after 20-30 seconds, with a
# single LED on the ring and one on the fan chain reverting to green. Writing
# the same colour again clears it, so this is decay of stored state rather than
# an addressing mistake -- which is also why OpenKraken streams continuously
# instead of writing once. `off` is exempt: a decayed LED goes green, and
# green-when-it-should-be-dark is worth one stray LED rather than a process
# running forever to keep the lights switched off.
HELD_MODES = ("static", "reactive") + ANIMATED_MODES
MODES = ("off",) + HELD_MODES

# How often a held mode rewrites an unchanged colour. Comfortably inside the
# observed 20-30s decay window without adding meaningful traffic: three 64-byte
# reports, against the HUD's 2-second LCD pushes.
REFRESH_SECONDS = 8.0

# How often static and reactive wake up. They do not animate, so this only
# bounds how quickly reactive notices a temperature change.
SLOW_POLL_SECONDS = 2.0

# Reactive endpoints in coolant degrees C: 30 is a typical idle for this loop,
# 45 a sustained-load reading. Outside the range simply clamps.
REACTIVE_MIN_C = 30.0
REACTIVE_MAX_C = 45.0

DEFAULT_COOL = "#22d3ee"  # Coldloop's default primary, when no palette is saved
DEFAULT_HOT = "#f97316"

DEFAULT_CONFIG: dict = {
    "channel": "sync",
    "mode": "static",
    "colour": DEFAULT_COOL,
    "brightness": 100,
    "fps": 4.0,
}

# OpenKraken validated up to 5 fps on this hardware. Above that the lighting
# and the HUD's LCD pushes start starving each other.
MAX_FPS = 5.0
MIN_FPS = 0.5


# --------------------------------------------------------------------------
# colours
# --------------------------------------------------------------------------


def normalize_hex(value) -> str:
    """Accept '#rgb', 'rgb', '#rrggbb' or 'rrggbb'; return '#rrggbb'."""
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"not a hex colour: {value!r}")
    int(text, 16)  # raises ValueError on non-hex digits
    return "#" + text.lower()


def to_rgb(hex_colour: str) -> tuple[int, int, int]:
    text = normalize_hex(hex_colour)[1:]
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def mix(a: str, b: str, t: float) -> str:
    """Blend two colours by rotating hue, not by averaging channels.

    Channel-wise interpolation is wrong here: the reactive endpoints are teal
    and orange, near-opposite on the wheel, so their sRGB midpoint lands close
    to grey. Measured, it gave #8ea382 -- olive drab, which on a ring reads as
    a fault rather than as "the coolant is warming up". Rotating hue keeps every
    intermediate saturated: teal -> green -> yellow -> orange.
    """
    t = max(0.0, min(1.0, t))
    ha, la, sa = colorsys.rgb_to_hls(*(c / 255.0 for c in to_rgb(a)))
    hb, lb, sb = colorsys.rgb_to_hls(*(c / 255.0 for c in to_rgb(b)))

    # Shorter way round the wheel, so teal -> orange runs down through green
    # and yellow rather than up through blue and magenta.
    delta = hb - ha
    if delta > 0.5:
        delta -= 1.0
    elif delta < -0.5:
        delta += 1.0

    rgb = colorsys.hls_to_rgb((ha + delta * t) % 1.0, la + (lb - la) * t, sa + (sb - sa) * t)
    return to_hex(c * 255.0 for c in rgb)


def scale(hex_colour: str, factor: float) -> str:
    """Scale a colour toward black. Brightness has to be applied host-side:
    this generation exposes no firmware brightness command."""
    factor = max(0.0, min(1.0, factor))
    return to_hex(c * factor for c in to_rgb(hex_colour))


def palette_colours() -> tuple[str, str]:
    """Reactive endpoints, following the HUD's palette so the ring matches the
    screen it surrounds."""
    cool, hot = DEFAULT_COOL, DEFAULT_HOT
    try:
        data = json.loads(PALETTE_PATH.read_text())
    except (OSError, ValueError):
        return cool, hot
    if not isinstance(data, dict):
        return cool, hot
    with contextlib.suppress(ValueError, TypeError, KeyError):
        cool = normalize_hex(data["primary"])
    with contextlib.suppress(ValueError, TypeError, KeyError):
        hot = normalize_hex(data["tertiary"])
    return cool, hot


# --------------------------------------------------------------------------
# device
# --------------------------------------------------------------------------


@contextlib.contextmanager
def device_lock(timeout: float = 20.0):
    """Hold the shared cooler lock. Yields False if it could not be taken;
    callers proceed anyway, matching the HUD's stance that a missed frame beats
    a stall -- a colliding LED write is cosmetic."""
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


def _write_colors_hue2(self, cid, mode, colors, sval, direction):
    """PR #882's `_write_colors`, for per-LED modes only.

    The installed 1.16.0 packs every LED into one 0x10 report and then sends an
    empty 0x11. A 64-byte HID frame fits a 4-byte header plus 20 RGB triplets,
    so that addresses LEDs 0-19 and leaves 20-39 dark. Splitting the payload
    across both sub-commands is the whole fix.

    Anything that is not a per-LED mode falls through to the stock
    implementation untouched.
    """
    if mode not in ("super-fixed", "super-breathing"):
        return KrakenZ3._write_colors(self, cid, mode, colors, sval, direction)

    mval, _size_variant, speed_scale, _mincolors, maxcolors = _COLOR_MODES[mode]
    leds = list(itertools.chain(*colors)) + [0x00, 0x00, 0x00] * (maxcolors - len(colors))
    speed_value = _SPEED_VALUE[speed_scale][sval]
    self._write([0x22, 0x10, cid, 0x00] + leds[0:60])
    self._write([0x22, 0x11, cid, 0x00] + leds[60:])
    self._write(
        [0x22, 0xA0, cid, 0x00, mval]
        + speed_value
        + [0x08, 0x00, 0x00, 0x80, 0x00, 0x32, 0x00, 0x00, 0x01]
    )


def find_cooler():
    """Return a connected, lighting-capable driver instance, or None.

    `initialize()` is deliberately never called. It is unnecessary for colour
    writes, it costs about 0.85s of blanked LCD, and on this device its
    LED-info parser asserts against a channel count that only matches once the
    channels below are installed -- an assertion the service's own
    `initialize all` sidesteps today only because liquidctl believes this
    cooler has no LEDs at all.
    """
    for candidate in KrakenZ3.find_supported_devices():
        info = candidate.device
        if (info.vendor_id, info.product_id) != (VENDOR_ID, PRODUCT_ID):
            continue
        # PR #882 applied to this instance only; the package on disk is never
        # modified.
        candidate._color_channels = dict(COLOR_CHANNELS)
        candidate._write_colors = _write_colors_hue2.__get__(candidate, type(candidate))
        candidate.connect()
        return candidate
    return None


def set_colour(device, channel: str, colour: str, brightness: int = 100) -> None:
    """Write one colour to every LED slot on a channel.

    Uses `super-fixed` rather than `fixed` because this generation's firmware
    blinks a firmware-driven fixed colour off periodically.

    This is a single write, and a single write does not last: the firmware
    starts corrupting stored per-LED state after 20-30 seconds. Callers that
    want a colour to stay put must keep calling this -- see `stream()`.
    """
    rgb = list(to_rgb(scale(colour, brightness / 100.0)))
    device.set_color(channel, "super-fixed", [rgb] * RING_LED_SLOTS)


def set_off(device, channel: str) -> None:
    set_colour(device, channel, "#000000", 100)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return config
    if not isinstance(data, dict):
        return config
    if data.get("channel") in COLOR_CHANNELS:
        config["channel"] = data["channel"]
    if data.get("mode") in MODES:
        config["mode"] = data["mode"]
    with contextlib.suppress(ValueError, TypeError, KeyError):
        config["colour"] = normalize_hex(data["colour"])
    with contextlib.suppress(ValueError, TypeError, KeyError):
        config["brightness"] = max(0, min(100, int(data["brightness"])))
    with contextlib.suppress(ValueError, TypeError, KeyError):
        config["fps"] = max(MIN_FPS, min(MAX_FPS, float(data["fps"])))
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    os.replace(tmp, CONFIG_PATH)  # atomic: a reader never sees a half file


# --------------------------------------------------------------------------
# effects
# --------------------------------------------------------------------------


def read_coolant() -> float | None:
    try:
        data = json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        return None
    value = data.get("coolant")
    return float(value) if isinstance(value, (int, float)) else None


def reactive_colour(coolant: float) -> str:
    cool, hot = palette_colours()
    span = REACTIVE_MAX_C - REACTIVE_MIN_C
    return mix(cool, hot, (coolant - REACTIVE_MIN_C) / span if span else 0.0)


def frame_colour(mode: str, base: str, elapsed: float) -> str | None:
    """The colour a streamed effect should show now, or None to hold.

    Returning None matters for `reactive`: with no fresh telemetry there is
    nothing to react to, and holding the last colour beats inventing one.
    """
    if mode == "breathing":
        # Eased so it spends longer near full brightness than near dark; a raw
        # sine spends too much of its cycle looking switched off.
        phase = (1.0 - math.cos(2.0 * math.pi * elapsed / 4.0)) / 2.0
        return scale(base, 0.15 + 0.85 * (phase**0.6))
    if mode == "pulse":
        phase = (elapsed / 2.0) % 1.0
        return scale(base, (1.0 - phase) ** 2)
    if mode == "spectrum":
        _hue, lightness, saturation = colorsys.rgb_to_hls(*(c / 255.0 for c in to_rgb(base)))
        rgb = colorsys.hls_to_rgb((elapsed / 12.0) % 1.0, lightness, saturation)
        return to_hex(c * 255.0 for c in rgb)
    if mode == "reactive":
        coolant = read_coolant()
        return reactive_colour(coolant) if coolant is not None else None
    return base


def config_mtime() -> float:
    try:
        return CONFIG_PATH.stat().st_mtime
    except OSError:
        return 0.0


def stream(device, config: dict) -> int:
    """Hold the configured mode on the LEDs until interrupted.

    This generation's firmware neither animates on its own nor retains per-LED
    state, so every frame is a full per-LED write sharing the cooler with the
    HUD's LCD pushes. Two things follow: animated modes are rate-capped, and
    even an unchanged colour is rewritten every REFRESH_SECONDS, because
    letting it sit turns one LED per channel green within half a minute.

    The config file is re-read whenever its mtime changes, the same way the HUD
    watches face.json. Without that, this process would hold whatever it read
    at startup and silently ignore every later change -- which is exactly what
    happened when only a restart could update it: the holder sat rewriting its
    original colour every 8 seconds and overwrote anything else applied.
    """
    channel = config["channel"]
    seen_mtime = config_mtime()

    def pacing() -> tuple[float, str]:
        if config["mode"] in ANIMATED_MODES:
            fps = max(MIN_FPS, min(MAX_FPS, config["fps"]))
            return 1.0 / fps, f"{fps:.1f} fps"
        return SLOW_POLL_SECONDS, f"refreshed every {REFRESH_SECONDS:.0f}s"

    interval, rate = pacing()
    print(f"{config['mode']} on {CHANNEL_LABELS[channel]}, {rate}. Ctrl-C to stop.", flush=True)

    started = time.monotonic()
    last: str | None = None
    last_write = 0.0
    try:
        while True:
            mtime = config_mtime()
            if mtime != seen_mtime:
                seen_mtime = mtime
                fresh = load_config()
                if fresh["channel"] != channel:
                    # The channel we are leaving would otherwise keep its last
                    # colour and decay to green with nothing refreshing it.
                    with device_lock():
                        set_off(device, channel)
                config = fresh
                channel = config["channel"]
                interval, rate = pacing()
                # Restart the animation phase and force a write, so a change
                # takes effect now rather than at the next refresh.
                started, last, last_write = time.monotonic(), None, 0.0
                print(f"{config['mode']} on {CHANNEL_LABELS[channel]}, {rate}", flush=True)

            now = time.monotonic()
            if config["mode"] == "off":
                if last != "off":
                    with device_lock():
                        set_off(device, channel)
                    last = "off"
                time.sleep(SLOW_POLL_SECONDS)
                continue

            colour = frame_colour(config["mode"], config["colour"], now - started)
            if colour is not None:
                brightness = config["brightness"]
                shown = scale(colour, brightness / 100.0)
                # Skip identical frames to leave the device time for the HUD,
                # but never for longer than the decay window.
                if shown != last or (now - last_write) >= REFRESH_SECONDS:
                    with device_lock():
                        set_colour(device, channel, colour, brightness)
                    last, last_write = shown, now
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped (LEDs will drift within ~30s without a holder)")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def show(config: dict) -> int:
    device = find_cooler()
    print(f"config      {CONFIG_PATH}")
    print(f"cooler      {'found' if device else 'NOT FOUND'}")
    if device:
        device.disconnect()
    print(f"channel     {config['channel']}  ({CHANNEL_LABELS[config['channel']]})")
    print(f"mode        {config['mode']}")
    print(f"colour      {config['colour']}")
    print(f"brightness  {config['brightness']}%")
    print(f"fps         {config['fps']} (streamed modes only)")
    cool, hot = palette_colours()
    print(f"reactive    {cool} at {REACTIVE_MIN_C:.0f}C -> {hot} at {REACTIVE_MAX_C:.0f}C")
    return 0


def apply(config: dict, once: bool = False) -> int:
    device = find_cooler()
    if device is None:
        print("Kraken not found (expected USB 1e71:3012).", file=sys.stderr)
        return 1
    try:
        channel, mode = config["channel"], config["mode"]
        if once:
            # A single write. For a colour the firmware starts corrupting it
            # within 20-30 seconds, so this is for testing the wire protocol
            # and for the service's ExecStopPost -- not how lighting is driven.
            with device_lock():
                if mode == "off":
                    set_off(device, channel)
                else:
                    set_colour(device, channel, config["colour"], config["brightness"])
            detail = "off" if mode == "off" else f"{config['colour']} (single write, will decay)"
            print(f"{CHANNEL_LABELS[channel]}: {detail}")
            return 0
        # "off" is held rather than written once so that the holder stays alive
        # to notice the config being changed back.
        return stream(device, config)
    finally:
        device.disconnect()


def probe() -> int:
    """Report what the cooler exposes, without lighting anything."""
    device = find_cooler()
    if device is None:
        print("Kraken 1e71:3012 not found.")
        return 1
    try:
        print(f"device      {device.description}")
        print(f"address     {device.address}")
        print(f"channels    {', '.join(f'{k}=0b{v:03b}' for k, v in COLOR_CHANNELS.items())}")
        print(f"led slots   {RING_LED_SLOTS}")
        print("per-LED writes split across sub-commands 0x10 and 0x11 (PR #882)")
    finally:
        device.disconnect()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Light the Kraken's pump ring and RGB fan chain.",
        epilog="liquidctl 1.16.0 cannot do this; see the module docstring for why.",
    )
    parser.add_argument("--channel", choices=sorted(COLOR_CHANNELS), help="what to light")
    parser.add_argument("--mode", choices=sorted(MODES), help="lighting mode")
    parser.add_argument("--colour", "--color", dest="colour", help="hex colour")
    parser.add_argument("--brightness", type=int, metavar="0-100", help="host-side brightness")
    parser.add_argument("--fps", type=float, help=f"streamed effect rate ({MIN_FPS}-{MAX_FPS})")
    parser.add_argument("--apply", action="store_true", help="apply the saved settings now")
    parser.add_argument(
        "--once",
        action="store_true",
        help="write the colour a single time and exit; it decays within ~30s",
    )
    parser.add_argument("--off", action="store_true", help="turn the selected channel off")
    parser.add_argument(
        "--save-only",
        action="store_true",
        help="write the settings to the config file without touching the LEDs",
    )
    parser.add_argument("--show", action="store_true", help="print current settings")
    parser.add_argument("--probe", action="store_true", help="report what the cooler exposes")
    args = parser.parse_args(argv)

    if args.probe:
        return probe()

    config = load_config()
    dirty = False
    for key in ("channel", "mode", "brightness", "fps"):
        value = getattr(args, key)
        if value is not None:
            config[key] = value
            dirty = True
    if args.colour:
        try:
            config["colour"] = normalize_hex(args.colour)
        except ValueError as exc:
            parser.error(str(exc))
        dirty = True
    if dirty:
        config["brightness"] = max(0, min(100, int(config["brightness"])))
        config["fps"] = max(MIN_FPS, min(MAX_FPS, float(config["fps"])))
        save_config(config)

    if args.save_only:
        # Lets a caller that does not own the LEDs -- the GUI, when the
        # lighting service is the holder -- record a choice and then hand the
        # actual writing to whoever does.
        if not dirty:
            save_config(config)
        print(f"saved to {CONFIG_PATH}")
        return 0

    if args.off:
        # A one-shot darkening, deliberately not saved: persisting it would
        # mean the service's own ExecStopPost rewrote the saved mode to "off"
        # and every later start came up dark. To turn the lights off *durably*
        # save mode="off" instead, which a running holder picks up live.
        return apply({**config, "mode": "off"}, once=True)

    if args.show:
        return show(config)
    if args.apply or args.once or dirty:
        return apply(config, once=args.once)
    return show(config)


if __name__ == "__main__":
    sys.exit(main())
