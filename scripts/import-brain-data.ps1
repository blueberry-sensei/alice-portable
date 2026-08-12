# Nạp tri thức đã tích luỹ từ brain đang chạy vào BẢN DÙNG RIÊNG của bạn.
#
# ⚠️ Thứ này KHÔNG đi vào bộ cài đem phát. Alice trong bộ cài khởi đầu với brain
# RỖNG và tự đắp dần — tri thức của một project là dữ liệu của người đó, nhét vào
# bộ cài phát cho người khác là phát tán dữ liệu nhầm chỗ.
#
# Copy hai thứ:
#   - `sag.db`   — SQLite: source, document, chunk, entity, telemetry
#   - `engine/`  — LanceDB: vector đã embed (phần nặng, vài trăm MB)

# `param()` PHẢI đứng trước mọi câu lệnh (chú thích không tính).
param(
  # Xem ghi chú ở bundle-brain.ps1: tên container không được ghi cứng.
  [string]$Container = $(if ($env:ALICE_BRAIN_CONTAINER) { $env:ALICE_BRAIN_CONTAINER } else { '' })
)

$ErrorActionPreference = 'Stop'

$root    = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $root 'alice-data\brain'

if (-not $Container) {
  Write-Output "Chưa chỉ container brain. Các container đang chạy:"
  wsl -e docker ps --format "  {{.Names}}  ({{.Status}})"
  throw "Đặt `$env:ALICE_BRAIN_CONTAINER hoặc truyền -Container <ten>."
}
$container = $Container

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

function To-WslPath([string]$p) {
  $full = [System.IO.Path]::GetFullPath($p)
  return "/mnt/" + $full.Substring(0, 1).ToLower() + $full.Substring(2).Replace('\', '/')
}
$wsl = To-WslPath $dataDir

# WAL phải được gộp vào file chính TRƯỚC khi copy. SQLite ở chế độ WAL giữ phần ghi
# mới nhất trong `sag.db-wal` (ở đây đang là 4MB); copy mỗi `sag.db` là copy một bản
# thiếu đúng phần mới nhất — mà "mới nhất" chính là tri thức vừa sync.
Write-Output "[1/3] Checkpoint WAL…"
wsl -e docker exec $container python -c "import sqlite3; c=sqlite3.connect('/data/sag.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close(); print('wal checkpoint ok')"

Write-Output "[2/3] Copy sag.db…"
# Xoá cả `-wal` và `-shm`, không chỉ file chính. Một WAL mồ côi của lần chạy trước
# nằm cạnh một `sag.db` vừa được thay là đường tới đọc nhầm dữ liệu — SQLite sẽ coi
# WAL đó là phần ghi mới hơn và áp lên một database nó không thuộc về.
foreach ($f in @('sag.db', 'sag.db-wal', 'sag.db-shm')) {
  $p = Join-Path $dataDir $f
  if (Test-Path $p) { Remove-Item $p -Force }
}
wsl -e docker cp "${container}:/data/sag.db" "$wsl/sag.db"

Write-Output "[3/3] Copy LanceDB…"
# LanceDB có **37.208 file nhỏ**. `docker cp` một thư mục như thế ra `/mnt/d` đi qua
# 9p của WSL và bò với tốc độ ~1MB/phút — đo thật, bỏ chạy sau 20 phút mới được 6MB.
#
# Đường nhanh: gói thành MỘT file tar (ghi tuần tự, 9p chịu được), rồi giải nén bằng
# `tar` của Windows ngay trên NTFS.
$engine = Join-Path $dataDir 'engine'
$tar = Join-Path $dataDir 'engine.tar'
if (Test-Path $engine) { Remove-Item $engine -Recurse -Force }
if (Test-Path $tar) { Remove-Item $tar -Force }

wsl -e bash -c "docker exec $container tar cf - -C /data engine > '$wsl/engine.tar'"
if (-not (Test-Path $tar)) { throw "Không tạo được engine.tar" }
$tarMb = (Get-Item $tar).Length / 1MB
if ($tarMb -lt 100) { throw ("engine.tar chỉ {0:N0} MB — quá nhỏ so với ~650MB, chắc chắn đứt giữa chừng." -f $tarMb) }

Write-Output ("      tar {0:N0} MB, đang giải nén…" -f $tarMb)
tar -xf $tar -C $dataDir
if ($LASTEXITCODE -ne 0) { throw "tar -xf hỏng (mã $LASTEXITCODE)" }
Remove-Item $tar -Force

# Nghiệm thu bằng SỐ ĐẾM, không tin lệnh chạy xong là xong. Bản trước của script này
# báo "Xong" sau khi lệnh copy bị giết giữa chừng — đúng kiểu "xong giả".
$files = (Get-ChildItem $engine -Recurse -File).Count
$engMb = (Get-ChildItem $engine -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
if ($files -lt 1000) { throw "Chỉ giải nén được $files file — nguồn có hơn 37.000." }

$dbSize = '{0:N1} MB' -f ((Get-Item (Join-Path $dataDir 'sag.db')).Length / 1MB)
Write-Output ("Xong. sag.db = {0} · engine = {1:N0} MB, {2:N0} file" -f $dbSize, $engMb, $files)
Write-Output "Lưu ý: SAG_SECRET_KEY của bản portable KHÁC key của container, nên credential"
Write-Output "provider lưu trong bảng settings sẽ không giải mã được — nhập lại trong app."
