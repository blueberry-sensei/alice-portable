'use strict';

const { spawn } = require('node:child_process');
const readline = require('node:readline');
const path = require('node:path');
const crypto = require('node:crypto');

const CLAUDE_MODELS = [
  'claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5-20251001', 'claude-fable-5',
];

class CancelledError extends Error {
  constructor() {
    super('Đã dừng theo yêu cầu.');
    this.cancelled = true;
  }
}

/**
 * Bọc CLI `claude` (Claude Code) thành cùng interface với `OpencodeEngine`, để
 * `turn.js`/`memory.js` không phải biết đang nói chuyện với engine nào.
 *
 * Khác `OpencodeEngine` ở hai chỗ CỐ Ý:
 *   - Không có "xoay nhiều model free khi hỏng" — subscription Claude không có khái
 *     niệm đó. `runWithFallback` chỉ là `run()` một lần, `attempts` luôn rỗng.
 *   - `listModels()` trả danh sách CỐ ĐỊNH — `claude` không có lệnh liệt kê model
 *     kiểu `opencode models`.
 *
 * Auth: cô lập theo `CLAUDE_CONFIG_DIR = <baseDir>/claude-config` — xác minh THẬT
 * (2026-08-13): set biến này trỏ thư mục trống thì `claude auth status` báo
 * `loggedIn: false`, hoàn toàn tách khỏi phiên đăng nhập global của máy.
 */
class ClaudeEngine {
  constructor(settings) {
    this.settings = settings || {};
    this.baseDir = null;
    this._cancelled = false;
    this._child = null;
    // Cho test ghi đè: trỏ vào script giả (node + script) thay vì `claude` thật.
    this.binPath = 'claude';
    this.binSource = 'host'; // `claude` luôn cài qua PATH, không có bản nhúng như opencode
    this._binArgs = [];
    this._extraEnv = {};
  }

  setBaseDir(dir) {
    this.baseDir = dir;
  }

  get available() {
    // Không có một đường dẫn cố định để `fs.existsSync` (CLI cài qua PATH) — coi là
    // "có khả năng dùng được" và để `run()` báo lỗi rõ ràng nếu spawn hỏng thật.
    return true;
  }

  /** Danh sách cố định — xem docstring class. */
  async listModels() {
    return CLAUDE_MODELS.slice();
  }

  /** Không có chuỗi model để xoay — gọi `run()` một lần, giữ contract `attempts: []`
   * để `turn.js` không phải rẽ nhánh theo engine. */
  async runWithFallback(opts) {
    this._cancelled = false;
    const out = await this.run(opts);
    return { ...out, attempts: [] };
  }

  /**
   * @param {object} opts
   * @param {string} opts.message
   * @param {string?} opts.sessionId  session id CỦA CHÍNH `claude` (không phải `ses_xxx`
   *   của opencode) — `null` = phiên mới.
   * @param {string?} opts.model
   * @param {string} opts.cwd
   * @param {function} [opts.onEvent]  (ev, partialText) — `ev` cùng hình dạng với
   *   event của `OpencodeEngine` (`{type:'text'|'tool', part}`) để
   *   `activity.js#toolActivity` dùng chung, không cần biết engine nào.
   */
  run({ message, sessionId = null, model = null, cwd, onEvent = null }) {
    if (this._cancelled) return Promise.reject(new CancelledError());

    const sessionArgs = sessionId ? ['--resume', sessionId] : ['--session-id', crypto.randomUUID()];
    const modelArgs = model ? ['--model', model] : [];
    const args = [
      ...this._binArgs,
      '--print', '--output-format', 'stream-json', '--verbose',
      '--include-partial-messages',
      '--dangerously-skip-permissions',
      ...sessionArgs, ...modelArgs,
      message,
    ];

    return new Promise((resolve, reject) => {
      const child = spawn(this.binPath, args, {
        cwd,
        windowsHide: true,
        env: {
          ...process.env,
          ...(this.baseDir ? { CLAUDE_CONFIG_DIR: path.join(this.baseDir, 'claude-config') } : {}),
          ...this._extraEnv,
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      this._child = child;

      let text = '';
      let resolvedSession = sessionId;
      let stderr = '';
      let resultLine = null;
      const toolNames = new Map(); // block index → tên tool, để đóng event `tool` đúng tên

      const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
      rl.on('line', (raw) => {
        const t = raw.trim();
        if (!t.startsWith('{')) return;
        let ev;
        try { ev = JSON.parse(t); } catch { return; }

        if (ev.type === 'system' && ev.session_id) resolvedSession = ev.session_id;

        if (ev.type === 'stream_event' && ev.event) {
          const se = ev.event;
          if (se.type === 'content_block_start' && se.content_block
              && se.content_block.type === 'tool_use') {
            toolNames.set(se.index, se.content_block.name);
            if (onEvent) {
              onEvent({ type: 'tool', part: {
                tool: se.content_block.name, id: se.content_block.id,
                callID: se.content_block.id, state: { status: 'running' },
              } }, text);
            }
          } else if (se.type === 'content_block_delta' && se.delta
              && se.delta.type === 'text_delta') {
            text += se.delta.text;
            if (onEvent) onEvent({ type: 'text', part: { id: `t${se.index}`, text } }, text);
          } else if (se.type === 'content_block_stop' && toolNames.has(se.index)) {
            const name = toolNames.get(se.index);
            if (onEvent) {
              onEvent({ type: 'tool', part: {
                tool: name, id: `${se.index}`, callID: `${se.index}`,
                state: { status: 'completed' },
              } }, text);
            }
          }
        }

        if (ev.type === 'result') resultLine = ev;
      });

      child.stderr.on('data', (b) => { stderr += b.toString('utf8'); });

      child.on('error', (err) => {
        this._child = null;
        reject(new Error(`Không chạy được claude: ${err.message}`));
      });

      child.on('close', (code) => {
        this._child = null;
        if (this._cancelled) { reject(new CancelledError()); return; }
        if (!resultLine) {
          reject(new Error(
            `claude thoát mã ${code} không có dòng result: ${stderr.trim().slice(0, 500) || '(không có stderr)'}`
          ));
          return;
        }
        if (resultLine.is_error) {
          reject(new Error(resultLine.result || 'claude báo lỗi không rõ'));
          return;
        }
        let tokens = null;
        if (resultLine.usage) {
          tokens = {
            input: resultLine.usage.input_tokens || 0,
            output: resultLine.usage.output_tokens || 0,
            cache: { read: resultLine.usage.cache_read_input_tokens || 0 },
          };
        }
        resolve({
          sessionId: resultLine.session_id || resolvedSession,
          text: resultLine.result != null ? resultLine.result : text,
          tokens, model: model || null, events: [],
        });
      });
    });
  }

  /** Giết cả cây tiến trình — `claude` có thể đẻ tiến trình con. */
  cancel() {
    this._cancelled = true;
    if (this._child) {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/F', '/T', '/PID', String(this._child.pid)], { windowsHide: true });
      } else {
        this._child.kill();
      }
      this._child = null;
      return true;
    }
    return false;
  }
}

module.exports = { ClaudeEngine, CancelledError, CLAUDE_MODELS };
