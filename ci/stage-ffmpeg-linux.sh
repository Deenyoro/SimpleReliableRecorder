#!/usr/bin/env bash
# Stage a STATIC, portable ffmpeg into ./ffmpeg/ffmpeg for the Linux build.
#
#   bash ci/stage-ffmpeg-linux.sh x64      # or arm64
#
# Same sources, order and gates as the "Stage ffmpeg (Linux)" step of
# .github/workflows/build.yml. Policy: only STATIC builds that actually run
# are accepted. If no static source can be staged and verified the script
# FAILS - it never falls back to a runner-local (apt) dynamically linked
# binary, because that produces a release that only runs in CI.
set -eu

arch="${1:-x64}"
mkdir -p ffmpeg

# Portability gate: statically linked, or it will only run on machines with
# the exact same shared libraries as the build container.
is_static() {
  ldd "$1" 2>&1 | grep -q "not a dynamic executable" && return 0
  file "$1" | grep -qi "static"
}

# Accept a source only if it yields a STATIC binary that actually runs.
# Retries with timeouts so a single flaky mirror cannot fail the build.
stage_tarxz() {
  url="$1"
  echo "Trying ffmpeg (tar.xz): $url"
  # --retry-max-time caps ALL retries in one budget and the speed floor
  # aborts a trickling mirror, so a dying server costs at most ~6 minutes.
  curl -fL --retry 2 --retry-all-errors --connect-timeout 20 \
       --max-time 300 --retry-max-time 360 \
       --speed-limit 200000 --speed-time 30 \
       "$url" -o ff.tar.xz || { echo "  download failed"; return 1; }
  # A previous candidate's extract is moved aside, not deleted.
  if [ -e ffx ]; then mv ffx "ffx.prev-$(date +%s%N)"; fi
  mkdir ffx
  tar xf ff.tar.xz -C ffx || { echo "  extract failed"; return 1; }
  bin="$(find ffx -name ffmpeg -type f | head -n1)"
  [ -n "$bin" ] || { echo "  no ffmpeg in archive"; return 1; }
  cp "$bin" ffmpeg/ffmpeg && chmod +x ffmpeg/ffmpeg
  is_static ffmpeg/ffmpeg || { echo "  rejected: dynamically linked (not portable)"; return 1; }
  ./ffmpeg/ffmpeg -version >/dev/null 2>&1 || { echo "  binary does not run"; return 1; }
  return 0
}

# Same gates for sources that publish the binary directly (no tar).
stage_rawbin() {
  url="$1"
  echo "Trying ffmpeg (raw binary): $url"
  curl -fL --retry 2 --retry-all-errors --connect-timeout 20 \
       --max-time 300 --retry-max-time 360 \
       --speed-limit 200000 --speed-time 30 \
       "$url" -o ffmpeg/ffmpeg || { echo "  download failed"; return 1; }
  chmod +x ffmpeg/ffmpeg
  is_static ffmpeg/ffmpeg || { echo "  rejected: dynamically linked (not portable)"; return 1; }
  ./ffmpeg/ffmpeg -version >/dev/null 2>&1 || { echo "  binary does not run"; return 1; }
  return 0
}

# Four candidates per arch, GitHub-hosted sources first: BtbN sometimes
# ships dynamically linked linux builds (the gate rejects them fast) and
# johnvansickle can degrade into an alive-but-crawling server, so it is
# consulted last, behind the speed floor.
case "$arch" in
  x64)
    stage_tarxz "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" \
      || stage_rawbin "https://github.com/eugeneware/ffmpeg-static/releases/latest/download/ffmpeg-linux-x64" \
      || stage_tarxz "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" \
      || stage_tarxz "https://johnvansickle.com/ffmpeg/builds/ffmpeg-git-amd64-static.tar.xz" \
      || { echo "ERROR: no static ffmpeg source succeeded for linux-x64." >&2
           echo "Refusing to ship a dynamically linked fallback; failing the build." >&2; exit 1; }
    ;;
  arm64)
    stage_tarxz "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linuxarm64-gpl.tar.xz" \
      || stage_rawbin "https://github.com/eugeneware/ffmpeg-static/releases/latest/download/ffmpeg-linux-arm64" \
      || stage_tarxz "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz" \
      || stage_tarxz "https://johnvansickle.com/ffmpeg/builds/ffmpeg-git-arm64-static.tar.xz" \
      || { echo "ERROR: no static ffmpeg source succeeded for linux-arm64." >&2
           echo "Refusing to ship a dynamically linked fallback; failing the build." >&2; exit 1; }
    ;;
  *) echo "usage: $0 x64|arm64" >&2; exit 2 ;;
esac
echo "Staged: $(./ffmpeg/ffmpeg -version | head -1)"
