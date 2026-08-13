'use strict';

/**
 * Public Server — máy chủ của một Alice: trang web chat + ba mức cửa.
 * Test HTTP THẬT (node:http) với engine giả.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { PublicServer, hashPassword, MAX_BODY } = require('../src/main/public-server');

function tmpBase() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-pub-'));
  return path.join(dir, 'alice');
}

function makeServer(baseDir) {
  fs.mkdirSync(baseDir, { recursive: true });
  const settings = { contextCeiling: 1000, windowRatio: 0.6, compactRatio: 0.8, keepVerbatim: 4, rotateDaily: true };
  const alice = { id: 'a1', name: 'Alice Test' };
  const engine = {
    setBaseDir() {},
    runWithFallback: async (opts) => ({ text: `trả lời: ${opts.message.slice(0, 20)}`, model: 'fake/model', attempts: [] }),
  };
  const server = new PublicServer({
    alice, baseDir, settings, engine, brainMcp: null,
    log: { info: () => {}, error: () => {} },
  });
  server.saveConfig({ enabled: false, mode: 'anyone', port: 0, accounts: [] });
  return server;
}

async function freePort() {
  const net = require('node:net');
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => { const p = srv.address().port; srv.close(() => resolve(p)); });
    srv.on('error', reject);
  });
}

/**
 * Regression của lỗi đã dính thật (2026-08-13): chọn "ai có link cũng vào được"
 * mà quét QR trên điện thoại VẪN thấy màn đăng nhập.
 *
 * Nguyên nhân không nằm ở logic mà ở CSS: `.login { display:flex }` do trang tự
 * đặt THẮNG `display:none` mà trình duyệt gán cho `[hidden]`, nên `login.hidden =
 * true` trong JS không giấu được gì. Trang phải tự vô hiệu hoá cái bẫy đó.
 */
test('trang public: thuộc tính hidden phải thắng mọi class (không thì màn đăng nhập luôn hiện)', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'src', 'main', 'public-web', 'index.html'), 'utf8');
  assert.match(
    html,
    /\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}/,
    'phải có luật [hidden] { display: none !important }'
  );
  // Và luật đó phải đứng TRƯỚC class nào đặt display, không thì thứ tự lại thua.
  const guard = html.search(/\[hidden\]\s*\{\s*display:\s*none\s*!important/);
  const firstFlex = html.search(/\.(gate|feed|compose)\s*\{[^}]*display:\s*flex/);
  assert.ok(guard > -1 && guard < firstFlex, 'luật [hidden] phải nằm trước các class display:flex');
});

test('trang public: mọi ô nhập >= 16px và viewport khoá zoom (iOS không tự phóng lúc focus)', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'src', 'main', 'public-web', 'index.html'), 'utf8');
  assert.match(html, /maximum-scale=1/, 'viewport phải khoá zoom');
  assert.match(html, /user-scalable=no/);
  assert.match(html, /input,\s*textarea,\s*button,\s*select\s*\{\s*font-size:\s*16px/,
    'ô nhập phải 16px — nhỏ hơn là Safari tự phóng trang');
});

test('public server: mode account chưa có tài khoản thì từ chối mở máy chủ', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({ enabled: false, mode: 'account', port: 0, accounts: [] });
  await assert.rejects(() => server.start(0), /Chưa có tài khoản/);
  server.stop();
});

test('public server: mode anyone — không cần token, chat trả lời, có trang web', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);

  try {
    const page = await fetch(`http://127.0.0.1:${port}/`);
    assert.equal(page.status, 200);
    const html = await page.text();
    assert.match(html, /MODE = 'anyone'/, 'trang phải được chèn mode anyone');
    assert.match(html, /Chat với Alice/, 'trang phải là web chat');
    // Đổi mode xong mà điện thoại dựng lại bản cache là thấy đúng cửa vừa gỡ.
    assert.match(page.headers.get('cache-control') || '', /no-store/);

    const ok = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'hôm nay thế nào' }),
    });
    assert.equal(ok.status, 200);
    assert.equal((await ok.json()).text, 'trả lời: hôm nay thế nào');

    const who = await fetch(`http://127.0.0.1:${port}/v1/who`);
    assert.equal(who.status, 200);

    const empty = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
    });
    assert.equal(empty.status, 400);
  } finally {
    server.stop();
  }
});

test('public server: mode code — sai mã thì không vào, đúng mã thì chat được', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({ enabled: false, mode: 'code', port: 0, code: '13572468', accounts: [] });
  const port = await freePort();
  await server.start(port);

  try {
    const html = await (await fetch(`http://127.0.0.1:${port}/`)).text();
    assert.match(html, /MODE = 'code'/);

    const noAuth = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'chào' }),
    });
    assert.equal(noAuth.status, 401);

    const bad = await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: '00000000' }),
    });
    assert.equal(bad.status, 401);

    const good = await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: '13572468' }),
    });
    assert.equal(good.status, 200);
    const { token } = await good.json();

    const ok = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ message: 'xin chào' }),
    });
    assert.equal(ok.status, 200);
  } finally {
    server.stop();
  }
});

