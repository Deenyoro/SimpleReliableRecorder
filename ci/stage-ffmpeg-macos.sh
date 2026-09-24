#!/usr/bin/env bash
# Stage a STATIC, portable ffmpeg into ./ffmpeg/ffmpeg for the macOS build.
#
#   bash ci/stage-ffmpeg-macos.sh arm64    # or x64
#
# Same sources, order and gates as the "Stage ffmpeg (macOS)" step of
# .github/workflows/build.yml. Never falls back to Homebrew's ffmpeg
# (dynamically linked against /opt/homebrew, so it only runs on the CI box).
#
# Sources are chosen PER ARCH (an x86_64 binary passes "-version" under
# Rosetta on Apple Silicon, so a run check alone is not enough). Each
# candidate must pass three gates before acceptance:
#   1. arch gate:        lipo -archs must report the target arch
#   2. portability gate: otool -L may reference only /usr/lib and /System
#                        (no /opt/homebrew, /usr/local)
#   3. run gate:         ffmpeg -version must exit 0
set -eu

arch="${1:-arm64}"
mkdir -p ffmpeg

try_source() {
  url="$1"; expected="$2"
  echo "Trying ffmpeg source: $url (need $expected)"
  curl -fL --retry 2 --retry-all-errors --connect-timeout 20 \
       --max-time 300 --retry-max-time 360 \
       --speed-limit 200000 --speed-time 30 \
       "$url" -o ff.zip || { echo "  download failed"; return 1; }
  if ! file ff.zip | grep -qi 'zip'; then
    echo "  not a zip archive"; return 1
  fi
  # A previous candidate's extract is moved aside, not deleted.
  if [ -e ffx ]; then mv ffx "ffx.prev-$(date +%s)-$$-$RANDOM"; fi
  mkdir ffx
  unzip -o -q ff.zip -d ffx || { echo "  unzip failed"; return 1; }
  bin="$(find ffx -name ffmpeg -type f | head -n1)"
  [ -n "$bin" ] || { echo "  no ffmpeg in archive"; return 1; }
  cp "$bin" ffmpeg/ffmpeg && chmod +x ffmpeg/ffmpeg
  # Apple Silicon requires at least an ad-hoc signature to execute.
  codesign --force --sign - ffmpeg/ffmpeg >/dev/null 2>&1 || true
  if ! lipo -archs ffmpeg/ffmpeg | grep -q "$expected"; then
    echo "  rejected: wrong arch ($(lipo -archs ffmpeg/ffmpeg 2>/dev/null)), need $expected"
    return 1
  fi
  if otool -L ffmpeg/ffmpeg | tail -n +2 | awk '{print $1}' \
       | grep -Ev '^(/usr/lib/|/System/)' | grep -q .; then
    echo "  rejected: links non-system libraries (not portable):"
    otool -L ffmpeg/ffmpeg | tail -n +2 | awk '{print $1}' \
      | grep -Ev '^(/usr/lib/|/System/)' | sed 's/^/    /'
    return 1
  fi
  ./ffmpeg/ffmpeg -version >/dev/null 2>&1 || { echo "  binary does not run"; return 1; }
  return 0
}

staged=0
case "$arch" in
  arm64)
    # osxexperts publishes native arm64 static builds; the URL embeds the
    # ffmpeg version, so try the current one first, then the previous one.
    for url in \
      "https://www.osxexperts.net/ffmpeg81arm.zip" \
      "https://www.osxexperts.net/ffmpeg711arm.zip"; do
      if try_source "$url" arm64; then staged=1; break; fi
    done
    ;;
  x64)
    # evermeet ships static x86_64 builds; osxexperts intel is the backup.
    for url in \
      "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip" \
      "https://www.osxexperts.net/ffmpeg80intel.zip"; do
      if try_source "$url" x86_64; then staged=1; break; fi
    done
    ;;
  *) echo "usage: $0 arm64|x64" >&2; exit 2 ;;
esac
if [ "$staged" -ne 1 ]; then
  echo "ERROR: could not stage a static $arch ffmpeg for macOS." >&2
  echo "Refusing to fall back to Homebrew (dynamically linked, CI-only); failing the build." >&2
  exit 1
fi
echo "Staged: $(./ffmpeg/ffmpeg -version | head -1) [$(lipo -archs ffmpeg/ffmpeg)]"
