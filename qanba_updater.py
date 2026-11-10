#!/usr/bin/env python3
"""Qanba Sapphire firmware updater for macOS.

Reads the firmware straight out of Qanba's official Windows updater
(e.g. QanbaSapphireVS122.zip) and flashes it over USB.

    ./qanba-updater firmware            # inspect the firmware package (no controller needed)
    ./qanba-updater scan                # list USB disks (no sudo needed)
    sudo ./qanba-updater backup         # read-only: identify the controller and back it up
    sudo ./qanba-updater update         # back up, then flash the new firmware
    sudo ./qanba-updater restore FILE   # write a backup taken earlier

Update mode: hold OPTIONS (≡) while plugging the controller in, when the tool asks.
"""

import argparse
import glob
import os
import sys

from qanba.jlfirmware import FirmwareError, load_firmwares
from qanba.updater import (DEFAULT_LOADER_ARG, Session, UpdateAborted, bundled_loader,
                           default_backup_path, plan_update)

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(HERE, "backups")


def user_home():
    sudo_user = os.environ.get("SUDO_USER")
    return os.path.expanduser(f"~{sudo_user}" if sudo_user else "~")


def find_package(explicit):
    """The Qanba updater .zip/.exe: --firmware, else this folder, its parent or ~/Downloads."""
    if explicit:
        return explicit
    hits = []
    for d in (HERE, os.path.dirname(HERE), os.path.join(user_home(), "Downloads")):
        for pattern in ("*Sapphire*.zip", "*Sapphire*.exe", "*Sapphire*.fw"):
            hits += glob.glob(os.path.join(d, pattern))
    hits = sorted({os.path.realpath(h) for h in hits})
    if len(hits) == 1:
        return hits[0]
    if not hits:
        sys.exit("No Qanba Sapphire firmware package found. Download it from Qanba's firmware page "
                 "and put the .zip in this folder or in ~/Downloads, or pass --firmware PATH.")
    sys.exit("Several firmware packages found; choose one with --firmware PATH:\n  " + "\n  ".join(hits))


def cmd_firmware(args):
    path = find_package(args.firmware)
    print(f"Firmware package: {path}")
    for fw in load_firmwares(path):
        print(f"\n  {fw.name}  (chip {fw.chip}, {len(fw.image.data)} bytes, "
              f"written to flash at 0x{fw.write_base:X})")
        print(f"    {fw.image.describe()}")
    print("\nAll CRCs OK.")


def cmd_scan(args):
    from qanba.jlusb import describe, get_backend, list_msc, looks_jieli
    devs = list_msc(get_backend())
    if not devs:
        print("No USB mass-storage devices found. (Is the controller in update mode? "
              "Hold OPTIONS while plugging it in.)")
    for d in devs:
        print(("* " if looks_jieli(d) else "  ") + describe(d))


ACTIVE = {}   # the current session, so errors can report whether the flash was touched


def connect(args):
    from qanba.jlusb import BulkOnlyMSC, JieLi, get_backend, wait_for_device
    be = get_backend()
    print("Waiting for the controller in update mode...")
    print("  (hold OPTIONS (≡) while plugging it in; if macOS asks about the disk, click Ignore)")
    dev = wait_for_device(be, timeout=args.timeout)
    msc = BulkOnlyMSC(dev)
    msc.open()
    session = Session(JieLi(msc))
    ACTIVE["session"] = session
    try:
        session.start(bundled_loader(), args.loader_arg)
    except Exception:
        msc.close()
        raise
    return msc, session


def confirm(prompt, assume_yes):
    if assume_yes:
        return
    if input(f"{prompt} Type 'yes' to continue: ").strip().lower() != "yes":
        raise UpdateAborted("cancelled by user; nothing was written")


def cmd_backup(args):
    from qanba.jlfirmware import FlashImage
    msc, s = connect(args)
    try:
        data = s.backup(args.output or default_backup_path(BACKUP_DIR))
        try:
            print("Current firmware:", FlashImage.parse(data, app_key=s.chipkey).describe())
        except FirmwareError as e:
            print(f"Current firmware could not be parsed: {e}")
    finally:
        msc.close()
    print("\nDone. Nothing was written. Unplug the controller to return to normal mode.")


