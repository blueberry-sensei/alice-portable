# Cài Alice bằng một lệnh.
#
# Khách chạy:
#   irm https://raw.githubusercontent.com/blueberry-sensei/alice-portable/main/install.ps1 | iex
#
# Script này chỉ làm ba việc, và nói ra từng việc trước khi làm:
#   1. Hỏi GitHub xem bản mới nhất là bản nào.
#   2. Tải bộ cài về Downloads.
#   3. Mở bộ cài lên.
#
# Nó KHÔNG tự cài chui, KHÔNG sửa registry, KHÔNG đụng file nào khác. Bộ cài hiện ra
# vẫn hỏi bạn cài vào đâu.
#
# File nằm ở gốc repo (không phải trong scripts/) để đường dẫn raw ngắn và dễ đọc —
# một câu lệnh mà khách phải dán thì mỗi ký tự thừa đều là một chỗ để gõ sai.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Repo = 'blueberry-sensei/alice-portable'

function Say($msg) { Write-Host $msg }
function Oops($msg) { Write-Host "" ; Write-Host "  $msg" -ForegroundColor Red ; Write-Host "" }

Say ""
Say "  Alice — cài đặt"
Say "  ---------------"
Say ""

# ── 1. Bản mới nhất ────────────────────────────────────────────────────────
Say "  [1/3] Đang hỏi bản mới nhất..."
try {
  $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" `
                           -Headers @{ 'User-Agent' = 'alice-installer' } -TimeoutSec 30
} catch {
  # Phân biệt 404 với lỗi mạng. Bảo "kiểm tra mạng" khi thật ra chưa có bản phát
  # hành nào thì khách sẽ đi sửa nhầm chỗ, và sửa mãi không xong.
  $code = $null
  try { $code = [int]$_.Exception.Response.StatusCode } catch { }
  if ($code -eq 404) {
    Oops "Chưa có bản phát hành nào để tải."
    Say  "  Nhắn cho người đưa app cho bạn — họ cần đăng một bản lên."
  } elseif ($code -eq 403) {
    Oops "GitHub đang chặn tạm (quá nhiều lượt hỏi). Chờ khoảng 10 phút rồi chạy lại nhé."
  } else {
    Oops "Không hỏi được GitHub. Kiểm tra mạng rồi chạy lại giúp mình nhé."
    Say  "  (chi tiết: $($_.Exception.Message))"
  }
  return
}

$asset = $rel.assets | Where-Object { $_.name -like 'Alice-Setup-*.exe' } | Select-Object -First 1
if (-not $asset) {
  Oops "Bản $($rel.tag_name) chưa có bộ cài cho Windows."
  Say  "  Xem tại: https://github.com/$Repo/releases"
  return
}

$sizeMb = [math]::Round($asset.size / 1MB)
Say "        Bản $($rel.tag_name) · $sizeMb MB"

# ── 2. Tải ─────────────────────────────────────────────────────────────────
$dest = Join-Path ([Environment]::GetFolderPath('UserProfile')) "Downloads\$($asset.name)"
Say ""
Say "  [2/3] Đang tải về $dest"
Say "        ($sizeMb MB — tuỳ mạng, có thể mất vài phút. Cứ để đó.)"

# Chỉ nhận link của chính GitHub. Nếu API trả về domain khác thì có gì đó không ổn,
# và một script cài đặt thì tuyệt đối không được tải file lạ về chạy.
if ($asset.browser_download_url -notmatch '^https://(github\.com|objects\.githubusercontent\.com)/') {
  Oops "Link tải không phải của GitHub — dừng lại cho chắc."
  Say  "  ($($asset.browser_download_url))"
  return
}

try {
  Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $dest -UseBasicParsing -TimeoutSec 3600
} catch {
  Oops "Tải không xong. Mạng có thể bị ngắt giữa chừng — chạy lại lệnh này là được."
  Say  "  (chi tiết: $($_.Exception.Message))"
  return
}

$got = (Get-Item $dest).Length
if ($got -ne $asset.size) {
  Oops "File tải về không đủ ($([math]::Round($got/1MB)) MB / $sizeMb MB). Chạy lại giúp mình nhé."
  Remove-Item $dest -Force -ErrorAction SilentlyContinue
  return
}

# ── 3. Mở bộ cài ───────────────────────────────────────────────────────────
Say ""
Say "  [3/3] Mở bộ cài..."
Say ""
Say "        Windows có thể hiện bảng xanh 'Windows protected your PC'."
Say "        Bấm 'More info' rồi 'Run anyway' — bảng đó hiện vì app chưa mua chữ ký số."
Say ""
Start-Process -FilePath $dest

Say "  Xong phần của mình. Làm theo bộ cài là chạy được Alice."
Say ""
