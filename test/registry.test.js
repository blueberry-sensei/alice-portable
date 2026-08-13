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

// ── thư mục do người dùng chọn ────────────────────────────────────────────────

test('create: chọn thư mục riêng thì Alice sống ở đó, trong thư mục con mang tên nó', () => {
  const paths = tmpPaths();
  const home = path.join(paths._root, 'Work', 'du-an');
  fs.mkdirSync(home, { recursive: true });

  const { alice } = registry.create({ name: 'Alice K-OS', key: 'sk-1', dir: home }, paths);

  // Không đổ thẳng vào thư mục người dùng chỉ — rải file vào một thư mục đang có
  // việc khác là cách nhanh nhất để họ mất niềm tin.
  assert.equal(alice.dir, path.join(home, 'Alice-K-OS'));
  assert.ok(fs.existsSync(alice.dir));
  assert.ok(fs.existsSync(path.join(alice.dir, 'brain')));
  assert.equal(registry.dirOf(alice, paths), alice.dir);

  const authFile = path.join(alice.dir, 'opencode', 'data', 'opencode', 'auth.json');
  assert.equal(JSON.parse(fs.readFileSync(authFile, 'utf8')).opencode.key, 'sk-1');

  // Thư mục mặc định KHÔNG được tạo song song.
  assert.equal(fs.existsSync(registry.aliceDirOf(registry.resolve(paths), alice.id)), false);
});

test('slug: tên tiếng Việt có dấu thành tên thư mục Windows nhận được', () => {
  assert.equal(registry.slug('Alice Phượng'), 'Alice-Phuong');
  assert.equal(registry.slug('Alice / GoDine?'), 'Alice-GoDine');
  assert.equal(registry.slug('Đặng'), 'Dang');
  assert.equal(registry.slug('***'), '');
});

test('remove: KHÔNG xoá thư mục do người dùng chọn — chỉ gỡ khỏi danh sách', () => {
  const paths = tmpPaths();
  const home = path.join(paths._root, 'Work');
  fs.mkdirSync(home, { recursive: true });
  const { alice } = registry.create({ name: 'Alice K-OS', key: 'sk-1', dir: home }, paths);
  // Một file của NGƯỜI DÙNG nằm cạnh đó — xoá nhầm ở đây là mất dữ liệu thật.
  fs.writeFileSync(path.join(home, 'bao-cao.txt'), 'quan trọng');

  const { state, removed, keptDir } = registry.remove(alice.id, paths);
  assert.equal(removed, true);
  assert.equal(state.alices.length, 0);
  assert.equal(keptDir, alice.dir, 'phải báo lại thư mục còn giữ để UI nói cho người dùng');
  assert.ok(fs.existsSync(alice.dir), 'thư mục người dùng chọn không được tự xoá');
  assert.ok(fs.existsSync(path.join(home, 'bao-cao.txt')));
});

test('remove: thư mục do CHÍNH APP tạo thì xoá hẳn, không báo giữ lại', () => {
  const paths = tmpPaths();
  const { alice } = registry.create({ name: 'Alice', key: 'sk-1' }, paths);
  const dir = registry.aliceDirOf(registry.resolve(paths), alice.id);
  assert.ok(fs.existsSync(dir));

  const { removed, keptDir } = registry.remove(alice.id, paths);
  assert.equal(removed, true);
  assert.equal(keptDir, null);
  assert.equal(fs.existsSync(dir), false);
});
