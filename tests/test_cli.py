"""Command-line flow tests (simulated chip, no USB)."""

import builtins
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import qanba_updater as cli
from qanba.jlusb import DeviceError
from qanba.updater import Session, bundled_loader
from test_simulated import BASE, PKG, FakeChip, flash_with, real_backup
from qanba.jlfirmware import load_firmwares, FlashImage


class FakeMSC:
    def close(self):
        pass


@unittest.skipIf(PKG is None, "Qanba firmware package not found (set QANBA_FIRMWARE)")
class CliTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = (cli.connect, builtins.input, cli.BACKUP_DIR)
        cli.BACKUP_DIR = self.tmp.name
        cli.ACTIVE.clear()

        def fake_input(prompt=""):
            self.events.append("confirm")
            return "yes"
        builtins.input = fake_input

    def tearDown(self):
        cli.connect, builtins.input, cli.BACKUP_DIR = self._orig
        self.tmp.cleanup()

    def use_chip(self, chip):
        def fake_connect(args):
            self.events.append("connect")
            chip.loader_running = False          # connect always waits for a fresh plug-in
            s = Session(chip, log=lambda *a: None)
            cli.ACTIVE["session"] = s
            s.start(bundled_loader())
            return FakeMSC(), s
        cli.connect = fake_connect

    def args(self, **kw):
        base = dict(firmware=PKG, variant="auto", erase_all=False, force=False, yes=False,
                    timeout=1, loader_arg=1)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_update_confirms_before_connecting_and_never_waits_after(self):
        chip = FakeChip(self.start_image())
        self.use_chip(chip)
        with redirect_stdout(io.StringIO()):
            cli.cmd_update(self.args())
        self.assertEqual(self.events, ["confirm", "connect"])
        after = FlashImage.parse(bytes(chip.flash))
        self.assertEqual((after.version, after.base), ("1.22", BASE))

    def run_main(self, *argv):
        sys_argv = sys.argv
        sys.argv = ["qanba_updater.py", "--firmware", PKG, *argv]
        try:
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
                cli.main()
        finally:
            sys.argv = sys_argv
        return str(cm.exception.code)

    def start_image(self):
        key = next(f for f in load_firmwares(PKG) if "no_key" not in f.name.lower())
        return real_backup() or bytes(flash_with(key.image.data))

    def test_disconnect_during_backup_reports_nothing_written(self):
        start = self.start_image()
        chip = FakeChip(start)
        reads = {"n": 0}
        orig_read = chip.flash_read

        def flaky_read(addr, n):
            reads["n"] += 1
            if reads["n"] > 5:
                raise DeviceError("No such device")
            return orig_read(addr, n)
        chip.flash_read = flaky_read
        self.use_chip(chip)
        msg = self.run_main("update")
        self.assertIn("Nothing was written", msg)
        self.assertEqual(bytes(chip.flash), start)
        self.assertEqual(chip.ops, [])

    def test_disconnect_at_first_erase_then_rerun_succeeds(self):
        start = self.start_image()
        chip = FakeChip(start)
        orig_erase = chip.erase_sector
        chip.erase_sector = lambda addr: (_ for _ in ()).throw(DeviceError("No such device"))
        self.use_chip(chip)
        msg = self.run_main("update")
        self.assertIn("interrupted", msg)
        self.assertEqual(bytes(chip.flash), start)       # erase never reached the chip
        # re-plug and run again: old firmware still intact, so no --force needed
        chip.erase_sector = orig_erase
        self.events.clear()
        with redirect_stdout(io.StringIO()):
            cli.cmd_update(self.args())
        after = FlashImage.parse(bytes(chip.flash))
        self.assertEqual((after.version, after.base), ("1.22", BASE))


if __name__ == "__main__":
    unittest.main()
