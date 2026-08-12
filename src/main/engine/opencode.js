'use strict';

const { spawn } = require('node:child_process');
const readline = require('node:readline');
const fs = require('node:fs');

const { resolveOpencode, DATA_DIR } = require('../config');
const { portableEnv } = require('./auth');

/**
 * Engine adapter — `opencode run`.
 *
 * `D-0055`: dùng CLI `opencode run --session ses_…`, KHÔNG dựng `opencode serve`.
 * Đã đo: nối tiếp session thật (lượt 1 bảo nhớ 4271, lượt 2 hỏi lại → trả 4271).
 * Lỗi `Session not found` ở bản alice-social là do đưa **uuid ngoài** vào `--session`;
 * opencode chỉ nhận id của chính nó, dạng `ses_…`. Bởi vậy:
 *
 *   - id hội thoại của app (uuid) và id session của engine (`ses_…`) là HAI thứ
 *     khác nhau, không bao giờ được dùng lẫn.
 *   - `--format json` trả NDJSON, trong đó `step_finish.tokens.input` là độ đầy cửa
 *     sổ ĐO ĐƯỢC — đây là số mà tầng trí nhớ dùng để quyết định xoay session.
 */

const SESSION_RE = /^ses_[A-Za-z0-9]+$/;

class OpencodeEngine {
  constructor(settings) {
    this.settings = settings;
    const found = resolveOpencode();
    this.binPath = found.path;
    this.binSource = found.source; // bundled | host | missing
    this._child = null;
    // Thư mục dữ liệu của ALICE ĐANG MỞ — auth/session của mỗi Alice nằm riêng.
    // Mặc định là data dir cũ để test chạy không cần set; main gọi setBaseDir
    // mỗi khi đổi Alice.
    this.baseDir = DATA_DIR;
  }

  setBaseDir(dir) {
    this.baseDir = dir;
  }

  get available() {
    return Boolean(this.binPath && fs.existsSync(this.binPath));
  }

  /**
   * Danh sách model duyệt TỪ opencode, không hard-code (`D-0054` mục 5).
   * Lý do cụ thể: digest phiên trước ghi 10 model free của Zen; đo lại hôm nay chỉ
   * còn 7, và có model đổi tên (`ling-3.0-tiny-free` → `ling-3.0-flash-free`).
   * Bảng hard-code sẽ trỏ vào model không tồn tại.
   */
  async listModels() {
    const { stdout } = await this._exec(['models'], { timeout: 60000 });
    return stdout
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter((l) => /^[\w.-]+\/[\w.:-]+$/.test(l));
  }

  /**
   * Thứ tự thử: ưu tiên của user ∩ model đang có thật, rồi tới phần còn lại.
   * Trả mảng — người gọi xoay vòng khi lỗi.
   */
  async modelChain() {
    let available;
    try {
      available = await this.listModels();
    } catch {
      return this.settings.modelPreference.slice(); // không hỏi được thì cứ thử
    }
    const set = new Set(available);
    const preferred = this.settings.modelPreference.filter((m) => set.has(m));
    const rest = available.filter((m) => !preferred.includes(m));
    return preferred.concat(rest);
  }

