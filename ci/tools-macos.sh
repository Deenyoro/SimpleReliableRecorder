#!/usr/bin/env bash
# Toolchain bootstrap for the macOS GitLab runner (Apple Silicon, shell
# executor). Sourced from before_script with MAC_ARCH=arm64|x64; afterwards
# $PY is a Python 3.12 (with Tk 8.6) of that architecture, in a fresh venv
# inside the job's checkout (.venv-ci, cleaned with the checkout).
#
# PyInstaller cannot cross-compile, so the x64 build needs an x86_64 CPython
# running under Rosetta 2 (as in Tagestry). Both arches use the same source
# for Python: python-build-standalone builds installed by a pinned uv
# (a single-arch x86_64 interpreter runs under Rosetta by itself). These
# builds include tkinter + Tcl/Tk, which Homebrew's python@3.12 does not
# without python-tk, and install under the user's home without admin rights.
#
# Downloads are pinned: uv by version + SHA-256 below; uv in turn verifies
# the SHA-256 of the CPython archive it fetches for the exact version.
# Cache: ~/Library/Caches/srr-ci (nothing outside it is written).
set -euo pipefail

UV_VERSION=0.12.18
UV_SHA256=cf40e0c6a202190ccd9e0406dcfdd5b2d6668a9a5c779b17948963df32aafe5b   # uv-aarch64-apple-darwin.tar.gz
PY_VERSION=3.12.10   # same CPython as the Windows build (ci/tools-windows.ps1)

MAC_ARCH="${MAC_ARCH:-arm64}"
case "$MAC_ARCH" in
  arm64) py_key="cpython-$PY_VERSION-macos-aarch64-none"; want=arm64 ;;
  x64)   py_key="cpython-$PY_VERSION-macos-x86_64-none";  want=x86_64
         /usr/bin/pgrep -q oahd || { echo "Rosetta 2 is required: softwareupdate --install-rosetta --agree-to-license"; exit 1; } ;;
  *) echo "MAC_ARCH must be arm64 or x64, got '$MAC_ARCH'"; exit 1 ;;
esac

cache="$HOME/Library/Caches/srr-ci"
uv_dir="$cache/uv-$UV_VERSION"
mkdir -p "$cache"
if [ ! -x "$uv_dir/uv" ]; then
  tgz="$cache/uv-$UV_VERSION-aarch64-apple-darwin.tar.gz"
  curl -fsSL --retry 3 --connect-timeout 20 \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz" -o "$tgz.part"
  got=$(shasum -a 256 "$tgz.part" | awk '{print $1}')
  if [ "$got" != "$UV_SHA256" ]; then
    mv "$tgz.part" "$tgz.bad-$(date +%Y%m%d%H%M%S)"
    echo "uv SHA-256 mismatch: expected $UV_SHA256, got $got (kept as $tgz.bad-*)"; exit 1
  fi
  mv "$tgz.part" "$tgz"
  mkdir -p "$uv_dir"
  tar xzf "$tgz" -C "$uv_dir" --strip-components 1
fi
export UV_PYTHON_INSTALL_DIR="$cache/uv-python"
"$uv_dir/uv" python install --quiet "$py_key"
base_py="$("$uv_dir/uv" python find --managed-python --no-project "$py_key")"

# A fresh venv per job (in the checkout), so no package state leaks between
# builds; pip's cache keeps the reinstall cheap.
"$base_py" -m venv .venv-ci
PY="$PWD/.venv-ci/bin/python"
export PY
export PIP_CACHE_DIR="$cache/pip-$MAC_ARCH" PIP_DISABLE_PIP_VERSION_CHECK=1

got_arch="$("$PY" -c 'import platform; print(platform.machine())')"
[ "$got_arch" = "$want" ] || { echo "Python runs as $got_arch, need $want"; exit 1; }
# Gate: this is a Tkinter app; refuse to build with a Python that lacks Tk 8.6.
tkv="$("$PY" -c 'import tkinter; tkinter.Tcl(); print(tkinter.TkVersion)')"
[ "$tkv" = "8.6" ] || { echo "tkinter check failed: got '$tkv' (need 8.6)"; exit 1; }
echo "toolchain: $("$PY" --version) $got_arch, Tk $tkv (uv $UV_VERSION)"
set +u
