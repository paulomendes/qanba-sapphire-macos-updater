# Qanba Sapphire firmware updater for macOS

Update the firmware of the **Qanba Sapphire S1** leverless controller from a Mac, using
Qanba's **official** firmware package. You don't need Windows, a virtual machine or Boot Camp.

Qanba only publishes a Windows updater. This tool reads the firmware out of that same
download and flashes it over USB, the same way the Windows tool does.

> **Tested:** Qanba Sapphire S1 (standard edition), firmware **V1.21 → VS1.22**
> (`QanbaSapphireVS122.zip`), macOS 26 on Apple Silicon.
> Other Sapphire packages (V1.22, VC/VCS Juri Edition) use the same format and should work,
> but haven't been tested. The tool refuses to write anything it doesn't recognise.

> **Unofficial.** This project isn't affiliated with or endorsed by Qanba. Updating firmware
> always carries some risk. The tool backs up your controller first and is designed to stay
> recoverable, but you use it at your own risk.

---

## What you need

- A Mac with [Homebrew](https://brew.sh) and Python 3. Python 3 comes with the Xcode Command
  Line Tools; install them with `xcode-select --install` if needed.
- Your controller's USB cable. Plug the controller **directly into the Mac** if you can;
  some USB hubs interfere with update mode.
- Qanba's firmware package for your edition, from Qanba's
  [firmware page](https://qanba.com/pages/firmware), for example `QanbaSapphireVS122.zip`.
  **You don't need to unzip it.**

## Step by step

### 1. Install the tool (once)

```bash
git clone https://github.com/paulomendes/qanba-sapphire-macos-updater.git
cd qanba-sapphire-macos-updater
./setup.sh
```

`setup.sh` installs `libusb` with Homebrew, creates a private Python environment, and
downloads the small flash loader the chip needs (checksum-verified, see [Credits](#credits)).

### 2. Check the firmware package

Put the downloaded `.zip` in this folder or leave it in `~/Downloads`, then run:

```bash
./qanba-updater firmware
```

You should see the firmware version (e.g. `version 1.22`) and **`All CRCs OK.`**
If the tool finds more than one package, choose one with `--firmware path/to/file.zip`.

### 3. Update

Unplug the controller first, then run:

```bash
sudo ./qanba-updater update
```

1. Enter your Mac password (needed to take the controller away from macOS's disk driver).
2. Type **`yes`** to confirm.
3. When it says **`Waiting for the controller in update mode...`**, hold the **OPTIONS (≡)**
   button and plug the controller in.
   - If macOS says *"The disk you attached was not readable"*, click **Ignore**.
     Never click *Initialize*.
4. The tool then runs on its own, in about 10 seconds:
   - backs up the controller's whole flash to `backups/`;
   - checks the chip, the flash and the current firmware, and **stops before writing if
     anything is unexpected**;
   - writes the new firmware and verifies every byte.
5. **Don't unplug the controller** until you see **`Update complete and verified`**.

### 4. Finish

Unplug the controller and plug it back in normally. Press a button: per Qanba, the HUD lights
**green** with VS/VCS (multi-button mapping) firmware and **red** with standard V/VC firmware.

Your button settings are kept.

---

## Troubleshooting

| Problem | What to do |
|---|---|
| Stuck at `Waiting for the controller in update mode...` | Hold OPTIONS *while* plugging in. Try a direct USB port or another cable. `./qanba-updater scan` should list a device called `BR23UBOOT1.00`. |
| The controller dropped out of update mode | It leaves update mode by itself after a minute or two without activity. Unplug it and plug it in again (holding OPTIONS) while the tool is waiting. |
| `ABORTED: ...` | Nothing was written. The message explains why. |
| `DEVICE ERROR ... Nothing was written` | The controller disconnected before any change. Run the command again. |
| `DEVICE ERROR ... interrupted while changing the flash` | Your backup is safe in `backups/`. The firmware header is written last, so the controller comes back in update mode by itself. Run `sudo ./qanba-updater update` again (add `--force` if it says the current firmware isn't recognised), or restore your backup. |

### Restore a backup

Every run saves a verified copy of the full flash in `backups/`. To put a backup back on the
controller:

```bash
sudo ./qanba-updater restore backups/sapphire-backup-YYYYMMDD-HHMMSS.bin
```

### Other commands and options

```bash
sudo ./qanba-updater backup     # read-only: identify the controller and save a backup
./qanba-updater scan            # list USB disks (to check that update mode works)
```

- `--firmware PATH`: which Qanba package to use.
- `--variant auto|key|nokey`: each package holds two images, *Key* and *No_Key*. They run
  the **same application**; *No_Key* also stores Bluetooth voice-prompt sounds. `auto`
  (the default) keeps whichever layout your controller already has.
- `--erase-all`: also wipe settings and the reserved areas, like the Windows tool's
  default "Erase Entire Flash".
- `--force`: allow updating a controller whose current firmware can't be read, for example
  to recover from an interrupted update.

---

## How it works

- The Sapphire is built on a **JieLi AC695x (BR23)** chip. Holding OPTIONS while plugging in
  starts the chip's ROM download mode. It shows up as a USB mass-storage device called
  `BR23UBOOT1.00` (USB ID `4c4a:2342`) that accepts vendor-specific SCSI commands.
- Qanba's Windows updater is JieLi's generic "Firmware Upgrade Utility" with the firmware
  appended to the `.exe`. The tool unpacks it (compressed package → `.fw` container →
  `flash.bin`) and checks every CRC on the way.
- macOS has no SCSI pass-through for USB disks, so the tool uses **libusb** to detach
  Apple's mass-storage driver and talks USB Bulk-Only Transport directly. That step is why
  it needs `sudo`.
- It uploads a small flash loader into the chip's RAM, backs up the 1 MiB flash, then erases
  and writes `flash.bin` at offset `0x1000`, leaving the first 4 KiB blank as the firmware's
  download config requires. The sector holding the firmware header is erased first and
  written last, so an interrupted update always leaves the chip in its ROM download mode.
- Safety checks before any write: chip family, flash size, chip encryption key, current
  product, reserved-area layout, flash erase actually working, and a full read-back
  verification afterwards.

### Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The tests run the whole procedure against a simulated chip (NOR flash behaviour, write
protection, disconnects, interrupted writes). They need the Qanba package (found
automatically, or set `QANBA_FIRMWARE=/path/to/package.zip`). If a real backup exists in
`backups/`, extra tests replay the update on it.

## Credits

This tool builds on [kagaimiq](https://github.com/kagaimiq)'s public research on JieLi
chips, [jl-uboot-tool](https://github.com/kagaimiq/jl-uboot-tool) and
[jielie](https://github.com/kagaimiq/jielie). `setup.sh` downloads the BR23 flash loader from
jl-uboot-tool, pinned to one commit and SHA-256 verified (MIT license, see
[`qanba/loaders/LICENSE-jl-uboot-tool.txt`](qanba/loaders/LICENSE-jl-uboot-tool.txt)).

Qanba firmware isn't included in this repository; download it from Qanba.
"Qanba" and "Sapphire" are trademarks of their respective owners.

## License

[MIT](LICENSE)
