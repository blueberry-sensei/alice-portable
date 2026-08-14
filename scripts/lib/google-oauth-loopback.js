'use strict';

/**
 * Phần dùng chung cho các script OAuth-một-lần của Google (Desktop app, loopback
 * redirect) — tách ra vì `add-chat-app-to-space.js` và `chat-user-login.js` cần
 * hệt cùng một luồng: mở trình duyệt, chờ code ở localhost, đổi code lấy token.
 */

const http = require('node:http');
const { URL } = require('node:url');
const { execFile } = require('node:child_process');

const REDIRECT_PORT = 53682;
const REDIRECT_URI = `http://127.0.0.1:${REDIRECT_PORT}/`;

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

async function exchangeCode({ clientId, clientSecret, code }) {
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
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`Đổi code lấy token thất bại: HTTP ${res.status} ${JSON.stringify(data)}`);
  return data; // { access_token, refresh_token, expires_in, ... }
}

/** Chạy trọn luồng: mở trình duyệt → chờ code → đổi lấy token. */
async function runLoopbackFlow({ clientId, clientSecret, scope }) {
  const authUrl = new URL('https://accounts.google.com/o/oauth2/v2/auth');
  authUrl.searchParams.set('client_id', clientId);
  authUrl.searchParams.set('redirect_uri', REDIRECT_URI);
  authUrl.searchParams.set('response_type', 'code');
  authUrl.searchParams.set('scope', scope);
  authUrl.searchParams.set('access_type', 'offline');
  authUrl.searchParams.set('prompt', 'consent');

  console.log('Đang mở trình duyệt để đăng nhập Google...');
  console.log(`Nếu không tự mở, dán link này vào trình duyệt:\n${authUrl.toString()}\n`);
  openBrowser(authUrl.toString());

  const code = await waitForCode();
  console.log('Đã nhận code — đang đổi lấy token...');
  return exchangeCode({ clientId, clientSecret, code });
}

module.exports = { REDIRECT_URI, parseArgs, openBrowser, waitForCode, exchangeCode, runLoopbackFlow };
