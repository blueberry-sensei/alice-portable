'use strict';

/**
 * Bộ thu thập dữ liệu báo cáo tuần.
 *
 *   - `lastThursday`: mốc thời gian mặc định — sai một ngày là cả báo cáo sai lệch.
 *   - `gitLog`: commit thật của một repo tạm (git phải có trên máy — máy dev có).
 *   - `planeIssues` + `chatMessages`: chạy TRỌN VẸN qua server HTTP GIẢ ở localhost
 *     (token endpoint giả + Chat API giả), không đụng tới Google/Plane thật. Lượt
 *     JWT RS256 được ký THẬT bằng node:crypto và thẩm định lại bằng public key —
 *     đúng khâu dễ sai nhất của flow service account.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const http = require('node:http');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');

const { gitLog, planeIssues, chatMessages, serviceAccountToken } = require('../src/main/report/collector');
const { lastThursday } = require('../src/main/report/config');

// ── lastThursday ───────────────────────────────────────────────────────────

test('lastThursday: từ mọi ngày trong tuần đều ra Thứ 5 TUẦN TRƯỚC', () => {
  // Tuần 03/08–09/08/2026: Thứ 5 của tuần đó là 06/08 — TUẦN TRƯỚC nó là 30/07.
  const cases = [
    ['2026-08-09T12:00:00Z', '2026-07-30'], // CN
    ['2026-08-08T12:00:00Z', '2026-07-30'], // T7
    ['2026-08-06T12:00:00Z', '2026-07-30'], // T5 — đúng ngày Thứ 5 thì lùi đủ 7 ngày
    ['2026-08-04T12:00:00Z', '2026-07-30'], // T3
    ['2026-08-14T12:00:00Z', '2026-08-06'], // T6 tuần sau — vẫn ra Thứ 5 tuần trước đó
  ];
  for (const [now, want] of cases) {
    assert.equal(lastThursday(new Date(now)), want, `từ ${now} phải ra ${want}`);
  }
});

// ── gitLog ─────────────────────────────────────────────────────────────────

/**
 * Dọn thư mục tạm — KHÔNG để lỗi cleanup làm rớt một test đã assert đúng.
 * Windows: git.exe / Defender còn giữ handle vài trăm ms sau khi tiến trình đã
 * thoát (EPERM), đo thật trên CI lẫn máy dev — không phải bug của gitLog().
 */
function rmSyncSafe(dir) {
  try {
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 300 });
  } catch {
    // Temp dir của OS tự dọn sau — không phải việc test phải đảm bảo.
  }
}

function makeRepo() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-git-'));
  const run = (args, env = process.env) =>
    execFileSync('git', args, { cwd: dir, env, stdio: 'ignore', windowsHide: true });
  run(['init', '-q', '-b', 'main']);
  run(['config', 'user.email', 'test@alice.local']);
  run(['config', 'user.name', 'Tester']);
  const oldEnv = {
    ...process.env,
    GIT_AUTHOR_DATE: '2026-07-01T10:00:00+07:00',
    GIT_COMMITTER_DATE: '2026-07-01T10:00:00+07:00',
  };
  fs.writeFileSync(path.join(dir, 'a.txt'), 'one');
  run(['add', '.'], oldEnv);
  run(['commit', '-q', '-m', 'commit cũ — ngoài kỳ báo cáo'], oldEnv);
  fs.writeFileSync(path.join(dir, 'a.txt'), 'two');
  run(['add', '.']);
  run(['commit', '-q', '-m', 'commit mới — trong kỳ báo cáo']);
  return dir;
}

test('gitLog: chỉ trả commit từ mốc since, đủ hash/author/date/subject', () => {
  const repo = makeRepo();
  const { rows, error } = gitLog(repo, lastThursday(new Date('2026-08-14T12:00:00Z')));
  assert.equal(error, undefined);
  assert.equal(rows.length, 1, 'chỉ commit mới nằm trong kỳ');
  assert.equal(rows[0].subject, 'commit mới — trong kỳ báo cáo');
  assert.equal(rows[0].author, 'Tester');
  assert.match(rows[0].hash, /^[0-9a-f]{7,}$/);
  rmSyncSafe(repo);
});

test('gitLog: thư mục không phải repo thì trả lỗi, không throw', () => {
  const notRepo = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-git-x-'));
  const r1 = gitLog(notRepo, '2026-08-06');
  assert.match(r1.error, /Không phải repo git/);
  const r2 = gitLog(path.join(notRepo, 'khong-ton-tai'), '2026-08-06');
  assert.match(r2.error, /Không phải repo git/);
  rmSyncSafe(notRepo);
});

// ── Plane (mock) ───────────────────────────────────────────────────────────

