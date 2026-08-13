'use strict';

/**
 * Public Server — biến một Alice thành MÁY CHỦ có TRANG WEB CHAT.
 *
 * Bệ hạ chốt (2026-08-13): "public là một bản website luôn — người khác quét mã
 * là vào chat được luôn." Ba mode, từ mở tới kín:
 *   - anyone  : ai có link/QR đều vào chat được, không hỏi gì. CHỈ dùng trong LAN;
 *   - code    : hỏi MỘT mã truy cập dùng chung — đủ để phát cho cả phòng mà vẫn
 *               không phải ai dò trúng URL cũng vào được;
 *   - account : mỗi người một username + password do chủ Alice tạo trước.
 *
 * Kiến trúc: mỗi Alice public có một worker RIÊNG — chat.db, session và trí nhớ
 * của chính nó, độc lập với Alice đang mở trên màn hình.
 *
 * Không dependency mới: node:http. Không HTTP framework.
 */

const http = require('node:http');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const { Store } = require('./memory/store');
const { Memory } = require('./memory/memory');
const { createTurnRunner } = require('./turn');
const { provisionWorkspace } = require('./alice');

const DEFAULT_PORT = 8931;

/** Trần thân request. Không có trần = một POST 2 GB là đủ giết app. */
const MAX_BODY = 256 * 1024;
/** Mỗi IP: bao nhiêu lượt chat trong bao lâu. */
const CHAT_LIMIT = { max: 30, windowMs: 60 * 1000 };
/** Mỗi IP: bao nhiêu lần đoán mã/mật khẩu trước khi bị chặn. */
const LOGIN_LIMIT = { max: 10, windowMs: 5 * 60 * 1000 };
/** Bao nhiêu người được xếp hàng chờ tới lượt trước khi máy chủ bảo "thử lại sau". */
const MAX_QUEUE = 4;
const SESSION_TTL_MS = 7 * 24 * 3600 * 1000;

const MODES = new Set(['anyone', 'code', 'account']);

/** Băm mật khẩu — KHÔNG bao giờ lưu plaintext (scrypt, salt riêng mỗi tài khoản). */
function hashPassword(password, salt = crypto.randomBytes(16).toString('hex')) {
  const hash = crypto.scryptSync(String(password), salt, 64).toString('hex');
  return { salt, hash };
}

