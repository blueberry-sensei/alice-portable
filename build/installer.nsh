; Giữ DỮ LIỆU CỦA NGƯỜI DÙNG khi cài đè / gỡ app.
;
; Vì sao phải thủ công: uninstaller mặc định của electron-builder chạy
; `RMDir /r $INSTDIR` — xoá sạch cả `alice-data` (lịch sử chat, API key, brain).
; Các bản v0.1.2 trở về trước phát hành đúng theo cách đó, nên khách cài đè lên
; bản cũ là mất hết dữ liệu mà không được báo trước.
;
; Ba macro dưới đây làm ba việc:
;   1. customInit         — installer MỚI chạy TRƯỚC khi gọi uninstaller cũ:
;                           chép `alice-data` sang `$TEMP` an toàn.
;   2. customInstall      — sau khi file mới đã vào chỗ, chép trả lại (nếu có bản sao).
;   3. customRemoveFiles  — khi gỡ app (hoặc upgrade giữa các bản MỚI): chỉ xoá
;                           file app, giữ nguyên `alice-data`.

!define ALICE_DATA_BAK "$TEMP\alice-portable-data-bak"

; Đếm xem `alice-data` có file thật nào không — thư mục vừa tạo rỗng thì không
; phải dữ liệu của người dùng, không cần backup.
!macro aliceDataHasFiles RESULT
  StrCpy ${RESULT} "0"
  ${if} ${FileExists} "$INSTDIR\alice-data\*.*"
    StrCpy ${RESULT} "1"
  ${endIf}
!macroend

!macro customInit
  Push $0
  !insertmacro aliceDataHasFiles $0
  ${if} $0 == "1"
    RMDir /r "${ALICE_DATA_BAK}"
    CreateDirectory "${ALICE_DATA_BAK}"
    CopyFiles "$INSTDIR\alice-data\*.*" "${ALICE_DATA_BAK}\"
  ${endIf}
  Pop $0
!macroend

!macro customInstall
  Push $0
  ${if} ${FileExists} "${ALICE_DATA_BAK}\*.*"
    CreateDirectory "$INSTDIR\alice-data"
    CopyFiles "${ALICE_DATA_BAK}\*.*" "$INSTDIR\alice-data\"
  ${endIf}
  RMDir /r "${ALICE_DATA_BAK}"
  Pop $0
!macroend

!macro customRemoveFiles
  ; Xoá file app, GIỮ `alice-data` — đúng lời hứa "gỡ app không xoá lịch sử chat".
  ; (File uninstaller đang chạy không tự xoá được chính nó — để lại là chấp nhận được.)
  Delete "$INSTDIR\*.*"
  Delete "$INSTDIR\${UNINSTALL_FILENAME}"
  RMDir /r "$INSTDIR\resources"
  RMDir /r "$INSTDIR\locales"
  RMDir /r "$INSTDIR\swiftshader"
  RMDir "$INSTDIR"
!macroend
