"""
Indigo-independent helpers: how RF packets map onto plugin devices and states.
Kept free of `indigo` imports so it can be unit-tested outside Indigo.
"""

import random
import re

from RFXtrx import lowlevel

# ----------------------------------------------------------------------------
# Device identity
# ----------------------------------------------------------------------------
def make_key(packettype, subtype, id_string):
    return "{0:02x}:{1:02x}:{2}".format(packettype, subtype, id_string)


def split_key(key):
    pt, st, ident = key.split(":", 2)
    return int(pt, 16), int(st, 16), ident


def group_base(packettype, id_string):
    """The part of an id shared by all units on one remote/house code."""
    if packettype == 0x10:                      # Lighting1: "A1" -> "A"
        return id_string[:1]
    if packettype in (0x11, 0x14):              # Lighting2/5: "0abcdef:3" -> "0abcdef"
        return id_string.split(":")[0]
    if packettype == 0x15:                      # Lighting6: "1234:B3" -> "1234:B"
        return id_string[:-1]
    return None


def protocol_name(packettype, subtype):
    cls = lowlevel.PACKET_TYPES.get(packettype)
    types = getattr(cls, "TYPES", {}) if cls else {}
    sub = types.get(subtype)
    base = cls.__name__ if cls else "0x{0:02X}".format(packettype)
    return "{0} ({1})".format(sub, base) if sub else "{0} sub 0x{1:02X}".format(base, subtype)


def short_protocol(packettype, subtype):
    cls = lowlevel.PACKET_TYPES.get(packettype)
    types = getattr(cls, "TYPES", {}) if cls else {}
    return types.get(subtype) or (cls.__name__ if cls else "RF")


# ----------------------------------------------------------------------------
# What kind of plugin device a packet type becomes
# ----------------------------------------------------------------------------
LIGHTING_TYPES = (0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x1E)
COVER_TYPES = (0x19, 0x1A, 0x31)
CHIME_TYPES = (0x16,)
SECURITY_TYPES = (0x20,)

# Lighting packet types that can carry a dim level
DIMMABLE_TYPES = (0x11, 0x12, 0x14)


def device_type_for(packettype, command=None):
    """Choose the Indigo device type to auto-create for a packet type."""
    if packettype in CHIME_TYPES:
        return "rfxDoorbell"
    if packettype in SECURITY_TYPES:
        return "rfxSecurity"
    if packettype in COVER_TYPES:
        return "rfxCover"
    if packettype == 0x14 and command and "inline relay" in command.lower():
        return "rfxCover"
    if packettype in LIGHTING_TYPES:
        return "rfxSwitch"
    return "rfxSensor"


def default_role(packettype):
    return "button" if packettype == 0x13 else "switch"


# ----------------------------------------------------------------------------
# Command semantics
# ----------------------------------------------------------------------------
GROUP_ON = ("group on", "all/group on")
GROUP_OFF = ("group off", "all/group off")


def classify_command(command):
    """Return (kind, on_state) for a received command string.

    kind: 'on', 'off', 'level', 'group_on', 'group_off', 'group_level',
          'open', 'close', 'stop', 'other'
    """
    c = (command or "").strip().lower()
    if c in ("on", "up", "open (inline relay)"):
        return ("open" if c != "on" else "on"), True
    if c in ("off", "down", "close (inline relay)"):
        return ("close" if c != "off" else "off"), False
    if c in ("stop", "stop (inline relay)"):
        return "stop", None
    if c in GROUP_ON:
        return "group_on", True
    if c in GROUP_OFF:
        return "group_off", False
    if c == "set level":
        return "level", None
    if c == "set group level":
        return "group_level", None
    if re.match(r"^level \d$", c):              # Ikea Koppla
        return "level", None
    return "other", None


# Commands sent for group on/off, per packet type (and Lighting5 subtype)
def group_command_codes(packettype, subtype):
    if packettype == 0x10:
        return 0x06, 0x05
    if packettype == 0x11:
        return 0x04, 0x03
    if packettype == 0x14:
        if subtype == 0x00:
            return None, 0x02                   # LightwaveRF: group off only
        if subtype in (0x02, 0x04, 0x0F):
            return 0x03, 0x02
        return None, None
    if packettype == 0x15:
        return 0x02, 0x03
    return None, None


LIGHTWAVE_COMMANDS = {
    "mood1": 0x03, "mood2": 0x04, "mood3": 0x05, "mood4": 0x06, "mood5": 0x07,
    "unlock": 0x0A, "lock": 0x0B, "alllock": 0x0C,
}

