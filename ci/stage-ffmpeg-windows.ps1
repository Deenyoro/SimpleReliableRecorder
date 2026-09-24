# Stage a static ffmpeg.exe into .\ffmpeg\ffmpeg.exe for the Windows build.
#
#   .\ci\stage-ffmpeg-windows.ps1            # x64 (the only Windows runner)
#   .\ci\stage-ffmpeg-windows.ps1 -Arch arm64
#
# Same source and fallback as the "Stage ffmpeg (Windows)" step of
# .github/workflows/build.yml: BtbN's rolling "latest" GPL build, and when
# that 404s (BtbN empties and re-uploads it during the daily autobuild) the
# newest dated BtbN release that carries the asset. The release-list query
# is unauthenticated here (GitLab has no GitHub token; 60 requests/hour per
# IP is plenty for one query per build). BtbN's Windows builds are static.
# Works on Windows PowerShell 5.1 (the runner's shell) and PowerShell 7.
param([ValidateSet('x64', 'arm64')][string]$Arch = 'x64')
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$suffix = if ($Arch -eq 'arm64') { 'winarm64-gpl.zip' } else { 'win64-gpl.zip' }
$urls = @("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-$suffix")
try {
  $rels = Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/BtbN/FFmpeg-Builds/releases?per_page=10'
  foreach ($r in $rels) {
    $a = $r.assets | Where-Object { $_.name -like "*-$suffix" } | Select-Object -First 1
    if ($a -and $a.browser_download_url -ne $urls[0]) {
      $urls += $a.browser_download_url
      break
    }
  }
} catch { Write-Warning "BtbN release-list query failed: $_" }

New-Item -ItemType Directory -Force -Path ffmpeg | Out-Null
$downloaded = $false
foreach ($url in $urls) {
  Write-Host "Trying ffmpeg: $url"
  # -sS: no progress meter on stderr, errors still shown.
  curl.exe -sSfL --retry 4 --retry-all-errors --connect-timeout 20 --max-time 300 $url -o ff.zip
  if ($LASTEXITCODE -eq 0) { $downloaded = $true; break }
  Write-Warning "download failed: $url"
}
if (-not $downloaded) { throw "ffmpeg download failed from every source" }
Expand-Archive ff.zip -DestinationPath ffx -Force
$exe = Get-ChildItem ffx -Recurse -Filter ffmpeg.exe | Select-Object -First 1
if (-not $exe) { throw "no ffmpeg.exe found in the downloaded archive" }
Copy-Item $exe.FullName ffmpeg\ffmpeg.exe -Force
# Capture all output first: cutting the pipeline short with Select-Object
# can stop ffmpeg early and leave a meaningless $LASTEXITCODE.
$out = & .\ffmpeg\ffmpeg.exe -version
if ($LASTEXITCODE -ne 0) { throw "staged ffmpeg.exe does not run (exit $LASTEXITCODE)" }
Write-Host "Staged: $($out | Select-Object -First 1)"
