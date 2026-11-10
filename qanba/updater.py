"""High-level, fail-safe update procedure for the Qanba Sapphire (JieLi BR23)."""

import os
import sys
import time
from importlib import resources

from .jlcrypto import crc16
from .jlfirmware import FirmwareError, FlashImage
from .jlusb import DeviceError

LOADER_ADDR = 0x12000       # BR23 loader load/entry address
LOADER_BLOCK = 512          # loader is MengLi-scrambled per 512-byte block; ROM descrambles
DEFAULT_LOADER_ARG = 0x0001 # target = SPI NOR flash, default clock, 1-bit SPI
SECTOR = 0x1000
BLOCK = 0x10000
WRITE_CHUNK = 0x1000
SPI_NOR_TYPES = (0x03, 0x16)
PRESERVED_AREAS = ("VM", "BTIF", "key_mac")
# Product IDs stored in Sapphire firmware headers ("Quanba_3in1" is how v1.21 labels itself).
KNOWN_PRODUCTS = ("Qanba Sapphire", "Quanba_3in1")


class UpdateAborted(Exception):
    pass


def bundled_loader() -> bytes:
    path = resources.files("qanba").joinpath("loaders/br23loader.bin")
    if not path.is_file():
        raise UpdateAborted("the flash loader is missing; run ./setup.sh first")
    return path.read_bytes()


class Progress:
    def __init__(self, label, total, out=sys.stdout):
        self.label, self.total, self.done, self.out = label, max(total, 1), 0, out
        self.t0 = time.time()
        self._last = -1

    def add(self, n):
        self.done += n
        pct = int(self.done * 100 / self.total)
        if pct != self._last:
            self._last = pct
            self.out.write(f"\r  {self.label}: {pct:3d}%")
            self.out.flush()

    def finish(self):
        self.out.write(f"\r  {self.label}: 100%  ({time.time() - self.t0:.1f}s)\n")
        self.out.flush()


