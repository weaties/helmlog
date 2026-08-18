# PGN Notes — B&G / NMEA 2000

## Standard PGNs Supported

| PGN    | Description               | Key Fields                          |
|--------|---------------------------|-------------------------------------|
| 127250 | Vessel Heading            | heading (rad), deviation, variation |
| 128259 | Speed Through Water       | speed (m/s)                         |
| 128267 | Water Depth               | depth (m), offset                   |
| 129025 | Position Rapid Update     | latitude, longitude (1e-7 deg)      |
| 129026 | COG & SOG Rapid Update    | cog (rad), sog (m/s)                |
| 130306 | Wind Data                 | wind speed (m/s), wind angle (rad)  |
| 130310 | Environmental Parameters  | water temperature (K)               |

## Decoding Notes

- All multi-byte integers are **little-endian** (NMEA 2000 spec).
- Angles are in **radians × 10000** (i.e., `int16 / 10000.0` → radians) for most PGNs.
- Speed fields are typically in **0.01 m/s** units (i.e., `uint16 / 100.0` → m/s).
- Temperature is in **0.01 K** units (i.e., `uint16 / 100.0` → Kelvin; subtract 273.15 for Celsius).

## PGN Extraction from 29-bit CAN ID (J1939/N2K)

```
priority     = (arb_id >> 26) & 0x7
reserved     = (arb_id >> 25) & 0x1
data_page    = (arb_id >> 24) & 0x1
pdu_format   = (arb_id >> 16) & 0xFF
pdu_specific = (arb_id >> 8) & 0xFF
src_addr     = arb_id & 0xFF

# PDU2 (broadcast): pdu_format >= 240
pgn = (data_page << 16) | (pdu_format << 8) | pdu_specific

# PDU1 (peer-to-peer): pdu_format < 240
pgn = (data_page << 16) | (pdu_format << 8)
```

## Simrad/B&G Proprietary PGNs

Manufacturer code **0x9F41** (bytes `41 9F` at payload[0:2], little-endian).
Both PGNs use **Fast Packet** multi-frame encoding — reassemble with
`FastPacketBuffer` from `helmlog.nmea2000` before decoding.

| PGN    | CAN ID pattern | Description            | Key Fields                        |
|--------|---------------|------------------------|-----------------------------------|
| 130845 | 0x_DFF1D__    | Set Timer              | payload[6:10]==07 42 00 01 (SET discriminator); payload[10]=minutes |
| 130850 | 0x_9FF22__    | Start/Stop/Reset/NM    | payload[6]: 3D=start, 3E=stop, 3F=nearest-minute, 40=reset |

Running-state broadcasts also arrive on PGN 130845 with discriminator
`02 00 00 01` at payload[6:10] — these are ignored by the decoder.

Decoded via `helmlog.nmea2000._decode_130845` / `_decode_130850` into
`SimradTimerRecord` dataclasses.

## Fast Packet Reassembly

`FastPacketBuffer` in `helmlog.nmea2000` handles multi-frame reassembly.
Frame 0 carries `(seq<<5|0, total_bytes, data[0:6])`.
Subsequent frames carry `(seq<<5|frame_num, data[7*n:7*(n+1)])`.
Key is `(pgn, source_addr, seq)` so concurrent ECUs don't collide.

## TODO

- [ ] Verify heading reference field (magnetic vs true) in PGN 127250