# Event-menu command values -> received command strings they match
TRIGGER_COMMANDS = {
    "on": ("on",),
    "off": ("off",),
    "group_on": GROUP_ON,
    "group_off": GROUP_OFF,
    "level": ("set level", "set group level"),
    "mood1": ("mood1",), "mood2": ("mood2",), "mood3": ("mood3",),
    "mood4": ("mood4",), "mood5": ("mood5",),
    "open": ("open (inline relay)", "up"),
    "close": ("close (inline relay)", "down"),
    "stop": ("stop", "stop (inline relay)"),
    "lock": ("lock", "all lock"),
    "unlock": ("unlock",),
}

SECURITY_ON = ("alarm", "alarm delayed", "motion", "panic", "ir")
SECURITY_OFF = ("normal", "normal delayed", "no motion", "end panic")


# ----------------------------------------------------------------------------
# Sensor values
# ----------------------------------------------------------------------------
VALUE_STATE_IDS = {
    "Temperature": "temperature",
    "Temperature2": "temperature2",
    "Humidity": "humidity",
    "Humidity status": "humidityStatus",
    "Barometer": "pressure",
    "Forecast": "forecast",
    "Rain rate": "rainRate",
    "Rain total": "rainTotal",
    "Wind direction": "windDirection",
    "Wind average speed": "windSpeed",
    "Wind gust": "windGust",
    "Chill": "windChill",
    "UV": "uvIndex",
    "Energy usage": "power",
    "Total usage": "energyTotal",
    "Count": "count",
    "Current Ch. 1": "current1",
    "Current Ch. 2": "current2",
    "Current Ch. 3": "current3",
    "Voltage": "voltage",
    "Current": "current",
    "Counter value": "counter",
    "Sensor Status": "sensorStatus",
    "Contract type": "contractType",
}

# values we never turn into states (handled elsewhere or redundant)
SKIP_VALUES = ("Battery numeric", "Rssi numeric", "Humidity status numeric",
               "Forecast numeric", "Payload", "Tamper")

STRING_STATES = ("humidityStatus", "forecast", "sensorStatus", "contractType", "windDirectionText")

STATE_LABELS = {
    "temperature": "Temperature", "temperature2": "Temperature 2", "humidity": "Humidity",
    "humidityStatus": "Humidity Status", "pressure": "Pressure", "forecast": "Forecast",
    "rainRate": "Rain Rate", "rainTotal": "Rain Total", "windDirection": "Wind Direction",
    "windSpeed": "Wind Speed", "windGust": "Wind Gust", "windChill": "Wind Chill",
    "uvIndex": "UV Index", "power": "Power", "energyTotal": "Energy Total", "count": "Count",
    "current1": "Current Ch 1", "current2": "Current Ch 2", "current3": "Current Ch 3",
    "voltage": "Voltage", "current": "Current", "counter": "Counter",
    "sensorStatus": "Sensor Status", "contractType": "Contract Type",
    "windDirectionText": "Wind Direction (compass)",
}

# Preferred primary value (shown in sensorValue) - first present wins
PRIMARY_ORDER = ("temperature", "power", "humidity", "pressure", "rainRate", "windSpeed",
                 "uvIndex", "current", "current1", "counter", "rainTotal", "energyTotal")


def state_id_for(value_name):
    if value_name in VALUE_STATE_IDS:
        return VALUE_STATE_IDS[value_name]
    words = re.sub(r"[^A-Za-z0-9 ]", " ", value_name).split()
    if not words:
        return None
    return words[0].lower() + "".join(w.capitalize() for w in words[1:])


def extract_values(values):
    """pyRFXtrx SensorEvent.values -> {stateId: value}"""
    out = {}
    for name, val in values.items():
        if name in SKIP_VALUES or val is None:
            continue
        sid = state_id_for(name)
        if sid:
            out[sid] = val
    if "windDirection" in out:
        out["windDirectionText"] = compass(out["windDirection"])
    return out


def choose_primary(state_ids, preferred=None):
    if preferred and preferred in state_ids:
        return preferred
    for sid in PRIMARY_ORDER:
        if sid in state_ids:
            return sid
    numeric = [s for s in state_ids if s not in STRING_STATES]
    return numeric[0] if numeric else (state_ids[0] if state_ids else None)


