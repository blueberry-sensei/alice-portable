'use strict';

/**
 * PDF sidecar của báo cáo tuần — HTTP server (chỉ 127.0.0.1, cổng 8934) sống trong
 * MAIN process. Vì sao cần riêng: MCP server `report` chạy bằng `ELECTRON_RUN_AS_NODE`
 * (node thuần, không có BrowserWindow) nên không thể `printToPDF` — nó POST markdown
 * sang đây, main dựng hidden BrowserWindow để in ra PDF thật.
 *
 * Vào ngày nào đó Alice hỏi "in hộ tôi file này": chỉ cần cho MCP tool thứ 2 biết
 * nói chuyện với sidecar này (đang chạy khi app mở).
 */

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const PORT = 8934;
const MAX_BODY = 2 * 1024 * 1024;

class PdfExporter {
  constructor() {
    this.server = null;
    this.window = null;
  }

  /** Lazy — không chạy tới lúc Alice thật sự gọi export_pdf. */
  async start() {
    if (this.server) return true;
    const alive = await this._probe();
    if (alive) return true; // đã có sidecar (cửa sổ app khác / bản cũ còn sống)
    await new Promise((resolve, reject) => {
      const server = http.createServer((req, res) => this._handle(req, res));
      server.on('error', reject);
      server.listen(PORT, '127.0.0.1', () => {
        this.server = server;
        server.on('error', () => { this.server = null; });
        resolve(true);
      });
    });
    return true;
  }

  stop() {
    if (this.server) {
      try { this.server.close(); } catch { /* đã đóng */ }
      this.server = null;
    }
    if (this.window) {
      try { this.window.destroy(); } catch { /* đã đóng */ }
      this.window = null;
    }
  }

  /** In trực tiếp trong main (nút "Làm báo cáo ngay" — không qua HTTP). */
  async print(markdown, title, outPath) {
    const abs = this._resolveOut(outPath);
    const pages = await this._print(markdown, title, abs);
    return { path: abs, pages };
  }

  async _probe() {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/_alive`, { signal: AbortSignal.timeout(1500) });
      return res.ok;
    } catch {
      return false;
    }
  }

  _json(res, status, body) {
    const payload = JSON.stringify(body);
    res.writeHead(status, {
      'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': Buffer.byteLength(payload),
    });
    res.end(payload);
  }

  _readBody(req) {
    return new Promise((resolve, reject) => {
      let size = 0;
      const chunks = [];
      req.on('data', (c) => {
        size += c.length;
        if (size > MAX_BODY) { reject(new Error('Body quá lớn (giới hạn 2MB).')); req.destroy(); return; }
        chunks.push(c);
      });
      req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
      req.on('error', reject);
    });
  }

  async _handle(req, res) {
    if (req.method === 'GET' && req.url === '/_alive') {
      this._json(res, 200, { ok: true, pid: process.pid });
      return;
    }
    if (req.method !== 'POST' || req.url !== '/') {
      this._json(res, 404, { ok: false, error: 'Chỉ nhận POST /' });
      return;
    }
    let body;
    try {
      body = JSON.parse(await this._readBody(req));
    } catch (e) {
      this._json(res, 400, { ok: false, error: `Body lỗi: ${e.message}` });
      return;
    }
    const markdown = String(body.markdown || '');
    if (!markdown.trim()) {
      this._json(res, 400, { ok: false, error: 'Thiếu markdown.' });
      return;
    }
    try {
      const outPath = this._resolveOut(body.outPath);
      const pages = await this._print(markdown, String(body.title || ''), outPath);
      this._json(res, 200, { ok: true, path: outPath, pages });
    } catch (e) {
      this._json(res, 500, { ok: false, error: String(e.message || e) });
    }
  }

  _resolveOut(outPath) {
    if (!outPath) throw new Error('Thiếu outPath — báo Alice dùng đường dẫn từ cấu hình.');
    const abs = path.resolve(outPath);
    fs.mkdirSync(path.dirname(abs), { recursive: true });
    return abs;
  }

  /** Markdown → HTML tối giản: đủ cho báo cáo tuần (heading, bôi đậm, list, code). */
  mdToHtml(md) {
    const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const inline = (s) => s
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
    const lines = md.replace(/\r\n/g, '\n').split('\n');
    const out = [];
    let list = null; // 'ul' | 'ol' | null
    let code = null;
    const closeList = () => {
      if (list) { out.push(`</${list}>`); list = null; }
    };
    for (const raw of lines) {
      if (raw.startsWith('```')) {
        closeList();
        if (code) { out.push('</pre>'); code = null; }
        else { out.push('<pre>'); code = true; }
        continue;
      }
      if (code) { out.push(esc(raw)); continue; }
      const h = raw.match(/^(#{1,6})\s+(.*)$/);
      if (h) { closeList(); out.push(`<h${h[1].length}>${inline(esc(h[2]))}</h${h[1].length}>`); continue; }
      if (/^\s*[-*]\s+/.test(raw)) {
        if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
        out.push(`<li>${inline(esc(raw.replace(/^\s*[-*]\s+/, '')))}</li>`);
        continue;
      }
      const ol = raw.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ol) {
        if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; }
        out.push(`<li>${inline(esc(ol[1]))}</li>`);
        continue;
      }
      closeList();
      if (/^\s*---+\s*$/.test(raw)) { out.push('<hr>'); continue; }
      if (raw.trim() === '') continue;
      out.push(`<p>${inline(esc(raw.trim()))}</p>`);
    }
    closeList();
    if (code) out.push('</pre>');
    return out.join('\n');
  }

  async _print(markdown, title, outPath) {
    const { BrowserWindow } = require('electron');
    const escTitle = title.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const html = `<!doctype html><html lang="vi"><head><meta charset="utf-8">
<style>
  @page { size: A4; margin: 18mm 16mm; }
  body { font-family: "Segoe UI", Arial, sans-serif; font-size: 12px; line-height: 1.55; color: #111; }
  h1 { font-size: 20px; border-bottom: 2px solid #1a73e8; padding-bottom: 6px; }
  h2 { font-size: 15px; margin-top: 18px; color: #1a73e8; }
  h3 { font-size: 13px; margin-top: 12px; }
  pre { background: #f4f4f4; padding: 8px; font-size: 10px; border-radius: 4px; white-space: pre-wrap; }
  code { background: #f4f4f4; padding: 0 3px; border-radius: 3px; font-size: 11px; }
  li { margin: 2px 0; }
  hr { border: none; border-top: 1px solid #ccc; }
  a { color: #1a73e8; }
</style></head><body>${title ? `<h1>${escTitle}</h1>` : ''}${this.mdToHtml(markdown)}</body></html>`;

    const win = new BrowserWindow({
      show: false,
      width: 900,
      height: 1200,
      webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true },
    });
    this.window = win;
    try {
      await win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
      await new Promise((r) => setTimeout(r, 300)); // chờ layout ổn định
      const pdf = await win.webContents.printToPDF({
        printBackground: true,
        pageSize: 'A4',
        margins: { top: 0.5, bottom: 0.5, left: 0.4, right: 0.4 },
      });
      fs.writeFileSync(outPath, pdf);
      const latin = pdf.toString('latin1');
      const pages = (latin.match(/\/Type\s*\/Page\b/g) || []).length
        - (latin.match(/\/Type\s*\/Pages\b/g) || []).length;
      return Math.max(pages, 1);
    } finally {
      try { win.destroy(); } catch { /* đã đóng */ }
      this.window = null;
    }
  }
}

module.exports = { PdfExporter, PORT };
