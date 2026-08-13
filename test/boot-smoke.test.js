'use strict';

/**
 * Smoke test: MỞ THẬT app rồi xem nó boot xong không.
 *
 * Vì sao cần: `main.js` không có một dòng test nào, và `node --check` chỉ bắt lỗi cú
 * pháp. Bản v0.3.1 phát cho khách mang một `ReferenceError: brainMcp is not defined`
 * ngay trong `activateAlice` — một dòng khai báo bị xoá nhầm lúc sửa chỗ khác. Cú
 * pháp hợp lệ, mọi test khác xanh, nhưng app hỏng hoàn toàn với BẤT KỲ ai đã có một
 * Alice: boot chết, mọi IPC hỏng theo, ô chọn model đứng ở "(đang tải…)" và nút Tạo
 * treo ở "Đang tạo…".
 *
 * Test này đi đúng đường đó: dựng một thư mục cài sạch có sẵn MỘT Alice, mở app
 * bằng chính Electron, rồi đọc nhật ký.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const ELECTRON = path.join(ROOT, 'node_modules', 'electron', 'dist',
  process.platform === 'win32' ? 'electron.exe' : 'electron');

// Cần binary Electron thật và một màn hình. CI chạy test bằng electron-as-node nên
// bỏ qua ở đó thay vì báo đỏ giả.
const skip = !fs.existsSync(ELECTRON) || process.env.ALICE_SKIP_SMOKE
  ? 'cần Electron GUI (đặt ALICE_SKIP_SMOKE=1 để bỏ qua)'
  : false;

test('boot: app mở được với một Alice đã có, không lỗi fatal', { skip, timeout: 180000 }, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-smoke-'));
  const dataDir = path.join(root, 'alice-data');
  const id = crypto.randomUUID();
  const aliceDir = path.join(dataDir, 'alices', id);
  fs.mkdirSync(path.join(aliceDir, 'brain'), { recursive: true });
  fs.writeFileSync(
    path.join(dataDir, 'alices.json'),
    JSON.stringify({ active: id, alices: [{ id, name: 'Alice Smoke', provider: 'opencode', model: null }] }),
    'utf8'
  );

  const shot = path.join(root, 'shot.png');
  const env = {
    ...process.env,
    ALICE_PORTABLE_ROOT: root,   // app đọc/ghi trong thư mục tạm, không đụng máy thật
    ALICE_CAPTURE: shot,         // chụp xong tự thoát — test không treo
    ALICE_CAPTURE_DELAY: '9000',
  };
  // Bộ chạy test bật `ELECTRON_RUN_AS_NODE=1` (vì `node:sqlite` cần Node 24). Con
  // thừa hưởng biến đó là Electron khởi động ở chế độ node thuần: `require('electron')`
  // trả về một CHUỖI đường dẫn, `app` thành undefined và app chết ngay dòng đầu.
  delete env.ELECTRON_RUN_AS_NODE;

  const res = spawnSync(ELECTRON, [ROOT], {
    cwd: ROOT, encoding: 'utf8', timeout: 150000, windowsHide: true, env,
  });

  const logFile = path.join(dataDir, 'logs', 'app.log');
  const log = fs.existsSync(logFile) ? fs.readFileSync(logFile, 'utf8') : '';

  assert.ok(log.includes('boot: root='), `app không boot. exit=${res.status}\n${res.stderr || ''}`);
  const fatal = log.split('\n').filter((l) => /ERROR fatal:/.test(l));
  assert.deepEqual(fatal, [], `boot ném lỗi:\n${fatal.join('\n')}`);
  assert.match(log, /alice active: /, 'phải mở được Alice đã có trong danh sách');
  assert.match(log, /boot done — ready/, 'boot phải chạy tới cùng');
});
