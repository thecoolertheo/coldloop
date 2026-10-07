#!/usr/bin/env python3
"""Fast LCD writer: ~6.6 fps instead of ~1.8.

WHY THE OLD CEILING WAS NOT THE DEVICE
--------------------------------------
Every frame used to cost a consistent 0.56s, and the whole HUD was designed
around the ~1.8 fps that implies -- including the README's standing rule
against crossfades. That number was real but it was never the panel's limit.
Measured on the real cooler, a 0.56s push breaks down as:

    bulk transfer of the frame          ~155 ms
    `_prepare_static_file`               ~52 ms   1.6M Python appends
    `_query_buckets`, 16 HID round trips  ~32 ms
    orientation/brightness read           ~19 ms
    bucket alloc/free churn               ~20 ms
    liquidctl subprocess start            ~90 ms
    PNG encode, write, re-decode          ~30 ms

Only the first line is the device. Everything else is per-frame work that never
changes between frames, so this module does it once:

* The device is held open, so no subprocess and no USB re-open per frame.
* Orientation is read once at connect, not before every push.
* Two buckets are claimed once and alternated, so there is no per-frame
  allocation, no 16-query scan, and -- importantly -- no wrap-around.
* Frames are converted with numpy (2.5 ms, byte-identical to the driver's
  loop) and sent as `bytes`, so pyusb is handed a buffer rather than a
  1.6-million-element list.

Measured result: 6.6 fps sustained, median 152 ms/frame, and flat across USB
chunk sizes from 64 KB to 2 MB -- which is how we know the remaining time is
the device accepting ~10.3 MB/s, not host overhead. A 640x640 frame is 1600 KB
at 4 bytes per pixel, so 1600/10.3 ~= 155 ms is the floor for a full-colour
frame. Sending fewer bytes is the only way past it.

A SIDE EFFECT WORTH KEEPING
---------------------------
The HUD's long-standing "randomly goes black for a second" bug came from the
driver cycling all 16 buckets and wrapping, which surfaces as
`AssertionError('reached max bucket')` and a black panel until the next push.
Alternating two fixed buckets never wraps, so that failure mode is designed
out rather than retried around.
"""

from __future__ import annotations

import contextlib
import fcntl
import math
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

for _packages in sorted((HERE / "venv" / "lib").glob("python3.*/site-packages")):
    sys.path.insert(0, str(_packages))

try:
    import numpy as np
    from PIL import Image
    from liquidctl.driver.kraken3 import KrakenZ3
except ImportError as exc:  # pragma: no cover - environment problem
    print(f"[lcd] cannot import dependencies: {exc}", file=sys.stderr)
    raise

VENDOR_ID = 0x1E71
PRODUCT_ID = 0x3012

SIZE = 640
BYTES_PER_PIXEL = 4  # the device wants RGBX; the X byte is sent as 0

# Shared with kraken_hud.py, kraken_controller.py and coldloop_lighting.py.
LOCK_PATH = Path(os.environ.get("KRAKEN_LOCK_PATH", "/dev/shm/kraken_liquidctl.lock"))

# Constant preamble of every bulk transfer, straight from the driver.
_MAGIC = [0x12, 0xFA, 0x01, 0xE8, 0xAB, 0xCD, 0xEF, 0x98, 0x76, 0x54, 0x32, 0x10]

# Two buckets, alternated. Two is the minimum that lets the device keep showing
# a complete frame while the next one is written into the other bucket.
_BUCKETS = (0, 1)

# Opcode for a static RGBX image, as used by the driver's own static path.
_STATIC = 0x02


