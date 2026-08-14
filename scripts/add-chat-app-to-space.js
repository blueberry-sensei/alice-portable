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
 * Chạy MỘT LẦN, bằng chính Electron-as-node (không cần cài Node riêng, giống
 * scripts/run-tests.ps1 — máy này có Node 20 trên PATH, thiếu `fetch` ổn định và
 * `node:sqlite` không dùng ở đây nhưng cứ đồng bộ một chuẩn cho đỡ rối):
 *
 *   $env:ELECTRON_RUN_AS_NODE = '1'
 *   & "node_modules\electron\dist\electron.exe" scripts\add-chat-app-to-space.js `
 *     --client-id=XXXX.apps.googleusercontent.com --client-secret=YYYY --space=AAQAecMzBo4
 *
 * Lấy client-id/client-secret: Cloud Console → Credentials → Create Credentials →
 * OAuth client ID → Application type "Desktop app". Trước đó phải cấu hình xong
 * "OAuth consent screen" (User type = Internal, vì project thuộc Workspace).
 *
 * `--space` là ID trong URL Chat (`chat.google.com/room/<ID>`) — chấp nhận cả dạng
 * trần lẫn `spaces/<ID>`.
 */

const http = require('node:http');
const { URL } = require('node:url');
const { execFile } = require('node:child_process');

const REDIRECT_PORT = 53682;
const REDIRECT_URI = `http://127.0.0.1:${REDIRECT_PORT}/`;
const SCOPE = 'https://www.googleapis.com/auth/chat.memberships.app';

function parseArgs() {
  const out = {};
  for (const a of process.argv.slice(2)) {
    const m = a.match(/^--([a-z-]+)=(.*)$/);
    if (m) out[m[1]] = m[2];
  }
  return out;
}

/**
 * `cmd /c start "" <url>` VỠ URL có `&` (query string OAuth luôn có nhiều `&`):
 * cmd.exe tự phân tích dòng lệnh và cắt tại `&` đầu tiên coi như lệnh kế tiếp —
 * đo thật: browser chỉ nhận được `...?client_id=XXXX`, mất sạch phần sau
 * (`response_type`, `scope`...) → Google báo "Required parameter is missing:
 * response_type". `rundll32 url.dll,FileProtocolHandler` nhận URL làm một tham
 * số duy nhất, không qua cmd.exe nên không bị cắt.
 */
function openBrowser(url) {
  if (process.platform === 'win32') {
    execFile('rundll32', ['url.dll,FileProtocolHandler', url], () => {});
    return;
  }
  const cmd = process.platform === 'darwin' ? 'open' : 'xdg-open';
  execFile(cmd, [url], () => {});
}

function waitForCode() {
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      const u = new URL(req.url, REDIRECT_URI);
      const code = u.searchParams.get('code');
      const err = u.searchParams.get('error');
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(err
        ? `<h3>Lỗi: ${err}</h3><p>Đóng tab này, xem lỗi trong terminal.</p>`
        : '<h3>Xong — đóng tab này rồi quay lại terminal.</h3>');
      server.close();
      if (err) reject(new Error(`Google từ chối: ${err}`));
      else if (code) resolve(code);
      else reject(new Error('Không nhận được code lẫn error từ redirect.'));
    });
    // Không bắt lỗi ở đây là crash thô "Unhandled 'error' event" — đo thật: lần
    // chạy trước bị ngắt giữa chừng (đóng terminal trước khi bấm xong OAuth) để
    // lại server cũ còn giữ cổng, lần chạy sau EADDRINUSE ngay từ đầu.
    server.on('error', (err) => {
      if (err.code === 'EADDRINUSE') {
        reject(new Error(
          `Cổng ${REDIRECT_PORT} đang bị chiếm — có lẽ lần chạy script trước chưa `
          + `thoát hẳn. Đóng cửa sổ terminal đó (hoặc tắt tiến trình đang giữ cổng `
          + `này) rồi chạy lại.`
        ));
      } else {
        reject(err);
      }
    });
    server.listen(REDIRECT_PORT, '127.0.0.1');
  });
}

async function exchangeCode(clientId, clientSecret, code) {
  const res = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      client_id: clientId,
      client_secret: clientSecret,
      code,
      grant_type: 'authorization_code',
      redirect_uri: REDIRECT_URI,
    }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(`Đổi code lấy token thất bại: HTTP ${res.status} ${JSON.stringify(data)}`);
  return data.access_token;
}

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

  const authUrl = new URL('https://accounts.google.com/o/oauth2/v2/auth');
  authUrl.searchParams.set('client_id', clientId);
  authUrl.searchParams.set('redirect_uri', REDIRECT_URI);
  authUrl.searchParams.set('response_type', 'code');
  authUrl.searchParams.set('scope', SCOPE);
  authUrl.searchParams.set('access_type', 'offline');
  authUrl.searchParams.set('prompt', 'consent');

  console.log('Đang mở trình duyệt để đăng nhập Google (đăng nhập bằng tài khoản có mặt trong Space đó)...');
  console.log(`Nếu không tự mở, dán link này vào trình duyệt:\n${authUrl.toString()}\n`);
  openBrowser(authUrl.toString());

  const code = await waitForCode();
  console.log('Đã nhận code — đang đổi lấy access token...');
  const accessToken = await exchangeCode(clientId, clientSecret, code);

  console.log(`Đang thêm app vào space ${space}...`);
  const result = await addAppToSpace(accessToken, space);
  console.log('THÀNH CÔNG:', JSON.stringify(result, null, 2));
}

main().catch((err) => {
  console.error('THẤT BẠI:', err.message);
  process.exit(1);
});
