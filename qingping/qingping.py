import asyncio
import logging
import time
import re
from datetime import time as dtime
from typing import Callable, Optional, Dict
from bleak import BleakClient, BleakScanner

from .configuration import Configuration, Language
from .util import updates_configuration
from .alarm import Alarm, AlarmDay
from .eventbus import EventBus
from .exceptions import NotConnectedError, ValidationError
from .events import (
    DEVICE_CONNECT,
    DEVICE_DISCONNECT,
    DEVICE_CONFIG_UPDATE,
    ALARMS_UPDATE,
)

_LOGGER = logging.getLogger(__name__)

MAIN_CHAR = "00000001-0000-1000-8000-00805f9b34fb"
CFG_WRITE_CHAR = "0000000B-0000-1000-8000-00805f9b34fb"
CFG_READ_CHAR = "0000000C-0000-1000-8000-00805f9b34fb"

AUTH_PREFIX_1 = bytes.fromhex("1101")
AUTH_PREFIX_2 = bytes.fromhex("1102")

AUDIO_INIT_PREFIX = bytes.fromhex("0810")  # 0x08 0x10
AUDIO_DATA_PREFIX = bytes.fromhex("8108")  # 0x81 0x08
AUDIO_ACK_PREFIX = bytes.fromhex("04ff")   # 0x04 0xff
AUDIO_PACKET_SIZE = 128
AUDIO_BLOCK_PACKETS = 4
AUDIO_PAD_BYTE = 0xFF

# Standalone defaults (used to come from Home Assistant integration const.py)
ALARM_SLOTS_COUNT = 16
DISCONNECT_DELAY = 5.0
CONNECTION_TIMEOUT = 30.0
RETRY_INTERVAL = 2.0
SCAN_TIMEOUT = 8.0
CONNECT_TIMEOUT = 10.0
RESPONSE_TIMEOUT = 10.0


