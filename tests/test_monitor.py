"""Tests for the system health monitor module."""

from __future__ import annotations

from collections import namedtuple
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from helmlog.monitor import _DEFAULT_INTERVAL_S, _collect_and_write, _get_interval

if TYPE_CHECKING:
    import pytest


class TestGetInterval:
    """_get_interval reads MONITOR_INTERVAL_S from env with clamping."""

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MONITOR_INTERVAL_S", raising=False)
        assert _get_interval() == _DEFAULT_INTERVAL_S

    def test_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_INTERVAL_S", "10")
        assert _get_interval() == 10

    def test_clamps_low(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_INTERVAL_S", "0")
        assert _get_interval() == 1

    def test_clamps_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_INTERVAL_S", "999")
        assert _get_interval() == 300

    def test_invalid_value_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_INTERVAL_S", "abc")
        assert _get_interval() == _DEFAULT_INTERVAL_S

    def test_empty_string_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_INTERVAL_S", "")
        assert _get_interval() == _DEFAULT_INTERVAL_S


# Fake psutil types --------------------------------------------------------

_FanReading = namedtuple("_FanReading", ["label", "current"])

_VirtualMemory = namedtuple("_VirtualMemory", ["percent"])
_DiskUsage = namedtuple("_DiskUsage", ["percent"])
_NetIO = namedtuple("_NetIO", ["bytes_sent", "bytes_recv"])


def _make_psutil_mock(
    *,
    fan_entries: dict[str, list[Any]] | None = None,
    has_sensors_fans: bool = True,
) -> MagicMock:
    """Return a mock psutil module with controllable fan data."""
    mock = MagicMock()
    mock.cpu_percent.return_value = 25.0
    mock.virtual_memory.return_value = _VirtualMemory(percent=40.0)
    mock.disk_usage.return_value = _DiskUsage(percent=55.0)
    mock.net_io_counters.return_value = _NetIO(bytes_sent=0, bytes_recv=0)

    # Temperature — always provide a value
    _TempReading = namedtuple("_TempReading", ["label", "current"])
    mock.sensors_temperatures.return_value = {"cpu_thermal": [_TempReading(label="", current=50.0)]}

    # Fan
    if has_sensors_fans:
        mock.sensors_fans.return_value = fan_entries or {}
    else:
        del mock.sensors_fans  # simulate platform without the API

    return mock


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestFanSpeedCollection:
    """Fan RPM is collected when psutil exposes it."""

    def test_fan_rpm_written_when_available(self) -> None:
        """Fan RPM should be written to InfluxDB when psutil reports fan data."""
        psutil_mock = _make_psutil_mock(
            fan_entries={"cooling_fan0": [_FanReading(label="", current=3500)]}
        )
        point_instance = MagicMock()
        point_cls = MagicMock(return_value=point_instance)
        point_instance.field.return_value = point_instance  # chaining

        write_api = MagicMock()
        client = MagicMock()

        with (
            patch.dict("sys.modules", {"psutil": psutil_mock}),
            patch("helmlog.influx._client", return_value=(client, write_api)),
            patch.dict("sys.modules", {"influxdb_client": MagicMock(Point=point_cls)}),
        ):
            # Reset network state so rate calc is skipped on first call
            import helmlog.monitor as mod

            mod._prev_net = None
            mod._prev_net_time = None
            mod._prev_pernic = None
            mod._prev_pernic_time = None
            _collect_and_write()

        # Gather all .field() calls
        field_calls = {call.args[0]: call.args[1] for call in point_instance.field.call_args_list}
        assert "fan_rpm" in field_calls
        assert field_calls["fan_rpm"] == 3500.0

    def test_fan_rpm_omitted_when_no_fans(self) -> None:
        """No fan_rpm field when psutil reports empty fan dict."""
        psutil_mock = _make_psutil_mock(fan_entries={})
        point_instance = MagicMock()
        point_cls = MagicMock(return_value=point_instance)
        point_instance.field.return_value = point_instance

        write_api = MagicMock()
        client = MagicMock()

        with (
            patch.dict("sys.modules", {"psutil": psutil_mock}),
            patch("helmlog.influx._client", return_value=(client, write_api)),
            patch.dict("sys.modules", {"influxdb_client": MagicMock(Point=point_cls)}),
        ):
            import helmlog.monitor as mod

            mod._prev_net = None
            mod._prev_net_time = None
            mod._prev_pernic = None
            mod._prev_pernic_time = None
            _collect_and_write()

        field_names = [call.args[0] for call in point_instance.field.call_args_list]
        assert "fan_rpm" not in field_names

    def test_fan_rpm_omitted_when_no_api(self) -> None:
        """No fan_rpm field when psutil lacks sensors_fans entirely (e.g. macOS)."""
        psutil_mock = _make_psutil_mock(has_sensors_fans=False)
        point_instance = MagicMock()
        point_cls = MagicMock(return_value=point_instance)
        point_instance.field.return_value = point_instance

        write_api = MagicMock()
        client = MagicMock()

        with (
            patch.dict("sys.modules", {"psutil": psutil_mock}),
            patch("helmlog.influx._client", return_value=(client, write_api)),
            patch.dict("sys.modules", {"influxdb_client": MagicMock(Point=point_cls)}),
        ):
            import helmlog.monitor as mod

            mod._prev_net = None
            mod._prev_net_time = None
            mod._prev_pernic = None
            mod._prev_pernic_time = None
            _collect_and_write()

        field_names = [call.args[0] for call in point_instance.field.call_args_list]
        assert "fan_rpm" not in field_names


class TestPerInterfaceBandwidth:
    """Per-interface bandwidth metrics are emitted to InfluxDB (#256)."""

    def test_pernic_points_written_on_second_call(self) -> None:
        """After two collections, per-interface points should be written."""
        _NicIO = namedtuple("_NicIO", ["bytes_sent", "bytes_recv"])
        psutil_mock = _make_psutil_mock()
        psutil_mock_pernic_first = {"eth0": _NicIO(100, 200), "wlan0": _NicIO(50, 75)}
        psutil_mock_pernic_second = {"eth0": _NicIO(600, 1200), "wlan0": _NicIO(150, 275)}
        pernic_calls = [psutil_mock_pernic_first, psutil_mock_pernic_second]
        pernic_idx = [0]
        agg_calls = iter([_NetIO(0, 0), _NetIO(1000, 2000)])

        def net_io_side_effect(pernic: bool = False, **kwargs: object) -> object:
            if pernic:
                result = pernic_calls[pernic_idx[0]]
                pernic_idx[0] = min(pernic_idx[0] + 1, len(pernic_calls) - 1)
                return result
            return next(agg_calls)

        psutil_mock.net_io_counters = MagicMock(side_effect=net_io_side_effect)

        point_instances: list[MagicMock] = []

        def make_point(*args: object, **kwargs: object) -> MagicMock:
            p = MagicMock()
            p.tag.return_value = p
            p.field.return_value = p
            point_instances.append(p)
            return p

        point_cls = MagicMock(side_effect=make_point)
        write_api = MagicMock()
        client = MagicMock()

        monotonic_values = iter([100.0, 100.0, 102.0, 102.0])

        with (
            patch.dict("sys.modules", {"psutil": psutil_mock}),
            patch("helmlog.influx._client", return_value=(client, write_api)),
            patch.dict("sys.modules", {"influxdb_client": MagicMock(Point=point_cls)}),
            patch("time.monotonic", side_effect=lambda: next(monotonic_values)),
        ):
            import helmlog.monitor as mod

            mod._prev_net = None
            mod._prev_net_time = None
            mod._prev_pernic = None
            mod._prev_pernic_time = None
            _collect_and_write()
            _collect_and_write()

        point_names = [call.args[0] for call in point_cls.call_args_list]
        assert "net_interface" in point_names
        assert point_names.count("net_interface") == 2  # eth0 + wlan0

    def test_pernic_no_points_on_first_call(self) -> None:
        """First collection should not emit per-interface points (no baseline)."""
        psutil_mock = _make_psutil_mock()
        _NicIO = namedtuple("_NicIO", ["bytes_sent", "bytes_recv"])

        def net_io_side_effect(pernic: bool = False, **kw: object) -> object:
            if pernic:
                return {"eth0": _NicIO(100, 200)}
            return _NetIO(bytes_sent=0, bytes_recv=0)

        psutil_mock.net_io_counters = MagicMock(side_effect=net_io_side_effect)

        point_instances: list[MagicMock] = []

        def make_point(*args: object, **kwargs: object) -> MagicMock:
            p = MagicMock()
            p.tag.return_value = p
            p.field.return_value = p
            point_instances.append(p)
            return p

        point_cls = MagicMock(side_effect=make_point)
        write_api = MagicMock()
        client = MagicMock()

        with (
            patch.dict("sys.modules", {"psutil": psutil_mock}),
            patch("helmlog.influx._client", return_value=(client, write_api)),
            patch.dict("sys.modules", {"influxdb_client": MagicMock(Point=point_cls)}),
        ):
            import helmlog.monitor as mod

            mod._prev_net = None
            mod._prev_net_time = None
            mod._prev_pernic = None
            mod._prev_pernic_time = None
            _collect_and_write()

        point_names = [call.args[0] for call in point_cls.call_args_list]
        assert "net_interface" not in point_names
