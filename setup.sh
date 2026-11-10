#!/bin/bash
# One-time setup: libusb (Homebrew), a private Python environment with pyusb,
# and the BR23 flash loader from kagaimiq/jl-uboot-tool (MIT), checksum-pinned.
set -euo pipefail
cd "$(dirname "$0")"

LOADER=qanba/loaders/br23loader.bin
LOADER_URL=https://raw.githubusercontent.com/kagaimiq/jl-uboot-tool/adb3f18889e88ac512ce0a3c4d8cc3d3cb30696a/data/loaderblobs/usb/br23loader.bin
LOADER_SHA256=7384429e33ee1f340fbffe46aa928a0099c163e1374a40f6f9f27468a6f16ddf

if ! command -v brew >/dev/null; then
  echo "Homebrew is required: https://brew.sh" >&2; exit 1
fi
brew list libusb >/dev/null 2>&1 || brew install libusb

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f "$LOADER" ] || [ "$(shasum -a 256 "$LOADER" | cut -d' ' -f1)" != "$LOADER_SHA256" ]; then
  echo "Downloading the BR23 flash loader..."
  tmp="$(mktemp)"
  curl -sSfL "$LOADER_URL" -o "$tmp"
  if [ "$(shasum -a 256 "$tmp" | cut -d' ' -f1)" != "$LOADER_SHA256" ]; then
    rm -f "$tmp"; echo "Loader checksum mismatch, refusing to use it." >&2; exit 1
  fi
  mkdir -p "$(dirname "$LOADER")"
  mv "$tmp" "$LOADER"
  chmod 644 "$LOADER"
fi

echo "Setup complete. Next: ./qanba-updater firmware"