class Qingping:
    def __init__(self, mac: str, name: str | None = None, token: str | bytes | None = None):
        """Initialize the Qingping CGD1 Alarm Clock client (standalone)."""
        self.mac = mac
        self.name = name or mac
        self.token = self._normalize_token(token)

        self.client: BleakClient | None = None
        self.configuration: Configuration | None = None
        self.alarms: list[Alarm] = []
        self.eventbus = EventBus()

        self._connect_lock = asyncio.Lock()
        self._configuration_event = asyncio.Event()
        self._alarms_event = asyncio.Event()

        # Ringtone upload helpers (does not affect existing functionality)
        self.ringtone_signature: Optional[bytes] = None
        self._ack_waiters: Dict[int, asyncio.Future] = {}
        self._cfg_notify_started = False
        self._disconnect_task: asyncio.Task | None = None
        self._alarms_by_slot: Dict[int, Alarm] = {}

    def _arm_ack(self, opcode: int) -> asyncio.Future:
        """Create/replace waiter BEFORE sending packet (prevents race)."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        old = self._ack_waiters.get(opcode)
        if old and not old.done():
            old.cancel()

        self._ack_waiters[opcode] = fut
        return fut

    @staticmethod
    def _normalize_token(token: str | bytes | None) -> bytes:
        """Return 16-byte token.

        Accepts:
        - bytes (len==16)
        - hex string (32 hex chars), separators like ':' or spaces allowed
        """
        if token is None:
            raise ValidationError("Token is required for authentication")

        if isinstance(token, (bytes, bytearray)):
            b = bytes(token)
            if len(b) != 16:
                raise ValidationError("Token must be exactly 16 bytes")
            return b

        cleaned = re.sub(r"[^0-9a-fA-F]", "", str(token))
        if len(cleaned) != 32:
            raise ValidationError("Token must be 16 bytes = 32 hex chars (e.g. 0e659b...ce6e)")
        return bytes.fromhex(cleaned)

    async def connect(self) -> bool:
        async with self._connect_lock:
            if self.client and self.client.is_connected:
                return True

            device = None
            try:
                device = await BleakScanner.find_device_by_address(self.mac, timeout=SCAN_TIMEOUT)
            except Exception as e:
                _LOGGER.debug("BLE scan failed for %s: %s", self.mac, e)

            target = device if device is not None else self.mac
            self.client = BleakClient(target, disconnected_callback=self._on_disconnect)

            _LOGGER.debug("Connecting to %s...", self.mac)
            try:
                try:
                    await self.client.connect(timeout=CONNECT_TIMEOUT)
                except TypeError:
                    await self.client.connect()
            except Exception as e:
                _LOGGER.debug("Failed to connect to %s: %s", self.mac, e)
                self.client = None
                return False

            await asyncio.sleep(2.0)  # give some time for service discovery

            _LOGGER.debug("Connected to %s, authenticating...", self.mac)

            # Step 1 auth
            await self._write_gatt_char(MAIN_CHAR, AUTH_PREFIX_1 + self.token)
            # Step 2 auth
            await self._write_gatt_char(MAIN_CHAR, AUTH_PREFIX_2 + self.token)

            self.eventbus.send(DEVICE_CONNECT, self)

            _LOGGER.debug("Starting notifications...")
            await self.client.start_notify(CFG_READ_CHAR, self._notification_handler)

            self._cfg_notify_started = True

            return True

    async def connect_if_needed(self) -> bool:
        if not self.configuration or self.configuration.is_expired:
            return await self.connect()
        return False

    async def disconnect(self) -> bool:
        if self.client and self.client.is_connected:
            _LOGGER.debug("Disconnecting from %s...", self.mac)
            await self.client.disconnect()
            return True
        return False

    async def delayed_disconnect(self):
        if not self.client or not self.client.is_connected:
            return

        try:
            await asyncio.sleep(DISCONNECT_DELAY)
            await self.disconnect()
            if self._disconnect_task:
                self._disconnect_task.cancel()
                self._disconnect_task = None
            _LOGGER.debug("Disconnected from %s", self.mac)
        except Exception as e:
            _LOGGER.debug("Failed to disconnect. Error: %s", e)

    async def get_configuration(self):
        if self.client and self.client.is_connected:
            self._configuration_event.clear()
            await self._write_config(b"\x01\x02")
            await asyncio.wait_for(self._configuration_event.wait(), timeout=RESPONSE_TIMEOUT)
        else:
            raise NotConnectedError("Not connected")

    async def set_configuration(self, configuration: Configuration):
        await self._write_config(configuration.to_bytes())
        await self.get_configuration()

    async def set_time(self, timestamp: int, timezone_offset: int | None = None):
        start_time = time.time()

        await self._ensure_connected()
        await self._ensure_configuration()

        # Account for time passed while connecting
        timestamp = int(timestamp + (time.time() - start_time))

        timestamp_bytes = self._get_timestamp_bytes(timestamp)
        await self._write_gatt_char(MAIN_CHAR, timestamp_bytes)

        if timezone_offset is not None and self.configuration and self.configuration.timezone_offset != timezone_offset:
            self.configuration.timezone_offset = timezone_offset
            await self.set_configuration(self.configuration)

    async def get_alarms(self):
        if self.client and self.client.is_connected:
            self._alarms_event.clear()
            self.alarms = []
            self._alarms_by_slot.clear()
            await self._write_config(b"\x01\x06")
            await asyncio.wait_for(self._alarms_event.wait(), timeout=RESPONSE_TIMEOUT)
        else:
            raise NotConnectedError("Not connected")

    async def set_alarm(
        self,
        slot: int,
        is_enabled: bool | None,
        time: dtime | None,
        days: set[AlarmDay] | None,
        snooze: bool | None,
    ) -> bool:
        await self._ensure_alarms()
        await self._ensure_connected()

        if 0 <= slot < ALARM_SLOTS_COUNT:
            alarm: Alarm = self.alarms[slot]
            if is_enabled is not None:
                alarm.is_enabled = is_enabled
            if time is not None:
                alarm.time = time
            if days is not None:
                alarm.days = days
            if snooze is not None:
                alarm.snooze = snooze
            if not alarm.is_configured:
                raise ValidationError("Alarm not configured.")

            await self._write_config(alarm.to_bytes())
            await self.get_alarms()
            return True

        return False

    async def delete_alarm(self, slot: int) -> bool:
        await self._ensure_alarms()
        await self._ensure_connected()

        if 0 <= slot < ALARM_SLOTS_COUNT:
            alarm: Alarm = self.alarms[slot]
            alarm.deactivate()

            await self._write_config(alarm.to_bytes())
            await self.get_alarms()
            return True

        return False

    @updates_configuration
    async def enable_alarms(self, is_enabled: bool):
        if not self.configuration:
            raise ValidationError("No configuration loaded")
        self.configuration.alarms_on = is_enabled
        await self._write_config(self.configuration.to_bytes())

    async def _wait_for_ack(self, cmd_id: int, timeout: float = RESPONSE_TIMEOUT) -> int:
        """Wait for a 0x04ff ACK for given command id (0x10 init, 0x08 data block)."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        prev = self._ack_waiters.pop(cmd_id, None)
        if prev is not None and not prev.done():
            prev.cancel()

        self._ack_waiters[cmd_id] = fut
        return await asyncio.wait_for(fut, timeout=timeout)

    async def upload_ringtone(self, pcm_data: bytes, signature: bytes, on_progress=None) -> bool:
        await self._ensure_connected()

        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
            self._disconnect_task = None

        if self.client and self.client.is_connected and not self._cfg_notify_started:
            await self.client.start_notify(CFG_READ_CHAR, self._notification_handler)
            self._cfg_notify_started = True
            await asyncio.sleep(0.3)

        if len(signature) != 4:
            raise ValidationError("Ringtone signature must be 4 bytes")

        size = len(pcm_data)
        init_payload = bytes([
            0x08, 0x10,
            (size >> 0) & 0xFF,
            (size >> 8) & 0xFF,
            (size >> 16) & 0xFF,
            signature[0], signature[1], signature[2], signature[3],
        ])

        _LOGGER.info("Sending Init: %s", init_payload.hex())

        init_fut = self._arm_ack(0x10)

        await self.client.write_gatt_char(CFG_WRITE_CHAR, init_payload, response=True)

        await asyncio.wait_for(init_fut, timeout=2.0)

        packet_size = 128
        packets_per_block = 4
        block_size = packet_size * packets_per_block

        sent = 0
        total_blocks = (size + block_size - 1) // block_size

        for block_num in range(total_blocks):
            for pkt_idx in range(packets_per_block):
                pkt_off = block_num * block_size + pkt_idx * packet_size

                if pkt_off >= size:
                    audio_len = 0
                    chunk = bytes([AUDIO_PAD_BYTE]) * packet_size
                else:
                    remaining = size - pkt_off
                    audio_len = min(packet_size, remaining)
                    chunk = pcm_data[pkt_off:pkt_off + audio_len]
                    if len(chunk) < packet_size:
                        chunk = chunk + bytes([AUDIO_PAD_BYTE]) * (packet_size - len(chunk))

                packet = AUDIO_DATA_PREFIX + chunk

                is_last_in_block = (pkt_idx == packets_per_block - 1)

                if is_last_in_block:
                    blk_fut = self._arm_ack(0x08)
                    await self.client.write_gatt_char(CFG_WRITE_CHAR, packet, response=True)

                    await asyncio.sleep(DISCONNECT_DELAY)
                else:
                    await self.client.write_gatt_char(CFG_WRITE_CHAR, packet, response=True)
                    await asyncio.sleep(0.02)

                sent += audio_len

            if on_progress:
                on_progress(min(1.0, sent / size))

            _LOGGER.debug("Block %d/%d complete (offset=%d/%d)", block_num + 1, total_blocks, sent, size)

        return True

    @updates_configuration
    async def set_sound_volume(self, volume: int):
        if not self.configuration:
            raise ValidationError("No configuration loaded")
        self.configuration.sound_volume = volume
        await self._write_config(self.configuration.to_bytes())
        await self._write_config(b"\x01\x04")

    @updates_configuration
    async def set_screen_light_time(self, _time: int):
        if not self.configuration:
            raise ValidationError("No configuration loaded")
        self.configuration.screen_light_time = _time
        await self._write_config(self.configuration.to_bytes())

    @updates_configuration
    async def set_daytime_brightness(self, brightness: int):
        if not self.configuration:
            raise ValidationError("No configuration loaded")
        self.configuration.daytime_brightness = brightness
        await self._write_config(self.configuration.to_bytes())
        await self._write_config(bytes([0x02, 0x03, brightness // 10]))

    @updates_configuration
    async def set_nighttime_brightness(self, brightness: int):
        if not self.configuration:
            raise ValidationError("No configuration loaded")
        self.configuration.nighttime_brightness = brightness
        await self._write_config(self.configuration.to_bytes())
        await self._write_config(bytes([0x02, 0x03, brightness // 10]))

    @updates_configuration
    async def set_nighttime_start_time(self, _time: dtime):
        if not self.configuration:
            raise ValidationError("No configuration loaded")
        self.configuration.night_time_start_time = _time
        await self._write_config(self.configuration.to_bytes())

    @updates_configuration
    async def set_nighttime_end_time(self, _time: dtime):
        if not self.configuration:
            raise ValidationError("No configuration loaded")
        self.configuration.night_time_end_time = _time
        await self._write_config(self.configuration.to_bytes())

    @updates_configuration
    async def set_night_mode(self, is_night_mode: bool):
        if not self.configuration:
            raise ValidationError("No configuration loaded")
        self.configuration.night_mode_enabled = is_night_mode
        await self._write_config(self.configuration.to_bytes())

    @updates_configuration
    async def set_language(self, language: Language):
        if not self.configuration:
            raise ValidationError("No configuration loaded")
        self.configuration.language = language
        await self._write_config(self.configuration.to_bytes())

    @updates_configuration
    async def set_24h_time_format(self, is_24h: bool):
        if not self.configuration:
            raise ValidationError("No configuration loaded")
        self.configuration.use_24h_format = is_24h
        await self._write_config(self.configuration.to_bytes())

    @updates_configuration
    async def set_uses_celsius(self, is_celsius: bool):
        if not self.configuration:
            raise ValidationError("No configuration loaded")
        self.configuration.use_celsius = is_celsius
        await self._write_config(self.configuration.to_bytes())

    async def _ensure_connected(self):
        async def wait_for_connected():
            while not self.client or not self.client.is_connected:
                success = await self.connect()
                if success:
                    _LOGGER.info("Successfully connected to the Bluetooth device.")
                    return
                _LOGGER.error("Failed to connect. Retrying in %s seconds...", RETRY_INTERVAL)
                await asyncio.sleep(RETRY_INTERVAL)

        try:
            await asyncio.wait_for(wait_for_connected(), timeout=CONNECTION_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.error("Connection timeout.")
            raise NotConnectedError("Connection timeout")

    async def _ensure_configuration(self):
        if not self.configuration or self.configuration.is_expired:
            await self._ensure_connected()
            await self.get_configuration()

    async def _ensure_alarms(self):
        if not self.alarms or len(self.alarms) < ALARM_SLOTS_COUNT:
            await self._ensure_connected()
            await self.get_alarms()

    async def _write_config(self, data: bytes):
        if self.client and self.client.is_connected:
            await self._write_gatt_char(CFG_WRITE_CHAR, data)

            loop = asyncio.get_running_loop()
            if self._disconnect_task is not None:
                self._disconnect_task.cancel()
            self._disconnect_task = loop.create_task(self.delayed_disconnect())
        else:
            raise NotConnectedError("Not connected")

    async def _write_gatt_char(self, uuid: str, data: bytes):
        if self.client and self.client.is_connected:
            _LOGGER.debug(">> %s: %s", uuid, data.hex())
            await self.client.write_gatt_char(uuid, data)
        else:
            raise NotConnectedError("Not connected")

    def _get_timestamp_bytes(self, timestamp: int):
        timestamp_bytes = [0] * 6
        timestamp_bytes[0] = 0x05
        timestamp_bytes[1] = 0x09
        timestamp_bytes[2] = (timestamp >> 0) & 0xFF
        timestamp_bytes[3] = (timestamp >> 8) & 0xFF
        timestamp_bytes[4] = (timestamp >> 16) & 0xFF
        timestamp_bytes[5] = (timestamp >> 24) & 0xFF
        return bytes(timestamp_bytes)

    def _notification_handler(self, sender, data):
        """Bleak notification callback (must be sync)."""
        payload = bytes(data)
        _LOGGER.debug("<< %s", payload.hex())

        if payload.startswith(AUDIO_ACK_PREFIX) and len(payload) >= 3:
            opcode = payload[2]  # 0x10 init ack, 0x08 block ack
            fut = self._ack_waiters.pop(opcode, None)
            if fut and not fut.done():
                fut.set_result(payload)
            _LOGGER.debug("AUDIO ACK opcode=0x%02x payload=%s", opcode, payload.hex())
            return

        # Audio ACK frames: 04 ff [cmd] [status]
        if payload.startswith(AUDIO_ACK_PREFIX) and len(payload) >= 4:
            cmd_id = payload[2]
            status = payload[3]
            fut = self._ack_waiters.pop(cmd_id, None)
            if fut is not None and not fut.done():
                fut.set_result(status)
            return

        # Configuration packet
        if payload.startswith(b"\x13\x02"):
            _LOGGER.debug("Got configuration bytes: %s", payload.hex())
            self.configuration = Configuration(payload)
            self.ringtone_signature = self.configuration.ringtone_signature
            self._configuration_event.set()
            self.eventbus.send(DEVICE_CONFIG_UPDATE, self.configuration)
            return

        # Alarms packet:
        #   11 06 [Base Index] [Entry0 (5B)] [Entry1 (5B)] ...
        # Each entry: [Enabled] [HH] [MM] [DaysBitmask] [Snooze]
        # Empty slot: FF FF FF FF FF
        if payload.startswith(b"\x11\x06") and len(payload) >= 3 + 5:
            _LOGGER.debug("Got alarms bytes: %s", payload.hex())
            base_index = payload[2]

            # New snapshot begins at base 0
            if base_index == 0:
                self._alarms_by_slot.clear()

            body = payload[3:]
            entry_count = len(body) // 5
            for i in range(entry_count):
                slot = base_index + i
                if slot < 0 or slot >= ALARM_SLOTS_COUNT:
                    continue
                entry = body[i * 5 : (i + 1) * 5]
                self._alarms_by_slot[slot] = Alarm(slot, entry)

            # When we have all slots, expose as ordered list and unblock waiters
            if len(self._alarms_by_slot) >= ALARM_SLOTS_COUNT:
                self.alarms = [self._alarms_by_slot[i] for i in range(ALARM_SLOTS_COUNT)]
                self._alarms_event.set()
                self.eventbus.send(ALARMS_UPDATE, self.alarms)
            else:
                # Partial snapshot - still notify listeners with what we have
                partial = [self._alarms_by_slot[i] for i in sorted(self._alarms_by_slot)]
                self.eventbus.send(ALARMS_UPDATE, partial)

    def _on_disconnect(self, client: BleakClient):
        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
            self._disconnect_task = None

        self.client = None
        self.eventbus.send(DEVICE_DISCONNECT, self)
