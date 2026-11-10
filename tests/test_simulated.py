"""End-to-end tests of the update procedure against a simulated BR23 chip.

Run from the repository folder:  .venv/bin/python -m unittest discover -s tests -v

Needs Qanba's official firmware package (found like the tool does, or set
QANBA_FIRMWARE=/path/to/package.zip); tests are skipped without it. If a real
controller backup exists in backups/, extra tests replay the update on it.
"""

import os
import sys
import glob
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qanba.jlcrypto import crc16
from qanba.jlfirmware import load_firmwares
from qanba.jlusb import DeviceError
from qanba.updater import (LOADER_ADDR, Session, UpdateAborted, bundled_loader, plan_update)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLASH = 0x100000


def find_test_package():
    if os.environ.get("QANBA_FIRMWARE"):
        return os.environ["QANBA_FIRMWARE"]
    hits = []
    for d in (REPO, os.path.dirname(REPO), os.path.expanduser("~/Downloads")):
        hits += glob.glob(os.path.join(d, "*Sapphire*.zip")) + glob.glob(os.path.join(d, "*Sapphire*.exe"))
    return sorted(hits)[0] if hits else None


PKG = find_test_package()


class FakeChip:
    """Simulated JieLi BR23 in UBOOT mode, with NOR flash semantics."""

    def __init__(self, flash: bytes, chipkey=0xFFFF, write_protect=False, bad_crc_cmd=False,
                 fail_after_writes=None):
        self.flash = bytearray(flash)
        self.chipkey = chipkey
        self.wp = write_protect
        self.bad_crc = bad_crc_cmd
        self.fail_after = fail_after_writes
        self.loader_running = False
        self.ram = {}
        self.ops = []
        self.writes = 0

    def _need_loader(self):
        if not self.loader_running:
            raise DeviceError("command rejected by device (ROM)")

    def inquiry(self):
        return "BR23", "UBOOT1.00", "2.00"

    def mem_write(self, addr, data):
        assert not self.loader_running, "loader re-uploaded over itself"
        self.ram[addr] = data

    def mem_jump(self, addr, arg):
        assert addr == LOADER_ADDR and LOADER_ADDR in self.ram
        self.loader_running = True

    def usb_buffer_size(self):
        self._need_loader(); return 0x8000

    def chip_key(self):
        self._need_loader(); return self.chipkey

    def online_device(self):
        self._need_loader(); return 0x03, 0x856014   # Puya 1 MiB SPI NOR

    def flash_read(self, addr, n):
        self._need_loader(); return bytes(self.flash[addr:addr + n])

    def flash_write(self, addr, data):
        self._need_loader()
        if self.fail_after is not None and self.writes >= self.fail_after:
            raise DeviceError("USB transfer failed: simulated unplug")
        self.writes += 1
        self.ops.append(("write", addr, len(data)))
        if not self.wp:
            for i, b in enumerate(data):
                self.flash[addr + i] &= b

    def _erase(self, addr, n):
        self._need_loader()
        self.ops.append(("erase", addr, n))
        if not self.wp:
            self.flash[addr:addr + n] = b"\xff" * n

    def erase_sector(self, addr):
        assert addr % 0x1000 == 0; self._erase(addr, 0x1000)

    def erase_block(self, addr):
        assert addr % 0x10000 == 0; self._erase(addr, 0x10000)

    def flash_crc16(self, addr, n):
        self._need_loader()
        c = crc16(bytes(self.flash[addr:addr + n]))
        return c ^ 1 if self.bad_crc else c

    def run_app(self):
        self.loader_running = False


BASE = 0x1000   # the Windows tool writes flash.bin after a reserved 4K (NEED_RESERVED_4K)
BACKUP_DIRS = (os.path.join(REPO, "backups"), os.path.join(os.path.dirname(REPO), "backups"))


