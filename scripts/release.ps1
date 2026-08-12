# Phát hành một bản lên GitHub Releases.
#
# Chạy sau `npm run build`. Cần một GitHub token có quyền `contents: write`:
#   $env:GITHUB_TOKEN = '...'      (token do BẠN tạo, script không bao giờ in nó ra)
#
# Vì sao cần bước này: `install.ps1` mà khách chạy đi hỏi
# `/releases/latest` — không có release thì lệnh một-dòng đó không có gì để tải.
param(
  # Số phiên bản. Bỏ trống thì lấy từ package.json.
  [string]$Version = '',
  [string]$Repo = 'blueberry-sensei/alice-portable',
  [switch]$Draft
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

if (-not $env:GITHUB_TOKEN) {
  throw @"
Chưa có token. Tạo ở https://github.com/settings/tokens (quyền: contents write), rồi:
  `$env:GITHUB_TOKEN = 'token-cua-ban'
Script không lưu và không in token ra bất cứ đâu.
"@
}

if (-not $Version) {
  $Version = (Get-Content (Join-Path $root 'package.json') -Raw | ConvertFrom-Json).version
}
$tag = "v$Version"

$setup = Get-ChildItem (Join-Path $root 'dist') -Filter 'Alice-Setup-*.exe' -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $setup) { throw "Không thấy bộ cài trong dist\ — chạy ``npm run build`` trước." }

$sizeMb = [math]::Round($setup.Length / 1MB)
# GitHub chặn asset > 2GB. Biết trước còn hơn upload một tiếng rồi mới bị từ chối.
if ($setup.Length -ge 2GB) {
  throw "Bộ cài $sizeMb MB, vượt giới hạn 2GB/file của GitHub Release. Tỉa bớt runtime hoặc tách tri thức ra bản tải riêng."
}

Write-Output "Phát hành $tag — $($setup.Name) ($sizeMb MB) → $Repo"

$headers = @{
  Authorization = "Bearer $env:GITHUB_TOKEN"
  Accept        = 'application/vnd.github+json'
  'User-Agent'  = 'alice-release'
}

# Release đã có thì dùng lại, không tạo trùng.
$release = $null
try {
  $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/tags/$tag" -Headers $headers
  Write-Output "  Release $tag đã có, dùng lại."
} catch {
  $body = @{
    tag_name = $tag
    name     = "Alice $Version"
    draft    = [bool]$Draft
    body     = @"
## Cài Alice

Dán dòng này vào **PowerShell** rồi Enter:

``````
irm https://raw.githubusercontent.com/$Repo/main/install.ps1 | iex
``````

Hoặc tải thẳng **Alice-Setup-$Version.exe** ở dưới rồi bấm đúp.

Lần đầu mở app sẽ xin một API key — làm theo hướng dẫn trên màn hình là xong.
"@
  } | ConvertTo-Json
  $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases" `
                               -Method Post -Headers $headers -Body $body -ContentType 'application/json'
  Write-Output "  Đã tạo release $tag."
}

# Asset trùng tên thì xoá bản cũ, nếu không GitHub từ chối.
foreach ($a in $release.assets) {
  if ($a.name -eq $setup.Name) {
    Write-Output "  Xoá asset cũ cùng tên..."
    Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/assets/$($a.id)" -Method Delete -Headers $headers | Out-Null
  }
}

$uploadUrl = $release.upload_url -replace '\{.*\}', ''
Write-Output "  Đang upload (chậm, cứ để đó)..."
Invoke-RestMethod -Uri "${uploadUrl}?name=$($setup.Name)" -Method Post -Headers $headers `
                  -ContentType 'application/octet-stream' -InFile $setup.FullName -TimeoutSec 7200 | Out-Null

Write-Output ""
Write-Output "Xong: https://github.com/$Repo/releases/tag/$tag"
Write-Output "Khách cài bằng:  irm https://raw.githubusercontent.com/$Repo/main/install.ps1 | iex"
