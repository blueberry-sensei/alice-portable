'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');

const config = require('../config');

/**
 * Alice Brain, không Docker (`D-0053` mục 2 — và không được giảm năng lực recall).
 *
 * Đã mở ruột image brain trước khi viết file này, chứ không phán từ trí nhớ:
 *
 *   - `sag_api` (105 file `.py`) + `sag_agent` + `alicecore` (87 file `.py`) đều
 *     **thuần Python**, 0 `.so`, 0 `.pyd` → mang sang Windows được.
 *   - Vector store **LanceDB**, DB **SQLite** (`sqlite+aiosqlite`) → nhúng được cả hai.
 *   - Embedding chỉ là **một URL OpenAI-compatible** (`SAG_EMBEDDING_BASE_URL` +
 *     `SAG_EMBEDDING_MODEL`) → local hay API là lựa chọn của user (`D-0054` mục 2).
 *   - **MCP chạy in-process**, không qua HTTP: docstring của `sag_api/mcp/server.py`
 *     nói rõ "the HTTP wrapper, the in-process agent and the stdio entry point" đều
 *     dùng chung tool. Nên recall KHÔNG cần dựng server — chỉ cần stdio MCP.
 *
 * Hệ quả kiến trúc: bản portable chạy **ba** tiến trình chứ không phải bốn.
 * `sag_api.desktop` (uvicorn) chỉ bật khi cần HTTP API cho ingest/sync.
 */
class BrainSidecar {
  /**
   * @param {object} brainSettings
   * @param {object} [paths]  ghi đè đường dẫn — để test dựng được một "máy vừa cài"
   *   riêng mà KHÔNG phải sửa biến môi trường toàn tiến trình. Bản đầu của test làm
   *   thế và nó làm mù luôn các test brain khác chạy cùng lượt: 0 fail nhưng 2 skip,
   *   nhìn qua tưởng vẫn xanh.
   */
  constructor(brainSettings, paths = {}) {
    this.settings = brainSettings || {};
    this.runtimeDir = paths.runtimeDir || path.join(config.RESOURCES_DIR, 'brain');
    // Windows: python embeddable đặt python.exe ngay trong runtime/brain/python.
    // macOS/Linux: bundle là cả cây python (bin/python3).
    this.pythonPath = process.platform === 'win32'
      ? path.join(this.runtimeDir, 'python', 'python.exe')
      : path.join(this.runtimeDir, 'python', 'bin', 'python3');
    this.appDir = path.join(this.runtimeDir, 'app');
    this.dataDir = paths.dataDir || path.join(config.DATA_DIR, 'brain');
    this.proc = null;
    this.lastError = null;
  }

  get available() {
    return fs.existsSync(this.pythonPath) && fs.existsSync(path.join(this.appDir, 'sag_api'));
  }

  /**
   * Dựng brain RỖNG cho lần chạy đầu.
   *
   * Alice khởi đầu **không có tri thức nào** và tự đắp dần khi làm việc — đó là cách
   * ALICE CODING (github.com/blueberry-sensei/alice-coding) hoạt động. Bộ cài KHÔNG
   * mang theo tri thức của ai cả:
   *
   *   - Tri thức của một project là dữ liệu **của người đó**. Nhét brain của project
   *     A vào bộ cài phát cho người B là phát tán dữ liệu nhầm chỗ — bản đầu của
   *     script build đã làm đúng lỗi này, 546MB nhật ký quyết định của một khách
   *     hàng suýt đi vào một repo public.
   *   - Bỏ nó ra thì bộ cài từ ~1,9GB xuống ~350MB, lọt trần 2GB của GitHub Release,
   *     và CI dựng được cho cả ba hệ điều hành.
   *
   * Schema tự tạo được: `sag_api.core.db.init_db()` gọi `Base.metadata.create_all`.
   * Không cần alembic, không cần ship file `.db` dựng sẵn.
   *
   * Trả `{ created }`. Mất vài giây (nạp sqlalchemy + lancedb) nên người gọi nên
   * báo cho người dùng biết là đang bận.
   */
  ensureSchema() {
    if (fs.existsSync(path.join(this.dataDir, 'sag.db'))) return { created: false, reason: 'already' };

    const res = spawnSync(
      this.pythonPath,
      ['-c', 'import asyncio; from sag_api.core.db import init_db; asyncio.run(init_db())'],
      { cwd: this.appDir, windowsHide: true, env: { ...process.env, ...this.env() }, encoding: 'utf8' }
    );
    if (res.status !== 0) {
      throw new Error(`Không dựng được brain rỗng: ${String(res.stderr || '').trim().slice(-500)}`);
    }
    return { created: true };
  }

