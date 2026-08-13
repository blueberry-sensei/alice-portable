'use strict';

/**
 * Tunnel — đưa máy chủ của một Alice ra INTERNET, không cần mở port trên router.
 *
 * Vì sao cần: `http://192.168.1.x:8931` chỉ máy CÙNG WIFI mới vào được. Muốn gửi
 * link cho người ở nhà khác thì phải NAT/port-forward — thứ không ai ngoài dân
 * mạng làm được, và làm xong là mở thẳng máy mình ra Internet.
 *
 * Chọn `cloudflared` (Cloudflare Tunnel) thay vì ngrok:
 *   - quick tunnel KHÔNG cần tài khoản, không cần authtoken, không giới hạn phiên;
 *     ngrok bắt đăng ký, phát một authtoken, và bản free đổi domain mỗi lần chạy
 *     kèm trang cảnh báo chen giữa — người được chia sẻ sẽ tưởng là lừa đảo;
 *   - kết nối là do MÁY MÌNH gọi RA (outbound), nên không mở một port nào trên
 *     router; Cloudflare chỉ chuyển tiếp, không có đường nào vào máy ngoài đúng
 *     cái port đã trỏ;
 *   - có sẵn HTTPS, nên mật khẩu / mã truy cập không đi qua mạng ở dạng trần.
 *
 * Một file thực thi, không thêm dependency npm nào.
 *
 * CẢNH BÁO đã đóng vào luật ở `main.js`: link tunnel là link CÔNG KHAI trên
 * Internet. Không bao giờ được bật tunnel khi Alice đang ở mode `anyone` — khi đó
 * bất kỳ ai đoán trúng URL là chat được và đốt API key của chủ máy.
 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const https = require('node:https');
const { spawn, spawnSync } = require('node:child_process');

/** Tên file cloudflared cho từng hệ điều hành trên GitHub release của Cloudflare. */
function assetName() {
  const arch = process.arch === 'arm64' ? 'arm64' : (process.arch === 'ia32' ? '386' : 'amd64');
  if (process.platform === 'win32') return `cloudflared-windows-${arch === 'arm64' ? 'amd64' : arch}.exe`;
  if (process.platform === 'darwin') return `cloudflared-darwin-${arch}.tgz`;
  return `cloudflared-linux-${arch}`;
}

const RELEASE_BASE = 'https://github.com/cloudflare/cloudflared/releases/latest/download/';
const BIN_NAME = process.platform === 'win32' ? 'cloudflared.exe' : 'cloudflared';

class Tunnel {
  /**
   * @param {object} opts.resourcesDir  thư mục `runtime/` (bản nhúng, nếu có)
   * @param {string} opts.toolsDir      nơi tải cloudflared về khi máy chưa có
   * @param {object} opts.log           logger
   */
  constructor(opts = {}) {
    this.resourcesDir = opts.resourcesDir || null;
    this.toolsDir = opts.toolsDir || path.join(os.tmpdir(), 'alice-tools');
    this.log = opts.log || { info() {}, error() {} };
    this.proc = null;
    this.url = null;
    this.port = null;
    this.lastError = null;
    this.starting = false;
  }

  get running() {
    return Boolean(this.proc && this.url);
  }

  status() {
    return {
      running: this.running,
      starting: this.starting,
      url: this.url,
      port: this.port,
      binary: this.resolveBinary(),
      error: this.lastError,
    };
  }

  /**
   * Tìm cloudflared: bản nhúng trong `runtime/` → bản đã tải về `tools/` → máy có
   * sẵn trên PATH. Trả `null` nếu chưa có ở đâu cả (khi đó UI mời tải).
   */
  resolveBinary() {
    const candidates = [];
    if (this.resourcesDir) candidates.push(path.join(this.resourcesDir, 'cloudflared', BIN_NAME));
    candidates.push(path.join(this.toolsDir, BIN_NAME));
    for (const c of candidates) {
      if (fs.existsSync(c)) return c;
    }
    // Trên PATH? `--version` là lệnh rẻ nhất và không đụng mạng.
    try {
      const probe = spawnSync(BIN_NAME, ['--version'], { encoding: 'utf8', timeout: 5000 });
      if (probe.status === 0) return BIN_NAME;
    } catch { /* không có trên PATH */ }
    return null;
  }

