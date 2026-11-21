"""
Migration planner for devices created by the older RFXCOM plugin
(bundle nl.rjdekok.indigoplugin.RFXCOM).

Only the old plugin's stored device settings are read (device type ids and the
'address' / 'unit' / 'sensorNumber' values). Each becomes either an exact
RFXCom433 device key, or patterns the new device waits to hear when the old
settings are ambiguous. Used by the in-plugin migration and the offline
database converter.

Kept free of `indigo` so it can be tested outside Indigo.
"""

import itertools

OLD_PLUGIN_ID = "nl.rjdekok.indigoplugin.RFXCOM"

# Old states worth keeping on a migrated sensor, as RFXCom433 state ids
SENSOR_STATE_CARRY = ("temperature", "humidity", "humidityStatus")


class Plan:
    def __init__(self, old_id, old_name, old_type):
        self.old_id = old_id
        self.old_name = old_name
        self.old_type = old_type
        self.new_type = None
        self.key = None             # exact key, if known
        self.patterns = []          # otherwise fnmatch patterns to adopt on first reception
        self.role = "switch"
        self.extra_props = {}
        self.note = ""

    @property
    def supported(self):
        return self.new_type is not None

    def describe(self):
        if not self.supported:
            return "not migrated - " + self.note
        if self.key:
            target = self.key
        elif len(self.patterns) == 1:
            target = "learns its code when first heard ({0})".format(self.patterns[0])
        else:
            target = "learns its code when first heard ({0} possible codes)".format(len(self.patterns))
        role = {"button": ", local only (no transmit)"}.get(self.role, "")
        return "{0}{1}: {2}{3}".format(self.new_type, role, target, (" - " + self.note) if self.note else "")


def _hex(text, length):
    text = (text or "").strip().replace(" ", "").replace("-", "")
    if len(text) != length:
        raise ValueError("expected {0} hex digits, got '{1}'".format(length, text))
    return int(text, 16)


def _unit(value):
    unit = int(str(value).strip())
    if not 0 <= unit <= 16:
        raise ValueError("unit {0} out of range".format(unit))
    return unit


def _byte_candidates(pair):
    """The old plugin logged ids < 0x10 as e.g. '50' for 0x05. Return every byte a
    two-character id fragment could have been."""
    pair = pair.lower()
    value = int(pair, 16)
    if pair[1] == "0" and pair[0] != "0":
        return sorted({value, int(pair[0], 16)})
    return [value]


def _id_candidates(text, nbytes):
    text = (text or "").strip().replace(" ", "")
    if len(text) != nbytes * 2:
        raise ValueError("expected {0} characters, got '{1}'".format(nbytes * 2, text))
    pairs = [text[i:i + 2] for i in range(0, len(text), 2)]
    return sorted({int.from_bytes(bytes(c), "big")
                   for c in itertools.product(*[_byte_candidates(p) for p in pairs])})


def plan_device(old_id, old_name, old_type, props, old_states=None):
    p = Plan(old_id, old_name, old_type)
    old_states = old_states or {}
    try:
        if old_type in ("ACAppliance", "ACDimmer", "HEAppliance", "HEDimmer"):
            subtype = 0x00 if old_type.startswith("AC") else 0x01
            raw = _hex(props.get("address"), 8)
            ident = raw & 0x3FFFFFF
            p.new_type = "rfxDimmer" if old_type.endswith("Dimmer") else "rfxSwitch"
            p.key = "11:{0:02x}:{1:07x}:{2}".format(subtype, ident, _unit(props.get("unit")))
            if ident == 0:
                p.role = "button"
                p.note = "address 0 can't be transmitted (the RFXtrx rejects it), so this stays a local-only device"
            elif raw != ident:
                p.note = "address {0} is wider than AC's 26 bits; the RFXtrx only ever sent {1:07X}".format(
                    props.get("address"), ident)
        elif old_type in ("LWAppliance", "LWDimmer"):
            ident = _hex(props.get("address"), 6)
            p.new_type = "rfxDimmer" if old_type == "LWDimmer" else "rfxSwitch"
            p.key = "14:00:{0:06x}:{1}".format(ident, _unit(props.get("unit")))
        elif old_type == "X10Appliance":
            code = (props.get("address") or "").strip().upper()
            house, unit = code[:1], int(code[1:])
            if house not in "ABCDEFGHIJKLMNOP" or not 1 <= unit <= 16:
                raise ValueError("bad X10 address '{0}'".format(code))
            p.new_type = "rfxSwitch"
            p.key = "10:00:{0}{1}".format(house, unit)
        elif old_type == "ACSwitch":
            unit = _unit(props.get("unit"))
            ids = sorted({i & 0x3FFFFFF for i in _id_candidates(props.get("address"), 4)})
            p.new_type = "rfxRemote"
            p.patterns = ["11:0[01]:{0:07x}:{1}".format(i, unit) for i in ids]
            p.extra_props["legacyStates"] = True
        elif old_type == "LWSwitch":
            unit = _unit(props.get("unit"))
            ids = _id_candidates(props.get("address"), 3)
            p.new_type = "rfxRemote"
            if len(ids) == 1:
                p.key = "14:00:{0:06x}:{1}".format(ids[0], unit)
            else:
                p.patterns = ["14:00:{0:06x}:{1}".format(i, unit) for i in ids]
            p.extra_props["legacyStates"] = True
        elif old_type == "SecuritySensor":
            n = int(str(props.get("sensorNumber") or props.get("address")).strip())
            b0, b1, b2 = n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF
            p.new_type = "rfxSecurity"
            # the old plugin built its number differently per subtype
            p.patterns = ["20:0[0-2]:{0:02x}??{1:02x}".format(b0, b1),
                          "20:03:{0:02x}{1:02x}??".format(b0, b1),
                          "20:0[4-9a-f]:{0:02x}{1:02x}{2:02x}".format(b0, b1, b2)]
            p.extra_props["legacyStates"] = True
        elif old_type == "Temperature":
            number = int(str(props.get("sensorNumber")).strip())
            id1, id2 = number & 0xFF, (number >> 8) & 0xFF
            p.new_type = "rfxSensor"
            # Oregon/La Crosse temperature types 0x50 (temp), 0x52 (temp+hum), 0x54 (temp+hum+baro)
            p.patterns = ["5[0-4]:*:{0:02x}:{1:02x}".format(id1, id2)]
            keys = [k for k in SENSOR_STATE_CARRY if k in old_states]
            if keys:
                p.extra_props["valueKeys"] = ",".join(keys)
            if props.get("sensorValueType") == "Humid":
                p.extra_props["primaryValue"] = "humidity"
            p.extra_props["legacyStates"] = True
            p.note = "Oregon-style sensors pick a new id when their batteries are changed"
            if number == 0:
                p.note = "sensor number 0 looks unset in the old plugin - it may never match"
        elif old_type == "LWRemote":
            p.note = "press each button once; RFXCom433 creates one device per button"
        else:
            p.note = "this type isn't migrated automatically; it will auto-create when heard, or use " \
                     "Create Device from Heard Code"
    except (ValueError, TypeError) as exc:
        p.new_type = None
        p.key = None
        p.patterns = []
        p.note = "couldn't read its settings ({0})".format(exc)
    return p
