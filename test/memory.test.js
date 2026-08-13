'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { Store } = require('../src/main/memory/store');
const { Memory } = require('../src/main/memory/memory');
const { createTurnRunner, occupiedWindow } = require('../src/main/turn');
const { SESSION_RE } = require('../src/main/engine/opencode');

function tmpDb() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-test-'));
  return path.join(dir, 'alice.db');
}

function baseSettings(over = {}) {
  return {
    contextCeiling: 1000,
    windowRatio: 0.6,   // cửa sổ 600
    compactRatio: 0.8,  // xoay khi chạm 480
    keepVerbatim: 4,
    rotateDaily: true,
    modelPreference: ['fake/model'],
    ...over,
  };
}

/**
 * Engine giả — ghi lại MỌI lệnh nó nhận, để test khẳng định được điều mà nhìn UI
 * không bao giờ thấy: lượt sau có thật sự đi vào ĐÚNG session của lượt trước không.
 */
function fakeEngine({ tokensPerTurn = [] } = {}) {
  const calls = [];
  let n = 0;
  let session = null;
  return {
    calls,
    async runWithFallback(opts) {
      calls.push({ ...opts });
      if (opts.sessionId && !SESSION_RE.test(opts.sessionId)) {
        throw new Error(`Session not found: ${opts.sessionId}`);
      }
      session = opts.sessionId || `ses_fake${String(n).padStart(4, '0')}AAAA`;
      const tokens = { input: tokensPerTurn[n] ?? 10, output: 1, total: 11 };
      n += 1;
      return { sessionId: session, text: `trả lời ${n}`, tokens, model: 'fake/model', attempts: [] };
    },
  };
}

test('lượt thứ HAI đi vào đúng session của lượt đầu — không tạo session mới', async () => {
  const store = new Store(tmpDb());
  const settings = baseSettings();
  const memory = new Memory(store, settings);
  const engine = fakeEngine();
  const run = createTurnRunner({ store, memory, engine, workDir: '.', settings });

  const t1 = await run('Ghi nhớ số 4271');
  const t2 = await run('Số tôi vừa bảo là số nào?');

  assert.equal(engine.calls[0].sessionId, null, 'lượt 1 phải tạo session mới');
  assert.match(t1.engineSession, SESSION_RE);
  assert.equal(
    engine.calls[1].sessionId,
    t1.engineSession,
    'lượt 2 PHẢI nối tiếp session lượt 1 — đây chính là ca "quên từ lượt thứ HAI mà vẫn trả lời trơn"'
  );
  assert.equal(t2.engineSession, t1.engineSession);
  store.close();
});

test('không bao giờ đưa id ngoài (uuid) vào --session của opencode', async () => {
  const store = new Store(tmpDb());
  const settings = baseSettings();
  const memory = new Memory(store, settings);
  const engine = fakeEngine();
  const run = createTurnRunner({ store, memory, engine, workDir: '.', settings });

  await run('lượt một');
  await run('lượt hai');

  for (const c of engine.calls) {
    if (c.sessionId !== null) {
      assert.match(c.sessionId, SESSION_RE,
        'id hội thoại của app (uuid) và session engine (ses_…) là hai thứ khác nhau — D-0055');
    }
  }
  // và id hội thoại của app đúng là uuid, tức là hai không gian id thật sự khác nhau
  const conv = store.currentConversation();
  assert.match(conv.id, /^[0-9a-f]{8}-[0-9a-f]{4}-/);
  assert.notEqual(conv.id, conv.engine_session);
  store.close();
});

