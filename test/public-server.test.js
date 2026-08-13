'use strict';

/**
 * Public Server — máy chủ của một Alice: trang web chat + ba mức cửa.
 * Test HTTP THẬT (node:http) với engine giả.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { PublicServer, hashPassword, MAX_BODY } = require('../src/main/public-server');
const { Store } = require('../src/main/memory/store');

function tmpBase() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-pub-'));
  return path.join(dir, 'alice');
}

function makeServer(baseDir, extra = {}) {
  fs.mkdirSync(baseDir, { recursive: true });
  const settings = { contextCeiling: 1000, windowRatio: 0.6, compactRatio: 0.8, keepVerbatim: 4, rotateDaily: true };
  const alice = { id: 'a1', name: 'Alice Test' };
  const engine = {
    setBaseDir() {},
    // Vọng lại nguyên câu gửi cho model — đó là cách duy nhất để test nhìn thấy
    // Alice thật sự nhận được NHỮNG GÌ (tên người nói, khối tin chưa đọc, mồi).
    runWithFallback: async (opts) => ({ text: `trả lời: ${opts.message}`, model: 'fake/model', attempts: [] }),
  };
  const server = new PublicServer({
    alice, baseDir, settings, engine, brainMcp: null,
    log: { info: () => {}, error: () => {} },
    ...extra,
  });
  server.saveConfig({ enabled: false, mode: 'anyone', port: 0, accounts: [] });
  return server;
}

/** Chờ Alice trả lời — câu trả lời tới qua SSE nên `/v1/chat` về trước nó. */
async function waitForAlice(port, n = 1, headers = {}) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    const { messages } = await (await fetch(`http://127.0.0.1:${port}/v1/history`, { headers })).json();
    if (messages.filter((m) => m.role === 'alice').length >= n) return messages;
    await new Promise((r) => setTimeout(r, 120));
  }
  throw new Error(`Alice không trả lời đủ ${n} lượt`);
}

async function freePort() {
  const net = require('node:net');
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => { const p = srv.address().port; srv.close(() => resolve(p)); });
    srv.on('error', reject);
  });
}

/**
 * Regression của lỗi đã dính thật (2026-08-13): chọn "ai có link cũng vào được"
 * mà quét QR trên điện thoại VẪN thấy màn đăng nhập.
 *
 * Nguyên nhân không nằm ở logic mà ở CSS: `.login { display:flex }` do trang tự
 * đặt THẮNG `display:none` mà trình duyệt gán cho `[hidden]`, nên `login.hidden =
 * true` trong JS không giấu được gì. Trang phải tự vô hiệu hoá cái bẫy đó.
 */
test('trang public: thuộc tính hidden phải thắng mọi class (không thì màn đăng nhập luôn hiện)', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'src', 'main', 'public-web', 'index.html'), 'utf8');
  assert.match(
    html,
    /\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}/,
    'phải có luật [hidden] { display: none !important }'
  );
  // Và luật đó phải đứng TRƯỚC class nào đặt display, không thì thứ tự lại thua.
  const guard = html.search(/\[hidden\]\s*\{\s*display:\s*none\s*!important/);
  const firstFlex = html.search(/\.(gate|feed|compose)\s*\{[^}]*display:\s*flex/);
  assert.ok(guard > -1 && guard < firstFlex, 'luật [hidden] phải nằm trước các class display:flex');
});

test('trang public: mọi ô nhập >= 16px và viewport khoá zoom (iOS không tự phóng lúc focus)', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'src', 'main', 'public-web', 'index.html'), 'utf8');
  assert.match(html, /maximum-scale=1/, 'viewport phải khoá zoom');
  assert.match(html, /user-scalable=no/);
  assert.match(html, /input,\s*textarea,\s*button,\s*select\s*\{\s*font-size:\s*16px/,
    'ô nhập phải 16px — nhỏ hơn là Safari tự phóng trang');
});

test('public server: mode account chưa có tài khoản thì từ chối mở máy chủ', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({ enabled: false, mode: 'account', port: 0, accounts: [] });
  await assert.rejects(() => server.start(0), /Chưa có tài khoản/);
  server.stop();
});

