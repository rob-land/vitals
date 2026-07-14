# iHealth Gluco+ BG5S blood-glucose meter

> **Status: hardware-verified on a real BG5S (2026-07-13), except stored-
> record parsing.** The auth handshake, framing, fragment reassembly + ACK,
> and status decode were all confirmed live on a real BG5S unit: it
> authenticates (`0xFD`) and returns a correct status (battery 100 %, clock,
> timezone −5 h, unit). The meter was empty (`offline_count = 0`), so the
> 7-byte offline-record decode is still unit-tested only — re-verify once a
> reading is stored. Everything is derived from the Gluco-Smart app
> (`com.ihealth.communication` SDK v4.10.4) + native `libiHealth.so`; the
> pure code is unit-tested (`tests/test_ihealth_bg5.py`) with an independent
> XXTEA cross-check. See the checklist at the end.

Implemented by `src/vitals/devices/ihealth_bg5.py` as an **opportunistic
sensor** (`CATEGORY = "glucose"`). The BG5S does **not** expose the standard
Bluetooth Glucose service (`0x1808`); it uses iHealth's proprietary "Jiuan"
protocol over a vendor GATT service. When the meter is powered on (strip
inserted, or button pressed) it advertises as `BG5S`; the ScanBroker routes
the advertisement here, and we connect, authenticate, drain the meter's
stored readings, and record each as `blood_glucose`.

## GATT — the ASCII-UUID trick

iHealth encodes its service and characteristic UUIDs as **ASCII text**: the
16 bytes of the 128-bit UUID spell a string. The BG5S uses:

| Role | Text | UUID |
|---|---|---|
| Service | `com.jiuan.BGV42` | `636f6d2e-6a69-7561-6e2e-424756343200` |
| Write (host→meter) | `rec.jiuan.BGV42` | `7265632e-6a69-7561-6e2e-424756343200` |
| Notify (meter→host) | `sed.jiuan.BGV42` | `7365642e-6a69-7561-6e2e-424756343200` |

The prefixes are named from the **meter's** point of view: `rec` is where
the meter *receives* (so the host writes there — it has `write` /
`write-without-response`), `sed` is where the meter *sends* (so the host
subscribes — it has `notify`). Sibling glucose models substitute `BGU42` /
`BGV20` / `BGV40`. Rather than hard-code one model's hex, the plugin
**discovers** the characteristics by decoding each UUID to text and matching
the prefixes, so every BG variant works. No OS-level BLE bond is needed —
the app-level handshake is the pairing. The meter advertises as
`BG5S <serial>` (e.g. `BG5S 12345`) alongside the standard 1800/1801/180a
services and reports MTU 23, so replies fragment freely.

## Framing

A command *body* is `[0xA2, cmd, …args]` (`0xA2` is the SDK's per-family
"command flag"). The transport wraps it:

```
[head][len][frag][seq][ 0xA2 cmd args… ][checksum]
```

- `head` — `0xB0` host→meter, `0xA0` meter→host.
- `len` — byte count from `frag` through the last arg (i.e. `len(frame) − 3`).
- `frag` — `0x00` for a single unfragmented frame; for fragmented transfers
  the high nibble is `fragment_count − 1` and the low nibble counts **down**
  from `count − 1` to `0`, so the low nibble is `0` on the final fragment.
- `seq` — a per-frame sequence byte (host counter starts at 1, +2 each send).
- `checksum` — 8-bit sum of every byte from `frag` through the last arg.

Bodies ≤ 15 bytes are one frame; longer bodies split into 14-byte chunks.
The only host→meter body that fragments is the 18-byte auth INIT. The meter
fragments its longer replies the same way; **every meter frame whose `frag`
byte is not `0xF0` must be answered with a 6-byte host ACK**
`[0xB0 03 b2 b3 0xA2 sum]` (`b2 = (frag & 0x0F) + 0xA0`, `b3 = seq + 1`) —
this includes single unfragmented `frag = 0x00` replies, not just mid-stream
fragments. Skip the ACK and the meter retransmits the frame and the flow
stalls (learned the hard way on-device). The meter also sends its own 6-byte
ACKs (head `0xA0`, length 6) for each host fragment; ignore those. The
command byte rides in the first fragment (`frame[5]`), with `0xA2` echoed at
`frame[4]` in every fragment.

## Authentication (XXTEA challenge-response)

1. Host → `[0xA2, 0xFA, <16 random nonce bytes>]` (the meter ignores the
   nonce value; the app still sends one).
2. Meter → `0xFB` reply carrying a **48-byte challenge** = three 16-byte
   blocks `e ‖ b ‖ d`.
