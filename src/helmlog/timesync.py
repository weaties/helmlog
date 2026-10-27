"""GPS-disciplined system clock via chrony SHM refclock.

Writes GPS UTC timestamps received from Signal K (navigation.datetime)
into the chrony shared-memory refclock segment so chrony can discipline
the system clock from the boat's GPS instead of internet NTP alone.

Requires chrony configured with:
    refclock SHM 2 refid GPS poll 3 precision 1e-1

Uses SHM unit 2 (key 0x4E545032).  chrony creates units 0 and 1 with
mode 0600 (root-only); units 2+ are created with mode 0666 so any user
can attach without needing to be in the chrony group.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import time
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from datetime import datetime

# ---------------------------------------------------------------------------
# SysV SHM helpers
# ---------------------------------------------------------------------------

_libc: ctypes.CDLL = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_libc.shmget.restype = ctypes.c_int
_libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
_libc.shmat.restype = ctypes.c_void_p
_libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]

_IPC_CREAT = 0o001000
_SHM_KEY_BASE = 0x4E545030  # "NTP0" — chrony SHM unit 0


# ---------------------------------------------------------------------------
# chrony SHM struct  (matches refclock_shm.c shmTime)
# ---------------------------------------------------------------------------


class _ShmTime(ctypes.Structure):
    """Mirror of the C shmTime struct used by chrony's SHM refclock."""

    _fields_ = [
        ("mode", ctypes.c_int),
        ("count", ctypes.c_int),
        ("clock_sec", ctypes.c_long),  # time_t — 8 bytes on 64-bit Linux
        ("clock_usec", ctypes.c_int),
        ("recv_sec", ctypes.c_long),  # time_t
        ("recv_usec", ctypes.c_int),
        ("leap", ctypes.c_int),
        ("precision", ctypes.c_int),
        ("nsamples", ctypes.c_int),
        ("valid", ctypes.c_int),
        ("clock_nsec", ctypes.c_uint),
        ("recv_nsec", ctypes.c_uint),
        ("dummy", ctypes.c_int * 8),
    ]


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


class GpsTimeSyncer:
    """Attach to chrony's SHM segment and feed it GPS UTC timestamps.

    One instance should be created at startup and reused for every GPS
    fix received.  Thread-safe: the SHM write protocol uses a counter
    pair so chrony detects torn writes.
    """

    def __init__(self, unit: int = 2) -> None:
        key = _SHM_KEY_BASE + unit
        size = ctypes.sizeof(_ShmTime)
        shm_id = _libc.shmget(key, size, 0o666 | _IPC_CREAT)
        if shm_id == -1:
            errno = ctypes.get_errno()
            raise OSError(errno, f"shmget(key=0x{key:08x}): {os.strerror(errno)}")
        addr = _libc.shmat(shm_id, None, 0)
        # shmat returns (void*)-1 on failure; c_void_p gives a Python int
        if addr is None or addr == 2**64 - 1:
            errno = ctypes.get_errno()
            raise OSError(errno, f"shmat: {os.strerror(errno)}")
        self._shm = _ShmTime.from_address(addr)
        self._updates = 0
        logger.info("GpsTimeSyncer: attached to chrony SHM unit {} (key=0x{:08x})", unit, key)

    def update(self, gps_utc: datetime) -> None:
        """Write one GPS fix into the SHM segment.

        Uses the mode-1 protocol: chrony checks that ``count`` is equal
        before and after reading, so a partial write is safely detected.
        """
        recv_mono = time.time()
        gps_ts = gps_utc.timestamp()

        gps_s = int(gps_ts)
        gps_ns = round((gps_ts - gps_s) * 1_000_000_000)
        recv_s = int(recv_mono)
        recv_ns = round((recv_mono - recv_s) * 1_000_000_000)

        shm = self._shm
        shm.valid = 0  # invalidate while updating
        shm.count += 1  # odd count = write in progress

        shm.mode = 1
        shm.clock_sec = gps_s
        shm.clock_usec = gps_ns // 1000
        shm.clock_nsec = gps_ns
        shm.recv_sec = recv_s
        shm.recv_usec = recv_ns // 1000
        shm.recv_nsec = recv_ns
        shm.leap = 0
        shm.precision = -20  # ~1 µs

        shm.count += 1  # even count = write complete
        shm.valid = 1

        self._updates += 1
        if self._updates == 1:
            logger.info("GpsTimeSyncer: first GPS fix — {}", gps_utc.isoformat())
        elif self._updates % 3600 == 0:
            logger.debug("GpsTimeSyncer: {} fixes written", self._updates)