test('chạm ngưỡng → xoay session, và MỒI mang theo sự việc của lượt trước', async () => {
  const store = new Store(tmpDb());
  const settings = baseSettings();
  const memory = new Memory(store, settings);
  // lượt 1 nhẹ, lượt 2 chạm 480 → xoay sau lượt 2
  const engine = fakeEngine({ tokensPerTurn: [100, 500, 100] });
  const run = createTurnRunner({ store, memory, engine, workDir: '.', settings });

  await run('Mật khẩu wifi nhà là ALICE-4271');
  const t2 = await run('cảm ơn nhé');
  assert.ok(t2.rotated, 'chạm ngưỡng thì phải xoay');
  assert.equal(t2.rotated.reason, 'threshold');

  const t3 = await run('wifi nhà mật khẩu gì ấy nhỉ?');
  assert.ok(t3.seeded, 'lượt sau khi xoay phải được nạp mồi');
  assert.equal(engine.calls[2].sessionId, null, 'mồi phải đi vào session MỚI, không phải session cũ');

  const seeded = engine.calls[2].message;
  assert.match(seeded, /TIẾP NỐI HỘI THOẠI/);
  assert.match(seeded, /ALICE-4271/,
    'mồi rỗng chính là ca quên-mà-không-ai-thấy; sự việc cũ phải có mặt trong mồi');
  assert.match(seeded, /wifi nhà mật khẩu gì ấy nhỉ\?$/, 'tin thật của người dùng nằm ở cuối mồi');
  store.close();
});

test('sang ngày mới → xoay và mang bản compact cũ theo', async () => {
  const store = new Store(tmpDb());
  const settings = baseSettings();
  const memory = new Memory(store, settings);
  const engine = fakeEngine();
  const run = createTurnRunner({ store, memory, engine, workDir: '.', settings });

  await run('hôm nay Bệ hạ chốt: dùng OpenCode làm engine');

  // giả lập sang ngày mới bằng cách sửa `day` của hội thoại đang mở
  const conv = store.currentConversation();
  store.db.prepare('UPDATE conversations SET day = ? WHERE id = ?').run('2000-01-01', conv.id);

  await run('chào buổi sáng');
  assert.equal(engine.calls[1].sessionId, null, 'ngày mới phải là session mới');
  assert.match(engine.calls[1].message, /OpenCode làm engine/,
    'session mới phải mang bản compact của ngày cũ (D-0054 mục 4)');
  store.close();
});

test('tin nguyên văn được lưu KHÔNG kèm mồi', async () => {
  const store = new Store(tmpDb());
  const settings = baseSettings();
  const memory = new Memory(store, settings);
  const engine = fakeEngine({ tokensPerTurn: [500, 10] });
  const run = createTurnRunner({ store, memory, engine, workDir: '.', settings });

  await run('tin đầu tiên');
  await run('tin thứ hai');

  const rows = store.db.prepare("SELECT text FROM messages WHERE role = 'human' ORDER BY id").all();
  assert.deepEqual(rows.map((r) => r.text), ['tin đầu tiên', 'tin thứ hai'],
    'mồi trộn vào lịch sử thì lần nén sau sẽ nén cả mồi — lỗi tích luỹ');
  store.close();
});

test('FTS5 tìm không dấu', async () => {
  const store = new Store(tmpDb());
  const conv = store.createConversation({ id: 'c1', day: '2026-08-12' });
  store.add({ convId: conv.id, role: 'human', text: 'nhà hàng nướng ở quận Bình Thạnh' });

  const hits = store.search('nha hang');
  assert.equal(hits.length, 1, 'gõ không dấu phải tìm ra chữ có dấu (unicode61 remove_diacritics 2)');
  store.close();
});

test('độ đầy cửa sổ tính CẢ phần cache, không chỉ input', async () => {
  // Ca thật đo được: prompt-cache đẩy lịch sử cũ sang `cache.read`, nên `input` của
  // lượt sau nhỏ hơn lượt đầu dù hội thoại dài ra. Chỉ nhìn `input` thì ngưỡng nén
  // không bao giờ chạm — app tưởng cửa sổ co lại trong khi nó đang phình.
  const turn1 = { input: 8048, cache: { read: 0, write: 0 } };
  const turn2 = { input: 6131, cache: { read: 1920, write: 0 } };
  assert.ok(occupiedWindow(turn2) > occupiedWindow(turn1),
    `phải thấy cửa sổ phình: ${occupiedWindow(turn1)} → ${occupiedWindow(turn2)}`);
  assert.equal(occupiedWindow(null), null, 'không có số đo thì trả null, đừng bịa 0');
  assert.equal(occupiedWindow({ input: 100 }), 100, 'thiếu khối cache vẫn phải chạy');
});

