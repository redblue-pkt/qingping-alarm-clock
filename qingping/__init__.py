"""
Public API for the Qingping CGD1 alarm clock library.

Re-export the most useful classes/constants so users can do:
  from <package> import Qingping, Configuration, Alarm, ...
"""

from .qingping import Qingping
from .configuration import Configuration, Language
from .alarm import Alarm, AlarmDay
from .eventbus import EventBus

from .exceptions import (
    QingpingError,
    NotConnectedError,
    NoConfigurationError,
    ValidationError,
)

from .events import (
    DEVICE_CONNECT,
    DEVICE_DISCONNECT,
    DEVICE_CONFIG_UPDATE,
    ALARMS_UPDATE,
)

from .ringtones import (
    RINGTONE_SIGNATURES,
    CUSTOM_SLOT_DEAD,
    CUSTOM_SLOT_BEEF,
    parse_slot_signature,
    choose_next_custom_slot,
    get_custom_slot_signature,
)

from .util import alarm_days_from_string, updates_configuration

__all__ = [
    # main client
    "Qingping",

    # configuration
    "Configuration",
    "Language",

    # alarms
    "Alarm",
    "AlarmDay",

    # eventing
    "EventBus",
    "DEVICE_CONNECT",
    "DEVICE_DISCONNECT",
    "DEVICE_CONFIG_UPDATE",
    "ALARMS_UPDATE",

    # exceptions
    "QingpingError",
    "NotConnectedError",
    "NoConfigurationError",
    "ValidationError",

    # ringtones
    "RINGTONE_SIGNATURES",
    "CUSTOM_SLOT_DEAD",
    "CUSTOM_SLOT_BEEF",
    "parse_slot_signature",
    "choose_next_custom_slot",
    "get_custom_slot_signature",

    # utils
    "alarm_days_from_string",
    "updates_configuration",
]

