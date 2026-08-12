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
    this.pythonPath = path.join(this.runtimeDir, 'python', 'python.exe');
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
   */
  async start() {
    if (!this.available) throw new Error(`Chưa đóng gói brain: thiếu ${this.pythonPath}`);
    if (!this.settings.http) return { started: false, reason: 'http-disabled' };
    if (this.proc) return { started: true, reason: 'already-running' };

    this.proc = spawn(this.pythonPath, ['-m', 'sag_api.desktop'], {
      cwd: this.appDir,
      windowsHide: true,
      env: { ...process.env, ...this.env() },
    });
    this.proc.on('exit', (code) => {
      this.lastError = code === 0 ? null : `sidecar thoát mã ${code}`;
      this.proc = null;
    });

    await this._waitHealthy(30000);
    return { started: true, reason: null };
  }

  async _waitHealthy(timeoutMs) {
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
    throw new Error(`Brain sidecar không lên sau ${timeoutMs}ms (${last})`);
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
