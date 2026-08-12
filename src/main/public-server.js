'use strict';

/**
 * Public Server — biến một Alice thành MÁY CHỦ có TRANG WEB CHAT.
 *
 * Bệ hạ chốt (2026-08-13): "public là một bản website luôn — người khác quét mã
 * là vào chat được luôn. Hai mode:
 *   - anyone: ai có link/QR đều vào chat được, không cần gì;
 *   - account: phải đăng nhập username + password do chủ Alice tạo trước."
 *
 * Kiến trúc: mỗi Alice public có một worker RIÊNG — chat.db, session và trí nhớ
 * của chính nó, độc lập với Alice đang mở trên màn hình. Một lượt đồng thời.
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

/** Băm mật khẩu — KHÔNG bao giờ lưu plaintext (scrypt, salt riêng mỗi tài khoản). */
function hashPassword(password, salt = crypto.randomBytes(16).toString('hex')) {
  const hash = crypto.scryptSync(String(password), salt, 64).toString('hex');
  return { salt, hash };
}

function verifyPassword(password, account) {
  const { hash } = hashPassword(password, account.salt);
  return crypto.timingSafeEqual(Buffer.from(hash, 'hex'), Buffer.from(account.hash, 'hex'));
}

/**
 * @param {object} opts
 * @param {object} opts.alice       registry entry { id, name, provider }
 * @param {string} opts.baseDir     alices/<id>/
 * @param {object} opts.settings    settings chung của app
 * @param {object} opts.engine      OpencodeEngine (dùng chung, setBaseDir riêng)
 * @param {object} opts.brainMcp    cấu hình MCP brain của Alice này (hoặc null)
 * @param {object} opts.log         logger
 */
class PublicServer {
  constructor(opts) {
    this.alice = opts.alice;
    this.baseDir = opts.baseDir;
    this.settings = opts.settings;
    this.engine = opts.engine;
    this.brainMcp = opts.brainMcp;
    this.log = opts.log;
    this.store = null;
    this.runTurn = null;
    this.server = null;
    this.port = null;
    this.busy = false;
    this.lastError = null;
    this.sessions = new Map(); // sessionToken → username (chỉ trong bộ nhớ)
    this.webPage = null;
  }

  get running() {
    return Boolean(this.server && this.server.listening);
  }

  /** Cấu hình public của Alice: { enabled, mode, port, tokens, accounts }. */
  config() {
    try {
      const raw = JSON.parse(fs.readFileSync(path.join(this.baseDir, 'public.json'), 'utf8'));
      return {
        enabled: Boolean(raw.enabled),
        mode: raw.mode === 'account' ? 'account' : 'anyone',
        port: raw.port || DEFAULT_PORT,
        tokens: Array.isArray(raw.tokens) ? raw.tokens : [],
        accounts: Array.isArray(raw.accounts) ? raw.accounts : [],
      };
    } catch {
      return { enabled: false, mode: 'anyone', port: DEFAULT_PORT, tokens: [], accounts: [] };
    }
  }

  saveConfig(cfg) {
    fs.mkdirSync(this.baseDir, { recursive: true });
    fs.writeFileSync(path.join(this.baseDir, 'public.json'), JSON.stringify(cfg, null, 2), 'utf8');
  }

  /**
   * Mở server. Mode 'anyone' mở được ngay; mode 'account' cần ít nhất một tài
   * khoản (máy chủ không cửa là vô nghĩa).
   */
  async start(port) {
    if (this.running) return { ok: true };
    const cfg = this.config();
    if (cfg.mode === 'account' && !cfg.accounts.length) {
      throw new Error('Chưa có tài khoản nào — thêm username + password trước khi public.');
    }
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
    this.log.info(`public server DOWN: ${this.alice.name}`);
  }

  // ── HTTP ────────────────────────────────────────────────────────────────

  _handle(req, res) {
    const body = [];
    req.on('data', (c) => body.push(c));
    req.on('end', () => this._route(req, res, Buffer.concat(body)));
  }