test('public server: mode anyone — không cần token, chat trả lời, có trang web', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);

  try {
    const page = await fetch(`http://127.0.0.1:${port}/`);
    assert.equal(page.status, 200);
    const html = await page.text();
    assert.match(html, /MODE = 'anyone'/, 'trang phải được chèn mode anyone');
    assert.match(html, /Chat với Alice/, 'trang phải là web chat');
    // Đổi mode xong mà điện thoại dựng lại bản cache là thấy đúng cửa vừa gỡ.
    assert.match(page.headers.get('cache-control') || '', /no-store/);

    // Gọi tên thì Alice trả lời. Câu trả lời KHÔNG nằm trong response của
    // `/v1/chat` nữa — nó tới qua SSE, để mọi máy trong phòng thấy cùng lúc.
    const ok = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '@alice hôm nay thế nào' }),
    });
    assert.equal(ok.status, 202, 'nhận việc rồi trả ngay, không giữ kết nối tới lúc model xong');
    assert.equal((await ok.json()).replied, true);

    const messages = await waitForAlice(port);
    // Model PHẢI nhận kèm tên người nói: nhiều người chung một Alice, không có tên
    // thì nó trả lời người này bằng ngữ cảnh của người kia mà không biết mình nhầm.
    assert.match(messages.find((m) => m.role === 'alice').text, /\[anonymous/);

    const who = await fetch(`http://127.0.0.1:${port}/v1/who`);
    assert.equal(who.status, 200);

    const empty = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
    });
    assert.equal(empty.status, 400);
  } finally {
    server.stop();
  }
});

test('public server: mode code — sai mã thì không vào, đúng mã thì chat được', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({ enabled: false, mode: 'code', port: 0, code: '13572468', accounts: [] });
  const port = await freePort();
  await server.start(port);

  try {
    const html = await (await fetch(`http://127.0.0.1:${port}/`)).text();
    assert.match(html, /MODE = 'code'/);

    const noAuth = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'chào' }),
    });
    assert.equal(noAuth.status, 401);

    const bad = await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: '00000000' }),
    });
    assert.equal(bad.status, 401);

    const good = await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: '13572468' }),
    });
    assert.equal(good.status, 200);
    const { token } = await good.json();

    const ok = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ message: 'xin chào' }),
    });
    assert.equal(ok.status, 200);
  } finally {
    server.stop();
  }
});

test('public server: đổi mã là đá hết phiên cũ ra', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({ enabled: false, mode: 'code', port: 0, code: '11112222', accounts: [] });
  const port = await freePort();
  await server.start(port);

  try {
    const { token } = await (await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: '11112222' }),
    })).json();
    const before = await fetch(`http://127.0.0.1:${port}/v1/check`, { headers: { Authorization: `Bearer ${token}` } });
    assert.equal(before.status, 200);

    const fresh = server.rotateCode();
    assert.notEqual(fresh, '11112222');
    assert.equal(fresh.length, 8);

    const after = await fetch(`http://127.0.0.1:${port}/v1/check`, { headers: { Authorization: `Bearer ${token}` } });
    assert.equal(after.status, 401, 'phiên cũ phải chết sau khi đổi mã');
  } finally {
    server.stop();
  }
});

test('public server: mode account — phải đăng nhập mới chat được', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({
    enabled: false, mode: 'account', port: 0,
    accounts: [{ username: 'nga', ...hashPassword('mat-khau-123') }],
  });
  const port = await freePort();
  await server.start(port);

  try {
    const html = await (await fetch(`http://127.0.0.1:${port}/`)).text();
    assert.match(html, /MODE = 'account'/);

    const noAuth = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'chào' }),
    });
    assert.equal(noAuth.status, 401);

    const bad = await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'nga', password: 'sai-sai' }),
    });
    assert.equal(bad.status, 401);

    const login = await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'nga', password: 'mat-khau-123' }),
    });
    assert.equal(login.status, 200);
    const { token } = await login.json();
    assert.ok(token && token.length > 20, 'session token phải dài');

    const ok = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ message: '@alice xin chào' }),
    });
    assert.equal(ok.status, 202);
    const msgs = await waitForAlice(port, 1, { Authorization: `Bearer ${token}` });
    assert.equal(msgs.find((m) => m.role === 'alice').text, 'trả lời: [nga]: @alice xin chào',
      'model phải biết ai đang nói');

    const check = await fetch(`http://127.0.0.1:${port}/v1/check`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    assert.equal(check.status, 200);
  } finally {
    server.stop();
  }
});

