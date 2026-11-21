"""
Drives the real RFXCOM plugin.py against a stubbed `indigo` module.

RF packets are parsed by the real pyRFXtrx; transmission goes to a fake transport
that records bytes and answers with ACK/NAK. The firmware flasher is run
against a fake bootloader speaking the STX/DLE/ETX protocol.

This proves the plugin's own logic and its use of Indigo's API as I understand
it - not that the real Indigo host or real hardware behaves identically.
"""
import os
import random
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(__file__)
SP = next(p for p in (os.path.join(HERE, "..", "Contents", "Server Plugin"),
                         os.path.join(HERE, "..", "RFXCom433.indigoPlugin", "Contents", "Server Plugin"))
           if os.path.isdir(p))
sys.path.insert(0, os.path.abspath(SP))

# ---------------------------------------------------------------- indigo stub
indigo = types.ModuleType("indigo")


class Dict(dict):
    pass


class StateImage:
    Auto = "Auto"
    TemperatureSensor = "TemperatureSensor"
    HumiditySensor = "HumiditySensor"


class DevAction:
    TurnOn, TurnOff, Toggle, SetBrightness, BrightenBy, DimBy, AllLightsOn, AllLightsOff, AllOff, RequestStatus = range(10)


class Device:
    def __init__(self, id, name, typeId, props, address="", pluginId="com.durosity.indigoplugin.rfxcom"):
        self.pluginId = pluginId
        self.folderId = 0
        self.id = id
        self.name = name
        self.deviceTypeId = typeId
        self._props = dict(props)
        self.enabled = True
        self.address = address
        self.states = {}
        self.error = None
        self.image = None
        self._reset_states()

    def _reset_states(self):
        base = {"command": "", "lastCommand": "", "lastSeen": "", "lastSeenTimestamp": 0, "online": True, "signalStrength": 0}
        if self.deviceTypeId in ("rfxSwitch", "rfxCover"):
            base["onOffState"] = False
        if self.deviceTypeId == "rfxDimmer":
            base["onOffState"] = False
            base["brightnessLevel"] = 0
        if self.deviceTypeId == "rfxCover":
            base["coverState"] = ""
        if self._props.get("legacyStates"):
            base.update({"lastUpdated": ""})
        if self.deviceTypeId == "rfxSensor":
            base["sensorValue"] = 0
            for k in [k for k in self._props.get("valueKeys", "").split(",") if k]:
                base[k] = 0
        if self.deviceTypeId in ("rfxSecurity", "rfxDoorbell"):
            base["onOffState"] = False
        if self.deviceTypeId == "rfxSecurity":
            base.update({"status": "", "tamper": False})
        if self.deviceTypeId == "rfxInterface":
            for k in ("connectionState port transceiverType firmwareVersion firmwareType hardwareVersion outputPower "
                      "noiseLevel enabledProtocols receiverStarted lastConnected packetsReceived packetsSent "
                      "txFailures lastPacketTime lastPacketHex lastReceived lastNewSignal lastNewSignalKey "
                      "firmwareUpdateStatus").split():
                base[k] = 0 if k in ("packetsReceived", "packetsSent", "txFailures") else ""
        old = getattr(self, "states", {})
        self.states = {k: old.get(k, v) for k, v in base.items()}

    @property
    def pluginProps(self):
        return Dict(self._props)

    @property
    def onState(self):
        return self.states.get("onOffState")

    @property
    def brightness(self):
        return self.states.get("brightnessLevel", 0)

    @property
    def ownerProps(self):
        return Dict(self._props)

    def replaceOnServer(self):
        pass

    def replacePluginPropsOnServer(self, p):
        self._props = dict(p)

    def stateListOrDisplayStateIdChanged(self):
        self._reset_states()

    def updateStatesOnServer(self, lst):
        for s in lst:
            if s["key"] not in self.states and s["key"] != "batteryLevel":
                raise KeyError("unknown state " + s["key"])
            self.states[s["key"]] = s["value"]
            if s["key"] == "brightnessLevel":
                self.states["onOffState"] = s["value"] > 0
        self.error = None

    def updateStateOnServer(self, k, v):
        self.updateStatesOnServer([{"key": k, "value": v}])

    def setErrorStateOnServer(self, e):
        self.error = e

    def updateStateImageOnServer(self, i):
        self.image = i