def battery_percent(raw):
    """RFXtrx battery nibble 0..9 (9 = OK) -> percent"""
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        return None
    if raw < 0:
        return None
    return min(100, (raw + 1) * 10)


# ----------------------------------------------------------------------------
# Units
# ----------------------------------------------------------------------------
def convert(state_id, value, prefs):
    """Convert a native (C, mm, m/s, hPa) value per plugin prefs.
    Returns (value, unit_suffix, decimals)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value, "", None
    tdec = int(prefs.get("tempDecimals", 1))
    if state_id in ("temperature", "temperature2", "windChill"):
        if prefs.get("tempUnits", "C") == "F":
            return round(value * 9.0 / 5.0 + 32.0, tdec), " \u00b0F", tdec
        return round(value, tdec), " \u00b0C", tdec
    if state_id in ("windSpeed", "windGust"):
        u = prefs.get("windUnits", "mph")
        if u == "mph":
            return round(value * 2.23694, 1), " mph", 1
        if u == "kmh":
            return round(value * 3.6, 1), " km/h", 1
        return round(value, 1), " m/s", 1
    if state_id in ("rainRate", "rainTotal"):
        if prefs.get("rainUnits", "mm") == "in":
            return round(value / 25.4, 2), " in" + ("/h" if state_id == "rainRate" else ""), 2
        return round(value, 1), " mm" + ("/h" if state_id == "rainRate" else ""), 1
    if state_id == "humidity":
        return value, "%", 0
    if state_id == "pressure":
        return value, " hPa", 0
    if state_id == "windDirection":
        return value, "\u00b0", 0
    if state_id == "power":
        return value, " W", 0
    if state_id in ("current", "current1", "current2", "current3"):
        return round(value, 1), " A", 1
    if state_id == "voltage":
        return value, " V", 0
    if state_id == "energyTotal":
        return value, " Wh", 0
    return value, "", None


COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass(degrees):
    try:
        return COMPASS[int((float(degrees) % 360) / 22.5 + 0.5) % 16]
    except (TypeError, ValueError):
        return ""


# ----------------------------------------------------------------------------
# Interface status (packet type 0x01, subtype 0x00, cmd 0x02)
# ----------------------------------------------------------------------------
TRANSCEIVER_TYPES = {
    0x50: "RFXtrx 310 MHz", 0x51: "RFXtrx 315 MHz", 0x52: "RFXrec 433.92 MHz",
    0x53: "RFXtrx 433.92 MHz", 0x54: "RFXtrx 433.42 MHz", 0x55: "RFXtrx 868.00 MHz",
    0x56: "RFXtrx 868.00 MHz FSK", 0x57: "RFXtrx 868.30 MHz", 0x58: "RFXtrx 868.30 MHz FSK",
    0x59: "RFXtrx 868.35 MHz", 0x5A: "RFXtrx 868.35 MHz FSK", 0x5B: "RFXtrx 868.95 MHz",
    0x5C: "RFXtrx 868.30 MHz FSK PKT", 0x5D: "RFXtrx 868.35 MHz FSK PKT",
    0x5E: "RFXtrx 434.15 MHz", 0x5F: "RFXtrx 434.50 MHz",
}

FIRMWARE_TYPES = {
    0x00: "Type1 RX", 0x01: "Type1", 0x02: "Type2", 0x03: "Ext", 0x04: "Ext2",
    0x05: "Pro1", 0x06: "Pro2", 0x10: "Pro XL1", 0x13: "Pro XL2",
    0x14: "RFM69 433", 0x15: "RFM69 868", 0x16: "Pro XL RFM95", 0x18: "RFX 310",
}


def decode_status(data):
    """Decode the raw status response. Field positions follow the RFXtrx interface response layout."""
    if data is None or len(data) < 11 or data[1] != 0x01:
        return None
    ttype = data[5]
    raw_fw = data[6]
    info = {
        "transceiverTypeCode": ttype,
        "transceiverType": TRANSCEIVER_TYPES.get(ttype, "unknown (0x{0:02X})".format(ttype)),
        "protocolBytes": bytes(data[7:11]).hex(),
        "hardwareVersion": "",
        "outputPower": None,
        "firmwareTypeCode": None,
        "noiseLevel": None,
    }
    if data[0] > 13 and len(data) > 15:
        # newer firmware: long status packet
        info["firmwareVersion"] = raw_fw + 1000
        info["hardwareVersion"] = "{0}.{1}".format(data[11], data[12])
        info["outputPower"] = data[13]
        fwt = data[14]
        info["firmwareTypeCode"] = fwt
        if fwt >= 0x05:
            info["noiseLevel"] = data[15]
    else:
        info["firmwareVersion"] = raw_fw
        if ttype == 0x52 and raw_fw < 162:
            fwt = 0x00
        elif ttype == 0x52 and 162 < raw_fw < 225:
            fwt = 0x02
        else:
            fwt = 0x03
        info["firmwareTypeCode"] = fwt
        if len(data) > 13:
            info["outputPower"] = data[13]
    info["firmwareType"] = FIRMWARE_TYPES.get(info["firmwareTypeCode"],
                                              "0x{0:02X}".format(info["firmwareTypeCode"]))
    modes = set()
    for byte_index, names in enumerate(lowlevel.Status.RECMODES):
        if 7 + byte_index >= len(data):
            break
        for bit, name in enumerate(names):
            if data[7 + byte_index] & (1 << bit):
                modes.add(name)
    info["enabledProtocols"] = sorted(modes)
    return info


# All receive modes pyRFXtrx knows about (pref key -> mode name)
def all_recmodes():
    return [m for group in lowlevel.Status.RECMODES for m in group]


RECMODE_LABELS = {
    "aeblyss": "AE Blyss", "rubicson": "Rubicson / La Crosse / Banggood",
    "fineoffset": "Fine Offset / Viking", "lighting4": "Lighting4 (PT2262 and compatibles)",
    "rsl": "RSL / Revolt", "byronsx": "Byron SX / Select Plus",
    "imagintronix": "Imagintronix / Opus", "undecoded": "Undecoded (diagnostics only)",
    "mertik": "Mertik Maxitrol", "adlightwave": "LightwaveRF / Siemens",
    "hideki": "Hideki / UPM", "lacrosse": "La Crosse", "fs20": "FS20 (868 only)",
    "proguard": "ProGuard (868 only)", "blindst0": "Blinds T0", "blindst1234": "Blinds T1-T4",
    "x10": "X10", "arc": "ARC (KaKu, Domia, Chacon...)", "ac": "AC / HomeEasy UK / KaKu automatic",
    "homeeasy": "HomeEasy EU", "meiantech": "Meiantech", "oregon": "Oregon Scientific",
    "ati": "ATI remotes", "visonic": "Visonic (315/868 only)",
    "keeloq": "KeeLoq", "homeconfort": "HomeConfort",
}

DEFAULT_RECMODES = ("ac", "arc", "x10", "homeeasy", "adlightwave", "oregon", "lacrosse",
                    "byronsx", "lighting4")


# ----------------------------------------------------------------------------
# New transmitter codes
# ----------------------------------------------------------------------------
NEW_CODE_PROTOCOLS = {
    # menu value: (packettype, subtype, label)
    "ac": (0x11, 0x00, "AC / HomeEasy UK"),
    "heeu": (0x11, 0x01, "HomeEasy EU"),
    "lwrf": (0x14, 0x00, "LightwaveRF"),
    "arc": (0x10, 0x01, "ARC"),
}


def random_id_string(proto, unit):
    pt, st, _label = NEW_CODE_PROTOCOLS[proto]
    if pt == 0x11:
        return "{0:07x}:{1}".format(random.randint(0x10000, 0x3FFFFFF), unit)
    if pt == 0x14:
        return "{0:06x}:{1}".format(random.randint(0x1000, 0xFFFFFE), unit)
    if pt == 0x10:
        return "{0}{1}".format(random.choice("ABCDEFGHIJKLMNOP"), unit)
    raise ValueError(proto)


def parse_hex_packet(text):
    """Parse a user-entered raw packet. Accepts '0B 11 00 ...' or '0b1100...'.
    Adds the length byte if omitted."""
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", text or "")
    if not cleaned or len(cleaned) % 2:
        raise ValueError("enter an even number of hex digits")
    data = bytearray(bytes.fromhex(cleaned))
    if data[0] != len(data) - 1:
        data = bytearray([len(data)]) + data
    if data[0] != len(data) - 1:
        raise ValueError("length byte doesn't match packet length")
    return bytes(data)


# ----------------------------------------------------------------------------
# Devices converted from the older RFXCOM plugin keep its 'lastUpdated' state
# ----------------------------------------------------------------------------
LEGACY_TIME_FORMAT = "%Y/%m/%d %H:%M:%S"
