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
const { isMention, handlesFor } = require('./mention');
const { toolActivity } = require('./activity');

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
 * @param {object} opts.reportMcp   cấu hình MCP report của Alice này (hoặc null)
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
    this.reportMcp = opts.reportMcp || null;
    this.log = opts.log;
    this.avatar = opts.avatar || (() => null);
    // Báo cho tiến trình chính khi có tin MỚI từ phòng chat công khai này — để cửa
    // sổ app (nếu đang mở đúng Alice này) vẽ tin đó ra ngay, không phải tự bấm
    // "chọn lại Alice" mới thấy khách vừa nói gì.
    this.onMessage = typeof opts.onMessage === 'function' ? opts.onMessage : null;
    // Báo cho tiến trình chính khi Alice BẮT ĐẦU/NGỪNG trả lời một lượt trong phòng
    // công khai — `onMessage` chỉ báo khi có TIN, không báo trạng thái "đang gõ".
    // Thiếu callback này thì cửa sổ app không bao giờ biết để vẽ ba chấm nhấp
    // nháy, dù trang web (SSE) vẫn nhận đúng `busy`/`activity` — đúng triệu chứng
    // "không thấy typing" chỉ xảy ra bên app, còn trang public thì tự nó vẫn có dữ
    // liệu để vẽ (đo thật 2026-08-13).
    this.onBusy = typeof opts.onBusy === 'function' ? opts.onBusy : null;
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
    // Trình duyệt đang mở SSE. Đây là thứ biến trang web từ "mỗi máy tự vẽ tin của
    // chính nó" thành một phòng chat THẬT: ai gửi gì, mọi máy thấy ngay và thấy
    // CÙNG MỘT THỨ TỰ (thứ tự do id trong kho quyết định, không do máy nào tự xếp).
    this.clients = new Set();
    this.heartbeat = null;
    this.activity = null;        // tool Alice đang chạy, để máy vào sau biết ngay
  }

  get running() {
    return Boolean(this.server && this.server.listening);
  }

  /**
   * Bao nhiêu người đang chat với Alice này.
   *
   * `online`  — trình duyệt đang mở kết nối SSE ngay lúc này (`this.clients`).
   * `joined`  — tổng số phiên đã cấp kể từ lúc máy chủ này bật (`this.sessions`);
   *             mất khi `stop()` — đúng nghĩa "trong lần public này có bấy
   *             nhiêu người", không phải một con số tích luỹ vĩnh viễn.
   */
  stats() {
    return { online: this.clients.size, joined: this.sessions.size };
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
      // Nén bằng CHÍNH model đang dùng, không phải bản nén cơ học mặc định. Phòng
      // chat công khai dài rất nhanh (nhiều người cùng nói), nên chất lượng bản
      // compact quyết định Alice còn nhớ được gì sau khi xoay session.
      const memory = new Memory(this.store, this.settings, this._summarizer());
      const workDir = path.join(this.baseDir, 'workspace');
      provisionWorkspace(this.settings, { brainMcp: this.brainMcp, reportMcp: this.reportMcp, dir: workDir });
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
    // Qua cloudflared và qua proxy của nhà mạng, một kết nối SSE im lặng vài chục
    // giây là bị cắt. Dòng comment rỗng định kỳ giữ nó sống.
    this.heartbeat = setInterval(() => this._broadcastRaw(': ping\n\n'), 20000);
    this.heartbeat.unref?.();

    this.log.info(`public server UP: ${this.alice.name} mode=${cfg.mode} on :${this.port}`);
    return { ok: true };
  }

  /** Nén phần cũ bằng model — dùng cho `Memory` của phòng chat này. */
  _summarizer() {
    return async (messages) => {
      const transcript = messages.map((m) => `[${m.role === 'alice' ? 'Alice' : 'khách'}]: ${m.text}`).join('\n');
      try {
        this.engine.setBaseDir(this.baseDir);
        const out = await this.engine.runWithFallback({
          message:
            'Tóm tắt đoạn hội thoại nhiều người dưới đây thành ghi chú để CHÍNH BẠN đọc lại ở phiên sau. '
            + 'Giữ lại: ai hỏi gì, quyết định đã chốt, con số, tên riêng, việc còn dở. Bỏ lời chào. '
            + 'Viết tiếng Việt, gạch đầu dòng, không mở bài.\n\n' + transcript,
          sessionId: null,
          model: this.engine.settings?.model || null,
          cwd: path.join(this.baseDir, 'workspace'),
        });
        return out.text;
      } catch {
        return null; // → Memory rơi về bản nén cơ học, thà thô còn hơn mất
      }
    };
  }

  stop() {
    if (this.heartbeat) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
    for (const c of this.clients) {
      try { c.end(); } catch { /* đã đóng */ }
    }
    this.clients.clear();
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

    // Thông tin công khai — tên, ảnh, và các tên gọi được để trang chat gợi ý khi
    // khách gõ `@` (không phải ai cũng nhớ Alice này có thể có tên riêng ngoài
    // `alice`, ví dụ "Alice K-OS" → `@alice`, `@kos`, `@alice-k-os`).
    if (req.method === 'GET' && url.pathname === '/v1/who') {
      this._json(res, 200, {
        name: this.alice.name,
        avatar: this.avatar(),
        handles: [...handlesFor(this.alice.name)],
      });
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
      if (cfg.mode !== 'anyone' && !this._authOk(req, url)) {
        this._json(res, 401, { error: 'Cần vào cửa trước đã.' });
        return;
      }
      const since = url.searchParams.get('since');
      this._json(res, 200, {
        messages: since !== null
          ? this._since(Number(since) || 0)
          : this._history(Number(url.searchParams.get('limit')) || 60),
      });
      return;
    }

    // Dòng sự kiện realtime: mọi trình duyệt đang mở phòng đều nhận cùng một luồng.
    if (req.method === 'GET' && url.pathname === '/v1/events') {
      const cfg = this.config();
      if (cfg.mode !== 'anyone' && !this._authOk(req, url)) {
        this._json(res, 401, { error: 'Cần vào cửa trước đã.' });
        return;
      }
      this._openStream(req, res);
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
    return this.store.recent(conv.id, Math.min(Math.max(limit, 1), 200)).map((m) => this._wire(m));
  }

  /** Phần còn thiếu kể từ `sinceId` — trang web gọi sau mỗi lần nối lại SSE. */
  _since(sinceId) {
    if (!this.store) return [];
    const conv = this.store.currentConversation();
    if (!conv) return [];
    return this.store.since(conv.id, sinceId).map((m) => this._wire(m));
  }

  /**
   * Hợp lệ khi có phiên còn sống. Mode `anyone` không gọi hàm này cho /v1/chat.
   *
   * `EventSource` KHÔNG gắn được header, nên riêng luồng SSE token đi qua query.
   * Chấp nhận đánh đổi: token lọt vào log truy cập của proxy. Đổi lại nó là token
   * phiên chat, hết hạn 7 ngày và thu hồi được bằng cách đổi mã truy cập.
   */
  _authOk(req, url = null) {
    let token = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '');
    if (!token && url) token = url.searchParams.get('token') || '';
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

  // ── dòng sự kiện realtime (SSE) ─────────────────────────────────────────

  _openStream(req, res) {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      // Nginx và vài proxy gom đệm response; SSE mà bị gom là không còn realtime.
      'X-Accel-Buffering': 'no',
    });
    this.clients.add(res);
    // Máy vừa vào phải biết ngay Alice có đang bận không, chứ không ngồi chờ sự
    // kiện tiếp theo mới hiện trạng thái.
    res.write(`data: ${JSON.stringify({ type: 'hello', busy: this.busy, activity: this.activity })}\n\n`);

    // Nghe `res`, KHÔNG nghe `req`.
    //
    // `req` là luồng ĐỌC của một request GET không có thân — nó kết thúc ngay lập
    // tức, và Node bắn `req.on('close')` ngay sau đó. Bản đầu nghe ở đó nên mọi
    // client SSE bị gỡ khỏi danh sách đúng một nhịp sau khi vào: `clients` luôn
    // bằng 0, không ai nhận được gì, mà server vẫn báo 200 và trang web vẫn im
    // lặng như thể máy chủ không có tin nào để phát.
    res.on('close', () => this.clients.delete(res));
    res.on('error', () => this.clients.delete(res));
  }

  _broadcastRaw(chunk) {
    for (const c of this.clients) {
      try { c.write(chunk); } catch { this.clients.delete(c); }
    }
  }

  _broadcast(event) {
    this._broadcastRaw(`data: ${JSON.stringify(event)}\n\n`);
  }

  /**
   * Phát tin vừa được tạo BÊN NGOÀI phòng chat công khai — cụ thể là Bệ hạ chat
   * ngay trong app trong lúc Alice đang public. `chat.db` là CHUNG một file giữa
   * app và máy chủ public (cùng `baseDir`), nhưng hai bên là hai `Store` khác
   * nhau nên máy chủ public không tự biết app vừa ghi thêm gì — phải được gọi.
   *
   * Không có hàm này thì trang web/điện thoại chỉ thấy tin app gửi sau khi tự
   * tải lại trang (mất "realtime"), đúng triệu chứng "chat trên app, điện thoại
   * không thấy".
   */
  broadcastFromDesktop(conversationId, sinceId) {
    if (!this.store) return;
    for (const row of this.store.since(conversationId, sinceId)) {
      this._broadcast({ type: 'message', message: this._wire(row) });
    }
  }

  /** Một tin trong kho → hình dạng mà trang web hiểu. */
  _wire(row) {
    let who = null;
    try { who = (JSON.parse(row.meta || 'null') || {}).who || null; } catch { /* meta rác */ }
    return { id: row.id, role: row.role, text: row.text, ts: row.ts, who };
  }

  /**
   * Một lượt một người: engine chỉ chạy được một session tại một thời điểm. Người
   * tới sau XẾP HÀNG thay vì bị đuổi ngay — "cho mọi người cùng xài" mà cứ hai
   * người bấm cùng lúc là một người ăn lỗi thì không dùng được. Hàng có trần để
   * không biến thành chỗ chứa vô hạn.
   *
   * Trả lời NGAY sau khi lưu tin, không chờ Alice nói xong: câu của người gửi phải
   * hiện lên mọi máy tức thì. Câu trả lời của Alice tới sau qua SSE. Đây là chỗ
   * sửa "chat không realtime, thứ tự lộn xộn" — không máy nào tự chèn tin vào danh
   * sách của mình nữa, tất cả cùng nhận một luồng đã có id.
   */
  async _chat(res, message, who) {
    if (!this.runTurn) {
      this._json(res, 503, { error: 'Alice chưa sẵn sàng.' });
      return;
    }
    // `@alice` mới gọi Alice. Không gọi thì vẫn lưu và vẫn phát cho cả phòng —
    // im lặng khác với mù: lượt sau có gọi, Alice đọc lại hết phần này.
    const wanted = isMention(message, this.alice.name);

    if (!wanted) {
      const out = await this.runTurn(message, null, { who, silent: true });
      const row = { id: out.messageId, role: 'human', text: message, ts: Date.now(), who };
      this._broadcast({ type: 'message', message: row });
      if (this.onMessage) this.onMessage(row);
      this._json(res, 200, { ok: true, id: out.messageId, replied: false });
      return;
    }

    if (this.busy && this.queue >= MAX_QUEUE) {
      this._json(res, 429, { error: 'Alice đang bận nhiều lượt — thử lại sau một chút.' });
      return;
    }

    // Lưu + phát tin của người gửi TRƯỚC, rồi mới chạy lượt ở nền.
    this._json(res, 202, { ok: true, replied: true });
    this.queue += 1;
    try {
      while (this.busy) await new Promise((r) => setTimeout(r, 200));
      this.busy = true;
      try {
        this.engine.setBaseDir(this.baseDir);
        const seenIds = new Set();
        const out = await this.runTurn(message, (partial, ev) => {
          // Chỉ phát sự kiện TOOL, không phát chữ đang chảy: nhiều máy cùng nhận
          // một luồng chữ chạy thì tin nửa vời chen vào giữa lịch sử của người
          // khác — đúng kiểu "thứ tự lộn xộn" cần tránh.
          const act = toolActivity(ev);
          if (act && !seenIds.has(act.key)) {
            seenIds.add(act.key);
            this.activity = act.label;
            this._broadcast({ type: 'activity', label: act.label });
            if (this.onBusy) this.onBusy(true, act.label);
          }
        }, {
          who,
          // Tin của người gửi phải lên MỌI máy NGAY khi lưu xong — trước cả báo
          // "đang trả lời". Trước đây `busy:true` phát TRƯỚC (ngay khi vào try), còn
          // tin người gửi chỉ phát SAU KHI Alice trả lời xong — nên "Alice đang trả
          // lời…" luôn vẽ ra TRƯỚC tin vừa gõ, sai thứ tự (đo thật 2026-08-13, xem
          // `broadcastFromDesktop` cho nhánh app→web tương ứng).
          onSaved: (messageId) => {
            const row = { id: messageId, role: 'human', text: message, ts: Date.now(), who };
            this._broadcast({ type: 'message', message: row });
            if (this.onMessage) this.onMessage(row);
            this._broadcast({ type: 'busy', busy: true });
            if (this.onBusy) this.onBusy(true, null);
          },
        });

        this.activity = null;
        this.log.info(`public chat ok: who=${who} model=${out.model || '-'} caughtUp=${out.caughtUp || 0}`);
        // Tin người gửi đã phát ở `onSaved` rồi — giờ chỉ còn câu trả lời của Alice.
        for (const row of this.store.since(out.conversationId, out.messageId)) {
          const wired = this._wire(row);
          this._broadcast({ type: 'message', message: wired });
          if (this.onMessage) this.onMessage(wired);
        }
      } catch (err) {
        this.activity = null;
        this.lastError = err.message;
        this.log.error(`public chat failed: ${err.message}`);
        this._broadcast({ type: 'error', error: err.message });
      } finally {
        this.busy = false;
        this._broadcast({ type: 'busy', busy: false });
        if (this.onBusy) this.onBusy(false, null);
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
