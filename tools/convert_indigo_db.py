#!/usr/bin/env python3
"""
Convert devices owned by the old RFXCOM plugin (nl.rjdekok.indigoplugin.RFXCOM)
into RFXCom433 devices inside an Indigo database file, keeping every device ID,
so triggers, action groups, schedules, control pages and other plugins that
reference those devices keep working.

    python3 convert_indigo_db.py MyHome.indiDb               # dry run: report only
    python3 convert_indigo_db.py MyHome.indiDb -o New.indiDb  # write converted copy

It also updates what refers to those devices:
  * embedded scripts comparing dev.states["command"] to the old plugin's wording
    ("Mood 5", "Close"...) are changed to RFXCom433's ("Mood5", "Close (inline relay)"...)
  * with --drop-legacy, triggers on the old 'lastUpdated' state move to 'lastSeen'
    and the compatibility state is removed
Running it on a database that was converted earlier only does these updates.

Only run it on a copy, with the Indigo server stopped. Only the old plugin's
device entries are rewritten; every other byte of the file is left as it was.
"""

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = next(p for p in (os.path.join(HERE, "..", "Contents", "Server Plugin"),
                         os.path.join(HERE, "..", "RFXCom433.indigoPlugin", "Contents", "Server Plugin"))
           if os.path.isdir(p))
sys.path.insert(0, PLUGIN_DIR)

import rfx_migrate as MIG  # noqa: E402

NEW_PLUGIN_ID = "com.durosity.indigoplugin.rfxcom"
NEW_PLUGIN_NAME = "RFXCom433"

# Indigo device class numbers and the type flags RFXCom433's devices use
CLASS_FOR = {"relay": 1, "dimmer": 2, "sensor": 3, "custom": 0}
FLAGS_FOR = {"relay": 1, "dimmer": 3, "sensor": 65, "custom": 0}

# States each new type knows about; anything referenced outside these is reported
BASE_STATES = {"lastCommand", "lastSeen", "lastSeenTimestamp", "online", "signalStrength"}
TYPE_STATES = {
    "rfxSwitch": {"onOffState"}, "rfxDimmer": {"onOffState", "brightnessLevel"},
    "rfxCover": {"onOffState", "coverState"}, "rfxSecurity": {"onOffState", "status", "tamper"},
    "rfxDoorbell": {"onOffState"}, "rfxRemote": set(),
    "rfxSensor": {"sensorValue", "temperature", "humidity", "humidityStatus", "batteryLevel"},
}
LEGACY_STATES = {"lastUpdated"}
for _t in TYPE_STATES:
    TYPE_STATES[_t] |= {"command"}

# old plugin wording -> RFXCom433 wording for the 'command' state
OLD_TO_NEW_COMMAND = {
    "Mood 1": "Mood1", "Mood 2": "Mood2", "Mood 3": "Mood3", "Mood 4": "Mood4", "Mood 5": "Mood5",
    "Close": "Close (inline relay)", "Stop": "Stop (inline relay)", "Open": "Open (inline relay)",
    "Dimm": "Set level", "Bright": "Group off",
}


def device_types():
    """{typeId: (indigo kind, display name)} from RFXCom433's Devices.xml"""
    root = ET.parse(os.path.join(PLUGIN_DIR, "Devices.xml")).getroot()
    return {d.get("id"): (d.get("type"), d.findtext("Name")) for d in root.findall("Device")}


def typed(tag, value):
    e = ET.Element(tag)
    if isinstance(value, bool):
        e.set("type", "bool")
        e.text = "true" if value else "false"
    elif isinstance(value, int):
        e.set("type", "integer")
        e.text = str(value)
    else:
        e.set("type", "string")
        e.text = str(value)
    return e


def new_props(plan, kind, old_states):
    props = {
        "deviceKey": plan.key or "",
        "pendingPatterns": "|".join(plan.patterns),
        "protocol": plan.key[:5] if plan.key else "",
        "idString": plan.key.split(":", 2)[2] if plan.key else "",
        "address": plan.key.split(":", 2)[2] if plan.key else "",
        "role": plan.role,
        "respondToGroup": True,
        "repeats": "1",
        "autoDimmer": False,
        "momentarySeconds": "0",
        "offlineAfterMinutes": "0",
        "clearCommandSeconds": "0",
        "lastLevel": "100",
        "primaryValue": "",
        "migratedFrom": "database",
        "legacyStates": False,
        "dbMigrated": True,
        "autoCreated": False,
        "SupportsStatusRequest": False,
    }
    if plan.new_type == "rfxSensor":
        props.update({"SupportsSensorValue": True, "SupportsOnState": False,
                      "SupportsBatteryLevel": "batteryLevel" in old_states, "valueKeys": ""})
    elif plan.new_type in ("rfxSecurity", "rfxDoorbell"):
        props.update({"SupportsSensorValue": False, "SupportsOnState": True, "SupportsBatteryLevel": False})
    props.update(plan.extra_props)
    return props