class Devices(dict):
    folders = {}

    def __init__(self):
        super().__init__()
        self.next_id = 1000

    def __getitem__(self, k):
        if isinstance(k, str):
            for d in self.values():
                if d.name == k:
                    return d
            raise KeyError(k)
        return dict.__getitem__(self, k)

    def __contains__(self, k):
        if isinstance(k, str):
            return any(d.name == k for d in self.values())
        return dict.__contains__(self, k)

    def iter(self, f="self"):
        out = list(self.values())
        if "." in f:
            t = f.split(".", 1)[1]
            out = [d for d in out if d.deviceTypeId == t]
        return iter(out)


devices = Devices()


class Folder:
    def __init__(self, id):
        self.id = id


class FolderNS:
    @staticmethod
    def create(name):
        Devices.folders[name] = Folder(900 + len(Devices.folders))


class DeviceNS:
    folder = FolderNS

    @staticmethod
    def create(protocol=None, address="", name="", description="", pluginId="", deviceTypeId="", props=None, folder=0):
        if name in devices:
            raise ValueError("name exists")
        devices.next_id += 1
        d = Device(devices.next_id, name, deviceTypeId, props or {}, address)
        dict.__setitem__(devices, d.id, d)
        PLUGIN.deviceStartComm(d)
        return d

    @staticmethod
    def getDependencies(dev_id):
        return {"triggers": [1, 2]} if dev_id == 555 else {}

    @staticmethod
    def changeDeviceTypeId(dev, t):
        PLUGIN.deviceStopComm(dev)
        d = devices[dev.id]
        d.deviceTypeId = t
        d._reset_states()
        PLUGIN.deviceStartComm(d)
        return d


fired = []


class TriggerNS:
    @staticmethod
    def execute(t):
        fired.append(t.pluginTypeId)


class ServerNS:
    @staticmethod
    def savePluginPrefs():
        pass

    @staticmethod
    def log(*a, **k):
        pass


class PluginBase:
    class StopThread(Exception):
        pass

    def __init__(self, pid, name, ver, prefs):
        import logging
        self.pluginId = pid
        self.pluginPrefs = Dict(prefs)
        self.logger = logging.getLogger("Plugin")
        self.logs = []
        h = logging.Handler()
        h.emit = lambda r: self.logs.append((r.levelname, r.getMessage()))
        self.logger.addHandler(h)
        self.logger.setLevel(logging.DEBUG)

    def getDeviceStateList(self, dev):
        return []

    def getDeviceStateDictForNumberType(self, k, a, b):
        return {"Key": k, "Type": "Number"}

    def getDeviceStateDictForStringType(self, k, a, b):
        return {"Key": k, "Type": "String"}

    def deviceDeleted(self, dev):
        self.deviceStopComm(dev)

    def getSerialPortUrl(self, props, fid):
        return props.get("url", "")

    def validateSerialPortUi(self, v, e, f):
        pass

    def sleep(self, s):
        time.sleep(s)


indigo.PluginBase = PluginBase
indigo.Dict = Dict
indigo.devices = devices
indigo.device = DeviceNS
indigo.trigger = TriggerNS
indigo.server = ServerNS
indigo.kStateImageSel = StateImage
indigo.kDeviceAction = DevAction
indigo.kUniversalAction = types.SimpleNamespace(RequestStatus=99)
indigo.kProtocol = types.SimpleNamespace(Plugin="plugin")
sys.modules["indigo"] = indigo

import RFXtrx  # noqa
import plugin as P  # noqa
import rfx_firmware as FW  # noqa
import rfx_mapping as M  # noqa


# ---------------------------------------------------------------- fake transport
class FakeTransport:
    def __init__(self):
        self.sent = []
        self.nak = False
        self.packets_out = 0

    def send(self, data):
        self.sent.append(bytes(data))
        self.packets_out += 1

    def tx_result(self):
        return (False, "NAK - transmitter did not lock") if self.nak else (True, "ACK")


def rx(hexstr):
    pkt = bytearray(bytes.fromhex(hexstr.replace(" ", "")))
    ev = RFXtrx.RFXtrxTransport.parse(pkt)
    assert ev is not None, hexstr
    PLUGIN.events.put(("event", ev))
    pump()


def pump():
    while not PLUGIN.events.empty():
        PLUGIN._process_item(PLUGIN.events.get())
    PLUGIN._service_momentary()