test('public server: KHÔNG mở CORS cho website lạ (không thì trang bất kỳ đốt được API key của chủ máy)', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);
  try {
    const foreign = await fetch(`http://127.0.0.1:${port}/v1/who`, { headers: { Origin: 'https://ke-la.example' } });
    assert.equal(foreign.headers.get('access-control-allow-origin'), null, 'gốc lạ không được cấp CORS');

    const same = await fetch(`http://127.0.0.1:${port}/v1/who`, { headers: { Origin: `http://127.0.0.1:${port}` } });
    assert.equal(same.headers.get('access-control-allow-origin'), `http://127.0.0.1:${port}`);
    assert.equal(same.headers.get('x-content-type-options'), 'nosniff');
  } finally {
    server.stop();
  }
});

test('public server: thân request quá to bị chặn (không nuốt hết RAM)', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);
  try {
    const huge = JSON.stringify({ message: 'x'.repeat(MAX_BODY + 5000) });
    const r = await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: huge,
    }).catch((e) => ({ status: 413, thrown: e }));
    assert.ok(r.status === 413 || r.thrown, `phải bị từ chối, nhận ${r.status}`);
  } finally {
    server.stop();
  }
});

test('public server: đoán mã sai nhiều lần bị chặn tạm (chống dò mã)', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({ enabled: false, mode: 'code', port: 0, code: '87654321', accounts: [] });
  const port = await freePort();
  await server.start(port);
  try {
    let sawLimit = false;
    for (let i = 0; i < 14; i += 1) {
      const r = await fetch(`http://127.0.0.1:${port}/v1/login`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: '00000000' }),
      });
      if (r.status === 429) { sawLimit = true; break; }
    }
    assert.ok(sawLimit, 'phải chặn sau một số lần đoán sai');

    // Đã bị chặn thì mã ĐÚNG cũng không lọt — nếu không thì hạn mức là vô nghĩa.
    const right = await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: '87654321' }),
    });
    assert.equal(right.status, 429);
  } finally {
    server.stop();
  }
});

test('public server: mật khẩu KHÔNG lưu plaintext', () => {
  const { salt, hash } = hashPassword('mat-khau');
  assert.notEqual(hash, 'mat-khau', 'không được lưu mật khẩu trần');
  assert.ok(salt && salt.length >= 16);
  assert.equal(hash.length, 128, 'scrypt 64 byte → 128 hex');
  const { hash: h2 } = hashPassword('mat-khau');
  assert.notEqual(hash, h2, 'cùng mật khẩu, salt khác → hash khác');
});

// ── danh tính khách + lịch sử ────────────────────────────────────────────────

test('public server: mỗi khách một TÊN riêng, giữ nguyên qua reload', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);

  try {
    const join = async () => (await (await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })).json());

    const a = await join();
    const b = await join();
    assert.match(a.name, /^anonymous-[2-9a-z]{5}$/, `tên phải dạng anonymous-xxxxx, nhận ${a.name}`);
    assert.notEqual(a.name, b.name, 'hai khách khác nhau phải khác tên');

    // Reload trang = gọi /v1/check bằng token cũ → PHẢI trả lại đúng tên cũ, không
    // sinh tên mới (không thì lịch sử chat thành một đám người lạ mà chỉ là một người).
    const chk = await fetch(`http://127.0.0.1:${port}/v1/check`, {
      headers: { Authorization: `Bearer ${a.token}` },
    });
    assert.equal(chk.status, 200);
    assert.equal((await chk.json()).name, a.name);
  } finally {
    server.stop();
  }
});

test('public server: mode account — tên hiển thị là username, không phải anonymous', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({
    enabled: false, mode: 'account', port: 0,
    accounts: [{ username: 'nga', ...hashPassword('mat-khau-123') }],
  });
  const port = await freePort();
  await server.start(port);
  try {
    const d = await (await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'nga', password: 'mat-khau-123' }),
    })).json();
    assert.equal(d.name, 'nga');
  } finally {
    server.stop();
  }
});