def convert_device(dev, plan, types):
    """Rewrite one <Device> element in place."""
    kind, type_name = types[plan.new_type]
    old_states = {c.tag: c for c in (dev.find("States") if dev.find("States") is not None else [])}
    old_class = int(dev.findtext("Class") or 0)
    new_class = CLASS_FOR[kind]
    was_on = (dev.findtext("IsOn") == "true") or \
        (old_states.get("onOffState") is not None and old_states["onOffState"].text == "on")

    children = {c.tag: c for c in list(dev)}
    for c in list(dev):
        dev.remove(c)

    def put(tag, value):
        children[tag] = typed(tag, value)

    put("PluginID", NEW_PLUGIN_ID)
    put("PluginUiName", NEW_PLUGIN_NAME)
    put("TypeIdPlugin", plan.new_type)
    put("TypeName", type_name)
    put("Class", new_class)
    put("TypeFlags2", FLAGS_FOR[kind])
    put("StatesStructChanged", True)

    if kind in ("relay", "dimmer", "sensor"):
        put("IsOn", was_on if kind != "sensor" or plan.new_type != "rfxSensor" else False)
    else:
        children.pop("IsOn", None)
    if kind == "dimmer":
        if "BrightValue" not in children:
            put("BrightValue", 0)
    else:
        children.pop("BrightValue", None)
    if kind == "custom":
        put("DisplayStateId", "command")
    else:
        children.pop("DisplayStateId", None)
    if new_class != old_class:
        children.pop("StateImageTypeRaw", None)

    # plugin props: drop the old plugin's, add ours, keep every other plugin's
    meta = children.get("MetaProps")
    if meta is None:
        meta = ET.Element("MetaProps", {"type": "dict"})
        children["MetaProps"] = meta
    for c in list(meta):
        if c.tag in (MIG.OLD_PLUGIN_ID, NEW_PLUGIN_ID):
            meta.remove(c)
    props = new_props(plan, kind, old_states)
    if plan.new_type == "rfxSensor":
        props["valueKeys"] = ",".join(k for k in MIG.SENSOR_STATE_CARRY if k in old_states)
    ours = ET.Element(NEW_PLUGIN_ID, {"type": "dict"})
    for k in sorted(props):
        ours.append(typed(k, props[k]))
    meta.append(ours)
    for c in sorted(list(meta), key=lambda e: e.tag):
        meta.remove(c)
        meta.append(c)

    # Indigo-level capability flags
    properties = ET.Element("Properties", {"type": "dict"})
    caps = {}
    if kind in ("relay", "dimmer"):
        caps = {"SupportsOnState": True, "SupportsStatusRequest": False}
        if kind == "dimmer":
            caps["SupportsBrightness"] = True
    elif kind == "sensor":
        caps = {"SupportsOnState": props.get("SupportsOnState", False),
                "SupportsSensorValue": props.get("SupportsSensorValue", False),
                "SupportsBatteryLevel": props.get("SupportsBatteryLevel", False),
                "SupportsStatusRequest": False}
    for k in sorted(caps):
        properties.append(typed(k, caps[k]))
    if caps:
        children["Properties"] = properties
    else:
        children.pop("Properties", None)

    # states: keep values the new type also has; Indigo adds the rest on start
    keep = ((TYPE_STATES[plan.new_type] - {"command"}) |
            (LEGACY_STATES if props.get("legacyStates") else set())) & set(old_states)
    states = ET.Element("States", {"type": "dict"})
    for tag in sorted(keep):
        states.append(old_states[tag])
    if kind in ("relay", "dimmer") and "onOffState" not in keep:
        e = ET.Element("onOffState", {"type": "bool", "subtype": "onOff"})
        e.text = "on" if was_on else "off"
        states.append(e)
    children["States"] = states

    for tag in sorted(children):
        dev.append(children[tag])
    return dev


def indent(elem, level):
    pad = "\n" + "\t" * level
    if len(elem):
        elem.text = pad + "\t"
        for child in elem:
            indent(child, level + 1)
            child.tail = pad + "\t"
        elem[-1].tail = pad
    return elem


def collect_references(root, ids):
    """(where, device id, state) for every state reference to a converted device."""
    refs = []
    for sec in root:
        if sec.tag == "DeviceList":
            continue
        for parent in sec.iter():
            dev_id = parent.findtext("DeviceID") or parent.findtext("DevID") or parent.findtext("TargetElemID")
            if dev_id not in ids:
                continue
            state = (parent.findtext("DeviceStateSelector") or parent.findtext("DevState")
                     or parent.findtext("TargetElemSubKey"))
            name = None
            for owner in sec:
                if parent in owner.iter():
                    name = owner.findtext("Name")
                    break
            refs.append((sec.tag.replace("List", ""), name, dev_id, state or ""))
        for e in sec.iter():
            if e.text and "states[" in e.text:
                for dev_id in ids:
                    if dev_id in e.text:
                        for st in set(re.findall(r'states\[["\'](\w+)["\']\]', e.text)):
                            refs.append((sec.tag.replace("List", "") + " script", None, dev_id, st))
    return refs


