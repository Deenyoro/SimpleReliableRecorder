"""Build wrapper: produce one self-contained SimpleReliableRecorder executable.

Cross-platform (Windows / macOS / Linux). Steps:
  1. Stage an ffmpeg binary into ./ffmpeg/ (from FFMPEG_EXE env, ./ffmpeg, or PATH).
  2. Run PyInstaller against simplereliablerecorder.spec (onefile, windowed).
  3. Report the resulting executable path + size.

Usage:
  python build.py            # build
  python build.py --clean    # remove build/ and dist/ first
"""

import os
import shutil
import subprocess
import sys
import time

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
FF_NAME = "ffmpeg.exe" if IS_WIN else "ffmpeg"
EXE_NAME = "SimpleReliableRecorder.exe" if IS_WIN else "SimpleReliableRecorder"


def get_size_mb(path):
    return os.path.getsize(path) / (1024 * 1024) if os.path.isfile(path) else 0


def stage_ffmpeg(project_dir):
    """Ensure ffmpeg/<ffmpeg> exists; copy it from a discovered source."""
    dest_dir = os.path.join(project_dir, "ffmpeg")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, FF_NAME)
    if os.path.isfile(dest) and get_size_mb(dest) > 5:
        print(f"ffmpeg already staged: {dest} ({get_size_mb(dest):.0f} MB)")
        return True

    candidates = []
    env = os.environ.get("FFMPEG_EXE")
    if env:
        candidates.append(env)
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)

    for src in candidates:
        try:
            if os.path.isfile(src) and get_size_mb(src) > 5:
                print(f"Copying ffmpeg from {src} ...")
                shutil.copy2(src, dest)
                if not IS_WIN:
                    os.chmod(dest, 0o755)
                print(f"Staged {FF_NAME} ({get_size_mb(dest):.0f} MB)")
                return True
        except Exception as e:
            print(f"  could not copy {src}: {e}", file=sys.stderr)

    print("WARNING: no ffmpeg staged. Screen recording will not work in the build.\n"
          "         Set FFMPEG_EXE to a full ffmpeg build and rebuild.",
          file=sys.stderr)
    return False


def main():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    spec = os.path.join(project_dir, "simplereliablerecorder.spec")

    if "--clean" in sys.argv:
        for d in ("build", "dist"):
            p = os.path.join(project_dir, d)
            if os.path.isdir(p):
                print(f"Removing {d}/ ...")
                shutil.rmtree(p, ignore_errors=True)

    stage_ffmpeg(project_dir)

    print("\n" + "=" * 60)
    print("  Building SimpleReliableRecorder (onefile)")
    print("=" * 60)
    t0 = time.time()
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", spec, "--noconfirm"],
        cwd=project_dir)
    if result.returncode != 0:
        print(f"\nBuild FAILED (exit {result.returncode})", file=sys.stderr)
        sys.exit(result.returncode)

    exe = os.path.join(project_dir, "dist", EXE_NAME)
    print("\n" + "=" * 60)
    print("  Build SUCCEEDED")
    print("=" * 60)
    print(f"  Time: {time.time() - t0:.0f}s")
    if os.path.isfile(exe):
        if not IS_WIN:
            try:
                os.chmod(exe, 0o755)
            except Exception:
                pass
        print(f"  EXE:  {exe}")
        print(f"  Size: {get_size_mb(exe):.0f} MB")
    else:
        print("  WARNING: expected executable not found.", file=sys.stderr)


if __name__ == "__main__":
    main()
