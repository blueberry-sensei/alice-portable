# Nhúng binary opencode vào app.
#
# `D-0053` mục 3 (và bài học M-0035): app gọi engine bằng ĐƯỜNG DẪN TUYỆT ĐỐI tới
# binary nằm trong chính nó — không `npx`, không dựa vào PATH. PATH của máy người
# dùng không được quyền quyết định app chạy bằng phiên bản nào.
#
# Đã kiểm: `opencode.exe` là binary ĐỘC LẬP (~167 MB). Copy một mình nó sang thư mục
# trống rồi chạy `--version` vẫn ra — không cần `node_modules` bên cạnh.

# `param()` PHẢI là câu lệnh đầu tiên của script (chú thích thì không tính). Đặt sau
# một phép gán là script chết lúc chạy, dù parser tĩnh không kêu gì.
param(
  # Đường dẫn tới opencode.exe. Không có thì tự dò.
  [string]$Source = $(if ($env:ALICE_OPENCODE_EXE) { $env:ALICE_OPENCODE_EXE } else { '' })
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$dst  = Join-Path $root 'runtime\opencode'

if (-not $Source) {
  $found = Get-Command opencode -ErrorAction SilentlyContinue
  $candidates = @()

  # `opencode` trên PATH thường là shim .ps1/.cmd của npm, không phải exe thật —
  # nên phải lần về gói `opencode-ai` chứ không copy cái shim.
  if ($found) {
    $shimDir = Split-Path $found.Source -Parent
    $candidates += Join-Path $shimDir 'node_modules\opencode-ai\bin\opencode.exe'
  }
  $nvm = Join-Path $env:APPDATA 'nvm'
  if (Test-Path $nvm) {
    foreach ($v in Get-ChildItem $nvm -Directory) {
      $candidates += Join-Path $v.FullName 'node_modules\opencode-ai\bin\opencode.exe'
    }
  }
  $candidates += Join-Path $env:USERPROFILE '.opencode\bin\opencode.exe'

  $Source = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

if (-not $Source -or -not (Test-Path $Source)) {
  throw @"
Không tìm thấy opencode.exe.
Cài opencode (https://opencode.ai/docs) rồi chạy lại, hoặc chỉ thẳng đường dẫn:
  `$env:ALICE_OPENCODE_EXE = 'C:\duong\dan\opencode.exe'
"@
}

New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item $Source (Join-Path $dst 'opencode.exe') -Force

# Nghiệm thu bằng cách chạy BẢN ĐÃ COPY, ở vị trí mới — không tin bản gốc chạy được
# thì bản copy cũng chạy được.
$ver = & (Join-Path $dst 'opencode.exe') --version
if ($LASTEXITCODE -ne 0) { throw "Binary đã copy không chạy được (mã $LASTEXITCODE)" }

$mb = '{0:N0} MB' -f ((Get-Item (Join-Path $dst 'opencode.exe')).Length / 1MB)
Write-Output "Xong: opencode $ver ($mb) -> runtime\opencode\"
