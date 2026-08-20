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

## Not applicable to this device

- `set <channel> color ...` exists but is unused by this project.
- `liquidctl --match Kraken set lcd screen gif` is rejected on 0x300E firmware
  2.x; irrelevant here (that guard only triggers for PID 0x300E).
