"""Broadband CAN sniffer for detecting B&G start-line ping PGNs.

Listens on the NMEA 2000 (socketcan) bus and lets you interactively mark
the moment you press the boat-end ping and pin-end ping buttons on the B&G
display.  After capture it shows:

  - Inner window (±1s): every individual frame with its raw bytes and
    timestamp relative to the mark — this is where the ping frame lives.
  - Outer window (±3s): PGN summary with ALL unique reassembled payloads
    (not just the last one), so nothing is discarded.

Usage:
    uv run python scripts/ping_sniffer.py [--channel can0] [--output /tmp/sniffer_out.txt]

Workflow:
    1. Start this script.
    2. Press the BOAT END ping on your B&G display, then press 'b' here.
    3. Press the PIN END ping on your B&G display, then press 'p' here.
    4. Press q (or Ctrl-C) to stop and print the summary.
"""

from __future__ import annotations

import argparse
import collections
import sys
import threading
import time
from datetime import UTC, datetime
from typing import IO, NamedTuple

import can

sys.path.insert(0, "src")
from helmlog.can_reader import extract_pgn  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class Frame(NamedTuple):
    ts: float
    pgn: int
    sa: int
    da: int  # 0xFF = broadcast (PDU2); real DA for PDU1 unicast messages
    data: bytes  # raw 8 CAN bytes


class Mark(NamedTuple):
    ts: float
    label: str  # 'b' = boat end, 'p' = pin end


# ---------------------------------------------------------------------------
# Fast-packet reassembly — stores ALL completions with timestamps
# ---------------------------------------------------------------------------

# Single-frame proprietary PGNs (8-byte payload, no fast-packet header)
_SINGLE_FRAME_RANGE = range(65280, 65536)  # 0xFF00–0xFFFF

# These standard PGNs use fast-packet format (non-exhaustive, covers what
# we typically see on a B&G backbone)
_KNOWN_FAST_PGNS = {
    126996,
    127506,
    129029,
    129038,
    129039,
    129040,
    129284,
    129285,
    130074,
    130306,
    130310,
    130820,
    130821,
    130824,
    130850,
    130845,
}


def _is_fast_packet(pgn: int) -> bool:
    if pgn in _SINGLE_FRAME_RANGE:
        return False
    # Proprietary fast-packet range
    if 130816 <= pgn <= 131071:
        return True
    return pgn in _KNOWN_FAST_PGNS


class _FpBuf:
    """Fast-packet reassembler; accumulates ALL completed payloads with ts."""

    def __init__(self) -> None:
        self._state: dict[tuple[int, int], dict[str, object]] = {}
        # list of (ts, payload) per (pgn, sa)
        self.completed: dict[tuple[int, int], list[tuple[float, bytes]]] = collections.defaultdict(
            list
        )

    def feed(self, pgn: int, sa: int, data: bytes, ts: float) -> bytes | None:
        if not data or not _is_fast_packet(pgn):
            return None
        frame_byte = data[0] & 0x1F
        seq = (data[0] >> 5) & 0x7
        key = (pgn, sa)

        if frame_byte == 0:
            total = data[1] if len(data) > 1 else 0
            self._state[key] = {
                "seq": seq,
                "total": total,
                "buf": bytearray(data[2:8]),
                "ts": ts,
            }
            if total <= 6:
                payload = bytes(self._state[key]["buf"])[:total]
                self.completed[key].append((ts, payload))
                return payload
            return None

        state = self._state.get(key)
        if state is None or state["seq"] != seq:
            return None
        state["buf"] += data[1:8]  # type: ignore[operator]
        if len(state["buf"]) >= state["total"]:  # type: ignore[operator]
            payload = bytes(state["buf"])[: state["total"]]  # type: ignore[index]
            self.completed[key].append((state["ts"], payload))  # type: ignore[arg-type]
            return payload
        return None


# ---------------------------------------------------------------------------
# CAN reader thread
# ---------------------------------------------------------------------------


def _reader(
    channel: str,
    frames: list[Frame],
    fp: _FpBuf,
    stop: threading.Event,
) -> None:
    try:
        bus = can.Bus(channel=channel, interface="socketcan")
    except Exception as exc:
        print(f"\n[ERROR] Cannot open {channel}: {exc}", flush=True)
        return

    with bus:
        while not stop.is_set():
            msg = bus.recv(timeout=0.5)
            if msg is None or not msg.is_extended_id:
                continue
            arb = msg.arbitration_id
            pgn = extract_pgn(arb)
            sa = arb & 0xFF
            pf = (arb >> 16) & 0xFF
            # PDU1 (PF < 240): PS field is destination address; PDU2: broadcast
            da = ((arb >> 8) & 0xFF) if pf < 240 else 0xFF
            raw = bytes(msg.data)
            ts = msg.timestamp or time.time()
            frames.append(Frame(ts, pgn, sa, da, raw))
            fp.feed(pgn, sa, raw, ts)


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

INNER = 1.0  # ±s for per-frame detail
OUTER = 3.0  # ±s for PGN summary


def _hex(b: bytes) -> str:
    return " ".join(f"{x:02X}" for b in [b] for x in b)


def _rel(ts: float, mark_ts: float) -> str:
    d = ts - mark_ts
    return f"{d:+.3f}s"


