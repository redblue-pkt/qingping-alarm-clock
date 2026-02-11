# Qingping CGD1 Alarm Clock — `cgd1.py` (Python BLE CLI)

One-file Python CLI to control the **Qingping CGD1 Bluetooth Alarm Clock** over BLE.

**Clock / product page:** https://qingping.co/bluetooth-alarm-clock/overview

> Not affiliated with Qingping/Cleargrass/Xiaomi. Use at your own risk.

## Credits

This repo is built on top of other people’s heavy work:

- **Base / working:** https://github.com/ov1d1u/qingping_alarm_clock  
- **Protocol/spec documentation:** https://github.com/MrBoombastic/clOwOck  

**What I did:** I mostly wrote the **`cgd1.py` one-file CLI tool**, and I patched/cleaned up parts of the code (small improvements/additions).  
**There may still be bugs or edge cases I didn’t catch** — if something behaves wrong, assume it can be my mistake.

## What this tool is for

`cgd1.py` lets you manage the clock without the phone app:

- Set/sync time (including timezone offset)
- Read/update settings (volume, language, backlight, brightness, night mode, etc.)
- List/set/delete alarms
- Upload a custom ringtone

## Requirements

- Python 3
- BLE adapter
- CGD1 **BLE MAC address** and **16-byte token** (32 hex chars)

## Usage examples

```bash
Save credentials:

cgd1.py --set-config --address 58:AB:CD:EF:AB:CD --token 0123456789abcdef0123456789abcdef

Show current config (token hidden):

cgd1.py --show-config

Time

Sync with system time:

cgd1.py --set-time

Set explicit local time:

cgd1.py --set-time "2026-02-08 12:34"

Override timezone (device needs multiple of 6 minutes):

cgd1.py --set-time --tz +01:00

Settings

Read settings:

cgd1.py --get-settings

Update settings (examples):

cgd1.py --set-settings --volume 3 --lang en --timefmt 24 --temp c
cgd1.py --set-settings --backlight 0 --day-bright 50 --night-bright 20
cgd1.py --set-settings --night-mode off

Preview:

cgd1.py --preview-brightness 70
cgd1.py --preview-ringtone --preview-volume 5

Alarms

List:

cgd1.py --get-alarms

Update one slot:

cgd1.py --set-alarm --alarm-slot 0 --alarm-enable --alarm-time 07:30 --alarm-days weekdays --alarm-snooze on

Delete one slot:

cgd1.py --delete-alarm --alarm-slot 0

Delete all slots:

cgd1.py --delete-alarm --alarm-slot all

Custom ringtone upload (RAW)

The clock expects raw audio:

    8000 Hz

    mono

    unsigned 8-bit PCM

    raw .raw file

Convert any audio to .raw (ffmpeg)

Example:

ffmpeg -hide_banner -loglevel error -i "INPUT_FILE" -t 10 -ac 1 -ar 8000 -acodec pcm_u8 -f u8 "OUTPUT_FILE"

    INPUT_FILE = your audio file (mp3/wav/whatever)

    -t 10 = cut to 10 seconds

    OUTPUT_FILE.raw = output filename you choose

Upload to the clock

cgd1.py --upload-ringtone "out.raw" --ringtone-slot auto

--ringtone-slot can be:

    auto (recommended)

    dead

    beef

Debug

Add --debug for more logs + full traceback:

cgd1.py --get-settings --debug