  /**
   * Y hệt `ensureSchema()` nhưng KHÔNG chặn vòng lặp sự kiện.
   *
   * `spawnSync` nạp sqlalchemy + lancedb — vài giây trên máy ấm, lâu hơn nhiều ở lần
   * chạy đầu khi Windows Defender còn quét cả cây python vừa cài. Chạy nó trong tiến
   * trình main của Electron là ĐÓNG BĂNG TOÀN BỘ app: mọi IPC nằm chờ, nên ô chọn
   * model đứng mãi ở "(đang tải…)" và nút Tạo treo ở "Đang tạo…". Nhìn từ ngoài
   * giống hệt app chết.
   */
  ensureSchemaAsync() {
    if (fs.existsSync(path.join(this.dataDir, 'sag.db'))) {
      return Promise.resolve({ created: false, reason: 'already' });
    }
    return new Promise((resolve, reject) => {
      const child = spawn(
        this.pythonPath,
        ['-c', 'import asyncio; from sag_api.core.db import init_db; asyncio.run(init_db())'],
        { cwd: this.appDir, windowsHide: true, env: { ...process.env, ...this.env() }, stdio: ['ignore', 'pipe', 'pipe'] }
      );
      let stderr = '';
      child.stderr.on('data', (b) => { stderr += b.toString('utf8'); });
      child.on('error', (err) => reject(new Error(`Không chạy được python của brain: ${err.message}`)));
      child.on('close', (code) => {
        if (code === 0) resolve({ created: true });
        else reject(new Error(`Không dựng được brain rỗng: ${stderr.trim().slice(-500)}`));
      });
    });
  }

  /**
   * Khoá mã hoá credential provider trong bảng settings của brain (`core/crypto.py`
   * dùng AES-GCM). Sinh một lần rồi giữ nguyên: đổi khoá là mọi credential đã lưu
   * thành rác không giải được.
   */
  _secretKey() {
    const keyFile = path.join(this.dataDir, '.secret_key');
    if (fs.existsSync(keyFile)) return fs.readFileSync(keyFile, 'utf8').trim();
    const key = crypto.randomBytes(32).toString('hex');
    fs.mkdirSync(this.dataDir, { recursive: true });
    fs.writeFileSync(keyFile, key, { encoding: 'utf8', mode: 0o600 });
    return key;
  }

  /** Env dùng chung cho cả stdio MCP lẫn sidecar HTTP — một nguồn, khỏi lệch nhau. */
  env() {
    fs.mkdirSync(path.join(this.dataDir, 'engine'), { recursive: true });
    const e = {
      PYTHONPATH: this.appDir,
      PYTHONIOENCODING: 'utf-8', // M-0004: tiếng Việt trên Windows, không có dòng này là mojibake
      PYTHONUTF8: '1',
      SAG_DATABASE_URL: `sqlite+aiosqlite:///${path.join(this.dataDir, 'sag.db').replace(/\\/g, '/')}`,
      SAG_DATA_DIR: path.join(this.dataDir, 'engine'),
      SAG_LOG_DIR: path.join(this.dataDir, 'logs'),
      SAG_UPLOAD_DIR: path.join(this.dataDir, 'uploads'),
      SAG_SAG_VECTOR_PROVIDER: 'lancedb',
      SAG_SECRET_KEY: this._secretKey(),
      // `Settings.environment` là Literal['dev','prod'] — không có 'desktop'.
      // Bản đầu tự chế giá trị đó và server chết ngay lúc import config, trước cả
      // khi nói được câu MCP nào.
      SAG_ENVIRONMENT: 'prod',
      SAG_DESKTOP_HOST: this.settings.host || '127.0.0.1',
      SAG_DESKTOP_PORT: String(this.settings.port || 8931),
      // Dashboard (`runtime/webui`, Next.js) gọi API này từ TRÌNH DUYỆT, không
      // phải từ tiến trình main — CORS mặc định của sag_api chỉ cho
      // `localhost:3000` (dev port của apps/web gốc). Thiếu dòng này thì mọi
      // request từ dashboard bị trình duyệt CHẶN NGAY, hiện "Network error" dù cả
      // hai tiến trình đều đang chạy khoẻ mạnh (đo thật 2026-08-13). Next.js
      // redirect `/` → `/login` dùng hostname `localhost`, không phải
      // `127.0.0.1` dù server bind `127.0.0.1` — khai cả hai cho chắc.
      SAG_CORS_ORIGINS: 'http://localhost:8933,http://127.0.0.1:8933',
    };

    // `D-0054` mục 2: local hay API là lựa chọn của user, nói rõ đánh đổi trên wizard.
    // Không chọn hộ — nhưng cũng không im lặng chạy mà không có embedding, vì khi đó
    // search ngữ nghĩa hỏng trong khi app trông vẫn bình thường.
    const emb = this.settings.embedding;
    if (emb && emb.baseUrl) {
      e.SAG_EMBEDDING_BASE_URL = emb.baseUrl;
      e.SAG_EMBEDDING_MODEL = emb.model || 'bge-m3';
      if (emb.apiKey) e.SAG_EMBEDDING_API_KEY = emb.apiKey;
    }
    return e;
  }