test('public server: /v1/history trả lịch sử THẬT kèm tên người gửi', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);

  try {
    // Chưa nói gì → rỗng. Trang web hiện câu chào của chính nó, không bịa ra lịch sử.
    const empty = await (await fetch(`http://127.0.0.1:${port}/v1/history`)).json();
    assert.deepEqual(empty.messages, []);

    const { token, name } = await (await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })).json();

    await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ message: '@alice ai đang nói đây' }),
    });

    const messages = await waitForAlice(port);
    assert.equal(messages.length, 2, 'một tin của khách + một tin của Alice');
    assert.equal(messages[0].role, 'human');
    assert.equal(messages[0].text, '@alice ai đang nói đây');
    assert.equal(messages[0].who, name, 'tin của khách phải mang đúng tên khách');
    assert.equal(messages[1].role, 'alice');
    assert.equal(messages[1].who, null);
    // Không rò model / danh sách model hỏng ra cho khách.
    assert.equal(messages[1].meta, undefined);
  } finally {
    server.stop();
  }
});

test('public server: mode code — /v1/history cần vào cửa trước', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({ enabled: false, mode: 'code', port: 0, code: '55556666', accounts: [] });
  const port = await freePort();
  await server.start(port);
  try {
    const r = await fetch(`http://127.0.0.1:${port}/v1/history`);
    assert.equal(r.status, 401, 'chưa nhập mã thì không được đọc chuyện của người khác');
  } finally {
    server.stop();
  }
});

// ── phòng chat nhiều người: @alice + realtime ────────────────────────────────

/** Đọc `n` sự kiện SSE đầu tiên rồi đóng. */
function readEvents(port, n, tokenStr = null, afterOpen = null) {
  return new Promise((resolve, reject) => {
    const http = require('node:http');
    const q = tokenStr ? `?token=${encodeURIComponent(tokenStr)}` : '';
    const req = http.get(`http://127.0.0.1:${port}/v1/events${q}`, (res) => {
      if (res.statusCode !== 200) { reject(new Error(`SSE trả ${res.statusCode}`)); return; }
      const got = [];
      let buf = '';
      res.on('data', (c) => {
        buf += c.toString('utf8');
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, i);
          buf = buf.slice(i + 2);
          if (!frame.startsWith('data: ')) continue;   // heartbeat `: ping`
          got.push(JSON.parse(frame.slice(6)));
          if (got.length >= n) { req.destroy(); resolve(got); return; }
        }
      });
      res.on('error', () => {});
      if (afterOpen) setTimeout(afterOpen, 60);
    });
    req.on('error', (e) => { if (e.code !== 'ECONNRESET') reject(e); });
    setTimeout(() => { req.destroy(); reject(new Error('SSE không trả đủ sự kiện')); }, 20000);
  });
}

test('phòng chat: câu KHÔNG gọi @alice thì Alice im, nhưng vẫn lưu và vẫn phát cho cả phòng', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);

  try {
    const events = await readEvents(port, 2, null, () => {
      fetch(`http://127.0.0.1:${port}/v1/chat`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: 'mai họp lúc 9h nhé mọi người' }),
      });
    });

    assert.equal(events[0].type, 'hello', 'máy vừa vào phải biết ngay trạng thái');
    assert.equal(events[1].type, 'message');
    assert.equal(events[1].message.text, 'mai họp lúc 9h nhé mọi người');
    assert.ok(events[1].message.id > 0, 'tin phải có id — thứ tự cả phòng dựa vào nó');

    // Alice KHÔNG được trả lời.
    const { messages } = await (await fetch(`http://127.0.0.1:${port}/v1/history`)).json();
    assert.equal(messages.length, 1, `chỉ được có tin của khách, nhận: ${JSON.stringify(messages)}`);
    assert.equal(messages[0].role, 'human');
  } finally {
    server.stop();
  }
});

