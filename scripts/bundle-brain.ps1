# Đóng gói Alice Brain vào bản portable — KHÔNG Docker (D-0053 mục 2).
#
# Vì sao không copy site-packages của image: image là Linux/x86_64, trong đó có
# lancedb, pyarrow, tokenizers, numpy, pydantic-core… đều là binary `.so`. Bê sang
# Windows là bê một đống file không nạp được. Cách đúng:
#
#   - `sag_api` + `sag_agent` + `alicecore` là THUẦN PYTHON (đã đếm: 0 .so, 0 .pyd)
#     → lấy nguồn từ GitHub (mặc định) hoặc từ container đang chạy.
#   - Phần native cài lại từ PyPI bằng wheel ĐÚNG HỆ ĐIỀU HÀNH.
#   - Python: Windows dùng bản **embeddable** của python.org (giải nén là chạy);
#     macOS/Linux dùng cây python 3.11+ có sẵn (thường do CI cài) copy nguyên vào
#     runtime — cũng là "giải nén là chạy", không cần quyền cài.
#
# Feedback khách 2026-08-12: bản phát cho khách PHẢI có sẵn brain (recall), nên
# đường `github` là mặc định — CI dựng được mà không cần container nào.
#
# Chạy được nhiều lần; bước nào xong rồi thì bỏ qua.

