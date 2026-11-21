# Changelog

## 1.0.6
- 'command' is now the current command and is what "Clear Command after" blanks; 'lastCommand' always keeps the most recent one
- Both use RFXCom433's wording (e.g. "Mood5"); the old-plugin 'command' wording is gone
- Converted devices only keep the old 'lastUpdated' state (and only until the converter's --drop-legacy is used)
- convert_indigo_db.py: updates embedded scripts to the new wording, --drop-legacy moves lastUpdated triggers to lastSeen, --set-code sets a converted device's code, and it can be re-run on an already converted database

## 1.0.5
- "Recently heard" now lists every code heard this session, including ones that already have a device (marked "used by ...")
- A device can be given a code another device already uses (they share it and both update)

## 1.0.4
- Fixed "UI dynamic list function returned illegal ID string" when editing devices (empty option IDs in the heard-code, main-value and ignored-code lists)

## 1.0.3
- New device type: RF Remote / Wall Controller (receive only, no on/off) for handsets and master switches
- Several devices can now share one RF code (e.g. a remote and the plug it controls); all of them update
- Migrated devices keep the old plugin's 'command' and 'lastUpdated' states, in the old format ("Mood 5", "Group off"...), so existing triggers and scripts keep working
- Migration now also covers X10 appliances and security sensors; AC address 0 devices become local-only
- tools/convert_indigo_db.py: converts old RFXCOM devices inside a stopped Indigo database, keeping their IDs

## 1.0.2
- Plugins > RFXCom433 > Migrate from Old RFXCOM Plugin: creates RFXCom433 devices from the old plugin's AC, HomeEasy and LightwaveRF switches/dimmers, AC/LightwaveRF remote buttons and temperature sensors
- Migrated devices whose old code is ambiguous learn their real code the first time they're heard

## 1.0.1
- New device option: clear Last Command after X seconds (switches, dimmers, blinds)

## 1.0.0
First release.
- Devices identified by protocol + RF id; one device can be both the remote and the plug
- Auto-creation with sighting thresholds, folder, and ignore-on-delete
- Switch behaviour: send+receive / remote button (receive only) / transmit only
- Automatic switch-to-dimmer conversion when a level is heard
- Group on/off handling across all units on a code
- Transmit queue with per-command ACK/NAK; state only changes on ACK; optional repeats
- RF Command Received event fires on every press
- Sensors: one device type with dynamic states, unit conversion, main-value choice, battery
- Security sensors, doorbells/chimes, blinds/covers
- Offline detection with events and error state
- Interface device: firmware type/version, hardware, noise level, protocols, counters
- Receiver protocols managed per connect (no flash writes)
- Network RFXtrx support (socket / RFC2217)
- New transmitter codes, pairing signal, raw packets
- Firmware update over local USB
