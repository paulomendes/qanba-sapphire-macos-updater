"""Firmware containers used by Qanba's Windows updater.

Layers, outermost first:

1. Windows .exe (JieLi "Firmware Upgrade Utility") with a qCompress'd package
   appended after the PE sections (the "overlay").
2. Package: a small table of named files (config.ini, *.fw, window.png, ...),
   each with a CRC16.
3. .fw file in JieLi's UFW container: 0x40-byte header + 0x50-byte entries,
   each ENC-scrambled with key 0xFFFF. Entry type 0 is "flash.bin".
4. flash.bin: the raw SPI flash image in JieLi's JLFS layout (flash header,
   top-level file entries, uboot.boot, app area scrambled with the chip key).

Every layer is CRC checked; any mismatch raises FirmwareError.
"""

import io
import struct
import zipfile
import zlib
from dataclasses import dataclass, field

from .jlcrypto import chipkeybin_decode, crc16, enc, sfc


class FirmwareError(Exception):
    pass


# --------------------------------------------------------------------------
# Layers 1+2: Windows updater .exe -> package files
# --------------------------------------------------------------------------

def _pe_overlay(exe: bytes) -> bytes:
    if exe[:2] != b"MZ":
        raise FirmwareError("not a Windows executable")
    pe = struct.unpack_from("<I", exe, 0x3C)[0]
    if exe[pe:pe + 4] != b"PE\0\0":
        raise FirmwareError("bad PE signature")
    nsect = struct.unpack_from("<H", exe, pe + 6)[0]
    opt_size = struct.unpack_from("<H", exe, pe + 20)[0]
    sect = pe + 24 + opt_size
    end = 0
    for i in range(nsect):
        raw_size, raw_ptr = struct.unpack_from("<II", exe, sect + i * 40 + 16)
        end = max(end, raw_ptr + raw_size)
    return exe[end:]


def extract_package(path: str) -> dict:
    """Return {filename: bytes} for the files bundled in Qanba's updater.

    `path` may be the downloaded .zip or the .exe inside it.
    """
    with open(path, "rb") as f:
        blob = f.read()
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            exes = [n for n in z.namelist() if n.lower().endswith(".exe")]
            if len(exes) != 1:
                raise FirmwareError("expected exactly one .exe inside the zip")
            blob = z.read(exes[0])

    overlay = _pe_overlay(blob)
    if len(overlay) < 16:
        raise FirmwareError("no firmware package appended to the updater")
    # A UPX-packed exe keeps the overlay unchanged, so no unpacking is needed.
    size = struct.unpack_from(">I", overlay, 4)[0]
    try:
        payload = zlib.decompressobj().decompress(overlay[8:])
    except zlib.error as e:
        raise FirmwareError(f"package decompression failed: {e}")
    if len(payload) != size:
        raise FirmwareError("package size mismatch")

    count = struct.unpack_from("<I", payload, 4)[0]
    files = {}
    for i in range(count):
        o = 0x20 + i * 0x210
        _idx, dcrc, off, sz, _ = struct.unpack_from("<HHIII", payload, o)
        name = payload[o + 16:o + 0x210].split(b"\0")[0].decode("utf-8", "replace")
        data = payload[off:off + sz]
        if crc16(data) != dcrc:
            raise FirmwareError(f"CRC mismatch in packaged file {name}")
        files[name.replace("\\", "/").split("/")[-1]] = data
    return files


# --------------------------------------------------------------------------
# Layer 3: UFW (.fw) container
# --------------------------------------------------------------------------

@dataclass
class UfwEntry:
    type: int
    name: str
    data: bytes


def parse_ufw(fw: bytes) -> dict:
    hdr = enc(fw[:0x40])
    hcrc, lcrc, imgsize, n, _w3, _w4, chip = struct.unpack_from("<HHIHHI48s", hdr)
    if crc16(hdr[2:0x40]) != hcrc:
        raise FirmwareError(".fw header CRC mismatch")
    if imgsize != len(fw):
        raise FirmwareError(".fw size mismatch (truncated file?)")
    lst = fw[0x40:0x40 + n * 0x50]
    if crc16(lst) != lcrc:
        raise FirmwareError(".fw entry list CRC mismatch")
    out = {"chip": chip.split(b"\0")[0].decode(), "entries": {}}
    for i in range(n):
        e = enc(lst[i * 0x50:(i + 1) * 0x50])
        etype, _idx, dcrc, _w1, off, sz, _sz2, _w2, name = struct.unpack("<HHHHIII44s16s", e)
        name = name.split(b"\0")[0].decode()
        data = fw[off:off + sz]
        if etype == 0 and crc16(data) != dcrc:
            raise FirmwareError(f".fw entry {name} CRC mismatch")
        out["entries"][name] = UfwEntry(etype, name, data)
    if "flash.bin" not in out["entries"]:
        raise FirmwareError(".fw has no flash.bin")
    return out


# --------------------------------------------------------------------------
# Layer 4: JLFS flash image
# --------------------------------------------------------------------------

@dataclass
class JlfsEntry:
    hdr_off: int
    data_crc: int
    offset: int
    size: int
    flags: int
    index: int
    name: str


def _entry(raw32: bytes, where: int) -> JlfsEntry:
    if crc16(raw32[2:]) != struct.unpack_from("<H", raw32)[0]:
        raise FirmwareError(f"JLFS entry CRC mismatch at 0x{where:x}")
    dcrc, off, size, flags, _r, idx, name = struct.unpack("<HIIBBH16s", raw32[2:])
    return JlfsEntry(where, dcrc, off, size, flags, idx, name.split(b"\0")[0].decode("ascii", "replace"))


HEADER_OFFSETS = (0x0, 0x1000)   # where the boot ROM looks for the flash header first


