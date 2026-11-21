# RFXCom433 for Indigo

Indigo plugin for RFXCOM RFXtrx433, RFXtrx433E and RFXtrx868 transceivers, built on
[pyRFXtrx](https://github.com/Danielhiversen/pyRFXtrx) (the library Home Assistant uses).

By Durosity.

An independent, from-scratch plugin. If you're moving from another RFXCOM plugin, disable it
first (only one program can open the RFXtrx's port); devices auto-create the first time
you press them.

## How devices work

Like Home Assistant, every RF device is identified by **protocol + RF id** (for example
AC/HomeEasy UK `0abcdef:3`). That one code is the device: when a plug has learned a
remote's code, the device created from the remote press can also switch the plug, and
pressing the real remote updates the Indigo state.

- Every RF device has **command** (the current command, optionally cleared after a few
  seconds) and **lastCommand** (always the most recent one).
- **RF Switch / Plug / Remote** (relay) - each one has a *Behaviour* setting:
  - *Switch* - sends and receives (default)
  - *Remote button* - receive only, never transmits (optionally auto-off)
  - *Transmit only* - ignores received packets
- **RF Dimmer** - switches convert themselves to dimmers automatically when a dim level is heard
- **RF Blind / Cover** - On = open, Off = close, plus a Stop action (LightwaveRF inline relays, RollerTrol, Somfy RFY, DDxxxx)
- **RF Sensor** - temperature, humidity, pressure, rain, wind, UV, energy... every value
  becomes its own state; you choose which one is the main value (HomeKit uses it)
- **RF Security Sensor** - motion/door/smoke (on = alarm/motion), tamper, battery
- **RF Doorbell / Chime** - momentary "pressed" state, and a Ring Chime action
- **RFXtrx Interface** - connection, firmware type/version, hardware version, noise level,
  receiver protocols, packet counters, firmware update progress

Group on/off from a remote updates every device on that house/remote code.

## Moving from the old RFXCOM plugin

**Plugins > RFXCom433 > Migrate from Old RFXCOM Plugin...** reads the old plugin's devices
and creates matching RFXCom433 ones. *Preview* writes the plan to the Indigo log, including
what currently uses each old device. Migrating can rename the old devices to "... (old)",
give the new ones the original names and folders, and copy on/off and brightness.

- AC, HomeEasy and LightwaveRF appliances and dimmers get their exact code.
- AC and LightwaveRF remote buttons: the old plugin stored some codes ambiguously, so these
  wait and learn their real code the first time you press the button.
- A remote whose code matches a migrated plug becomes part of that plug's device.
- Temperature sensors learn their code on their first reading.
- LightwaveRF handsets and other types aren't migrated; they auto-create when used.

Indigo can't move a device's ID between plugins, so triggers, action groups, schedules and
control pages need repointing from the "(old)" devices to the new ones.

### Keeping device IDs (offline database conversion)

`tools/convert_indigo_db.py` converts the old plugin's devices inside a copy of your Indigo
database, keeping every device ID so nothing needs repointing. Stop the Indigo server, copy
the `.indiDb` file, then:

    python3 tools/convert_indigo_db.py Copy.indiDb                    # dry run report
    python3 tools/convert_indigo_db.py Copy.indiDb -o Converted.indiDb

The report lists every device's new type and code, and checks every trigger, condition,
control page and embedded script that reads one of their states. Only the old plugin's
device entries, the triggers that point at them and scripts that read their `command` state
are rewritten. Options:

- `--local-only ID,ID` keeps chosen relays from transmitting (virtual flags)
- `--drop-legacy` moves triggers on the old `lastUpdated` state to `lastSeen` and removes it
- `--set-code ID=KEY` sets a converted device's RF code (e.g. `123456789=14:00:a1b2c3:16`)

Running it on a database converted earlier just applies the reference updates and options.

## Auto-creation

Plugin settings > *Auto-create devices*:

- *Switches, remotes, doorbells and security sensors* (default)
- *Everything* - also weather and energy sensors, which must be heard 3 times in 10 minutes
  first so the neighbours' weather stations don't flood your device list
- *Off* - use **Plugins > RFXCom433 > Create Device from Heard Code...**

Deleting an auto-created device adds its code to an ignore list so it isn't re-created
(**Manage Ignored Codes...** to undo).

**Create New Transmitter Code...** generates a random AC/HomeEasy, HomeEasy EU, LightwaveRF
or ARC code so you can pair a plug with no remote: create it, put the plug into learn mode,
turn the device on (or run *Send Pairing Signal*).

## Actions and events

Actions: Send Group On/Off, LightwaveRF Mood 1-5 / Lock / Unlock / All Lock, Stop Blind,
Send Pairing Signal, Ring Chime, Send Raw RFXtrx Packet, Reconnect.

Events: RF Command Received (fires on every press, even if the state didn't change -
filter by device and command), New RF Code Heard, Device Went Offline, Device Came Back
Online, RFXtrx Connection Lost, RFXtrx Connected, Firmware Update Finished.

## Receiver protocols

Tick *Let the plugin set which protocols the RFXtrx listens for* to manage them from the
plugin. They're applied on every connect and not written to the RFXtrx's flash, so there's
no flash wear and they survive firmware updates. Leave it unticked to keep what RFXmngr set.

## Network RFXtrx

The port setting accepts a local USB port, a network socket (`socket://host:port`, e.g.
ser2net on a Pi) or RFC2217.

## Firmware updates

**Plugins > RFXCom433 > Update RFXtrx Firmware...** flashes an RFXCOM `.hex` file over the
local USB connection, talking directly to the RFXtrx's serial bootloader. It refuses network connections, Pro XL / RFM-based hardware, and files that
don't match the unit's frequency. *Check File* validates the file (Intel HEX checksums,
block alignment, address range) before anything is sent to the RFXtrx.

The bootloader is never erased, so an interrupted update can be re-run, or recovered with
RFXflash on Windows. Receiver protocol settings are reset by any firmware update.

## Requirements

Indigo 2023.2 or later (Python 3). `pyRFXtrx` is installed automatically from
`requirements.txt`.

## Licence

MIT (see LICENSE). All code in this plugin is original.

Third-party: [pyRFXtrx](https://github.com/Danielhiversen/pyRFXtrx) (LGPL-3.0) is not
included in this repository; Indigo downloads and installs it unmodified from PyPI via
`requirements.txt`, and the plugin uses it as a library. "RFXCOM" and "RFXtrx" are names
of RFXCOM's products; this plugin isn't affiliated with RFXCOM.

## Installing

Download the latest `RFXCom433.vX.Y.Z.indigoplugin.zip` from Releases, unzip it and double-click
`RFXCom433.indigoPlugin`.

## Development

`tests/test_plugin.py` runs the plugin against a stubbed Indigo with real pyRFXtrx packet parsing;
`tests/test_pty.py` runs it against an emulated RFXtrx on a pseudo-terminal:

    pip install pyRFXtrx==0.34.0
    python3 tests/test_plugin.py && python3 tests/test_pty.py