def flash_with(image: bytes, tail_fill=b"\xA5", at=BASE) -> bytearray:
    """Build a 1 MiB flash like the controller's: image at 0x1000, fake VM/BTIF/key_mac."""
    f = bytearray(b"\xff" * FLASH)
    f[at:at + len(image)] = image
    f[0xFD000:0x100000] = tail_fill * 0x3000
    return f


def real_backup():
    files = sorted(f for d in BACKUP_DIRS for f in glob.glob(os.path.join(d, "sapphire-backup-*.bin")))
    if not files:
        return None
    with open(files[0], "rb") as f:
        return f.read()


def quiet(*_a, **_k):
    pass


class UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if PKG is None:
            raise unittest.SkipTest("Qanba firmware package not found (set QANBA_FIRMWARE)")
        fws = load_firmwares(PKG)
        cls.fws = fws
        cls.key = next(f for f in fws if "no_key" not in f.name.lower())
        cls.nokey = next(f for f in fws if "no_key" in f.name.lower())

    def session(self, chip):
        chip.loader_running = False          # every run starts from a fresh plug-in (ROM mode)
        s = Session(chip, log=quiet)
        s.start(bundled_loader())
        return s

    def run_update(self, chip, variant="auto", erase_all=False, force=False):
        s = self.session(chip)
        with tempfile.TemporaryDirectory() as d:
            backup = s.backup(os.path.join(d, "b.bin"))
        fw, end = plan_update(s, self.fws, backup, variant, erase_all, force, log=quiet)
        self.assertEqual(fw.write_base, BASE)
        s.program(fw.image.data, fw.write_base, end, header_at=fw.write_base)
        return fw, end, backup

    def expected(self, image, preserved_from, erase_all):
        out = bytearray(b"\xff" * FLASH)
        out[BASE:BASE + len(image)] = image
        if not erase_all:
            out[0xFD000:] = preserved_from[0xFD000:]
        return bytes(out)

    def test_auto_picks_nokey_when_tones_present(self):
        old = flash_with(self.nokey.image.data)
        chip = FakeChip(old)
        fw, end, _ = self.run_update(chip)
        self.assertIs(fw, self.nokey)
        self.assertEqual(end, 0xFD000)
        self.assertEqual(bytes(chip.flash), self.expected(self.nokey.image.data, old, False))

    def test_auto_picks_key_when_no_tones(self):
        old = flash_with(self.key.image.data)
        chip = FakeChip(old)
        fw, _, _ = self.run_update(chip)
        self.assertIs(fw, self.key)
        self.assertEqual(bytes(chip.flash), self.expected(self.key.image.data, old, False))

    def test_switching_variant_clears_stale_data(self):
        old = flash_with(self.nokey.image.data)           # bigger image on the device
        chip = FakeChip(old)
        self.run_update(chip, variant="key")              # smaller image written
        self.assertEqual(bytes(chip.flash), self.expected(self.key.image.data, old, False))

    def test_erase_all(self):
        chip = FakeChip(flash_with(self.key.image.data))
        self.run_update(chip, erase_all=True)
        self.assertEqual(bytes(chip.flash), self.expected(self.key.image.data, None, True))

    def test_header_erased_first_and_written_last(self):
        chip = FakeChip(flash_with(self.key.image.data))
        self.run_update(chip)
        first_erase = next(op for op in chip.ops if op[0] == "erase")
        self.assertEqual(first_erase[1:], (BASE, 0x1000))
        writes = [op for op in chip.ops if op[0] == "write"]
        self.assertEqual(writes[-1][1], BASE)
        self.assertTrue(all(op[1] >= BASE + 0x1000 for op in writes[:-1]))
        self.assertTrue(all(op[2] <= 0x1000 for op in writes), "writes must stay within 4 KiB")

    def test_never_touches_preserved_areas(self):
        chip = FakeChip(flash_with(self.key.image.data))
        self.run_update(chip)
        for kind, addr, n in chip.ops:
            self.assertLessEqual(addr + n, 0xFD000, f"{kind} at {addr:x} hits preserved area")

    def test_chipkey_mismatch_aborts_before_writing(self):
        chip = FakeChip(flash_with(self.key.image.data), chipkey=0x1234)
        with self.assertRaises(UpdateAborted):
            self.run_update(chip)
        self.assertEqual(chip.ops, [])

    def test_unknown_product_aborts(self):
        chip = FakeChip(b"\x00" * FLASH)                  # unreadable firmware
        with self.assertRaises(UpdateAborted):
            self.run_update(chip)
        self.assertEqual(chip.ops, [])

    def test_write_protected_flash_aborts_without_changes(self):
        old = flash_with(self.key.image.data)
        chip = FakeChip(old, write_protect=True)
        with self.assertRaises(UpdateAborted):
            self.run_update(chip)
        self.assertEqual(bytes(chip.flash), bytes(old))
        self.assertFalse(any(op[0] == "write" for op in chip.ops))

    def test_interrupted_write_leaves_no_header_then_recovers(self):
        old = flash_with(self.key.image.data)
        chip = FakeChip(old, fail_after_writes=10)
        with self.assertRaises(DeviceError):
            self.run_update(chip)
        self.assertEqual(bytes(chip.flash[:0x2000]), b"\xff" * 0x2000)   # no header: ROM stays in UBOOT mode
        # user replugs: chip reboots into ROM, firmware now unreadable -> needs --force + explicit variant
        chip.loader_running, chip.fail_after = False, None
        with self.assertRaises(UpdateAborted):
            self.run_update(chip)
        self.run_update(chip, variant="key", force=True, erase_all=True)
        self.assertEqual(bytes(chip.flash), self.expected(self.key.image.data, None, True))

    def test_broken_crc_command_falls_back_to_readback(self):
        old = flash_with(self.nokey.image.data)
        chip = FakeChip(old, bad_crc_cmd=True)
        s = self.session(chip)
        self.assertFalse(s.crc_cmd_ok)
        with tempfile.TemporaryDirectory() as d:
            backup = s.backup(os.path.join(d, "b.bin"))
        fw, end = plan_update(s, self.fws, backup, "auto", False, False, log=quiet)
        s.program(fw.image.data, fw.write_base, end, header_at=fw.write_base)
        self.assertEqual(bytes(chip.flash), self.expected(self.nokey.image.data, old, False))

    def test_restore_roundtrip(self):
        old = bytes(flash_with(self.nokey.image.data))
        chip = FakeChip(flash_with(self.key.image.data))
        s = self.session(chip)
        s.program(old, 0, s.flash_size, header_at=BASE)
        self.assertEqual(bytes(chip.flash), old)

    def test_stale_header_at_offset_0_is_removed(self):
        old = flash_with(self.key.image.data, at=0)       # non-standard old layout
        chip = FakeChip(old)
        self.run_update(chip)
        self.assertEqual(bytes(chip.flash), self.expected(self.key.image.data, old, False))

    # -- against the real controller's backup, if one has been taken -------------
    def test_real_backup_update(self):
        real = real_backup()
        if real is None:
            self.skipTest("no backup in ../backups yet")
        chip = FakeChip(real)
        fw, end, _ = self.run_update(chip)
        self.assertIs(fw, self.key)                       # v1.21 on the device has no voice prompts
        self.assertEqual(end, 0xFD000)
        self.assertEqual(bytes(chip.flash), self.expected(self.key.image.data, real, False))
        self.assertEqual(bytes(chip.flash[0xFD000:]), real[0xFD000:])   # settings untouched

    def test_real_backup_restore(self):
        real = real_backup()
        if real is None:
            self.skipTest("no backup in ../backups yet")
        chip = FakeChip(flash_with(self.key.image.data))
        s = self.session(chip)
        s.program(real, 0, s.flash_size, header_at=BASE)
        self.assertEqual(bytes(chip.flash), real)


if __name__ == "__main__":
    unittest.main()
