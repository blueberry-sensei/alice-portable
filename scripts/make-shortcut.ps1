# Tạo lối tắt Alice ngoài Desktop.
#
# Bản portable cố ý không ghi vào Start Menu hay registry — cài đặt kiểu đó là thứ
# phải gỡ, mà "portable" nghĩa là xoá thư mục là xong. Lối tắt thì chỉ là một file
# .lnk, xoá lúc nào cũng được.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$exe  = Join-Path $root 'dist\win-unpacked\Alice.exe'

if (-not (Test-Path $exe)) {
  throw "Chưa có $exe — chạy ``npm run build`` trước đã."
}

$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Alice.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath       = $exe
$s.WorkingDirectory = Split-Path $exe -Parent
$s.Description      = 'Alice — bản portable'
$s.Save()

Write-Output "Xong: $lnk"
