# Coldloop — Kraken Elite control suite

Linux control for the NZXT Kraken 2024 Elite RGB (CAM is Windows-only), built on
[liquidctl](https://github.com/liquidctl/liquidctl).

| File | Purpose |
| --- | --- |
| `kraken_hud.py` | Renders the telemetry HUD and pushes it to the 640x640 LCD in a loop |
| `coldloop_lighting.py` | Pump-ring and fan-chain RGB (the only part that does not use the liquidctl CLI) |
| `kraken_controller.py` | **Coldloop**, the PyQt6 control panel (hardware, gallery, editor, diagnostics) |
| `liquidctl.service` | systemd `--user` unit for the HUD; installed copy lives at `~/.config/systemd/user/` |
| `coldloop-lighting.service` | systemd `--user` unit that holds the LED colour (the firmware forgets it) |
| `VERIFIED_COMMANDS.md` | Ground-truth liquidctl syntax and duty limits for this device |

The controller is in the GNOME app grid and dock as **Coldloop**
(`~/.local/share/applications/coldloop.desktop`, icon
`~/.local/share/icons/hicolor/scalable/apps/coldloop.svg`).

Both processes serialise device access through a shared `flock` on
`/dev/shm/kraken_liquidctl.lock`, and the HUD publishes its latest reading to
`/dev/shm/kraken_status.json` so the GUI can show live telemetry without
opening a second conversation with the cooler.

## Everyday use

```
systemctl --user start   liquidctl.service
systemctl --user stop    liquidctl.service     # also restores the native display
systemctl --user status  liquidctl.service
python kraken_hud.py --preview --output /tmp/x.png   # render without the device
python kraken_hud.py --check-contrast                # WCAG check on text colours
```

## If something goes wrong

The service is `Restart=on-failure` (not `always`) and rate-limited to 5 starts
per 5 minutes, so a failing HUD gives up rather than reapplying a bad state
forever. It is wanted by `graphical-session.target`, so it stops at logout.

If the device goes unreachable for an extended stretch (observed once as the
Kraken's HID endpoint dropping out with `ValueError: The device has no
langid`, a transient USB-level error distinct from the LCD's own
bucket-wrap `AssertionError`), `kraken_hud.py` no longer retries forever with
the screen stuck black: after `MAX_CONSECUTIVE_FAILURES` (12) consecutive
failed frames it exits non-zero itself, so systemd's `Restart=on-failure`
picks it back up automatically. You shouldn't need to restart the service by
hand for this anymore -- if the LCD is still black more than a couple of
minutes after going dark, check `journalctl --user -u liquidctl.service` for
whether it's still retrying or has hit the 5-per-5-minute start limit
(`systemctl --user reset-failed liquidctl.service` clears that).

Disable it without a desktop session:

```
systemctl --user disable --now liquidctl.service
systemctl --user reset-failed liquidctl.service   # after hitting the start limit
```

If the graphical session itself will not come up, Bazzite's GRUB emergency mode
(add `emergency` to the kernel command line) drops to a root shell with no root
password; from there:

```
rm /home/theo/.config/systemd/user/graphical-session.target.wants/liquidctl.service
```

## Switching faces

The service owns which face is on screen. Coldloop's "Apply to LCD" button
writes it to `~/.config/coldloop/face.json`, or from the CLI:

```
python kraken_hud.py --set-face bars     # the running HUD crossfades to it
```

The service notices that file while idle (not just at the top of its 2s
interval) and applies the new face in about 0.4s.

**Do not reintroduce a crossfade.** This panel accepts about **1.8 frames per
second** -- a `set lcd screen static` push measures a consistent 0.56s -- and
an alpha blend needs far more than that to read as one image dissolving into
another. A four-step crossfade was tried and looked like a rendering fault: a
slideshow of half-transparent double-exposures with both faces legible at
once. A blend's intermediate frames are only meaningful as part of a smooth
sequence; a *wipe's* are not, because each one is a hard-edged, intentional
looking state on its own. That is why the default transition is a wipe and not
a fade, and it is a property of the frame rate, not of the step count.

Set from the Face switching dropdown on the Gallery tab, or `--transition`:

| Mode | Cost | Behaviour |
| --- | --- | --- |
| `wipe` (default) | 3 pushes, ~1.5s | radial sweep from 12 o'clock, new face revealed behind a hard edge |
| `instant` | 1 push, ~0.56s | old face holds until the new one lands; no black frame |
| `loading` | 2 pushes, ~1.1s | shows the loading frame first, announcing the change |

```
python kraken_hud.py --set-face bars --transition loading
python kraken_hud.py --transition instant     # change the mode, keep the face
```

Colour changes never get an announcement frame: the layout is unchanged, so a
recolour reads as itself.

Applying a face used to run `kraken_hud.py --once`, which had two faults: it
called `liquidctl initialize all` first, resetting the panel for about a second
of black screen, and it wrote a single frame that the service's own next frame
overwrote within 0.8s, so the chosen face never actually stuck. Both processes
were pushing to one device. Now only the service pushes while it is running,
and `--once` is used only when the service is stopped.

On startup there is a genuine multi-second gap -- three `ExecStartPre`
liquidctl calls, then the first telemetry read -- which is now covered by a
loading frame instead of a black screen, so a restart no longer looks like a
failure.

## Building your own face

The Gallery tab's **Create a face** button opens a studio: drag arc gauges,
bar meters, live readouts and text labels onto the round display, drag them to
position, and set each one's metric, colour, size and weight individually.
Saved faces appear under "Your faces" above the built-ins, with Edit, Apply
and Delete.

A face is a JSON file in `~/.config/coldloop/faces/<name>.json` listing its
components, rendered by `render_custom()`. Select one anywhere a face is
accepted by prefixing its name:

```
python kraken_hud.py --list-faces
python kraken_hud.py --set-face 'custom:My Face'
python kraken_hud.py --preview --style 'custom:My Face' --output /tmp/x.png
```

Notes on how it fits together:

* The studio's canvas paints its own **approximation** of each component --
  gauges show a fixed part-filled arc, not live data -- because dragging has
  to repaint instantly while a real frame costs a subprocess, numpy and PIL.
  **Render preview** produces the genuine frame.
* Arc gauges are always concentric with the display, since `draw_arc()` works
  from the panel's centre. Only their radius, thickness and angles move.
* The studio builds its menus from `kraken_hud.py --dump-vocab`, so a metric
  or component added to the renderer appears in the studio without touching
  the GUI, and the two can never offer something the other cannot draw.
* Deleting a face that the HUD is currently showing is safe: it logs the
  missing face and falls back to `rings` rather than failing.
* Faces whose names begin with an underscore are hidden from the gallery;
  the studio uses one for its own scratch renders.

## Colours

Every HUD face is drawn from three user-chosen colours, set from the Colours
card at the top of Coldloop's Gallery tab or from the CLI:

```
python kraken_hud.py --primary '#c084fc' --secondary '#f472b6' --tertiary '#fdba74' --save-colours
python kraken_hud.py --show-colours     # active palette and everything derived from it
python kraken_hud.py --reset-colours    # back to the original teal
```

The palette lives at `~/.config/coldloop/palette.json`
(`KRAKEN_PALETTE_PATH` overrides it). The running HUD watches that file and
recolours within one frame, so changing colours never needs a service restart.
Deleting the file is equivalent to `--reset-colours`.

Only those three colours are user-supplied. Every *text* shade is derived from
them by shifting lightness until it meets the WCAG target it needs, rather
than being used as picked, so no choice of colour can make the readouts
unreadable — a hex that looks good as a gauge fill is usually illegible as
18px text. `--check-contrast` reports the derived shades for the active
palette, and the "night" face's colours are checked against its own dark
background rather than against white. The default teal is returned verbatim
rather than regenerated, since those values were hand-tuned.

## Lighting

Coldloop's **Lighting** tab drives the cooler's own LEDs: the pump ring around
the LCD, and the RGB header the radiator fans chain into. Pick what to light,
a mode, a colour and a brightness, then Apply. The same thing from the CLI:

```
python coldloop_lighting.py --channel ring     --mode static --colour '#22d3ee'
python coldloop_lighting.py --channel external --mode breathing
python coldloop_lighting.py --channel sync     --mode reactive   # colour tracks coolant
python coldloop_lighting.py --off
python coldloop_lighting.py --show
```

| Channel | Drives |
| --- | --- |
| `ring` | the pump ring around the display |
| `external` | the RGB fan chain |
| `sync` | both together |

Modes are `static`, `off`, `breathing`, `pulse`, `spectrum` and `reactive`.
Settings live in `~/.config/coldloop/lighting.json`.

**The cooler forgets its lighting.** Measured: a colour written once starts
corrupting after 20-30 seconds, one LED on the ring and one on the fan chain
turning green. Rewriting the same colour fixes it instantly, which is what
shows this is decay rather than an LED we never addressed. So every mode
except `off` is *held* by a running process that rewrites it every 8 seconds —
solid colours included. `--once` does a single write and says in its output
that it will decay; it exists for testing the protocol.

That means lighting lasts only as long as something is holding it, so
`coldloop-lighting.service` does the holding:

```
systemctl --user status  coldloop-lighting.service
systemctl --user stop    coldloop-lighting.service   # also turns the LEDs off
systemctl --user disable --now coldloop-lighting.service
```

It is a separate unit from `liquidctl.service` on purpose: nothing about
lighting should be able to restart the unit that owns pump and fan duties, and
this one never runs `initialize` or touches a duty.

The holder **watches `lighting.json`** and picks changes up within a couple of
seconds, exactly as the HUD watches `face.json`. That is load-bearing, not a
nicety: when the config was only read at startup, the holder sat rewriting its
original colour every 8 seconds and silently overwrote everything applied
afterwards — the lighting looked stuck on one colour no matter what you chose.

The LEDs can only have one owner, so when that service is running the Lighting
tab only writes `lighting.json` rather than lighting the ring itself — the same
arrangement faces already use. It deliberately does not restart the service,
which would blink the LEDs off and back via `ExecStopPost`. With the service
stopped, the window holds the lighting directly for as long as it is open.

Turning the lights off from the GUI saves `mode: "off"`, which the holder picks
up while staying alive, so Apply can turn them back on. The `--off` *flag* is a
one-shot darkening that is deliberately never saved — it is what `ExecStopPost`
uses, and persisting it would make every later start come up dark.

Held modes never exit on their own, so the GUI runs them as a child process
rather than through `dispatch()`, which waits for completion and would hang its
thread pool. Switching mode, turning the lighting off, or closing the window
all stop that process — otherwise it would outlive the window and hold the
cooler with no way to stop it from the UI.

This is the one part of the suite that does **not** shell out to `liquidctl`,
because the CLI refuses these commands — liquidctl 1.16.0 maps this cooler's
PID to an empty colour-channel table, so `set ring color` fails before writing
anything. The LEDs are on the pump regardless (the Kraken is the only NZXT
device on the USB bus). This module applies the still-unmerged liquidctl PR
\#882's Hue 2 logic at runtime to its own driver instance, and never modifies
the installed package — patching `venv/` would falsify the pin in
`requirements.txt` and be erased by the next `pip install -r requirements.txt`.
`VERIFIED_COMMANDS.md` has the protocol details.

Two consequences of how this generation's firmware works, neither fixable here:
animated modes are computed on the host and streamed, so **they stop when the
process stops**, and colours **reset on an AC power-cycle**. `static` and
`reactive` write only when the colour actually changes, so they are the cheap
ones; the animated modes hold the device open and are rate-capped to 5 fps so
they do not starve the HUD's LCD pushes.

## Fan safety

The fan channel's driver minimum is **0**, and `liquidctl set fan speed 0` is
accepted silently with exit status 0 while stopping the radiator fans — it looks
like success while the machine overheats. The GUI slider therefore cannot go
below 25%, `apply_fan()` clamps again independently of the widget range, and
every duty in the unit's fan curve is >= 30. Do not lower these.

## FPS

Linux has no system-wide FPS counter, so `kraken_hud.py` reads whatever a
provider last wrote and shows `--` when nothing is fresh (within 10s) rather
than a misleading `0`:

1. `/dev/shm/kraken_fps` (override with `KRAKEN_FPS_FILE`) — a plain number;
   anything may write it.
2. MangoHud CSV logs, if MangoHud is configured with an `output_folder`.

`~/.config/MangoHud/MangoHud.conf` is set up with `autostart_log` so any game
launched under MangoHud logs automatically (bounded to 6-hour sessions).
MangoHud's OpenGL hook currently crashes on this system on load
(`undefined symbol: __malloc_hook`, a glibc symbol removed in newer glibc than
what this mangohud build expects) — likely fixed by a `rpm-ostree upgrade`
picking up a newer mangohud package. The Vulkan layer (what Proton/Steam games
actually use) loads without that error.