def _block(raw, open_tag, close_tag, obj_id, section):
    """(start, end) of the element in `section` whose own <ID> is obj_id."""
    sec_start = raw.find("<" + section)
    sec_end = raw.find("</" + section + ">", sec_start)
    marker = '<ID type="integer">{0}</ID>'.format(obj_id)
    pos = raw.find(marker, sec_start, sec_end)
    if pos < 0:
        return None
    start = raw.rfind(open_tag, sec_start, pos)
    end = raw.find(close_tag, pos) + len(close_tag)
    return start, end


def rewrite_scripts(raw, ids, report):
    """Change old command wording in embedded scripts that read a converted device."""
    def fix_script(match):
        body = match.group(2)
        if not any("indigo.devices[{0}]".format(i) in body for i in ids):
            return match.group(0)
        changed = body
        for old, new in OLD_TO_NEW_COMMAND.items():
            pat = r'(states\[(?:&quot;|")command(?:&quot;|")\]\s*[=!]=\s*(?:&quot;|")){0}(&quot;|")'.format(re.escape(old))
            changed, n = re.subn(pat, lambda m: m.group(1) + new + m.group(2), changed)
            if n:
                report.append((old, new, n))
        return match.group(1) + changed + match.group(3)
    return re.sub(r'(<ScriptSource type="string">)(.*?)(</ScriptSource>)', fix_script, raw, flags=re.S)


def drop_legacy(raw, root, ids, report):
    """Move lastUpdated triggers to lastSeen and remove the compatibility state."""
    for trig in root.find("TriggerList"):
        if trig.findtext("DeviceID") in ids and trig.findtext("DeviceStateSelector") == "lastUpdated":
            span = _block(raw, '<Trigger type="dict">', "</Trigger>", trig.findtext("ID"), "TriggerList")
            if span is None:
                continue
            block = raw[span[0]:span[1]]
            new = block.replace('<DeviceStateSelector type="string">lastUpdated</DeviceStateSelector>',
                                '<DeviceStateSelector type="string">lastSeen</DeviceStateSelector>', 1)
            raw = raw[:span[0]] + new + raw[span[1]:]
            report.append("trigger '{0}' now fires on lastSeen".format(trig.findtext("Name")))
    return raw


def edit_device(raw, dev_id, fn):
    span = _block(raw, '<Device type="dict">', "</Device>", dev_id, "DeviceList")
    block = raw[span[0]:span[1]]
    if block.count("<Device ") != 1:
        raise SystemExit("!! couldn't isolate device {0} safely - nothing written".format(dev_id))
    elem = ET.fromstring(block)
    fn(elem)
    indent(elem, 2)
    elem.tail = None
    return raw[:span[0]] + ET.tostring(elem, encoding="unicode") + raw[span[1]:]


