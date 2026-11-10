"""JieLi checksum and scrambling primitives.

Algorithms follow kagaimiq's public documentation of JieLi chips
(https://github.com/kagaimiq/jielie, MIT licensed tooling).
"""

def crc16(data: bytes, crc: int = 0) -> int:
    """CRC-16/XMODEM (poly 0x1021, init 0), used everywhere in JieLi formats."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def enc(data: bytes, key: int = 0xFFFF) -> bytes:
    """JieLi "ENC" stream cipher (XOR with a CRC16-LFSR keystream). Symmetric."""
    out = bytearray(data)
    for i in range(len(out)):
        out[i] ^= key & 0xFF
        key = ((key << 1) ^ (0x1021 if key & 0x8000 else 0)) & 0xFFFF
    return bytes(out)


def sfc(data: bytes, key: int, base_offset: int = 0, block: int = 32) -> bytes:
    """ENC applied per 32-byte block with key ^ (offset >> 2).

    This is how the code area mapped through the SPI flash controller (the
    "app area") is scrambled. `base_offset` is the offset of data[0] relative
    to the start of that area and must be block aligned.
    """
    if base_offset % block:
        raise ValueError("sfc: base offset must be block aligned")
    out = bytearray()
    for i in range(0, len(data), block):
        out += enc(data[i:i + block], key ^ ((base_offset + i) >> 2))
    return bytes(out)


_MENGLI_MAGIC = "孟黎我爱你，玉林".encode("gb2312")


def mengli(data: bytes, key: int = 0xFFFFFFFF) -> bytes:
    """JieLi "CrcDecode"/MengLi cipher, used for loaders and the chip key reply."""
    out = bytearray(data)
    c = crc16((key >> 16).to_bytes(2, "little"), key & 0xFFFF)
    for i in range(len(out)):
        m = _MENGLI_MAGIC[i % len(_MENGLI_MAGIC)]
        c = crc16(bytes([m]), c)
        out[i] ^= c & 0xFF
    return bytes(out)


def chipkeybin_decode(blob: bytes) -> int:
    """Decode the 32-byte chip-key blob stored at the start of isd_config.ini."""
    s = sum(blob[:16]) & 0xFF
    if s >= 0xE0:
        s = 0xAA
    elif s <= 0x10:
        s = 0x55
    key = 0
    for i in range(16):
        if (blob[16 + i] ^ blob[15 - i]) < s:
            key |= 1 << i
    return key