class Session:
    """A connected chip with the loader running."""

    def __init__(self, jl, log=print):
        self.jl = jl
        self.log = log
        self.chunk = 512
        self.chipkey = None
        self.flash_id = None
        self.flash_size = None
        self.crc_cmd_ok = False
        self.modified = False      # becomes True once the first erase is sent

    def start(self, loader: bytes, loader_arg: int = DEFAULT_LOADER_ARG):
        """Identify a freshly plugged-in chip and get the loader running."""
        vendor, product, rev = self.jl.inquiry()
        self.log(f"Device reports: {vendor} {product} {rev}")
        if vendor.upper() != "BR23":
            raise UpdateAborted(f'expected a JieLi BR23 chip (AC695x), but the device says "{vendor}". '
                                "Nothing was sent to it.")
        if not product.upper().startswith("UBOOT"):
            raise UpdateAborted(f'device is not in ROM download mode ("{product}"). Unplug it and '
                                "reconnect while holding OPTIONS.")

        self.log(f"Uploading loader ({len(loader)} bytes) to RAM...")
        for off in range(0, len(loader), LOADER_BLOCK):
            self.jl.mem_write(LOADER_ADDR + off, loader[off:off + LOADER_BLOCK])
        self.jl.mem_jump(LOADER_ADDR, loader_arg)

        try:
            size = self.jl.usb_buffer_size()
        except DeviceError:
            size = 512
        # keep transfers page aligned and inside the 16-bit length field
        self.chunk = max(256, min(size, 0x8000)) // 256 * 256

        self.chipkey = self.jl.chip_key()
        dtype, self.flash_id = self.jl.online_device()
        if dtype not in SPI_NOR_TYPES:
            raise UpdateAborted(f"loader reports no SPI NOR flash (type 0x{dtype:02x}); aborting")
        # JEDEC ID = manufacturer, type, density (log2 of size). Accept either byte
        # order; manufacturer/type codes are never in the 0x10..0x18 density range.
        density = next((d for d in (self.flash_id & 0xFF, (self.flash_id >> 16) & 0xFF)
                        if 0x10 <= d <= 0x18), None)
        if density is None:
            raise UpdateAborted(f"unrecognised flash ID 0x{self.flash_id:06x}; aborting")
        self.flash_size = 1 << density

        self.log(f"Chip key: 0x{self.chipkey:04X}   Flash ID: 0x{self.flash_id:06X} "
                 f"({self.flash_size // 1024} KiB)   transfer size: {self.chunk} bytes")

        # Self-test the device-side CRC command against a real read; if it does
        # not agree, verification falls back to reading everything back.
        probe = self.jl.flash_read(0, SECTOR)
        try:
            self.crc_cmd_ok = self.jl.flash_crc16(0, SECTOR) == crc16(probe)
        except DeviceError:
            self.crc_cmd_ok = False
        if probe != self.jl.flash_read(0, SECTOR):
            raise UpdateAborted("flash reads are not stable; aborting (try another USB port/cable)")

    # -- primitives ---------------------------------------------------------
    def read(self, start, length, label="Reading") -> bytes:
        p = Progress(label, length)
        out = bytearray()
        for addr in range(start, start + length, self.chunk):
            n = min(self.chunk, start + length - addr)
            out += self.jl.flash_read(addr, n)
            p.add(n)
        p.finish()
        return bytes(out)

    def device_crc_matches(self, start, data) -> bool:
        step = 0x8000
        for off in range(0, len(data), step):
            piece = data[off:off + step]
            if self.jl.flash_crc16(start + off, len(piece)) != crc16(piece):
                return False
        return True

    def verify(self, start, data, label="Verifying"):
        back = self.read(start, len(data), label)
        if back != data:
            bad = next(i for i in range(len(data)) if back[i] != data[i])
            raise DeviceError(f"verification failed at 0x{start + bad:06X}")

    def erase(self, start, end, label="Erasing"):
        assert start % SECTOR == 0 and end % SECTOR == 0
        p = Progress(label, end - start)
        addr = start
        while addr < end:
            if addr % BLOCK == 0 and end - addr >= BLOCK:
                self.jl.erase_block(addr)
                step = BLOCK
            else:
                self.jl.erase_sector(addr)
                step = SECTOR
            addr += step
            p.add(step)
        p.finish()

    def check_blank(self, start, end, label="Blank check"):
        n = end - start
        if self.crc_cmd_ok:
            step = 0x8000
            for off in range(0, n, step):
                ln = min(step, n - off)
                if self.jl.flash_crc16(start + off, ln) != crc16(b"\xff" * ln):
                    return False
            return True
        return self.read(start, n, label) == b"\xff" * n

    def write(self, start, data, label="Writing"):
        # Reads use the loader's full buffer, but writes stay at one sector:
        # newer JieLi loaders may buffer less than they report, and a small
        # write size costs only a few seconds over the whole image.
        step = min(self.chunk, WRITE_CHUNK)
        p = Progress(label, len(data))
        for off in range(0, len(data), step):
            piece = data[off:off + step]
            if piece.count(0xFF) != len(piece):     # erased flash is already 0xFF
                self.jl.flash_write(start + off, piece)
            p.add(len(piece))
        p.finish()

    # -- procedures ---------------------------------------------------------
    def backup(self, path) -> bytes:
        data = self.read(0, self.flash_size, "Backing up flash")
        ok = self.device_crc_matches(0, data) if self.crc_cmd_ok else data == self.read(0, self.flash_size, "Re-reading")
        if not ok:
            raise UpdateAborted("backup could not be verified; aborting before any change")
        with open(path, "wb") as f:
            f.write(data)
        give_to_sudo_user(path)
        self.log(f"Backup saved and verified: {path}")
        return data

    def program(self, data: bytes, start: int, erase_end: int, header_at: int):
        """Erase [0, erase_end), then write `data` at `start`.

        The sector holding the firmware header (`header_at`) is erased first
        and written last. If anything is interrupted, the ROM finds no valid
        firmware and stays in download mode, so the operation can simply be
        repeated.
        """
        padded = data + b"\xff" * (-len(data) % SECTOR)
        end = start + len(padded)
        if start % SECTOR or header_at % SECTOR or not start <= header_at < end:
            raise UpdateAborted("internal error: bad write layout")
        if end > erase_end or erase_end > self.flash_size:
            raise UpdateAborted("image does not fit in the area to be erased")
        hdr = header_at - start

        self.modified = True
        self.erase(header_at, header_at + SECTOR, "Erasing header sector")
        if not self.check_blank(header_at, header_at + SECTOR):
            raise UpdateAborted("the flash did not erase (write-protected?). No data was changed.")
        if header_at:
            self.erase(0, header_at, "Erasing")
        self.erase(header_at + SECTOR, erase_end)
        if not (self.check_blank(0, header_at) and self.check_blank(header_at + SECTOR, erase_end)):
            raise DeviceError("erase verification failed")

        for a, b in ((0, hdr), (hdr + SECTOR, len(padded))):
            if b > a:
                self.write(start + a, padded[a:b], "Writing firmware")
                self.verify(start + a, padded[a:b], "Verifying firmware")
        self.write(header_at, padded[hdr:hdr + SECTOR], "Writing header")
        self.verify(header_at, padded[hdr:hdr + SECTOR], "Verifying header")
        if self.crc_cmd_ok and not self.device_crc_matches(start, padded):
            raise DeviceError("final CRC check failed")


