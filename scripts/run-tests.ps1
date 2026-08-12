# Chạy test bằng Electron-as-node, KHÔNG bằng node của máy.
#
# Lý do: `node:sqlite` cần Node >= 22, mà node trên PATH của máy này là v20.18.1.
# Electron 40 mang sẵn Node 24.15.0 — và đó cũng chính là runtime mà app sẽ chạy
# thật, nên test chạy đúng trên thứ sẽ ship (M-0060: Electron *là* node).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root 'node_modules\electron\dist\electron.exe'
if (-not (Test-Path $exe)) { throw "Chưa có electron: $exe (chạy npm install trước)" }

$env:ELECTRON_RUN_AS_NODE = '1'
$out = Join-Path $env:TEMP 'alice-test-out.txt'
$err = Join-Path $env:TEMP 'alice-test-err.txt'

# Liệt kê tường minh `*.test.js`: truyền cả thư mục thì node coi nó là một module và
# lỗi MODULE_NOT_FOUND; và `test/probe-runtime.js` không phải test, đưa vào là hỏng.
$files = Get-ChildItem (Join-Path $root 'test') -Filter '*.test.js' | ForEach-Object { $_.FullName }
if ($args.Count -gt 0) {
  $pattern = $args[0]
  $files = $files | Where-Object { $_ -like "*$pattern*" }
}
if (-not $files) { throw "Không có file test nào khớp trong $root\test" }
Write-Output ("Chạy: " + (($files | Split-Path -Leaf) -join ', '))

$p = Start-Process -FilePath $exe `
  -ArgumentList (@('--test') + $files) `
  -WorkingDirectory $root -NoNewWindow -Wait -PassThru `
  -RedirectStandardOutput $out -RedirectStandardError $err

Get-Content $out -Raw -Encoding utf8
$e = Get-Content $err -Raw -Encoding utf8
if ($e) { Write-Output "--- stderr ---"; Write-Output $e }
exit $p.ExitCode