  /**
   * Cấu hình MCP để nhét vào `opencode.json`.
   *
   * `D-0053` mục 3: `command[0]` là đường dẫn TUYỆT ĐỐI tới python nhúng trong app.
   * Không `python` trần, không `npx`, không phụ thuộc PATH của máy — đó là gốc bệnh
   * của `M-0035`.
   */
  mcpConfig() {
    if (!this.available) return null;
    return {
      type: 'local',
      command: [this.pythonPath, '-m', 'sag_api.mcp.server'],
      environment: { ...this.env(), SAG_MCP_ACTOR: 'alice-portable' },
      enabled: true,
      timeout: 120000,
    };
  }

  /**
   * Bật HTTP sidecar (`python -m sag_api.desktop`).
   *
   * Chỉ cần cho ingest/sync tri thức, KHÔNG cần cho recall. Mặc định tắt: một tiến
   * trình không dùng tới là một tiến trình có thể chết âm thầm rồi đổ lỗi cho chỗ khác.
   *
   * @param {object} [opts]
   * @param {number} [opts.timeoutMs]  chờ healthy bao lâu. Lần đầu mở brain của một
   *   Alice, Windows Defender quét cả cây python mới cài nên rất lâu — người gọi
   *   truyền 180s thay vì 30s mặc định (xem `alice:brain:open` trong main.js).
   */
  async start({ timeoutMs = 30000 } = {}) {
    if (!this.available) throw new Error(`Chưa đóng gói brain: thiếu ${this.pythonPath}`);
    if (!this.settings.http) return { started: false, reason: 'http-disabled' };
    if (this.proc) return { started: true, reason: 'already-running' };

    // Đã có một tiến trình sag_api đang phục vụ ĐÚNG cổng này (spawn lần trước bỏ
    // dở giữa chừng, hoặc sidecar của Alice này còn sống từ đợt mở trước) → dùng
    // luôn, không spawn cái thứ hai giành cổng. Chỉ tin khi header xác nhận là
    // uvicorn — một app khác đang chiếm cổng thì KHÔNG được mặc định "thế là xong".
    const host = this.settings.host || '127.0.0.1';
    const port = this.settings.port || 8931;
    if (await this._probe(`http://${host}:${port}/`)) {
      this.lastError = null;
      return { started: true, reason: 'already-serving' };
    }

    this.proc = spawn(this.pythonPath, ['-m', 'sag_api.desktop'], {
      cwd: this.appDir,
      windowsHide: true,
      // `stderr` phải có người đọc: python hỏng lúc import/startup sẽ in lỗi ra
      // đây, và câu lỗi đó phải tới tay người dùng thay cho "fetch failed" chung
      // chung. Bản trước để pipe mặc định mà không ai đọc — lỗi thật bị nuốt sạch.
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, ...this.env() },
    });
    let stderr = '';
    this.proc.stderr.on('data', (b) => { stderr += b.toString('utf8'); });
    this.proc.on('exit', (code) => {
      this.lastError = code === 0 ? null : `sidecar thoát mã ${code}`;
      this.proc = null;
    });

    try {
      await this._waitHealthy(timeoutMs, () => stderr);
    } catch (err) {
      // KHÔNG để lại tiến trình mồ côi: sidecar vẫn sống sau khi `start()` throw là
      // gốc của bug "bấm Xem Alice Brain mãi không mở" — main.js tưởng nó chết, lần
      // bấm sau lại stop() giết đúng cái vừa lên rồi spawn lại từ đầu. Giết ở đây,
      // đặt `this.proc = null`, rồi mới nói lỗi.
      try { this.proc.kill(); } catch { /* đã chết */ }
      this.proc = null;
      throw err;
    }
    return { started: true, reason: null };
  }

  async _waitHealthy(timeoutMs, stderrOf = () => '') {
    const url = `http://${this.settings.host || '127.0.0.1'}:${this.settings.port || 8931}/`;
    const deadline = Date.now() + timeoutMs;
    let last;
    while (Date.now() < deadline) {
      try {
        const res = await fetch(url, { signal: AbortSignal.timeout(3000) });
        if (res.ok || res.status === 404) return true;
        last = `HTTP ${res.status}`;
      } catch (err) {
        last = err.message;
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    // Lỗi THẬT từ python (import hỏng, thiếu thư viện, cổng bận…) nằm trong stderr —
    // nhét vào câu lỗi thay cho "fetch failed" không nói lên được gì.
    const why = stderrOf().trim().slice(-400);
    throw new Error(
      `Brain sidecar không lên sau ${timeoutMs}ms (${last})`
      + (why ? ` — ${why}` : '')
    );
  }

  /** Đúng cổng này có ai phục vụ và đó là uvicorn (sag_api) không. */
  async _probe(url) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(2000) });
      const server = String(res.headers.get('server') || '');
      return server.toLowerCase().includes('uvicorn');
    } catch {
      return false;
    }
  }

  stop() {
    if (this.proc) {
      this.proc.kill();
      this.proc = null;
    }
  }

  status() {
    return {
      enabled: this.settings.enabled !== false,
      available: this.available,
      python: this.pythonPath,
      app: this.appDir,
      data: this.dataDir,
      httpRunning: Boolean(this.proc),
      embedding: this.settings.embedding ? this.settings.embedding.mode : null,
      lastError: this.lastError,
    };
  }
}

module.exports = { BrainSidecar };
