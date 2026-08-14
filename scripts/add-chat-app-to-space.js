'use strict';

/**
 * Thêm Chat app (đã cấu hình ở Google Cloud Console, KHÔNG phải service account)
 * vào một Google Chat Space — bước MỘT LẦN cho mỗi Space.
 *
 * Vì sao không dùng service account: `spaces.members.create` với member đặc biệt
 * `users/app` CHỈ chấp nhận user-authentication (OAuth người dùng thật, scope
 * `chat.memberships.app`) — app không tự thêm được chính nó bằng app-authentication
 * (xem developers.google.com/workspace/chat/create-members). Đây là lý do dán
 * email service account vào ô "Thêm người và ứng dụng" không hoạt động, và vì sao
 * app mới cấu hình chưa chắc đã hiện ra trong ô tìm kiếm đó ngay.
 *
 * CHỈ DÙNG ĐƯỢC khi project GCP thuộc một Google Workspace org — nếu Cloud
 * Console báo project "No organization" (tài khoản cá nhân), Google khoá cứng
 * "Join spaces and group conversations" của MỌI Chat app trong project đó, script
 * này sẽ luôn thất bại với lỗi 403 "disabled by the developer" dù cấu hình đúng
 * hết. Trường hợp đó dùng `chat-user-login.js` thay thế — đọc chat bằng OAuth của
 * chính người dùng, không cần app/service-account gì cả.
 *
 * Chạy MỘT LẦN, bằng chính Electron-as-node (không cần cài Node riêng, giống
 * scripts/run-tests.ps1 — máy này có Node 20 trên PATH):
 *
 *   $env:ELECTRON_RUN_AS_NODE = '1'
 *   & "node_modules\electron\dist\electron.exe" scripts\add-chat-app-to-space.js `
 *     --client-id=XXXX.apps.googleusercontent.com --client-secret=YYYY --space=AAQAecMzBo4
 *
 * Lấy client-id/client-secret: Cloud Console → Credentials → Create Credentials →
 * OAuth client ID → Application type "Desktop app". Trước đó phải cấu hình xong
 * "OAuth consent screen".
 *
 * `--space` là ID trong URL Chat (`chat.google.com/room/<ID>`) — chấp nhận cả dạng
 * trần lẫn `spaces/<ID>`.
 */

const { parseArgs, runLoopbackFlow } = require('./lib/google-oauth-loopback');

const SCOPE = 'https://www.googleapis.com/auth/chat.memberships.app';

async function addAppToSpace(accessToken, space) {
  const parent = /^spaces\//.test(space) ? space : `spaces/${space}`;
  const res = await fetch(`https://chat.googleapis.com/v1/${parent}/members`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ member: { name: 'users/app', type: 'BOT' } }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`Chat API trả HTTP ${res.status}: ${JSON.stringify(data)}`);
  return data;
}

async function main() {
  const { 'client-id': clientId, 'client-secret': clientSecret, space } = parseArgs();
  if (!clientId || !clientSecret || !space) {
    console.error('Thiếu tham số. Dùng: --client-id=... --client-secret=... --space=AAQAecMzBo4');
    process.exit(1);
  }

  const tokens = await runLoopbackFlow({ clientId, clientSecret, scope: SCOPE });
  console.log(`Đang thêm app vào space ${space}...`);
  const result = await addAppToSpace(tokens.access_token, space);
  console.log('THÀNH CÔNG:', JSON.stringify(result, null, 2));
}

main().catch((err) => {
  console.error('THẤT BẠI:', err.message);
  process.exit(1);
});
