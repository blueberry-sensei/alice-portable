#!/usr/bin/env bash
# Cài Alice trên macOS / Linux bằng một lệnh.
#
#   curl -fsSL https://raw.githubusercontent.com/blueberry-sensei/alice-portable/main/install.sh | bash
#
# Script chỉ làm ba việc và nói ra từng việc trước khi làm:
#   1. Hỏi GitHub bản mới nhất.
#   2. Tải file hợp với máy này (.dmg cho macOS, .AppImage cho Linux).
#   3. Mở nó lên (macOS) hoặc đặt vào ~/Applications và cấp quyền chạy (Linux).
#
# Không sudo, không đụng /usr, không sửa file hệ thống.
set -euo pipefail

REPO="blueberry-sensei/alice-portable"

say()  { printf '  %s\n' "$*"; }
oops() { printf '\n  \033[31m%s\033[0m\n\n' "$*" >&2; }

printf '\n  Alice — cài đặt\n  ---------------\n\n'

# ── nhận diện máy ──────────────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Darwin) PATTERN='\.dmg$' ; KIND=macOS ;;
  Linux)  PATTERN='\.AppImage$' ; KIND=Linux ;;
  *)      oops "Chưa hỗ trợ hệ điều hành \"$OS\"."
          say  "Windows thì dùng lệnh PowerShell trong README nhé."
          exit 1 ;;
esac

# Máy Apple Silicon và Intel dùng file khác nhau; nếu bản phát hành có tách theo
# kiến trúc thì ưu tiên đúng cái, không có thì lấy cái chung.
case "$ARCH" in
  arm64|aarch64) ARCH_HINT='arm64' ;;
  x86_64|amd64)  ARCH_HINT='x64' ;;
  *)             ARCH_HINT='' ;;
esac

say "[1/3] Máy: $KIND ($ARCH). Đang hỏi bản mới nhất..."

command -v curl >/dev/null 2>&1 || { oops 'Máy chưa có curl. Cài curl rồi chạy lại giúp mình nhé.'; exit 1; }

API="https://api.github.com/repos/$REPO/releases/latest"
if ! JSON="$(curl -fsSL -H 'User-Agent: alice-installer' "$API" 2>/dev/null)"; then
  # Phân biệt "chưa có bản nào" với "mạng hỏng" — bảo sai thì người ta đi sửa nhầm chỗ.
  CODE="$(curl -s -o /dev/null -w '%{http_code}' -H 'User-Agent: alice-installer' "$API" || echo 000)"
  case "$CODE" in
    404) oops 'Chưa có bản phát hành nào để tải.'
         say  'Nhắn cho người đưa app cho bạn — họ cần đăng một bản lên.' ;;
    403) oops 'GitHub đang chặn tạm (quá nhiều lượt hỏi). Chờ khoảng 10 phút rồi chạy lại nhé.' ;;
    *)   oops 'Không hỏi được GitHub. Kiểm tra mạng rồi chạy lại giúp mình nhé.' ;;
  esac
  exit 1
fi

TAG="$(printf '%s' "$JSON" | grep -o '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"

# Lấy mọi link tải, lọc theo đuôi file hợp với máy, ưu tiên bản đúng kiến trúc.
URLS="$(printf '%s' "$JSON" | grep -o '"browser_download_url"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\(https[^"]*\)"$/\1/')"
URL="$(printf '%s\n' "$URLS" | grep -E "$PATTERN" | { [ -n "$ARCH_HINT" ] && grep -- "$ARCH_HINT" || cat; } | head -1)"
[ -n "$URL" ] || URL="$(printf '%s\n' "$URLS" | grep -E "$PATTERN" | head -1)"

if [ -z "$URL" ]; then
  oops "Bản $TAG chưa có file cho $KIND."
  say  "Xem tại: https://github.com/$REPO/releases"
  exit 1
fi

# Chỉ nhận link của chính GitHub. Script cài đặt thì tuyệt đối không tải file lạ về chạy.
case "$URL" in
  https://github.com/*|https://objects.githubusercontent.com/*) ;;
  *) oops 'Link tải không phải của GitHub — dừng lại cho chắc.'; say "($URL)"; exit 1 ;;
esac

NAME="$(basename "$URL")"
DEST="$HOME/Downloads/$NAME"
mkdir -p "$HOME/Downloads"

say ''
say "[2/3] Đang tải $NAME"
say '      (nặng vài trăm MB — tuỳ mạng có thể mất vài phút. Cứ để đó.)'
curl -fL --progress-bar -o "$DEST" "$URL"

say ''
if [ "$KIND" = macOS ]; then
  say '[3/3] Mở file cài...'
  say ''
  say '      Kéo Alice vào thư mục Applications.'
  say '      Lần đầu mở, macOS có thể báo "Alice cannot be opened" —'
  say '      chuột phải vào Alice → Open → Open.'
  open "$DEST"
else
  APPDIR="$HOME/Applications"
  mkdir -p "$APPDIR"
  mv -f "$DEST" "$APPDIR/Alice.AppImage"
  chmod +x "$APPDIR/Alice.AppImage"
  say "[3/3] Đã đặt vào $APPDIR/Alice.AppImage"
  say ''
  say '      Chạy bằng cách bấm đúp trong file manager, hoặc gõ:'
  say "        $APPDIR/Alice.AppImage"
  say ''
  say '      Nếu báo thiếu FUSE: sudo apt install libfuse2'
fi

say ''
say 'Xong phần của mình. Lần đầu mở, Alice sẽ xin một API key — làm theo màn hình là được.'
say ''