function verifyPassword(password, account) {
  if (!account || !account.salt || !account.hash) return false;
  const { hash } = hashPassword(password, account.salt);
  const a = Buffer.from(hash, 'hex');
  const b = Buffer.from(account.hash, 'hex');
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

/** So sánh chuỗi KHÔNG rò rỉ độ dài khớp qua thời gian — dùng cho mã truy cập. */
function safeEqual(a, b) {
  const ha = crypto.createHash('sha256').update(String(a)).digest();
  const hb = crypto.createHash('sha256').update(String(b)).digest();
  return crypto.timingSafeEqual(ha, hb);
}

/** Mã truy cập 8 chữ số — đọc qua điện thoại được, mà 100 triệu khả năng. */
function newAccessCode() {
  return String(crypto.randomInt(0, 100000000)).padStart(8, '0');
}

/**
 * Tên cho khách vào bằng link/mã: `anonymous-<5 ký tự>`.
 *
 * Bỏ các ký tự dễ đọc nhầm khi nhìn qua màn hình điện thoại (0/O, 1/l/I) — tên này
 * hiện trong lịch sử chat và người ta sẽ đọc nó cho nhau nghe.
 */
const NANO_ALPHABET = '23456789abcdefghjkmnpqrstuvwxyz';
function anonName(size = 5) {
  let out = '';
  for (let i = 0; i < size; i += 1) {
    out += NANO_ALPHABET[crypto.randomInt(0, NANO_ALPHABET.length)];
  }
  return `anonymous-${out}`;
}

/**
 * @param {object} opts.alice       registry entry { id, name, provider }
 * @param {string} opts.baseDir     alices/<id>/
 * @param {object} opts.settings    settings chung của app
 * @param {object} opts.engine      OpencodeEngine (dùng chung, setBaseDir riêng)
 * @param {object} opts.brainMcp    cấu hình MCP brain của Alice này (hoặc null)
 * @param {object} opts.log         logger
 * @param {function} opts.avatar    () => data URI ảnh của Alice (tuỳ chọn)
 */
class PublicServer {
  constructor(opts) {
    this.alice = opts.alice;
    this.baseDir = opts.baseDir;
    this.settings = opts.settings;
    this.engine = opts.engine;
    this.brainMcp = opts.brainMcp;
    this.log = opts.log;
    this.avatar = opts.avatar || (() => null);
    this.store = null;
    this.runTurn = null;
    this.server = null;
    this.port = null;
    this.busy = false;
    this.queue = 0;              // số người đang chờ tới lượt
    this.lastError = null;
    this.sessions = new Map();   // sessionToken → { who, expires } (chỉ trong RAM)
    this.hits = new Map();       // ip → { chat: number[], login: number[] }
    this.webPage = null;
  }

  get running() {
    return Boolean(this.server && this.server.listening);
  }

  /** Cấu hình public của Alice: { enabled, mode, port, code, accounts }. */
  config() {
    try {
      const raw = JSON.parse(fs.readFileSync(path.join(this.baseDir, 'public.json'), 'utf8'));
      return {
        enabled: Boolean(raw.enabled),
        mode: MODES.has(raw.mode) ? raw.mode : 'anyone',
        port: raw.port || DEFAULT_PORT,
        code: typeof raw.code === 'string' && raw.code ? raw.code : null,
        accounts: Array.isArray(raw.accounts) ? raw.accounts : [],
      };
    } catch {
      return { enabled: false, mode: 'anyone', port: DEFAULT_PORT, code: null, accounts: [] };
    }
  }

  saveConfig(cfg) {
    fs.mkdirSync(this.baseDir, { recursive: true });
    fs.writeFileSync(path.join(this.baseDir, 'public.json'), JSON.stringify(cfg, null, 2), 'utf8');
  }

  /** Mã truy cập hiện có; chưa có thì sinh và lưu ngay (mode `code` cần nó). */
  ensureCode() {
    const cfg = this.config();
    if (cfg.code) return cfg.code;
    cfg.code = newAccessCode();
    this.saveConfig(cfg);
    return cfg.code;
  }

  /** Đổi mã truy cập — mọi phiên đang mở bị đá ra, đúng nghĩa "thu hồi". */
  rotateCode() {
    const cfg = this.config();
    cfg.code = newAccessCode();
    this.saveConfig(cfg);
    for (const [tok, s] of this.sessions) {
      if (s.kind === 'code') this.sessions.delete(tok);
    }
    return cfg.code;
  }

  /**
   * Mở server. Mode `anyone` mở được ngay; `account` cần ít nhất một tài khoản;
   * `code` tự sinh mã nếu chưa có (máy chủ không cửa là vô nghĩa).
   */
  async start(port) {
    if (this.running) return { ok: true };
    const cfg = this.config();
    if (cfg.mode === 'account' && !cfg.accounts.length) {
      throw new Error('Chưa có tài khoản nào — thêm username + password trước khi public.');
    }
    if (cfg.mode === 'code') this.ensureCode();

    if (!this.store) {
      this.store = new Store(path.join(this.baseDir, 'chat.db'));
      const memory = new Memory(this.store, this.settings);
      const workDir = path.join(this.baseDir, 'workspace');
      provisionWorkspace(this.settings, { brainMcp: this.brainMcp, dir: workDir });
      this.runTurn = createTurnRunner({ store: this.store, memory, engine: this.engine, workDir, settings: this.settings });
    }
    try {
      this.webPage = fs.readFileSync(path.join(__dirname, 'public-web', 'index.html'), 'utf8');
    } catch (err) {
      this.webPage = null;
      this.lastError = `thiếu trang web public: ${err.message}`;
    }

    this.port = Number(port) || DEFAULT_PORT;
    await new Promise((resolve, reject) => {
      this.server = http.createServer((req, res) => this._handle(req, res));
      this.server.on('error', (err) => {
        this.lastError = err.message;
        reject(err);
      });
      this.server.listen(this.port, '0.0.0.0', () => resolve());
    });
    this.log.info(`public server UP: ${this.alice.name} mode=${cfg.mode} on :${this.port}`);
    return { ok: true };
  }

  stop() {
    if (this.server) {
      try { this.server.close(); } catch { /* đã đóng */ }
      this.server = null;
    }
    if (this.store) {
      try { this.store.close(); } catch { /* đã đóng */ }
      this.store = null;
      this.runTurn = null;
    }
    this.sessions.clear();
    this.hits.clear();
    this.log.info(`public server DOWN: ${this.alice.name}`);
  }

  // ── HTTP ────────────────────────────────────────────────────────────────

  _handle(req, res) {
    const body = [];
    let size = 0;
    let killed = false;
    req.on('data', (c) => {
      if (killed) return;
      size += c.length;
      if (size > MAX_BODY) {
        killed = true;
        this._json(res, 413, { error: 'Tin nhắn quá dài.' });
        req.destroy();
        return;
      }
      body.push(c);
    });
    req.on('end', () => {
      if (killed) return;
      this._route(req, res, Buffer.concat(body));
    });
    req.on('error', () => { killed = true; });
  }

  /**
   * IP của NGƯỜI GỌI.
   *
   * Qua cloudflared, mọi request tới máy này đều từ 127.0.0.1 — đếm theo địa chỉ
   * socket là gộp cả thế giới vào một rổ và một người nghịch là chặn hết mọi
   * người. `CF-Connecting-IP` chỉ được tin khi kết nối ĐẾN TỪ loopback (tức là do
   * cloudflared chuyển tiếp); gọi thẳng trong LAN thì header đó bịa được nên bỏ.
   */
  _clientIp(req) {
    const socketIp = req.socket.remoteAddress || 'unknown';
    const viaLoopback = socketIp === '127.0.0.1' || socketIp === '::1' || socketIp === '::ffff:127.0.0.1';
    if (viaLoopback && req.headers['cf-connecting-ip']) {
      return String(req.headers['cf-connecting-ip']).slice(0, 64);
    }
    return socketIp;
  }

  /** Cửa đếm trượt: true = còn lượt, false = vượt hạn mức. */
  _allow(ip, kind, limit) {
    const now = Date.now();
    let rec = this.hits.get(ip);
    if (!rec) { rec = { chat: [], login: [] }; this.hits.set(ip, rec); }
    rec[kind] = rec[kind].filter((t) => now - t < limit.windowMs);
    if (rec[kind].length >= limit.max) return false;
    rec[kind].push(now);
    // Dọn rác: không để Map phình theo số IP đã từng ghé.
    if (this.hits.size > 5000) {
      for (const [k, v] of this.hits) {
        if (!v.chat.length && !v.login.length) this.hits.delete(k);
      }
    }
    return true;
  }

  _route(req, res, bodyBuf) {
    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    this._securityHeaders(req, res);

    if (req.method === 'OPTIONS') {
      res.writeHead(204);
      res.end();
      return;
    }

    // Trang web chat — ai cũng mở được (cửa đăng nhập nằm TRONG trang).
    if (req.method === 'GET' && url.pathname === '/') {
      if (!this.webPage) {
        this._json(res, 500, { error: 'Thiếu trang web public trong bản cài này.' });
        return;
      }
      res.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        // Không cache: đổi mode xong mà điện thoại vẫn dựng lại trang cũ thì
        // người dùng thấy đúng cái cửa mà mình vừa gỡ bỏ.
        'Cache-Control': 'no-store, must-revalidate',
      });
      res.end(this.webPage.replace('__MODE__', this.config().mode));
      return;
    }

    if (req.method === 'GET' && url.pathname === '/favicon.ico') {
      res.writeHead(204);
      res.end();
      return;
    }

    // Thông tin công khai — tên và ảnh để trang chat hiện đúng Alice.
    if (req.method === 'GET' && url.pathname === '/v1/who') {
      this._json(res, 200, { name: this.alice.name, avatar: this.avatar() });
      return;
    }

    // Vào cửa: mode `code` gửi { code }, mode `account` gửi { username, password }.
    if (req.method === 'POST' && url.pathname === '/v1/login') {
      const ip = this._clientIp(req);
      if (!this._allow(ip, 'login', LOGIN_LIMIT)) {
        this._json(res, 429, { error: 'Sai quá nhiều lần — chờ vài phút rồi thử lại.' });
        return;
      }
      this._login(res, bodyBuf);
      return;
    }

    // Kiểm tra phiên còn sống (web reload không phải vào cửa lại). Trả kèm TÊN để
    // trang khôi phục đúng danh tính cũ thay vì sinh một cái tên mới mỗi lần F5.
    if (req.method === 'GET' && url.pathname === '/v1/check') {
      const name = this._whoami(req);
      this._json(res, name ? 200 : 401, name ? { ok: true, name } : { error: 'Hết phiên.' });
      return;
    }

    // Lịch sử cuộc trò chuyện — để trang web hiện đúng những gì đã nói, thay vì mở
    // ra là một câu chào viết cứng trong mã.
    if (req.method === 'GET' && url.pathname === '/v1/history') {
      const cfg = this.config();
      if (cfg.mode !== 'anyone' && !this._authOk(req)) {
        this._json(res, 401, { error: 'Cần vào cửa trước đã.' });
        return;
      }
      this._json(res, 200, { messages: this._history(Number(url.searchParams.get('limit')) || 60) });
      return;
    }

    // Chat.
    if (req.method === 'POST' && url.pathname === '/v1/chat') {
      const cfg = this.config();
      if (cfg.mode !== 'anyone' && !this._authOk(req)) {
        this._json(res, 401, { error: 'Cần vào cửa trước đã.' });
        return;
      }
      if (!this._allow(this._clientIp(req), 'chat', CHAT_LIMIT)) {
        this._json(res, 429, { error: 'Bạn nhắn nhanh quá — chờ một phút nhé.' });
        return;
      }
      let payload;
      try { payload = JSON.parse(bodyBuf.toString('utf8')); } catch {
        this._json(res, 400, { error: 'Body phải là JSON { "message": "..." }.' });
        return;
      }
      const message = String(payload.message || '').trim();
      if (!message) {
        this._json(res, 400, { error: 'Thiếu "message".' });
        return;
      }
      // Mode `anyone` không bắt vào cửa, nhưng khách nào có phiên thì vẫn dùng
      // đúng tên của họ; chưa có phiên thì ghi là khách vô danh.
      this._chat(res, message, this._whoami(req) || 'anonymous');
      return;
    }

    this._json(res, 404, { error: 'Không có endpoint này. Xem README: GET /, POST /v1/chat' });
  }

  /**
   * Lịch sử để hiện trên trang web: nguyên văn, kèm TÊN người đã gửi.
   *
   * Không kèm `meta` đầy đủ — trong đó có model và danh sách model đã hỏng, là
   * chuyện nội bộ của chủ máy, không phải thứ khách cần thấy.
   */
  _history(limit) {
    if (!this.store) return [];
    const conv = this.store.currentConversation();
    if (!conv) return [];
    return this.store.recent(conv.id, Math.min(Math.max(limit, 1), 200)).map((m) => {
      let who = null;
      try { who = (JSON.parse(m.meta || 'null') || {}).who || null; } catch { /* meta rác */ }
      return { role: m.role, text: m.text, ts: m.ts, who };
    });
  }

  /** Hợp lệ khi có phiên còn sống. Mode `anyone` không gọi hàm này cho /v1/chat. */
  _authOk(req) {
    const token = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '');
    if (!token) return false;
    const session = this.sessions.get(token);
    if (!session) return false;
    if (session.expires < Date.now()) {
      this.sessions.delete(token);
      return false;
    }
    return true;
  }

  /**
   * @param {string} name  tên hiển thị của khách — `anonymous-xxxxx` hoặc username
   * @param {string} kind  'anon' | 'code' | 'account' (để `rotateCode` biết đá ai)
   */
  _newSession(name, kind) {
    const token = crypto.randomBytes(24).toString('base64url');
    this.sessions.set(token, { name, kind, expires: Date.now() + SESSION_TTL_MS });
    return token;
  }

  /** Tên của khách đang gọi, hoặc null nếu chưa có phiên. */
  _whoami(req) {
    const token = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '');
    const s = token ? this.sessions.get(token) : null;
    if (!s || s.expires < Date.now()) return null;
    return s.name;
  }

  _login(res, bodyBuf) {
    let payload;
    try { payload = JSON.parse(bodyBuf.toString('utf8')); } catch {
      this._json(res, 400, { error: 'Body phải là JSON.' });
      return;
    }
    const cfg = this.config();

    if (cfg.mode === 'anyone') {
      // Không có cửa, nhưng vẫn cấp phiên: khách cần một CÁI TÊN để tin của họ
      // phân biệt được với nhau trong lịch sử chat.
      const name = anonName();
      this._json(res, 200, { token: this._newSession(name, 'anon'), name });
      return;
    }

    if (cfg.mode === 'code') {
      const code = String(payload.code || '').trim();
      if (!cfg.code || !code || !safeEqual(code, cfg.code)) {
        this._json(res, 401, { error: 'Mã truy cập không đúng.' });
        return;
      }
      // Cùng một mã dùng chung, nhưng mỗi người một tên — không thì cả phòng nói
      // chuyện dưới một danh tính và không ai biết câu nào của ai.
      const name = anonName();
      this._json(res, 200, { token: this._newSession(name, 'code'), name });
      return;
    }

    const username = String(payload.username || '').trim();
    const password = String(payload.password || '');
    const account = cfg.accounts.find((a) => a.username === username);
    if (!account || !verifyPassword(password, account)) {
      this._json(res, 401, { error: 'Sai tên đăng nhập hoặc mật khẩu.' });
      return;
    }
    this._json(res, 200, { token: this._newSession(username, 'account'), name: username });
  }

  /**
   * Một lượt một người: engine chỉ chạy được một session tại một thời điểm. Người
   * tới sau XẾP HÀNG thay vì bị đuổi ngay — "cho mọi người cùng xài" mà cứ hai
   * người bấm cùng lúc là một người ăn lỗi thì không dùng được. Hàng có trần để
   * không biến thành chỗ chứa vô hạn.
   */
  async _chat(res, message, who) {
    if (!this.runTurn) {
      this._json(res, 503, { error: 'Alice chưa sẵn sàng.' });
      return;
    }
    if (this.busy && this.queue >= MAX_QUEUE) {
      this._json(res, 429, { error: 'Alice đang bận nhiều lượt — thử lại sau một chút.' });
      return;
    }
    this.queue += 1;
    try {
      while (this.busy) {
        await new Promise((r) => setTimeout(r, 250));
      }
      this.busy = true;
      try {
        this.engine.setBaseDir(this.baseDir);
        const out = await this.runTurn(message, null, { who });
        this.log.info(`public chat ok: who=${who} model=${out.model || '-'} session=${out.engineSession || '-'}`);
        this._json(res, 200, { text: out.text, model: out.model });
      } catch (err) {
        this.lastError = err.message;
        this.log.error(`public chat failed: ${err.message}`);
        this._json(res, 500, { error: err.message });
      } finally {
        this.busy = false;
      }
    } finally {
      this.queue -= 1;
    }
  }

  /**
   * Trang này chỉ phục vụ chính nó, không phải một API mở.
   *
   * Trước đây gửi `Access-Control-Allow-Origin: *`: bất kỳ website nào người dùng
   * ghé thăm cũng gọi được `/v1/chat` của Alice trong LAN và đốt API key của chủ
   * máy mà không ai thấy. Giờ chỉ mở CORS cho ĐÚNG gốc của chính trang.
   */
  _securityHeaders(req, res) {
    const origin = req.headers.origin;
    const host = req.headers.host;
    if (origin && host) {
      try {
        if (new URL(origin).host === host) {
          res.setHeader('Access-Control-Allow-Origin', origin);
          res.setHeader('Vary', 'Origin');
        }
      } catch { /* Origin rác — không mở cửa */ }
    }
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('X-Frame-Options', 'DENY');
    res.setHeader('Referrer-Policy', 'no-referrer');
  }

  _json(res, code, obj) {
    if (res.writableEnded) return;
    res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(obj));
  }
}

module.exports = {
  PublicServer, hashPassword, verifyPassword, newAccessCode, anonName,
  DEFAULT_PORT, MODES, MAX_BODY,
};
