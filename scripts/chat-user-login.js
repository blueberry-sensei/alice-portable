'use strict';

/**
 * Lấy refresh_token để Alice đọc Google Chat THAY người dùng thật — dùng khi
 * project GCP là "personal account" (không thuộc Workspace org nào), lúc đó
 * Google khoá cứng "Join spaces and group conversations" của mọi Chat app,
 * nên đường service-account/app-authentication (`add-chat-app-to-space.js` +
 * `collector.js#serviceAccountToken`) không bao giờ hoạt động được — đo thật
 * 2026-08-14: 403 "disabled by the developer" dù cấu hình đúng hết mọi ô.
 *
 * Lối này né hẳn khái niệm "Chat app": KHÔNG cần cấu hình Google Chat API,
 * KHÔNG cần thêm gì vào Space. Chỉ cần người dùng (đã sẵn là thành viên của
 * Space) đăng nhập một lần, cho Alice mượn quyền ĐỌC tin nhắn — scope
 * `chat.messages.readonly` ở dạng user-authentication này KHÔNG cần Workspace
 * admin phê duyệt (khác với app-authentication cùng scope, xem ghi chú trong
 * `collector.js`).
 *
 * Chạy MỘT LẦN:
 *
 *   $env:ELECTRON_RUN_AS_NODE = '1'
 *   & "node_modules\electron\dist\electron.exe" scripts\chat-user-login.js `
 *     --client-id=XXXX.apps.googleusercontent.com --client-secret=YYYY `
 *     --out=D:\Work\erp\alice-portable\Alice-K-OS\chat-user.json
 *
 * client-id/client-secret: CÙNG loại "OAuth client ID — Desktop app" đã tạo cho
 * `add-chat-app-to-space.js` (dùng lại được, không cần tạo cái mới). File JSON
 * sinh ra dán thẳng vào ô "đường dẫn file service account" trong Cài đặt → Báo
 * cáo tuần — `collector.js#chatMessages` tự nhận diện qua field `refresh_token`.
 *
 * `refresh_token` không tự hết hạn (trừ khi bạn tự thu hồi ở
 * myaccount.google.com/permissions, hoặc 6 tháng không dùng tới) — chỉ cần lấy
 * một lần.
 */

const fs = require('node:fs');
const path = require('node:path');
const { parseArgs, runLoopbackFlow } = require('./lib/google-oauth-loopback');

const SCOPE = 'https://www.googleapis.com/auth/chat.messages.readonly';

async function main() {
  const { 'client-id': clientId, 'client-secret': clientSecret, out } = parseArgs();
  if (!clientId || !clientSecret || !out) {
    console.error('Thiếu tham số. Dùng: --client-id=... --client-secret=... --out=đường-dẫn-file.json');
    process.exit(1);
  }

  const tokens = await runLoopbackFlow({ clientId, clientSecret, scope: SCOPE });
  if (!tokens.refresh_token) {
    throw new Error(
      'Google không trả refresh_token — có thể vì tài khoản này đã đồng ý quyền này '
      + 'trước đó (Google chỉ cấp refresh_token lần ĐẦU trừ khi buộc consent lại). '
      + 'Vào myaccount.google.com/permissions, gỡ quyền của app này rồi chạy lại.'
    );
  }

  const cred = {
    type: 'user',
    client_id: clientId,
    client_secret: clientSecret,
    refresh_token: tokens.refresh_token,
  };
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(cred, null, 2), 'utf8');
  console.log(`THÀNH CÔNG — đã lưu: ${out}`);
  console.log('Dán đường dẫn này vào ô "service account" của Báo cáo tuần trong Cài đặt.');
}

main().catch((err) => {
  console.error('THẤT BẠI:', err.message);
  process.exit(1);
});
