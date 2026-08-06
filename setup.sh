#!/usr/bin/env bash
# One-time setup. Homebrew's Python refuses system-wide installs (PEP 668), so
# everything lives in a virtualenv next to this script. fpgatest re-executes
# itself into that virtualenv automatically, so you never have to activate it.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null; then
    echo "python3 not found. brew install python@3.12" >&2
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "python3 is too old; brew install python@3.12" >&2
    exit 1
fi

# pyftdi talks to the cable through libusb.
if ! (brew list libusb >/dev/null 2>&1 || ls /usr/local/lib/libusb-1.0* /opt/homebrew/lib/libusb-1.0* >/dev/null 2>&1); then
    echo "==> installing libusb"
    brew install libusb
fi

echo "==> creating .venv"
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip --quiet
echo "==> installing python dependencies"
./.venv/bin/python -m pip install -r requirements.txt --quiet

if [ ! -f fpgatest.toml ]; then
    cp fpgatest.example.toml fpgatest.toml
    echo "==> wrote fpgatest.toml (edit remote.host / remote.project / remote.setup)"
fi

echo "==> verifying"
if ! ./.venv/bin/python -c 'import pyftdi, usb, serial' 2>/dev/null; then
    echo
    echo "dependencies did not land in .venv. Full output:" >&2
    ./.venv/bin/python -m pip install -r requirements.txt
    ./.venv/bin/python -c 'import pyftdi, usb, serial'
fi
echo "    interpreter: $(./.venv/bin/python -V) at $PWD/.venv"
echo "    pyftdi:      $(./.venv/bin/python -c 'import pyftdi; print(pyftdi.__version__)')"

echo
echo "done. next:"
echo "    ./fpgatest scan      # prove the cable and the JTAG chain"
echo "    ./fpgatest doctor    # check every link end to end"
