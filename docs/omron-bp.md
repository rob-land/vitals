# OMRON blood-pressure monitors (BP5465 / HEM-738xT1)

> **Status: hardware-verified on a real BP5465.** Service, bonding, the
> Start/read/End session, the per-user record addresses, and the record
> byte layout were all confirmed on the device — a known 115/72/73 reading
> round-trips through the plugin. The only step not yet run live is the
> plugin's full opportunistic `_sync` end to end (each component is
> individually proven).

Implemented by `src/vitals/devices/omron_bp.py` as an **opportunistic
sensor** (`CATEGORY = "blood_pressure"`). OMRON Connect / Intelli-IT
monitors do **not** expose the standard Bluetooth Blood Pressure service —
they use a proprietary **memory-map** protocol: over a bonded link you send
framed EEPROM-read commands and the monitor streams raw EEPROM bytes back,
which you slice into fixed 16-byte reading records.

Reverse-engineered from OMRON Connect (`012.000.00001`) and cross-checked
against the [omblepy](https://github.com/userx14/omblepy) project. The
BP5465 is internally **HEM-7382T1**; siblings HEM-7380/7383–7389T1 share
this driver.

## GATT

Two generations. The BP5465 is expected to be the **newer** one:

| | Newer (HEM-738xT1, incl. BP5465) | Legacy (Complete/EVOLV) |
|---|---|---|
| Service | `0000fe4a-0000-1000-8000-00805f9b34fb` | `ecbe3980-c9a2-11e1-b1bd-0002a5d5c51b` |
| Auth | OS-level BLE bond only, **no unlock key** | 16-byte unlock key write |

Both reuse four **TX** (write, host→device, `db5b55e0…` +3) and four **RX**
(notify, device→host, `49123040…` +3) characteristics.

## Frames

`[len][type][payload…][bcc]` — `len` is the whole-frame length; `bcc` makes
the XOR of the entire frame `0x00`.

| Purpose | Bytes |
|---|---|
| Start transmission | `08 00 00 00 00 10 00 18` |
| EEPROM read | `08 01 00 <addr_hi> <addr_lo> <size> 00 <bcc>` → reply `[len] 81 00 <data…> <bcc>` |
| End transmission | `08 0F 00 00 00 00 00 07` |

## Session

Bond → enable notify on the RX char → **Start** (`08 00 00 00 00 10 00 18`)
→ walk each per-user record region reading one 16-byte record per command
until an empty slot (which the monitor answers with a short ack, no data) →
parse → **End** (`08 0F 00 00 00 00 00 07`) → disconnect. The two per-user
record regions on the BP5465 (hardware-confirmed) start at **`0x0810`** and
**`0x0E50`**, each a ring of up to 100 records. A read response carries its
data as `[len][81][00][addr_hi][addr_lo][size][data…][00][bcc]` (length =
size + 8); an empty slot returns `[08][81][00][addr…][e3][bcc]`.

## Record (16 bytes, hardware-confirmed)

```
0-1   uint16 LE: hour b0-4, day b5-9, month b10-13,
                 b14 = irregular-heartbeat, b15 = body-movement
2-3   uint16 LE: second b0-5, minute b6-11
6     sequence number
12    systolic - 25
13    diastolic
14    pulse
15    year - 2000
```

(This differs from the HEM-7380T1/7377T1 siblings — both the addresses and
the field positions are model-specific, so the BP5465 map was recovered
directly from the device.)

Recorded as `blood_pressure` (`{systolic, diastolic}`) with `pulse_rate`,
`irregular_heartbeat`, and `body_movement` in `meta`. Deduped on the
reading's timestamp so re-dumps upsert.

## Pairing notes (learned on hardware)

The monitor bonds to **one host at a time**: delete it in the OMRON Connect
phone app first. To bond from BlueZ: adapter must be **`pairable on`** with a
**NoInputNoOutput agent** registered, and the monitor in pairing mode
(blinking **P** — a long window; a short Bluetooth-button tap gives only a
brief, connect-flaky window). BlueZ's explicit `Pair()` returns
`AuthenticationFailed` unless pairable + agent are set. Once bonded it stays
bonded across reconnects. The **"P" pairing-mode window is the reliable time
to connect**; idle reconnects frequently time out.

Primary protocol reference: [omblepy](https://github.com/userx14/omblepy).
