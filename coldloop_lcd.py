#!/usr/bin/env python3
"""Fast LCD writer: ~2.7 fps instead of ~1.8.

WHAT IS AND IS NOT THE DEVICE
-----------------------------
A liquidctl `set lcd screen static` push costs a consistent 0.56s. About 200ms
of that is host-side work that is identical between frames:

    liquidctl subprocess start            ~90 ms
    `_prepare_static_file`                ~52 ms   1.6M Python appends
    `_query_buckets`, 16 HID round trips  ~32 ms
    PNG encode, write, re-decode          ~30 ms
    orientation/brightness read           ~19 ms

This module does that once instead of per frame: the device is held open,
orientation is read at connect, two buckets are claimed and alternated, and
frames are converted with numpy (2.5 ms, byte-identical to the driver's loop)
and sent as `bytes`.

What remains is the device ingesting the frame into a freshly prepared bucket:
~350 ms for 1600 KB. Measured result, visually confirmed on the panel with a
test card: 165 frames in 60s, 2.7 fps.

THE BUG THE FIRST VERSION SHIPPED WITH
--------------------------------------
The first version skipped deleting a bucket before re-using it, called setup on
the occupied bucket, and ignored the result. The device rejects setup on an
occupied bucket -- reproduced directly: remove the deletes and setup returns a
rejection on the first frame. So that version streamed every frame into a
bucket that had refused it, and the panel showed ghosted, monochrome copies of
the HUD with a black blink every few seconds. It also *measured* 6.6 fps,
because a rejected bucket ingests data far faster than a real one. Both the
corruption and the speed were artifacts of the same mistake, and it was only
caught by looking at the panel: every byte-level check had passed.

Two rules follow, and `push_payload` enforces both:

* Delete a bucket (twice, as the driver does for an occupied one) before
  setting it up, and treat a rejected setup or switch as a failure.
* Match every HID reply to its own command. The driver's `_write_then_read`
  returns whichever report arrives next, and its `_send_data` never reads the
  reply to `[0x36, 0x02]`, so the bucket switch after it reads a stale reply.
  That is why the driver logs "Failed to switch active bucket" on nearly every
  push while frames display fine -- and why checking results without matching
  replies would have produced false failures here too.

Alternating two fixed buckets should also design out the HUD's old "randomly
goes black for a second" bug, which came from the driver cycling all 16 buckets
and wrapping (`AssertionError('reached max bucket')`). That one is expected,
not yet observed over a long run.
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

# Reports to read past while waiting for a specific reply. Matches the driver's
# own _MAX_READ_ATTEMPTS; each read has its own timeout.
_MAX_REPLY_READS = 12


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

        self.device.device.clear_enqueued_reports()
        self._command([0x36, 0x03])
        self.device._delete_all_buckets()
        self._offsets = {}
        for n, index in enumerate(_BUCKETS):
            offset = list((n * blocks).to_bytes(2, "little"))
            self._offsets[index] = offset
            if not self._setup(index):
                raise RuntimeError(f"could not set up bucket {index}")

    # -- protocol --------------------------------------------------------

    def _command(self, data: list[int]) -> bytes:
        """Send a HID command and return *its* reply, not just the next one.

        The driver's `_write_then_read` returns whatever report arrives next,
        and its `_send_data` ends a transfer with a plain `_write([0x36, 0x02])`
        that never reads its reply. That reply then sits in the queue, so the
        bucket switch that follows reads it instead of its own -- which is why
        the driver logs "Failed to switch active bucket" on nearly every push
        while frames display fine. Checking those results is only meaningful
        if each reply is matched to its command.

        Replies echo the command with the first byte incremented
        (0x30 0x01 -> 0x31 0x01, 0x32 0x02 -> 0x33 0x02), so read until that
        prefix arrives and discard anything stale on the way. Reads time out,
        so a reply that never comes raises rather than hangs.
        """
        expected = bytes([data[0] + 1, data[1]])
        self.device._write(data)
        for _ in range(_MAX_REPLY_READS):
            msg = self.device._read()
            if bytes(msg[0:2]) == expected:
                return msg
        raise RuntimeError(f"no reply to command {data[0]:#04x} {data[1]:#04x}")

    def _setup(self, index: int) -> bool:
        offset = self._offsets[index]
        reply = self._command([0x32, 0x01, index, index + 1,
                               offset[0], offset[1], self._blocks[0], self._blocks[1], 0x01])
        return reply[14] == 0x01

    # -- pushing ---------------------------------------------------------

    def push_payload(self, payload: bytes) -> None:
        """Send one already-converted frame. Caller holds the lock."""
        device = self.device
        index = _BUCKETS[self._slot]
        self._slot ^= 1

        bulk_info = [_STATIC, 0x0, 0x0, 0x0] + list(len(payload).to_bytes(4, "little"))

        # Mirror the driver's per-push preamble. Its purpose is undocumented
        # ("unknown" in the driver), it costs one HID round trip, and the
        # first version of this writer skipping it is one of two differences
        # from the driver in place when frames came out corrupted.
        # Start from an empty queue so nothing stale from a previous frame, or
        # from another process's HID command, can be mistaken for a reply.
        device.device.clear_enqueued_reports()
        self._command([0x36, 0x03])

        # Never write into a bucket that still holds data. The driver always
        # deletes first, and deletes *twice* when the bucket was occupied
        # (`_prepare_bucket`). The first version of this writer re-ran setup on
        # the occupied bucket and ignored the result; on the real panel that
        # produced several ghosted, monochrome copies of the HUD and a black
        # blink every few seconds -- consistent with the device appending each
        # frame to the existing asset and playing them back as a sequence.
        for _ in range(2):
            device._delete_bucket(index)
        if not self._setup(index):
            # Raising sends push() down its reconnect path and the HUD falls
            # back to liquidctl for this frame. A rejected setup must never be
            # followed by a transfer, which is what silently corrupted frames.
            raise RuntimeError(f"bucket {index} setup rejected")

        self._command([0x36, 0x01, index])
        device._bulk_write(bytes(_MAGIC + bulk_info))
        for start in range(0, len(payload), device.bulk_buffer_size):
            device._bulk_write(payload[start : start + device.bulk_buffer_size])
        # Unlike the driver, read this reply: leaving it queued is what made
        # every switch below look like a failure.
        self._command([0x36, 0x02])
        if self._command([0x38, 0x01, 0x04, index])[14] != 0x01:
            raise RuntimeError(f"switch to bucket {index} rejected")

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
    # A test card rather than something pretty, because it has to make the
    # failure modes impossible to miss: four saturated quadrants show a colour
    # or monochrome fault at a glance, and a single hand shows ghosting as
    # multiple hands. The first version of this writer passed every byte-level
    # check and still put garbage on the panel; only looking caught it.
    import math as _math
    half = SIZE // 2
    for i in range(count):
        frame = Image.new("RGB", (SIZE, SIZE), "#000000")
        draw = ImageDraw.Draw(frame)
        draw.rectangle([0, 0, half, half], fill="#ff0000")
        draw.rectangle([half, 0, SIZE, half], fill="#00ff00")
        draw.rectangle([0, half, half, SIZE], fill="#0000ff")
        draw.rectangle([half, half, SIZE, SIZE], fill="#ffff00")
        draw.ellipse([120, 120, SIZE - 120, SIZE - 120], fill="#000000")
        angle = 2 * _math.pi * i / count - _math.pi / 2
        tip = (half + 190 * _math.cos(angle), half + 190 * _math.sin(angle))
        draw.line([(half, half), tip], fill="#ffffff", width=14)
        draw.ellipse([half - 18, half - 18, half + 18, half + 18], fill="#ffffff")
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
