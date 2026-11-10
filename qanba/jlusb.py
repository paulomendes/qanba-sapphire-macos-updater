"""USB access to a JieLi chip in download ("UBOOT") mode, via libusb.

In download mode the chip enumerates as a USB mass-storage device and is driven
with vendor-specific SCSI commands. macOS has no SCSI pass-through for USB
disks, so we detach Apple's mass-storage driver and speak the Bulk-Only
Transport ourselves (libusb needs root for the detach on macOS).

Command set: kagaimiq's documentation of the JieLi "USB loader v2" protocol.
"""

import os
import struct
import time

import usb.core
import usb.util
import usb.backend.libusb1

from .jlcrypto import crc16, mengli

MSC_CLASS, MSC_SUBCLASS_SCSI, MSC_PROTO_BOT = 0x08, 0x06, 0x50


class DeviceError(Exception):
    pass


def get_backend():
    for path in ("/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib"):
        if os.path.exists(path):
            be = usb.backend.libusb1.get_backend(find_library=lambda _n, p=path: p)
            if be:
                return be
    be = usb.backend.libusb1.get_backend()
    if be is None:
        raise DeviceError("libusb not found. Install it with: brew install libusb")
    return be


def _str(dev, idx):
    try:
        return usb.util.get_string(dev, idx) if idx else ""
    except Exception:
        return ""


def describe(dev) -> str:
    return (f"{dev.idVendor:04x}:{dev.idProduct:04x} "
            f'"{_str(dev, dev.iManufacturer)}" "{_str(dev, dev.iProduct)}" '
            f"bus {dev.bus} addr {dev.address}")


def msc_interface(dev):
    try:
        for cfg in dev:
            for intf in cfg:
                if (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol) == \
                        (MSC_CLASS, MSC_SUBCLASS_SCSI, MSC_PROTO_BOT):
                    return cfg, intf
    except usb.core.USBError:
        pass
    return None, None


def list_msc(backend):
    return [d for d in usb.core.find(find_all=True, backend=backend) if msc_interface(d)[1] is not None]


def _key(dev):
    return (dev.bus, dev.address, dev.idVendor, dev.idProduct)


def looks_jieli(dev) -> bool:
    text = f"{_str(dev, dev.iManufacturer)} {_str(dev, dev.iProduct)}".upper()
    return (dev.idVendor in (0x4C4A, 0x2C22)
            or any(t in text for t in ("UBOOT", "BR23", "JIELI", "UDISK")))


def wait_for_device(backend, timeout=120.0, log=print):
    """Wait for the controller to be plugged in, in download mode.

    Only a device that is plugged in while we wait is accepted, so the chip
    is guaranteed to be in its clean ROM state (no loader left running by a
    previous run). Other USB disks that were already attached are ignored.
    """
    present = list_msc(backend)
    before = {_key(d) for d in present}
    if any(looks_jieli(d) for d in present):
        log("  The controller is already connected: unplug it now, then plug it back in holding OPTIONS.")
    deadline = time.time() + timeout
    while time.time() < deadline:
        now = list_msc(backend)
        keys = {_key(d) for d in now}
        before &= keys                      # forget devices that were unplugged
        cands = [d for d in now if _key(d) not in before]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            raise DeviceError("several USB disks appeared at once; unplug other USB drives and retry:\n  "
                              + "\n  ".join(describe(d) for d in cands))
        time.sleep(0.3)
    raise DeviceError("timed out waiting for the controller in update mode")


