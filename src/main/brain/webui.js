'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

const config = require('../config');

/**
 * Dashboard THẬT của Alice Brain (`apps/web` gốc, đã build sẵn thành Next.js
 * standalone — xem `docs/superpowers/specs/2026-08-13-brain-dashboard-design.md`).
 *
 * Chạy bằng CHÍNH Node của Electron (`ELECTRON_RUN_AS_NODE=1`) — khỏi phải nhúng
 * thêm một bản Node.js riêng chỉ để chạy một file `server.js`.
 *
 * `NEXT_PUBLIC_API_BASE` đã đóng cứng lúc build (đọc kỹ trong spec: Next.js inline
 * biến `NEXT_PUBLIC_*` vào bundle client lúc `next build`, không đọc lại lúc chạy),
 * nên server này CHỈ chạy được trên ĐÚNG cổng API đã build cùng — không tự đổi
 * được ở runtime. Muốn xem brain của Alice khác thì đổi Alice nào đang chạy
 * `sag_api.desktop` trên cổng đó, không đổi cổng của dashboard.
 */
const WEB_PORT = 8933;

class NextDashboard {
  constructor(paths = {}) {
    this.runtimeDir = paths.runtimeDir || path.join(config.RESOURCES_DIR, 'webui');
    this.serverFile = path.join(this.runtimeDir, 'server.js');
    this.proc = null;
    this.lastError = null;
  }

  get available() {
    return fs.existsSync(this.serverFile);
  }

  get running() {
    return Boolean(this.proc);
  }

  /** Khởi nếu chưa chạy — gọi lại khi đã chạy là no-op, KHÔNG khởi động lại. */
  async start() {
    if (!this.available) throw new Error(`Chưa đóng gói dashboard: thiếu ${this.serverFile}`);
    if (this.proc) return { started: true, reason: 'already-running' };

    this.proc = spawn(process.execPath, [this.serverFile], {
      cwd: this.runtimeDir,
      windowsHide: true,
      env: {
        ...process.env,
        ELECTRON_RUN_AS_NODE: '1',
        PORT: String(WEB_PORT),
        HOSTNAME: '127.0.0.1',
      },
    });
    this.proc.on('exit', (code) => {
      this.lastError = code === 0 ? null : `dashboard thoát mã ${code}`;
      this.proc = null;
    });

    await this._waitHealthy(30000);
    return { started: true, reason: null };
  }

  async _waitHealthy(timeoutMs) {
    const url = `http://127.0.0.1:${WEB_PORT}/`;
    const deadline = Date.now() + timeoutMs;
    let last;
    while (Date.now() < deadline) {
      try {
        const res = await fetch(url, { signal: AbortSignal.timeout(3000) });
        // Bất kỳ mã nào Next.js tự trả (kể cả redirect sang /login) đều nghĩa là
        // server đã lên — không cần đăng nhập mới coi là "sẵn sàng".
        if (res.status < 500) return true;
        last = `HTTP ${res.status}`;
      } catch (err) {
        last = err.message;
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    throw new Error(`Dashboard không lên sau ${timeoutMs}ms (${last})`);
  }

  stop() {
    if (this.proc) {
      this.proc.kill();
      this.proc = null;
    }
  }

  get url() {
    return `http://127.0.0.1:${WEB_PORT}/`;
  }
}

module.exports = { NextDashboard, WEB_PORT };
