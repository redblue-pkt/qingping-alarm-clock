#!/usr/bin/env python3
"""
cgd1.py — one-file, not user-friendly CLI for Qingping CGD1 (BLE)

NO subcommands. Everything is controlled with --options (as requested).

Credentials:
  - If you haven't saved config yet, you MUST pass --address and --token.
  - After you save config with --set-config, you can omit them.

Consistent output:
  INFO: <action>: OK - <details>
  ERROR: <action>: FAILED - <reason>

Debug:
  --debug prints extra diagnostics + full traceback on error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import traceback
import wave
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any


# -------------------------- defaults / config --------------------------

DEFAULT_CONFIG_PATH = "~/.config/qingping-cgd1/config.json"


@dataclass(frozen=True)
class StoredConfig:
    address: str
    token_hex: str


def _expand_config_path(path_str: str | None) -> Path:
    raw = path_str or DEFAULT_CONFIG_PATH
    return Path(raw).expanduser().resolve()


def _normalize_token_hex(token_str: str) -> str:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", token_str or "")
    if len(cleaned) != 32:
        raise ValueError("token must be 16 bytes = 32 hex chars (separators allowed)")
    return cleaned.lower()


def _read_config(path: Path) -> StoredConfig | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    address = str(data.get("address", "")).strip()
    token_hex = str(data.get("token", "")).strip()
    if not address or not token_hex:
        return None
    return StoredConfig(address=address, token_hex=_normalize_token_hex(token_hex))


def _write_config(path: Path, cfg: StoredConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"address": cfg.address, "token": cfg.token_hex}, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _resolve_creds(args, config_path: Path) -> StoredConfig:
    addr = (args.address or "").strip()
    tok = (args.token or "").strip()

    # If user explicitly provided both, use them.
    if addr and tok:
        return StoredConfig(address=addr, token_hex=_normalize_token_hex(tok))

    # Otherwise try config file.
    saved = _read_config(config_path)
    if saved:
        return saved

    # No config and missing creds -> hard error.
    raise RuntimeError(
        f"Missing credentials. Provide --address and --token, or first run:\n"
        f"  {Path(sys.argv[0]).name} --set-config --address <MAC> --token <HEX32>\n"
        f"(config path: {config_path})"
    )


# -------------------------- reporting / logging --------------------------

class Reporter:
    def __init__(self, debug: bool):
        self.debug_enabled = debug

    def info(self, action: str, details: str | None = None) -> None:
        msg = f"INFO: {action}: OK"
        if details:
            msg += f" - {details}"
        print(msg)

    def error(self, action: str, reason: str | None = None) -> None:
        msg = f"ERROR: {action}: FAILED"
        if reason:
            msg += f" - {reason}"
        print(msg, file=sys.stderr)

    def debug(self, msg: str) -> None:
        if self.debug_enabled:
            print(f"DEBUG: {msg}", file=sys.stderr)


def _setup_logging(debug: bool) -> Reporter:
    reporter = Reporter(debug=debug)
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    # Keep bleak quiet unless debug
    logging.getLogger("bleak").setLevel(logging.DEBUG if debug else logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.DEBUG if debug else logging.WARNING)

    return reporter


# -------------------------- import library (folder: qingping/) --------------------------

def _import_lib() -> dict[str, Any]:
    """
    Imports from ./qingping/*.py.
    The script may be launched from any directory, so we ensure repo root is on sys.path.
    """
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from qingping.qingping import Qingping
    from qingping.configuration import Language
    from qingping.alarm import AlarmDay
    import qingping.ringtones as ringtones

    return {
        "Qingping": Qingping,
        "Language": Language,
        "AlarmDay": AlarmDay,
        "ringtones": ringtones,
    }


# -------------------------- helpers --------------------------

def _err_reason(e: Exception) -> str:
    s = str(e).strip()
    return s if s else repr(e)


def _parse_hhmm(value: str) -> dtime:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not m:
        raise ValueError('time must be "HH:MM" (e.g. 07:30)')
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("invalid HH:MM range")
    return dtime(hh, mm)


def _parse_onoff(value: str) -> bool:
    v = value.strip().lower()
    if v in ("on", "1", "true", "yes", "enable", "enabled"):
        return True
    if v in ("off", "0", "false", "no", "disable", "disabled"):
        return False
    raise ValueError('expected "on" or "off"')


def _parse_tz(v: str) -> int:
    """
    Device timezone offset is in minutes, must be multiple of 6, range ±720.
    Accept:
      +HH:MM / -HH:MM
      minutes (e.g. -60)
    """
    t = v.strip()
    if ":" in t:
        m = re.fullmatch(r"([+-])(\d{1,2}):(\d{2})", t)
        if not m:
            raise ValueError('tz must be +HH:MM / -HH:MM or minutes (e.g. -60)')
        sign = 1 if m.group(1) == "+" else -1
        hh = int(m.group(2))
        mm = int(m.group(3))
        minutes = sign * (hh * 60 + mm)
    else:
        minutes = int(t)

    if minutes < -720 or minutes > 720:
        raise ValueError("tz out of range (±12:00)")
    if minutes % 6 != 0:
        raise ValueError("tz must be multiple of 6 minutes (device limitation)")
    return minutes


def _parse_time_arg(v: str | None) -> tuple[int, int, str]:
    """
    If v is None -> system time.
    If v is digits -> epoch seconds.
    Else expects "YYYY-MM-DD HH:MM" in local timezone.
    Returns: (timestamp, tz_offset_minutes, display_string)
    """
    if not v or v == "__SYSTEM__":
        dt = datetime.now().astimezone()
        ts = int(dt.timestamp())
        off = dt.utcoffset()
        off_min = int(off.total_seconds() // 60) if off else 0
        return ts, off_min, dt.isoformat(timespec="minutes")

    s = v.strip()
    if re.fullmatch(r"\d{9,12}", s):
        ts = int(s)
        dt = datetime.fromtimestamp(ts).astimezone()
        off = dt.utcoffset()
        off_min = int(off.total_seconds() // 60) if off else 0
        return ts, off_min, dt.isoformat(timespec="minutes")

    dt_naive = datetime.strptime(s, "%Y-%m-%d %H:%M")
    dt = dt_naive.replace(tzinfo=datetime.now().astimezone().tzinfo)
    ts = int(dt.timestamp())
    off = dt.utcoffset()
    off_min = int(off.total_seconds() // 60) if off else 0
    return ts, off_min, dt.isoformat(timespec="minutes")


def _read_pcm(path: Path) -> bytes:
    """
    Read PCM bytes.
      - .raw: returned as-is
      - .wav: validates 8kHz, u8, mono, then returns raw frames
    """
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as w:
            nch = w.getnchannels()
            sampw = w.getsampwidth()
            rate = w.getframerate()
            if nch != 1:
                raise ValueError(f"WAV must be mono (1ch), got {nch}")
            if sampw != 1:
                raise ValueError(f"WAV must be 8-bit unsigned (sampwidth=1), got {sampw}")
            if rate != 8000:
                raise ValueError(f"WAV must be 8000 Hz, got {rate}")
            return w.readframes(w.getnframes())
    return path.read_bytes()


DAY_ENUM_BY_KEY = {
    "mon": "MONDAY",
    "tue": "TUESDAY",
    "wed": "WEDNESDAY",
    "thu": "THURSDAY",
    "fri": "FRIDAY",
    "sat": "SATURDAY",
    "sun": "SUNDAY",
}


def _days_set_from_spec(spec: str, AlarmDayEnum):
    s = spec.strip().lower().replace(" ", "")
    if s in ("once", "0", "none", ""):
        return set()

    if s == "weekdays":
        keys = ["mon", "tue", "wed", "thu", "fri"]
    elif s == "weekend":
        keys = ["sat", "sun"]
    elif s == "all":
        keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    else:
        keys = [k for k in s.split(",") if k]

    out = set()
    for k in keys:
        if k not in DAY_ENUM_BY_KEY:
            raise ValueError("invalid days spec (use once|weekdays|weekend|all|mon,tue,...)")
        out.add(getattr(AlarmDayEnum, DAY_ENUM_BY_KEY[k]))
    return out


def _days_pretty(days) -> str:
    if days is None:
        return "?"
    if len(days) == 0:
        return "once"
    short = {
        "MONDAY": "Mon", "TUESDAY": "Tue", "WEDNESDAY": "Wed", "THURSDAY": "Thu",
        "FRIDAY": "Fri", "SATURDAY": "Sat", "SUNDAY": "Sun",
    }
    parts = []
    for d in sorted(list(days), key=lambda x: x.value):
        name = getattr(d, "name", str(d))
        parts.append(short.get(name, name[:3]))
    return " ".join(parts)


def _parse_ringtone_signature(v: str, ringtones_mod) -> bytes:
    """
    Accept:
      - built-in names from ringtones.py:RINGTONE_SIGNATURES (e.g. 'beep', 'digital_ringtone')
      - custom slot names: 'dead', 'beef' (via parse_slot_signature)
      - raw 8-hex: fdc366a5
    """
    s = (v or "").strip().lower()
    if not s:
        raise ValueError("--ringtone requires a value (name/hex/dead/beef)")

    sigs = getattr(ringtones_mod, "RINGTONE_SIGNATURES", {}) or {}
    s2 = s.replace("-", "_").replace(" ", "_")
    if s in sigs:
        return bytes(sigs[s])
    if s2 in sigs:
        return bytes(sigs[s2])

    if hasattr(ringtones_mod, "parse_slot_signature"):
        try:
            return bytes(ringtones_mod.parse_slot_signature(s))
        except Exception:
            pass

    cleaned = re.sub(r"[^0-9a-f]", "", s)
    if len(cleaned) != 8:
        raise ValueError("ringtone must be a known name, 'dead'/'beef', or 8 hex chars (4 bytes)")
    return bytes.fromhex(cleaned)


# -------------------------- BLE wrapper --------------------------

async def _with_device(QingpingCls, mac: str, token_hex: str, reporter: Reporter, fn):
    dev = QingpingCls(mac, token=token_hex)
    ok = await dev.connect()
    if not ok:
        raise RuntimeError(f"failed to connect/authenticate to {mac}")
    try:
        return await fn(dev)
    finally:
        try:
            await dev.disconnect()
        except Exception as e:
            reporter.debug(f"disconnect error: {e!r}")


# -------------------------- actions --------------------------

async def do_set_time(args, creds: StoredConfig, lib: dict[str, Any], reporter: Reporter) -> int:
    action = "time update"
    ts, sys_tz_min, display = _parse_time_arg(args.set_time)

    tz_to_set = sys_tz_min
    if args.tz is not None:
        tz_to_set = _parse_tz(args.tz)
    if args.no_tz:
        tz_to_set = None

    Qingping = lib["Qingping"]

    async def _op(dev):
        await dev.set_time(ts, timezone_offset=tz_to_set)

    await _with_device(Qingping, creds.address, creds.token_hex, reporter, _op)

    tz_msg = f"tz={tz_to_set} min" if tz_to_set is not None else "tz=unchanged"
    reporter.info(action, f"{display} ({tz_msg})")
    return 0


async def do_get_settings(args, creds: StoredConfig, lib: dict[str, Any], reporter: Reporter) -> int:
    action = "settings get"
    Qingping = lib["Qingping"]
    ringtones_mod = lib["ringtones"]

    async def _op(dev):
        await dev.get_configuration()
        cfg = getattr(dev, "configuration", None)
        if not cfg:
            raise RuntimeError("no configuration received")

        # compact, readable
        lang = getattr(cfg, "language", None)
        lang_s = getattr(lang, "value", str(lang)) if lang is not None else "?"
        tf = "24h" if getattr(cfg, "use_24h_format", False) else "12h"
        tu = "C" if getattr(cfg, "use_celsius", True) else "F"
        vol = getattr(cfg, "sound_volume", "?")
        tz = int(getattr(cfg, "timezone_offset", 0))
        bl = getattr(cfg, "screen_light_time", "?")
        db = getattr(cfg, "daytime_brightness", "?")
        nb = getattr(cfg, "nighttime_brightness", "?")
        ns = getattr(cfg, "night_time_start_time", None)
        ne = getattr(cfg, "night_time_end_time", None)
        nm = getattr(cfg, "night_mode_enabled", None)
        alarms = getattr(cfg, "alarms_on", None)
        sig_hex = getattr(cfg, "ringtone_signature_hex", "????????")

        # try resolve ringtone name (best effort)
        sig_bytes = getattr(cfg, "ringtone_signature", None)
        rt_name = "unknown"
        try:
            if isinstance(sig_bytes, (bytes, bytearray)) and len(sig_bytes) == 4:
                for k, v in getattr(ringtones_mod, "RINGTONE_SIGNATURES", {}).items():
                    if bytes(v) == bytes(sig_bytes):
                        rt_name = k
                        break
                if rt_name == "unknown":
                    if hasattr(ringtones_mod, "CUSTOM_SLOT_DEAD") and bytes(sig_bytes) == bytes(getattr(ringtones_mod, "CUSTOM_SLOT_DEAD")):
                        rt_name = "custom_dead"
                    if hasattr(ringtones_mod, "CUSTOM_SLOT_BEEF") and bytes(sig_bytes) == bytes(getattr(ringtones_mod, "CUSTOM_SLOT_BEEF")):
                        rt_name = "custom_beef"
        except Exception:
            rt_name = "unknown"

        ns_s = ns.strftime("%H:%M") if ns else "??:??"
        ne_s = ne.strftime("%H:%M") if ne else "??:??"
        nm_s = "on" if nm else "off"
        alarms_s = "on" if alarms else "off"

        print("Device Settings")
        print("-" * 60)
        print(f"Volume           : {vol} (1-5)")
        print(f"Language         : {lang_s}")
        print(f"Time format      : {tf}")
        print(f"Temp unit        : {tu}")
        print(f"Timezone         : {tz:+d} min")
        print(f"Backlight        : {bl} s (0=off)")
        print(f"Brightness day   : {db}")
        print(f"Brightness night : {nb}")
        print(f"Night mode       : {nm_s}")
        print(f"Night start/end  : {ns_s} - {ne_s}")
        print(f"Master alarms    : {alarms_s}")
        print(f"Ringtone         : {rt_name} ({sig_hex})")
        print("-" * 60)

    await _with_device(Qingping, creds.address, creds.token_hex, reporter, _op)
    reporter.info(action)
    return 0


async def do_set_settings(args, creds: StoredConfig, lib: dict[str, Any], reporter: Reporter) -> int:
    action = "settings update"
    Qingping = lib["Qingping"]
    Language = lib["Language"]
    ringtones_mod = lib["ringtones"]

    # collect changes (for INFO line)
    changes: list[str] = []

    async def _op(dev):
        nonlocal changes
        await dev.get_configuration()
        cfg = getattr(dev, "configuration", None)
        if not cfg:
            raise RuntimeError("no configuration received")

        changed_any = False

        if args.volume is not None:
            if not (1 <= args.volume <= 5):
                raise ValueError("volume must be 1..5")
            cfg.sound_volume = args.volume
            changes.append(f"volume={args.volume}")
            changed_any = True

        if args.lang is not None:
            cfg.language = Language.EN if args.lang == "en" else Language.ZH
            changes.append(f"lang={args.lang}")
            changed_any = True

        if args.timefmt is not None:
            cfg.use_24h_format = (args.timefmt == "24")
            changes.append(f"timefmt={args.timefmt}")
            changed_any = True

        if args.temp is not None:
            cfg.use_celsius = (args.temp == "c")
            changes.append(f"temp={args.temp}")
            changed_any = True

        if args.master_alarms is not None:
            cfg.alarms_on = _parse_onoff(args.master_alarms)
            changes.append(f"master_alarms={args.master_alarms}")
            changed_any = True

        if args.backlight is not None:
            # library setter rejects 0, but device supports 0=off; use internal field for 0
            if not (0 <= args.backlight <= 30):
                raise ValueError("backlight must be 0..30 seconds (0=off)")
            if args.backlight == 0:
                setattr(cfg, "_screen_light_time", 0)
            else:
                cfg.screen_light_time = args.backlight
            changes.append(f"backlight={args.backlight}")
            changed_any = True

        if args.day_bright is not None:
            if args.day_bright < 0 or args.day_bright > 100 or args.day_bright % 10 != 0:
                raise ValueError("day brightness must be 0..100 step 10")
            cfg.daytime_brightness = args.day_bright
            changes.append(f"day_bright={args.day_bright}")
            changed_any = True

        if args.night_bright is not None:
            if args.night_bright < 0 or args.night_bright > 100 or args.night_bright % 10 != 0:
                raise ValueError("night brightness must be 0..100 step 10")
            cfg.nighttime_brightness = args.night_bright
            changes.append(f"night_bright={args.night_bright}")
            changed_any = True

        if args.night_start is not None:
            cfg.night_time_start_time = _parse_hhmm(args.night_start)
            changes.append(f"night_start={args.night_start}")
            changed_any = True

        if args.night_end is not None:
            cfg.night_time_end_time = _parse_hhmm(args.night_end)
            changes.append(f"night_end={args.night_end}")
            changed_any = True

        if args.night_mode is not None:
            nm = _parse_onoff(args.night_mode)
            cfg.night_mode_enabled = nm
            changes.append(f"night_mode={args.night_mode}")
            changed_any = True
            # workaround (as in your project script) when turning OFF without times
            if nm is False and args.night_start is None and args.night_end is None:
                cfg.night_time_start_time = dtime(0, 0)
                cfg.night_time_end_time = dtime(0, 1)

        if args.ringtone is not None:
            cfg.ringtone_signature = _parse_ringtone_signature(args.ringtone, ringtones_mod)
            changes.append(f"ringtone={args.ringtone}")
            changed_any = True

        if not changed_any:
            raise ValueError("no settings provided (use e.g. --volume/--lang/--backlight/...)")

        await dev.set_configuration(cfg)

    await _with_device(Qingping, creds.address, creds.token_hex, reporter, _op)

    reporter.info(action, ", ".join(changes) if changes else None)
    return 0


async def do_preview_brightness(args, creds: StoredConfig, lib: dict[str, Any], reporter: Reporter) -> int:
    action = "brightness preview"
    Qingping = lib["Qingping"]

    val = args.preview_brightness
    if val is None:
        raise ValueError("--preview-brightness requires a value")
    if val < 0 or val > 100 or val % 10 != 0:
        raise ValueError("brightness must be 0..100 and multiple of 10")

    async def _op(dev):
        payload = bytes([0x02, 0x03, val // 10])
        await dev._write_config(payload)

    await _with_device(Qingping, creds.address, creds.token_hex, reporter, _op)
    reporter.info(action, f"value={val}")
    return 0


async def do_preview_ringtone(args, creds: StoredConfig, lib: dict[str, Any], reporter: Reporter) -> int:
    action = "ringtone preview"
    Qingping = lib["Qingping"]

    if args.preview_volume is not None:
        if not (1 <= args.preview_volume <= 5):
            raise ValueError("preview volume must be 1..5")
        payload = bytes([0x02, 0x04, args.preview_volume])
        details = f"volume={args.preview_volume}"
    else:
        payload = b"\x01\x04"
        details = None

    async def _op(dev):
        await dev._write_config(payload)

    await _with_device(Qingping, creds.address, creds.token_hex, reporter, _op)
    reporter.info(action, details)
    return 0


async def do_get_alarms(args, creds: StoredConfig, lib: dict[str, Any], reporter: Reporter) -> int:
    action = "alarms get"
    Qingping = lib["Qingping"]

    async def _op(dev):
        await dev.get_alarms()
        alarms = getattr(dev, "alarms", []) or []

        # print table
        print("Alarms")
        print("-" * 78)
        print(f"{'Slot':>4}  {'State':<5}  {'Time':<5}  {'Repeat':<27}  {'Snooze':<6}")
        print("-" * 78)

        configured = 0
        enabled = 0
        empty = 0

        for a in alarms:
            slot = getattr(a, "slot", None)
            if not getattr(a, "is_configured", False):
                empty += 1
                print(f"{slot:>4}  {'EMPTY':<5}  {'--:--':<5}  {'-':<27}  {'-':<6}")
                continue

            configured += 1
            st = "ON" if a.is_enabled else "OFF"
            if a.is_enabled:
                enabled += 1
            t = a.time
            t_str = t.strftime("%H:%M") if t else "--:--"
            rep = _days_pretty(getattr(a, "days", None))
            snooze = getattr(a, "snooze", None)
            snooze_str = "on" if snooze else "off"
            print(f"{slot:>4}  {st:<5}  {t_str:<5}  {rep:<27}  {snooze_str:<6}")

        print("-" * 78)
        print(f"Configured: {configured}  Enabled: {enabled}  Empty: {empty}")

    await _with_device(Qingping, creds.address, creds.token_hex, reporter, _op)
    reporter.info(action)
    return 0


async def do_set_alarm(args, creds: StoredConfig, lib: dict[str, Any], reporter: Reporter) -> int:
    action = "alarm update"
    Qingping = lib["Qingping"]
    AlarmDayEnum = lib["AlarmDay"]

    if args.alarm_slot is None:
        raise ValueError("--alarm-slot is required with --set-alarm")

    slot_raw = str(args.alarm_slot).strip()
    if slot_raw.lower() == "all":
        raise ValueError('--alarm-slot "all" is only valid with --delete-alarm')
    try:
        slot = int(slot_raw)
    except Exception:
        raise ValueError("--alarm-slot must be an integer (e.g. 0)")

    enable_val = None
    if args.alarm_enable:
        enable_val = True
    elif args.alarm_disable:
        enable_val = False

    time_val = _parse_hhmm(args.alarm_time) if args.alarm_time else None
    snooze_val = _parse_onoff(args.alarm_snooze) if args.alarm_snooze is not None else None
    days_val = _days_set_from_spec(args.alarm_days, AlarmDayEnum) if args.alarm_days else None

    async def _op(dev):
        ok = await dev.set_alarm(
            slot=slot,
            is_enabled=enable_val,
            time=time_val,
            days=days_val,
            snooze=snooze_val,
        )
        if not ok:
            raise RuntimeError("set_alarm returned False")

    await _with_device(Qingping, creds.address, creds.token_hex, reporter, _op)

    details = [f"slot={slot}"]
    if enable_val is not None:
        details.append(f"enabled={'on' if enable_val else 'off'}")
    if time_val is not None:
        details.append(f"time={time_val.strftime('%H:%M')}")
    if days_val is not None:
        details.append(f"days={_days_pretty(days_val)}")
    if snooze_val is not None:
        details.append(f"snooze={'on' if snooze_val else 'off'}")

    reporter.info(action, ", ".join(details))
    return 0


async def do_delete_alarm(args, creds: StoredConfig, lib: dict[str, Any], reporter: Reporter) -> int:
    action = "alarm delete"
    Qingping = lib["Qingping"]

    if args.alarm_slot is None:
        raise ValueError("--alarm-slot is required with --delete-alarm (use a number or 'all')")

    slot_raw = str(args.alarm_slot).strip().lower()

    async def _op(dev):
        if slot_raw == "all":
            # Wipe every slot we can see from get_alarms
            await dev.get_alarms()
            alarms = getattr(dev, "alarms", []) or []

            # Determine slot indices robustly
            slots: list[int] = []
            for a in alarms:
                s = getattr(a, "slot", None)
                if s is None:
                    continue
                try:
                    slots.append(int(s))
                except Exception:
                    continue

            if not slots:
                # fallback: try 0..len(alarms)-1
                slots = list(range(len(alarms)))

            slots = sorted(set(slots))

            failed: list[int] = []
            for s in slots:
                ok = await dev.delete_alarm(s)
                if not ok:
                    failed.append(s)

            if failed:
                raise RuntimeError(f"failed to delete alarm slots: {failed}")

            reporter.debug(f"deleted slots: {slots}")
            return ("all", len(slots))

        # single slot
        try:
            slot = int(slot_raw)
        except Exception:
            raise ValueError("--alarm-slot must be an integer (e.g. 0) or 'all'")

        ok = await dev.delete_alarm(slot)
        if not ok:
            raise RuntimeError("delete_alarm returned False")

        return (slot, 1)

    res = await _with_device(Qingping, creds.address, creds.token_hex, reporter, _op)
    if isinstance(res, tuple) and res[0] == "all":
        reporter.info(action, f"slot=all (deleted={res[1]})")
    else:
        reporter.info(action, f"slot={res[0]}")
    return 0


async def do_upload_ringtone(args, creds: StoredConfig, lib: dict[str, Any], reporter: Reporter) -> int:
    action = "ringtone upload"
    Qingping = lib["Qingping"]
    ringtones_mod = lib["ringtones"]

    path = Path(args.upload_ringtone).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"file not found: {path}")

    pcm = _read_pcm(path)

    slot = (args.ringtone_slot or "auto").strip().lower()
    if slot not in ("auto", "dead", "beef"):
        raise ValueError("--ringtone-slot must be: auto|dead|beef")

    async def _op(dev):
        # decide target signature
        if slot == "auto":
            await dev.get_configuration()
            cfg = getattr(dev, "configuration", None)
            current_sig = getattr(cfg, "ringtone_signature", None) if cfg else None
            sig = ringtones_mod.choose_next_custom_slot(current_sig)
        else:
            sig = ringtones_mod.parse_slot_signature(slot)

        reporter.debug(f"target signature: {bytes(sig).hex()}")

        last_pct = -1

        def _progress(p: float):
            nonlocal last_pct
            pct = int(p * 100)
            if pct != last_pct:
                last_pct = pct
                print(f"\rUploading: {pct:3d}%", end="", flush=True)

        ok = await dev.upload_ringtone(pcm, signature=bytes(sig), on_progress=_progress)
        print("\rUploading: 100%")
        if not ok:
            raise RuntimeError("upload_ringtone returned False")

    await _with_device(Qingping, creds.address, creds.token_hex, reporter, _op)
    reporter.info(action, f"file={path.name}, bytes={len(pcm)}, slot={slot}")
    return 0


# -------------------------- argparse (NO subcommands) --------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        formatter_class=argparse.RawTextHelpFormatter,
        description="Qingping CGD1 one-file CLI (no subcommands).",
        epilog=(
            "Config:\n"
            "  Save credentials once:\n"
            "    cgd1.py --set-config --address 58:AB:CD:EF:AB:CD --token 0abcd...efgh\n"
            "  Show config:\n"
            "    cgd1.py --show-config\n\n"
            "Time:\n"
            "  Sync with system time:\n"
            "    cgd1.py --set-time\n"
            "  Set explicit local time:\n"
            "    cgd1.py --set-time \"2026-02-08 12:34\"\n"
            "  Override timezone (device needs multiple of 6 minutes):\n"
            "    cgd1.py --set-time --tz +01:00\n\n"
            "Settings:\n"
            "  Read settings:\n"
            "    cgd1.py --get-settings\n"
            "  Update settings (examples):\n"
            "    cgd1.py --set-settings --volume 3 --lang en --timefmt 24 --temp c\n"
            "    cgd1.py --set-settings --backlight 0 --day-bright 50 --night-bright 20\n"
            "    cgd1.py --set-settings --night-mode off\n"
            "  Preview:\n"
            "    cgd1.py --preview-brightness 70\n"
            "    cgd1.py --preview-ringtone --preview-volume 5\n\n"
            "Alarms:\n"
            "  List:\n"
            "    cgd1.py --get-alarms\n"
            "  Update slot:\n"
            "    cgd1.py --set-alarm --alarm-slot 0 --alarm-enable --alarm-time 07:30 --alarm-days weekdays --alarm-snooze on\n"
            "  Delete:\n"
            "    cgd1.py --delete-alarm --alarm-slot 0\n"
            "    cgd1.py --delete-alarm --alarm-slot all\n\n"
            "Ringtone:\n"
            "  Upload custom ringtone (.wav 8kHz u8 mono or .raw):\n"
            "    cgd1.py --upload-ringtone my.wav --ringtone-slot auto\n\n"
            "Debug:\n"
            "  Add --debug to print more diagnostics and full traceback on errors.\n"
        ),
    )

    p.add_argument("--config", default=None, help=f"Config path (default: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--debug", action="store_true", help="Debug output (more logs + traceback on errors)")

    # Credentials (required unless saved config exists)
    p.add_argument("--address", default=None, help="BLE MAC address (required unless config saved)")
    p.add_argument("--token", default=None, help="16-byte token (32 hex chars) (required unless config saved)")

    # One action required
    actions = p.add_mutually_exclusive_group(required=True)

    actions.add_argument("--set-config", action="store_true", help="Save --address/--token to config file")
    actions.add_argument("--show-config", action="store_true", help="Show current config (token hidden)")

    actions.add_argument(
        "--set-time",
        nargs="?",
        const="__SYSTEM__",
        metavar="TIME",
        help='Update device time. If TIME omitted: system time. TIME format: "YYYY-MM-DD HH:MM" or epoch seconds.',
    )
    p.add_argument("--tz", default=None, help="Timezone offset for --set-time: +HH:MM / -HH:MM or minutes (multiple of 6)")
    p.add_argument("--no-tz", action="store_true", help="For --set-time: do NOT update timezone offset")

    actions.add_argument("--get-settings", action="store_true", help="Read settings")
    actions.add_argument("--set-settings", action="store_true", help="Update settings (use options below)")
    actions.add_argument("--preview-brightness", type=int, metavar="0..100", help="Send brightness preview (0..100 step 10)")
    actions.add_argument("--preview-ringtone", action="store_true", help="Play ringtone preview")

    # settings options (used with --set-settings)
    p.add_argument("--volume", type=int, default=None, help="Volume 1..5")
    p.add_argument("--lang", choices=["en", "zh"], default=None, help="Language")
    p.add_argument("--timefmt", choices=["24", "12"], default=None, help="Time format")
    p.add_argument("--temp", choices=["c", "f"], default=None, help="Temperature unit")
    p.add_argument("--master-alarms", default=None, help='Master alarms: "on" or "off"')
    p.add_argument("--backlight", type=int, default=None, help="Backlight seconds 0..30 (0=off)")
    p.add_argument("--day-bright", dest="day_bright", type=int, default=None, help="Day brightness 0..100 step 10")
    p.add_argument("--night-bright", dest="night_bright", type=int, default=None, help="Night brightness 0..100 step 10")
    p.add_argument("--night-mode", default=None, help='Night mode: "on" or "off"')
    p.add_argument("--night-start", default=None, help='Night start time "HH:MM"')
    p.add_argument("--night-end", default=None, help='Night end time "HH:MM"')
    p.add_argument("--ringtone", default=None, help="Ringtone: name, 'dead'/'beef', or 8 hex chars (4 bytes)")

    # preview options
    p.add_argument("--preview-volume", type=int, default=None, help="Used with --preview-ringtone: volume 1..5")

    # alarm actions
    actions.add_argument("--get-alarms", action="store_true", help="List alarms")
    actions.add_argument("--set-alarm", action="store_true", help="Update one alarm slot (use --alarm-* options)")
    actions.add_argument("--delete-alarm", action="store_true", help="Delete one alarm slot (use --alarm-slot)")

    # alarm params
    p.add_argument("--alarm-slot", default=None, help="Alarm slot index (e.g. 0..15). For --delete-alarm you can also use: all")
    p.add_argument("--alarm-time", default=None, help='Alarm time "HH:MM"')
    p.add_argument("--alarm-days", default=None, help="Repeat: once|weekdays|weekend|all|mon,tue,wed,...")
    p.add_argument("--alarm-snooze", default=None, help='Snooze: "on" or "off"')
    en = p.add_mutually_exclusive_group(required=False)
    en.add_argument("--alarm-enable", action="store_true", help="Enable alarm")
    en.add_argument("--alarm-disable", action="store_true", help="Disable alarm")

    # ringtone upload action
    actions.add_argument("--upload-ringtone", dest="upload_ringtone", metavar="FILE", help="Upload custom ringtone (.wav/.raw)")
    p.add_argument("--ringtone-slot", default="auto", help="Upload target slot: auto|dead|beef (default: auto)")

    return p


# -------------------------- main --------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    reporter = _setup_logging(args.debug)
    config_path = _expand_config_path(args.config)

    try:
        # config-only operations
        if args.set_config:
            if not args.address or not args.token:
                raise ValueError("--set-config requires --address and --token")
            cfg = StoredConfig(address=args.address.strip(), token_hex=_normalize_token_hex(args.token))
            _write_config(config_path, cfg)
            reporter.info("config set", f"path={config_path}")
            return 0

        if args.show_config:
            cfg = _read_config(config_path)
            if cfg is None:
                reporter.error("config show", f"not found/invalid at {config_path}")
                return 2
            print("Config")
            print("-" * 60)
            print(f"path   : {config_path}")
            print(f"address: {cfg.address}")
            print(f"token  : {cfg.token_hex[:4]}...{cfg.token_hex[-4:]} (hidden)")
            print("-" * 60)
            reporter.info("config show")
            return 0

        # BLE operations
        creds = _resolve_creds(args, config_path)
        lib = _import_lib()

        # dispatch (one action at a time)
        if args.set_time is not None:
            return asyncio.run(do_set_time(args, creds, lib, reporter))

        if args.get_settings:
            return asyncio.run(do_get_settings(args, creds, lib, reporter))

        if args.set_settings:
            return asyncio.run(do_set_settings(args, creds, lib, reporter))

        if args.preview_brightness is not None:
            return asyncio.run(do_preview_brightness(args, creds, lib, reporter))

        if args.preview_ringtone:
            return asyncio.run(do_preview_ringtone(args, creds, lib, reporter))

        if args.get_alarms:
            return asyncio.run(do_get_alarms(args, creds, lib, reporter))

        if args.set_alarm:
            return asyncio.run(do_set_alarm(args, creds, lib, reporter))

        if args.delete_alarm:
            return asyncio.run(do_delete_alarm(args, creds, lib, reporter))

        if args.upload_ringtone is not None:
            return asyncio.run(do_upload_ringtone(args, creds, lib, reporter))

        raise RuntimeError("no action selected (argparse should prevent this)")

    except KeyboardInterrupt:
        reporter.error("main", "interrupted")
        return 130
    except Exception as e:
        reporter.error("main", _err_reason(e))
        if reporter.debug_enabled:
            traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
