# Multi-engine support (Claude Code + opencode) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mỗi Alice chọn được engine `opencode` (API key, đã có) hoặc `claude` (Claude
Code CLI, subscription ghim cứng theo Alice) — cùng một bộ luật rotation/compact/skill-
reload ở tầng `Memory`, khác nhau ở cách mỗi engine thực thi một lượt.

**Architecture:** Thêm `ClaudeEngine` (mirror `OpencodeEngine`'s interface:
`run/runWithFallback/listModels/cancel/setBaseDir/available`) để `turn.js`/`memory.js`
không cần biết đang nói chuyện với engine nào. `registry.js` thêm field `provider:
'opencode'|'claude'`. `main.js` chọn engine instance theo field đó lúc
`activateAlice()`/`publicServerFor()`.

**Tech Stack:** Node.js (Electron main process), `node:child_process`, `node:readline`,
`node:sqlite` (better-sqlite3-compatible qua `DatabaseSync`), `node:test`.

**Spec:** `docs/superpowers/specs/2026-08-13-multi-engine-support-design.md`

## Global Constraints

- Rotation rule mới: `tuổi session > 12h VÀ im lặng kể từ tin cuối > 1h` → xoay (AND,
  không phải OR) — cộng thêm vào 2 điều kiện cũ (hết ngày, tràn context), không thay thế.
- `ClaudeEngine` phải cùng interface với `OpencodeEngine` — `turn.js` KHÔNG được sửa để
  rẽ nhánh theo engine.
- Cô lập auth Claude bằng `CLAUDE_CONFIG_DIR=<alice-home>/claude-config` — đã xác minh
  thật (không phải giả định) rằng nó cô lập cả token OAuth, không chỉ settings.
- Hook `SessionStart` của Claude Code: bỏ trống `matcher` để bắt mọi nguồn
  (`startup`/`resume`/`compact`/`clear`) — đã xác minh thật qua log stream-json.
- Không tự dựng UI OAuth trong Electron cho Claude — người dùng tự chạy `claude login`
  (câu lệnh app in sẵn, có `CLAUDE_CONFIG_DIR` đúng của Alice đó) trong terminal.
- Không đổi cơ chế multi-model-fallback hiện có của `OpencodeEngine`.

---

### Task 1: Rotation rule mới trong `Memory` — tuổi > 12h VÀ im lặng > 1h

**Files:**
- Modify: `src/main/memory/store.js` (thêm method)
- Modify: `src/main/memory/memory.js:56-78` (`ensureConversation`)
- Test: `test/memory.test.js`

**Interfaces:**
- Produces: `Store.lastMessageTs(convId): number|null`, `Memory._staleReason(current, now):
  string|null` (`'daily'|'stale'|null`) — dùng nội bộ, không export thêm khỏi module.

- [ ] **Step 1: Viết test cho `Store.lastMessageTs`**

Mở `test/memory.test.js`, tìm khối test của `Store` (import từ `../src/main/memory/store`),
thêm:

```js
test('lastMessageTs: trả ts của tin GẦN NHẤT trong hội thoại, null nếu chưa có tin nào', () => {
  const store = new Store(':memory:');
  const conv = store.createConversation({ id: 'c1', day: '2026-08-13' });
  assert.equal(store.lastMessageTs(conv.id), null);
  store.add({ convId: conv.id, role: 'human', text: 'một', ts: 1000 });
  store.add({ convId: conv.id, role: 'alice', text: 'hai', ts: 2000 });
  assert.equal(store.lastMessageTs(conv.id), 2000);
  store.close();
});
```

- [ ] **Step 2: Chạy test, xác nhận FAIL**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1 memory
```
Expected: FAIL — `store.lastMessageTs is not a function`.

- [ ] **Step 3: Thêm `Store.lastMessageTs`**

Trong `src/main/memory/store.js`, ngay dưới `lastTokens(convId)` (khoảng dòng 274), thêm:

```js
  /** `ts` của tin GẦN NHẤT trong hội thoại — dùng để tính "đã im lặng bao lâu" cho
   * rotation. `null` nếu hội thoại chưa có tin nào. */
  lastMessageTs(convId) {
    const row = this.db.prepare(
      'SELECT ts FROM messages WHERE conv_id = ? ORDER BY id DESC LIMIT 1'
    ).get(convId);
    return row ? row.ts : null;
  }
```

- [ ] **Step 4: Chạy lại, xác nhận PASS**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1 memory
```
Expected: PASS.

- [ ] **Step 5: Viết test cho rotation rule mới (AND: tuổi>12h VÀ idle>1h)**

Thêm vào `test/memory.test.js`, cạnh các test rotation hiện có (tìm khối test
`Memory`/`ensureConversation`):

```js
const ONE_HOUR = 3600 * 1000;
const TWELVE_HOURS = 12 * ONE_HOUR;

function memoryWith(store, overrides = {}) {
  const settings = {
    contextCeiling: 100000, windowRatio: 0.6, compactRatio: 0.8,
    keepVerbatim: 10, rotateDaily: false, ...overrides,
  };
  return new Memory(store, settings, async () => 'tóm tắt giả');
}

test('rotation: tuổi > 12h NHƯNG vẫn đang nhắn liên tục (idle < 1h) → KHÔNG xoay', async () => {
  const store = new Store(':memory:');
  const memory = memoryWith(store);
  const base = new Date('2026-08-13T00:00:00Z');
  await memory.ensureConversation(base); // tạo hội thoại đầu tiên
  const conv = store.currentConversation();
  store.add({ convId: conv.id, role: 'human', text: 'chào', ts: base.getTime() });

  // 13 tiếng sau, nhưng tin gần nhất cách đây có 5 phút (idle < 1h).
  const later = new Date(base.getTime() + TWELVE_HOURS + ONE_HOUR);
  store.add({ convId: conv.id, role: 'human', text: 'vẫn đang nói',
              ts: later.getTime() - 5 * 60 * 1000 });

  const { conversation } = await memory.ensureConversation(later);
  assert.equal(conversation.id, conv.id, 'tuổi một mình không đủ — phải còn idle>1h nữa mới xoay');
  store.close();
});

test('rotation: tuổi > 12h VÀ im lặng > 1h → xoay', async () => {
  const store = new Store(':memory:');
  const memory = memoryWith(store);
  const base = new Date('2026-08-13T00:00:00Z');
  await memory.ensureConversation(base);
  const conv = store.currentConversation();
  store.add({ convId: conv.id, role: 'human', text: 'chào', ts: base.getTime() });

  // 14 tiếng sau, tin gần nhất cách đây 2 tiếng (idle > 1h) → cả hai trục đều thoả.
  const later = new Date(base.getTime() + TWELVE_HOURS + 2 * ONE_HOUR);
  const { conversation, seed } = await memory.ensureConversation(later);
  assert.notEqual(conversation.id, conv.id, 'phải xoay sang hội thoại mới');
  assert.ok(seed, 'hội thoại mới phải có mồi tiếp nối');
  store.close();
});

test('rotation: im lặng > 1h NHƯNG session chưa tới 12h tuổi → KHÔNG xoay', async () => {
  const store = new Store(':memory:');
  const memory = memoryWith(store);
  const base = new Date('2026-08-13T00:00:00Z');
  await memory.ensureConversation(base);
  const conv = store.currentConversation();
  store.add({ convId: conv.id, role: 'human', text: 'chào', ts: base.getTime() });

  // Chỉ 2 tiếng tuổi, nhưng im lặng những 90 phút — chưa đủ 12h tuổi nên KHÔNG xoay.
  const later = new Date(base.getTime() + 2 * ONE_HOUR);
  const { conversation } = await memory.ensureConversation(later);
  assert.equal(conversation.id, conv.id);
  store.close();
});
```

