'use strict';

/**
 * Nghiệm thu bằng ENGINE THẬT — gọi opencode thật, qua mạng thật.
 *
 * `test/memory.test.js` dùng engine giả nên chứng minh được *chính sách* đúng, chứ
 * không chứng minh được *engine* làm đúng. Hai thứ khác nhau, và cái hỏng ở bản
 * alice-social nằm ở chỗ nối giữa chúng: chính sách gửi `--session ses_…` đúng, mà
 * id truyền vào lại là uuid của hệ khác, nên mỗi lượt lặng lẽ thành một hội thoại
 * mới. Nhìn UI không thấy gì bất thường — bot vẫn trả lời trôi chảy.
 *
 * Test này chậm (gọi model free) và cần mạng. Bỏ qua bằng `ALICE_SKIP_E2E=1`.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { Store } = require('../src/main/memory/store');
const { Memory } = require('../src/main/memory/memory');
const { createTurnRunner } = require('../src/main/turn');
const { OpencodeEngine, SESSION_RE } = require('../src/main/engine/opencode');

const skip = process.env.ALICE_SKIP_E2E === '1';

test('opencode thật: lượt 2 nhớ được điều nói ở lượt 1', { skip, timeout: 300000 }, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-e2e-'));
  const settings = {
    contextCeiling: 128000, windowRatio: 0.6, compactRatio: 0.8,
    keepVerbatim: 40, rotateDaily: true,
    // Ngắn hơn mặc định: test không nên ngồi chờ một model treo. Đúng ca đã dính —
    // lần chạy trước một model free im lặng 13 phút và kéo cả bộ test đứng hình.
    idleTimeoutMs: 60000,
    modelPreference: ['opencode/deepseek-v4-flash-free', 'opencode/nemotron-3-ultra-free'],
    model: null,
  };

  const engine = new OpencodeEngine(settings);
  if (!engine.available) {
    assert.fail(`Không tìm thấy opencode — engine.binSource=${engine.binSource}`);
  }

  const store = new Store(path.join(dir, 'alice.db'));
  const memory = new Memory(store, settings);
  const run = createTurnRunner({ store, memory, engine, workDir: dir, settings });

  const t1 = await run('Ghi nho con so 8642. Tra loi dung mot tu: ok');
  assert.match(t1.engineSession, SESSION_RE, `session phải dạng ses_…, nhận được: ${t1.engineSession}`);

  const t2 = await run('Con so toi vua bao ban ghi nho la so nao? Tra loi dung con so do.');
  assert.equal(t2.engineSession, t1.engineSession, 'lượt 2 phải nằm trong CÙNG session');
  assert.match(t2.text, /8642/, `lượt 2 phải nhớ được 8642, nhận được: ${JSON.stringify(t2.text)}`);

  // tokens.input là số ĐO ĐƯỢC — tầng trí nhớ quyết định xoay session dựa vào nó,
  // nên nếu engine ngừng trả field này thì cơ chế nén sẽ chết âm thầm.
  assert.ok(t2.tokens && t2.tokens.input > 0, 'phải có tokens.input để đo cửa sổ (D-0055 mục 3)');

  // Cửa sổ thật = `input` + `cache.read`, KHÔNG phải mỗi `input`.
  //
  // Có prompt-cache thì phần lịch sử cũ chuyển sang `cache.read` và `input` lượt sau
  // có thể NHỎ hơn lượt đầu (đo thật: 8048 → 6131 với cache.read 1920). So mỗi
  // `input` là so nhầm, và nếu tầng trí nhớ cũng chỉ nhìn `input` thì nó sẽ tưởng
  // cửa sổ đang co lại trong khi hội thoại vẫn dài ra — tức là không bao giờ nén.
  const occupied = (t) => t.tokens.input + (t.tokens.cache ? t.tokens.cache.read : 0);
  assert.ok(occupied(t2) > occupied(t1),
    `cửa sổ phải phình ra theo lịch sử: lượt1=${occupied(t1)} lượt2=${occupied(t2)}`);

  store.close();
});

test('opencode thật: danh sách model duyệt được từ API, không rỗng', { skip, timeout: 120000 }, async () => {
  const engine = new OpencodeEngine({ modelPreference: [] });
  const models = await engine.listModels();
  assert.ok(models.length > 0, 'phải liệt kê được model — nếu rỗng thì fallback vòng model chết');
  assert.ok(models.every((m) => m.includes('/')), 'mỗi model phải dạng provider/model');
});