test('lastTokens trả số ĐO ĐƯỢC của lượt gần nhất, không phải ước lượng', async () => {
  const store = new Store(tmpDb());
  const conv = store.createConversation({ id: 'c1', day: '2026-08-12' });
  store.add({ convId: conv.id, role: 'human', text: 'hỏi' });
  store.add({ convId: conv.id, role: 'alice', text: 'đáp', tokensInput: 12345 });
  assert.equal(store.lastTokens(conv.id), 12345);
  store.close();
});

test('lastMessageTs: trả ts của tin GẦN NHẤT trong hội thoại, null nếu chưa có tin nào', () => {
  const store = new Store(tmpDb());
  const conv = store.createConversation({ id: 'c1', day: '2026-08-13' });
  assert.equal(store.lastMessageTs(conv.id), null);
  store.add({ convId: conv.id, role: 'human', text: 'một', ts: 1000 });
  store.add({ convId: conv.id, role: 'alice', text: 'hai', ts: 2000 });
  assert.equal(store.lastMessageTs(conv.id), 2000);
  store.close();
});

// ── rotation theo tuổi + im lặng (2026-08-13) ────────────────────────────────
//
// Bệ hạ chốt: xoay session khi tuổi > 12h VÀ im lặng kể từ tin cuối > 1h — AND,
// không phải OR: một session 13 tiếng tuổi mà vẫn đang nhắn liên tục KHÔNG xoay.
// Cộng thêm vào rotation cũ (hết ngày / tràn context), không thay thế.

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
  const store = new Store(tmpDb());
  const memory = memoryWith(store);
  const base = new Date('2026-08-13T00:00:00Z');
  await memory.ensureConversation(base);
  const conv = store.currentConversation();
  store.add({ convId: conv.id, role: 'human', text: 'chào', ts: base.getTime() });

  const later = new Date(base.getTime() + TWELVE_HOURS + ONE_HOUR);
  store.add({
    convId: conv.id, role: 'human', text: 'vẫn đang nói',
    ts: later.getTime() - 5 * 60 * 1000,
  });

  const { conversation } = await memory.ensureConversation(later);
  assert.equal(conversation.id, conv.id, 'tuổi một mình không đủ — phải còn idle>1h nữa mới xoay');
  store.close();
});

test('rotation: tuổi > 12h VÀ im lặng > 1h → xoay', async () => {
  const store = new Store(tmpDb());
  const memory = memoryWith(store);
  const base = new Date('2026-08-13T00:00:00Z');
  await memory.ensureConversation(base);
  const conv = store.currentConversation();
  store.add({ convId: conv.id, role: 'human', text: 'chào', ts: base.getTime() });

  const later = new Date(base.getTime() + TWELVE_HOURS + 2 * ONE_HOUR);
  const { conversation, seed } = await memory.ensureConversation(later);
  assert.notEqual(conversation.id, conv.id, 'phải xoay sang hội thoại mới');
  assert.ok(seed, 'hội thoại mới phải có mồi tiếp nối');
  store.close();
});

test('rotation: im lặng > 1h NHƯNG session chưa tới 12h tuổi → KHÔNG xoay', async () => {
  const store = new Store(tmpDb());
  const memory = memoryWith(store);
  const base = new Date('2026-08-13T00:00:00Z');
  await memory.ensureConversation(base);
  const conv = store.currentConversation();
  store.add({ convId: conv.id, role: 'human', text: 'chào', ts: base.getTime() });

  const later = new Date(base.getTime() + 2 * ONE_HOUR);
  const { conversation } = await memory.ensureConversation(later);
  assert.equal(conversation.id, conv.id);
  store.close();
});
