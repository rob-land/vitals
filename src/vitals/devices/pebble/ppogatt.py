"""PPoGATT GATT server — the host side of Pebble Protocol over GATT.

A Pebble (LE) doesn't expose its features as readable GATT
characteristics; they ride PPoGATT, a framed and sequenced transport.
The phone is the GATT *server*: over the LE link Vitals opens to the
watch (GATT is symmetric on one connection), the watch acts as GATT
client — it writes PPoGATT packets to our data characteristic and
subscribes for the packets we notify back. This matches Gadgetbridge's
PebbleGATTServer, which Vitals's wire format is pinned to.

This module hosts that server over BlueZ via dbus_fast (already bundled
as bleak's Linux backend, so no new dependency). It is deliberately just
the byte pipe: inbound writes surface through the `on_write` callback,
outbound bytes go out via `notify()`. The PPoGATT link layer
(reset/window/ACK, sequencing) and the Pebble Protocol above it live in
the device plugin — this layer neither frames nor interprets.

Server layout (Gadgetbridge-compatible):

  service 10000000-328e-0fbb-c642-1aa6699bdada
    char  10000001-…  write-without-response + notify  — the data pipe;
                      the watch writes inbound packets, we notify
                      outbound ones (BlueZ manages the CCCD for notify)
    char  10000002-…  read                             — handshake meta
  service badbadba-dbad-badb-adba-badbadbadbad         — a second service
                      Pebble firmware expects to be present

No advertisement is needed: Vitals connects to the watch as central (see
`PebbleGateway` below), and the watch reaches this server over the same link.
Requires bluetoothd's experimental interfaces (`Experimental = true`),
like the rest of the Pebble path.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable

log = logging.getLogger(__name__)

# Phone-hosted PPoGATT server (Gadgetbridge PebbleGATTServer UUIDs).
PPOGATT_SERVER_SERVICE = "10000000-328e-0fbb-c642-1aa6699bdada"
PPOGATT_DATA_CHAR      = "10000001-328e-0fbb-c642-1aa6699bdada"  # write + notify
PPOGATT_META_CHAR      = "10000002-328e-0fbb-c642-1aa6699bdada"  # read
# A second service Pebble firmware expects to exist alongside the data
# service; it carries no characteristics of its own.
PPOGATT_SECOND_SERVICE = "badbadba-dbad-badb-adba-badbadbadbad"

# PPoGATT protocol versions this gateway supports.
PPOGATT_MIN_VERSION = 0x00
PPOGATT_MAX_VERSION = 0x01
# Value of the meta characteristic, which the watch's PPoGATT client reads
# *first* (PPoGATTMetaV0): {min_version, max_version, 16-byte app UUID}.
# The all-zero UUID is Pebble's "system" UUID, routing this session to the
# system comm session (time, app messages, health). Per PebbleOS, an empty
# or short value — or an all-0xFF ("invalid") UUID — makes the watch reject
# the server at meta-read and tear down, so this 18-byte value is mandatory
# for the client to proceed to subscribe + the reset handshake.
PPOGATT_META_VALUE = bytes([PPOGATT_MIN_VERSION, PPOGATT_MAX_VERSION]) + bytes(16)

# Object paths under our application root.
APP_ROOT     = "/land/rob/vitals/ppogatt"
SERVICE_PATH = APP_ROOT + "/service0"
DATA_PATH    = SERVICE_PATH + "/data"
META_PATH    = SERVICE_PATH + "/meta"
SECOND_PATH  = APP_ROOT + "/service1"

GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHAR_IFACE    = "org.bluez.GattCharacteristic1"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
OBJECT_MANAGER     = "org.freedesktop.DBus.ObjectManager"


class PpogattServer:
    """Hosts the PPoGATT GATT server for one adapter.

    `on_write(data: bytes)` is invoked for each inbound packet the watch
    writes. `notify(data: bytes)` pushes an outbound packet to the watch
    (a no-op until the watch has subscribed). Lifecycle: `register()`
    then `unregister()`.
    """

    def __init__(self, bus, adapter: str = "hci0",
                 on_write: Callable[[bytes], None] | None = None):
        self._bus = bus
        self._adapter_path = f"/org/bluez/{adapter}"
        self.on_write = on_write
        self._objects: list = []
        self._data_char = None
        self._registered = False

    async def register(self) -> None:
        Application, Service, Characteristic = _object_classes()

        data = Characteristic(
            DATA_PATH, PPOGATT_DATA_CHAR, SERVICE_PATH,
            ["write-without-response", "notify"], on_write=self._dispatch)
        meta = Characteristic(
            META_PATH, PPOGATT_META_CHAR, SERVICE_PATH, ["read"],
            initial=PPOGATT_META_VALUE)
        service = Service(SERVICE_PATH, PPOGATT_SERVER_SERVICE)
        second = Service(SECOND_PATH, PPOGATT_SECOND_SERVICE)
        self._data_char = data
        self._objects = [service, data, meta, second]

        self._bus.export(APP_ROOT, Application(self._objects))
        for obj in self._objects:
            self._bus.export(obj.path, obj)

        manager = await self._interface(self._adapter_path, GATT_MANAGER_IFACE)
        await manager.call_register_application(APP_ROOT, {})
        self._registered = True
        log.info("PPoGATT: GATT server registered at %s", APP_ROOT)

    async def unregister(self) -> None:
        if not self._registered:
            return
        try:
            manager = await self._interface(
                self._adapter_path, GATT_MANAGER_IFACE)
            await manager.call_unregister_application(APP_ROOT)
        except Exception:
            log.debug("PPoGATT: unregister failed", exc_info=True)
        finally:
            for path in [obj.path for obj in self._objects] + [APP_ROOT]:
                try:
                    self._bus.unexport(path)
                except Exception:
                    pass
            self._objects = []
            self._data_char = None
            self._registered = False

    def notify(self, data: bytes) -> None:
        """Push one outbound PPoGATT packet to the watch."""
        if self._data_char is not None:
            self._data_char.send_notification(bytes(data))

    # ── internals ──────────────────────────────────────────────────

    def _dispatch(self, data: bytes) -> None:
        if self.on_write is not None:
            self.on_write(data)

    async def _interface(self, path: str, name: str):
        intro = await self._bus.introspect("org.bluez", path)
        obj = self._bus.get_proxy_object("org.bluez", path, intro)
        return obj.get_interface(name)


# ── PPoGATT link layer ────────────────────────────────────────────
# The framed/sequenced layer that rides on top of the GATT byte pipe.

# Commands (low 3 bits of the 1-byte PPoGATT header).
PPOGATT_CMD_DATA           = 0
PPOGATT_CMD_ACK            = 1
PPOGATT_CMD_RESET_REQUEST  = 2
PPOGATT_CMD_RESET_COMPLETE = 3
PPOGATT_SEQ_MOD            = 32  # the sequence is 5 bits

# Window sizes the gateway advertises in its Reset Complete. 25/25 is
# what the official app uses (`03 19 19`).
PPOGATT_RX_WINDOW = 25
PPOGATT_TX_WINDOW = 25

# Largest PPoGATT data payload per packet = negotiated ATT MTU minus the
# ATT notification header (3) and the PPoGATT header (1). A notification
# longer than the MTU is silently truncated, so this is set from the
# live MTU once connected; the conservative default never truncates for
# any MTU >= 132 (the watch negotiates far higher — 256 observed).
PPOGATT_DEFAULT_MAX_PAYLOAD = 128
# Unacked DATA packets we keep in flight during a windowed send. The
# watch's receive window is >= 13 for any MTU it negotiates, so 8 is
# always safe; the firmware updater sizes each PutBytes chunk to fit
# this window, so a chunk is a single clean round-trip.
PPOGATT_TX_INFLIGHT = 8
# A windowed send that can't make progress: the watch stopped ACK'ing.
# Surfaced as an error rather than hanging the transfer.
PPOGATT_ACK_TIMEOUT = 30.0


def ppogatt_header(command: int, sequence: int) -> int:
    """Pack a PPoGATT header byte: high 5 bits sequence (mod 32), low 3
    bits command."""
    return ((sequence % PPOGATT_SEQ_MOD) << 3) | (command & 0x07)


def parse_ppogatt_header(byte: int) -> tuple[int, int]:
    """Inverse of `ppogatt_header` → (command, sequence)."""
    return byte & 0x07, (byte >> 3) & 0x1F


class PpogattLink:
    """PPoGATT link-layer state machine over a `PpogattServer`.

    Owns the reset handshake and per-packet ACKs, reassembles the Pebble
    Protocol byte stream, and surfaces inbound messages via
    `on_message(endpoint, payload)`. Outbound Pebble Protocol messages go
    through `send_message(endpoint, payload)`.

    This is the transport only — it neither builds nor interprets Pebble
    Protocol payloads; the device plugin handles those (phone-version,
    time, health, …).
    """

    def __init__(self, server: PpogattServer,
                 on_message: Callable[[int, bytes], None] | None = None):
        self._server = server
        server.on_write = self._handle_inbound
        self.on_message = on_message
        self._tx_seq = 0          # next sequence number to send
        self._send_base = 0       # oldest sequence still awaiting an ACK
        self._max_payload = PPOGATT_DEFAULT_MAX_PAYLOAD
        self._ack_event: asyncio.Event | None = None
        self._open = False
        self._rxbuf = bytearray()

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def max_payload(self) -> int:
        """Largest PPoGATT data payload per packet (MTU-derived)."""
        return self._max_payload

    def set_max_payload(self, value: int) -> None:
        """Set the per-packet payload cap from the negotiated ATT MTU.
        Clamped to a sane floor so a bad read can't stall the link."""
        self._max_payload = max(20, int(value))

    def _handle_inbound(self, data: bytes) -> None:
        if not data:
            return
        command, sequence = parse_ppogatt_header(data[0])
        payload = data[1:]
        if command == PPOGATT_CMD_RESET_REQUEST:
            self._handle_reset_request(payload)
        elif command == PPOGATT_CMD_DATA:
            self._handle_data(sequence, payload)
        elif command == PPOGATT_CMD_RESET_COMPLETE:
            self._open = True
        elif command == PPOGATT_CMD_ACK:
            self._handle_ack(sequence)

    def _handle_reset_request(self, payload: bytes) -> None:
        version = payload[0] if payload else 0
        log.info("PPoGATT: reset request (client v%d) → reset complete "
                 "(windows %d/%d)", version,
                 PPOGATT_RX_WINDOW, PPOGATT_TX_WINDOW)
        self._tx_seq = 0
        self._send_base = 0
        self._rxbuf = bytearray()
        self._open = True
        self._server.notify(bytes([
            ppogatt_header(PPOGATT_CMD_RESET_COMPLETE, 0),
            PPOGATT_RX_WINDOW, PPOGATT_TX_WINDOW]))

    def _handle_data(self, sequence: int, payload: bytes) -> None:
        # ACK every DATA packet by echoing its sequence, then feed the
        # reassembly buffer.
        self._server.notify(bytes([ppogatt_header(PPOGATT_CMD_ACK, sequence)]))
        self._rxbuf += payload
        self._drain()

    def _drain(self) -> None:
        # Pebble Protocol packets: uint16 length + uint16 endpoint (both
        # big-endian) + payload. Reassemble across PPoGATT DATA packets.
        while len(self._rxbuf) >= 4:
            length, endpoint = struct.unpack(">HH", bytes(self._rxbuf[:4]))
            if len(self._rxbuf) < 4 + length:
                break
            payload = bytes(self._rxbuf[4:4 + length])
            del self._rxbuf[:4 + length]
            log.info("PPoGATT: ← endpoint 0x%04x (%d bytes)", endpoint, length)
            if self.on_message is not None:
                try:
                    self.on_message(endpoint, payload)
                except Exception:
                    log.exception("PPoGATT: on_message handler raised")

    def send_message(self, endpoint: int, payload: bytes) -> None:
        """Send one small Pebble Protocol message to the watch.

        Wrapped in the Pebble Protocol envelope and sent as a single
        PPoGATT DATA packet — fine for the small control/time messages,
        which fit one packet. Larger messages (the firmware transfer)
        must use `send_message_windowed`, which chunks to the MTU and
        paces on ACKs."""
        framed = struct.pack(">HH", len(payload), endpoint) + payload
        self._server.notify(
            bytes([ppogatt_header(PPOGATT_CMD_DATA, self._tx_seq)]) + framed)
        self._tx_seq = (self._tx_seq + 1) % PPOGATT_SEQ_MOD

    async def send_message_windowed(self, endpoint: int,
                                    payload: bytes) -> None:
        """Send one Pebble Protocol message, chunked across PPoGATT DATA
        packets no larger than the negotiated MTU and paced by the TX
        window — blocking until the watch's ACKs free window space. Used
        for the large firmware objects."""
        framed = struct.pack(">HH", len(payload), endpoint) + payload
        for offset in range(0, len(framed), self._max_payload):
            chunk = framed[offset:offset + self._max_payload]
            await self._await_window()
            self._server.notify(
                bytes([ppogatt_header(PPOGATT_CMD_DATA, self._tx_seq)]) + chunk)
            self._tx_seq = (self._tx_seq + 1) % PPOGATT_SEQ_MOD

    def _handle_ack(self, sequence: int) -> None:
        # ACKs are cumulative: acknowledging `sequence` acknowledges it
        # and everything before, so the window base advances past it.
        self._send_base = (sequence + 1) % PPOGATT_SEQ_MOD
        if self._ack_event is not None:
            self._ack_event.set()

    def _inflight(self) -> int:
        return (self._tx_seq - self._send_base) % PPOGATT_SEQ_MOD

    async def _await_window(self) -> None:
        if self._ack_event is None:
            self._ack_event = asyncio.Event()
        while self._inflight() >= PPOGATT_TX_INFLIGHT:
            self._ack_event.clear()
            if self._inflight() < PPOGATT_TX_INFLIGHT:
                return
            try:
                await asyncio.wait_for(
                    self._ack_event.wait(), PPOGATT_ACK_TIMEOUT)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "PPoGATT: timed out waiting for window ACKs") from exc