def set_prop(elem, key, value):
    ours = elem.find("MetaProps/" + NEW_PLUGIN_ID)
    node = ours.find(key)
    if node is None:
        ours.append(typed(key, value))
    else:
        repl = typed(key, value)
        node.attrib = repl.attrib
        node.text = repl.text
    for c in sorted(list(ours), key=lambda e: e.tag):
        ours.remove(c)
        ours.append(c)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("database")
    ap.add_argument("-o", "--output", help="write the converted database here (otherwise dry run)")
    ap.add_argument("--drop-legacy", action="store_true",
                    help="move 'lastUpdated' triggers to 'lastSeen' and remove the old compatibility state")
    ap.add_argument("--set-code", action="append", default=[], metavar="ID=KEY",
                    help="set a converted device's RF code, e.g. 123456789=14:00:a1b2c3:16")
    ap.add_argument("--local-only", default="", metavar="IDS",
                    help="comma-separated device IDs to make local-only (no RF sent), e.g. virtual flags")
    args = ap.parse_args()

    raw = open(args.database, "r", encoding="utf-8").read()
    root = ET.fromstring(raw)
    types = device_types()
    dlist = root.find("DeviceList")
    olds = [d for d in dlist if d.findtext("PluginID") == MIG.OLD_PLUGIN_ID]
    previously = {d.findtext("ID") for d in dlist if d.findtext("PluginID") == NEW_PLUGIN_ID and
                  d.findtext("MetaProps/{0}/migratedFrom".format(NEW_PLUGIN_ID)) == "database"}

    local_only = {x.strip() for x in args.local_only.split(",") if x.strip()}
    plans = {}
    print("{0} device(s) from the old RFXCOM plugin:\n".format(len(olds)))
    for d in sorted(olds, key=lambda x: (x.findtext("TypeIdPlugin"), x.findtext("Name"))):
        meta = d.find("MetaProps/" + MIG.OLD_PLUGIN_ID)
        props = {c.tag: c.text for c in meta} if meta is not None else {}
        states = {c.tag: c.text for c in d.find("States")} if d.find("States") is not None else {}
        p = MIG.plan_device(d.findtext("ID"), d.findtext("Name"), d.findtext("TypeIdPlugin"), props, states)
        if p.old_id in local_only and p.new_type in ("rfxSwitch", "rfxDimmer"):
            p.role = "button"
            p.note = (p.note + "; " if p.note else "") + "local-only by request"
        plans[p.old_id] = p
        print("  [{0}] {1}\n      {2} -> {3}".format(d.findtext("ID"), p.old_name, p.old_type, p.describe()))

    convert_ids = {i for i, p in plans.items() if p.supported}
    skipped = [p for p in plans.values() if not p.supported]
    all_ids = convert_ids | previously
    if previously:
        print("{0} device(s) converted earlier (references will be updated).".format(len(previously)))
    if not all_ids:
        print("Nothing from the old RFXCOM plugin in this database.")
        return 0

    out = raw
    for d in olds:
        dev_id = d.findtext("ID")
        if dev_id in convert_ids:
            out = edit_device(out, dev_id, lambda e, i=dev_id: convert_device(e, plans[i], types))

    for item in args.set_code:
        dev_id, key = item.split("=", 1)
        if dev_id not in all_ids:
            raise SystemExit("!! --set-code: {0} isn't a converted device".format(dev_id))
        ident = key.split(":", 2)[2]

        def apply(e, key=key, ident=ident):
            for k, v in (("deviceKey", key), ("protocol", key[:5]), ("idString", ident), ("address", ident),
                         ("pendingPatterns", "")):
                set_prop(e, k, v)
        out = edit_device(out, dev_id, apply)
        print("code for {0} set to {1}".format(dev_id, key))

    script_report = []
    out = rewrite_scripts(out, all_ids, script_report)
    trig_report = []
    if args.drop_legacy:
        out = drop_legacy(out, ET.fromstring(out), all_ids, trig_report)

        def no_legacy(e):
            set_prop(e, "legacyStates", False)
            states = e.find("States")
            if states is not None:
                for c in list(states):
                    if c.tag == "lastUpdated":
                        states.remove(c)
        for dev_id in all_ids:
            out = edit_device(out, dev_id, no_legacy)

    # check references against what the devices will have
    check_root = ET.fromstring(out)
    legacy_ids = {d.findtext("ID") for d in check_root.find("DeviceList")
                  if d.findtext("ID") in all_ids and
                  d.findtext("MetaProps/{0}/legacyStates".format(NEW_PLUGIN_ID)) == "true"}
    new_types = {d.findtext("ID"): d.findtext("TypeIdPlugin") for d in check_root.find("DeviceList")
                 if d.findtext("ID") in all_ids}
    names = {d.findtext("ID"): d.findtext("Name") for d in check_root.find("DeviceList")}
    print("\nReferences to converted devices that name a state:")
    problems = 0
    for where, owner, dev_id, state in sorted(set(collect_references(check_root, all_ids)), key=lambda r: (r[2], r[0])):
        if not state:
            continue
        known = BASE_STATES | TYPE_STATES.get(new_types.get(dev_id), set()) | \
            (LEGACY_STATES if dev_id in legacy_ids else set())
        ok = state in known or state.endswith(".ui")
        problems += 0 if ok else 1
        print("  {0} {1:18} {2:12} {3}{4}".format("ok " if ok else "!! ", where, state, names.get(dev_id),
                                                  " (in '{0}')".format(owner) if owner else ""))
    if script_report:
        print("\nScript changes:")
        for old, new, n in script_report:
            print("  \"{0}\" -> \"{1}\" ({2}x)".format(old, new, n))
    for line in trig_report:
        print("  " + line)

    print("\nSummary: {0} converted now, {1} converted earlier, {2} left on the old plugin, "
          "{3} state reference(s) to check.".format(len(convert_ids), len(previously), len(skipped), problems))

    if not args.output:
        print("\nDry run - nothing written. Add -o OUTPUT to write a converted copy.")
        return 0

    check = ET.fromstring(out)
    if len(check.find("DeviceList")) != len(dlist) or \
            [d.findtext("ID") for d in check.find("DeviceList")] != [d.findtext("ID") for d in dlist]:
        print("!! verification failed - nothing written")
        return 2
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(out)
    print("\nWrote {0} (all device IDs unchanged).".format(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
