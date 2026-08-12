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

Write-Output "Build bằng $node24"
Push-Location $root
try {
  & $node24 $builder --win dir
  if ($LASTEXITCODE -ne 0) { throw "electron-builder thoát mã $LASTEXITCODE" }
} finally {
  Pop-Location
}

$out = Join-Path $root 'dist\win-unpacked'
if (-not (Test-Path (Join-Path $out 'Alice.exe'))) { throw "Không thấy Alice.exe trong $out" }

# Tri thức của brain đi theo bản build, nhưng KHÔNG đi kèm lịch sử chat và KHÔNG đi
# kèm API key.
#
# `runtime/brain` chỉ là mã nguồn + thư viện; không có `alice-data/brain` thì brain
# lên được mà DB rỗng — chưa cả schema — và mọi truy vấn recall chết ở
# `no such table: sources`. Một brain rỗng là giảm năng lực recall xuống 0, đúng thứ
# `D-0053` mục 2 cấm.
#
# Ngược lại, `alice.db` (chat) và `alice-data/opencode/data` (auth + session) là dữ
# liệu RIÊNG của máy này — nhân bản chúng vào một bản phát đi là phát tán cả nhật ký
# hội thoại lẫn credential.
$srcBrain = Join-Path $root 'alice-data\brain'
$dstBrain = Join-Path $out 'alice-data\brain'
if ($env:ALICE_SKIP_BRAIN_DATA -eq '1') {
  Write-Output "Bỏ qua dữ liệu brain (ALICE_SKIP_BRAIN_DATA=1) — bản build sẽ KHÔNG có recall."
} elseif (Test-Path $srcBrain) {
  Write-Output "Chép tri thức brain vào bản build…"
  if (Test-Path $dstBrain) { Remove-Item $dstBrain -Recurse -Force }
  New-Item -ItemType Directory -Force -Path (Split-Path $dstBrain) | Out-Null
  # `.secret_key` KHÔNG chép: mỗi bản cài tự sinh khoá của nó.
  robocopy $srcBrain $dstBrain /E /NFL /NDL /NJH /NJS /NP /XF '.secret_key' | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "robocopy hỏng (mã $LASTEXITCODE)" }
  $n = (Get-ChildItem $dstBrain -Recurse -File).Count
  if ($n -lt 1000) { throw "Chỉ chép được $n file brain — nguồn có hơn 37.000." }
  Write-Output "      $('{0:N0}' -f $n) file"
} else {
  Write-Warning "Không có $srcBrain — chạy scripts/import-brain-data.ps1 nếu muốn bản build có recall."
}

$size = '{0:N0} MB' -f ((Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Output "Xong: $out = $size"
