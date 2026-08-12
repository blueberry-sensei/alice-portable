# Đồng bộ NGUỒN BRAIN vào `brain-source/` trong repo này.
#
# Vì sao phải vendor: alice-brain và alice-core là repo PRIVATE, mà CI của
# alice-portable là repo PUBLIC — không clone được. Cách alice-coding dùng
# (kéo image Docker GHCR public) không áp dụng được: image là binary Linux,
# bản portable cần cài wheel đúng từng hệ điều hành.
#
# Giá phải trả: nguồn brain trong repo này ĐÓNG BĂNG theo release. Sau mỗi lần
# alice-brain/alice-core đổi, chạy lại script này rồi mới build/release.
#
#   npm run brain:sync-source
#   # hoặc: .\scripts\sync-brain-source.ps1 -FromRoot E:\ALICE

param(
  # Thư mục CHỨA hai repo `alice-brain` và `alice-core`.
  [string]$FromRoot = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root      = Split-Path -Parent $PSScriptRoot
$dest      = Join-Path $root 'brain-source'

if (-not $FromRoot) { $FromRoot = Join-Path (Split-Path $root -Parent) '' }
$brainRepo = Join-Path $FromRoot 'alice-brain'
$coreRepo  = Join-Path $FromRoot 'alice-core'

foreach ($r in @($brainRepo, $coreRepo)) {
  if (-not (Test-Path (Join-Path $r '.git'))) {
    throw "Không thấy repo tại $r — truyền -FromRoot <thư mục chứa alice-brain và alice-core>."
  }
}

function Copy-Tree([string]$src, [string]$dstName) {
  $target = Join-Path $dest $dstName
  if (Test-Path $target) { Remove-Item $target -Recurse -Force }
  New-Item -ItemType Directory -Force -Path $dest | Out-Null
  Copy-Item -Path $src -Destination $target -Recurse -Force
  if (-not (Test-Path $target)) { throw "Copy hỏng: $src → $target" }
}

Write-Output "Nguồn: $brainRepo"
Write-Output "       $coreRepo"

Copy-Tree (Join-Path $brainRepo 'apps\api\sag_api')   'sag_api'
Copy-Tree (Join-Path $brainRepo 'apps\api\sag_agent') 'sag_agent'
Copy-Tree (Join-Path $coreRepo 'src\alicecore')       'alicecore'

# Rác không cần thiết cho build: bytecode biên dịch, cache test.
Get-ChildItem $dest -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Get-ChildItem $dest -Recurse -File -Filter '*.pyc' | Remove-Item -Force

# Ghi bản nguồn (commit hash) để ai cũng biết đây là bản nào.
$note = @(
  'Nguồn brain đóng băng cho bản portable — KHÔNG sửa tay.',
  'Sau khi alice-brain / alice-core đổi, chạy:  npm run brain:sync-source',
  '',
  "alice-brain: $(git -C $brainRepo rev-parse HEAD)",
  "alice-core:  $(git -C $coreRepo rev-parse HEAD)",
  "sync lúc:    $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
) -join "`n"
Set-Content -Path (Join-Path $dest 'VERSION.txt') -Value $note -Encoding utf8

$size = '{0:N0} KB' -f ((Get-ChildItem $dest -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
Write-Output "Xong. brain-source = $size (đã ghi VERSION.txt)"