class BulkOnlyMSC:
    """Minimal USB Mass Storage Bulk-Only Transport (SCSI pass-through)."""

    def __init__(self, dev):
        self.dev = dev
        self.cfg, self.intf = msc_interface(dev)
        if self.intf is None:
            raise DeviceError("device has no mass-storage interface")
        self.ifnum = self.intf.bInterfaceNumber
        self.ep_in = usb.util.find_descriptor(self.intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN and
            usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        self.ep_out = usb.util.find_descriptor(self.intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT and
            usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        if self.ep_in is None or self.ep_out is None:
            raise DeviceError("mass-storage interface lacks bulk endpoints")
        self.tag = 1
        self.detached = False

    def open(self):
        try:
            if self.dev.is_kernel_driver_active(self.ifnum):
                self.dev.detach_kernel_driver(self.ifnum)
                self.detached = True
        except usb.core.USBError as e:
            if os.geteuid() != 0:
                raise DeviceError("macOS's disk driver owns the device; run this tool with sudo") from e
            raise DeviceError(f"could not detach the macOS disk driver: {e}") from e
        except NotImplementedError:
            pass
        try:
            usb.util.claim_interface(self.dev, self.ifnum)
        except usb.core.USBError as e:
            hint = " (try running with sudo)" if os.geteuid() != 0 else ""
            raise DeviceError(f"could not claim the USB interface{hint}: {e}") from e

    def close(self):
        try:
            usb.util.release_interface(self.dev, self.ifnum)
        except Exception:
            pass
        usb.util.dispose_resources(self.dev)

    def reset_recovery(self):
        try:
            self.dev.ctrl_transfer(0x21, 0xFF, 0, self.ifnum, None, timeout=2000)
        except usb.core.USBError:
            pass
        for ep in (self.ep_in, self.ep_out):
            try:
                self.dev.clear_halt(ep)
            except usb.core.USBError:
                pass

    def _read_csw(self, timeout):
        for attempt in range(2):
            try:
                return bytes(self.ep_in.read(13, timeout=timeout))
            except usb.core.USBError as e:
                if attempt == 0 and getattr(e, "errno", None) in (32, None):  # EPIPE / stall
                    try:
                        self.dev.clear_halt(self.ep_in)
                    except usb.core.USBError:
                        pass
                    continue
                raise

    def execute(self, cdb: bytes, data_out: bytes = None, in_len: int = 0, timeout: int = 5000) -> bytes:
        if len(cdb) > 16:
            raise ValueError("CDB too long")
        tag = self.tag
        self.tag = (self.tag + 1) & 0xFFFFFFFF or 1
        length = len(data_out) if data_out else in_len
        flags = 0x80 if in_len else 0x00
        cbw = struct.pack("<IIIBBB", 0x43425355, tag, length, flags, 0, len(cdb)) + cdb.ljust(16, b"\0")
        try:
            self.ep_out.write(cbw, timeout=timeout)
            data = b""
            if in_len:
                try:
                    data = bytes(self.ep_in.read(in_len, timeout=timeout))
                except usb.core.USBError as e:
                    if getattr(e, "errno", None) == 32:
                        self.dev.clear_halt(self.ep_in)
                    else:
                        raise
            elif data_out:
                self.ep_out.write(data_out, timeout=timeout)
            csw = self._read_csw(timeout)
        except usb.core.USBError as e:
            self.reset_recovery()
            raise DeviceError(f"USB transfer failed: {e}") from e
        sig, rtag, _residue, status = struct.unpack("<IIIB", csw)
        if sig != 0x53425355 or rtag != tag:
            self.reset_recovery()
            raise DeviceError("bad mass-storage status block")
        if status == 2:
            self.reset_recovery()
            raise DeviceError("mass-storage phase error")
        if status != 0:
            raise DeviceError(f"command {cdb[:2].hex()} rejected by device")
        return data


class JieLi:
    """JieLi UBOOT1.00 (mask ROM) and loader-v2 commands."""

    def __init__(self, msc: BulkOnlyMSC):
        self.msc = msc

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _cdb(cmd: int, args: bytes) -> bytes:
        return (cmd.to_bytes(2, "big") + args).ljust(16, b"\xff")

    def _exec(self, cmd: int, args: bytes = b"", timeout=5000) -> bytes:
        resp = self.msc.execute(self._cdb(cmd, args), in_len=16, timeout=timeout)
        if len(resp) < 16 or int.from_bytes(resp[:2], "big") != cmd:
            raise DeviceError(f"unexpected reply to command {cmd:04X}: {resp.hex()}")
        return resp[2:]

    def _datain(self, cmd, args, n, timeout=5000) -> bytes:
        data = self.msc.execute(self._cdb(cmd, args), in_len=n, timeout=timeout)
        if len(data) != n:
            raise DeviceError(f"short read ({len(data)} of {n} bytes)")
        return data

    def _dataout(self, cmd, args, data, timeout=5000):
        self.msc.execute(self._cdb(cmd, args), data_out=data, timeout=timeout)

    # -- standard SCSI ----------------------------------------------------
    def inquiry(self):
        d = self.msc.execute(bytes([0x12, 0, 0, 0, 36, 0]), in_len=36)
        txt = lambda b: b.decode("ascii", "replace").strip()
        return txt(d[8:16]), txt(d[16:32]), txt(d[32:36])

    # -- UBOOT1.00 (mask ROM) ---------------------------------------------
    def mem_write(self, addr: int, data: bytes):
        self._dataout(0xFB06, addr.to_bytes(4, "big") + len(data).to_bytes(2, "big") + b"\0"
                      + crc16(data).to_bytes(2, "little"), data)

    def mem_jump(self, addr: int, arg: int):
        self._exec(0xFB08, addr.to_bytes(4, "big") + arg.to_bytes(2, "big"), timeout=15000)

    # -- loader -------------------------------------------------------------
    def usb_buffer_size(self) -> int:
        return int.from_bytes(self._exec(0xFC14)[:4], "big")

    def chip_key(self) -> int:
        r = self._exec(0xFC09, (0xAC6900).to_bytes(4, "big"))
        return int.from_bytes(mengli(r[4:6][::-1]), "little")

    def online_device(self):
        r = self._exec(0xFC0A)
        return r[0], int.from_bytes(r[2:6], "little")

    def flash_read(self, addr: int, n: int) -> bytes:
        return self._datain(0xFD05, addr.to_bytes(4, "big") + n.to_bytes(2, "big"), n, timeout=10000)

    def flash_write(self, addr: int, data: bytes):
        self._dataout(0xFB04, addr.to_bytes(4, "big") + len(data).to_bytes(2, "big") + b"\0"
                      + crc16(data).to_bytes(2, "little"), data, timeout=10000)

    def erase_sector(self, addr: int):          # 4 KiB
        self._exec(0xFB01, addr.to_bytes(4, "big"), timeout=10000)

    def erase_block(self, addr: int):           # 64 KiB
        self._exec(0xFB00, addr.to_bytes(4, "big"), timeout=20000)

    def flash_crc16(self, addr: int, n: int) -> int:
        return int.from_bytes(self._exec(0xFC13, addr.to_bytes(4, "big") + n.to_bytes(2, "big"),
                                         timeout=15000)[:2], "big")

    def run_app(self):
        try:
            self._exec(0xFC0C, (1).to_bytes(4, "big"), timeout=2000)
        except DeviceError:
            pass  # the chip may reset before answering
