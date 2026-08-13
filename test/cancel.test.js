'use strict';

/**
 * Dừng một lượt.
 *
 * Bug đã dính (2026-08-13): bấm nút dừng khi Alice đang "tự xoay model free" thì
 * không có gì xảy ra. Nguyên nhân: `cancel()` giết tiến trình con, `run()` reject,
 * và `runWithFallback` — vốn tồn tại để xoay model khi model hỏng — không phân biệt
 * được "bị người dùng giết" với "model hỏng", nên nó vui vẻ chạy model kế tiếp.
 * Chuỗi model free có 6–7 model, nên bấm dừng nhìn như hoàn toàn vô tác dụng.
 *
 * Test ở tầng `runWithFallback` với `run` giả: chỗ hỏng nằm ở QUYẾT ĐỊNH xoay hay
 * không, không nằm ở việc spawn tiến trình.
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const { OpencodeEngine, CancelledError } = require('../src/main/engine/opencode');

function fakeEngine(models = ['x/m1', 'x/m2', 'x/m3']) {
  const engine = new OpencodeEngine({ modelPreference: [], idleTimeoutMs: 1000 });
  engine.listModels = async () => models.slice();
  return engine;
}

test('bấm dừng giữa lượt: DỪNG HẲN, không xoay sang model kế tiếp', async () => {
  const engine = fakeEngine();
  const tried = [];
  engine.run = async ({ model }) => {
    tried.push(model);
    engine.cancel();              // đúng lúc người dùng bấm nút dừng
    throw new CancelledError();   // tiến trình con bị giết → run() reject
  };

  await assert.rejects(
    () => engine.runWithFallback({ message: 'chào', cwd: '.' }),
    (err) => err.cancelled === true
  );
  assert.deepEqual(tried, ['x/m1'], 'chỉ được thử ĐÚNG một model rồi dừng');
});

test('model hỏng thật thì VẪN xoay hết chuỗi — lệnh dừng không làm hỏng fallback', async () => {
  const engine = fakeEngine();
  const tried = [];
  engine.run = async ({ model }) => {
    tried.push(model);
    throw new Error('hết quota');
  };

  await assert.rejects(
    () => engine.runWithFallback({ message: 'chào', cwd: '.' }),
    /Mọi model đều hỏng/
  );
  assert.deepEqual(tried, ['x/m1', 'x/m2', 'x/m3']);
});

test('cờ dừng thuộc về MỘT lượt — lượt sau không thừa hưởng', async () => {
  const engine = fakeEngine();
  engine.run = async ({ model }) => {
    if (engine._cancelled) throw new CancelledError();
    return { sessionId: 'ses_x', text: 'ổn', tokens: null, model, events: [] };
  };

  engine.cancel();
  assert.equal(engine._cancelled, true);

  // Lượt MỚI phải chạy bình thường; không reset cờ thì Alice câm vĩnh viễn sau
  // lần bấm dừng đầu tiên.
  const out = await engine.runWithFallback({ message: 'lượt sau', cwd: '.' });
  assert.equal(out.text, 'ổn');
  assert.equal(engine._cancelled, false);
});

test('dừng trước khi kịp spawn thì không mở thêm tiến trình nào', async () => {
  const engine = fakeEngine();
  engine.cancel();
  await assert.rejects(() => engine.run({ message: 'x', model: 'x/m1', cwd: '.' }),
    (err) => err.cancelled === true);
});

test('cancel() giết cả tiến trình phụ đang duyệt danh sách model', () => {
  const engine = fakeEngine();
  let killed = false;
  engine._probes.add({ kill: () => { killed = true; } });
  assert.equal(engine.cancel(), true);
  assert.equal(killed, true, 'opencode models phải bị giết — lượt đầu nằm gần trọn ở bước này');
  assert.equal(engine._probes.size, 0);
});