  /**
   * Tải cloudflared về `toolsDir`. ~35 MB, một lần cho mỗi máy.
   * @param {(pct:number|null)=>void} onProgress
   */
  async download(onProgress = () => {}) {
    fs.mkdirSync(this.toolsDir, { recursive: true });
    const url = RELEASE_BASE + assetName();
    const isTgz = url.endsWith('.tgz');
    const tmp = path.join(this.toolsDir, isTgz ? 'cloudflared.tgz' : `${BIN_NAME}.part`);

    this.log.info(`tunnel: tải cloudflared từ ${url}`);
    await fetchToFile(url, tmp, onProgress);

    if (isTgz) {
      // macOS: `tar` có sẵn trong hệ điều hành, không cần thư viện giải nén.
      const r = spawnSync('tar', ['-xzf', tmp, '-C', this.toolsDir], { encoding: 'utf8' });
      fs.rmSync(tmp, { force: true });
      if (r.status !== 0) throw new Error(`Không giải nén được cloudflared: ${r.stderr || r.status}`);
    } else {
      fs.renameSync(tmp, path.join(this.toolsDir, BIN_NAME));
    }
    if (process.platform !== 'win32') {
      fs.chmodSync(path.join(this.toolsDir, BIN_NAME), 0o755);
    }
    this.log.info(`tunnel: cloudflared sẵn sàng tại ${path.join(this.toolsDir, BIN_NAME)}`);
    return path.join(this.toolsDir, BIN_NAME);
  }

  /**
   * Mở tunnel tới `http://127.0.0.1:<port>`. Trả về URL công khai (https).
   * Chờ tối đa 45s — cloudflared in URL ra stderr sau khi bắt tay xong.
   */
  async start(port) {
    if (this.running) return { url: this.url };
    const bin = this.resolveBinary();
    if (!bin) throw new Error('Chưa có cloudflared trên máy — bấm "Tải cloudflared" trước.');

    this.starting = true;
    this.lastError = null;
    this.port = Number(port);

    return new Promise((resolve, reject) => {
      const args = [
        'tunnel', '--no-autoupdate',
        '--url', `http://127.0.0.1:${this.port}`,
      ];
      const proc = spawn(bin, args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
      this.proc = proc;

      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        this.starting = false;
        this.stop();
        reject(new Error('cloudflared không trả về địa chỉ sau 45 giây — kiểm tra mạng rồi thử lại.'));
      }, 45000);

      const scan = (chunk) => {
        const s = chunk.toString('utf8');
        const m = s.match(/https:\/\/[a-z0-9-]+\.trycloudflare\.com/i);
        if (m && !settled) {
          settled = true;
          clearTimeout(timer);
          this.url = m[0];
          this.starting = false;
          this.log.info(`tunnel UP: ${this.url} → 127.0.0.1:${this.port}`);
          resolve({ url: this.url });
        }
      };
      proc.stdout.on('data', scan);
      proc.stderr.on('data', scan);

      proc.on('error', (err) => {
        this.lastError = err.message;
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this.starting = false;
        this.proc = null;
        reject(new Error(`Không chạy được cloudflared: ${err.message}`));
      });

      proc.on('exit', (code) => {
        this.log.info(`tunnel DOWN (exit ${code})`);
        this.proc = null;
        this.url = null;
        this.starting = false;
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error(`cloudflared thoát sớm (mã ${code}).`));
        }
      });
    });
  }

  stop() {
    if (this.proc) {
      try { this.proc.kill(); } catch { /* đã chết */ }
      this.proc = null;
    }
    this.url = null;
    this.starting = false;
  }
}

/** GET có theo redirect (GitHub release luôn 302 sang objects.githubusercontent.com). */
function fetchToFile(url, dest, onProgress, depth = 0) {
  if (depth > 6) return Promise.reject(new Error('Quá nhiều lần chuyển hướng khi tải cloudflared.'));
  return new Promise((resolve, reject) => {
    https.get(url, { headers: { 'User-Agent': 'alice-portable' } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        fetchToFile(res.headers.location, dest, onProgress, depth + 1).then(resolve, reject);
        return;
      }
      if (res.statusCode !== 200) {
        res.resume();
        reject(new Error(`Tải cloudflared hỏng: HTTP ${res.statusCode}`));
        return;
      }
      const total = Number(res.headers['content-length'] || 0);
      let got = 0;
      const out = fs.createWriteStream(dest);
      res.on('data', (c) => {
        got += c.length;
        if (total) onProgress(Math.round((got / total) * 100));
      });
      res.pipe(out);
      out.on('finish', () => out.close(() => resolve(dest)));
      out.on('error', reject);
    }).on('error', reject);
  });
}

module.exports = { Tunnel, assetName };