function mockServer(handler) {
  return new Promise((resolve) => {
    const server = http.createServer(handler);
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

test('planeIssues: lọc theo updated_at >= since, lỗi HTTP thành lỗi đọc được', async () => {
  const server = await mockServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    if (u.pathname === '/api/v1/workspaces/w1/issues/' && u.searchParams.get('page') === '1') {
      if (req.headers['x-api-key'] !== 'key-123') {
        res.writeHead(401); res.end('{}'); return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        next_page_results: false,
        results: [
          { identifier: 'KOS-1', name: 'task cũ', state: { name: 'Done' }, priority: 'high',
            updated_at: '2026-07-01T10:00:00.000Z', assignees: [{ display_name: 'Minh' }] },
          { identifier: 'KOS-2', name: 'task mới', state: { name: 'In Progress' }, priority: 'urgent',
            updated_at: '2026-08-10T09:30:00.000Z', assignees: [] },
        ],
      }));
      return;
    }
    res.writeHead(500); res.end('{}');
  });
  const port = server.address().port;

  const ok = await planeIssues({ baseUrl: `http://127.0.0.1:${port}`, apiKey: 'key-123', workspace: 'w1', since: '2026-08-06' });
  assert.equal(ok.rows.length, 1, 'chỉ task cập nhật trong kỳ');
  assert.equal(ok.rows[0].identifier, 'KOS-2');
  assert.equal(ok.rows[0].state, 'In Progress');

  const bad = await planeIssues({ baseUrl: `http://127.0.0.1:${port}`, apiKey: 'sai', workspace: 'w1', since: '2026-08-06' });
  assert.match(bad.error, /HTTP 401/);

  const noKey = await planeIssues({ baseUrl: `http://127.0.0.1:${port}`, apiKey: '', workspace: 'w1', since: '2026-08-06' });
  assert.match(noKey.error, /Thiếu planeApiKey/);

  server.close();
});

// ── Google Chat (mock trọn flow SA) ───────────────────────────────────────

function makeSaCreds(tokenUri) {
  const { publicKey, privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
  return {
    creds: {
      client_email: 'sa-weekly-report@proj.iam.gserviceaccount.com',
      private_key: privateKey.export({ type: 'pkcs8', format: 'pem' }),
      token_uri: tokenUri,
    },
    publicKeyPem: publicKey.export({ type: 'spki', format: 'pem' }),
  };
}

function assertJwt(jwt, publicKeyPem, clientEmail) {
  const [h, p, sig] = jwt.split('.');
  const header = JSON.parse(Buffer.from(h, 'base64url').toString('utf8'));
  const payload = JSON.parse(Buffer.from(p, 'base64url').toString('utf8'));
  assert.equal(header.alg, 'RS256', 'phải ký RS256');
  assert.equal(payload.iss, clientEmail);
  assert.ok(payload.exp - payload.iat === 3600, 'token sống đúng 1 tiếng');
  const verify = crypto.createVerify('RSA-SHA256');
  verify.update(`${h}.${p}`);
  assert.ok(verify.verify(publicKeyPem, Buffer.from(sig, 'base64url')),
    'chữ ký phải khớp public key — nếu không Google sẽ trả 401');
}

test('chatMessages: tự ký JWT service account, đọc tin từ since, gửi đúng Bearer', async () => {
  let seenToken = false;
  let seenAuth = null;
  let seenFilter = null;
  const server = await mockServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    if (req.method === 'POST' && u.pathname === '/token') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        seenToken = true;
        const params = new URLSearchParams(body);
        assert.equal(params.get('grant_type'), 'urn:ietf:params:oauth:grant-type:jwt-bearer');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ access_token: 'tok-gia-123', expires_in: 3600 }));
      });
      return;
    }
    if (req.method === 'GET' && u.pathname === '/v1/spaces/AAAA123/messages') {
      seenAuth = req.headers.authorization;
      seenFilter = u.searchParams.get('filter');
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        messages: [
          { text: 'tin cũ', sender: { displayName: 'Minh' }, createTime: '2026-07-20T08:00:00Z' },
          { text: 'tin mới', sender: { displayName: 'Lan' }, createTime: '2026-08-12T09:15:00Z', thread: { name: 'spaces/x/threads/y' } },
        ],
      }));
      return;
    }
    res.writeHead(404); res.end('{}');
  });
  const port = server.address().port;

  const { creds, publicKeyPem } = makeSaCreds(`http://127.0.0.1:${port}/token`);
  const saFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'alice-sa-')), 'sa.json');
  fs.writeFileSync(saFile, JSON.stringify(creds));

  const { rows, error } = await chatMessages({
    credentialsPath: saFile,
    space: 'AAAA123',
    since: '2026-08-06',
    chatBaseUrl: `http://127.0.0.1:${port}`,
  });

  assert.equal(error, undefined);
  assert.ok(seenToken, 'phải gọi token endpoint');
  assert.equal(seenAuth, 'Bearer tok-gia-123', 'Chat API phải nhận đúng access_token');
  assert.match(seenFilter, /2026-08-06/, 'filter phải đúng mốc since');
  assert.equal(rows.length, 2);
  assert.equal(rows[0].author, 'Minh');
  assert.equal(rows[0].text, 'tin cũ');
  assert.equal(rows[1].thread, 'spaces/x/threads/y');
  assert.ok(rows[0].time < rows[1].time, 'sắp theo thời gian tăng dần');
  server.close();
});