test('phòng chat: gọi @alice — tin người gửi phải phát TRƯỚC báo "đang trả lời", không phải sau', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);

  try {
    // Bug đã đo thật (2026-08-13): `busy:true` phát ngay khi vào lượt, còn tin của
    // người gửi chỉ phát SAU KHI Alice trả lời xong — nên trang web vẽ "Alice đang
    // trả lời…" ở TRÊN tin vừa gõ. Đúng thứ tự phải là: hello, tin người gửi, rồi
    // mới tới busy.
    const events = await readEvents(port, 3, null, () => {
      fetch(`http://127.0.0.1:${port}/v1/chat`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: '@alice ơi' }),
      });
    });

    assert.equal(events[0].type, 'hello');
    assert.equal(events[1].type, 'message', 'tin người gửi phải tới TRƯỚC busy');
    assert.equal(events[1].message.role, 'human');
    assert.equal(events[1].message.text, '@alice ơi');
    assert.equal(events[2].type, 'busy');
    assert.equal(events[2].busy, true);
  } finally {
    server.stop();
  }
});

test('phòng chat: gọi @alice thì Alice đọc LẠI hết những câu chưa đọc', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);

  try {
    const say = (message) => fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    });

    await say('tuần này mình chốt màu xanh nhé');
    await say('ok, mà ngân sách chỉ còn 3 triệu');
    await say('@alice tóm tắt giúp');

    // Engine giả trả lại 200 ký tự đầu của message → đọc được Alice nhận những gì.
    const deadline = Date.now() + 15000;
    let messages = [];
    while (Date.now() < deadline) {
      ({ messages } = await (await fetch(`http://127.0.0.1:${port}/v1/history`)).json());
      if (messages.some((m) => m.role === 'alice')) break;
      await new Promise((r) => setTimeout(r, 150));
    }
    const reply = messages.find((m) => m.role === 'alice');
    assert.ok(reply, `Alice phải trả lời khi được gọi. Có: ${JSON.stringify(messages)}`);
    assert.match(reply.text, /CHƯA ĐỌC/,
      'câu gửi cho model phải kèm khối tin chưa đọc — không thì Alice trả lời như vừa vào phòng');

    // Ba câu của khách + một câu trả lời, đúng thứ tự id.
    assert.equal(messages.length, 4);
    const ids = messages.map((m) => m.id);
    assert.deepEqual(ids, [...ids].sort((a, b) => a - b), 'lịch sử phải theo đúng thứ tự id');
  } finally {
    server.stop();
  }
});

test('phòng chat: gọi @alice lần hai KHÔNG đọc lại phần đã đọc', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);

  const say = (message) => fetch(`http://127.0.0.1:${port}/v1/chat`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  const waitReplies = async (n) => {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      const { messages } = await (await fetch(`http://127.0.0.1:${port}/v1/history`)).json();
      if (messages.filter((m) => m.role === 'alice').length >= n) return messages;
      await new Promise((r) => setTimeout(r, 150));
    }
    throw new Error(`không đủ ${n} câu trả lời`);
  };

  try {
    await say('câu nền một');
    await say('@alice lần một');
    await waitReplies(1);
    await say('@alice lần hai');
    const messages = await waitReplies(2);

    const second = messages.filter((m) => m.role === 'alice')[1];
    assert.doesNotMatch(second.text, /CHƯA ĐỌC/,
      'phần đã đưa vào ngữ cảnh rồi thì không được gộp lại lần nữa — nếu không mỗi lượt lại dài thêm');
  } finally {
    server.stop();
  }
});

test('phòng chat: /v1/history?since= chỉ trả phần còn thiếu (nối lại sau khi rớt mạng)', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);

  try {
    const say = (message) => fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    });
    await say('một');
    await say('hai');
    const all = (await (await fetch(`http://127.0.0.1:${port}/v1/history`)).json()).messages;
    assert.equal(all.length, 2);

    const rest = (await (await fetch(`http://127.0.0.1:${port}/v1/history?since=${all[0].id}`)).json()).messages;
    assert.equal(rest.length, 1, 'chỉ phần sau mốc');
    assert.equal(rest[0].id, all[1].id);

    const none = (await (await fetch(`http://127.0.0.1:${port}/v1/history?since=${all[1].id}`)).json()).messages;
    assert.deepEqual(none, [], 'không thiếu gì thì trả rỗng');
  } finally {
    server.stop();
  }
});

test('phòng chat: mode code — chưa vào cửa thì không mở được dòng sự kiện', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  server.saveConfig({ enabled: false, mode: 'code', port: 0, code: '90909090', accounts: [] });
  const port = await freePort();
  await server.start(port);
  try {
    const r = await fetch(`http://127.0.0.1:${port}/v1/events`);
    assert.equal(r.status, 401);

    const { token } = await (await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: '90909090' }),
    })).json();
    // EventSource không gắn được header → token đi qua query.
    const ok = await readEvents(port, 1, token);
    assert.equal(ok[0].type, 'hello');
  } finally {
    server.stop();
  }
});

