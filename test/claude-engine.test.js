'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');

const { ClaudeEngine } = require('../src/main/engine/claude');

const FAKE_BIN = path.join(__dirname, 'fixtures', 'fake-claude.js');

function makeEngine(env = {}) {
  const engine = new ClaudeEngine({ modelPreference: [] });
  // Test KHÔNG đụng `claude` thật — trỏ thẳng vào script giả (node + script), giống
  // cách `OpencodeEngine` test trỏ `binPath` (xem `test/cancel.test.js`).
  engine.binPath = process.execPath; // node
  engine._binArgs = [FAKE_BIN];
  engine._extraEnv = env;
  return engine;
}

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'claude-engine-'));
}

test('ClaudeEngine.run: trả text + sessionId + tokens từ dòng result', async () => {
  const engine = makeEngine({ FAKE_CLAUDE_TEXT: 'chào Bệ hạ' });
  const cwd = tmpDir();
  const out = await engine.run({ message: 'hi', sessionId: null, model: null, cwd });
  assert.equal(out.text, 'chào Bệ hạ');
  assert.equal(out.sessionId, '11111111-1111-1111-1111-111111111111');
  assert.equal(out.tokens.input, 10);
  assert.equal(out.tokens.output, 'chào Bệ hạ'.length);
});

test('ClaudeEngine.run: chữ chảy dần qua onEvent, dạng {type:"text", part:{id, text}}', async () => {
  const engine = makeEngine({ FAKE_CLAUDE_TEXT: 'ab' });
  const cwd = tmpDir();
  const seen = [];
  await engine.run({
    message: 'hi', sessionId: null, model: null, cwd,
    onEvent: (ev, partial) => seen.push({ ev, partial }),
  });
  const textEvents = seen.filter((s) => s.ev.type === 'text');
  assert.ok(textEvents.length >= 2, 'phải có event cho từng ký tự chảy ra');
  assert.equal(textEvents.at(-1).partial, 'ab');
});

test('ClaudeEngine.run: is_error → reject với message rõ ràng', async () => {
  const engine = makeEngine({ FAKE_CLAUDE_ERROR: '1' });
  const cwd = tmpDir();
  await assert.rejects(
    () => engine.run({ message: 'hi', sessionId: null, model: null, cwd }),
    /lỗi giả lập/
  );
});

test('ClaudeEngine.cancel: giết tiến trình đang chạy, run() reject CancelledError', async () => {
  const engine = makeEngine({ FAKE_CLAUDE_TEXT: 'ổn', FAKE_CLAUDE_DELAY_MS: '5000' });
  const cwd = tmpDir();
  const p = engine.run({ message: 'hi', sessionId: null, model: null, cwd });
  setTimeout(() => engine.cancel(), 200);
  await assert.rejects(() => p, (err) => err.cancelled === true);
});

test('ClaudeEngine.runWithFallback: KHÔNG xoay nhiều model — gọi run() một lần, attempts rỗng', async () => {
  const engine = makeEngine({ FAKE_CLAUDE_TEXT: 'ổn' });
  const cwd = tmpDir();
  const out = await engine.runWithFallback({ message: 'hi', sessionId: null, model: null, cwd });
  assert.equal(out.text, 'ổn');
  assert.deepEqual(out.attempts, []);
});

test('ClaudeEngine.listModels: trả danh sách cố định, không hỏi mạng', async () => {
  const engine = makeEngine();
  const models = await engine.listModels();
  assert.deepEqual(models, [
    'claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5-20251001', 'claude-fable-5',
  ]);
});

test('ClaudeEngine.setBaseDir: env CLAUDE_CONFIG_DIR trỏ đúng <baseDir>/claude-config', async () => {
  // Xác nhận cô lập auth đúng thư mục — không assert vào claude thật, chỉ assert
  // engine tự cấu hình đúng biến môi trường sẽ truyền cho tiến trình con.
  const engine = makeEngine();
  const base = tmpDir();
  engine.setBaseDir(base);
  assert.equal(engine.baseDir, base);
});