# ── Gateway orchestration ─────────────────────────────────────────
# Ties the whole Pebble connection together: force-LE connect (or
# reconnect to a bond), authenticated pairing, the GATT server + link,
# the connectivity subscription that makes the watch open PPoGATT, and
# the phone-version handshake. The device plugin drives this.

BLUEZ_SERVICE  = "org.bluez"
ADAPTER_IFACE  = "org.bluez.Adapter1"
DEVICE_IFACE   = "org.bluez.Device1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
AGENT_PATH     = "/land/rob/vitals/agent"

# Pebble LE Pairing Service (FED9) characteristics the gateway uses.
PEBBLE_CONNECTIVITY_CHAR = "00000001-328e-0fbb-c642-1aa6699bdada"
PEBBLE_TRIGGER_CHAR      = "00000002-328e-0fbb-c642-1aa6699bdada"
# Writing this to the trigger asks the watch to bond with the phone as
# initiator (NO_SEC_REQ bit set); the official app uses exactly this.
PEBBLE_TRIGGER_VALUE     = 0x03

# Pebble Protocol endpoints + the canned phone-version response the
# watch waits for before it will exchange anything else.
EP_PHONE_VERSION = 0x0011
EP_TIME          = 0x000B
PHONE_VERSION_RESPONSE = bytes.fromhex(
    "01ffffffff000000000000000202040402af08800000000000")