# `param()` PHẢI đứng trước mọi câu lệnh (chú thích không tính).
param(
  # Nguồn lấy code brain:
  #   github   (mặc định) — clone alice-brain + alice-core từ GitHub, dùng cho CI
  #   container            — copy từ một container Alice Brain đang chạy (cách cũ)
  #   local                — copy từ thư mục đã có sẵn hai repo (phát triển nhanh)
  [ValidateSet('github', 'container', 'local')]
  [string]$Source = 'github',

  # Tên container brain đang chạy khi -Source container. KHÔNG ghi cứng: mỗi người
  # dựng brain của mình với một tên khác, và một script chỉ chạy được trên đúng
  # một máy thì vô dụng với người thứ hai.
  #   $env:ALICE_BRAIN_CONTAINER = 'ten-container-cua-ban'
  #   hoặc: .\bundle-brain.ps1 -Source container -Container ten-container-cua-ban
  [string]$Container = $(if ($env:ALICE_BRAIN_CONTAINER) { $env:ALICE_BRAIN_CONTAINER } else { '' }),

  # Nhánh/tag của hai repo khi -Source github.
  [string]$Ref = 'main',

  # Thư mục CHỨA hai repo `alice-brain` và `alice-core` khi -Source local.
  [string]$LocalRoot = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root      = Split-Path -Parent $PSScriptRoot
$runtime   = Join-Path $root 'runtime/brain'
$pyDir     = Join-Path $runtime 'python'
$appDir    = Join-Path $runtime 'app'
$pyVersion = '3.12.10'

$winOs = $env:OS -eq 'Windows_NT'

New-Item -ItemType Directory -Force -Path $runtime, $appDir | Out-Null

# ── 1. Python ──────────────────────────────────────────────────────────────
if ($winOs) {
  $pyExe = Join-Path $pyDir 'python.exe'
  if (-not (Test-Path $pyExe)) {
    Write-Output "[1/5] Tải Python $pyVersion embeddable…"
    $zip = Join-Path $env:TEMP "python-embed-$pyVersion.zip"
    if (-not (Test-Path $zip)) {
      Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 `
        -Uri "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip" -OutFile $zip
    }
    New-Item -ItemType Directory -Force -Path $pyDir | Out-Null
    Expand-Archive -Path $zip -DestinationPath $pyDir -Force
  } else {
    Write-Output "[1/5] Python embeddable đã có."
  }

  # Bản embeddable mặc định TẮT site-packages: `._pth` phải bỏ chú thích `import site`,
  # nếu không thì pip cài xong mà `import` vẫn không thấy gì.
  $pth = Get-ChildItem $pyDir -Filter 'python*._pth' | Select-Object -First 1
  if ($pth) {
    $content = Get-Content $pth.FullName
    if ($content -match '^\s*#\s*import site') {
      ($content -replace '^\s*#\s*import site', 'import site') + @('Lib\site-packages', '..\app') |
        Set-Content $pth.FullName -Encoding ascii
      Write-Output "      bật site-packages trong $($pth.Name)"
    }
  }
} else {
  # macOS/Linux: dùng python3 có sẵn (CI cài setup-python 3.11+), copy nguyên cây
  # prefix vào runtime. Cây này tự chứa libpython + site-packages nên sau khi copy
  # vẫn tự chạy được — đúng tinh thần portable.
  $pyExe = Join-Path $pyDir 'bin/python3'
  if (-not (Test-Path $pyExe)) {
    $probe = Get-Command python3 -ErrorAction SilentlyContinue
    if (-not $probe) { throw "Không tìm thấy python3 trên PATH (macOS/Linux cần Python 3.11+)." }
    Write-Output "[1/5] Copy cây python ($(& python3 -c 'import sys; print(sys.version.split()[0])')) vào runtime…"
    $prefix = (& python3 -c 'import sys; print(sys.prefix)').Trim()
    if (-not $prefix) { throw "Không đọc được sys.prefix của python3." }
    Copy-Item -Path $prefix -Destination $pyDir -Recurse -Force
  } else {
    Write-Output "[1/5] Python đã có."
  }
  # Copy-Item giữ bit chạy, nhưng chắc chắn thì chmod lại một lần.
  & chmod +x "$pyExe"
  if ($LASTEXITCODE -ne 0) { Write-Output "      (chmod không chạy được — bỏ qua)" }
}

# ── 2. pip ─────────────────────────────────────────────────────────────────
# Kiểm bằng SỰ CÓ MẶT CỦA FILE, không bằng cách chạy `python -c "import pip"`.
# Trong PowerShell 5.1, `2>$null` trên một native command làm mỗi dòng stderr thành
# một ErrorRecord — với $ErrorActionPreference='Stop' thì "pip chưa có" (một tình
# huống BÌNH THƯỜNG ở bước này) giết luôn cả script.
if ($winOs) {
  $hasPip = Test-Path (Join-Path $pyDir 'Lib/site-packages/pip')
  if (-not $hasPip) {
    Write-Output "[2/5] Cài pip…"
    $getpip = Join-Path $env:TEMP 'get-pip.py'
    if (-not (Test-Path $getpip)) {
      Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getpip
    }
    & $pyExe $getpip --no-warn-script-location
  } else {
    Write-Output "[2/5] pip đã có."
  }
} else {
  Write-Output "[2/5] pip có sẵn trong python."
}

# ── 3. Nguồn thuần Python ──────────────────────────────────────────────────
Write-Output "[3/5] Lấy sag_api + sag_agent + alicecore (nguồn: $Source)…"

function Copy-SourceDir([string]$src, [string]$dst) {
  $target = Join-Path $appDir $dst
  if (Test-Path $target) { Remove-Item $target -Recurse -Force }
  Copy-Item -Path $src -Destination $target -Recurse -Force
  if (-not (Test-Path $target)) { throw "Copy hỏng: $src → $target" }
}

if ($Source -eq 'github') {
  $srcDir = Join-Path $env:TEMP "alice-bundle-src-$Ref"
  New-Item -ItemType Directory -Force -Path $srcDir | Out-Null
  foreach ($repo in @('alice-brain', 'alice-core')) {
    $repoDir = Join-Path $srcDir $repo
    if (-not (Test-Path (Join-Path $repoDir '.git'))) {
      Write-Output "      clone $repo ($Ref)…"
      git clone --depth 1 --branch $Ref "https://github.com/blueberry-sensei/$repo.git" $repoDir
      if ($LASTEXITCODE -ne 0) { throw "Clone $repo hỏng (mã $LASTEXITCODE)." }
    } else {
      Write-Output "      $repo đã có sẵn."
    }
  }
  Copy-SourceDir (Join-Path $srcDir 'alice-brain/apps/api/sag_api')   'sag_api'
  Copy-SourceDir (Join-Path $srcDir 'alice-brain/apps/api/sag_agent') 'sag_agent'
  Copy-SourceDir (Join-Path $srcDir 'alice-core/src/alicecore')       'alicecore'
} elseif ($Source -eq 'local') {
  if (-not $LocalRoot) { throw "-Source local cần -LocalRoot <thư mục chứa alice-brain và alice-core>." }
  Copy-SourceDir (Join-Path $LocalRoot 'alice-brain/apps/api/sag_api')   'sag_api'
  Copy-SourceDir (Join-Path $LocalRoot 'alice-brain/apps/api/sag_agent') 'sag_agent'
  Copy-SourceDir (Join-Path $LocalRoot 'alice-core/src/alicecore')       'alicecore'
} else {
  if (-not $Container) {
    Write-Output "Chưa chỉ container brain. Các container đang chạy:"
    wsl -e docker ps --format "  {{.Names}}  ({{.Status}})"
    throw "Đặt `$env:ALICE_BRAIN_CONTAINER hoặc truyền -Container <ten>."
  }
  # `docker cp` chứ KHÔNG phải `docker exec tar | Set-Content`: PowerShell 5.1 chuyển
  # stdout của native command thành CHUỖI, nên mọi byte nhị phân qua pipe đều hỏng
  # ("Cannot proceed with byte encoding"). docker cp ghi thẳng ra đĩa, không qua pipe.
  function To-WslPath([string]$p) {
    $full = [System.IO.Path]::GetFullPath($p)
    $drive = $full.Substring(0, 1).ToLower()
    return "/mnt/$drive" + $full.Substring(2).Replace('\', '/')
  }
  $appWsl = To-WslPath $appDir
  $sitePkgs = '/usr/local/lib/python3.12/site-packages'
  foreach ($item in @(
      @{ src = "/app/sag_api";   dst = "sag_api" },
      @{ src = "/app/sag_agent"; dst = "sag_agent" },
      @{ src = "$sitePkgs/alicecore"; dst = "alicecore" }
    )) {
    $target = Join-Path $appDir $item.dst
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    wsl -e docker cp "${container}:$($item.src)" "$appWsl/$($item.dst)"
    if (-not (Test-Path $target)) { throw "docker cp hỏng: $($item.src) — container $container còn chạy không?" }
  }
}

# `__pycache__` là bytecode biên dịch cho CPython của NƠI COPY. Trên máy người dùng
# nó vô dụng và chỉ tổ phình bundle; Python sẽ tự sinh lại bản của nó.
Get-ChildItem $appDir -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Write-Output "      $(@(Get-ChildItem $appDir -Recurse -Filter '*.py').Count) file .py"

# ── 4. Dependency native, wheel đúng hệ điều hành ──────────────────────────
Write-Output "[4/5] pip install dependency (mất vài phút)…"
$deps = @(
  # sag_api
  'fastapi>=0.115', 'uvicorn[standard]>=0.30', 'sqlalchemy[asyncio]>=2.0', 'aiosqlite>=0.20',
  'pydantic>=2.7', 'pydantic-settings>=2.3', 'pyjwt>=2.8', 'bcrypt>=4.1',
  'cryptography>=42,<47', 'python-multipart>=0.0.9', 'sse-starlette>=2.1', 'httpx>=0.27',
  'litellm>=1.92,<2', 'orjson>=3.11.6,<4', 'alembic>=1.13', 'trafilatura>=1.8',
  'charset-normalizer>=3.3,<4', 'mcp>=1.28,<2', 'tzdata>=2024.1',
  # alicecore
  'aiohttp>=3.9', 'json-repair>=0.58', 'jsonschema>=4', 'lancedb>=0.16', 'numpy>=1.26',
  'openai>=1.6', 'pyyaml>=6', 'tiktoken>=0.5', 'tokenizers>=0.22'
)
& $pyExe -m pip install --no-warn-script-location --disable-pip-version-check @deps
if ($LASTEXITCODE -ne 0) { throw "pip install hỏng (mã $LASTEXITCODE)" }

# `markitdown` tách riêng: nặng và CHỈ cần cho ingest tài liệu, không cần cho recall.
# Thiếu nó thì `search`/`grep`/`get_entity` vẫn chạy — nên không để nó chặn cả bước.
& $pyExe -m pip install --no-warn-script-location --disable-pip-version-check 'markitdown[docx,pdf,pptx,xls,xlsx]>=0.1.6,<0.2'
if ($LASTEXITCODE -ne 0) { Write-Warning "markitdown cài không được — recall vẫn chạy, chỉ mất phần ingest tài liệu." }

# ── 5. Nghiệm thu: import thật, không tin là xong ──────────────────────────
Write-Output "[5/5] Nghiệm thu import…"
$env:PYTHONPATH = $appDir
& $pyExe -c "import sag_api, alicecore, lancedb, mcp; print('OK', sag_api.__name__, alicecore.__name__, lancedb.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Import hỏng — bundle chưa dùng được." }

$size = '{0:N0} MB' -f ((Get-ChildItem $runtime -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Output "Xong. runtime\brain = $size"
