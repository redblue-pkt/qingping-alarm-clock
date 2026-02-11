from datetime import time, datetime, timedelta
from enum import Enum

CONFIGURATION_VALIDITY_TIME = timedelta(minutes=30)


class Language(Enum):
    EN = "en"
    ZH = "zh"


class Configuration:
    """Device settings blob (0x13) parsed from the device reply.

    Payload layout (20 bytes):
      0   : 0x13
      1   : 0x02 (read response) / 0x01 (set)
      2   : volume (1-5)
      3-4 : fixed header/version (typically 0x58 0x02)
      5   : flags bitfield
      6   : tz offset in 6-minute units (magnitude)
      7   : backlight seconds (0=off)
      8   : brightness packed nibbles (day/10, night/10)
      9-12: night start/end times
      13  : tz sign (1=positive, 0=negative)
      14  : night mode enabled (0/1)
      15  : reserved (preserve)
      16-19: ringtone signature (4 bytes)
    """
    def __init__(self, config_bytes: bytes | bytearray):
        self.date = datetime.now()

        b = bytes(config_bytes)
        if len(b) < 20:
            raise ValueError(f"Configuration payload must be 20 bytes, got {len(b)}")

        self._sound_volume = b[2]
        self._hdr1 = b[3]
        self._hdr2 = b[4]

        # stored as absolute minutes; sign in _tz_plus_flag
        self._timezone_offset = b[6] * 6
        self._screen_light_time = b[7]

        brightness = self._byte_to_brightness(b[8])
        self._daytime_brightness, self._nighttime_brightness = brightness

        self._night_time_start_hour = b[9]
        self._night_time_start_minute = b[10]
        self._night_time_end_hour = b[11]
        self._night_time_end_minute = b[12]
        self._tz_plus_flag = b[13] == 1
        self._night_mode = b[14] == 1
        self._reserved = b[15]
        self._ringtone_signature = b[16:20]

        flags = b[5]
        self._language = Language.ZH if flags & (1 << 0) == 0 else Language.EN
        self._use_24h_format = flags & (1 << 1) == 0
        self._use_celsius = flags & (1 << 2) == 0
        self._alarms_on = flags & (1 << 4) == 0

    @property
    def is_expired(self):
        return self.date + CONFIGURATION_VALIDITY_TIME < datetime.now()

    @property
    def sound_volume(self):
        return self._sound_volume

    @sound_volume.setter
    def sound_volume(self, value):
        if value < 1 or value > 5:
            raise ValueError("Invalid value")
        self._sound_volume = value

    @property
    def ringtone_signature(self) -> bytes:
        """Current ringtone signature (4 bytes) reported by the device."""
        return self._ringtone_signature

    @property
    def ringtone_signature_hex(self) -> str:
        return self._ringtone_signature.hex()

    @ringtone_signature.setter
    def ringtone_signature(self, value: bytes | bytearray | str):
        if isinstance(value, str):
            cleaned = re.sub(r"[^0-9a-fA-F]", "", value)
            if len(cleaned) != 8:
                raise ValueError("Ringtone signature must be 4 bytes = 8 hex chars")
            value = bytes.fromhex(cleaned)
        bb = bytes(value)
        if len(bb) != 4:
            raise ValueError("Ringtone signature must be exactly 4 bytes")
        self._ringtone_signature = bb

    @property
    def timezone_offset(self):
        """ Timezone offset in minutes. """
        if self._tz_plus_flag:
            return self._timezone_offset
        else:
            return -self._timezone_offset

    @timezone_offset.setter
    def timezone_offset(self, value):
        if value > 720 or value < -720:
            raise ValueError("Invalid value")
        self._timezone_offset = abs(value)
        self._tz_plus_flag = value >= 0

    @property
    def screen_light_time(self):
        return self._screen_light_time

    @screen_light_time.setter
    def screen_light_time(self, value):
        if value < 1 or value > 30:
            raise ValueError("Invalid value")
        self._screen_light_time = value

    @property
    def daytime_brightness(self):
        return self._daytime_brightness

    @daytime_brightness.setter
    def daytime_brightness(self, value):
        if value < 1 or value > 100:
            raise ValueError("Invalid value")
        self._daytime_brightness = value

    @property
    def nighttime_brightness(self):
        return self._nighttime_brightness

    @nighttime_brightness.setter
    def nighttime_brightness(self, value):
        if value < 1 or value > 100:
            raise ValueError("Invalid value")
        self._nighttime_brightness = value

    @property
    def night_time_start_hour(self):
        return self._night_time_start_hour

    @night_time_start_hour.setter
    def night_time_start_hour(self, value):
        if value < 0 or value > 23:
            raise ValueError("Invalid value")
        self._night_time_start_hour = value

    @property
    def night_time_start_minute(self):
        return self._night_time_start_minute

    @night_time_start_minute.setter
    def night_time_start_minute(self, value):
        if value < 0 or value > 59:
            raise ValueError("Invalid value")
        self._night_time_start_minute = value

    @property
    def night_time_end_hour(self):
        return self._night_time_end_hour

    @night_time_end_hour.setter
    def night_time_end_hour(self, value):
        if value < 0 or value > 23:
            raise ValueError("Invalid value")
        self._night_time_end_hour = value

    @property
    def night_time_end_minute(self):
        return self._night_time_end_minute

    @night_time_end_minute.setter
    def night_time_end_minute(self, value):
        if value < 0 or value > 59:
            raise ValueError("Invalid value")
        self._night_time_end_minute = value

    @property
    def night_time_start_time(self):
        return time(hour=self._night_time_start_hour, minute=self._night_time_start_minute)

    @night_time_start_time.setter
    def night_time_start_time(self, value: time):
        self._night_time_start_hour = value.hour
        self._night_time_start_minute = value.minute

    @property
    def night_time_end_time(self):
        return time(hour=self._night_time_end_hour, minute=self._night_time_end_minute)

    @night_time_end_time.setter
    def night_time_end_time(self, value: time):
        self._night_time_end_hour = value.hour
        self._night_time_end_minute = value.minute

    @property
    def language(self):
        return self._language

    @language.setter
    def language(self, value):
        self._language = value

    @property
    def use_24h_format(self):
        return self._use_24h_format

    @use_24h_format.setter
    def use_24h_format(self, value):
        self._use_24h_format = value

    @property
    def use_celsius(self):
        return self._use_celsius

    @use_celsius.setter
    def use_celsius(self, value):
        self._use_celsius = value

    @property
    def alarms_on(self):
        return self._alarms_on

    @alarms_on.setter
    def alarms_on(self, value):
        self._alarms_on = value

    @property
    def night_mode_enabled(self):
        return self._night_mode

    @night_mode_enabled.setter
    def night_mode_enabled(self, value):
        self._night_mode = value
        if value:
            self._night_time_start_hour = 21
            self._night_time_start_minute = 0
            self._night_time_end_hour = 6
            self._night_time_end_minute = 0
        else:
            self._night_time_start_hour = 0
            self._night_time_start_minute = 0
            self._night_time_end_hour = 0
            self._night_time_end_minute = 1

    def to_bytes(self):
        byte_array = [0x13, 0x01]
        byte_array.append(self.sound_volume)
        byte_array.extend([self._hdr1, self._hdr2])

        config_byte = 0
        config_byte |= 0 if self.language == Language.ZH else (1 << 0)
        config_byte |= 0 if self.use_24h_format else (1 << 1)
        config_byte |= 0 if self.use_celsius else (1 << 2)
        config_byte |= 0 if self.alarms_on else (1 << 4)
        byte_array.append(config_byte)

        byte_array.append(self.timezone_offset // 6)
        byte_array.append(self.screen_light_time)
        byte_array.append(
            self._brightness_to_byte(self.daytime_brightness, self.nighttime_brightness)
        )

        byte_array.append(self.night_time_start_hour)
        byte_array.append(self.night_time_start_minute)

        byte_array.append(self.night_time_end_hour)
        byte_array.append(self.night_time_end_minute)
        byte_array.append(0x01 if self._tz_plus_flag else 0x00)
        byte_array.append(0x01 if self._night_mode else 0x00)

        byte_array.append(self._reserved)
        byte_array.append(self._ringtone_signature)
        bytes_result = b''.join([bytes([x]) if isinstance(x, int) else x for x in byte_array])

        if len(bytes_result) != 20:
            raise ValueError("Configuration bytes must be 20 bytes long.")

        return bytes_result

    def _byte_to_brightness(self, int_value):
        first_nibble = (int_value >> 4) & 0x0F
        second_nibble = int_value & 0x0F

        daytime_brightness = first_nibble * 10
        nighttime_brightness = second_nibble * 10

        return (daytime_brightness, nighttime_brightness)

    def _brightness_to_byte(self, daytime_brightness, nighttime_brightness):
        if not 0 <= daytime_brightness <= 100 or daytime_brightness % 10 != 0:
            raise ValueError("Daytime brightness must be between 0 and 100 and a multiple of 10.")
        if not 0 <= nighttime_brightness <= 100 or nighttime_brightness % 10 != 0:
            raise ValueError("Nighttime brightness must be between 0 and 100 and a multiple of 10.")

        first_nibble = daytime_brightness // 10
        second_nibble = nighttime_brightness // 10

        combined_byte_value = (first_nibble << 4) | second_nibble
        combined_byte = combined_byte_value.to_bytes(1, byteorder='big')

        return combined_byte
