#!/usr/bin/env python3
"""Read-only validator: does this boat's B&G display emit the race-timer PGNs?

Before we port the MarkPre3802 B&G start-timer feature into HelmLog, we need
to know whether *our* instruments (Corvo / B&G Triton2) actually transmit the
two proprietary NMEA 2000 PGNs the feature depends on:

    130845  (0x1FF1D)  Set Timer    — SET countdown duration (minutes)
    130850  (0x1FF22)  Start/Stop   — START / STOP / RESET / NEAREST-MINUTE

This script ONLY listens. It never transmits a frame, so it is safe to run on
the live boat network alongside Signal K. Operate the start timer on the
Triton2 display (set a duration, press start, press stop) and watch the output.

The decode layout below was reverse-engineered on a *different* boat (a Simrad
system on "CreativePi"). Triton2 is the same vendor (Navico, mfr code 0x9F41)
so the framing is likely identical — but it may not be. That is exactly what
this run confirms. Every frame on the two target PGNs is logged with its RAW
reassembled bytes even when it does NOT decode, so a differing Triton2 layout
can be adapted from the capture without another trip to the water.

Usage (on corvopi-live):
    uv run python scripts/verify_timer_pgns.py                 # listen until Ctrl-C
    uv run python scripts/verify_timer_pgns.py --duration 300  # listen 5 min then report
    uv run python scripts/verify_timer_pgns.py --json-out /tmp/timer_pgns.json
    uv run python scripts/verify_timer_pgns.py --self-test     # no bus; prove the decoder

Send the verdict block (and the JSON file, if used) back for the port decision.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from dataclasses import dataclass, field

import can

# Reuse the shipped decoder (ported to helmlog.nmea2000, #789) so the CLI and
# the web audit share one source of truth — no drift between them.
from helmlog.can_reader import extract_pgn
from helmlog.nmea2000 import (
    PGN_SIMRAD_SET_TIMER,
    PGN_SIMRAD_START_STOP,
    FastPacketBuffer,
    SimradTimerRecord,
    decode,
)

# Target PGNs (proprietary, not covered by canboat).
PGN_SET_TIMER = PGN_SIMRAD_SET_TIMER  # 130845 / 0x1FF1D
PGN_START_STOP = PGN_SIMRAD_START_STOP  # 130850 / 0x1FF22
_TARGET_PGNS = (PGN_SET_TIMER, PGN_START_STOP)


def decode_simrad(pgn: int, payload: bytes) -> tuple[str, int | None] | None:
    """Return (action, minutes) for a Simrad timer payload, or None.

    Thin wrapper over helmlog.nmea2000.decode(). None means "this PGN appeared
    but the payload is not a recognised timer command" — e.g. a running-state
    broadcast, or a layout we have not seen.
    """
    record = decode(pgn, payload, 0, 0.0)
    if isinstance(record, SimradTimerRecord):
        return (record.action, record.minutes)
    return None


# ---------------------------------------------------------------------------
# Capture state + reporting
# ---------------------------------------------------------------------------


@dataclass
class PgnStat:
    pgn: int
    frames: int = 0
    reassembled: int = 0
    decoded: int = 0
    source_addrs: set[int] = field(default_factory=set)
    decoded_samples: list[str] = field(default_factory=list)
    raw_samples: list[str] = field(default_factory=list)  # undecoded reassembled payloads

    def summary(self) -> dict[str, object]:
        return {
            "pgn": self.pgn,
            "frames": self.frames,
            "reassembled": self.reassembled,
            "decoded": self.decoded,
            "source_addrs": sorted(f"0x{a:02X}" for a in self.source_addrs),
            "decoded_samples": self.decoded_samples[:8],
            "undecoded_raw_samples": self.raw_samples[:8],
        }


def _hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def _verdict(stats: dict[int, PgnStat]) -> tuple[str, str]:
    """Return (level, human message). level in PASS / PARTIAL / FAIL."""
    set_s, ss_s = stats[PGN_SET_TIMER], stats[PGN_START_STOP]
    any_decoded = set_s.decoded or ss_s.decoded
    any_frames = set_s.frames or ss_s.frames
    if set_s.decoded and ss_s.decoded:
        return "PASS", "Both timer PGNs seen AND decoded. The feature is viable for this boat."
    if any_decoded:
        missing = [
            name
            for name, st in (("130845 Set", set_s), ("130850 Start/Stop", ss_s))
            if not st.decoded
        ]
        return "PARTIAL", f"Decoded some timer traffic but never saw: {', '.join(missing)}."
    if any_frames:
        return (
            "PARTIAL",
            "Target PGNs appeared but NONE decoded — Triton2 likely uses a different "
            "byte layout. The raw samples below have the bytes needed to adapt the decoder.",
        )
    return (
        "FAIL",
        "No 130845/130850 frames at all. Either the display does not emit a controllable "
        "timer over N2K, or can0 is not the right interface (see notes).",
    )


def _print_report(stats: dict[int, PgnStat], elapsed: float, channel: str) -> dict[str, object]:
    level, message = _verdict(stats)
    print("\n" + "=" * 72)
    print(f"  B&G TIMER PGN VALIDATION  —  verdict: {level}")
    print("=" * 72)
    print(f"  interface : {channel}")
    print(f"  listened  : {elapsed:.0f}s")
    for pgn, label in ((PGN_SET_TIMER, "130845 Set Timer"), (PGN_START_STOP, "130850 Start/Stop")):
        st = stats[pgn]
        srcs = ", ".join(sorted(f"0x{a:02X}" for a in st.source_addrs)) or "—"
        print(
            f"  {label:<22} frames={st.frames:<4} reassembled={st.reassembled:<4} "
            f"decoded={st.decoded:<4} src={srcs}"
        )
        for sample in st.decoded_samples[:8]:
            print(f"        decoded : {sample}")
        for sample in st.raw_samples[:8]:
            print(f"        RAW(undecoded) : {sample}")
    print("-" * 72)
    print(f"  {message}")
    print("=" * 72 + "\n")
    return {
        "verdict": level,
        "message": message,
        "channel": channel,
        "listened_s": round(elapsed, 1),
        "pgns": {str(p): stats[p].summary() for p in _TARGET_PGNS},
    }


# ---------------------------------------------------------------------------
# Live listen
# ---------------------------------------------------------------------------


def listen(channel: str, duration: float, show_all: bool) -> dict[int, PgnStat]:
    stats = {p: PgnStat(p) for p in _TARGET_PGNS}
    buf = FastPacketBuffer()
    try:
        bus = can.Bus(channel=channel, interface="socketcan")
    except Exception as exc:  # noqa: BLE001 — operator-facing diagnostic
        print(f"\nERROR: could not open '{channel}' as a socketcan interface: {exc}")
        print("Checks:")
        print(f"  • ip link show {channel}        (does the interface exist and is it UP?)")
        print("  • Is Signal K reading CAN via socketcan, or via a serial N2K gateway?")
        print("    If it uses a serial gateway (Actisense/YDEN), there is no can0 to sniff —")
        print("    capture at the gateway instead and send the dump back.")
        sys.exit(2)

    deadline = time.monotonic() + duration if duration > 0 else None
    print(f"Listening read-only on {channel} for PGNs 130845 / 130850.")
    print("Operate the Triton2 start timer now (set duration, start, stop). Ctrl-C to finish.\n")
    try:
        with bus:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                msg = bus.recv(timeout=1.0)
                if msg is None or not msg.is_extended_id:
                    continue
                pgn = extract_pgn(msg.arbitration_id)
                if show_all and pgn not in _TARGET_PGNS:
                    print(f"  · PGN {pgn}")
                if pgn not in _TARGET_PGNS:
                    continue
                sa = msg.arbitration_id & 0xFF
                st = stats[pgn]
                st.frames += 1
                st.source_addrs.add(sa)
                payload = buf.feed(pgn, sa, bytes(msg.data))
                if payload is None:
                    continue
                st.reassembled += 1
                decoded = decode_simrad(pgn, payload)
                ts = time.strftime("%H:%M:%S")
                if decoded is not None:
                    st.decoded += 1
                    action, minutes = decoded
                    detail = f"{action}" + (f" {minutes}min" if minutes is not None else "")
                    line = f"PGN {pgn} SA=0x{sa:02X} -> {detail}  [{_hex(payload)}]"
                    st.decoded_samples.append(line)
                    print(f"{ts}  DECODED  {line}")
                else:
                    line = f"PGN {pgn} SA=0x{sa:02X} undecoded [{_hex(payload)}]"
                    if line not in st.raw_samples:
                        st.raw_samples.append(line)
                    print(f"{ts}  RAW      {line}")
    except KeyboardInterrupt:
        print("\n(stopped)")
    return stats


# ---------------------------------------------------------------------------
# Self-test — synthesise frames, prove the decoder, no bus required
# ---------------------------------------------------------------------------


def _fast_packet_frames(payload: bytes) -> list[bytes]:
    frames = [bytes([0x00, len(payload)]) + payload[:6]]
    offset, num = 6, 1
    while offset < len(payload):
        frames.append(bytes([num]) + payload[offset : offset + 7])
        offset += 7
        num += 1
    return [f.ljust(8, b"\xff") for f in frames]


def self_test() -> int:
    # Synthetic payloads matching the shipped decoder's expected layout.
    mfr = bytes([0x41, 0x9F])
    set_disc = bytes([0x07, 0x42, 0x00, 0x01])
    cases: list[tuple[int, bytes, tuple[str, int | None]]] = [
        (
            PGN_SET_TIMER,
            mfr + bytes([0xFF, 0xFF, 0xFF, 0xFF]) + set_disc + bytes([5, 0xFF, 0xFF, 0xFF]),
            ("set", 5),
        ),
        (PGN_START_STOP, mfr + bytes([0xFF, 0xFF, 0x01, 0x17, 0x3D, 0x00]), ("start", None)),
        (PGN_START_STOP, mfr + bytes([0xFF, 0xFF, 0x01, 0x17, 0x3E, 0x00]), ("stop", None)),
    ]
    ok = True
    for pgn, payload, expected in cases:
        buf = FastPacketBuffer()
        out: bytes | None = None
        for frame in _fast_packet_frames(payload):
            out = buf.feed(pgn, 0x09, frame)
        got = decode_simrad(pgn, out) if out is not None else None
        status = "ok" if got == expected else "FAIL"
        ok = ok and got == expected
        print(f"  [{status}] PGN {pgn}: reassemble+decode -> {got} (expected {expected})")
    print("\nself-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--channel", default="can0", help="socketcan interface (default: can0)")
    ap.add_argument(
        "--duration", type=float, default=0.0, help="seconds to listen (0 = until Ctrl-C)"
    )
    ap.add_argument("--json-out", default="", help="write the summary to this JSON path")
    ap.add_argument("--all", action="store_true", help="also print every other PGN seen (noisy)")
    ap.add_argument(
        "--self-test", action="store_true", help="prove the decoder without a bus, then exit"
    )
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    # Make SIGTERM behave like Ctrl-C so an unattended `timeout 300 …` run still
    # prints the verdict instead of dying silently.
    def _on_term(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_term)

    start = time.monotonic()
    stats = listen(args.channel, args.duration, args.all)
    report = _print_report(stats, time.monotonic() - start, args.channel)

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"summary written to {args.json_out}")


if __name__ == "__main__":
    main()