Đảm bảo `Store`/`Memory` đã được import ở đầu `test/memory.test.js` (kiểm tra dòng import
hiện có — nếu thiếu thì thêm `const { Store } = require('../src/main/memory/store');` và
`const { Memory } = require('../src/main/memory/memory');`).

- [ ] **Step 6: Chạy test, xác nhận FAIL**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1 memory
```
Expected: FAIL trên 2 test đầu (rotation theo tuổi chưa tồn tại) — test thứ 3 có thể PASS
tình cờ (hành vi hiện tại đã đúng "không xoay" khi không thoả gì), không sao.

- [ ] **Step 7: Thêm rotation rule vào `memory.js`**

Trong `src/main/memory/memory.js`, thêm hằng số ngay dưới dòng `class Memory {` (trước
`constructor`), và thay `ensureConversation`:

```js
const ONE_HOUR_MS = 3600 * 1000;
const SESSION_MAX_AGE_MS = 12 * ONE_HOUR_MS;
const SESSION_IDLE_MS = ONE_HOUR_MS;

class Memory {
  ...
  async ensureConversation(now = new Date()) {
    const day = Memory.today(now);
    const current = this.store.currentConversation();

    if (!current) {
      return { conversation: this._fresh(day, null, ''), seed: null, reason: 'first-run' };
    }

    const staleReason = this._staleReason(current, now, day);
    const conversation = staleReason
      ? (await this._rotate(current, day, staleReason)).conversation
      : current;

    const seed = this.store.takePendingSeed(conversation.id);
    return {
      conversation,
      seed,
      reason: seed ? (conversation.rotated_from ? 'rotated' : 'seeded') : null,
    };
  }

  /**
   * Vì sao phải xoay TRƯỚC khi chạy lượt này — rỗng = dùng tiếp session cũ.
   *
   * Ba trục, cộng dồn (không thay nhau): hết ngày (`daily`), tràn context (kiểm ở
   * `afterTurn`, không nằm ở đây), và MỚI — tuổi + im lặng (`stale`). Trục `stale`
   * dùng AND (không phải OR như hai trục kia đơn lẻ): một session 13 tiếng tuổi mà
   * vẫn đang nhắn liên tục KHÔNG bị xoay — tuổi một mình không phải tín hiệu đủ.
   */
  _staleReason(current, now, day) {
    if (this.settings.rotateDaily && current.day !== day) return 'daily';
    const ageMs = now.getTime() - current.created_at;
    if (ageMs <= SESSION_MAX_AGE_MS) return null;
    const lastTs = this.store.lastMessageTs(current.id);
    const idleMs = lastTs ? now.getTime() - lastTs : 0;
    if (idleMs > SESSION_IDLE_MS) return 'stale';
    return null;
  }
  ...
```

Xoá dòng `const conversation = (this.settings.rotateDaily && current.day !== day) ? ...`
cũ — đã được `_staleReason` thay thế hoàn toàn (bao gồm cả điều kiện `daily` cũ).

- [ ] **Step 8: Chạy lại toàn bộ `memory.test.js`, xác nhận PASS**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1 memory
```
Expected: PASS toàn bộ, kể cả các test rotation CŨ (hết ngày, tràn context) — không được
để rotation mới phá rotation cũ.

- [ ] **Step 9: Chạy toàn bộ suite, xác nhận không phá gì khác**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1
```
Expected: tất cả PASS.

- [ ] **Step 10: Commit**

```bash
git add src/main/memory/store.js src/main/memory/memory.js test/memory.test.js
git commit -m "feat: xoay session khi tuổi >12h VÀ im lặng >1h (thêm, không thay rotation cũ)"
```

---

### Task 2: `ClaudeEngine` — spawn `claude`, parse `stream-json`

**Files:**
- Create: `src/main/engine/claude.js`
- Test: `test/claude-engine.test.js`
- Test fixture: `test/fixtures/fake-claude.js` (script giả lập `claude` CLI cho unit test
  nhanh — KHÔNG gọi Claude thật, không tốn subscription)

**Interfaces:**
- Consumes: không phụ thuộc task nào trước (độc lập).
- Produces: `class ClaudeEngine` với `constructor(settings)`, `setBaseDir(dir)`,
  `get available`, `async listModels()`, `async run(opts)`, `async runWithFallback(opts)`,
  `cancel()` — Task 5 (`main.js`) và Task 4 (`registry.js`) dùng class này.

**Schema `stream-json` đã đo THẬT** (xem spec, mục "Schema stream-json đo THẬT") — dùng
làm căn cứ viết parser, KHÔNG đoán.

- [ ] **Step 1: Viết `claude` giả cho test**

Tạo `test/fixtures/fake-claude.js`:

```js
#!/usr/bin/env node
'use strict';

/**
 * `claude` giả — in ra đúng khuôn `stream-json` đã đo thật từ CLI thật (xem spec
 * 2026-08-13), để test `ClaudeEngine` không tốn subscription thật và chạy được
 * trên máy CI không có `claude`.
 *
 * Điều khiển qua biến môi trường (test set trước khi spawn):
 *   FAKE_CLAUDE_TEXT     text trả về (mặc định "ổn")
 *   FAKE_CLAUDE_EXIT     mã thoát (mặc định 0)
 *   FAKE_CLAUDE_ERROR    có mặt (bất kỳ giá trị) → in dòng result lỗi thay vì thành công
 *   FAKE_CLAUDE_DELAY_MS trễ trước khi in xong, để test cancel() (mặc định 0)
 */

const sessionId = process.env.FAKE_CLAUDE_SESSION || '11111111-1111-1111-1111-111111111111';
const text = process.env.FAKE_CLAUDE_TEXT || 'ổn';
const exitCode = Number(process.env.FAKE_CLAUDE_EXIT || 0);
const isError = Boolean(process.env.FAKE_CLAUDE_ERROR);
const delayMs = Number(process.env.FAKE_CLAUDE_DELAY_MS || 0);

function line(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

line({ type: 'system', subtype: 'init', session_id: sessionId, model: 'claude-sonnet-5' });
for (const ch of text) {
  line({
    type: 'stream_event',
    event: { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: ch } },
    session_id: sessionId,
  });
}

setTimeout(() => {
  if (isError) {
    line({ type: 'result', subtype: 'error_during_execution', is_error: true,
           result: 'lỗi giả lập', session_id: sessionId });
  } else {
    line({
      type: 'result', subtype: 'success', is_error: false, result: text,
      session_id: sessionId,
      usage: { input_tokens: 10, output_tokens: text.length, cache_read_input_tokens: 0 },
    });
  }
  process.exit(exitCode);
}, delayMs);
```

- [ ] **Step 2: Viết test `run()` — text thường**

Tạo `test/claude-engine.test.js`:

```js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');

const { ClaudeEngine, CancelledError } = require('../src/main/engine/claude');

const FAKE_BIN = path.join(__dirname, 'fixtures', 'fake-claude.js');

function makeEngine(env = {}) {
  const engine = new ClaudeEngine({ modelPreference: [] });
  // Test KHÔNG đụng `claude` thật — trỏ thẳng vào script giả, giống cách
  // `OpencodeEngine` test trỏ `binPath` (xem `test/cancel.test.js`).
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
  // Phải có ít nhất một event `text` với `part.text` là chuỗi đang chảy dần.
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
```

- [ ] **Step 3: Chạy test, xác nhận FAIL**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1 claude-engine
```
Expected: FAIL — `Cannot find module '../src/main/engine/claude'`.

- [ ] **Step 4: Viết `src/main/engine/claude.js`**

```js
'use strict';

const { spawn } = require('node:child_process');
const readline = require('node:readline');
const path = require('node:path');
const fs = require('node:fs');

const CLAUDE_MODELS = [
  'claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5-20251001', 'claude-fable-5',
];

class CancelledError extends Error {
  constructor() {
    super('Đã dừng theo yêu cầu.');
    this.cancelled = true;
  }
}

/**
 * Bọc CLI `claude` (Claude Code) thành cùng interface với `OpencodeEngine`, để
 * `turn.js`/`memory.js` không phải biết đang nói chuyện với engine nào.
 *
 * Khác `OpencodeEngine` ở hai chỗ CỐ Ý:
 *   - Không có "xoay nhiều model free khi hỏng" — subscription Claude không có khái
 *     niệm đó. `runWithFallback` chỉ là `run()` một lần, `attempts` luôn rỗng.
 *   - `listModels()` trả danh sách CỐ ĐỊNH — `claude` không có lệnh liệt kê model
 *     kiểu `opencode models`.
 */
class ClaudeEngine {
  constructor(settings) {
    this.settings = settings || {};
    this.baseDir = null;
    this._cancelled = false;
    this._child = null;
    // Cho test ghi đè: trỏ vào script giả thay vì `claude` thật trên PATH.
    this.binPath = 'claude';
    this._binArgs = [];
    this._extraEnv = {};
  }

  setBaseDir(dir) {
    this.baseDir = dir;
  }

  get available() {
    if (this.binPath !== 'claude') return true; // test đã trỏ thẳng vào script giả
    // Không dò PATH thủ công — để spawn tự thất bại và báo lỗi rõ ràng lúc chạy
    // thật, giống cách OpencodeEngine coi `available` dựa trên `fs.existsSync`.
    // Ở đây không có một đường dẫn cố định để `existsSync` (CLI cài qua PATH), nên
    // coi là "có khả năng dùng được" và để `run()` báo lỗi cụ thể nếu spawn hỏng.
    return true;
  }

  /** Danh sách cố định — xem docstring class. */
  async listModels() {
    return CLAUDE_MODELS.slice();
  }

  /** Không có chuỗi model để xoay — gọi `run()` một lần, giữ contract `attempts: []`
   * để `turn.js` không phải rẽ nhánh theo engine. */
  async runWithFallback(opts) {
    this._cancelled = false;
    const out = await this.run(opts);
    return { ...out, attempts: [] };
  }

  /**
   * @param {object} opts
   * @param {string} opts.message
   * @param {string?} opts.sessionId  session id CỦA CHÍNH `claude` (không phải `ses_xxx`
   *   của opencode) — `null` = phiên mới.
   * @param {string?} opts.model
   * @param {string} opts.cwd
   * @param {function} [opts.onEvent]  (ev, partialText) — `ev` cùng hình dạng với
   *   event của `OpencodeEngine` (`{type:'text'|'tool'|'step_finish'|'error', part}`)
   *   để `activity.js#toolActivity` dùng chung, không cần biết engine nào.
   */
  run({ message, sessionId = null, model = null, cwd, onEvent = null }) {
    if (this._cancelled) return Promise.reject(new CancelledError());

    const sessionArgs = sessionId ? ['--resume', sessionId] : ['--session-id', randomUuid()];
    const modelArgs = model ? ['--model', model] : [];
    const args = [
      ...this._binArgs,
      '--print', '--output-format', 'stream-json', '--verbose',
      '--include-partial-messages',
      '--dangerously-skip-permissions',
      ...sessionArgs, ...modelArgs,
      message,
    ];

    return new Promise((resolve, reject) => {
      const child = spawn(this.binPath, args, {
        cwd,
        windowsHide: true,
        env: {
          ...process.env,
          ...(this.baseDir ? { CLAUDE_CONFIG_DIR: path.join(this.baseDir, 'claude-config') } : {}),
          ...this._extraEnv,
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      this._child = child;

      let text = '';
      let resolvedSession = sessionId;
      let tokens = null;
      let stderr = '';
      let resultLine = null;
      const toolNames = new Map(); // block index → tool name, để đóng event `tool` đúng tên

      const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
      rl.on('line', (raw) => {
        const t = raw.trim();
        if (!t.startsWith('{')) return;
        let ev;
        try { ev = JSON.parse(t); } catch { return; }

        if (ev.type === 'system' && ev.session_id) resolvedSession = ev.session_id;

        if (ev.type === 'stream_event' && ev.event) {
          const se = ev.event;
          if (se.type === 'content_block_start' && se.content_block
              && se.content_block.type === 'tool_use') {
            toolNames.set(se.index, se.content_block.name);
            if (onEvent) {
              onEvent({ type: 'tool', part: {
                tool: se.content_block.name, id: se.content_block.id,
                callID: se.content_block.id, state: { status: 'running' },
              } }, text);
            }
          } else if (se.type === 'content_block_delta' && se.delta
              && se.delta.type === 'text_delta') {
            text += se.delta.text;
            if (onEvent) onEvent({ type: 'text', part: { id: `t${se.index}`, text } }, text);
          } else if (se.type === 'content_block_stop' && toolNames.has(se.index)) {
            const name = toolNames.get(se.index);
            if (onEvent) {
              onEvent({ type: 'tool', part: {
                tool: name, id: `${se.index}`, callID: `${se.index}`,
                state: { status: 'completed' },
              } }, text);
            }
          }
        }

        if (ev.type === 'result') resultLine = ev;
      });

      child.stderr.on('data', (b) => { stderr += b.toString('utf8'); });

      child.on('error', (err) => {
        this._child = null;
        reject(new Error(`Không chạy được claude: ${err.message}`));
      });

      child.on('close', (code) => {
        this._child = null;
        if (this._cancelled) { reject(new CancelledError()); return; }
        if (!resultLine) {
          reject(new Error(`claude thoát mã ${code} không có dòng result: ${stderr.trim().slice(0, 500) || '(không có stderr)'}`));
          return;
        }
        if (resultLine.is_error) {
          reject(new Error(resultLine.result || 'claude báo lỗi không rõ'));
          return;
        }
        if (resultLine.usage) {
          tokens = {
            input: resultLine.usage.input_tokens || 0,
            output: resultLine.usage.output_tokens || 0,
            cache: { read: resultLine.usage.cache_read_input_tokens || 0 },
          };
        }
        resolve({
          sessionId: resultLine.session_id || resolvedSession,
          text: resultLine.result != null ? resultLine.result : text,
          tokens, model: model || null, events: [],
        });
      });
    });
  }

  /** Giết cả cây tiến trình — `claude` có thể đẻ tiến trình con. */
  cancel() {
    this._cancelled = true;
    if (this._child) {
      if (process.platform === 'win32') {
        require('node:child_process').spawn(
          'taskkill', ['/F', '/T', '/PID', String(this._child.pid)], { windowsHide: true }
        );
      } else {
        this._child.kill();
      }
      this._child = null;
      return true;
    }
    return false;
  }
}

function randomUuid() {
  return require('node:crypto').randomUUID();
}

module.exports = { ClaudeEngine, CancelledError, CLAUDE_MODELS };
```

- [ ] **Step 5: Chạy lại, xác nhận PASS**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1 claude-engine
```
Expected: PASS toàn bộ 6 test.

- [ ] **Step 6: Xác minh THẬT schema tool-use với `claude` thật (một lần, thủ công — không phải test tự động)**

Chạy trực tiếp (tốn một lượt gọi thật, dùng model rẻ):

```bash
claude --print --output-format stream-json --verbose --include-partial-messages --model claude-haiku-4-5-20251001 "Run: echo hi (use the Bash tool)"
```

Đọc output, so khớp field `content_block_start`/`content_block.type === "tool_use"` với
code Step 4. Lệch chỗ nào thì sửa Step 4 ngay, chạy lại Step 5.

- [ ] **Step 7: Commit**

```bash
git add src/main/engine/claude.js test/claude-engine.test.js test/fixtures/fake-claude.js
git commit -m "feat: thêm ClaudeEngine — cùng interface với OpencodeEngine, chạy claude CLI"
```

---

### Task 3: Hook nạp lại skill sau compact (Claude Code) trong `provisionWorkspace`

**Files:**
- Modify: `src/main/alice.js`
- Test: `test/workspace.test.js`

**Interfaces:**
- Consumes: không phụ thuộc Task 1/2.
- Produces: `provisionWorkspace()` giờ SINH THÊM `workspace/.claude/settings.json` và
  `workspace/.claude-hooks/reload-skill.js` — Task 5 không cần đụng gì thêm ở đây, chỉ
  gọi `provisionWorkspace()` như cũ.

- [ ] **Step 1: Viết test**

Mở `test/workspace.test.js`, thêm (dùng đúng biến `sandbox`/cách gọi `provisionWorkspace`
đã có sẵn trong file — xem test hiện tại để lấy đúng tên biến):

```js
test('provisionWorkspace: sinh hook SessionStart cho Claude Code — không matcher, bắt cả compact', () => {
  const dir = provisionWorkspace(
    { contextCeiling: 1000, windowRatio: 0.6, compactRatio: 0.8, keepVerbatim: 4 },
    { dir: path.join(sandbox, 'ws-claude-hook') }
  );

  const settingsPath = path.join(dir, '.claude', 'settings.json');
  assert.ok(fs.existsSync(settingsPath), 'phải sinh .claude/settings.json');
  const settings = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
  const hookEntry = settings.hooks.SessionStart[0];
  assert.equal(hookEntry.matcher, undefined, 'bỏ trống matcher — bắt mọi nguồn kể cả compact');
  assert.match(hookEntry.hooks[0].command, /reload-skill\.js/);

  const hookScript = path.join(dir, '.claude-hooks', 'reload-skill.js');
  assert.ok(fs.existsSync(hookScript), 'phải sinh script hook');
});

test('provisionWorkspace: hook reload-skill.js in ĐÚNG nội dung AGENTS.md ra stdout', () => {
  const dir = provisionWorkspace(
    { contextCeiling: 1000, windowRatio: 0.6, compactRatio: 0.8, keepVerbatim: 4 },
    { dir: path.join(sandbox, 'ws-claude-hook-2') }
  );
  const hookScript = path.join(dir, '.claude-hooks', 'reload-skill.js');
  const out = require('node:child_process').execFileSync(
    process.execPath, [hookScript], { cwd: dir, encoding: 'utf8' }
  );
  const agentsMd = fs.readFileSync(path.join(dir, 'AGENTS.md'), 'utf8');
  assert.match(out, /<alice-workspace-reload>/);
  assert.ok(out.includes(agentsMd.trim().slice(0, 200)), 'phải chứa nội dung AGENTS.md');
});
```

- [ ] **Step 2: Chạy test, xác nhận FAIL**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1 workspace
```
Expected: FAIL — thiếu `.claude/settings.json`.

- [ ] **Step 3: Thêm hàm sinh hook vào `src/main/alice.js`**

Thêm hai hàm mới (đặt sau `buildOpencodeJson`, trước `provisionWorkspace`):

```js
/**
 * Hook `SessionStart` cho Claude Code — bỏ trống `matcher` để bắt MỌI nguồn
 * (`startup`, `resume`, `compact`, `clear`). Đã xác minh thật 2026-08-13: hook không
 * `matcher` của `kd-reserve/automation` tự chạy đúng lúc `SessionStart:startup`.
 *
 * output của hook được Claude Code nạp thẳng vào context — đây là lớp phòng thủ DUY
 * NHẤT sống ngoài model, nên auto-compact không xoá được, kể cả khi nó xoá sạch phần
 * còn lại của context.
 */
function buildClaudeSettings() {
  return {
    hooks: {
      SessionStart: [
        {
          hooks: [
            { type: 'command', command: 'node .claude-hooks/reload-skill.js' },
          ],
        },
      ],
    },
  };
}

/** Script hook — đọc AGENTS.md CÙNG THƯ MỤC workspace, in ra stdout. Không tự dựng nội
 * dung: nhờ vậy không có bản luật thứ hai trôi khác AGENTS.md (tham khảo
 * `kd-reserve/automation/knowledge/tools/reminder.js`, đã chạy thật). */
function buildReloadSkillHook() {
  return `#!/usr/bin/env node
'use strict';
const fs = require('fs');
const path = require('path');
const agentsPath = path.join(__dirname, '..', 'AGENTS.md');
if (!fs.existsSync(agentsPath)) { process.exit(0); }
const content = fs.readFileSync(agentsPath, 'utf8').trim();
if (!content) { process.exit(0); }
process.stdout.write([
  '<alice-workspace-reload>',
  'Luật của workspace này, nạp lại tự động vì phiên vừa khởi động hoặc vừa auto-compact.',
  'Ký ức trong context sau compaction KHÔNG đáng tin.',
  '',
  content,
  '</alice-workspace-reload>',
  '',
].join('\\n'));
`;
}
```

Sửa `provisionWorkspace` — thêm hai dòng ghi file sau dòng ghi `opencode.json`:

```js
function provisionWorkspace(settings, { brainMcp = null, dir = null } = {}) {
  const target = dir || config.workDir();
  fs.mkdirSync(target, { recursive: true });

  fs.writeFileSync(path.join(target, 'AGENTS.md'), buildAgentsMd(config.knowledgeDir()), 'utf8');
  fs.writeFileSync(
    path.join(target, 'opencode.json'),
    JSON.stringify(buildOpencodeJson(settings, { brainMcp }), null, 2),
    'utf8'
  );

  // Hook cho Claude Code — sinh LUÔN dù Alice đang dùng opencode: vô hại (opencode
  // không đọc `.claude/`), và Alice đổi provider sau này thì hook đã sẵn sàng.
  const claudeDir = path.join(target, '.claude');
  const hooksDir = path.join(target, '.claude-hooks');
  fs.mkdirSync(claudeDir, { recursive: true });
  fs.mkdirSync(hooksDir, { recursive: true });
  fs.writeFileSync(
    path.join(claudeDir, 'settings.json'),
    JSON.stringify(buildClaudeSettings(), null, 2),
    'utf8'
  );
  fs.writeFileSync(path.join(hooksDir, 'reload-skill.js'), buildReloadSkillHook(), 'utf8');

  return target;
}
```

Cập nhật `module.exports` cuối file, thêm `buildClaudeSettings, buildReloadSkillHook`.

- [ ] **Step 4: Chạy lại, xác nhận PASS**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1 workspace
```
Expected: PASS toàn bộ.

- [ ] **Step 5: Chạy toàn bộ suite**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1
```
Expected: PASS toàn bộ — đặc biệt chú ý các test `public-server.test.js` vẫn pass (chúng
cũng gọi `provisionWorkspace` gián tiếp qua `PublicServer.start()`).

- [ ] **Step 6: Commit**

```bash
git add src/main/alice.js test/workspace.test.js
git commit -m "feat: sinh hook SessionStart cho Claude Code trong provisionWorkspace"
```

---

### Task 4: `registry.js` — field `provider` nhận `'claude'`, helper `claudeConfigDir`

**Files:**
- Modify: `src/main/registry.js`
- Test: `test/registry.test.js`

**Interfaces:**
- Consumes: không phụ thuộc task nào (độc lập với 1-3).
- Produces: `registry.create({..., provider})` nhận `provider` (mặc định `'opencode'`
  như cũ); `registry.claudeConfigDir(aliceHomeDir): string` — Task 5 dùng hàm này khi
  spawn `claude`.

- [ ] **Step 1: Viết test**

Thêm vào `test/registry.test.js`:

```js
test('create: provider mặc định opencode, chọn được claude', () => {
  const paths = tmpPaths();
  const a1 = registry.create({ name: 'A', key: 'k1' }, paths).alice;
  assert.equal(a1.provider, 'opencode');

  const a2 = registry.create({ name: 'B', provider: 'claude' }, paths).alice;
  assert.equal(a2.provider, 'claude');
  // Claude không cần API key — không được đòi `key` bắt buộc cho provider này.
});

test('claudeConfigDir: nằm TRONG thư mục của Alice, cô lập theo từng Alice', () => {
  const home = path.join(os.tmpdir(), 'alice-claude-cfg-test');
  const dir = registry.claudeConfigDir(home);
  assert.equal(dir, path.join(home, 'claude-config'));
});
```

- [ ] **Step 2: Chạy test, xác nhận FAIL**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1 registry
```
Expected: FAIL — `a1.provider` hiện đã đúng `'opencode'` (không đổi) nhưng
`registry.claudeConfigDir` chưa tồn tại → `TypeError`.

- [ ] **Step 3: Sửa `registry.js`**

Trong hàm `create()`, sửa chữ ký và validate:

```js
function create({ name, key, model = null, dir = null, provider = 'opencode' }, paths = {}) {
  const p = resolve(paths);
  const state = load(paths);
  const id = crypto.randomUUID();
  ...
  const alice = {
    id,
    name: String(name || '').trim() || 'Alice',
    provider,
    model: model || null,
    created_at: Date.now(),
  };
  if (custom) alice.dir = home;
  state.alices.push(alice);
  if (!state.active) state.active = id;
  save(state, paths);

  fs.mkdirSync(home, { recursive: true });
  fs.mkdirSync(path.join(home, 'brain'), { recursive: true });
  // Claude dùng subscription (đăng nhập qua `claude login`, cô lập bằng
  // CLAUDE_CONFIG_DIR) — KHÔNG có API key để lưu.
  if (provider === 'opencode' && key && String(key).trim()) {
    auth.setApiKey('opencode', String(key).trim(), home);
  }
  return { state, alice };
}
```

Thêm hàm mới, đặt cạnh `aliceDirOf`:

```js
/** Thư mục CLAUDE_CONFIG_DIR riêng của một Alice dùng provider `claude` — cô lập
 * subscription, không ảnh hưởng đăng nhập `claude` global của máy hay Alice khác. */
function claudeConfigDir(aliceHomeDir) {
  return path.join(aliceHomeDir, 'claude-config');
}
```

Cập nhật `module.exports` cuối file, thêm `claudeConfigDir`.

- [ ] **Step 4: Sửa lời gọi `alice:alice:create` trong `main.js` để truyền `provider`**

Trong `src/main/main.js`, tìm `ipcMain.handle('alice:alice:create', ...)`
(khoảng dòng 407-427). Sửa:

```js
ipcMain.handle('alice:alice:create', async (_e, { name, key, model, dir, provider }) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  const nameT = String(name || '').trim();
  const providerT = provider === 'claude' ? 'claude' : 'opencode';
  const keyT = String(key || '').trim();
  if (!nameT) return { error: 'Nhập tên cho Alice.' };
  if (providerT === 'opencode' && !keyT) return { error: 'Alice cần một chìa khoá riêng.' };
  const dirT = String(dir || '').trim();
  if (dirT && !fs.existsSync(dirT)) return { error: `Không thấy thư mục: ${dirT}` };
  let state;
  let alice;
  try {
    ({ state, alice } = registryModule.create({
      name: nameT, key: keyT, model: model || null, dir: dirT || null, provider: providerT,
    }));
  } catch (err) {
    return { error: String(err.message || err) };
  }
  registry = state;
  log.info(`alice created: ${alice.id} (${alice.name}) provider=${alice.provider} model=${alice.model || 'auto'} dir=${alice.dir || 'mặc định'}`);
  try {
    await activateAlice(alice.id);
  } catch (err) {
    return { error: String(err.message || err) };
  }
  return { alice };
});
```

(Đây là phần sửa CHỒNG lên phần "chặn trùng thư mục" đã làm trước đó trong phiên —
giữ nguyên khối `try { ({state, alice} = registryModule.create(...)) } catch` đã có, chỉ
thêm `providerT` và điều kiện `keyT` bắt buộc CHỈ khi `opencode`.)

- [ ] **Step 5: Chạy lại test, xác nhận PASS**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1 registry
```
Expected: PASS toàn bộ.

- [ ] **Step 6: Chạy toàn bộ suite**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1
```
Expected: PASS toàn bộ.

- [ ] **Step 7: Commit**

```bash
git add src/main/registry.js src/main/main.js test/registry.test.js
git commit -m "feat: registry nhận provider claude, thêm claudeConfigDir cho cô lập subscription"
```

---

### Task 5: `main.js` — chọn engine theo `provider`, IPC đổi provider + xem trạng thái đăng nhập Claude

**Files:**
- Modify: `src/main/main.js`

**Interfaces:**
- Consumes: `ClaudeEngine` (Task 2), `registryModule.claudeConfigDir` (Task 4).
- Produces: IPC mới `alice:alice:set-provider`, `alice:claude:status` — Task 6 (renderer)
  gọi qua `window.alice.*`.

- [ ] **Step 1: Thêm import + hàm chọn engine**

Đầu `src/main/main.js`, cạnh `const { OpencodeEngine } = require('./engine/opencode');`
(tìm dòng import hiện có), thêm:

```js
const { ClaudeEngine } = require('./engine/claude');
const { execFile } = require('node:child_process');
```

Thêm hàm mới, đặt cạnh `aliceDirFor`:

```js
/** Engine ĐÚNG cho một Alice, theo `provider` của nó. */
function engineFor(alice, settings) {
  return alice.provider === 'claude' ? new ClaudeEngine(settings) : new OpencodeEngine(settings);
}
```

- [ ] **Step 2: Sửa `activateAlice()` để chọn đúng engine**

Tìm `async function activateAlice(id)` (khoảng dòng 204). Sửa dòng
`engine.setBaseDir(base);` thành:

```js
async function activateAlice(id) {
  const alice = registry.alices.find((a) => a.id === id);
  if (!alice) throw new Error('Không tìm thấy Alice.');

  if (scheduler) scheduler.stop();
  if (brain) brain.stop();
  if (store) { store.close(); store = null; }
  scheduler = null;
  brain = null;
  memory = null;
  runTurn = null;

  registry.active = id;
  registryModule.save(registry);

  const base = aliceDirFor(id);
  engine = engineFor(alice, settings);           // MỖI Alice engine RIÊNG — provider
                                                    // khác nhau không lẫn state (đặc
                                                    // biệt `_cancelled`/`_child`).
  engine.setBaseDir(base);
  engine.settings = { ...settings, model: alice.model || null };
  ...
```

`engine` hiện là biến module-scope (`let engine`) — kiểm tra khai báo ở đầu file, KHÔNG
đổi kiểu khai báo, chỉ đổi chỗ này từ `engine.setBaseDir(base)` (dùng chung MỘT instance
`OpencodeEngine` toàn app) thành gán MỚI mỗi lần `activateAlice` (mỗi Alice một engine
instance riêng, đúng provider của nó). Rà toàn bộ file tìm chỗ nào tạo `engine = new
OpencodeEngine(...)` lúc boot (đầu file, ngoài `activateAlice`) — vẫn giữ để có instance
mặc định trước khi có Alice nào active (dùng cho `alice:auth:test`, `alice:models` khi
`registry.active` null).

- [ ] **Step 3: Sửa `publicServerFor()` để dùng engine đúng provider**

Tìm `function publicServerFor(id)` (Task hôm trước đã sửa, giờ sửa tiếp): thay
`engine,` (đang dùng biến module-scope chung) thành:

```js
function publicServerFor(id) {
  let pub = publicServers.get(id);
  if (!pub) {
    const alice = registry.alices.find((a) => a.id === id);
    if (!alice) throw new Error('Không tìm thấy Alice.');
    const base = aliceDirFor(id);
    const bs = new BrainSidecar(settings.brain || {}, { dataDir: path.join(base, 'brain') });
    const pubEngine = engineFor(alice, settings);   // engine RIÊNG của máy chủ public,
    pubEngine.setBaseDir(base);                     // độc lập với engine của cửa sổ app
    pubEngine.settings = { ...settings, model: alice.model || null };
    pub = new PublicServer({
      alice,
      baseDir: base,
      settings,
      engine: pubEngine,
      ...
```

(Giữ nguyên phần còn lại — `brainMcp`, `log`, `avatar`, `onMessage` — chỉ đổi `engine,`
thành `engine: pubEngine,` và thêm hai dòng khởi tạo `pubEngine` phía trên.)

- [ ] **Step 4: Thêm IPC đổi provider**

Cạnh `ipcMain.handle('alice:alice:set-model', ...)` (khoảng dòng 429), thêm:

```js
ipcMain.handle('alice:alice:set-provider', async (_e, id, provider) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  const providerT = provider === 'claude' ? 'claude' : 'opencode';
  const { state, updated } = registryModule.update(id, { provider: providerT });
  if (!updated) return { error: 'Không tìm thấy Alice.' };
  registry = state;
  if (registry.active === id) await activateAlice(id); // đổi engine ngay, không đợi restart
  return { ok: true };
});

/** Trạng thái đăng nhập Claude của MỘT Alice — đọc qua `claude auth status` với
 * `CLAUDE_CONFIG_DIR` cô lập của chính Alice đó, không đụng đăng nhập global. */
ipcMain.handle('alice:claude:status', async (_e, id) => {
  const alice = registry.alices.find((a) => a.id === id);
  if (!alice) return { error: 'Không tìm thấy Alice.' };
  const base = aliceDirFor(id);
  const configDir = registryModule.claudeConfigDir(base);
  return new Promise((resolve) => {
    execFile('claude', ['auth', 'status'], {
      env: { ...process.env, CLAUDE_CONFIG_DIR: configDir }, timeout: 10000,
    }, (err, stdout) => {
      if (err) { resolve({ loggedIn: false, error: 'Chưa cài `claude` hoặc chưa đăng nhập.' }); return; }
      try { resolve(JSON.parse(stdout)); } catch { resolve({ loggedIn: false }); }
    });
  });
});
```

Trong `registryModule.update()` (`src/main/registry.js`), kiểm tra field `provider` đã
được cho phép patch chưa — nếu `update()` hiện chỉ nhận `patch.name`/`patch.model`
(xem hàm `update` đã đọc ở phần trước), THÊM:

```js
function update(id, patch, paths = {}) {
  const state = load(paths);
  const alice = state.alices.find((a) => a.id === id);
  if (!alice) return { state, updated: false };
  if (patch.name !== undefined) alice.name = String(patch.name).trim() || alice.name;
  if (patch.model !== undefined) alice.model = patch.model || null;
  if (patch.provider !== undefined) alice.provider = patch.provider === 'claude' ? 'claude' : 'opencode';
  save(state, paths);
  return { state, updated: true };
}
```

- [ ] **Step 5: Thêm expose IPC trong `preload.js`**

Trong `src/main/preload.js`, cạnh `aliceSetModel`, thêm:

```js
  aliceSetProvider: (id, provider) => ipcRenderer.invoke('alice:alice:set-provider', id, provider),
  claudeStatus: (id) => ipcRenderer.invoke('alice:claude:status', id),
```

- [ ] **Step 6: Chạy toàn bộ suite**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1
```
Expected: PASS toàn bộ — `boot-smoke.test.js` đặc biệt quan trọng ở bước này (đảm bảo
`activateAlice`/`publicServerFor` sửa xong app vẫn boot được).

- [ ] **Step 7: Commit**

```bash
git add src/main/main.js src/main/preload.js src/main/registry.js
git commit -m "feat: main.js chọn engine theo provider của từng Alice, IPC đổi provider"
```

---

### Task 6: UI — chọn Provider trong màn Tạo Alice + Cài đặt

**Files:**
- Modify: `src/renderer/app.js`

**Interfaces:**
- Consumes: `window.alice.aliceCreate({..., provider})`, `window.alice.aliceSetProvider`,
  `window.alice.claudeStatus` (Task 5).
- Produces: không có task nào sau phụ thuộc — task cuối của plan.

- [ ] **Step 1: Thêm stub cho chế độ xem thử**

Trong khối `if (!window.alice) { ... window.alice = {...} }` (đầu file), thêm hai dòng
cạnh `testApiKey`:

```js
    aliceSetProvider: async () => ({ ok: true }),
    claudeStatus: async () => ({ loggedIn: false }),
```

- [ ] **Step 2: Thêm chọn Provider vào màn Tạo Alice**

Trong `openCreateAlice()`, tìm khối HTML (`openSheet('Tạo Alice mới', ...)`), thêm MỘT
field mới ngay sau field "Tên của Alice", TRƯỚC field "Chìa khoá":

```html
    <div class="field">
      <label for="ca-provider">Chạy bằng</label>
      <select id="ca-provider">
        <option value="opencode">opencode (chìa khoá API riêng)</option>
        <option value="claude">Claude Code (subscription, đăng nhập bằng claude login)</option>
      </select>
      <div class="desc" id="ca-provider-desc">opencode: dán API key ở dưới. Claude: không cần key,
        chạy <code>claude login</code> trong terminal SAU KHI tạo Alice — app sẽ cho lệnh chính xác.</div>
    </div>
```

Sửa khối ẩn/hiện field chìa khoá + model theo provider — thêm listener ngay sau
`$('ca-name').addEventListener('input', syncCreateBtn);`:

```js
  $('ca-provider').addEventListener('change', () => {
    const isClaude = $('ca-provider').value === 'claude';
    $('ca-key').closest('.field').hidden = isClaude;
    $('ca-model').closest('.field').hidden = isClaude;
    keyOk = isClaude; // Claude không cần "Kiểm tra" — không có API key để thử
    syncCreateBtn();
  });
```

Sửa `saveBtn.onclick` — truyền `provider` khi gọi `aliceCreate`:

```js
  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = 'Đang tạo…';
    $('ca-msg').textContent = 'Đang dựng thư mục và trí nhớ cho Alice…';
    try {
      const provider = $('ca-provider').value;
      const r = await window.alice.aliceCreate({
        name: $('ca-name').value,
        key: provider === 'claude' ? '' : $('ca-key').value,
        model: provider === 'claude' ? null : ($('ca-model').value || null),
        dir: $('ca-dir').value || null,
        provider,
      });
      if (r.error) { $('ca-msg').textContent = r.error; return; }
      closeSheet();
      showChat();
      await refreshHeader();
      await loadHistory();
      if (provider === 'claude' && r.alice) {
        const dir = r.alice.dir || '(thư mục mặc định của Alice)';
        alert(`Alice tạo xong nhưng CHƯA đăng nhập Claude.\n\nMở terminal, chạy đúng lệnh này:\n\n` +
          `CLAUDE_CONFIG_DIR="${dir}\\claude-config" claude login\n\n` +
          `Đăng nhập xong quay lại app, vào Cài đặt để kiểm tra trạng thái.`);
      }
    } catch (err) {
      $('ca-msg').textContent = `Không tạo được: ${String(err && err.message || err)}`;
    } finally {
      saveBtn.textContent = 'Tạo';
      syncCreateBtn();
    }
  };
```

Cập nhật `syncCreateBtn` — nút "Tạo" không được đòi `keyOk`/model khi đang ở provider
`claude` (đã set `keyOk = true` giả trong listener trên, nhưng field model cũng cần bỏ
qua điều kiện `$('ca-model').value`):

```js
  const syncCreateBtn = () => {
    const isClaude = $('ca-provider') && $('ca-provider').value === 'claude';
    const ready = isClaude
      ? Boolean($('ca-name').value.trim())
      : (keyOk && $('ca-model').value && $('ca-name').value.trim());
    saveBtn.disabled = !ready;
    saveBtn.title = ready ? 'Tạo Alice' : 'Nhập tên, kiểm tra chìa khoá và chọn model trước đã';
  };
```

- [ ] **Step 3: Thêm hiện trạng thái Claude trong Cài đặt**

Trong `openSettings()`, tìm khối field "Chìa khoá (API key)". Bọc nó trong điều kiện, và
thêm khối Claude song song — sửa phần build HTML của `openSheet('Cài đặt', ...)`:

```js
async function openSettings() {
  const [status, av] = await Promise.all([window.alice.status(), window.alice.getAvatar()]);
  status.auth = status.auth || { configured: false, providers: [] };
  const currentModel = status.model || null;
  const provider = status.provider || 'opencode'; // `alice:status` cần trả field này —
                                                    // xem ghi chú cuối bước này

  const authFieldHtml = provider === 'claude'
    ? `<div class="field">
        <label>Đăng nhập Claude</label>
        <div class="desc" id="s-claude-status">Đang kiểm tra…</div>
      </div>`
    : `<div class="field">
        <label for="f-key">Chìa khoá (API key) của Alice này</label>
        <input id="f-key" type="password" placeholder="${status.auth.configured ? 'đã có key — gõ vào đây để thay' : 'dán key vào đây'}" autocomplete="off">
        <div class="desc">
          ${status.auth.configured
            ? `Đang dùng: <b>${status.auth.providers.map(escapeHtml).join(', ')}</b>.`
            : 'Chưa có chìa khoá. Alice chỉ chạy với chìa khoá dán vào ô trên.'}
        </div>
      </div>`;

  openSheet('Cài đặt', 'Đổi ảnh, chọn model, quản lý lịch hẹn — tất cả là của riêng Alice này.', `
    ... (giữ nguyên phần ảnh) ...
    <div class="field">
      <label for="f-model">Model của Alice này</label>
      <select id="f-model"><option value="">(đang tải danh sách model…)</option></select>
      <div class="desc" id="f-model-desc">Để trống, Alice tự chọn model tốt nhất còn dùng được.</div>
    </div>
    ${authFieldHtml}
    ... (giữ nguyên phần Lịch hẹn + Cuộc trò chuyện) ...
  `, async () => {
    if (provider === 'opencode') {
      const key = $('f-key').value.trim();
      if (key) {
        const p = ($('f-model').value.split('/')[0]) || 'opencode';
        await window.alice.setApiKey(p, key);
      }
    }
    const model = $('f-model').value || null;
    if (model !== currentModel) {
      await window.alice.aliceSetModel(status.active, model);
    }
    await refreshHeader();
  });

  loadModelsInto($('f-model'), $('f-model-desc'), currentModel);
  if (provider === 'claude') {
    window.alice.claudeStatus(status.active).then((st) => {
      const el = $('s-claude-status');
      if (!el) return;
      el.innerHTML = st.loggedIn
        ? `Đã đăng nhập: <b>${escapeHtml(st.email || '')}</b> (${escapeHtml(st.subscriptionType || '')})`
        : `Chưa đăng nhập. Chạy <code>claude login</code> với đúng <code>CLAUDE_CONFIG_DIR</code> của Alice này (xem lúc tạo Alice).`;
    });
  }
  ... (giữ nguyên phần còn lại của hàm) ...
```

**Ghi chú bắt buộc:** `ipcMain.handle('alice:status', ...)` (trong `main.js`, chỗ đã đọc
ở Task 5 Step 6 để kiểm chứng boot) hiện KHÔNG trả field `provider`. Thêm dòng
`provider: (registry.alices.find((a) => a.id === registry.active) || {}).provider ||
'opencode',` vào object trả về của handler đó (tìm khối `return { root: ..., dataDir:
..., ... }` trong `alice:status`).

- [ ] **Step 4: Kiểm tra bằng tay (không có test tự động cho renderer)**

Renderer không có unit test (xem `test/` — không có file test cho `app.js`). Xác nhận
bằng `npm start`: mở màn Tạo Alice, chọn Claude, xác nhận field key/model ẩn đi, tạo
xong hiện đúng lệnh `claude login`. Đây là bước bắt buộc trước khi coi Task 6 xong.

- [ ] **Step 5: Chạy toàn bộ suite (đảm bảo sửa `alice:status` không phá gì)**

```bash
powershell -ExecutionPolicy Bypass -File scripts/run-tests.ps1
```
Expected: PASS toàn bộ.

- [ ] **Step 6: Commit**

```bash
git add src/renderer/app.js src/main/main.js
git commit -m "feat: UI chọn Provider (opencode/claude) lúc tạo Alice + xem trạng thái đăng nhập Claude"
```

---

## Sau khi xong cả 6 task

- [ ] Restart app dev (`npm start`) — theo quy tắc đã thống nhất trong phiên, LUÔN restart
  sau khi vá xong để Bệ hạ test ngay.
- [ ] Test tay đầu-cuối MỘT lần với `claude` thật: tạo một Alice provider `claude`, chạy
  `claude login` với đúng `CLAUDE_CONFIG_DIR`, chat thử, xác nhận Cài đặt hiện đúng email
  đã đăng nhập, và KHÔNG phải logout ở máy/trình duyệt khác.
- [ ] Báo Bệ hạ: việc 2 (typing đồng bộ) và việc 3 (@mention) VẪN CHƯA làm — xếp riêng
  theo đúng thứ tự đã thống nhất đầu phiên.
