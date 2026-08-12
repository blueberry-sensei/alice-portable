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

const { PublicServer, newToken } = require('../src/main/public-server');

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
  server.saveConfig({ enabled: false, port: 0, tokens });
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

test('public server: chưa có token thì từ chối mở máy chủ', async () => {
  const server = makeServer(tmpBase(), { tokens: [] });
  await assert.rejects(() => server.start(0), /Chưa có token/);
  server.stop();
});

test('public server: không token → 401; token đúng → chat trả lời', async () => {
  const base = tmpBase();
  const tok = newToken();
  const server = makeServer(base, { tokens: [{ label: 'Nga', token: tok, created_at: 1 }] });
  const port = await freePort();
  await server.start(port);

  try {
    // Không có token.
    const noAuth = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'chào' }),
    });
    assert.equal(noAuth.status, 401);

    // Token sai.
    const badAuth = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer sai' },
      body: JSON.stringify({ message: 'chào' }),
    });
    assert.equal(badAuth.status, 401);

    // Token đúng.
    const ok = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tok}` },
      body: JSON.stringify({ message: 'hôm nay thế nào' }),
    });
    assert.equal(ok.status, 200);
    const data = await ok.json();
    assert.equal(data.text, 'trả lời: hôm nay thế nào');
    assert.equal(data.model, 'fake/model');
    assert.equal(ok.headers.get('access-control-allow-origin'), '*', 'CORS mở cho client khác');

    // Health không cần token.
    const health = await fetch(`http://127.0.0.1:${port}/`);
    assert.equal(health.status, 200);

    // Body thiếu message → 400.
    const empty = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tok}` },
      body: JSON.stringify({}),
    });
    assert.equal(empty.status, 400);
  } finally {
    server.stop();
  }
});

test('public server: thu hồi token là hết quyền ngay', async () => {
  const base = tmpBase();
  const tok = newToken();
  const server = makeServer(base, { tokens: [{ label: 'A', token: tok, created_at: 1 }] });
  const port = await freePort();
  await server.start(port);
  try {
    const cfg = server.config();
    server.saveConfig({ ...cfg, tokens: [] }); // thu hồi
    const r = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tok}` },
      body: JSON.stringify({ message: 'x' }),
    });
    assert.equal(r.status, 401, 'token đã thu hồi phải bị từ chối');
  } finally {
    server.stop();
  }
});
