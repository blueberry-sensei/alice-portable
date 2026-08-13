'use strict';

/**
 * Tunnel — chia sẻ máy chủ của Alice ra Internet qua cloudflared.
 *
 * Test KHÔNG đụng mạng: phần tải và phần bắt tay với Cloudflare không kiểm được ở
 * đây. Cái kiểm được — và cũng là chỗ đã hỏng nếu sai — là: chọn đúng file cho hệ
 * điều hành đang chạy, và chưa có binary thì nói thẳng chứ không im lặng.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { Tunnel, assetName } = require('../src/main/tunnel');

test('tunnel: chọn đúng file cloudflared cho hệ điều hành đang chạy', () => {
  const name = assetName();
  if (process.platform === 'win32') assert.match(name, /^cloudflared-windows-.+\.exe$/);
  else if (process.platform === 'darwin') assert.match(name, /^cloudflared-darwin-.+\.tgz$/);
  else assert.match(name, /^cloudflared-linux-/);
});

test('tunnel: chưa có binary thì start() nói thẳng, không treo im lặng', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-tun-'));
  const t = new Tunnel({ resourcesDir: dir, toolsDir: path.join(dir, 'tools') });
  // Máy CI có thể đã cài sẵn cloudflared trên PATH — khi đó phép thử này vô nghĩa.
  if (t.resolveBinary()) return;

  assert.equal(t.running, false);
  await assert.rejects(() => t.start(8931), /cloudflared/i);
  assert.equal(t.status().binary, null);
});

test('tunnel: tìm binary theo thứ tự bản nhúng → bản đã tải', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-tun-'));
  const binName = process.platform === 'win32' ? 'cloudflared.exe' : 'cloudflared';
  const bundled = path.join(dir, 'runtime', 'cloudflared');
  fs.mkdirSync(bundled, { recursive: true });
  fs.writeFileSync(path.join(bundled, binName), 'giả');

  const t = new Tunnel({ resourcesDir: path.join(dir, 'runtime'), toolsDir: path.join(dir, 'tools') });
  assert.equal(t.resolveBinary(), path.join(bundled, binName), 'bản nhúng phải được ưu tiên');
});

test('tunnel: stop() khi chưa chạy không ném lỗi', () => {
  const t = new Tunnel({ toolsDir: path.join(os.tmpdir(), 'alice-tun-none') });
  t.stop();
  assert.equal(t.running, false);
  assert.equal(t.status().url, null);
});
