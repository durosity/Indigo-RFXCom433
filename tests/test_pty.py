"""Emulated RFXtrx on a pty: exercises IndigoRFXTransport + pyRFXtrx Connect + plugin connection/TX path."""
import os, pty, threading, time, sys, tty
sys.argv=["x"]
exec(open("test_plugin.py").read().split("# ---------------------------------------------------------------- tests")[0])

master, slave = pty.openpty()
tty.setraw(slave)
port = os.ttyname(slave)
log = []
NAK_NEXT = {"v": False}
STATE = {"modes": bytes([0x08,0x02,0x2F,0x00])}

def emulator():
    buf = bytearray()
    import select
    while True:
        if STATE.get("stop"):
            os.close(master); return
        r,_,_ = select.select([master],[],[],0.05)
        if not r: continue
        try:
            d = os.read(master, 256)
        except OSError:
            return
        buf += d
        while buf and len(buf) >= buf[0] + 1:
            pkt = bytes(buf[:buf[0]+1]); del buf[:buf[0]+1]
            log.append(pkt)
            if pkt[1] == 0x00:
                cmd = pkt[4]
                if cmd == 0x02 or cmd == 0x03:   # status / setmode -> long status
                    if cmd == 0x03: STATE["modes"] = pkt[7:11]
                    modes = STATE["modes"]
                    r = bytes([0x14,0x01,0x00,pkt[3],cmd,0x53,0x0C]) + modes + bytes([0x01,0x02,0x1C,0x04,0x00,0,0,0,0,0])
                    os.write(master, r)
                elif cmd == 0x07:
                    r = bytes([0x14,0x01,0x07,pkt[3],0x07]) + b"Copyright RFXCOM"
                    os.write(master, r)
            elif pkt[1] >= 0x10:
                msg = 0x02 if NAK_NEXT["v"] else 0x00
                NAK_NEXT["v"] = False
                os.write(master, bytes([0x04,0x02,0x01,pkt[3],msg]))
threading.Thread(target=emulator, daemon=True).start()

PLUGIN = P.Plugin("x","RFXCOM","4",{"url":port,"autoCreate":"controls","minSightingsControls":"1",
    "manageProtocols":True,"proto_ac":True,"proto_adlightwave":True,"proto_oregon":True,"dedupeSeconds":"0.5"})
PLUGIN.startup()
PLUGIN._ensure_connection()
check(PLUGIN.connected, "connected via pty")
check(PLUGIN.status_info["firmwareType"]=="Ext2" and PLUGIN.status_info["firmwareVersion"]==1012, "status decoded from real handshake")
check(PLUGIN.transport.receiver_started(), "start-receiver copyright seen")
check(set(PLUGIN.status_info["enabledProtocols"])=={"ac","adlightwave","oregon"}, "protocols applied: %s"%PLUGIN.status_info["enabledProtocols"])
iface = list(devices.iter("self.rfxInterface"))[0]
check(iface.states["connectionState"]=="connected" and iface.states["receiverStarted"], "interface device updated")

# incoming RF from the 'air'
os.write(master, bytes.fromhex("0B1100010123456703010F70"))
time.sleep(0.3)
pump()
sw = devices["AC 1234567:3"]
check(sw.states["onOffState"], "RF received over pty -> device created + on")

PLUGIN.actionControlDevice(types.SimpleNamespace(deviceAction=DevAction.TurnOff, actionValue=0), sw)
time.sleep(1)
check(sw.states["onOffState"] is False, "TX over pty, ACK parsed, state updated")
check(log[-1] == bytes.fromhex("0B1100000123456703000000"), "exact bytes on the wire")
NAK_NEXT["v"] = True
PLUGIN.actionControlDevice(types.SimpleNamespace(deviceAction=DevAction.TurnOn, actionValue=0), sw)
time.sleep(1)
check(sw.states["onOffState"] is False and any("NAK" in m for _,m in PLUGIN.logs), "real NAK detected through transport")

# lose the connection
STATE["stop"] = True
time.sleep(1.5)
print("thread alive:", PLUGIN.conn._thread.is_alive(), "queue:", PLUGIN.events.qsize())
pump()
check(not PLUGIN.connected, "connection loss detected")
check(iface.states["connectionState"]=="disconnected", "interface shows disconnected")
PLUGIN.stop_event.set()
print("\nPTY: ALL OK")
