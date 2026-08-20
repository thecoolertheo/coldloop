"""Compatibility alias so liquidctl can `from smbus import SMBus`.

install.sh copies this into the virtualenv's site-packages. It is not imported
by Coldloop itself.

liquidctl declares a hard dependency on `smbus` (Requires-Dist: smbus;
sys_platform == "linux") and `liquidctl/driver/smbus.py` imports it at module
level, which `liquidctl/driver/__init__.py` pulls in unconditionally via the
DDR4 driver. So the module must exist for `liquidctl list` to run at all, even
though Coldloop drives a USB HID cooler and never touches an I2C bus.

The distributions on PyPI that provide `smbus` are C extensions needing Python
headers and a compiler. On an immutable distribution such as Bazzite those are
awkward to install, and everywhere else it is a pointless build. smbus2 is a
pure-python drop-in replacement written for exactly this purpose, so this
re-exports it under the name liquidctl expects.

Only the names liquidctl actually uses are re-exported. In particular note that
smbus2 0.6 removed `SMBusWrapper`, so re-exporting it here would turn a missing
module into an ImportError with a more confusing message.
"""

from smbus2 import SMBus, i2c_msg  # noqa: F401