def _summarise(
    frames: list[Frame], marks: list[Mark], fp: _FpBuf, out: IO[str] = sys.stdout
) -> None:
    def p(*args: object, **kwargs: object) -> None:
        print(*args, **kwargs, file=out)

    if not marks:
        p("\nNo marks recorded — nothing to summarise.")
        return

    for mark in marks:
        label_name = "BOAT END" if mark.label == "b" else "PIN END"
        ts_str = datetime.fromtimestamp(mark.ts, tz=UTC).strftime("%H:%M:%S.%f")[:-3]

        p(f"\n{'=' * 70}")
        p(f"  Mark: {label_name}  at {ts_str} UTC")
        p(f"{'=' * 70}")

        # --- Inner window: per-frame detail with raw bytes ---
        inner = [f for f in frames if abs(f.ts - mark.ts) <= INNER]
        p(f"\n  INNER ±{INNER}s ({len(inner)} frames) — raw bytes per frame:\n")

        # Highlight any unicast (PDU1) messages — these are the ones we may have missed
        unicast = [f for f in inner if f.da != 0xFF]
        if unicast:
            p("  *** UNICAST (addressed) messages — DA shown: ***")
            for f in unicast:
                p(
                    f"    {_rel(f.ts, mark.ts)}  PGN {f.pgn:6d} (0x{f.pgn:05X})  "
                    f"SA=0x{f.sa:02X}→DA=0x{f.da:02X}  {_hex(f.data)}"
                )
            p()

        if inner:
            by_pgn_inner: dict[tuple[int, int, int], list[Frame]] = collections.defaultdict(list)
            for f in inner:
                by_pgn_inner[(f.pgn, f.sa, f.da)].append(f)
            for (pgn, sa, da), flist in sorted(by_pgn_inner.items()):
                da_str = f"→DA=0x{da:02X}" if da != 0xFF else ""
                p(f"  PGN {pgn:6d} (0x{pgn:05X})  SA=0x{sa:02X}{da_str}:")
                for f in flist:
                    p(f"    {_rel(f.ts, mark.ts)}  {_hex(f.data)}")
        else:
            p("  (no frames in inner window — try pressing the key sooner)")

        # --- Outer window: PGN summary with ALL unique payloads ---
        outer = [f for f in frames if abs(f.ts - mark.ts) <= OUTER]
        by_pgn: dict[tuple[int, int, int], list[Frame]] = collections.defaultdict(list)
        for f in outer:
            by_pgn[(f.pgn, f.sa, f.da)].append(f)

        p(f"\n  OUTER ±{OUTER}s ({len(outer)} frames) — all reassembled payloads:\n")
        for (pgn, sa, da), flist in sorted(by_pgn.items()):
            key = (pgn, sa)
            da_str = f"→DA=0x{da:02X}" if da != 0xFF else ""
            completions = [
                (t, pt) for t, pt in fp.completed.get(key, []) if abs(t - mark.ts) <= OUTER
            ]

            # For single-frame PGNs, show unique raw payloads
            if pgn in _SINGLE_FRAME_RANGE:
                unique_raw: dict[bytes, float] = {}
                for f in flist:
                    d = f.data
                    if d not in unique_raw:
                        unique_raw[d] = f.ts
                p(
                    f"  PGN {pgn:6d} (0x{pgn:05X})  SA=0x{sa:02X}{da_str}"
                    f"  ×{len(flist):3d}  [single-frame]"
                )
                for raw_bytes, first_ts in unique_raw.items():
                    p(f"    {_rel(first_ts, mark.ts)}  {_hex(raw_bytes)}")
            else:
                p(f"  PGN {pgn:6d} (0x{pgn:05X})  SA=0x{sa:02X}{da_str}  ×{len(flist):3d}")
                if completions:
                    # Deduplicate payloads but show first occurrence time
                    seen: dict[bytes, float] = {}
                    for t, pt in completions:
                        if pt not in seen:
                            seen[pt] = t
                    for payload, first_ts in seen.items():
                        p(f"    {_rel(first_ts, mark.ts)}  {_hex(payload)}")
                else:
                    p("    (no complete fast-packet payload captured)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Broadband CAN sniffer for B&G line pings")
    ap.add_argument("--channel", default="can0")
    ap.add_argument(
        "--output",
        default="/tmp/sniffer_out.txt",
        help="Also write summary to this file (default: /tmp/sniffer_out.txt)",
    )
    args = ap.parse_args()

    frames: list[Frame] = []
    marks: list[Mark] = []
    fp = _FpBuf()
    stop = threading.Event()

    t = threading.Thread(target=_reader, args=(args.channel, frames, fp, stop), daemon=True)
    t.start()

    print(f"Listening on {args.channel}  (Ctrl-C / q to stop)")
    print("Keys:  b = boat-end ping   p = pin-end ping\n")

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            now = time.time()
            ts_str = datetime.fromtimestamp(now, tz=UTC).strftime("%H:%M:%S.%f")[:-3]
            if ch in ("\x03", "\x04", "q", "Q"):
                break
            elif ch in ("b", "B"):
                marks.append(Mark(now, "b"))
                sys.stdout.write(f"\r[{ts_str}] *** BOAT END marked ({len(frames)} frames)\n")
                sys.stdout.flush()
            elif ch in ("p", "P"):
                marks.append(Mark(now, "p"))
                sys.stdout.write(f"\r[{ts_str}] *** PIN END  marked ({len(frames)} frames)\n")
                sys.stdout.flush()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    stop.set()
    t.join(timeout=2.0)

    print(f"\nCapture complete: {len(frames)} frames, {len(marks)} mark(s).")
    _summarise(frames, marks, fp)

    with open(args.output, "w") as f:
        f.write(f"Capture complete: {len(frames)} frames, {len(marks)} mark(s).\n")
        _summarise(frames, marks, fp, out=f)
    print(f"[Summary also written to {args.output}]")


if __name__ == "__main__":
    main()