3. Host computes and sends `[0xA2, 0xFC, <16-byte token>]`:

   ```
   seed  = XXTEA( wordrev(e), J )
   token = wordrev( XXTEA( wordrev(d), seed ) )      # block b is unused
   J     = XXTEA( nibbleswap(model_key), nibbleswap(g) )
   ```

   `XXTEA` is textbook XXTEA-encrypt on one 16-byte block, big-endian words,
   19 rounds. `wordrev` reverses the bytes within each 4-byte word;
   `nibbleswap` swaps each byte's nibbles. `g` is the fixed ASCII key
   `Ch/HQ4LzItYT42s=`.
4. Meter → `0xFD` success (or `0xFE` fail).

`model_key` is a 16-byte per-model secret baked into `libiHealth.so`'s
`getKey()` (a plain model→key table — no runtime RSA; the RSA code paths in
that library belong to unrelated `decrypt`). The BG5S connects on its
`com.jiuan.BGV*` service, which the SDK maps to the `o1 = false` path and
authenticates as model **`BG5L`**:

| Model | Key (hex) |
|---|---|
| BG5 | `1108781187f7f1d5f10e35f87a3bcb98` |
| **BG5L** (used by BG5S) | `48e05e3231bbc447a066d8e9a2927b4e` |
| BG1304 | `cdd03d65291f60b43f5aeae0c051578d` |
| BG1305 | `2944688c0d2ec8326f5fa3c687d5b44e` |

(The `com.jiuan.dev` service is the `o1 = true` firmware/DFU variant, which
would authenticate as `BG5S` — a key the table does *not* contain — and is
out of scope here.)

## Commands

All bodies begin `[0xA2, cmd]`:

| Purpose | Body | Reply cmd |
|---|---|---|
| Auth INIT | `A2 FA <16 nonce>` | `FB` challenge |
| Auth reply | `A2 FC <16 token>` | `FD` / `FE` |
| Get status | `A2 26 00 00 00` | `26` |
| Get offline packet *i* | `A2 4B i 00 00` | `4B` |
| Set time | `A2 49 yy MM dd HH mm ss tz` | `49` |
| Set unit | `A2 23 u 00 00` | `23` |
| Start measurement | `A2 31 i` | `31…36` |
| Delete offline data | `A2 43 00` | `43` |

### Status reply (`0x26`)

`[battery, yy, MM, dd, HH, mm, ss, tz, used_hi, used_lo, off_hi, off_lo,
codeBlood, codeCtl, unit]` — `battery` is 1–100 (else invalid), `tz` is
quarter-hours (bit 7 = sign), `off_hi/off_lo` is the **stored-reading
count**, `unit` is 0/1/2.

### Offline packet (`0x4B`)

`[record_count, packet_index, records…]`, up to 19 records per packet, each
**7 bytes**:

| Byte | Bits |
|---|---|
| 0 | b7 = time-unset flag, b0–6 = year − 2000 |
| 1 | b0–3 = month, b4–7 = hour high (`<<2`) |
| 2 | b0–4 = day, b5–7 = timezone high |
| 3 | minute |
| 4 | b0–5 = second, b6–7 = hour low |
| 5 | b3–7 = timezone low, b0–1 = value high |
| 6 | value low — `value = (b5 & 3) << 8 | b6`, **mg/dL** |

The glucose value is always transmitted in **mg/dL** regardless of the
meter's display unit; Vitals stores it with `unit: "mg/dL"` and `core.units`
converts to canonical mmol/L (÷ 18.0156). Readings dedupe on the meter's
UTC timestamp, so re-syncs upsert rather than duplicate. Records whose
time-unset flag is set carry `meta.time_unverified`.

## Sync flow

connect → discover `sed.`/`rec.` chars + `start_notify` → auth handshake →
get status (read `offline_count`) → request packets `0..count/19`, ACKing
fragments, parsing 7-byte records → disconnect. A 30 s per-address cooldown
throttles reconnects while the meter stays awake.

## Hardware verification checklist

Confirmed live on a real BG5S (2026-07-13):

- [x] Discovery/match by name `BG5S …` + service `com.jiuan.BGV42`.
- [x] Characteristic roles: host writes `rec.jiuan.*`, notifies on
      `sed.jiuan.*`.
- [x] Auth: INIT → `0xFB` (48-byte challenge, delivered as four fragments)
      → our `0xFC` token → `0xFD` success, using the `BG5L` key.
- [x] Framing, fragment reassembly, and the ACK-everything-but-`0xF0` rule.
- [x] `get status` (0x26) decodes correctly — battery, clock, timezone,
      counts, unit — delivered as two fragments.

Still to confirm (the test meter was empty, `offline_count = 0`):

- [ ] With ≥1 stored reading, `offline_count` > 0 and each 0x4B packet's
      7-byte records parse to the right value/time. Take a fingerstick (or
      a control-solution) measurement first, then re-sync.
- [ ] A meter holding >19 readings fragments a packet; confirm the record
      count matches.
- [ ] A known value round-trips to the correct mg/dL (dashboard shows the
      expected mmol/L), and a known local time lands at the right UTC
      instant (the meter reported timezone −5 h in the test).