test('serviceAccountToken: chữ ký JWT RS256 hợp lệ với public key', async () => {
  const { creds, publicKeyPem } = makeSaCreds(`http://127.0.0.1:0/token`);
  const server = await mockServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      const assertion = new URLSearchParams(body).get('assertion');
      assertJwt(assertion, publicKeyPem, 'sa-weekly-report@proj.iam.gserviceaccount.com');
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ access_token: 'jwt-token' }));
    });
  });
  const tok = await serviceAccountToken(
    { ...creds, token_uri: `http://127.0.0.1:${server.address().port}/token` },
    ['https://www.googleapis.com/auth/chat.messages.readonly']
  );
  assert.equal(tok.token, 'jwt-token');
  server.close();
});

test('chatMessages: thiếu file credentials thì lỗi rõ, không throw', async () => {
  const r = await chatMessages({
    credentialsPath: 'Z:\\khong-ton-tai\\sa.json',
    space: 'AAAA',
    since: '2026-08-06',
  });
  assert.match(r.error, /Không tìm thấy file credentials/);
});

test('chatMessages: file lạ (thiếu cả refresh_token lẫn private_key) → lỗi rõ', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-sa-bad-'));
  const f = path.join(dir, 'sa.json');
  fs.writeFileSync(f, JSON.stringify({ hello: 'world' }));
  const r = await chatMessages({ credentialsPath: f, space: 'AAAA', since: '2026-08-06' });
  assert.match(r.error, /thiếu cả refresh_token.*private_key/);
});

// ── Google Chat qua OAuth NGƯỜI DÙNG (project "personal account") ──────────

test('chatMessages: nhận diện file OAuth người dùng qua refresh_token, đọc tin bằng access_token làm mới', async () => {
  let seenRefresh = false;
  let seenAuth = null;
  const server = await mockServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    if (req.method === 'POST' && u.pathname === '/token') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        const params = new URLSearchParams(body);
        assert.equal(params.get('grant_type'), 'refresh_token');
        assert.equal(params.get('refresh_token'), 'rt-abc');
        assert.equal(params.get('client_id'), 'cid-123');
        seenRefresh = true;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ access_token: 'fresh-access-tok' }));
      });
      return;
    }
    if (req.method === 'GET' && u.pathname === '/v1/spaces/BBBB/messages') {
      seenAuth = req.headers.authorization;
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        messages: [{ text: 'tin của sếp', sender: { displayName: 'Boss' }, createTime: '2026-08-12T09:00:00Z' }],
      }));
      return;
    }
    res.writeHead(404); res.end('{}');
  });
  const port = server.address().port;

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-oauth-user-'));
  const credFile = path.join(dir, 'chat-user.json');
  fs.writeFileSync(credFile, JSON.stringify({
    type: 'user',
    client_id: 'cid-123',
    client_secret: 'secret-xyz',
    refresh_token: 'rt-abc',
    token_uri: `http://127.0.0.1:${port}/token`, // chỉ fixture test mới có field này
  }));

  const { rows, error } = await chatMessages({
    credentialsPath: credFile,
    space: 'BBBB',
    since: '2026-08-06',
    chatBaseUrl: `http://127.0.0.1:${port}`,
  });

  assert.equal(error, undefined);
  assert.ok(seenRefresh, 'phải gọi refresh_token grant');
  assert.equal(seenAuth, 'Bearer fresh-access-tok');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].author, 'Boss');
  server.close();
});

test('chatMessages: OAuth người dùng — refresh token hỏng thì lỗi rõ, không throw', async () => {
  const server = await mockServer((req, res) => {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'invalid_grant' }));
  });
  const port = server.address().port;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-oauth-user-bad-'));
  const credFile = path.join(dir, 'chat-user.json');
  fs.writeFileSync(credFile, JSON.stringify({
    client_id: 'cid', client_secret: 'sec', refresh_token: 'rt',
    token_uri: `http://127.0.0.1:${port}/token`,
  }));

  const r = await chatMessages({ credentialsPath: credFile, space: 'BBBB', since: '2026-08-06' });
  assert.match(r.error, /Làm mới token thất bại/);
  server.close();
});