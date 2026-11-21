"""
RFXtrx firmware updater for RFXCom433.

Original implementation (MIT, Durosity). It talks to the serial bootloader
in RFXtrx433 / 433E / 868 units using these interoperability facts:

  * Framing is the Microchip AN1310 serial bootloader format: two STX bytes,
    the payload with STX/ETX/DLE bytes escaped by a preceding DLE, a checksum
    byte (two's complement of the payload sum, escaped the same way), then ETX.
  * Commands: 0x00 = bootloader info, 0x02 = write a 256-byte flash row,
    0x03 = erase flash pages, 0x08 = mark application verified,
    [0x00 0x00] = reset into the application.
  * The running RFXtrx firmware drops into its bootloader when it receives the
    frame 01 01 00 00 FF.
  * Flash is word-addressed (2 bytes per address); rows are 256 bytes and erase
    pages are 2048 bytes. Application flash is word 0x1800-0xA7FF on 433 MHz
    units and 0x1000-0x147FF on 868 MHz units.

The bootloader region is never erased or written, so an interrupted update can
always be re-run.
"""

import time

import serial

# ---------------------------------------------------------------- protocol facts
STX, ETX, DLE = 0x55, 0x04, 0x05

ROW_BYTES = 256
PAGE_BYTES = 2048
BYTES_PER_WORD = 2
ROW_WORDS = ROW_BYTES // BYTES_PER_WORD
PAGE_WORDS = PAGE_BYTES // BYTES_PER_WORD

PM_RANGE_433 = (0x001800, 0x00A7FF)
PM_RANGE_868 = (0x001000, 0x0147FF)

CMD_INFO = 0x00
CMD_WRITEPM = 0x02
CMD_ERASEPM = 0x03
CMD_VERIFY_OK = 0x08

ENTER_BOOTLOADER = bytes([0x01, 0x01, 0x00, 0x00, 0xFF])
REQUEST_INFO = bytes([CMD_INFO, 0x02])
MARK_VERIFIED = bytes([CMD_VERIFY_OK, 0x01, 0x00, 0x00, 0x00])
RESET_TO_APP = bytes([0x00, 0x00])

# Hardware we're prepared to flash: classic RFXtrx433/433E (type 0x52/0x53) and
# RFXtrx868 variants, with firmware types up to Pro2. Pro XL and RFM69/RFM95
# based units use a different bootloader.
FLASHABLE_433_TYPES = (0x52, 0x53)
FLASHABLE_868_TYPES = tuple(range(0x55, 0x5E))
MAX_FLASHABLE_FW_TYPE = 0x06

# Link timing (seconds) - module level so tests can shorten them
WRITE_DELAY = 0.3
READ_TIMEOUT = 0.2


class FirmwareError(Exception):
    pass


