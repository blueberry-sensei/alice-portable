'use strict';

/**
 * Model mồ côi — bug đã tới tay khách (2026-08-14).
 *
 * Triệu chứng: Alice cấu hình `provider=claude, model=claude-sonnet-5`, thanh tiêu
 * đề hiện đúng `claude-sonnet-5`, nhưng mỗi lượt chat trả về một bong bóng đỏ
 * tiếng Anh — CLI `claude` bị gọi với `--model opencode/deepseek-v4-flash`.
 *
 * Nguyên nhân: `settings.json` của máy đã chạy bản một-Alice còn field `model`
 * toàn cục. Bản đa-Alice không dùng field đó nữa nhưng cũng không xoá, và bốn chỗ
 * trong code vẫn đọc nó.
 *
 * Hai lớp phòng thủ, test riêng từng lớp:
 *   1. `loadSettings` dọn field mồ côi khỏi ĐĨA — không còn nguồn để rò.
 *   2. `modelFor` chặn mọi ghép sai provider/model — kể cả khi nguồn khác lọt vào.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { modelFor } = require('../src/main/engine/model');
const { buildOpencodeJson } = require('../src/main/alice');

test('modelFor: Alice claude nhận model claude thì giữ nguyên', () => {
  const r = modelFor({ provider: 'claude', model: 'claude-sonnet-5' });
  assert.equal(r.model, 'claude-sonnet-5');
  assert.equal(r.warning, null);
});

test('modelFor: Alice claude nhận model opencode thì hạ về mặc định + nói lý do', () => {
  const r = modelFor({ provider: 'claude', model: 'opencode/deepseek-v4-flash' });
  assert.equal(r.model, null, 'không được đẩy model opencode xuống CLI claude');
  assert.match(r.warning, /Claude Code/);
  assert.match(r.warning, /opencode\/deepseek-v4-flash/, 'phải nói rõ model sai là cái nào');
});

test('modelFor: Alice opencode nhận model claude thì hạ về mặc định', () => {
  const r = modelFor({ provider: 'opencode', model: 'claude-sonnet-5' });
  assert.equal(r.model, null);
  assert.match(r.warning, /opencode/);
});

test('modelFor: Alice opencode nhận model opencode thì giữ nguyên', () => {
  const r = modelFor({ provider: 'opencode', model: 'opencode/nemotron-3-ultra-free' });
  assert.equal(r.model, 'opencode/nemotron-3-ultra-free');
  assert.equal(r.warning, null);
});

test('modelFor: chưa chọn model thì không phải lỗi — để engine tự chọn', () => {
  assert.deepEqual(modelFor({ provider: 'claude', model: null }), { model: null, warning: null });
  assert.deepEqual(modelFor({ provider: 'opencode' }), { model: null, warning: null });
  assert.deepEqual(modelFor(null), { model: null, warning: null });
});

test('buildOpencodeJson: model phải được TRUYỀN VÀO, không đọc lén settings', () => {
  // Đây là chỗ rò thứ tư, sống sót qua đúng một lần vá.
  const withOrphan = buildOpencodeJson({ model: 'opencode/deepseek-v4-flash' }, {});
  assert.equal(withOrphan.model, undefined, 'settings.model không được lọt vào opencode.json');

  const explicit = buildOpencodeJson({}, { model: 'opencode/mimo-v2.5-free' });
  assert.equal(explicit.model, 'opencode/mimo-v2.5-free');

  const none = buildOpencodeJson({}, {});
  assert.equal(none.model, undefined, 'không có model thì không ghi key model');
});

test('loadSettings: dọn field model mồ côi khỏi settings.json trên ĐĨA', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-cfg-'));
  const prev = process.env.ALICE_PORTABLE_ROOT;
  process.env.ALICE_PORTABLE_ROOT = root;
  // `config.js` chốt ROOT lúc require — nạp lại module sau khi đã set biến.
  delete require.cache[require.resolve('../src/main/config')];
  const config = require('../src/main/config');
  try {
    fs.mkdirSync(config.DATA_DIR, { recursive: true });
    fs.writeFileSync(config.SETTINGS_PATH, JSON.stringify({
      model: 'opencode/deepseek-v4-flash',
      contextCeiling: 200000,
    }, null, 2), 'utf8');

    const s = config.loadSettings();
    assert.equal(s.model, undefined, 'không được trả field mồ côi ra cho phần còn lại của app');
    assert.equal(s.contextCeiling, 200000, 'các cài đặt thật phải giữ nguyên');

    const onDisk = JSON.parse(fs.readFileSync(config.SETTINGS_PATH, 'utf8'));
    assert.equal('model' in onDisk, false, 'phải xoá khỏi ĐĨA, không chỉ khỏi bộ nhớ');
    assert.equal(onDisk.contextCeiling, 200000);

    // Và một patch từ UI cũ cũng không đưa nó quay lại được.
    config.saveSettings({ ...s, model: 'opencode/lạc-lối' });
    assert.equal('model' in JSON.parse(fs.readFileSync(config.SETTINGS_PATH, 'utf8')), false);
  } finally {
    if (prev === undefined) delete process.env.ALICE_PORTABLE_ROOT;
    else process.env.ALICE_PORTABLE_ROOT = prev;
    delete require.cache[require.resolve('../src/main/config')];
    fs.rmSync(root, { recursive: true, force: true });
  }
});