def wait_tx(n, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if len(FT.sent) >= n and PLUGIN.txq.empty():
            time.sleep(0.15)
            return
        time.sleep(0.02)


def by_name_prefix(prefix):
    return [d for d in devices.values() if d.name.startswith(prefix)]


def trig(id_, type_, **props):
    t = types.SimpleNamespace(id=id_, name=type_, pluginTypeId=type_, pluginProps=props)
    PLUGIN.triggerStartProcessing(t)


ok_count = 0


def check(cond, msg):
    global ok_count
    if not cond:
        raise AssertionError(msg)
    ok_count += 1
    print("  ok -", msg)


# ---------------------------------------------------------------- tests
PLUGIN = P.Plugin("com.durosity.indigoplugin.rfxcom", "RFXCOM", "4.0.0",
                  {"autoCreate": "controls", "minSightingsControls": "1", "minSightingsSensors": "2",
                   "folderName": "RFXCOM", "dedupeSeconds": "0.5", "url": "/dev/null"})
PLUGIN.startup()
FT = FakeTransport()
PLUGIN.transport = FT
PLUGIN.connected = True

print("interface device")
check(len(list(devices.iter("self.rfxInterface"))) == 1, "interface device auto-created")

print("AC / HomeEasy UK remote -> auto-created switch")
# Lighting2 AC: len 0B, type 11, sub 00, seq, id 01 23 45 67 -> id_combined 0x1234567, unit 03, cmd 01 (On), level 0F, rssi 70
rx("0B 11 00 01 01 23 45 67 03 01 0F 70")
sw = by_name_prefix("AC 1234567:3")
check(len(sw) == 1 and sw[0].deviceTypeId == "rfxSwitch", "AC switch created")
sw = sw[0]
check(sw.states["onOffState"] is True, "state on from remote")
check(sw.states["signalStrength"] == 7, "rssi recorded")
check("commandReceived" not in fired, "no triggers yet registered")

trig(1, "commandReceived", deviceId=str(sw.id), command="on")
trig(2, "commandReceived", deviceId="any", command="any")
rx("0B 11 00 02 01 23 45 67 03 01 0F 70")       # duplicate within window
check(fired.count("commandReceived") == 0, "duplicate press suppressed")
PLUGIN.recent.clear()
rx("0B 11 00 03 01 23 45 67 03 01 0F 70")       # same On again (state unchanged) -> trigger still fires
check(fired.count("commandReceived") == 2, "trigger fires on repeat press with no state change")
rx("0B 11 00 04 01 23 45 67 03 00 0F 70")       # Off
check(sw.states["onOffState"] is False, "off from remote")

print("same remote, second unit + group off")
rx("0B 11 00 05 01 23 45 67 04 01 0F 70")
sw4 = devices["AC 1234567:4"]
check(sw4.states["onOffState"], "unit 4 on")
rx("0B 11 00 01 01 23 45 67 03 01 0F 70")
PLUGIN.recent.clear()
rx("0B 11 00 06 01 23 45 67 03 03 0F 70")       # group off sent from unit 3
check(not sw.states["onOffState"] and not sw4.states["onOffState"], "group off turned off both units")

print("transmit from the same device (plug learned the remote)")
PLUGIN.actionControlDevice(types.SimpleNamespace(deviceAction=DevAction.TurnOn, actionValue=0), sw)
wait_tx(1)
check(FT.sent[-1] == bytes.fromhex("0B 11 00 00 01 23 45 67 03 01 00 00".replace(" ", "")), "AC On packet built correctly")
check(sw.states["onOffState"] is True, "state updated after ACK")

FT.nak = True
PLUGIN.actionControlDevice(types.SimpleNamespace(deviceAction=DevAction.TurnOff, actionValue=0), sw)
wait_tx(2)
check(sw.states["onOffState"] is True, "NAK leaves state unchanged")
check(any("NAK" in m for lvl, m in PLUGIN.logs if lvl == "ERROR"), "NAK logged as error")
FT.nak = False

print("dim level heard -> auto-upgrade to dimmer")
PLUGIN.recent.clear()
rx("0B 11 00 07 01 23 45 67 03 02 07 70")       # set level 7 -> (7+1)*100//16 = 50
sw = devices[sw.id]
check(sw.deviceTypeId == "rfxDimmer", "converted to dimmer")
check(sw.states["brightnessLevel"] == 50, "level 50%")
PLUGIN.actionControlDevice(types.SimpleNamespace(deviceAction=DevAction.SetBrightness, actionValue=75), sw)
wait_tx(3)
check(FT.sent[-1][9] == 0x02 and FT.sent[-1][10] == ((75 + 6) * 16 // 100) - 1, "dim packet level byte")
check(sw.states["brightnessLevel"] == 75, "brightness 75 after ACK")
check(sw.pluginProps["lastLevel"] == "75", "last level remembered")

print("LightwaveRF")
# Lighting5: len 0A, type 14, sub 00, seq, id f1 a2 b3, unit 01, cmd 01, level 00, rssi 60
rx("0A 14 00 01 F1 A2 B3 01 01 00 60")
lw = devices["LightwaveRF, Siemens f1a2b3:1"]
check(lw.states["onOffState"], "LW switch created and on")
PLUGIN.actionLightwave(types.SimpleNamespace(props={"lwCommand": "mood2"}), lw)
wait_tx(4)
check(FT.sent[-1][8] == 0x04, "LW mood 2 command byte")
print("command clears after X seconds, lastCommand stays")
p = lw.pluginProps
p["clearCommandSeconds"] = "2"
lw.replacePluginPropsOnServer(p)
PLUGIN.recent.clear()
rx("0A 14 00 02 F1 A2 B3 01 05 00 60")          # LightwaveRF Mood3 from the remote
check(lw.states["command"] == "Mood3" and lw.states["lastCommand"] == "Mood3", "Mood3 in both states")
check(lw.states["onOffState"] is True, "mood press doesn't change on/off")
PLUGIN._service_momentary()
check(lw.states["command"] == "Mood3", "still there before the delay")
PLUGIN.pending_clear[lw.id] = time.time() - 0.1
PLUGIN._service_momentary()
check(lw.states["command"] == "" and lw.states["lastCommand"] == "Mood3", "command blanked, lastCommand kept")
PLUGIN.recent.clear()
rx("0A 14 00 03 F1 A2 B3 01 05 00 60")
check(lw.states["command"] == "Mood3" and lw.id in PLUGIN.pending_clear, "same command shows again and re-arms")
p["clearCommandSeconds"] = "0"
lw.replacePluginPropsOnServer(p)
PLUGIN.recent.clear()
rx("0A 14 00 04 F1 A2 B3 01 05 00 60")
check(lw.id not in PLUGIN.pending_clear, "0 seconds keeps the command")

n = len(FT.sent)
PLUGIN.actionPair(types.SimpleNamespace(props={}), lw)
wait_tx(n + 3, timeout=8)
check(len(FT.sent) == n + 3, "pairing sends On three times")

print("receive-only button")
rx("0A 14 00 01 11 22 33 02 01 00 60")
btn = devices["LightwaveRF, Siemens 112233:2"]
p = btn.pluginProps
p["role"] = "button"
btn.replacePluginPropsOnServer(p)
n = len(FT.sent)
PLUGIN.actionControlDevice(types.SimpleNamespace(deviceAction=DevAction.TurnOff, actionValue=0), btn)
time.sleep(0.3)
check(len(FT.sent) == n and btn.states["onOffState"] is False, "button role: no transmit, state toggled locally")

print("transmit-only role ignores RX")
p["role"] = "transmit"
btn.replacePluginPropsOnServer(p)
PLUGIN.recent.clear()
rx("0A 14 00 01 11 22 33 02 01 00 60")
check(btn.states["onOffState"] is False, "transmit-only ignored received On")

print("sensors need 2 sightings and autoCreate=all")
# Oregon TempHumid 0x52: len 0A, type 52, sub 01, seq, id 9b 03, temp 00 d7 (21.5C), hum 37 (55%), status 02, batt/rssi 69
rx("0A 52 01 01 9B 03 00 D7 37 02 69")
check(not by_name_prefix("THGN122/123, THGN132, THGR122/228/238/268 9b:03"), "sensor not created in 'controls' mode")
PLUGIN.pluginPrefs["autoCreate"] = "all"
PLUGIN.recent.clear()
rx("0A 52 01 02 9B 03 00 D7 37 02 69")
sens = [d for d in devices.values() if d.deviceTypeId == "rfxSensor"]
check(len(sens) == 1, "sensor auto-created on 2nd sighting")
s = sens[0]
check(abs(s.states["temperature"] - 21.5) < 0.01 and s.states["humidity"] == 55, "temp + humidity states")
check(abs(s.states["sensorValue"] - 21.5) < 0.01, "sensorValue = temperature")
check(s.states.get("batteryLevel") == 100, "battery 100% from nibble 9")
check(s.image == "TemperatureSensor", "temperature icon")
states = [x["Key"] for x in PLUGIN.getDeviceStateList(s)]
check("temperature" in states and "humidityStatus" in states, "dynamic state list")

PLUGIN.pluginPrefs["tempUnits"] = "F"
PLUGIN.recent.clear()
rx("0A 52 01 03 9B 03 00 D7 37 02 69")
check(abs(s.states["temperature"] - 70.7) < 0.01, "Fahrenheit conversion")
PLUGIN.pluginPrefs["tempUnits"] = "C"

print("offline detection")
s.states["lastSeenTimestamp"] = int(time.time()) - 200 * 60
trig(3, "deviceOffline", deviceId="any")
trig(4, "deviceOnline", deviceId="any")
PLUGIN._health_check()
check(s.states["online"] is False and s.error == "offline", "marked offline with error state")
check("deviceOffline" in fired, "offline trigger")
PLUGIN.recent.clear()
rx("0A 52 01 04 9B 03 00 D7 37 02 69")
check(s.states["online"] is True and "deviceOnline" in fired, "back online + trigger")

print("delete -> ignored, not re-created")
PLUGIN.deviceDeleted(lw)
dict.__delitem__(devices, lw.id)
PLUGIN.recent.clear()
rx("0A 14 00 05 F1 A2 B3 01 01 00 60")
check(not by_name_prefix("LightwaveRF, Siemens f1a2b3:1"), "deleted device stays deleted")
check(M.make_key(0x14, 0, "f1a2b3:1") in PLUGIN._ignored(), "key in ignore list")

print("create from heard + new code")
v = PLUGIN.createFromHeard(Dict(heardKey=M.make_key(0x14, 0, "f1a2b3:1"), createType="rfxDimmer", createName="Lounge Lamp"))
check(devices["Lounge Lamp"].deviceTypeId == "rfxDimmer", "manual create from heard code: " + v["createResult"])
check(M.make_key(0x14, 0, "f1a2b3:1") not in PLUGIN._ignored(), "un-ignored on manual create")
v = PLUGIN.createNewCode(Dict(newProtocol="ac", newUnit="5", newDimmable=False, newName="Garden Socket"))
g = devices["Garden Socket"]
check(g.pluginProps["deviceKey"].startswith("11:00:") and g.pluginProps["deviceKey"].endswith(":5"), "new AC code created")
RFXtrx.get_device(0x11, 0, g.pluginProps["idString"])
check(True, "new code is valid for pyRFXtrx")

print("device config validation")
ok, vd, *err = PLUGIN.validateDeviceConfigUi(Dict(protocol="11:00", idString="zzz"), "rfxSwitch", 0)
check(not ok, "invalid id rejected")
ok, vd = PLUGIN.validateDeviceConfigUi(Dict(protocol="11:00", idString="0abcdef:2"), "rfxSwitch", 0)
check(ok and vd["deviceKey"] == "11:00:0abcdef:2", "valid id accepted -> key built")

print("security + doorbell")
# Security1 0x20: len 08, type 20, sub 05 (Visonic motion), seq, id 12 34 56, status 04 (motion), batt/rssi 59
rx("08 20 05 01 12 34 56 04 59")
sec = [d for d in devices.values() if d.deviceTypeId == "rfxSecurity"][0]
check(sec.states["onOffState"] is True and sec.states["status"] == "Motion", "motion detected")
PLUGIN.recent.clear()
rx("08 20 05 02 12 34 56 05 59")
check(sec.states["onOffState"] is False, "no motion")
# Chime 0x16: len 07, type 16, sub 00 (Byron SX), seq, id 00 1A, sound 05, rssi 70
rx("07 16 00 01 00 1A 05 70")
bell = [d for d in devices.values() if d.deviceTypeId == "rfxDoorbell"][0]
check(bell.states["onOffState"] is True, "doorbell pressed")
PLUGIN.pending_off[bell.id] = time.time() - 1
PLUGIN._service_momentary()
check(bell.states["onOffState"] is False, "doorbell auto-reset")

print("unsupported packet type + raw send")
PLUGIN.events.put(("unsupported", bytes.fromhex("0740000102030405AA")))
pump()
check(any("0x40" in m for _, m in PLUGIN.logs), "unsupported packet logged")
n = len(FT.sent)
PLUGIN.actionRaw(types.SimpleNamespace(props={"packet": "11 00 00 01 23 45 67 03 01 00 00"}))
wait_tx(n + 1)
check(FT.sent[-1][0] == 0x0B, "raw packet length byte added")

print("migration from the old plugin")
OLD = "nl.rjdekok.indigoplugin.RFXCOM"
def old_dev(i, name, t, props, on=False):
    d = Device(i, name, t, props, pluginId=OLD)
    d.states["onOffState"] = on
    dict.__setitem__(devices, i, d)
    return d
old_dev(555, "Kitchen Lamp", "ACDimmer", {"address": "02ABCDEF", "unit": "1"}, on=True)
old_dev(556, "Kitchen Remote", "ACSwitch", {"address": "20abcdef", "unit": "1"})
old_dev(557, "Hall Plug", "HEAppliance", {"address": "00112233", "unit": "2"})
old_dev(558, "Garage Light", "LWAppliance", {"address": "C0FFEE", "unit": "4"})
old_dev(559, "Bedroom Button", "ACSwitch", {"address": "30501020", "unit": "5"})
old_dev(560, "Lounge Temp", "Temperature", {"sensorNumber": str(0x012a), "sensorValueType": "Temp"})
old_dev(561, "LW Handset", "LWRemote", {"address": "aabbcc"})
old_dev(562, "Original AC", "ACAppliance", {"address": "01234567", "unit": "3"})
existing_ac = PLUGIN.devices_by_key["11:00:1234567:3"]

v = PLUGIN.migratePreview(Dict())
check("7 to migrate" in v["migrateResult"] and "1 not supported" in v["migrateResult"], "preview: " + v["migrateResult"])
check(any("used by: 2 triggers" in m for _, m in PLUGIN.logs), "preview shows what uses each old device")
v = PLUGIN.migrateRun(Dict(migrateRename=True, migrateStates=True))
check("Created 6, reused 1, failed 0" in v["migrateResult"], "migrate: " + v["migrateResult"])
lamp = devices["Kitchen Lamp"]
check(lamp.deviceTypeId == "rfxDimmer" and lamp.pluginProps["deviceKey"] == "11:00:2abcdef:1", "AC dimmer key exact")
check(devices[555].name == "Kitchen Lamp (old)", "old device renamed")
check(lamp.states["onOffState"] is True, "on/off copied")
remote = devices["Kitchen Remote"]
check(remote.deviceTypeId == "rfxRemote" and remote.pluginProps["deviceKey"] == "", "remote waits to learn its code")
PLUGIN.recent.clear()
rx("0B 11 00 0A 02 AB CD EF 01 00 0F 70")       # the remote sends Off with the lamp's code
check(remote.pluginProps["deviceKey"] == "11:00:2abcdef:1", "remote learned the code it shares with the lamp")
check(lamp.states["onOffState"] is False, "lamp follows the same press")
check(remote.states["command"] == "Off" and remote.states["lastCommand"] == "Off" and remote.states["lastUpdated"], "command, lastCommand and legacy lastUpdated on the remote")
check(devices["Hall Plug"].pluginProps["deviceKey"] == "11:01:0112233:2", "HomeEasy -> Lighting2 subtype 1")
check(devices["Garage Light"].pluginProps["deviceKey"] == "14:00:c0ffee:4", "LightwaveRF key")
check(devices["Original AC"].id == existing_ac, "existing auto-created device reused and renamed")
btn = devices["Bedroom Button"]
check(btn.deviceTypeId == "rfxRemote" and btn.pluginProps["deviceKey"] == "", "ambiguous remote waits to hear its code")
PLUGIN.recent.clear()
rx("0B 11 00 09 03 05 10 20 05 01 0F 70")       # the real remote: id bytes 03 05 10 20, unit 5
check(btn.pluginProps["deviceKey"] == "11:00:3051020:5" and btn.states["command"] == "On", "remote learned its real code on first press")
check(not by_name_prefix("AC 3051020:5"), "no duplicate auto-created")
rx("0A 52 01 05 2A 01 00 C8 32 02 69")          # Oregon temp+hum id 2a:01, 20.0C
t = devices["Lounge Temp"]
check(t.pluginProps["deviceKey"] == "52:01:2a:01" and abs(t.states["temperature"] - 20.0) < 0.01, "temperature sensor learned + reading")
check(t.states["lastUpdated"] != "", "sensor keeps the legacy lastUpdated state")
v = PLUGIN.migrateRun(Dict(migrateRename=True, migrateStates=True))
check("Created 0, reused 0" in v["migrateResult"], "running again does nothing")

print("LightwaveRF mood strings")
old_dev(570, "Master Switch", "LWSwitch", {"address": "d7c6b5", "unit": "16"})
PLUGIN.migrateRun(Dict(migrateRename=True, migrateStates=True))
ms = devices["Master Switch"]
check(ms.pluginProps["deviceKey"] == "14:00:d7c6b5:16", "unambiguous LW switch gets its exact code")
PLUGIN.recent.clear()
rx("0A 14 00 06 D7 C6 B5 10 07 00 60")          # Mood 5 (cmd 0x07) on unit 16
check(ms.states["command"] == "Mood5" and ms.states["lastCommand"] == "Mood5", "command and lastCommand both get Mood5")
PLUGIN.recent.clear()
rx("0A 14 00 07 D7 C6 B5 10 02 00 60")
check(ms.states["command"] == "Group off", "Group off string")

print("dynamic lists never return empty IDs")
for fn in (PLUGIN.heardList, PLUGIN.protocolList, PLUGIN.primaryValueList, PLUGIN.ignoredList, PLUGIN.rfDeviceList):
    for filt in ("", "transmit", "sensor"):
        ids = [i for i, _ in fn(filter=filt, valuesDict=Dict(), typeId="", targetId=s.id)]
        check(all(i != "" for i in ids), "{0}({1}): {2} options, none empty".format(fn.__name__, filt, len(ids)))
PLUGIN.pluginPrefs["ignoredKeys"] = ""
check(PLUGIN.ignoredList()[0][0] == "none", "empty ignore list uses a real placeholder id")
v = PLUGIN.useHeardCode(Dict(heardKey="none", protocol="11:00", idString="0abcdef:2"))
check(v["idString"] == "0abcdef:2", "'Use This Code' with nothing chosen leaves the fields alone")

print("heard codes stay pickable after auto-create")
PLUGIN.recent.clear()
rx("0A 14 00 08 A5 C3 E7 10 07 00 60")          # Mood5 from a code no device has yet
auto = devices["LightwaveRF, Siemens a5c3e7:16"]
opts = dict(PLUGIN.heardList(targetId=ms.id))
check("14:00:a5c3e7:16" in opts and "used by 'LightwaveRF, Siemens a5c3e7:16'" in opts["14:00:a5c3e7:16"],
      "auto-created code still listed, marked with its owner")
v = PLUGIN.useHeardCode(Dict(heardKey="14:00:a5c3e7:16", protocol="", idString=""))
ok, vd = PLUGIN.validateDeviceConfigUi(Dict(protocol=v["protocol"], idString=v["idString"]), "rfxRemote", ms.id)
check(ok and vd["deviceKey"] == "14:00:a5c3e7:16", "another device may take the same code")
p = ms.pluginProps; p.update(vd); ms.replacePluginPropsOnServer(p)
PLUGIN.deviceStopComm(ms); PLUGIN.deviceStartComm(ms)
PLUGIN.recent.clear()
rx("0A 14 00 09 A5 C3 E7 10 06 00 60")          # Mood4
check(ms.states["command"] == "Mood4" and auto.states["lastCommand"] == "Mood4", "both devices sharing the code update")

print("status decode")
info = M.decode_status(bytes.fromhex("1401000102530C2F2F2F0101021C0400000000000000"))
check(info["firmwareType"] == "Ext2" and info["firmwareVersion"] == 1012 and info["hardwareVersion"] == "1.2", "long status packet")
info = M.decode_status(bytes.fromhex("0D01000102535C00002F0000000000"[:28]))
check(info["firmwareVersion"] == 92 and info["firmwareType"] == "Ext", "short (old) status packet")

# ---------------------------------------------------------------- firmware
print("firmware: hex parsing")


def make_hex(path, start_word, nblocks, extra_outside=False):
    lines = []
    def rec(addr, rtype, data):
        b = bytes([len(data), (addr >> 8) & 0xFF, addr & 0xFF, rtype]) + data
        return ":" + (b + bytes([(-sum(b)) & 0xFF])).hex().upper()
    byte_addr = start_word * 2
    cur_high = -1
    rnd = random.Random(1)
    for blk in range(nblocks):
        for off in range(0, 256, 16):
            a = byte_addr + blk * 256 + off
            if a >> 16 != cur_high:
                cur_high = a >> 16
                lines.append(rec(0, 4, bytes([cur_high >> 8, cur_high & 0xFF])))
            data = bytes(rnd.choice([0x55, 0x04, 0x05, 0x00, 0xFF, 0x12]) for _ in range(16))
            lines.append(rec(a & 0xFFFF, 0, data))
    lines.append(rec(0, 1, b""))
    open(path, "w").write("\n".join(lines) + "\n")


tmp = tempfile.mkdtemp()
hexp = os.path.join(tmp, "RFXtrx433_Ext2_test.hex")
make_hex(hexp, 0x1800, 6)
blocks = FW.parse_hex(hexp)
check(sorted(blocks) == [0x1800 + i * 128 for i in range(6)], "rows keyed by word address (byte address / 2)")
bad = os.path.join(tmp, "bad.hex")
open(bad, "w").write(open(hexp).read().replace(":10", ":11", 1))
try:
    FW.parse_hex(bad)
    check(False, "corrupt file rejected")
except FW.FirmwareError:
    check(True, "corrupt file rejected")

gap = os.path.join(tmp, "gap.hex")
make_hex(gap, 0x1800, 1)
txt = open(gap).read().splitlines()
txt = [l for l in txt if not l.startswith(":10" + "3010")]   # drop 16 bytes mid-row
open(gap, "w").write("\n".join(txt) + "\n")
g = FW.parse_hex(gap)
check(list(g) == [0x1800] and g[0x1800][0x10:0x20] == b"\xff" * 16, "gap inside a row padded with 0xFF")
print("firmware: framing")
for payload in (bytes([0x55, 0x04, 0x05, 1, 2]), PKT := FW.MARK_VERIFIED, bytes(range(0, 255, 7))):
    check(FW.unframe(FW.frame(payload)) == payload, "frame/unframe roundtrip {0}...".format(payload[:5].hex()))


class FakeBootloaderSerial:
    """Speaks the bootloader protocol and keeps a flash image."""
    def __init__(self, *a, **k):
        self.inbuf = bytearray()
        self.out = bytearray()
        self.flash = {}
        self.erased = False
        self.in_boot = False
        self.fail_writes = 0
        FakeBootloaderSerial.last = self

    def write(self, data):
        self.inbuf += data
        while True:
            if FW.ETX not in self.inbuf:
                return
            # find complete frame (an unescaped ETX)
            i, esc = 2, False
            while i < len(self.inbuf):
                b = self.inbuf[i]
                if esc:
                    esc = False
                elif b == FW.DLE:
                    esc = True
                elif b == FW.ETX:
                    break
                i += 1
            if i >= len(self.inbuf):
                return
            frame_bytes = bytes(self.inbuf[:i + 1])
            del self.inbuf[:i + 1]
            p = FW.unframe(frame_bytes)
            if p is None:
                continue
            self.handle(p)

    def reply(self, payload):
        self.out += FW.frame(payload)

    def handle(self, p):
        if p == FW.ENTER_BOOTLOADER:
            self.in_boot = True
            return
        if not self.in_boot:
            return
        cmd = p[0]
        if cmd == 0x00 and p == FW.REQUEST_INFO:
            self.reply(bytes([0x00, 0x02, 0x05, 0x01]))
        elif cmd == FW.CMD_ERASEPM:
            self.erased = True
            self.flash.clear()
            self.reply(bytes([cmd]))
        elif cmd == FW.CMD_WRITEPM:
            if self.fail_writes:
                self.fail_writes -= 1
                return
            addr = p[2] | (p[3] << 8) | (p[4] << 16)
            self.flash[addr] = p[5:]
            self.reply(bytes([cmd]))
        elif cmd == 0x08:
            self.reply(bytes([0x08]))
        elif p == FW.RESET_TO_APP:
            self.in_boot = False

    def read(self, n=1):
        if not self.out:
            time.sleep(0.01)
            return b""
        chunk = bytes(self.out[:n])
        del self.out[:n]
        return chunk

    def flush(self):
        pass

    def close(self):
        pass


FW.WRITE_DELAY = 0.001
FW.READ_TIMEOUT = 0.05
shared = FakeBootloaderSerial()
FW.serial.serial_for_url = lambda *a, **k: shared
progress = []
ok = FW.Bootloader("/dev/cu.fake", progress=lambda p, m: progress.append(p)).flash(blocks, FW.PM_RANGE_433)
check(ok and shared.erased, "flash completed against fake bootloader")
check(shared.flash == blocks, "every block written byte-for-byte (incl. DLE-escaped bytes)")
check(progress[-1] == 100, "progress reached 100%")
check(not shared.in_boot, "reset sent at the end")

shared2 = FakeBootloaderSerial()
shared2.fail_writes = 25            # more than 20 retries on the first block
FW.serial.serial_for_url = lambda *a, **k: shared2
try:
    FW.Bootloader("/dev/cu.fake").flash(blocks, FW.PM_RANGE_433)
    check(False, "write failure raises")
except FW.FirmwareError as exc:
    check("still in its bootloader" in str(exc), "write failure raises with recovery advice")

print("firmware: plugin preflight")
PLUGIN.status_info = M.decode_status(bytes.fromhex("1401000102530C2F2F2F0101021C0400000000000000"))
ok, msg, _, _ = PLUGIN._firmware_preflight(hexp)
check(ok, "preflight OK for 433 Ext2: " + msg)
PLUGIN.pluginPrefs["url"] = "socket://10.0.0.5:4000"
ok, msg, _, _ = PLUGIN._firmware_preflight(hexp)
check(not ok and "local USB" in msg, "network port refused")
PLUGIN.pluginPrefs["url"] = "/dev/null"
PLUGIN.status_info["firmwareTypeCode"] = 0x10
ok, msg, _, _ = PLUGIN._firmware_preflight(hexp)
check(not ok and "Pro XL" in msg, "Pro XL refused")

PLUGIN.stop_event.set()
print("\nALL {0} CHECKS PASSED".format(ok_count))
