'use strict';

/**
 * Registry đa-Alice: tạo/xoá Alice phải sạch cả thư mục dữ liệu, và dữ liệu CŨ
 * (bản một-Alice) phải migrate nguyên vẹn sang Alice đầu tiên — lỗi ở đây là
 * người dùng nâng cấp mất hết lịch sử chat.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const registry = require('../src/main/registry');

function tmpPaths() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-reg-'));
  return {
    dataDir: path.join(dir, 'data'),
    alicesDir: path.join(dir, 'data', 'alices'),
    registryPath: path.join(dir, 'data', 'alices.json'),
    _root: dir,
  };
}

test('create: Alice mới có tên + key riêng, thư mục đủ', () => {
  const paths = tmpPaths();
  const { state, alice } = registry.create({ name: 'Alice GoDine', key: 'sk-test-123' }, paths);

  assert.equal(state.alices.length, 1);
  assert.equal(state.active, alice.id);
  assert.equal(alice.name, 'Alice GoDine');

  const dir = registry.aliceDirOf(registry.resolve(paths), alice.id);
  assert.ok(fs.existsSync(dir), 'phải có thư mục dữ liệu của Alice');
  // Key phải nằm trong auth.json của RIÊNG Alice đó.
  const authFile = path.join(dir, 'opencode', 'data', 'opencode', 'auth.json');
  const auth = JSON.parse(fs.readFileSync(authFile, 'utf8'));
  assert.equal(auth.opencode.key, 'sk-test-123');

  // Alice thứ hai không đụng Alice thứ nhất.
  const { state: s2 } = registry.create({ name: 'Alice PHUONG', key: 'sk-2' }, paths);
  assert.equal(s2.alices.length, 2);
  assert.equal(s2.active, alice.id, 'active giữ Alice đầu tiên');
  fs.rmSync(paths._root, { recursive: true, force: true });
});

test('remove: xoá khỏi danh sách + xoá cả thư mục dữ liệu', () => {
  const paths = tmpPaths();
  const { alice: a1 } = registry.create({ name: 'A', key: 'k1' }, paths);
  const { alice: a2 } = registry.create({ name: 'B', key: 'k2' }, paths);

  const dir1 = registry.aliceDirOf(registry.resolve(paths), a1.id);
  assert.ok(fs.existsSync(dir1));

  const { state, removed } = registry.remove(a1.id, paths);
  assert.equal(removed, true);
  assert.equal(state.alices.length, 1);
  assert.equal(fs.existsSync(dir1), false, 'thư mục của Alice bị xoá phải biến mất');
  assert.equal(state.active, a2.id, 'xoá Alice đang mở → chuyển sang Alice còn lại');
  fs.rmSync(paths._root, { recursive: true, force: true });
});

test('migrateLegacy: dữ liệu một-Alice cũ gom thành Alice đầu tiên', () => {
  const paths = tmpPaths();
  fs.mkdirSync(paths.dataDir, { recursive: true });

  // Giả lập bản cũ: chat db + brain + opencode auth nằm thẳng trong dataDir.
  fs.writeFileSync(path.join(paths.dataDir, 'alice.db'), 'CHAT-DB-CU');
  fs.mkdirSync(path.join(paths.dataDir, 'brain'), { recursive: true });
  fs.writeFileSync(path.join(paths.dataDir, 'brain', 'sag.db'), 'BRAIN-CU');
  fs.mkdirSync(path.join(paths.dataDir, 'opencode', 'data', 'opencode'), { recursive: true });
  fs.writeFileSync(
    path.join(paths.dataDir, 'opencode', 'data', 'opencode', 'auth.json'),
    JSON.stringify({ opencode: { type: 'api', key: 'sk-cu' } })
  );

  const state = registry.migrateLegacy({ name: 'Alice Cu' }, paths);
  assert.ok(state, 'có dữ liệu cũ thì phải migrate');
  assert.equal(state.alices.length, 1);
  assert.equal(state.alices[0].name, 'Alice Cu');
  assert.equal(state.alices[0].migrated, true);

  const dir = registry.aliceDirOf(registry.resolve(paths), state.active);
  assert.equal(fs.readFileSync(path.join(dir, 'chat.db'), 'utf8'), 'CHAT-DB-CU', 'chat db cũ phải đi theo');
  assert.equal(fs.readFileSync(path.join(dir, 'brain', 'sag.db'), 'utf8'), 'BRAIN-CU', 'brain cũ phải đi theo');
  const auth = JSON.parse(fs.readFileSync(path.join(dir, 'opencode', 'data', 'opencode', 'auth.json'), 'utf8'));
  assert.equal(auth.opencode.key, 'sk-cu', 'chìa khoá cũ phải đi theo');
  fs.rmSync(paths._root, { recursive: true, force: true });
});

test('migrateLegacy: máy mới tinh (không có alice.db) thì không tạo gì', () => {
  const paths = tmpPaths();
  fs.mkdirSync(paths.dataDir, { recursive: true });
  const state = registry.migrateLegacy({ name: 'X' }, paths);
  assert.equal(state, null);
  assert.equal(fs.existsSync(paths.registryPath), false, 'không được ghi registry khi chưa có gì');
  fs.rmSync(paths._root, { recursive: true, force: true });
});