// ── đồng bộ app desktop ⇄ trang public (2026-08-13) ─────────────────────────
//
// Bệ hạ báo: chat trong app không hiện trên điện thoại (trang public), và chat
// trên điện thoại không hiện trong app. Hai bên dùng CHUNG một `chat.db` (cùng
// `baseDir`) nhưng là hai `Store` khác nhau, nên bên nào tự ghi thì chỉ bên đó
// biết — không ai tự động báo cho bên kia để đẩy realtime.

test('public server: broadcastFromDesktop — tin app chat trong lúc public phải lên trang web ngay, không cần tải lại', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);

  try {
    // Cần một hội thoại có thật trước đã (như khi khách đã chat ít nhất một câu).
    await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'khách chào trước' }),
    });
    const conv = server.store.currentConversation();
    assert.ok(conv, 'phải có hội thoại để test broadcastFromDesktop');

    // App desktop có `Store` RIÊNG nhưng trỏ CÙNG file `chat.db` — đúng kiến trúc
    // thật (`activateAlice` và `publicServerFor` cùng dùng `path.join(base,
    // 'chat.db')`). Giả lập Bệ hạ vừa gõ trong app.
    const desktopStore = new Store(path.join(base, 'chat.db'));
    let newId;
    try {
      newId = desktopStore.add({ convId: conv.id, role: 'human', text: 'Bệ hạ gõ trong app', delivered: true });
    } finally {
      desktopStore.close();
    }

    const events = await readEvents(port, 2, null, () => {
      server.broadcastFromDesktop(conv.id, newId - 1);
    });
    assert.equal(events[0].type, 'hello');
    assert.equal(events[1].type, 'message');
    assert.equal(events[1].message.id, newId);
    assert.equal(events[1].message.text, 'Bệ hạ gõ trong app', 'trang public phải thấy ĐÚNG câu app vừa gõ');
  } finally {
    server.stop();
  }
});

test('public server: khách nhắn qua trang public thì onMessage báo cho app biết (để app vẽ ngay, không đợi đổi Alice qua lại)', async () => {
  const base = tmpBase();
  const seen = [];
  const server = makeServer(base, { onMessage: (row) => seen.push(row) });
  const port = await freePort();
  await server.start(port);

  try {
    // Không gọi @alice — vẫn phải báo: im lặng khác mù, app phải biết ngay cả
    // khi Alice chưa trả lời.
    await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'khách chào (không gọi alice)' }),
    });
    await new Promise((r) => setTimeout(r, 80));
    assert.equal(seen.length, 1);
    assert.equal(seen[0].role, 'human');
    assert.equal(seen[0].text, 'khách chào (không gọi alice)');

    // Gọi @alice — cả tin khách lẫn câu trả lời đều phải báo cho app.
    await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '@alice trả lời đi' }),
    });
    await waitForAlice(port);
    await new Promise((r) => setTimeout(r, 80));
    assert.equal(seen.length, 3, 'thêm đúng một tin khách + một tin Alice');
    assert.equal(seen[1].role, 'human');
    assert.equal(seen[2].role, 'alice');
  } finally {
    server.stop();
  }
});

// ── typing indicator: app phải biết Alice đang trả lời khách trên trang public
// (Bệ hạ báo 2026-08-13: mở public link, khách nhắn @alice, KHÔNG thấy typing ở
// cả app lẫn trang web, dù câu trả lời vẫn tới sau đó). Gốc rễ ở app: `onMessage`
// chỉ báo khi có TIN, không báo trạng thái "đang trả lời" — nên thêm `onBusy`.