test('public server: đổi mã là đá hết phiên cũ ra', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({ enabled: false, mode: 'code', port: 0, code: '11112222', accounts: [] });
  const port = await freePort();
  await server.start(port);

  try {
    const { token } = await (await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: '11112222' }),
    })).json();
    const before = await fetch(`http://127.0.0.1:${port}/v1/check`, { headers: { Authorization: `Bearer ${token}` } });
    assert.equal(before.status, 200);

    const fresh = server.rotateCode();
    assert.notEqual(fresh, '11112222');
    assert.equal(fresh.length, 8);

    const after = await fetch(`http://127.0.0.1:${port}/v1/check`, { headers: { Authorization: `Bearer ${token}` } });
    assert.equal(after.status, 401, 'phiên cũ phải chết sau khi đổi mã');
  } finally {
    server.stop();
  }
});

test('public server: mode account — phải đăng nhập mới chat được', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({
    enabled: false, mode: 'account', port: 0,
    accounts: [{ username: 'nga', ...hashPassword('mat-khau-123') }],
  });
  const port = await freePort();
  await server.start(port);

  try {
    const html = await (await fetch(`http://127.0.0.1:${port}/`)).text();
    assert.match(html, /MODE = 'account'/);

    const noAuth = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'chào' }),
    });
    assert.equal(noAuth.status, 401);

    const bad = await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'nga', password: 'sai-sai' }),
    });
    assert.equal(bad.status, 401);

    const login = await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'nga', password: 'mat-khau-123' }),
    });
    assert.equal(login.status, 200);
    const { token } = await login.json();
    assert.ok(token && token.length > 20, 'session token phải dài');

    const ok = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ message: 'xin chào' }),
    });
    assert.equal(ok.status, 200);
    assert.equal((await ok.json()).text, 'trả lời: xin chào');

    const check = await fetch(`http://127.0.0.1:${port}/v1/check`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    assert.equal(check.status, 200);
  } finally {
    server.stop();
  }
});

test('public server: KHÔNG mở CORS cho website lạ (không thì trang bất kỳ đốt được API key của chủ máy)', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);
  try {
    const foreign = await fetch(`http://127.0.0.1:${port}/v1/who`, { headers: { Origin: 'https://ke-la.example' } });
    assert.equal(foreign.headers.get('access-control-allow-origin'), null, 'gốc lạ không được cấp CORS');

    const same = await fetch(`http://127.0.0.1:${port}/v1/who`, { headers: { Origin: `http://127.0.0.1:${port}` } });
    assert.equal(same.headers.get('access-control-allow-origin'), `http://127.0.0.1:${port}`);
    assert.equal(same.headers.get('x-content-type-options'), 'nosniff');
  } finally {
    server.stop();
  }
});

test('public server: thân request quá to bị chặn (không nuốt hết RAM)', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);
  try {
    const huge = JSON.stringify({ message: 'x'.repeat(MAX_BODY + 5000) });
    const r = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: huge,
    }).catch((e) => ({ status: 413, thrown: e }));
    assert.ok(r.status === 413 || r.thrown, `phải bị từ chối, nhận ${r.status}`);
  } finally {
    server.stop();
  }
});

test('public server: đoán mã sai nhiều lần bị chặn tạm (chống dò mã)', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({ enabled: false, mode: 'code', port: 0, code: '87654321', accounts: [] });
  const port = await freePort();
  await server.start(port);
  try {
    let sawLimit = false;
    for (let i = 0; i < 14; i += 1) {
      const r = await fetch(`http://127.0.0.1:${port}/v1/login`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: '00000000' }),
      });
      if (r.status === 429) { sawLimit = true; break; }
    }
    assert.ok(sawLimit, 'phải chặn sau một số lần đoán sai');

    // Đã bị chặn thì mã ĐÚNG cũng không lọt — nếu không thì hạn mức là vô nghĩa.
    const right = await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: '87654321' }),
    });
    assert.equal(right.status, 429);
  } finally {
    server.stop();
  }
});

test('public server: mật khẩu KHÔNG lưu plaintext', () => {
  const { salt, hash } = hashPassword('mat-khau');
  assert.notEqual(hash, 'mat-khau', 'không được lưu mật khẩu trần');
  assert.ok(salt && salt.length >= 16);
  assert.equal(hash.length, 128, 'scrypt 64 byte → 128 hex');
  const { hash: h2 } = hashPassword('mat-khau');
  assert.notEqual(hash, h2, 'cùng mật khẩu, salt khác → hash khác');
});
