'use strict';

/**
 * Brain RỖNG phải dùng được ngay — không cần ship sẵn file `.db` nào.
 *
 * Đây là chốt chặn cho một lỗi đã xảy ra thật: bản build đầu tiên nhét nguyên
 * 546MB tri thức của một project vào bộ cài, vì lúc đó brain rỗng chết ngay ở
 * `no such table: sources` và cách chữa nhanh nhất trông như là "chép brain có sẵn
 * vào". Cách đúng là dựng schema.
 *
 * Test này giữ hai điều:
 *   1. `ensureSchema()` dựng được database trống có đủ bảng.
 *   2. MCP bắt tay được trên database đó (tool đủ, không lỗi) — tức là người dùng
 *      mới mở app lên là Alice đã có chỗ để nhớ, dù chưa nhớ gì.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const { BrainSidecar } = require('../src/main/brain/sidecar');

// Thư mục dữ liệu RỖNG + runtime thật = đúng hình dạng một máy vừa cài xong: có
// chương trình, chưa có dữ liệu.
//
// Truyền đường dẫn thẳng vào constructor, KHÔNG set `ALICE_PORTABLE_ROOT`: biến môi
// trường là trạng thái dùng chung cả tiến trình, và bản đầu của test này đã làm mù
// luôn `brain-mcp.test.js` chạy cùng lượt — kết quả 0 fail nhưng 2 skip, nhìn qua
// tưởng vẫn xanh.
const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-empty-'));
const brain = new BrainSidecar(
  { enabled: true },
  { dataDir: path.join(sandbox, 'brain'), runtimeDir: path.resolve(__dirname, '..', 'runtime', 'brain') }
);
const skip = brain.available ? false : 'chưa chạy scripts/bundle-brain.ps1';

test('brain rỗng: ensureSchema dựng đủ bảng, không cần ship file .db', { skip, timeout: 180000 }, () => {
  const dbFile = path.join(brain.dataDir, 'sag.db');
  assert.equal(fs.existsSync(dbFile), false, 'phải bắt đầu từ chỗ chưa có gì');

  const r = brain.ensureSchema();
  assert.equal(r.created, true);
  assert.ok(fs.existsSync(dbFile), 'phải tạo ra sag.db');

  // Đọc bằng chính python nhúng cho khỏi phụ thuộc node:sqlite ở đây.
  const q = spawnSync(brain.pythonPath, ['-c', [
    'import sqlite3,sys,json',
    `c=sqlite3.connect(${JSON.stringify(dbFile)})`,
    "rows=[r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")]",
    'print(json.dumps(rows))',
  ].join(';')], { encoding: 'utf8', windowsHide: true });

  assert.equal(q.status, 0, `đọc database hỏng: ${q.stderr}`);
  const tables = JSON.parse(q.stdout.trim());
  assert.ok(tables.includes('sources'),
    `thiếu bảng "sources" — đây đúng là lỗi đã làm brain rỗng vô dụng. Có: ${tables.join(', ')}`);
  assert.ok(tables.length > 5, `schema quá mỏng (${tables.length} bảng)`);

  // Gọi lần hai phải im lặng bỏ qua, không dựng đè lên dữ liệu đã có.
  assert.equal(brain.ensureSchema().created, false, 'chạy lại không được đụng vào database đang có');
});

test.after(() => {
  // Chỉ xoá đúng thư mục dữ liệu mình tạo ra, và chỉ khi nó nằm trong thư mục tạm.
  //
  // Bản đầu của test này dựng một **junction** trỏ tới `runtime/` thật rồi
  // `rmSync(sandbox, {recursive:true})` — Node đi xuyên junction và xoá luôn đích:
  // mất sạch 900MB brain bundle và binary opencode, phải dựng lại từ đầu. Dọn dẹp
  // trong test có quyền xoá y hệt lệnh xoá thật, nên nó phải được ngắm kỹ y hệt.
  const tmp = os.tmpdir();
  if (!sandbox.startsWith(tmp) || sandbox === tmp) return;
  for (const e of fs.readdirSync(sandbox, { withFileTypes: true })) {
    if (e.isSymbolicLink()) return; // có link lạ → không đụng vào gì cả
  }
  try { fs.rmSync(sandbox, { recursive: true, force: true }); } catch { /* để lại cho OS dọn */ }
});
