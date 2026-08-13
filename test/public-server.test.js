'use strict';

/**
 * Public Server — máy chủ của một Alice: chỉ ai có token mới gọi được, chat trả
 * lời bằng đúng trí nhớ của Alice đó. Test HTTP THẬT (node:http) với engine giả.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { PublicServer } = require('../src/main/public-server');

function tmpBase() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-pub-'));
  return path.join(dir, 'alice');
}

function makeServer(baseDir, { tokens = [] } = {}) {
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

test('public server: mode account chưa có tài khoản thì từ chối mở máy chủ', async () => {
  const base = tmpBase();
  const server = makeServer(base, {});
  server.saveConfig({ enabled: false, mode: 'account', port: 0, accounts: [] });
  await assert.rejects(() => server.start(0), /Chưa có tài khoản/);
  server.stop();
});

test('public server: mode anyone — không cần token, chat trả lời, có trang web', async () => {
  const base = tmpBase();
  const server = makeServer(base, {});
  const port = await freePort();
  await server.start(port);

  try {
    // Trang web chat: ai cũng mở được, chèn đúng mode.
    const page = await fetch(`http://127.0.0.1:${port}/`);
    assert.equal(page.status, 200);
    const html = await page.text();
    assert.match(html, /MODE = 'anyone'/, 'trang phải được chèn mode anyone');
    assert.match(html, /Chat với Alice/, 'trang phải là web chat');

    // Không token vẫn chat được (anyone).
    const ok = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'hôm nay thế nào' }),
    });
    assert.equal(ok.status, 200);
    const data = await ok.json();
    assert.equal(data.text, 'trả lời: hôm nay thế nào');
    assert.equal(ok.headers.get('access-control-allow-origin'), '*', 'CORS mở cho client khác');

    // Health.
    const who = await fetch(`http://127.0.0.1:${port}/v1/who`);
    assert.equal(who.status, 200);

    // Body thiếu message → 400.
    const empty = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
    });
    assert.equal(empty.status, 400);
  } finally {
    server.stop();
  }
});

test('public server: mode account — phải đăng nhập mới chat được', async () => {
  const base = tmpBase();
  const server = makeServer(base, {});
  const { hashPassword } = require('../src/main/public-server');
  server.saveConfig({
    enabled: false, mode: 'account', port: 0, tokens: [],
    accounts: [{ username: 'nga', ...hashPassword('mat-khau-123') }],
  });
  const port = await freePort();
  await server.start(port);

  try {
    // Trang web chèn mode account.
    const html = await (await fetch(`http://127.0.0.1:${port}/`)).text();
    assert.match(html, /MODE = 'account'/);

    // Chưa đăng nhập → 401.
    const noAuth = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'chào' }),
    });
    assert.equal(noAuth.status, 401);

    // Sai mật khẩu → 401.
    const bad = await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'nga', password: 'sai-sai' }),
    });
    assert.equal(bad.status, 401);

    // Đúng → session token → chat được.
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
    const data = await ok.json();
    assert.equal(data.text, 'trả lời: xin chào');

    // /v1/check với session hợp lệ.
    const check = await fetch(`http://127.0.0.1:${port}/v1/check`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    assert.equal(check.status, 200);
  } finally {
    server.stop();
  }
});

test('public server: mật khẩu KHÔNG lưu plaintext', () => {
  const { hashPassword } = require('../src/main/public-server');
  const { salt, hash } = hashPassword('mat-khau');
  assert.notEqual(hash, 'mat-khau', 'không được lưu mật khẩu trần');
  assert.ok(salt && salt.length >= 16);
  assert.equal(hash.length, 128, 'sha512-hex');
  const { hash: h2 } = hashPassword('mat-khau');
  assert.notEqual(hash, h2, 'cùng mật khẩu, salt khác → hash khác');
});
