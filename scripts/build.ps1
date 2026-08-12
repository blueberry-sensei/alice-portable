# Đóng gói bản portable.
#
# Chạy electron-builder bằng **Node 24**, không bằng `node` trên PATH.
# Lý do: PATH của máy này là v20.18.1, mà electron-builder 26 kéo `@noble/hashes` v2
# vốn là ESM-only → `require()` nó từ CommonJS ném `ERR_REQUIRE_ESM` và build chết
# trước khi đóng gói được gì. Đây đúng họ `M-0035`: đừng để PATH của máy quyết định
# công cụ chạy bằng runtime nào — chỉ thẳng vào bản đúng.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$node24 = @(
  "$env:APPDATA\nvm\v24.14.0\node.exe",
  "$env:APPDATA\nvm\v22.12.0\node.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $node24) { throw "Không tìm thấy Node >= 22. electron-builder 26 không chạy trên Node 20." }

$builder = Join-Path $root 'node_modules\electron-builder\out\cli\cli.js'
if (-not (Test-Path $builder)) { throw "Chưa cài electron-builder: $builder" }

$env:ELECTRON_MIRROR = 'https://npmmirror.com/mirrors/electron/'
$env:ELECTRON_BUILDER_BINARIES_MIRROR = 'https://npmmirror.com/mirrors/electron-builder-binaries/'

# ── Dựng bản seed tri thức TRƯỚC khi đóng gói ──────────────────────────────
# Bộ cài NSIS đóng từ những gì electron-builder biết, nên tri thức phải nằm trong
# `runtime/` (đi vào extraResources) từ trước. Chép sau khi build thì chỉ thư mục
# `win-unpacked` có, còn người cài bằng bộ cài sẽ nhận một brain rỗng.
$srcBrain  = Join-Path $root 'alice-data\brain'
$seedBrain = Join-Path $root 'runtime\brain-seed'
if ($env:ALICE_SKIP_BRAIN_DATA -eq '1') {
  Write-Output "Bỏ qua tri thức (ALICE_SKIP_BRAIN_DATA=1) — bản build sẽ KHÔNG có recall."
  if (Test-Path $seedBrain) { Remove-Item $seedBrain -Recurse -Force }
} elseif (Test-Path (Join-Path $srcBrain 'sag.db')) {
  Write-Output "Dựng bản seed tri thức…"
  if (Test-Path $seedBrain) { Remove-Item $seedBrain -Recurse -Force }
  New-Item -ItemType Directory -Force -Path $seedBrain | Out-Null
  # `.secret_key` KHÔNG đi theo: mỗi bản cài tự sinh khoá của nó.
  robocopy $srcBrain $seedBrain /E /NFL /NDL /NJH /NJS /NP /XF '.secret_key' | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "robocopy hỏng (mã $LASTEXITCODE)" }
  $n = (Get-ChildItem $seedBrain -Recurse -File).Count
  if ($n -lt 1000) { throw "Bản seed chỉ có $n file — nguồn có hơn 37.000." }
  Write-Output ("      {0:N0} file" -f $n)
} else {
  Write-Warning "Không có $srcBrain — chạy ``npm run import:brain-data`` nếu muốn bản build có recall."
}

Write-Output "Build bằng $node24"
Push-Location $root
try {
  # Không truyền `--win dir`: target lấy từ electron-builder.yml (nsis + dir), nên
  # một lần build ra cả bộ cài lẫn thư mục mang đi được.
  & $node24 $builder --win
  if ($LASTEXITCODE -ne 0) { throw "electron-builder thoát mã $LASTEXITCODE" }
} finally {
  Pop-Location
}

$out = Join-Path $root 'dist\win-unpacked'
if (-not (Test-Path (Join-Path $out 'Alice.exe'))) { throw "Không thấy Alice.exe trong $out" }

# Tri thức đã đi theo `runtime/brain-seed` ở bước trên, nên KHÔNG chép lại vào
# `win-unpacked/alice-data` nữa: app tự bung seed ở lần chạy đầu. Chép cả hai nơi là
# nhân đôi 550MB trên đĩa mà không được gì.
#
# Thứ KHÔNG bao giờ đi theo bản phát: `alice.db` (lịch sử chat), `alice-data/opencode`
# (API key + session), `.secret_key`. Đó là dữ liệu riêng của máy build.

$setup = Get-ChildItem (Join-Path $root 'dist') -Filter 'Alice-Setup-*.exe' -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1

$dirSize = '{0:N0} MB' -f ((Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Output ""
Write-Output "Xong."
Write-Output "  Bộ cài (đưa cho người dùng): $(if ($setup) { "$($setup.FullName)  ({0:N0} MB)" -f ($setup.Length/1MB) } else { 'KHÔNG TẠO ĐƯỢC' })"
Write-Output "  Thư mục mang đi được       : $out  ($dirSize)"
if (-not $setup) { throw "Không thấy bộ cài Alice-Setup-*.exe trong dist\" }