# ---------------------------------------------------------------- firmware image
class HexImage:
    """An Intel HEX file loaded into a sparse byte map, served as flash rows."""

    def __init__(self):
        self.bytes = {}

    @classmethod
    def load(cls, path):
        image = cls()
        base = 0
        finished = False
        with open(path, "r") as fh:
            for number, text in enumerate(fh, 1):
                text = text.strip()
                if not text:
                    continue
                if finished:
                    raise FirmwareError("data after the end-of-file record (line {0})".format(number))
                record = cls._decode_record(text, number)
                kind, offset, data = record
                if kind == 0x00:
                    for i, b in enumerate(data):
                        image.bytes[base + offset + i] = b
                elif kind == 0x01:
                    finished = True
                elif kind == 0x02:
                    base = int.from_bytes(data, "big") << 4
                elif kind == 0x04:
                    base = int.from_bytes(data, "big") << 16
                elif kind in (0x03, 0x05):
                    pass        # start address records don't affect flash contents
                else:
                    raise FirmwareError("line {0}: unknown record type {1}".format(number, kind))
        if not finished:
            raise FirmwareError("no end-of-file record - the download looks truncated")
        if not image.bytes:
            raise FirmwareError("the file contains no data")
        return image

    @staticmethod
    def _decode_record(text, number):
        if text[0] != ":":
            raise FirmwareError("line {0} isn't an Intel HEX record".format(number))
        try:
            raw = bytes.fromhex(text[1:])
        except ValueError:
            raise FirmwareError("line {0} contains non-hex characters".format(number))
        if len(raw) < 5 or len(raw) != raw[0] + 5:
            raise FirmwareError("line {0} has the wrong length".format(number))
        if sum(raw) & 0xFF:
            raise FirmwareError("line {0} fails its checksum".format(number))
        return raw[3], int.from_bytes(raw[1:3], "big"), raw[4:-1]

    def rows(self):
        """{word address of row: 256 bytes}, gaps inside a row padded with 0xFF."""
        out = {}
        for row_start in sorted({a - a % ROW_BYTES for a in self.bytes}):
            data = bytes(self.bytes.get(row_start + i, 0xFF) for i in range(ROW_BYTES))
            out[row_start // BYTES_PER_WORD] = data
        return out


def parse_hex(path):
    """Load a firmware file and return its flash rows keyed by word address."""
    return HexImage.load(path).rows()


def summarise(rows, pm_range):
    first = min(rows)
    last = max(rows) + ROW_WORDS - 1
    stray = [a for a in rows if a < pm_range[0] or a + ROW_WORDS - 1 > pm_range[1]]
    return {"blocks": len(rows), "low": first, "high": last, "outside": stray}


def pm_range_for(transceiver_type):
    if transceiver_type in FLASHABLE_433_TYPES:
        return PM_RANGE_433
    if transceiver_type in FLASHABLE_868_TYPES:
        return PM_RANGE_868
    return None


def check_hardware(transceiver_type, fw_type):
    """None if this unit can be flashed here, otherwise the reason it can't."""
    if transceiver_type is None:
        return "the interface hasn't reported its type yet - connect to it normally first"
    if pm_range_for(transceiver_type) is None:
        return "transceiver type 0x{0:02X} is not supported for flashing".format(transceiver_type)
    if fw_type is not None and fw_type > MAX_FLASHABLE_FW_TYPE:
        return ("firmware type 0x{0:02X} (Pro XL / RFM-based hardware) uses a different bootloader; "
                "use RFXCOM's own tools for this unit").format(fw_type)
    return None


# ---------------------------------------------------------------- framing
def _escaped(value, out):
    if value in (STX, ETX, DLE):
        out.append(DLE)
    out.append(value)


def frame(payload):
    """Wrap a payload for the wire."""
    out = bytearray((STX, STX))
    for value in payload:
        _escaped(value, out)
    _escaped((256 - sum(payload) % 256) % 256, out)
    out.append(ETX)
    return bytes(out)


def unframe(data):
    """Extract the payload from one received frame, or None if incomplete/corrupt."""
    pos = 0
    while pos < len(data) and data[pos] != STX:
        pos += 1
    while pos < len(data) and data[pos] == STX:
        pos += 1
    body = bytearray()
    escape = False
    for value in data[pos:]:
        if escape:
            body.append(value)
            escape = False
        elif value == DLE:
            escape = True
        elif value == ETX:
            if len(body) < 2 or sum(body) % 256:
                return None
            return bytes(body[:-1])
        else:
            body.append(value)
    return None


# ---------------------------------------------------------------- link
class BootloaderLink:
    """Request/response exchange with the bootloader over a serial port."""

    def __init__(self, port_url):
        self.port_url = port_url
        self.port = None

    def open(self):
        self.port = serial.serial_for_url(self.port_url, baudrate=38400, bytesize=8, parity="N",
                                          stopbits=1, timeout=READ_TIMEOUT, write_timeout=2)

    def close(self):
        if self.port is None:
            return
        for step in (self.port.flush, self.port.close):
            try:
                step()
            except Exception:
                pass
        self.port = None

    def _collect_reply(self, wait):
        received = bytearray()
        end = time.time() + wait
        while time.time() < end:
            chunk = self.port.read(512)
            if chunk:
                received.extend(chunk)
            if received and (ETX in received or not chunk):
                payload = unframe(bytes(received))
                if payload is not None:
                    return payload
        return None

    def request(self, payload, attempts=3):
        """Send a command and return the bootloader's reply payload, or None."""
        packet = frame(payload)
        for _ in range(max(1, attempts)):
            self.port.write(packet)
            time.sleep(WRITE_DELAY)
            reply = self._collect_reply(READ_TIMEOUT + 0.75)
            if reply is not None:
                return reply
        return None

    def notify(self, payload):
        """Send a command whose reply we don't depend on."""
        self.request(payload, attempts=1)


# ---------------------------------------------------------------- updater
class Bootloader:
    """Runs a complete firmware update. Raises FirmwareError on failure."""

    def __init__(self, port_url, log=None, progress=None):
        self.link = BootloaderLink(port_url)
        self.log = log or (lambda message, error=False: None)
        self.progress = progress or (lambda percent, message: None)

    def _enter_bootloader(self):
        # sent twice: the first can be swallowed while the application hands over
        self.link.notify(ENTER_BOOTLOADER)
        self.link.notify(ENTER_BOOTLOADER)

    def _identify(self):
        reply = self.link.request(REQUEST_INFO)
        if reply is None or len(reply) < 4:
            raise FirmwareError("no reply from the bootloader (is this an RFXtrx433/433E/868 on a local USB port?)")
        self.log("bootloader version v{0}.{1}".format(reply[3], reply[2]))

    def _erase(self, first, last):
        total_pages = -(-(last - first + 1) // PAGE_WORDS)
        address = first
        while total_pages > 0:
            count = min(total_pages, 255)
            command = bytes([CMD_ERASEPM, count]) + address.to_bytes(3, "little")
            if self.link.request(command, attempts=5) is None:
                raise FirmwareError("erase failed at 0x{0:06X}".format(address))
            address += count * PAGE_WORDS
            total_pages -= count

    def _program(self, rows, stop_event):
        ordered = sorted(rows)
        for done, address in enumerate(ordered, 1):
            if stop_event is not None and stop_event.is_set():
                raise FirmwareError("stopped because the plugin is shutting down - re-run the update")
            command = bytes([CMD_WRITEPM, 1]) + address.to_bytes(3, "little") + rows[address]
            if self.link.request(command, attempts=20) is None:
                raise FirmwareError("write failed at 0x{0:06X} - the unit is still in its bootloader, "
                                    "re-run the update".format(address))
            if done == len(ordered) or done % 8 == 0:
                self.progress(3 + 95 * done // len(ordered), "writing 0x{0:06X}".format(address))

    def flash(self, rows, pm_range, stop_event=None):
        succeeded = False
        try:
            self.link.open()
            self.progress(0, "starting bootloader")
            self.log("entering bootloader")
            self._enter_bootloader()
            self._identify()

            self.progress(2, "erasing")
            self.log("erasing application flash 0x{0:06X}-0x{1:06X}".format(*pm_range))
            self._erase(*pm_range)

            # reopen the port after the erase and re-enter the bootloader before writing
            self.link.close()
            time.sleep(1)
            self.link.open()
            self._enter_bootloader()

            self._program(rows, stop_event)

            self.progress(99, "verifying")
            reply = self.link.request(MARK_VERIFIED)
            if not reply or reply[0] != CMD_VERIFY_OK:
                raise FirmwareError("the bootloader didn't confirm the update - re-run it")
            succeeded = True
            self.log("programming completed successfully")
        finally:
            if self.link.port is not None:
                self.log("restarting the interface")
                try:
                    self.link.notify(RESET_TO_APP)
                except Exception:
                    pass
            self.link.close()
            time.sleep(1)
            self.progress(100 if succeeded else -1, "done" if succeeded else "failed")
        return succeeded
