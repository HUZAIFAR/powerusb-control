"""Control library for the PowerUSB USB-controlled power strip."""

from .device import (
    PowerUSB,
    PowerUSBError,
    SOCKET_COUNT,
    VENDOR_ID,
    PRODUCT_ID,
    find_strips,
)

__all__ = [
    "PowerUSB",
    "PowerUSBError",
    "SOCKET_COUNT",
    "VENDOR_ID",
    "PRODUCT_ID",
    "find_strips",
    "load_config",
]

__version__ = "1.0.0"

from .config import load_config  # noqa: E402  (re-export after __all__)