@contextlib.contextmanager
def device_lock(timeout: float = 20.0):
    """Hold the shared cooler lock, matching the rest of the suite.

    Yields False rather than raising if the lock cannot be taken: a dropped
    frame is better than a stalled render loop.
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
                time.sleep(0.01)
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def to_payload(image: Image.Image, rotation: int) -> bytes:
    """Convert a frame to the device's RGBX byte order.

    Vectorised equivalent of the driver's `_prepare_static_file`, which appends
    four values per pixel in a Python loop -- 1,638,400 appends for one frame,
    about 52 ms. This is ~2.5 ms and produces byte-identical output (verified
    against the driver's own function on the real frame).
    """
    if image.size != (SIZE, SIZE):
        image = image.resize((SIZE, SIZE))
    if rotation:
        image = image.rotate(rotation * -90)
    if image.mode != "RGB":
        image = image.convert("RGB")

    rgb = np.asarray(image, dtype=np.uint8)
    buffer = np.zeros((rgb.shape[0], rgb.shape[1], BYTES_PER_PIXEL), dtype=np.uint8)
    buffer[:, :, :3] = rgb
    return buffer.tobytes()


class FastLcd:
    """A Kraken LCD held open, with the per-frame work hoisted out."""

    def __init__(self) -> None:
        self.device = None
        self.orientation = 0
        self.brightness = 100
        self._slot = 0
        self._blocks: list[int] = []
        self._offsets: dict[int, list[int]] = {}

    # -- lifecycle -------------------------------------------------------

    def connect(self) -> bool:
        """Open the cooler and claim two buckets. False if unavailable."""
        try:
            for candidate in KrakenZ3.find_supported_devices():
                info = candidate.device
                if (info.vendor_id, info.product_id) != (VENDOR_ID, PRODUCT_ID):
                    continue
                candidate.connect()
                self.device = candidate
                break
            else:
                return False
        except Exception as exc:  # pragma: no cover - USB level failure
            print(f"[lcd] connect failed: {exc}", file=sys.stderr)
            return False

        try:
            self._read_lcd_info()
            self._claim_buckets()
        except Exception as exc:
            print(f"[lcd] setup failed: {exc}", file=sys.stderr)
            self.close()
            return False
        return True

    def close(self) -> None:
        if self.device is not None:
            with contextlib.suppress(Exception):
                self.device.disconnect()
            self.device = None

    def _read_lcd_info(self) -> None:
        """Read orientation and brightness once.

        The driver does this before every push. It is a HID round trip worth
        ~19 ms and the answer does not change while we hold the device, so the
        HUD would be paying it 30 times a minute for nothing.
        """
        def parse(msg):
            self.brightness = msg[0x18]
            self.orientation = msg[0x1A]

        self.device._write([0x30, 0x01])
        self.device._read_until({b"\x31\x01": parse})

    def _claim_buckets(self) -> None:
        """Lay out two same-sized buckets back to back, once."""
        payload = SIZE * SIZE * BYTES_PER_PIXEL
        blocks = math.ceil((len(_MAGIC) + 8 + payload) / 1024)
        self._blocks = list(blocks.to_bytes(2, "little"))

        self.device._write_then_read([0x36, 0x03])
        self.device._delete_all_buckets()
        self._offsets = {}
        for n, index in enumerate(_BUCKETS):
            offset = list((n * blocks).to_bytes(2, "little"))
            self._offsets[index] = offset
            if not self.device._setup_bucket(index, index + 1, offset, self._blocks):
                raise RuntimeError(f"could not set up bucket {index}")

    # -- pushing ---------------------------------------------------------

    def push_payload(self, payload: bytes) -> None:
        """Send one already-converted frame. Caller holds the lock."""
        device = self.device
        index = _BUCKETS[self._slot]
        self._slot ^= 1

        bulk_info = [_STATIC, 0x0, 0x0, 0x0] + list(len(payload).to_bytes(4, "little"))

        # Re-asserting the bucket each frame costs ~0.3 ms and keeps the device
        # from drifting if something else touched it between frames.
        device._setup_bucket(index, index + 1, self._offsets[index], self._blocks)
        device._write_then_read([0x36, 0x01, index])
        device._bulk_write(bytes(_MAGIC + bulk_info))
        for start in range(0, len(payload), device.bulk_buffer_size):
            device._bulk_write(payload[start : start + device.bulk_buffer_size])
        device._write([0x36, 0x02])
        device._switch_bucket(index)

    def push(self, image: Image.Image) -> bool:
        """Convert and send one frame, taking the shared lock."""
        if self.device is None and not self.connect():
            return False
        try:
            payload = to_payload(image, self.orientation)
            with device_lock():
                self.push_payload(payload)
            return True
        except Exception as exc:
            print(f"[lcd] push failed: {exc}", file=sys.stderr)
            # Drop the handle so the next call reconnects from scratch rather
            # than repeatedly failing against a device in an unknown state.
            self.close()
            return False


def _service_warning() -> None:
    import subprocess

    try:
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "liquidctl.service"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    if active == "active":
        print("note: the HUD service is running and will push its own frames over this.")
        print("      stop it first for a clean view:")
        print("        systemctl --user stop liquidctl.service\n")


def demo(seconds: float = 12.0, fps: float = 6.5) -> int:
    """Animate a sweep, to see the frame rate rather than read about it."""
    from PIL import ImageDraw

    _service_warning()
    lcd = FastLcd()
    if not lcd.connect():
        print("Kraken 1e71:3012 not found", file=sys.stderr)
        return 1

    # Pre-render a full cycle: this measures the push rate, not PIL's speed.
    count = max(8, int(round(fps * 2)))
    print(f"pre-rendering {count} frames…")
    payloads = []
    for i in range(count):
        frame = Image.new("RGB", (SIZE, SIZE), "#05231f")
        draw = ImageDraw.Draw(frame)
        angle = 360.0 * i / count
        draw.pieslice([30, 30, SIZE - 30, SIZE - 30], angle - 90, angle - 18, fill="#2dd4bf")
        draw.ellipse([150, 150, SIZE - 150, SIZE - 150], fill="#05231f")
        draw.pieslice([190, 190, SIZE - 190, SIZE - 190], -angle * 2 - 90,
                      -angle * 2 - 40, fill="#7dd3fc")
        draw.ellipse([250, 250, SIZE - 250, SIZE - 250], fill="#05231f")
        payloads.append(to_payload(frame, lcd.orientation))

    print(f"animating for {seconds:.0f}s — watch the cooler")
    started = time.monotonic()
    pushed = 0
    try:
        while time.monotonic() - started < seconds:
            with device_lock():
                lcd.push_payload(payloads[pushed % count])
            pushed += 1
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = time.monotonic() - started
        lcd.close()
    print(f"  {pushed} frames in {elapsed:.1f}s = {pushed / elapsed:.1f} fps")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Fast LCD writer for the Kraken Elite.")
    parser.add_argument("--demo", action="store_true", help="animate a sweep on the LCD")
    parser.add_argument("--seconds", type=float, default=12.0, help="demo length")
    parser.add_argument("--info", action="store_true", help="connect and report, pushing nothing")
    args = parser.parse_args(argv)

    if args.demo:
        return demo(args.seconds)

    lcd = FastLcd()
    if not lcd.connect():
        print("Kraken 1e71:3012 not found", file=sys.stderr)
        return 1
    print(f"device       {lcd.device.description}")
    print(f"orientation  {lcd.orientation * 90}°")
    print(f"brightness   {lcd.brightness}%")
    print(f"buckets      {_BUCKETS} (alternating, never wrapping)")
    print(f"payload      {SIZE * SIZE * BYTES_PER_PIXEL / 1024:.0f} KB/frame")
    lcd.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