def preserved_start(img: FlashImage) -> int:
    return min(img.reserved[n][0] for n in PRESERVED_AREAS if n in img.reserved)


def plan_update(session: Session, firmwares, backup: bytes, variant: str, erase_all: bool,
                force: bool, log=print):
    """Pick the firmware variant and erase range; raise UpdateAborted if unsafe."""
    try:
        current = FlashImage.parse(backup, app_key=session.chipkey)
        log(f"Current firmware: {current.describe()}")
    except FirmwareError as e:
        current = None
        log(f"Current firmware could not be parsed ({e}).")
        if not force:
            raise UpdateAborted("the controller's current firmware is not recognised. If you are "
                                "recovering from a failed update, re-run with --force.")

    if current is not None and current.product not in KNOWN_PRODUCTS and not force:
        raise UpdateAborted(f'this device runs "{current.product}", not Qanba Sapphire firmware; aborting')

    by_variant = {("nokey" if "no_key" in fw.name.lower() else "key"): fw for fw in firmwares}
    if variant == "auto":
        if current is None:
            raise UpdateAborted("cannot auto-select a variant without a readable current firmware; "
                                "pass --variant key or --variant nokey")
        variant = "nokey" if current.has_tones else "key"
        log(f"Auto-selected variant '{variant}' (matches the layout already on the controller).")
    if variant not in by_variant:
        raise UpdateAborted(f"variant '{variant}' not found in the firmware package")
    fw = by_variant[variant]

    if session.chipkey != fw.image.firmware_chipkey:
        raise UpdateAborted(f"the chip has key 0x{session.chipkey:04X} but the firmware is built for "
                            f"0x{fw.image.firmware_chipkey:04X}; re-encryption is not implemented, aborting")
    # The image declares the flash space it was built for; together with the
    # reserved 4K at the start it must exactly fill the chip.
    if fw.write_base + fw.image.flash_size != session.flash_size:
        raise UpdateAborted(f"layout mismatch: image built for 0x{fw.image.flash_size:X} bytes at "
                            f"0x{fw.write_base:X}, but the flash is 0x{session.flash_size:X} bytes")

    if erase_all:
        erase_end = session.flash_size
    else:
        erase_end = preserved_start(fw.image)
        if current is None or {n: current.reserved.get(n) for n in PRESERVED_AREAS} != \
                {n: fw.image.reserved.get(n) for n in PRESERVED_AREAS}:
            raise UpdateAborted("settings/reserved areas differ between the current and new firmware; "
                                "re-run with --erase-all")
    if fw.write_base + len(fw.image.data) > erase_end:
        raise UpdateAborted("firmware would overlap the preserved areas")
    return fw, erase_end


def default_backup_path(directory) -> str:
    os.makedirs(directory, exist_ok=True)
    give_to_sudo_user(directory)
    return os.path.join(directory, time.strftime("sapphire-backup-%Y%m%d-%H%M%S.bin"))


def give_to_sudo_user(path):
    """Under sudo, hand files we create back to the real user."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid and os.geteuid() == 0:
        try:
            os.chown(path, int(uid), int(gid))
        except OSError:
            pass
