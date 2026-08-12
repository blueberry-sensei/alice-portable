'use strict';

/**
 * Public Server — biến một Alice thành MÁY CHỦ: người khác gọi HTTP tới máy
 * này bằng token do chủ cấp, Alice trả lời bằng CHÍNH trí nhớ của nó.
 *
 * Bệ hạ chốt (2026-08-13): "bấm Public Alice — máy tôi thành máy chủ; người khác
 * access với account tôi cấp; có thể unpublic".
 *
 * Kiến trúc: mỗi Alice public có một worker RIÊNG — mở chat.db của chính nó,
 * session/trí nhớ của chính nó, độc lập với Alice đang mở trên màn hình. Một lượt
 * đồng thời tại một thời điểm (busy toàn cục) — máy cá nhân không cần nhiều hơn.
 *
 * Không dependency mới: node:http. Không HTTP framework.
 */

const http = require('node:http');
const crypto = require('node:crypto');
const path = require('node:path');

const { Store } = require('./memory/store');
const { Memory } = require('./memory/memory');
const { createTurnRunner } = require('./turn');
const { provisionWorkspace } = require('./alice');
const auth = require('./engine/auth');

const DEFAULT_PORT = 8931;

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
  }

  get running() {
    return Boolean(this.server && this.server.listening);
  }

  /**
   * Mở server. Token phải có sẵn trong config trước (do UI tạo) — không có token
   * nào thì không mở (máy chủ không cửa là vô nghĩa).
   */
  async start(port) {
    if (this.running) return { ok: true };
    const cfg = this.config();
    if (!cfg.tokens.length) {
      throw new Error('Chưa có token truy cập nào — tạo token trước khi public.');
    }
    if (!this.store) {
      this.store = new Store(path.join(this.baseDir, 'chat.db'));
      const memory = new Memory(this.store, this.settings);
      const workDir = path.join(this.baseDir, 'workspace');
      provisionWorkspace(this.settings, { brainMcp: this.brainMcp, dir: workDir });
      this.runTurn = createTurnRunner({ store: this.store, memory, engine: this.engine, workDir, settings: this.settings });
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
    this.log.info(`public server UP: ${this.alice.name} on :${this.port}`);
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
    this.log.info(`public server DOWN: ${this.alice.name}`);
  }

  /** Cấu hình public của Alice: { enabled, port, tokens: [{label, token, created_at}] }. */
  config() {
    try {
      const raw = JSON.parse(require('node:fs').readFileSync(path.join(this.baseDir, 'public.json'), 'utf8'));
      return { enabled: Boolean(raw.enabled), port: raw.port || DEFAULT_PORT, tokens: Array.isArray(raw.tokens) ? raw.tokens : [] };
    } catch {
      return { enabled: false, port: DEFAULT_PORT, tokens: [] };
    }
  }

  saveConfig(cfg) {
    require('node:fs').mkdirSync(this.baseDir, { recursive: true });
    require('node:fs').writeFileSync(path.join(this.baseDir, 'public.json'), JSON.stringify(cfg, null, 2), 'utf8');
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

    // Bắt buộc có token hợp lệ — trừ GET / (health, không lộ gì).
    if (req.method === 'GET' && url.pathname === '/') {
      this._json(res, 200, { name: this.alice.name, status: 'ok', version: require('../../package.json').version });
      return;
    }

    const token = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '');
    if (!this._validToken(token)) {
      this._json(res, 401, { error: 'Token không hợp lệ.' });
      return;
    }

    if (req.method === 'POST' && url.pathname === '/v1/chat') {
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

  _validToken(token) {
    if (!token) return false;
    return this.config().tokens.some((t) => t.token === token);
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

module.exports = { PublicServer, newToken, DEFAULT_PORT };