def cmd_update(args):
    firmwares = load_firmwares(find_package(args.firmware))   # validate before touching the device
    version = firmwares[0].image.version
    # Confirm before connecting: the controller leaves update mode by itself
    # if it sits idle, so nothing may wait for the user once it is plugged in.
    print(f"This will back up the controller, then flash firmware {version} "
          f"(variant: {args.variant}), " + ("wiping settings." if args.erase_all else "keeping your settings."))
    print("It aborts before writing anything if the controller is not exactly as expected.")
    print("Once it starts writing, do NOT unplug the controller until it finishes (about 10 seconds).")
    confirm("Proceed?", args.yes)
    msc, s = connect(args)
    try:
        backup = s.backup(default_backup_path(BACKUP_DIR))
        fw, erase_end = plan_update(s, firmwares, backup, args.variant, args.erase_all, args.force)
        print(f"\nWriting {fw.name} (version {fw.image.version}): erase 0x000000-0x{erase_end:06X}, "
              f"write {len(fw.image.data)} bytes at 0x{fw.write_base:06X}, "
              + ("settings will be reset." if args.erase_all else "settings/reserved areas kept."))
        s.program(fw.image.data, fw.write_base, erase_end, header_at=fw.write_base)
        s.jl.run_app()
    finally:
        msc.close()
    print("\nUpdate complete and verified. Unplug the controller and plug it back in.")


def cmd_restore(args):
    from qanba.jlfirmware import FlashImage
    with open(args.file, "rb") as f:
        image = f.read()
    try:
        header_at = FlashImage.parse(image).base
    except FirmwareError as e:
        raise UpdateAborted(f"{args.file} does not contain a valid firmware ({e})")
    confirm(f"This will overwrite the whole flash with {args.file}.", args.yes)
    msc, s = connect(args)
    try:
        if len(image) != s.flash_size:
            raise UpdateAborted(f"backup is {len(image)} bytes but the flash is {s.flash_size} bytes")
        s.backup(default_backup_path(BACKUP_DIR))
        s.program(image, 0, s.flash_size, header_at=header_at)
        s.jl.run_app()
    finally:
        msc.close()
    print("\nRestore complete and verified. Unplug the controller and plug it back in.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--firmware", help="Qanba updater .zip/.exe (default: the one next to this folder)")
    ap.add_argument("--timeout", type=float, default=120, help="seconds to wait for the controller")
    ap.add_argument("--loader-arg", type=lambda v: int(v, 0), default=DEFAULT_LOADER_ARG, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("firmware", help="inspect and validate the firmware package")
    sub.add_parser("scan", help="list USB mass-storage devices")
    b = sub.add_parser("backup", help="read-only: identify the controller and back up its flash")
    b.add_argument("-o", "--output", help="backup file path")
    u = sub.add_parser("update", help="back up, then flash the new firmware")
    u.add_argument("--variant", choices=("auto", "key", "nokey"), default="auto")
    u.add_argument("--erase-all", action="store_true", help="also wipe settings/reserved areas (like the Windows tool)")
    u.add_argument("--force", action="store_true", help="allow flashing a controller whose current firmware is unreadable")
    u.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    r = sub.add_parser("restore", help="write a previously saved backup")
    r.add_argument("file")
    r.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    try:
        {"firmware": cmd_firmware, "scan": cmd_scan, "backup": cmd_backup,
         "update": cmd_update, "restore": cmd_restore}[args.cmd](args)
    except (UpdateAborted, FirmwareError) as e:
        sys.exit(f"\nABORTED: {e}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
    except Exception as e:
        from qanba.jlusb import DeviceError
        if isinstance(e, DeviceError):
            s = ACTIVE.get("session")
            if s is None or not s.modified:
                sys.exit(f"\nDEVICE ERROR: {e}\nNothing was written: the controller is unchanged. "
                         "Unplug it and run the command again.")
            sys.exit(f"\nDEVICE ERROR: {e}\nThe update was interrupted while changing the flash. The header is "
                     "written last, so if the old firmware was already erased the controller comes back in update "
                     "mode by itself. Unplug it, plug it back in holding OPTIONS and run 'update' again (add "
                     "--force if it says the current firmware is not recognised), or use 'restore' with the "
                     "backup in the backups/ folder.")
        raise


if __name__ == "__main__":
    main()
