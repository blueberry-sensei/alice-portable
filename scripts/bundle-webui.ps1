# Đóng gói Dashboard Alice Brain (`apps/web` của blueberry-sensei/alice-brain,
# Next.js) vào bản portable — xem docs/superpowers/specs/2026-08-13-brain-
# dashboard-design.md.
#
# Không vendor SOURCE vào repo này (khác `brain-source/`): Next.js build ra
# hàng nghìn file JS đã minify, vendor source vô nghĩa. Chỉ vendor BẢN ĐÃ BUILD
# (`runtime/webui/`), luôn build FRESH từ GitHub mỗi lần chạy script này —
# alice-brain giờ đã PUBLIC nên CI clone thẳng được, không cần vendor riêng như
# brain-source (vốn phải vendor vì alice-brain/alice-core từng là private).
#
# Chạy được nhiều lần; xoá `runtime/webui` cũ rồi build lại từ đầu mỗi lần.

param(
  # Nhánh/tag của alice-brain để build apps/web.
  [string]$Ref = 'main',

  # Cổng CỐ ĐỊNH của sag_api.desktop mà dashboard sẽ gọi — đóng cứng lúc build
  # (Next.js inline biến NEXT_PUBLIC_* vào bundle client, không đọc lại lúc
  # chạy). PHẢI khớp cổng `BrainSidecar` dùng khi bật cho dashboard (main.js,
  # `alice:brain:open`) — đổi một bên mà quên đổi bên kia là dashboard gọi API
  # sai cổng, luôn báo "Network error".
  [string]$ApiPort = '8932'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

$root    = Split-Path -Parent $PSScriptRoot
$dest    = Join-Path $root 'runtime/webui'
$work    = Join-Path ([System.IO.Path]::GetTempPath()) "alice-webui-build-$([guid]::NewGuid().ToString('N').Substring(0,8))"

Write-Output "Dựng dashboard từ blueberry-sensei/alice-brain@$Ref vào $dest"

if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
New-Item -ItemType Directory -Force -Path $work | Out-Null

try {
  git clone --depth 1 --branch $Ref https://github.com/blueberry-sensei/alice-brain.git $work/alice-brain
  if ($LASTEXITCODE -ne 0) { throw "git clone alice-brain hỏng (mã $LASTEXITCODE)" }

  $webDir = Join-Path $work 'alice-brain/apps/web'
  if (-not (Test-Path $webDir)) { throw "Không thấy apps/web trong bản clone" }

  # `outputFileTracingRoot`: KHÔNG để Next.js tự đoán workspace root. Đo thật
  # 2026-08-13: máy dev có lockfile lạc ở thư mục cha (ngoài repo), Next.js chọn
  # nhầm làm root và standalone output lồng theo NGUYÊN đường dẫn tuyệt đối của
  # máy đó — trên Windows vỡ luôn vì vượt trần độ dài đường dẫn. Ép rõ ràng để
  # kết quả build KHÔNG phụ thuộc máy/thư mục nào đang chạy script.
  $configPath = Join-Path $webDir 'next.config.mjs'
  $config = Get-Content $configPath -Raw
  if ($config -notmatch 'outputFileTracingRoot') {
    $config = $config -replace `
      "(?s)(const withNextIntl = createNextIntlPlugin\([^)]*\);)", `
      "`$1`nimport path from `"node:path`";`nimport { fileURLToPath } from `"node:url`";`nconst __dirname = path.dirname(fileURLToPath(import.meta.url));"
    $config = $config -replace 'output: "standalone",', "output: `"standalone`",`n  outputFileTracingRoot: __dirname,"
    Set-Content -Path $configPath -Value $config -NoNewline
  }

  Push-Location $webDir
  try {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw "npm ci hỏng (mã $LASTEXITCODE)" }

    $env:NEXT_PUBLIC_API_BASE = "http://127.0.0.1:$ApiPort"
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "next build hỏng (mã $LASTEXITCODE)" }
  } finally {
    Pop-Location
    Remove-Item Env:\NEXT_PUBLIC_API_BASE -ErrorAction SilentlyContinue
  }

  $standalone = Join-Path $webDir '.next/standalone'
  if (-not (Test-Path (Join-Path $standalone 'server.js'))) {
    throw "Build xong nhưng không thấy server.js ở $standalone — outputFileTracingRoot chưa đúng?"
  }

  New-Item -ItemType Directory -Force -Path $dest | Out-Null
  Copy-Item "$standalone\*" $dest -Recurse -Force
  # Next.js standalone KHÔNG tự kèm static/public — phải copy tay (xem docs
  # Next.js "output: standalone" caveats; đã xác minh thật 2026-08-13: thiếu
  # bước này thì server chạy nhưng mọi CSS/JS/ảnh trả 404).
  New-Item -ItemType Directory -Force -Path (Join-Path $dest '.next') | Out-Null
  Copy-Item (Join-Path $webDir '.next/static') (Join-Path $dest '.next/static') -Recurse -Force
  Copy-Item (Join-Path $webDir 'public') (Join-Path $dest 'public') -Recurse -Force

  Write-Output "Xong — $dest ($([math]::Round((Get-ChildItem $dest -Recurse | Measure-Object Length -Sum).Sum / 1MB)) MB)"
} finally {
  Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
