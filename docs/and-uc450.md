# A&D UC-450BLE body-composition scale

Plugin: `src/vitals/devices/and_uc450.py`. Opportunistic sensor
(`INTERACTION = "opportunistic"`, category `scale`).

The UC-450BLE deliberately avoids the standard Bluetooth Weight Scale
(`0x181D`) and Body Composition (`0x181B`) services, so the generic
`gatt-sensor` plugin can't read it. It uses a framed proprietary
transport over service `0xA602` — a **Lifesense OEM** protocol (the
scale's DIS manufacturer string reads `Lifesense`; the `0xA6xx`
characteristic block matches Lifesense's scale family), rebadged by A&D.

**Source.** First transcribed from a Blutter (Dart AOT) decompile of the
A&D Heart Track Android app (`jp.co.aandd.hearttrack`,
`flutter_bluetooth_devices/src/and/scale/uc450/**`), then corrected
against a **btsnoop capture of the vendor app driving the real scale**
(2026-07-17, `hci_snoop20260708171816.cfa` — onboarding + a live
weigh-in). Where the two disagree, this doc records the wire truth.

> **Status: hardware-verified end-to-end (2026-07-18).** The full vendor
> exchange — subscribe → login → set-time → sync → measurement — was
> first decoded from a btsnoop capture (90.60 kg + 520 Ω), then the
> rewritten plugin drove the real `UC-450BLE-CV_A22CF9` itself and read a
> **live weigh-in of 90.3 kg + 494 Ω impedance** (fresh values, not a
> replay), producing a `body_weight` record with the scale-stamped time.
> The already-onboarded scale skipped init/binding and accepted the
> `bound=1` login echo from a host it had never seen.

## Decompile → capture corrections

The static teardown got the shape right but four load-bearing details
wrong; all four are why the 2026-07-15 bring-up stalled at transport-ACK:

1. **Command codes are the canonical (`fromByte`) values** in both
   directions. The app's `getByte == fromByte << 1` is an internal
   artifact that never appears on air — the earlier plugin was sending
   doubled codes the scale silently ignored.
2. **The `len` byte counts command + payload** (`2 + len(payload)`), not
   payload alone.
3. **The scale leads, but only once subscriptions complete.** It sends
   its login request within ~10 ms of the last CCC write (vendor order:
   `0xA625` notify, `0xA621` notify, `0xA620` indicate). The 07-15
   "scale is passive" finding was an artifact of driving it with wrong
   frames before/without the full subscription.
4. **"Auth" is even simpler than the challenge-echo reading**: the scale
   volunteers a 4-byte identity code in its login request and the phone
   echoes it back. No registration command is ever sent; the scale never
   learns the phone's identity, so any host can read an onboarded scale.

Also confirmed: transport ACK is `00 01 01`, link stays **unencrypted**
(no bond; `pair()` is refused), set-time uses the **Unix epoch**.

## GATT wiring

Custom command service **`0xA602`**, two logical pipes plus DIS:

| UUID | Direction | Role |
|---|---|---|
| `0xA620` | scale → phone | **indicate** — subscribed but carried no frames in the capture; its CCC write is what triggers the scale's login request |
| `0xA621` | scale → phone | **notify** — all scale data frames |
| `0xA622` | phone → scale | **write-no-rsp** — host transport-ACK (`00 01 01`) per scale frame |
| `0xA624` | phone → scale | **write-no-rsp** — all host command frames |
| `0xA625` | scale → phone | **notify** — scale transport-ACK (`00 01 01`) per host frame |
| `0x2A25` (DIS) | read | serial = MAC in reverse byte order |

DIS oddities: manufacturer = `Lifesense`, model number = the literal
string `Model Number`, FW/SW `1.4.0.10`, HW `1.0.0.0`.

## Frame envelope

No CRC/checksum.

```
[frag][len][cmd_hi cmd_lo][payload…]
```

- `frag` — high nibble = total packet count, low nibble = packet index.
  Single-packet data frame `0x10`; transport ACK `0x00`.
- `len` — **command + payload** byte count (`2 + len(payload)`).
- `cmd` — big-endian u16, canonical codes:

| Wire | Direction | Command |
|---|---|---|
| `0x0007` | scale → phone | login request (identity code + battery) |
| `0x0008` | phone → scale | login response (echo) |
| `0x0009` | scale → phone | init request (asks for a property; `0x18` = clock) |
| `0x000A` | phone → scale | init response (property echo + clock) |
| `0x0003` | phone → scale | binding (`[user][01]`) |
| `0x0004` | scale → phone | binding response (`[01][…]`) |
| `0x1002` | phone → scale | set time (`[03][utc u32 BE][tz i8]`) |
| `0x1000` | scale → phone | setting ack (`[cmd u16 echo][status]`) |
| `0x4801` | phone → scale | sync request (`[user][01]`, user 0 = all) |
| `0x4802` | scale → phone | measurement data (one reading) |
| `0x0001`/`0x0002` | — | registration pair (decompile only; never seen on air) |
| `0x1004` | phone → scale | set unit (decompile only; untested) |

Both sides transport-ACK every data frame with `00 01 01` before the
next application frame moves.

## Connection sequence (from the capture)

First-time onboarding (vendor app, connection 1):

```
subscribe A625, A621, A620            (CCC order matters — A620 last)
scale → 0x0007  01 <code:4> 01 00 64            login request (batt 100 %)
phone → 0x0008  01 01 <code:4> 01 00 02         login response, bound=0
scale → 0x0009  18                              init request (wants clock)
phone → 0x000A  18 <utc:4> <tz>                 init response
phone → 0x0003  00 01                           bind user 0
scale → 0x0004  01 de                           bind ok
phone → 0x1002  03 <utc:4> <tz>                 set time
scale → 0x1000  10 02 01                        ack
(phone disconnects, reconnects)
```

Regular sync (connection 2) skips init/binding — the phone answers the
login request with **bound=1** (`… 01 01 02`), sets the clock, then:

```
phone → 0x4801  00 01                           sync request
        …8 s pass — user standing on the scale…
scale → 0x4802  <measurement payload>           taken + stamped live
```

The timestamps prove the scale clock: set-time carried `0x6A5AAAC2`
(2026-07-17 22:20:50 UTC, tz byte `0xFB` = −5 h — the capture phone ran
US Eastern) and the measurement 8 s later was stamped `0x6A5AAAC9`.

The plugin always claims `bound=1` (the scale can't tell hosts apart)
and additionally answers an init request + sends a binding if the scale
asks — covering a factory-fresh scale with the same code path.

## Measurement record (`0x4802` payload)

Fixed 8-byte head, big-endian:

| Payload off | Size | Field |
|---|---|---|
| 0 | u16 | `remaining` — records still queued after this one |
| 2 | u16 | `sequence` — 1-based index of this record |
| 4 | u16 | flags / presence bitmask (below) |
| 6 | u16 | weight — `raw / 100` kg (always present) |

Captured example (flags `0x4008` = UTC + impedance, unit bits `00` = kg):

```
00 00  00 01  40 08  23 64  6a 5a aa c9  02 08  00
remain seq    flags  90.60  utc          520 Ω  (trailing byte, ignored)
```

The flags word gates optional fields that follow at offset 8, each
present iff its bit is set, in this order (sizes/divisors from the
decompile; UTC + impedance capture-confirmed):

| Bit | Field | Size | Encoding |
|---|---|---|---|
| 0–1 | unit | — | 0 kg / 1 lb / 2 st / 3 catty |
| 2 | user number | u8 | profile index |
| 3 | utc | u32 | Unix epoch seconds ✓ |
| 4 | timezone | i8 | hours offset |
| 5 | timestamp | 7 B | local date/time (sub-structure unconfirmed; utc used instead) |
| 6 | BMI | u16 | `/10` |
| 7 | body fat % | u16 | `/10` |
| 8 | basal metabolism | u16 | kcal (integer) |
| 9 | muscle % | u16 | `/10` |
| 10 | muscle mass | u16 | `/100` kg |
| 11 | fat-free mass | u16 | `/100` kg |
| 12 | soft lean mass | u16 | `/100` kg |
| 13 | body-water mass | u16 | `/100` kg |
| 14 | impedance | u16 | ohms ✓ |

The scale itself sends only weight + impedance; the vendor app derives
BMI/fat/muscle on-phone with A&D's bundled Lifesense BIA engine
(`libbia_calc.so::doBIACalc`). Vitals does **not** ship that engine — it
records whatever the frame carries.

### Mapping to records

- `body_weight` (kg) — always; battery % from the login request is
  logged, and impedance / any BIA extras without a catalog type ride on
  the weight record's `meta`.
- `body_mass_index`, `body_fat_percentage`, `lean_body_mass` (from
  fat-free mass) — if their bits are ever set.

All records dedup on the scale's own timestamp (from the reading's
`utc`, else wall-clock), so a re-drained reading upserts to the same
records.

## Remaining unknowns

The end-to-end plugin run (2026-07-18) confirmed the live path
(subscribe → login → set-time → sync → one `remaining=0` measurement,
90.3 kg + 494 Ω); these edges are still unproven:

1. **Multiple stored readings** — every real weigh-in so far streamed a
   single `remaining=0` frame; the countdown drain over several queued
   readings is inferred from the field layout, not yet observed.
2. Whether the scale advertises unsolicited after an *unattended*
   weigh-in (opportunistic model), or only while awake from a step-on —
   both live reads were driven by a scan running as the scale woke.
3. The trailing byte after the flagged measurement fields, the `0x0004`
   binding-response payload (`01 de`), and the hello's constant bytes.
4. The factory-fresh path (init request → init response → binding): the
   test scale was already onboarded, so it skipped straight to login and
   accepted `bound=1`. The plugin still answers an init request + binds
   if a fresh scale asks, but that branch is untested on hardware.

Artifacts: Blutter findings at
`~/projects/mobile-linux/and-heart-track-teardown/blutter_findings/uc450_*.md`;
capture `hci_snoop20260708171816.cfa` (misnamed by the phone; taken
2026-07-17).
