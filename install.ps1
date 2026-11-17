# AI Bench installer (Windows 10/11).
#
#   irm https://raw.githubusercontent.com/vietlanvietlan2000-crypto/AI-benchmark/main/install.ps1 | iex
#
# What it does / Viec no lam:
#   [1/4] check Windows            kiem tra Windows
#   [2/4] choose install folder    chon thu muc cai dat
#   [3/4] download AI Bench        tai ma nguon AI Bench
#   [4/4] install uv + Python + libraries, then start the setup wizard
#         cai uv + Python + thu vien, roi mo trinh cai dat tung buoc
#         (kiem tra phan cung, cai LM Studio, chon noi luu model, goi y va tai model)
#
# Optional environment variables / Bien moi truong tuy chon:
#   AIBENCH_DIR     install folder (skips the question)
#   AIBENCH_SOURCE  local folder to copy from instead of downloading from GitHub
#   AIBENCH_NO_SETUP=1  do not start the setup wizard
#
# This file is kept ASCII-only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.

& {
    $ErrorActionPreference = 'Stop'
    $Repo   = 'vietlanvietlan2000-crypto/AI-benchmark'
    $Branch = 'main'
    $Total  = 4

    function Step($n, $text) { Write-Host ("`n[{0}/{1}] {2}" -f $n, $Total, $text) -ForegroundColor Cyan }
    function Ok($text)   { Write-Host "  [OK] $text" -ForegroundColor Green }
    function Note($text) { Write-Host "       $text" }
    function Fail($text) { Write-Host "  [X]  $text" -ForegroundColor Red; throw $text }

    Write-Host "`n  AI BENCH - installer / trinh cai dat" -ForegroundColor Cyan
    try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    # ---- 1. Windows -------------------------------------------------------------------------
    Step 1 'Checking Windows / Kiem tra Windows'
    if ($env:OS -ne 'Windows_NT') { Fail 'AI Bench needs Windows 10 or 11. / Can Windows 10 hoac 11.' }
    if (-not [Environment]::Is64BitOperatingSystem) { Fail 'A 64-bit Windows is required. / Can Windows 64-bit.' }
    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 17763) { Fail "Windows build $build is too old (need 1809 or newer). / Windows qua cu." }
    Ok "Windows build $build, 64-bit"

    # ---- 2. Install folder ------------------------------------------------------------------
    Step 2 'Install folder / Thu muc cai dat'
    $default = Join-Path $env:LOCALAPPDATA 'AIBench'
    Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Free } | ForEach-Object {
        Note ("{0}:\  free / con trong {1:N0} GB" -f $_.Name, ($_.Free / 1GB))
    }
    $dir = $env:AIBENCH_DIR
    if (-not $dir) {
        $dir = Read-Host "  ? Folder / Thu muc (Enter = $default)"
        if (-not $dir) { $dir = $default }
    }
    $dir = $dir.Trim('"')
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $dir = (Resolve-Path $dir).Path
    Ok "Install to / Cai vao: $dir"

    # ---- 3. Download ------------------------------------------------------------------------
    Step 3 'Downloading AI Bench / Tai ma nguon AI Bench'
    if ($env:AIBENCH_SOURCE) {
        $src = (Resolve-Path $env:AIBENCH_SOURCE).Path
        Note "Copying from / Chep tu: $src"
    } else {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ("aibench-" + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        $zip = Join-Path $tmp 'src.zip'
        $url = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
        Note $url
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Note 'Extracting / Dang giai nen...'
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $src = (Get-ChildItem -Path $tmp -Directory | Select-Object -First 1).FullName
    }
    # Copy over the program files. Your results/ and invoice output stay untouched (they are not in the repo).
    # Chep de len file chuong trinh. Thu muc results/ va ket qua hoa don cua ban duoc giu nguyen.
    robocopy $src $dir /E /NFL /NDL /NJH /NJS /NP /XD .git .venv .claude __pycache__ results | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "Copy failed (robocopy code $LASTEXITCODE). / Chep file loi." }
    if ($tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
    if (-not (Test-Path (Join-Path $dir 'pyproject.toml'))) { Fail 'Download looks incomplete (pyproject.toml missing). / Tai chua du.' }
    Ok 'Source ready / Ma nguon da san sang'

    # ---- 4. uv + Python + libraries ---------------------------------------------------------
    Step 4 'Installing uv, Python and libraries / Cai uv, Python va thu vien'
    $uvBin = Join-Path $env:USERPROFILE '.local\bin'
    if (-not (Get-Command uv -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $uvBin 'uv.exe'))) { $env:Path = "$uvBin;$env:Path" }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Note 'uv is missing, installing it / Chua co uv, dang cai...'
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $env:Path = "$uvBin;$env:Path"
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail 'uv could not be installed. See https://docs.astral.sh/uv/ / Khong cai duoc uv.' }
    }
    Ok ("uv: " + (uv --version))
    Note 'Downloading Python and libraries (1-3 minutes) / Dang tai Python va thu vien (1-3 phut)...'
    uv sync --directory $dir
    if ($LASTEXITCODE -ne 0) { Fail 'uv sync failed. / Cai thu vien loi.' }
    Ok 'Python and libraries installed / Da cai Python va thu vien'
    if ($env:AIBENCH_NO_SETUP -ne '1') {
        uv run --directory $dir python -m aibench setup
    }

    Write-Host "`n  Done / Xong.  Start / Mo ung dung:  $dir\run.bat`n" -ForegroundColor Green
}
