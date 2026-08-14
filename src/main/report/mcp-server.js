'use strict';

/**
 * MCP stdio server "report" — Alice gọi các tool này để làm báo cáo tuần.
 *
 * Chạy bằng CHÍNH electron trong chế độ node (`ELECTRON_RUN_AS_NODE=1`, xem
 * buildReportMcp trong report/config.js), đọc cấu hình từ `REPORT_CONFIG_FILE`
 * tại MỖI lần gọi — đổi settings trong UI không cần khởi động lại.
 *
 * `export_pdf` đặc biệt: tiến trình này là node thuần, KHÔNG có BrowserWindow —
 * nên nó gọi HTTP sidecar trong main (port 8934) là nơi duy nhất có quyền
 * `printToPDF` (xem src/main/report/pdf.js).
 */

const path = require('node:path');
const readline = require('node:readline');
const { load, lastThursday } = require('./config');
const { gitLog, planeIssues, chatMessages } = require('./collector');

const VERSION = '0.1.0';
const PDF_SIDECAR = 'http://127.0.0.1:8934/';

function cfg() {
  // REPORT_CONFIG_FILE là ĐƯỜNG DẪN ĐẦY ĐỦ tới report.json — còn `load` nhận
  // thư mục chứa nó, nên phải dirname, không thì "report.json/report.json" (lỗi
  // ENOENT → cấu hình rỗng → "Chưa cấu hình gitRepos" dù đã cấu hình).
  const f = process.env.REPORT_CONFIG_FILE || '';
  return f ? load(path.dirname(f)) : load('');
}

function fmtDate(since) {
  return since ? `${since} → hôm nay` : `thứ 5 tuần trước (${lastThursday()}) → hôm nay`;
}

function rowsText(rows, render) {
  if (!rows.length) return '(không có dòng nào trong khoảng thời gian này)';
  return rows.map(render).join('\n');
}

const TOOLS = [
  {
    name: 'git_commits',
    description: `Liệt kê commit của các repo git local (không cần token). Mặc định tính từ thứ 5 tuần trước. Repo từ cấu hình report.json của Alice.`,
    inputSchema: {
      type: 'object',
      properties: {
        since: { type: 'string', description: 'Mốc ngày YYYY-MM-DD (bao gồm). Bỏ trống = thứ 5 tuần trước.' },
      },
    },
  },
  {
    name: 'plane_issues',
    description: `Liệt kê tasks trên Plane đã được cập nhật từ mốc `,
    inputSchema: {
      type: 'object',
      properties: {
        since: { type: 'string', description: 'Mốc ngày YYYY-MM-DD (bao gồm). Bỏ trống = thứ 5 tuần trước.' },
      },
    },
  },
  {
    name: 'chat_messages',
    description: `Đọc tin nhắn Google Chat của space trong cấu hình, từ mốc thời gian (mặc định thứ 5 tuần trước). CHỈ ĐỌC — tuyệt đối không gửi tin nhắn đi.`,
    inputSchema: {
      type: 'object',
      properties: {
        since: { type: 'string', description: 'Mốc ngày YYYY-MM-DD (bao gồm). Bỏ trống = thứ 5 tuần trước.' },
        limit: { type: 'number', description: 'Tối đa số tin nhắn trả về (mặc định 500).' },
      },
    },
  },
  {
    name: 'export_pdf',
    description: `In văn bản markdown ra file PDF (A4) và trả về đường dẫn. Dùng làm bước cuối của báo cáo tuần.`,
    inputSchema: {
      type: 'object',
      required: ['markdown'],
      properties: {
        markdown: { type: 'string', description: 'Nội dung báo cáo dạng markdown.' },
        title: { type: 'string', description: 'Tiêu đề in ở đầu PDF.' },
        outPath: { type: 'string', description: 'Đường dẫn file PDF đích. Bỏ trống = thư mục outputDir trong cấu hình.' },
      },
    },
  },
];