test('public server: onBusy báo app biết Alice ĐANG trả lời khách trên trang public, rồi báo hết bận', async () => {
  const base = tmpBase();
  const seen = [];
  const server = makeServer(base, { onBusy: (busy, activity) => seen.push({ busy, activity }) });
  const port = await freePort();
  await server.start(port);

  try {
    // Không gọi @alice — không có lượt nào chạy, không được báo bận.
    await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'khách chào (không gọi alice)' }),
    });
    await new Promise((r) => setTimeout(r, 80));
    assert.equal(seen.length, 0, 'im lặng thì không có lượt nào để báo bận');

    // Gọi @alice — phải thấy busy:true TRƯỚC, rồi busy:false SAU khi xong.
    await fetch(`http://127.0.0.1:${port}/v1/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '@alice trả lời đi' }),
    });
    await waitForAlice(port);
    await new Promise((r) => setTimeout(r, 80));

    assert.ok(seen.length >= 2, `phải có ít nhất một cặp busy true/false, nhận: ${JSON.stringify(seen)}`);
    assert.equal(seen[0].busy, true, 'phải báo bận NGAY khi bắt đầu lượt');
    assert.equal(seen[seen.length - 1].busy, false, 'phải báo hết bận khi xong lượt');
  } finally {
    server.stop();
  }
});

// ── gợi ý @mention: trang public phải biết Alice này nhận những tên gọi nào ────

test('public server: /v1/who trả kèm handles để trang web gợi ý khi gõ @', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);
  try {
    const who = await (await fetch(`http://127.0.0.1:${port}/v1/who`)).json();
    assert.ok(Array.isArray(who.handles), 'phải có mảng handles');
    assert.ok(who.handles.includes('alice'), '"alice" luôn phải nhận được');
  } finally {
    server.stop();
  }
});

test('public server: /v1/who — Alice có tên riêng thì handles có thêm alias rút gọn', async () => {
  const base = tmpBase();
  fs.mkdirSync(base, { recursive: true });
  const settings = { contextCeiling: 1000, windowRatio: 0.6, compactRatio: 0.8, keepVerbatim: 4, rotateDaily: true };
  const alice = { id: 'a2', name: 'Alice K-OS' };
  const engine = { setBaseDir() {}, runWithFallback: async (opts) => ({ text: 'ok', model: 'fake/model', attempts: [] }) };
  const server = new PublicServer({ alice, baseDir: base, settings, engine, brainMcp: null, log: { info: () => {}, error: () => {} } });
  server.saveConfig({ enabled: false, mode: 'anyone', port: 0, accounts: [] });
  const port = await freePort();
  await server.start(port);
  try {
    const who = await (await fetch(`http://127.0.0.1:${port}/v1/who`)).json();
    assert.ok(who.handles.includes('alice'));
    assert.ok(who.handles.includes('kos'), `phải có alias rút gọn "kos", nhận: ${JSON.stringify(who.handles)}`);
  } finally {
    server.stop();
  }
});

// ── bao nhiêu người đã join ────────────────────────────────────────────────

test('public server: stats() đếm đúng người đang mở trang (online) và đã từng vào (joined)', async () => {
  const base = tmpBase();
  const server = makeServer(base);
  const port = await freePort();
  await server.start(port);
  try {
    assert.deepEqual(server.stats(), { online: 0, joined: 0 });

    await (await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })).json();
    await (await fetch(`http://127.0.0.1:${port}/v1/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })).json();
    assert.equal(server.stats().joined, 2, 'hai lượt /v1/login = hai người đã join');
    assert.equal(server.stats().online, 0, 'chưa ai mở kết nối SSE thì chưa tính "đang mở"');

    // Mở một kết nối SSE THẬT và giữ nó sống để đếm "đang mở" ngay trong lúc mở.
    const http = require('node:http');
    await new Promise((resolve, reject) => {
      const req = http.get(`http://127.0.0.1:${port}/v1/events`, (res) => {
        res.once('data', () => {
          assert.equal(server.stats().online, 1, 'đang có một kết nối SSE mở thì online phải là 1');
          req.destroy();
        });
      });
      req.on('close', resolve);
      req.on('error', (e) => { if (e.code === 'ECONNRESET') resolve(); else reject(e); });
    });
    await new Promise((r) => setTimeout(r, 50)); // để server xử lý sự kiện 'close' của response
    assert.equal(server.stats().online, 0, 'đóng kết nối rồi thì không còn tính là đang mở');
    assert.equal(server.stats().joined, 2, 'đóng kết nối KHÔNG xoá phiên đã join');
  } finally {
    server.stop();
  }
});
