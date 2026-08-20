# Verified liquidctl facts for THIS machine

Verified on 2026-08-16 against liquidctl **v1.16.0** by reading the installed
driver source (`venv/lib/python3.14/site-packages/liquidctl/driver/kraken3.py`)
and running the commands on the real device. Re-verify after any liquidctl upgrade.

## Device

- **NZXT Kraken 2024 Elite RGB**, VID `0x1e71`, PID `0x3012`, driver `KrakenZ3`
  (subclass of `KrakenX3`), bus `hid`, `/dev/hidraw7`.
- LCD resolution from the driver table: **640x640**. Bulk buffer 2 MB.
- **No kernel hwmon driver is bound** (`nzxt-kraken3` not loaded, no
  `/sys/class/hwmon/*/name` match). liquidctl therefore takes the *direct HID*
  path for all speed writes.

## Speed channels (driver table `_SPEED_CHANNELS_KRAKEN2023`)

| Channel | id | dmin | dmax |
| --- | --- | --- | --- |
| `pump` | `[0x1,0x1,0x0]` | **20** | 100 |
| `fan`  | `[0x2,0x1,0x1]` | **0**  | 100 |

`_CRITICAL_TEMPERATURE = 59`. Curve points are interpolated over 20..59 °C.

### ⚠️ The fan floor is 0, and that is the real hazard

`set fan speed <percentage>` **is** supported for this device — the docopt usage
lists both `set <channel> speed <percentage>` and the curve form, and
`KrakenX3.set_fixed_speed()` implements the flat case by calling
`set_speed_profile(channel, [(0, duty), (58, duty)])`. The syntax was never the
problem.

The danger is that `duty` is clamped to the channel range, and the fan range
starts at **0**. So `set fan speed 0` is accepted without error and stops the
radiator fans outright, which reads as a successful command (exit status 0)
while the machine cooks. Combined with a `Restart=always` service that reapplies
it at every login, that is a self-reinstating thermal shutdown loop — which
matches the reported "video cuts out, then full shutdown, repeating across
reboots" incident far better than a syntax error would (a bad syntax would just
have made liquidctl exit non-zero and change nothing).

**Consequence for this project:** never expose a fan duty below a safe floor.
This suite clamps fan to **>= 25%** in both the HUD/service and the GUI, and the
GUI slider cannot physically select a lower value.

## Confirmed-working commands

```
liquidctl initialize all
liquidctl --match Kraken status
liquidctl --match Kraken set pump speed <20-100>          # clamped to 20..100
liquidctl --match Kraken set fan  speed <0-100>           # SEE WARNING ABOVE
liquidctl --match Kraken set fan  speed <t1> <d1> <t2> <d2> ...   # curve form
liquidctl --match Kraken set lcd screen orientation <0|90|180|270>
liquidctl --match Kraken set lcd screen brightness <0-100>
liquidctl --match Kraken set lcd screen static <path-to-image>
liquidctl --match Kraken set lcd screen liquid            # native display
liquidctl --match Kraken set lcd screen gif <path>        # not used here
```

`set_screen` asserts `channel == "lcd"`; orientation must be exactly 0/90/180/270
(it divides by 90 internally); brightness is asserted to 0..100.

Note that `set lcd screen static` re-reads the device's stored orientation and
brightness first (`_write([0x30,0x01])`), then resizes/rotates the image itself,
so the HUD does not need to pre-rotate its own frames.

## Lighting: the pump ring and the RGB fan chain

Verified on 2026-08-20 on the real device, by watching the LEDs.

The CLI **cannot** do this, and that is a liquidctl gap, not missing hardware:

```
liquidctl --match Kraken set ring color fixed 00ff00
  -> ERROR: operation not supported by the device      # same for logo/external/sync/fan
```

`driver/kraken3.py` maps PID 0x3012 to `_COLOR_CHANNELS_KRAKEN2023`, which is
`{}`, so `set_color()` raises `NotSupportedByDevice` before writing a byte.

The LEDs are on the pump. `lsusb` shows exactly one NZXT device on the whole
bus (`1e71:3012`), so nothing else could be driving them; the ASUS Aura
controller (`0b05:19af`) drives motherboard headers only, and is out of scope.

`coldloop_lighting.py` therefore speaks the Hue 2 direct protocol itself,
applying liquidctl PR #882's logic at runtime to a driver instance it owns.
**The installed package is never modified** -- `requirements.txt` pins
liquidctl to match this file, and an in-place patch would falsify that pin and
be erased by the next `pip install -r requirements.txt`.

| Channel | id | Drives |
| --- | --- | --- |
| `ring` | `0b001` | the 40-slot pump ring around the LCD |
| `external` | `0b010` | the RGB header the radiator fans chain into |
| `sync` | `0b111` | both at once |

Confirmed working, watched on the hardware:

```
python coldloop_lighting.py --channel ring     --mode static --colour '#ff0000'
python coldloop_lighting.py --channel external --mode static --colour '#ff00ff'
python coldloop_lighting.py --channel sync     --mode reactive
python coldloop_lighting.py --off
```

Three things that are easy to get wrong:

1. Use `super-fixed`, not `fixed`. The firmware's own animation path blinks a
   "fixed" colour off periodically on this generation; per-LED writes hold.
2. Per-LED data needs **two** reports. A 64-byte HID frame carries a 4-byte
   header plus 20 RGB triplets, so LEDs 0-19 go under sub-command `0x10` and
   LEDs 20-39 under `0x11`. Stock 1.16.0 sends everything under `0x10` and an
   empty `0x11`, lighting only half the ring.
3. Colours go on the wire as **GRB**, not RGB (`set_color()` reorders them).

Do **not** call `initialize()` from the lighting path: it costs ~0.85s of
blanked LCD, and its LED-info parser asserts on a channel count that only
matches once the channels above are installed.

### ⚠️ The firmware does not retain per-LED state

Measured on the real cooler: a solid colour written **once** begins corrupting
after **20-30 seconds**, with a single LED on the ring and one on the fan chain
reverting to green. Writing the same colour again clears it immediately, which
is what proves this is decay of stored state rather than an addressing error —
an unaddressed LED would never come good.

Consequence: **every mode except `off` has to be held by a running process**,
solid colours included. `coldloop_lighting.py` rewrites an unchanged colour
every `REFRESH_SECONDS` (8s), comfortably inside the observed window. This is
also why OpenKraken streams continuously instead of writing once.

`--once` does a genuine single write. It is for testing the wire protocol, and
its output says so; do not build on it.

Effects are computed host-side and streamed, because this firmware rejects its
own animation modes. They therefore stop when the process stops, and colours
reset on an AC power-cycle. Lighting writes take the same `flock` as every
other device access; verified that a full colour sequence runs with the HUD
service active and leaves it untouched (service `active`, no log entries,
telemetry fresh).

## Not applicable to this device

- `set <channel> color ...` is rejected by the CLI for this PID; see the
  lighting section above for what this project does instead.
- `liquidctl --match Kraken set lcd screen gif` is rejected on 0x300E firmware
  2.x; irrelevant here (that guard only triggers for PID 0x300E).