@dataclass
class FlashImage:
    """A parsed, CRC-validated flash image (or flash dump).

    Regular file offsets are relative to the flash header (`base`); reserved
    areas (flag 0x10: VM, BTIF, PRCT, key_mac) use absolute flash addresses.
    """
    data: bytes
    base: int = 0              # offset of the flash header within `data`
    version: str = ""
    product: str = ""
    flash_size: int = 0
    firmware_chipkey: int = 0xFFFF
    app_base: int = 0
    app_end: int = 0           # end of the populated app/resource area
    top: list = field(default_factory=list)
    app_files: dict = field(default_factory=dict)   # name -> JlfsEntry
    reserved: dict = field(default_factory=dict)    # name -> (abs offset, size)
    has_tones: bool = False

    @classmethod
    def parse(cls, data: bytes, app_key: int = 0xFFFF) -> "FlashImage":
        img = cls(bytes(data))
        if len(data) < 0x1000:
            raise FirmwareError("flash image too small")
        for b in HEADER_OFFSETS:
            hdr = enc(data[b:b + 32])
            hcrc = struct.unpack_from("<H", hdr)[0]
            if hcrc != 0 and crc16(hdr[2:]) == hcrc:
                break
        else:
            raise FirmwareError("no valid flash header at offset 0x0 or 0x1000")
        img.base = b
        # vid/pid are stored unscrambled inside the scrambled header
        img.version = data[b + 4:b + 8].split(b"\0")[0].decode("ascii", "replace")
        img.product = data[b + 16:b + 32].split(b"\xff")[0].split(b"\0")[0].decode("ascii", "replace")
        img.flash_size = struct.unpack_from("<I", hdr, 8)[0]

        o = b + 32
        while True:
            e = _entry(enc(data[o:o + 32]), o)
            img.top.append(e)
            o += 32
            if e.index:
                break
            if o > b + 0x400:
                raise FirmwareError("runaway top-level entry list")

        isd = next((e for e in img.top if e.name == "isd_config.ini"), None)
        if isd is not None:
            blob = data[b + isd.offset:b + isd.offset + 34]
            if crc16(blob[:32]) == struct.unpack_from("<H", blob, 32)[0]:
                img.firmware_chipkey = chipkeybin_decode(blob[:32])

        app = next((e for e in img.top if e.name == "app_dir_head"), None)
        if app is None:
            raise FirmwareError("no app_dir_head")
        img.app_base = b + app.offset
        area = sfc(data[img.app_base:], app_key)
        off = 0
        while True:
            e = _entry(area[off:off + 32], img.app_base + off)
            if e.flags == 0x83:  # directory: nested entries follow its header
                sub = off + 32
                while True:
                    s = _entry(area[sub:sub + 32], img.app_base + sub)
                    if s.flags & 0x10:
                        img.reserved[s.name] = (s.offset, s.size)
                    else:
                        body = area[off + s.offset:off + s.offset + s.size]
                        if crc16(body) != s.data_crc:
                            raise FirmwareError(f"data CRC mismatch for {e.name}/{s.name}")
                        if e.name == "app_area_head":
                            img.app_files[s.name] = s
                    sub += 32
                    if s.index:
                        break
                if e.name == "tone":
                    img.has_tones = True
            off += e.size
            if e.index:
                break
        img.app_end = img.app_base + off
        if "app.bin" not in img.app_files:
            raise FirmwareError("app area has no app.bin")
        for e in img.top:
            if e.flags & 0x10:
                img.reserved[e.name] = (e.offset, e.size)
        return img

    def describe(self) -> str:
        res = ", ".join(f"{k}@0x{v[0]:X}" for k, v in sorted(self.reserved.items(), key=lambda kv: kv[1][0]))
        return (f'product "{self.product}", version {self.version}, header at 0x{self.base:X}, '
                f"app area 0x{self.app_base:X}-0x{self.app_end:X}, "
                f"voice prompts: {'yes' if self.has_tones else 'no'}, "
                f"firmware chip key 0x{self.firmware_chipkey:04X}, reserved: {res}")


@dataclass
class Firmware:
    """One .fw variant ready to be written."""
    name: str
    chip: str
    image: FlashImage
    write_base: int = 0        # flash offset where flash.bin goes (0x1000 with NEED_RESERVED_4K)


def download_config(ufw: dict) -> str:
    """The download-tool config (isd_config.ini) carried inside a .fw file."""
    e = ufw["entries"].get("isd_config.ini")
    if e is None:
        raise FirmwareError(".fw has no download config (isd_config.ini)")
    return sfc(e.data, 0xFFFF).decode("utf-8", "replace")


def _config_flag(text: str, key: str) -> bool:
    for line in text.splitlines():
        line = line.split("#", 1)[0].split("//", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip().upper() == key:
                return v.strip().rstrip(";").strip().upper() == "YES"
    return False


def load_firmwares(path: str) -> list:
    """Load every .fw from the updater .zip/.exe, or a single .fw file."""
    if path.lower().endswith(".fw"):
        with open(path, "rb") as f:
            blobs = {path.split("/")[-1]: f.read()}
    else:
        files = extract_package(path)
        blobs = {k: v for k, v in files.items() if k.lower().endswith(".fw")}
        if not blobs:
            raise FirmwareError("no .fw files inside the updater")
    out = []
    for name, blob in blobs.items():
        u = parse_ufw(blob)
        img = FlashImage.parse(u["entries"]["flash.bin"].data)
        if img.base != 0:
            raise FirmwareError(f"{name}: flash.bin does not start with its header")
        reserve_4k = _config_flag(download_config(u), "NEED_RESERVED_4K")
        out.append(Firmware(name, u["chip"], img, 0x1000 if reserve_4k else 0))
    return out