SERVICES_RESOLVED_TIMEOUT = 20.0
LINK_OPEN_TIMEOUT         = 15.0
# A bonded reconnect must not block forever: BlueZ's Device1.Connect has no
# timeout of its own, and on some adapters a connect attempt to the
# dual-mode watch wedges (or the bond is stale and the link drops before
# services resolve). Cap it, then fall back to a fresh force-LE pair.
CONNECT_TIMEOUT           = 25.0


class PebbleGateway:
    """The host (gateway) side of a Pebble connection.

    `connect()` brings up everything the watch needs to talk to us —
    force-LE connect (or a plain reconnect if already bonded),
    authenticated pairing, the PPoGATT server + link, and the
    connectivity subscription that triggers the watch to open PPoGATT —
    and answers the watch's phone-version request so the session opens.
    Then `read_char()` reads standard characteristics (battery, device
    info) and `send_message()` carries Pebble Protocol (time, …).

    Talks to BlueZ over dbus_fast directly because bleak can't host a
    GATT server or force an LE connection; needs bluetoothd's
    `Experimental = true`. See docs/pebble-ppogatt.md.
    """

    def __init__(self, address: str, adapter: str = "hci0",
                 on_message: Callable[[int, bytes], None] | None = None):
        self.address = address
        self._adapter_path = f"/org/bluez/{adapter}"
        self._device_path = (
            f"{self._adapter_path}/dev_" + address.replace(":", "_"))
        self.on_message = on_message
        self._bus = None
        self._server: PpogattServer | None = None
        self._link: PpogattLink | None = None
        self._char_paths: dict[str, str] = {}
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_link_open(self) -> bool:
        return self._link is not None and self._link.is_open

    async def connect(self) -> None:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        await self._register_agent()

        # The GATT server must exist before the watch connects so it can
        # discover it — register it up front.
        self._server = PpogattServer(self._bus)
        self._link = PpogattLink(self._server, on_message=self._on_pp_message)
        await self._server.register()

        # A device BlueZ already has bonded is reached with a plain
        # Connect; a fresh one needs the force-LE ConnectDevice. A bonded
        # reconnect that fails (times out, or the link drops before
        # services resolve — e.g. a stale bond left after the watch was
        # re-flashed) is healed by removing the bond and pairing fresh.
        paired = await self._is_paired()
        if paired:
            paired = await self._reconnect_bonded()
        else:
            log.info("Pebble: force-LE ConnectDevice to %s", self.address)
            await self._connect_device()
            await self._wait_services_resolved()

        await self._resolve_char_paths()
        await self._apply_mtu()
        self._connected = True

        if not paired:
            await self._pair()

        # Subscribing to the connectivity characteristic is what makes
        # the watch discover our server and open PPoGATT.
        await self._start_notify(PEBBLE_CONNECTIVITY_CHAR)
        await self._wait_link_open()

    async def _reconnect_bonded(self) -> bool:
        """Reconnect to a bonded watch. Returns True on success, or False
        after healing a stale bond by re-pairing fresh.

        A plain Device1.Connect has no timeout, so a wedged adapter would
        otherwise hang forever — cap it. Two failure modes are
        distinguished so a healthy bond isn't destroyed needlessly:

          * Connect itself fails/times out (watch out of range, adapter
            wedged) — keep the bond and raise, so the caller can retry.
          * Connect succeeds but the link drops before services resolve —
            the bond is stale (the watch was re-flashed); remove it and
            pair fresh."""
        log.info("Pebble: reconnecting to bonded watch %s", self.address)
        dev = await self._iface(self._device_path, DEVICE_IFACE)
        try:
            await asyncio.wait_for(dev.call_connect(), CONNECT_TIMEOUT)
        except Exception as exc:
            raise RuntimeError(
                f"could not connect to bonded watch {self.address}: {exc}"
            ) from exc
        try:
            await self._wait_services_resolved()
            return True
        except Exception:
            log.warning("Pebble: bonded link dropped before services "
                        "resolved — bond is stale, re-pairing fresh")
            await self._connect_device()  # removes the bond + force-LE connect
            await self._wait_services_resolved()
            return False

    async def disconnect(self) -> None:
        if self._bus is None:
            return
        try:
            if self._server is not None:
                await self._server.unregister()
            dev = await self._iface(self._device_path, DEVICE_IFACE)
            await dev.call_disconnect()
        except Exception:
            log.debug("Pebble: disconnect cleanup failed", exc_info=True)
        finally:
            self._bus.disconnect()
            self._bus = None
            self._server = None
            self._link = None
            self._char_paths = {}
            self._connected = False

    async def read_gatt_char(self, uuid: str) -> bytes:
        path = self._char_paths.get(uuid.lower())
        if path is None:
            raise RuntimeError(f"characteristic {uuid} not present")
        char = await self._iface(path, GATT_CHAR_IFACE)
        return bytes(await char.call_read_value({}))

    def send_message(self, endpoint: int, payload: bytes) -> None:
        if self._link is None or not self._link.is_open:
            raise RuntimeError("PPoGATT link is not open")
        self._link.send_message(endpoint, payload)

    async def send_message_async(self, endpoint: int, payload: bytes) -> None:
        """Send a (possibly large) Pebble Protocol message, chunked and
        ACK-paced. Used by the firmware updater."""
        if self._link is None or not self._link.is_open:
            raise RuntimeError("PPoGATT link is not open")
        await self._link.send_message_windowed(endpoint, payload)

    async def request(self, endpoint: int, payload: bytes,
                      reply_endpoint: int, timeout: float = 10.0) -> bytes:
        """Send one Pebble Protocol message and await the next reply on
        `reply_endpoint`. Other inbound messages keep flowing to the
        installed handler. Used for one-shot queries like WatchVersion."""
        if self._link is None or not self._link.is_open:
            raise RuntimeError("PPoGATT link is not open")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        previous = self.on_message

        def handler(ep: int, pl: bytes) -> None:
            if ep == reply_endpoint and not future.done():
                future.set_result(pl)
            elif previous is not None:
                previous(ep, pl)

        self.on_message = handler
        try:
            self._link.send_message(endpoint, payload)
            return await asyncio.wait_for(future, timeout)
        finally:
            self.on_message = previous

    @property
    def max_payload(self) -> int:
        """Largest PPoGATT data payload per packet (MTU-derived)."""
        return self._link.max_payload if self._link else \
            PPOGATT_DEFAULT_MAX_PAYLOAD

    def set_message_handler(
            self, handler: Callable[[int, bytes], None] | None) -> None:
        """Route inbound Pebble Protocol messages (other than the
        phone-version request the gateway answers itself) to `handler`.
        The firmware updater uses this to receive PutBytes / system
        replies."""
        self.on_message = handler

    # ── internals ──────────────────────────────────────────────────

    def _on_pp_message(self, endpoint: int, payload: bytes) -> None:
        # The watch asks for the phone's version before doing anything
        # else; answer it so the session opens. Everything else goes to
        # the plugin's handler.
        if endpoint == EP_PHONE_VERSION:
            log.info("Pebble: phone-version requested → responding")
            self._link.send_message(EP_PHONE_VERSION, PHONE_VERSION_RESPONSE)
        elif self.on_message is not None:
            self.on_message(endpoint, payload)

    async def _iface(self, path: str, name: str):
        intro = await self._bus.introspect(BLUEZ_SERVICE, path)
        obj = self._bus.get_proxy_object(BLUEZ_SERVICE, path, intro)
        return obj.get_interface(name)

    async def _register_agent(self) -> None:
        self._bus.export(AGENT_PATH, _agent_class()())
        manager = await self._iface("/org/bluez", AGENT_MANAGER_IFACE)
        try:
            # KeyboardDisplay → MITM + Secure Connections (numeric
            # comparison, confirmed on the watch); a NoInputNoOutput
            # agent's unauthenticated bond doesn't open PPoGATT.
            await manager.call_register_agent(AGENT_PATH, "KeyboardDisplay")
            await manager.call_request_default_agent(AGENT_PATH)
        except Exception:
            log.debug("Pebble: agent registration note", exc_info=True)

    async def _connect_device(self) -> None:
        from dbus_fast import Message, MessageType, Variant
        # Purge any stale entry, then force LE; the watch advertises in
        # bursts so retry.
        await self._remove_device()
        reply = None
        for attempt in range(5):
            reply = await self._bus.call(Message(
                destination=BLUEZ_SERVICE, path=self._adapter_path,
                interface=ADAPTER_IFACE, member="ConnectDevice",
                signature="a{sv}",
                body=[{"Address": Variant("s", self.address),
                       "AddressType": Variant("s", "public")}]))
            if reply.message_type != MessageType.ERROR:
                if reply.body:
                    self._device_path = reply.body[0]
                return
            await asyncio.sleep(3)
        raise RuntimeError(
            f"ConnectDevice failed: {reply.error_name if reply else '?'} "
            f"(is `Experimental = true` set in /etc/bluetooth/main.conf?)")

    async def _remove_device(self) -> None:
        try:
            adapter = await self._iface(self._adapter_path, ADAPTER_IFACE)
            await adapter.call_remove_device(self._device_path)
        except Exception:
            pass

    async def _is_paired(self) -> bool:
        try:
            dev = await self._iface(self._device_path, DEVICE_IFACE)
            return await dev.get_paired()
        except Exception:
            return False

    async def _pair(self) -> None:
        await self._write_char(
            PEBBLE_TRIGGER_CHAR, bytes([PEBBLE_TRIGGER_VALUE]))
        dev = await self._iface(self._device_path, DEVICE_IFACE)
        log.info("Pebble: pairing (confirm on the watch if prompted)")
        await dev.call_pair()

    async def _write_char(self, uuid: str, value: bytes) -> None:
        path = self._char_paths.get(uuid.lower())
        if path is None:
            raise RuntimeError(f"characteristic {uuid} not present")
        char = await self._iface(path, GATT_CHAR_IFACE)
        await char.call_write_value(value, {})

    async def _start_notify(self, uuid: str) -> None:
        path = self._char_paths.get(uuid.lower())
        if path is None:
            raise RuntimeError(f"characteristic {uuid} not present")
        char = await self._iface(path, GATT_CHAR_IFACE)
        await char.call_start_notify()

    async def _wait_services_resolved(self) -> None:
        dev = await self._iface(self._device_path, DEVICE_IFACE)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SERVICES_RESOLVED_TIMEOUT
        while loop.time() < deadline:
            try:
                if await dev.get_services_resolved():
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError("services not resolved (watch in range?)")

    async def _wait_link_open(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LINK_OPEN_TIMEOUT
        while loop.time() < deadline:
            if self._link.is_open:
                return
            await asyncio.sleep(0.5)
        # Not fatal: the BLE link is up (battery etc. still readable);
        # only PPoGATT features are unavailable. The watch's PPoGATT
        # engagement is occasionally flaky on reconnect.
        log.warning("Pebble: PPoGATT link did not open (watch did not "
                    "start the session)")

    async def _apply_mtu(self) -> None:
        """Read the negotiated ATT MTU and size the link's per-packet
        payload from it — a notification larger than the MTU is silently
        truncated, which would corrupt the firmware. Falls back to the
        conservative default if BlueZ doesn't expose the MTU (needs its
        experimental interfaces)."""
        mtu = await self._read_mtu()
        if mtu:
            # Reserve the ATT notification header (3) + PPoGATT header (1).
            self._link.set_max_payload(mtu - 4)
            log.info("Pebble: ATT MTU %d → PPoGATT payload %d B",
                     mtu, self._link.max_payload)
        else:
            log.info("Pebble: ATT MTU unavailable; using conservative "
                     "PPoGATT payload %d B", self._link.max_payload)

    async def _read_mtu(self) -> int | None:
        # BlueZ annotates each remote characteristic with the connection
        # MTU once services resolve; read it off whichever resolves first.
        for path in self._char_paths.values():
            try:
                char = await self._iface(path, GATT_CHAR_IFACE)
                mtu = await char.get_mtu()
                if mtu:
                    return int(mtu)
            except Exception:
                continue
        return None

    async def _resolve_char_paths(self) -> None:
        om = await self._iface("/", OBJECT_MANAGER)
        objects = await om.call_get_managed_objects()
        prefix = self._device_path + "/"
        self._char_paths = {
            ifaces[GATT_CHAR_IFACE]["UUID"].value.lower(): path
            for path, ifaces in objects.items()
            if GATT_CHAR_IFACE in ifaces and path.startswith(prefix)
        }


# dbus_fast isn't importable until runtime on a host with BlueZ, so the
# ServiceInterface subclasses (which need it at class-definition time)
# are built lazily and cached — keeping plain `import ppogatt` cheap and
# dependency-free for the rest of the app and the unit tests.
_CLASSES = None


def _object_classes():
    global _CLASSES
    if _CLASSES is not None:
        return _CLASSES

    from dbus_fast import PropertyAccess, Variant
    from dbus_fast.service import ServiceInterface, dbus_property, method

    class Application(ServiceInterface):
        """Application root: answers GetManagedObjects with our objects."""

        def __init__(self, objects):
            super().__init__(OBJECT_MANAGER)
            self.path = APP_ROOT
            self._objects = objects

        @method()
        def GetManagedObjects(self) -> "a{oa{sa{sv}}}":  # noqa: F722
            return {obj.path: obj.managed() for obj in self._objects}

    class Service(ServiceInterface):
        def __init__(self, path, uuid):
            super().__init__(GATT_SERVICE_IFACE)
            self.path = path
            self._uuid = uuid

        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s":  # noqa: F722
            return self._uuid

        @dbus_property(access=PropertyAccess.READ)
        def Primary(self) -> "b":  # noqa: F722
            return True

        def managed(self):
            return {GATT_SERVICE_IFACE: {
                "UUID": Variant("s", self._uuid),
                "Primary": Variant("b", True),
            }}

    class Characteristic(ServiceInterface):
        def __init__(self, path, uuid, service_path, flags, on_write=None,
                     initial=b""):
            super().__init__(GATT_CHAR_IFACE)
            self.path = path
            self._uuid = uuid
            self._service = service_path
            self._flags = flags
            self._on_write = on_write
            self._value = bytearray(initial)
            self._notifying = False

        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s":  # noqa: F722
            return self._uuid

        @dbus_property(access=PropertyAccess.READ)
        def Service(self) -> "o":  # noqa: F722
            return self._service

        @dbus_property(access=PropertyAccess.READ)
        def Flags(self) -> "as":  # noqa: F722
            return self._flags

        @dbus_property(access=PropertyAccess.READ)
        def Value(self) -> "ay":  # noqa: F722
            return bytes(self._value)

        @method()
        def ReadValue(self, options: "a{sv}") -> "ay":  # noqa: F722
            log.debug("PPoGATT: %s ReadValue -> %dB", self._uuid, len(self._value))
            return bytes(self._value)

        @method()
        def WriteValue(self, value: "ay", options: "a{sv}"):  # noqa: F722
            log.debug("PPoGATT: %s WriteValue %dB", self._uuid, len(value))
            if self._on_write is not None:
                self._on_write(bytes(value))

        @method()
        def StartNotify(self):
            # BlueZ tracks the subscription; we push by emitting on Value.
            log.info("PPoGATT: client subscribed to %s", self._uuid)
            self._notifying = True

        @method()
        def StopNotify(self):
            log.info("PPoGATT: client unsubscribed from %s", self._uuid)
            self._notifying = False

        def send_notification(self, data):
            # Emitting PropertiesChanged on Value is how a BlueZ GATT
            # server pushes a notification to subscribed clients.
            self._value = bytearray(data)
            self.emit_properties_changed({"Value": bytes(data)})

        def managed(self):
            return {GATT_CHAR_IFACE: {
                "UUID": Variant("s", self._uuid),
                "Service": Variant("o", self._service),
                "Flags": Variant("as", self._flags),
            }}

    _CLASSES = (Application, Service, Characteristic)
    return _CLASSES


_AGENT_CLASS = None


def _agent_class():
    """Lazily build the BlueZ pairing Agent (auto-accepts on the phone;
    the watch confirms the numeric comparison on its own face)."""
    global _AGENT_CLASS
    if _AGENT_CLASS is not None:
        return _AGENT_CLASS

    from dbus_fast.service import ServiceInterface, method

    class Agent(ServiceInterface):
        def __init__(self):
            super().__init__("org.bluez.Agent1")

        @method()
        def Release(self):
            pass

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F722,F821
            pass  # auto-accept; the watch shows + confirms the passkey

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: F722,F821
            pass

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F722,F821
            pass

        @method()
        def RequestPasskey(self, device: "o") -> "u":  # noqa: F722,F821
            return 0

        @method()
        def RequestPinCode(self, device: "o") -> "s":  # noqa: F722,F821
            return "0000"

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F722,F821
            pass

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F722,F821
            pass

        @method()
        def Cancel(self):
            pass

    _AGENT_CLASS = Agent
    return Agent
