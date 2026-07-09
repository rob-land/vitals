# A&D UP-200BLE fingertip pulse oximeter

> **Status: protocol fixed from a btsnoop of the vendor app; live end-to-end
> confirmation still pending (device wasn't on a finger).** The earlier
> "all-zeros, never streams" symptom was **not** bonding or a clock-set — the
> command frames were simply **zero-padded and the device ignored them**. A
> btsnoop of A&D Heart Track showed the real frames are short (`9b 01 1c` to
> start, not `9b 01 00 00 00 1c`), there is **no encryption/bond**, and the
> SpO₂+PR notifications are **8 bytes, not 16**. The plugin now sends the
> byte-exact frames and its real-time decoder is unit-tested against actual
> captured notifications (`eb 01 05 55 5f 7f 00 24` → SpO₂ 95 / PR 85).

The A&D UP-200BLE is an A&D-rebadged **Contec CMS50D-family** fingertip
oximeter. Despite the A&D branding it is **not** an A&D-proprietary device
and it does **not** use the standard Bluetooth PLX profile (`0x1822`) — it
speaks the Contec real-time protocol over a custom GATT service.

Implemented by `src/vitals/devices/and_oximeter.py` as an **opportunistic
sensor**: while worn, the oximeter advertises; the ScanBroker routes each
advertisement to the plugin, which connects, streams for a few seconds,
and records the **median** SpO2 and pulse (rated `high` quality, so they
outrank a watch/ring estimate).

Protocol reverse-engineered from **A&D Heart Track 2.3.4**
(`jp.co.aandd.hearttrack`) via Blutter decompilation of its Dart
`ContecPulseoxCms50dDevice`, and confirmed on the wire against a real unit
(`UP-200BLE_005765`, adv service `0xFF12`, A&D company id `0x0069`). Full
teardown artifacts: `~/projects/mobile-linux/and-heart-track-teardown/`.

## GATT

| Role | UUID |
|---|---|
| Service | `0000ff12-0000-1000-8000-00805f9b34fb` (`0xFF12`) |
| Write (host → device) | `0xFF01` |
| Notify (device → host) | `0xFF02` |

(The device also exposes an `0xFF00` service with `0xFF04/05/06`; the
oximeter protocol does not use it.)

## Frames

Every frame is `[header][payload…][checksum]`:

- **header** — first byte, always has bit 7 set; it also selects the
  frame length.
- **checksum** — last byte = `sum(all preceding bytes) & 0x7F`.
- Reassembly: scan the notify stream for a byte with bit 7 set (a header),
  read `length` bytes; bytes with bit 7 clear are inter-frame noise and
  are skipped.

Header → length (from the wire): `0x9A`→2, `0x9B`→3, `0xF3`→3, `0xEB`→depends
on its type byte (`0x01` SpO2+PR→**8**, `0x7F`→3).

## Commands (write to `0xFF01`, write-without-response)

The frames are **short — no zero padding** (this was the whole bug: the
padded form was silently ignored). Checksum = `sum(preceding) & 0x7F`.

| Purpose | Bytes |
|---|---|
| Device-connection notification | `9A 1A` |
| Start SpO2 + pulse streaming | `9B 01 1C` |
| Stop streaming | `9B 7F 1A` |

**Working sequence** (no bonding, no clock-set — a btsnoop of the app
confirmed this streams immediately):

1. enable notifications on `0xFF02` (writes its CCCD `01 00`);
2. write `9A 1A` to `0xFF01`;
3. write `9B 01 1C` to `0xFF01`.

The device then streams continuously. (The vendor app *also* sets the clock
with an `0x83` TimeTick on a separate first connection, but that's only to
timestamp the device's own memory — not needed to read the live stream.)

## SpO2 + pulse frame (`0xEB 0x01`, 8 bytes)

```
off  field
 0   0xEB   header
 1   0x01   type = SpO2+PR
 2   flags  bit1 (0x02) carries pulse bit 7
 3   pulse low 7 bits
 4   SpO2 %      (0x7F / 127 = no finger / invalid)
 5-6 further real-time fields (incl. perfusion index) — not decoded
 7   checksum
```

Example (real): `eb 01 05 55 5f 7f 00 24` → SpO₂ 95 %, pulse 85 bpm.

```
pulse = ((frame[2] & 0x02) << 6) | frame[3]
spo2  =  frame[4]                 # 127 → invalid
```

Invalid sentinels: SpO2 `127` (or `0`) and pulse `0`/`255` are dropped.

**Perfusion index** is in `frame[5..14]` but the A&D app decodes only SpO2
and pulse, so its exact offset is unconfirmed (Contec convention: a later
byte as PI×10). Not recorded until confirmed from a live capture.
