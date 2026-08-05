#!/usr/bin/env bash
# Build a self-contained svn2gitlab bundle for Linux or macOS.
#
# There is no .exe installer here: on Unix the natural distribution is either a pip
# install or the frozen directory produced below, which can be dropped into
# /opt/svn2gitlab and symlinked onto PATH.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SKIP_TESTS="${SKIP_TESTS:-0}"

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }

step "Checking Python"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' \
  || { echo "Python 3.9 or newer is required" >&2; exit 1; }
"$PYTHON" --version

step "Creating a clean build environment"
rm -rf .buildenv
"$PYTHON" -m venv .buildenv
VENV=".buildenv/bin/python"

step "Installing dependencies"
"$VENV" -m pip install --upgrade pip --quiet
"$VENV" -m pip install -e ".[dev]" --quiet

if [ "$SKIP_TESTS" != "1" ]; then
  step "Running tests"
  "$VENV" -m pytest -q
fi

step "Freezing with PyInstaller"
rm -rf dist/svn2gitlab build
"$VENV" -m PyInstaller installer/svn2gitlab.spec --noconfirm --clean

step "Smoke-testing the frozen binary"
./dist/svn2gitlab/svn2gitlab version

cat <<EOF

Build complete: dist/svn2gitlab/

Install system-wide with:
  sudo cp -r dist/svn2gitlab /opt/svn2gitlab
  sudo ln -sf /opt/svn2gitlab/svn2gitlab /usr/local/bin/svn2gitlab
EOF