  /**
   * Chạy một lượt.
   *
   * @param {object}   opts
   * @param {string}   opts.message    tin của người dùng (hoặc mồi tiếp nối)
   * @param {string?}  opts.sessionId  `ses_…` để nối tiếp; null = tạo session mới
   * @param {string}   opts.model      `provider/model`
   * @param {string}   opts.cwd        thư mục làm việc của agent
   * @param {function} [opts.onEvent]  nhận từng event NDJSON đã parse
   * @returns {Promise<{sessionId, text, tokens, model, events}>}
   */
  run({ message, sessionId = null, model, cwd, onEvent = null, signal = null }) {
    if (!this.available) {
      return Promise.reject(new Error(
        'Không tìm thấy binary opencode. Bản portable phải có runtime/opencode/opencode.exe (D-0053 mục 3).'
      ));
    }
    if (sessionId && !SESSION_RE.test(sessionId)) {
      // Chặn đúng lỗi đã làm mất một ngày ở alice-social: nhét uuid ngoài vào --session.
      return Promise.reject(new Error(
        `Session id không phải của opencode: ${sessionId}. Chỉ nhận dạng "ses_…" (D-0055).`
      ));
    }

    const args = ['run', '--format', 'json', '--dir', cwd];
    if (model) args.push('--model', model);
    if (sessionId) args.push('--session', sessionId);
    if (this.settings.autoApprove !== false) args.push('--auto');
    args.push(message);

    return new Promise((resolve, reject) => {
      const child = spawn(this.binPath, args, {
        cwd,
        windowsHide: true,
        env: { ...process.env, ...portableEnv(this.baseDir), FORCE_COLOR: '0', NO_COLOR: '1' },
        // `stdin: 'ignore'` — KHÔNG để mặc định 'pipe'.
        //
        // `opencode run` nhận thêm nội dung qua stdin, nên nếu stdin là một pipe
        // đang mở và không bao giờ EOF thì nó chờ vô hạn: **không nhả một event
        // nào**, kể cả `step_start`. Đúng triệu chứng đã đo: cùng bộ tham số, chạy
        // từ shell xong trong 4 giây, chạy qua `spawn` thì im lặng tới lúc timeout.
        // Bẫy này giết cả app chứ không chỉ một lượt, và nhìn từ ngoài nó trông y
        // hệt "model chậm".
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      this._child = child;

      const events = [];
      const textParts = new Map(); // part id -> text, để bản cập nhật ghi đè bản cũ
      let resolvedSession = sessionId;
      let tokens = null;
      let stderr = '';
      let timedOut = false;
      let firstError = null; // event {"type":"error"} — lỗi thật của opencode/model

      /**
       * Đồng hồ IM LẶNG, không phải đồng hồ tổng thời gian.
       *
       * Một lượt dùng nhiều tool có thể chạy lâu một cách chính đáng; cái không
       * chính đáng là **không có gì xảy ra cả**. Đã dính thật trong phiên build:
       * một lượt treo ~8 phút không nhả event nào, và vì `run()` bản đầu không có
       * timeout nên không có đường thoát ngoài việc người dùng tự bấm dừng.
       * (Nguyên nhân lượt đó hoá ra là opencode tự `npm install` plugin vào
       * `XDG_CONFIG_HOME` trống — xem `engine/auth.js#seedConfigDir`. Nhưng timeout
       * vẫn cần: digest đã cảnh báo có provider treo ăn trọn 900s mỗi lượt.)
       *
       * Timeout ném lỗi như mọi lỗi khác, nên `runWithFallback` tự xoay sang model
       * kế tiếp — model treo bị loại khỏi chuỗi thay vì kéo cả app xuống.
       */
      const idleMs = this.settings.idleTimeoutMs || 120000;
      let idleTimer = null;
      const resetIdle = () => {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => {
          timedOut = true;
          child.kill();
        }, idleMs);
      };

      if (signal) {
        signal.addEventListener('abort', () => child.kill(), { once: true });
      }

      resetIdle();

      const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
      rl.on('line', (line) => {
        resetIdle(); // có tiếng động là còn sống
        const t = line.trim();
        if (!t.startsWith('{')) return;
        let ev;
        try {
          ev = JSON.parse(t);
        } catch {
          return; // dòng không phải JSON (banner, log) — bỏ qua, không làm chết lượt
        }
        events.push(ev);
        if (ev.sessionID) resolvedSession = ev.sessionID;

        // Event lỗi của opencode/model — trước đây bị BỎ QUA nên lượt lỗi trả về
        // text rỗng, UI hiện "Alice không trả lời gì" mà không nói lý do.
        if (ev.type === 'error' && ev.error) {
          const msg = (ev.error.data && ev.error.data.message) || ev.error.name || 'lỗi không rõ';
          if (!firstError) firstError = { message: msg, name: ev.error.name || '' };
        }
        if (ev.type === 'text' && ev.part) {
          textParts.set(ev.part.id, ev.part.text || '');
        }
        if (ev.type === 'step_finish' && ev.part && ev.part.tokens) {
          tokens = ev.part.tokens;
        }
        if (onEvent) {
          try {
            onEvent(ev, joinParts(textParts));
          } catch { /* lỗi ở UI không được làm chết lượt engine */ }
        }
      });

      // KHÔNG reset đồng hồ theo stderr. Tiếng ồn không phải tiến triển: opencode
      // vẫn nhả log ra stderr trong lúc bootstrap/chờ, và nếu tính cả stderr thì
      // đồng hồ im-lặng không bao giờ chạm ngưỡng. Chỉ event NDJSON trên stdout —
      // thứ chứng minh model đang thật sự sinh ra cái gì đó — mới tính.
      child.stderr.on('data', (b) => { stderr += b.toString('utf8'); });

      child.on('error', (err) => {
        clearTimeout(idleTimer);
        this._child = null;
        reject(new Error(`Không chạy được opencode: ${err.message}`));
      });

      child.on('close', (code) => {
        clearTimeout(idleTimer);
        this._child = null;
        const text = joinParts(textParts);
        if (timedOut && !text) {
          reject(new Error(`Model ${model} im lặng quá ${Math.round(idleMs / 1000)}s — bỏ qua.`));
          return;
        }
        if (code !== 0 && !text) {
          // stderr rỗng nhưng opencode có nói lỗi qua event NDJSON → ưu tiên event đó.
          const why = (firstError ? firstError.message : '')
            || (stderr ? stderr.trim().slice(0, 500) : '(opencode thoát mà không nói lý do)');
          reject(new Error(`opencode thoát mã ${code} (model ${model}): ${why}`));
          return;
        }
        if (code === 0 && !text && firstError) {
          reject(new Error(`${model}: ${firstError.message}`));
          return;
        }
        resolve({ sessionId: resolvedSession, text, tokens, model, events, stderr, code });
      });
    });
  }

  /**
   * Chạy một lượt, tự xoay model khi lỗi/hết quota (`D-0054` mục 5).
   * Trả thêm `attempts` để UI nói được "đã thử model nào, hỏng vì gì" — im lặng
   * đổi model là kiểu lỗi rất khó truy về sau.
   */
  async runWithFallback(opts) {
    let chain;
    if (opts.model) {
      // Model người dùng CHỌN phải còn tồn tại thật (danh sách realtime). Zen đổi
      // tên/bỏ model theo thời gian — model đã chết mà vẫn cố chạy thì lượt lỗi
      // câm (đúng ca feedback khách: chọn model đã không còn → "opencode thoát mã 1").
      let available = null;
      try { available = await this.listModels(); } catch { /* không hỏi được thì cứ thử */ }
      if (available && available.includes(opts.model)) {
        // Model chọn thử TRƯỚC; hỏng (hết số dư, lỗi server...) thì tự chuyển sang
        // các model còn lại — Alice không kẹt cứng vì một model trả phí hết tiền.
        const rest = (await this.modelChain()).filter((m) => m !== opts.model);
        chain = [opts.model, ...rest];
      } else {
        // Model không còn tồn tại → bỏ qua nó, dùng chuỗi mặc định.
        chain = (await this.modelChain()).filter((m) => m !== opts.model);
        if (chain.length) chain.unshift(`__skipped:${opts.model}`);
      }
    } else {
      chain = await this.modelChain();
    }
    if (!chain.length) throw new Error('Không có model nào khả dụng (opencode models trả rỗng).');

    const attempts = [];
    for (const model of chain) {
      if (model.startsWith('__skipped:')) continue;
      try {
        const out = await this.run({ ...opts, model });
        return { ...out, attempts };
      } catch (err) {
        attempts.push({ model, error: err.message });
        // Session đã tạo bởi model hỏng vẫn dùng lại được — lịch sử nằm ở
        // session, không ở model.
      }
    }
    const detail = attempts.map((a) => `${a.model}: ${a.error}`).join(' | ');
    throw new Error(`Mọi model đều hỏng. ${detail}`);
  }

  cancel() {
    if (this._child) {
      this._child.kill();
      this._child = null;
      return true;
    }
    return false;
  }

  _exec(args, { timeout = 30000 } = {}) {
    return new Promise((resolve, reject) => {
      const child = spawn(this.binPath, args, {
        windowsHide: true,
        env: { ...process.env, ...portableEnv(this.baseDir) },
        stdio: ['ignore', 'pipe', 'pipe'], // cùng lý do như trong run()
      });
      let stdout = '';
      let stderr = '';
      const timer = setTimeout(() => { child.kill(); reject(new Error(`Quá ${timeout}ms`)); }, timeout);
      child.stdout.on('data', (b) => { stdout += b.toString('utf8'); });
      child.stderr.on('data', (b) => { stderr += b.toString('utf8'); });
      child.on('error', (e) => { clearTimeout(timer); reject(e); });
      child.on('close', (code) => {
        clearTimeout(timer);
        if (code === 0) resolve({ stdout, stderr });
        else reject(new Error(`opencode ${args[0]} thoát mã ${code}: ${stderr.slice(0, 300)}`));
      });
    });
  }
}

function joinParts(map) {
  return Array.from(map.values()).join('').trim();
}

module.exports = { OpencodeEngine, SESSION_RE };
