"""Background task: collect system health metrics and write to InfluxDB."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from loguru import logger

_DEFAULT_INTERVAL_S = 2

# Previous network counters for rate calculation (updated each collection cycle)
_prev_net: Any = None
_prev_net_time: float | None = None


def _get_interval() -> int:
    """Read monitor interval from env, clamp to 1–300, default to _DEFAULT_INTERVAL_S."""
    raw = os.environ.get("MONITOR_INTERVAL_S", "")
    if not raw:
        return _DEFAULT_INTERVAL_S
    try:
        return max(1, min(300, int(raw)))
    except ValueError:
        return _DEFAULT_INTERVAL_S


async def monitor_loop() -> None:
    """Collect CPU/mem/disk/temp/network metrics and write to InfluxDB periodically."""
    while True:
        try:
            await asyncio.to_thread(_collect_and_write)
        except Exception as exc:  # noqa: BLE001
            logger.warning("monitor_loop error (non-fatal): {}", exc)
        await asyncio.sleep(_get_interval())


def _collect_and_write() -> None:
    global _prev_net, _prev_net_time

    import psutil  # type: ignore[import-untyped]

    from helmlog.influx import _client

    cpu_pct: float = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    db_path = os.environ.get("DB_PATH", "data/logger.db")
    disk_root = db_path.split("/")[0] or "/"
    disk = psutil.disk_usage(disk_root)
    temp_c: float | None = None
    get_temps = getattr(psutil, "sensors_temperatures", None)
    if get_temps is not None:
        temps: dict[str, list[object]] = get_temps()
        for entries in temps.values():
            if entries:
                first = entries[0]
                current = getattr(first, "current", None)
                if current is not None:
                    temp_c = float(current)
                break

    # Fan speed (RPM) — available on Raspberry Pi 5 with active cooler
    fan_rpm: float | None = None
    get_fans = getattr(psutil, "sensors_fans", None)
    if get_fans is not None:
        fans: dict[str, list[object]] = get_fans()
        for fan_entries in fans.values():
            if fan_entries:
                fan_first = fan_entries[0]
                fan_current = getattr(fan_first, "current", None)
                if fan_current is not None:
                    fan_rpm = float(fan_current)
                break

    # Network throughput: compute bytes/sec since the previous sample
    net_now: Any = psutil.net_io_counters()
    net_time_now: float = time.monotonic()
    net_bytes_sent_per_s: float | None = None
    net_bytes_recv_per_s: float | None = None
    if _prev_net is not None and _prev_net_time is not None:
        dt = net_time_now - _prev_net_time
        if dt > 0:
            net_bytes_sent_per_s = (net_now.bytes_sent - _prev_net.bytes_sent) / dt
            net_bytes_recv_per_s = (net_now.bytes_recv - _prev_net.bytes_recv) / dt
    _prev_net = net_now
    _prev_net_time = net_time_now

    client, write_api = _client()
    if write_api is None:
        return

    from influxdb_client import Point  # type: ignore[attr-defined]

    bucket = os.environ.get("INFLUX_BUCKET", "signalk")
    org = os.environ.get("INFLUX_ORG", "helmlog")
    try:
        p: Any = (
            Point("system_health")  # type: ignore[no-untyped-call]
            .field("cpu_pct", cpu_pct)
            .field("mem_pct", mem.percent)
            .field("disk_pct", disk.percent)
        )
        if temp_c is not None:
            p = p.field("cpu_temp_c", temp_c)
        if fan_rpm is not None:
            p = p.field("fan_rpm", fan_rpm)
        if net_bytes_sent_per_s is not None:
            p = p.field("net_bytes_sent_per_s", net_bytes_sent_per_s)
        if net_bytes_recv_per_s is not None:
            p = p.field("net_bytes_recv_per_s", net_bytes_recv_per_s)
        write_api.write(bucket=bucket, org=org, record=p)
        logger.debug(
            "system_health written: cpu={:.1f}% mem={:.1f}% disk={:.1f}%",
            cpu_pct,
            mem.percent,
            disk.percent,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("InfluxDB system_health write failed: {}", exc)
    finally:
        if client:
            client.close()