function defaultOutPath(c) {
  const dir = c.outputDir || process.cwd();
  const stamp = new Date().toISOString().slice(0, 10);
  const safe = (c.outputName || 'HRM_Weekly_Report').replace(/[\\/:*?"<>|]/g, '_');
  return path.join(dir, `${safe} ${stamp}.pdf`);
}

async function callTool(name, args = {}) {
  const c = cfg();
  const since = args.since || lastThursday();

  switch (name) {
    case 'git_commits': {
      if (!c.gitRepos.length) {
        return err('Chưa cấu hình gitRepos — thêm đường dẫn repo vào Báo cáo tuần trong Settings.');
      }
      const blocks = [];
      for (const repo of c.gitRepos) {
        const { rows, error } = gitLog(repo, since);
        if (error) return err(error);
        blocks.push(
          `### ${repo}\n`
          + `${rowsText(rows, (r) => `- \`${r.hash}\` ${r.subject} — ${r.author} (${(r.date || '').slice(0, 10)})`)}`
        );
      }
      return ok(`**Commits** (${fmtDate(since)})\n\n${blocks.join('\n\n')}`);
    }
    case 'plane_issues': {
      if (!c.planeApiKey) return err('Chưa cấu hình planeApiKey — thêm vào Báo cáo tuần trong Settings.');
      const { rows, error } = await planeIssues(c, since);
      if (error) return err(error);
      return ok(
        `**Plane tasks** (${fmtDate(since)})\n\n`
        + rowsText(rows, (r) => `- ${r.identifier} [${r.state || '?'}/${r.priority || '?'}] ${r.name} — cập nhật ${r.updatedAt}${r.assignees.length ? ` (${r.assignees.join(', ')})` : ''}`)
      );
    }
    case 'chat_messages': {
      if (!c.googleServiceAccount) return err('Chưa cấu hình googleServiceAccount — thêm vào Báo cáo tuần trong Settings.');
      const { rows, error } = await chatMessages({
        credentialsPath: c.googleServiceAccount,
        space: c.googleSpace,
        since,
        limit: Math.min(Number(args.limit) || 500, 1000),
      });
      if (error) return err(error);
      const lines = rows.map((m) => {
        const text = String(m.text || '').replace(/\s+/g, ' ').slice(0, 300);
        return `- [${(m.time || '').slice(0, 16).replace('T', ' ')}] ${m.author || '?'}: ${text}`;
      });
      return ok(`**Google Chat** (${fmtDate(since)})\n\n${lines.length ? lines.join('\n') : '(không có tin nhắn trong khoảng thời gian này)'}`);
    }
    case 'export_pdf': {
      const md = String(args.markdown || '');
      if (!md.trim()) return err('export_pdf cần markdown.');
      const outPath = args.outPath || defaultOutPath(c);
      let res;
      try {
        res = await fetch(PDF_SIDECAR, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ markdown: md, title: args.title || '', outPath }),
          signal: AbortSignal.timeout(120000),
        });
      } catch (e) {
        return err(`PDF sidecar không phản hồi (${e.message}) — nếu Alice chạy ngoài app (vd claude CLI tay), xuất PDF sẽ không có; hãy dùng Alice trong app hoặc kể lại nội dung báo cáo.`);
      }
      let body;
      try { body = await res.json(); } catch { body = {}; }
      if (!res.ok || !body.ok) {
        return err(`PDF sidecar lỗi: HTTP ${res.status} ${body.error || ''}`);
      }
      return ok(`Đã in PDF: **${body.path}** (${body.pages} trang). Báo cáo xong.`);
    }
    default:
      return err(`Tool lạ: ${name}`);
  }
}

function ok(text) {
  return { content: [{ type: 'text', text }], isError: false };
}
function err(text) {
  return { content: [{ type: 'text', text: `LỖI: ${text}` }], isError: true };
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
  let req;
  try {
    req = JSON.parse(line);
  } catch {
    return; // dòng rác từ engine — bỏ qua
  }
  if (typeof req.id === 'undefined' || req.id === null) return; // notification
  let result;
  let error = null;
  try {
    switch (req.method) {
      case 'initialize':
        result = {
          protocolVersion: '2024-11-05',
          capabilities: { tools: {} },
          serverInfo: { name: 'alice-report', version: VERSION },
        };
        break;
      case 'notifications/initialized':
        result = {};
        break;
      case 'tools/list':
        result = { tools: TOOLS };
        break;
      case 'tools/call':
        result = await callTool(String(req.params && req.params.name || ''), req.params && req.params.arguments || {});
        break;
      case 'ping':
        result = {};
        break;
      default:
        error = { code: -32601, message: `Method không tồn tại: ${req.method}` };
    }
  } catch (e) {
    error = { code: -32603, message: String(e.message || e) };
  }
  const resp = { jsonrpc: '2.0', id: req.id };
  if (error) resp.error = error;
  else resp.result = result;
  process.stdout.write(`${JSON.stringify(resp)}\n`);
});