  _route(req, res, bodyBuf) {
    this._cors(res);
    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    if (req.method === 'OPTIONS') {
      res.writeHead(204);
      res.end();
      return;
    }

    // Trang web chat — ai cũng mở được (form đăng nhập nằm trong trang).
    if (req.method === 'GET' && url.pathname === '/') {
      if (!this.webPage) {
        this._json(res, 500, { error: 'Thiếu trang web public trong bản cài này.' });
        return;
      }
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(this.webPage.replace('__MODE__', this.config().mode));
      return;
    }

    // Thông tin công khai — không cần token.
    if (req.method === 'GET' && url.pathname === '/v1/who') {
      this._json(res, 200, { name: this.alice.name });
      return;
    }

    // Đăng nhập — mode account.
    if (req.method === 'POST' && url.pathname === '/v1/login') {
      this._login(res, bodyBuf);
      return;
    }

    // Kiểm tra session còn sống (web reload không phải đăng nhập lại).
    if (req.method === 'GET' && url.pathname === '/v1/check') {
      const ok = this._authOk(req);
      this._json(res, ok ? 200 : 401, ok ? { ok: true } : { error: 'Hết phiên.' });
      return;
    }

    // Chat.
    if (req.method === 'POST' && url.pathname === '/v1/chat') {
      const cfg = this.config();
      if (cfg.mode === 'account' && !this._authOk(req)) {
        this._json(res, 401, { error: 'Cần đăng nhập (hoặc token truy cập).' });
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
      this._chat(res, message);
      return;
    }

    this._json(res, 404, { error: 'Không có endpoint này. Xem README: GET /, POST /v1/chat' });
  }

  /**
   * Hợp lệ khi: (a) có session web còn sống, hoặc (b) có token API do chủ cấp.
   * Mode 'anyone' không gọi hàm này cho /v1/chat.
   */
  _authOk(req) {
    const token = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '');
    if (!token) return false;
    if (this.sessions.has(token)) return true;
    return this.config().tokens.some((t) => t.token === token);
  }

  _login(res, bodyBuf) {
    let payload;
    try { payload = JSON.parse(bodyBuf.toString('utf8')); } catch {
      this._json(res, 400, { error: 'Body phải là JSON { "username", "password" }.' });
      return;
    }
    const username = String(payload.username || '').trim();
    const password = String(payload.password || '');
    const account = this.config().accounts.find((a) => a.username === username);
    if (!account || !verifyPassword(password, account)) {
      this._json(res, 401, { error: 'Sai tên đăng nhập hoặc mật khẩu.' });
      return;
    }
    // Session mới cho mỗi lượt đăng nhập; hết hạn sau 7 ngày (đủ dùng web chat).
    const sessionToken = crypto.randomBytes(24).toString('base64url');
    this.sessions.set(sessionToken, { username, expires: Date.now() + 7 * 24 * 3600 * 1000 });
    this._json(res, 200, { token: sessionToken, name: username });
  }

  async _chat(res, message) {
    if (this.busy) {
      this._json(res, 429, { error: 'Alice đang bận lượt khác — thử lại sau.' });
      return;
    }
    if (!this.runTurn) {
      this._json(res, 503, { error: 'Alice chưa sẵn sàng.' });
      return;
    }
    this.busy = true;
    try {
      this.engine.setBaseDir(this.baseDir);
      const out = await this.runTurn(message);
      this.log.info(`public chat ok: model=${out.model || '-'} session=${out.engineSession || '-'}`);
      this._json(res, 200, { text: out.text, model: out.model });
    } catch (err) {
      this.lastError = err.message;
      this.log.error(`public chat failed: ${err.message}`);
      this._json(res, 500, { error: err.message });
    } finally {
      this.busy = false;
    }
  }

  _cors(res) {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  }

  _json(res, code, obj) {
    res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(obj));
  }
}

/** Tạo token truy cập mới. */
function newToken() {
  return crypto.randomBytes(24).toString('base64url');
}

module.exports = { PublicServer, newToken, hashPassword, verifyPassword, DEFAULT_PORT };
