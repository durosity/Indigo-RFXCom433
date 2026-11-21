"""
Transport layer for RFXCom433.

Subclasses pyRFXtrx's PySerialTransport so that we can:
  * open local serial ports *and* network sockets (ser2net, socket://host:port,
    rfc2217://...) through pyserial's serial_for_url();
  * see the transmitter ACK/NAK byte for every command we send (pyRFXtrx only
    signals "an ACK arrived", not what it said);
  * see every raw packet, including the interface responses and packet types
    pyRFXtrx doesn't decode, so the plugin can count, log and report them.
"""

import threading
import time

import serial
from RFXtrx import PySerialTransport, RFXtrxTransportError, lowlevel

# Transmitter response (packet type 0x02, subtype 0x01) message byte
TX_RESPONSES = {
    0x00: "ACK",
    0x01: "ACK (delayed)",
    0x02: "NAK - transmitter did not lock on the requested frequency",
    0x03: "NAK - AC address zero is not allowed",
}


class IndigoRFXTransport(PySerialTransport):
    """PySerialTransport opened by URL, with ACK/NAK and raw packet hooks."""

    def __init__(self, port_url, raw_hook=None):
        super().__init__(port_url)
        self.port_url = port_url
        self.raw_hook = raw_hook
        self.last_tx_response = None        # raw 0x02 packet for the last command
        self.interface_responses = []       # raw 0x01 packets seen during startup
        self.packets_in = 0
        self.packets_out = 0
        self._resp_lock = threading.Lock()

    # ------------------------------------------------------------------
    def connect(self, timeout=None):
        try:
            # timeout=None -> blocking reads; close() from another thread unblocks them
            self.serial = serial.serial_for_url(self.port_url, baudrate=38400, timeout=None)
        except (serial.SerialException, OSError, ValueError) as exc:
            raise RFXtrxTransportError("connect failed: {0}".format(exc)) from exc

    # ------------------------------------------------------------------
    def receive_blocking(self):
        try:
            return self._receive_packet()
        except (serial.SerialException, OSError, TypeError, AttributeError) as exc:
            raise RFXtrxTransportError("receive failed: {0}".format(exc)) from exc

    def _read_exact(self, count):
        """Read exactly `count` bytes; an empty read means the port has gone away."""
        chunks = bytearray()
        while len(chunks) < count:
            piece = self.serial.read(count - len(chunks))
            if not piece:
                raise RFXtrxTransportError("serial port closed")
            chunks.extend(piece)
        return bytes(chunks)

    def _receive_packet(self):
        # RFXtrx packets are length-prefixed: one length byte, then that many bytes.
        size = self._read_exact(1)[0]
        if size == 0:
            return None                         # padding byte the RFXtrx sometimes sends
        packet = bytes([size]) + self._read_exact(size)
        self.packets_in += 1
        kind = packet[1]

        # Store responses before waking the sender so tx_result() sees this one.
        with self._resp_lock:
            if kind == lowlevel.PACKETTYPE_TRANSMITTER_RESPONSE:
                self.last_tx_response = packet
            elif kind == lowlevel.PACKETTYPE_INTERFACE_RESPONSE:
                self.interface_responses = (self.interface_responses + [packet])[-10:]
        if kind in lowlevel.ACK_PACKETTYPES:
            self._ack_event.set()

        if self.raw_hook is not None:
            try:
                self.raw_hook("rx", packet)
            except Exception:                   # a logging hook must never stop the reader
                pass
        return self.parse(bytearray(packet))

    # ------------------------------------------------------------------
    def send(self, data):
        pkt = bytearray(data)
        with self._resp_lock:
            self.last_tx_response = None
        try:
            super().send(pkt)
        except (serial.SerialException, OSError, TypeError) as exc:
            raise RFXtrxTransportError("send failed: {0}".format(exc)) from exc
        self.packets_out += 1
        if self.raw_hook:
            try:
                self.raw_hook("tx", bytes(pkt))
            except Exception:
                pass

    def tx_result(self):
        """Return (ok, description) for the most recent device command."""
        with self._resp_lock:
            resp = self.last_tx_response
        if resp is None:
            return True, "ACK (no detail)"
        # [len, 0x02, subtype, seq, msg]
        if len(resp) >= 5 and resp[2] == 0x01:
            msg = resp[4]
            return msg in (0x00, 0x01), TX_RESPONSES.get(msg, "unknown response 0x{0:02X}".format(msg))
        if len(resp) >= 3 and resp[2] == 0x00:
            return False, "receiver did not lock"
        return True, "response {0}".format(resp.hex(" "))

    def receiver_started(self):
        """True if the 'Copyright RFXCOM' start-receiver reply was seen."""
        with self._resp_lock:
            for resp in self.interface_responses:
                if len(resp) > 4 and resp[2] == 0x07 and b"Copyright RFXCOM" in resp:
                    return True
        return False


def is_network_url(url):
    url = (url or "").lower()
    return "://" in url and not url.startswith("loop://")


def wait(seconds, stop_event=None):
    if stop_event is None:
        time.sleep(seconds)
    else:
        stop_event.wait(seconds)
