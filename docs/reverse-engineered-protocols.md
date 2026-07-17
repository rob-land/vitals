# Reverse-engineered device protocols

An index of the device protocols Vitals speaks, where each one came from,
and which physical devices it covers. **Tested directly** means confirmed
on real hardware (not just unit-tested against captured/decompiled byte
layouts).

The five protocols in the first table were recovered by tearing down
vendor Android apps (APK/xAPK) — Blutter/decompilation of the app code,
`libiHealth.so` native analysis, and btsnoop HCI captures of the app
driving the real device. The protocols in the second table came from
open references (PebbleOS/Gadgetbridge/Espruino/InfiniTime source and the
Bluetooth SIG spec), not from an APK teardown, and are listed for a
complete picture.

## Protocols reverse-engineered from vendor APKs

| Protocol | Source APK / artifact | Plugin | Devices covered | Tested directly |
|---|---|---|---|---|
| **Contec CMS50D real-time** (custom GATT `0xFF12`, not the SIG PLX profile) | A&D Heart Track 2.3.4 (`jp.co.aandd.hearttrack`) — Blutter decompile of `ContecPulseoxCms50dDevice` + btsnoop of A&D Heart Track | `and_oximeter.py` | A&D **UP-200BLE** fingertip oximeter; other A&D-rebadged Contec CMS50D-family oximeters | ✅ **Yes** — live SpO₂/pulse confirmed on a finger (`UP-200BLE_005765`), 2026-07-09 |
| **iHealth "Jiuan"** (ASCII-encoded `com.jiuan.*` GATT UUIDs, XXTEA challenge-response auth, fragmented+ACKed framing) | iHealth Gluco-Smart (`com.ihealth.communication` SDK v4.10.4) + native `libiHealth.so` `getKey()` model→key table | `ihealth_bg5.py` | iHealth Gluco+ **BG5S** (auths as `BG5L`); sibling BG5 / BG1304 / BG1305 keys in table; discovers `BGU42`/`BGV20`/`BGV40` service variants | ⚠️ **Partial** — auth handshake + status (battery/clock/tz/unit) confirmed live on a real BG5S 2026-07-13; **stored-record (`0x4B`) parse still unproven** (test meter was empty) |
| **OMRON memory-map** (proprietary framed EEPROM-read over a bonded link; no SIG BP service) | OMRON Connect (`012.000.00001`), cross-checked against [omblepy](https://github.com/userx14/omblepy) | `omron_bp.py` | OMRON **BP5465** (= HEM-7382T1); siblings HEM-7380 / 7383–7389T1 share the driver; legacy Complete/EVOLV service also mapped | ✅ **Yes** — service, bond, Start/read/End session, per-user record regions (`0x0810`/`0x0E50`) and 16-byte record layout all confirmed; a known 115/72/73 reading round-trips. (Full opportunistic `_sync` end-to-end not yet run, but each step is proven.) |
| **Yucheng YCBT** (length-prefixed, CRC16-framed command catalog on service `be94…`) | "SmartHealth"/YCBT ring SDK app | `yucheng_ring.py` | The whole family of no-name **Yucheng smart rings & bands** built on the YCBT SDK; reads the capability bitmap and gates per-sensor reads, so variants with fewer sensors work | ❌ **No** — frame codec + record decoders unit-tested against recovered byte layouts; live block-sync handshake needs on-device verification |
| **WaterH** (ASCII-tagged `GT`/`PT`/`RP`/`RT` ops on `FFE5/FFE9` ↔ `FFE0/FFE4`; 13-byte drink records) | WaterH bottle app | `waterh_bottle.py` | **WaterH-Bottle-1 "Vita"** (battery, water temp, fill level, TDS, drink log) and **WaterH-Bottle-B003 "Boost"** (battery + drink log only) | ❌ **No** — frame codec + drink-log decoder unit-tested; live connect/registration (tap-to-confirm) handshake needs on-device verification. (Goal/reminder *push* to the bottle was deployed to the phone 2026-07-10.) |
| **A&D UC-450 framed** (Lifesense OEM; custom service `0xA602`; `[frag][len][cmd u16][payload]`, len covers cmd, no CRC; **scale-led** Login→(Init/Bind)→SetTime→Sync once all three CCCs are enabled; identity-echo auth, unbonded link; transport ACK `00 01 01`) | A&D Heart Track 2.3.4 (`jp.co.aandd.hearttrack`) — Blutter decompile of `flutter_bluetooth_devices/.../scale/uc450/**`, corrected by a 2026-07-17 btsnoop of the app driving the scale | `and_uc450.py` | A&D **UC-450BLE** body-composition scale (weight `÷100` kg + flag-gated BIA fields; the scale itself sends weight + impedance) | ⚠️ **Partial** — full vendor exchange incl. a live measurement (90.60 kg + 520 Ω, scale-stamped) decoded from the 2026-07-17 capture on a real UC-450BLE-CV; plugin + tests pin the exact captured bytes (canonical cmd codes — the decompile's `<<1` codes and "phone-initiated" 07-15 reading were both wrong). The rewritten plugin's own end-to-end run against the scale is still pending |

## Protocols from open references (not APK teardowns)

Included for completeness — these were built from published/open-source
protocol docs, not by reversing a vendor app.

| Protocol | Source | Plugin | Devices covered | Tested directly |
|---|---|---|---|---|
| **Pebble Protocol over PPoGATT** (framed, sequenced transport; health, timeline, apps, notifications, music, weather, firmware) | Open-source PebbleOS / libpebble / Gadgetbridge; live scan of a Core Devices "obelix" (fw v4.9.142) | `pebble/` | Classic Pebbles and revived Core Devices watches (Core 2 Duo, Core Time 2) running PebbleOS/repebble | ✅ **Yes** — sync + weather confirmed on a real Pebble |
| **Espruino REPL + Gadgetbridge `GB()` over Nordic UART** | espruino.com reference + Gadgetbridge Bangle support | `bangle.py`, `bangle_dfu.py` | **Bangle.js 2** (and Espruino devices generally); Nordic-DFU firmware flashing | ✅ **Yes** — synced; Bangle DFU exercised on hardware |
| **InfiniTime standard-SIG + custom services** (Battery/DevInfo/HR/CTS/ANS + custom motion & SimpleWeatherService; legacy-DFU) | Open InfiniTime BLE docs | `pinetime.py`, `pinetime_dfu.py` | **PineTime** running InfiniTime | ✅ **Yes** — firmware update + push confirmed (`eed2523`, deployed 2026-07-10) |
| **Standard Bluetooth SIG health profiles** (inherited from the former *gauge* app) | Bluetooth SIG GATT spec | `sensors/` (`gatt.py`, `decoders.py`) | Any spec-compliant sensor: HR straps (`0x180D`), weight scales (`0x181D`), BP cuffs (`0x1810`), glucose meters (`0x1808`), pulse oximeters (`0x1822`), thermometers (`0x1809`); plus Xiaomi Mi Body Composition Scale via advertisement `service_data` (`0x181B`) | ➖ Generic profile decoders (unit-tested); coverage depends on the specific spec-compliant device presented |

## Notes

- **Why proprietary at all** — every device in the first table advertises
  a *custom* GATT service and deliberately avoids the matching standard
  SIG profile (PLX `0x1822`, Glucose `0x1808`, Blood Pressure `0x1810`),
  which is exactly why the standard `sensors/` plugin can't read them and
  each needed its own teardown.
- **A&D UP-200BLE caveat** — the plugin docstring still carries an older
  "parked / unproven" note from before the btsnoop fix; the protocol was
  subsequently corrected (short, unpadded frames; 8-byte SpO₂+PR frames)
  and **confirmed live on a finger 2026-07-09**. Treat it as tested.
- **Teardown artifacts** for the A&D work live at
  `~/projects/mobile-linux/and-heart-track-teardown/`.
- Per-device protocol detail lives in `docs/and-oximeter.md`,
  `docs/ihealth-bg5.md`, `docs/omron-bp.md` and `docs/and-uc450.md`; the
  Yucheng and WaterH protocols are documented in their plugin module
  docstrings.
