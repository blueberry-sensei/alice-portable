'use strict';

/**
 * MCP server "report" — đúng giao thức đúng định dạng là sống còn: opencode lẫn
 * Claude Code đều từ chối im lặng khi server nói sai JSON-RPC.
 *
 * Spawn server THẬT bằng chính runtime của test (electron-as-node), đọc cấu hình
 * từ một report.json tạm, rồi gọi theo đúng chuỗi của một engine:
 * initialize → tools/list → tools/call.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { execFileSync } = require('node:child_process');

const SERVER = path.join(__dirname, '..', 'src', 'main', 'report', 'mcp-server.js');

function makeCfg(over = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-mcp-'));
  fs.writeFileSync(path.join(dir, 'report.json'), JSON.stringify({
    googleServiceAccount: '',
    googleSpace: '',
    planeBaseUrl: 'https://api.plane.so',
    planeApiKey: '',
    planeWorkspace: '',
    gitRepos: [],
    templatePath: '',
    outputDir: '',
    outputName: 'HRM_Weekly_Report',
    ...over,
  }));
  return path.join(dir, 'report.json');
}

/** Mở server, trả về hàm `call(method, params)` chờ đúng id trả lời. */
function startServer(configFile) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [SERVER], {
      env: { ...process.env, ELECTRON_RUN_AS_NODE: '1', REPORT_CONFIG_FILE: configFile },
      stdio: ['pipe', 'pipe', 'ignore'],
    });
    let buf = '';
    const waiters = new Map();
    let nextId = 1;
    child.stdout.on('data', (d) => {
      buf += d.toString('utf8');
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        let msg;
        try { msg = JSON.parse(line); } catch { continue; }
        if (msg.id !== undefined && waiters.has(msg.id)) {
          waiters.get(msg.id)(msg);
          waiters.delete(msg.id);
        }
      }
    });
    const call = (method, params) => new Promise((done) => {
      const id = nextId++;
      waiters.set(id, done);
      child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    });
    resolve({ child, call });
  });
}

/**
 * LUÔN giết server con khi xong — kể cả khi test fail. Quên kill một lần là lần
 * chạy test sau đó treo vô hạn: pipe stdout của đứa con còn mở, event loop của
 * tiến trình test không bao giờ rỗng để thoát.
 */
async function withServer(configFile, fn) {
  const { child, call } = await startServer(configFile);
  try {
    return await fn({ child, call });
  } finally {
    child.kill();
  }
}

test('mcp: initialize trả đúng thủ tục của engine', async () => {
  await withServer(makeCfg(), async ({ call }) => {
    const r = await call('initialize', { protocolVersion: '2024-11-05' });
    assert.equal(r.result.serverInfo.name, 'alice-report');
    assert.deepEqual(r.result.capabilities.tools, {});
    assert.equal(r.result.protocolVersion, '2024-11-05');
  });
});

test('mcp: tools/list khai đủ 4 tool báo cáo tuần', async () => {
  await withServer(makeCfg(), async ({ call }) => {
    const r = await call('tools/list', {});
    const names = r.result.tools.map((t) => t.name).sort();
    assert.deepEqual(names, ['chat_messages', 'export_pdf', 'git_commits', 'plane_issues']);
    for (const t of r.result.tools) {
      assert.ok(t.inputSchema && t.inputSchema.type === 'object', `${t.name} phải có inputSchema`);
    }
  });
});

test('mcp: gọi tool khi chưa cấu hình → isError nói rõ thiếu gì', async () => {
  await withServer(makeCfg(), async ({ call }) => {
    const r = await call('tools/call', { name: 'chat_messages', arguments: {} });
    assert.equal(r.result.isError, true, 'thiếu cấu hình là LỖI chứ không trả kết quả rỗng');
    assert.match(r.result.content[0].text, /googleServiceAccount/);
  });
});

test('mcp: git_commits chạy trên repo tạm — commit trong kỳ xuất hiện', async () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-mcp-repo-'));
  const run = (args) => execFileSync('git', args, { cwd: repo, stdio: 'ignore', windowsHide: true });
  run(['init', '-q']);
  run(['config', 'user.email', 't@t']);
  run(['config', 'user.name', 'Tester']);
  fs.writeFileSync(path.join(repo, 'a.txt'), 'x');
  run(['add', '.']);
  run(['commit', '-q', '-m', 'them tinh nang bao cao tuan']);
  const cfg = makeCfg({ gitRepos: [repo] });

  await withServer(cfg, async ({ call }) => {
    const r = await call('tools/call', { name: 'git_commits', arguments: {} });
    assert.equal(r.result.isError, false);
    assert.match(r.result.content[0].text, /them tinh nang bao cao tuan/);
    assert.match(r.result.content[0].text, /Tester/);
  });
  // Windows: git.exe/Defender còn giữ handle vài trăm ms sau khi thoát (EPERM) —
  // đã assert xong ở trên rồi, lỗi cleanup không được làm rớt một test đã đúng.
  try {
    fs.rmSync(repo, { recursive: true, force: true, maxRetries: 10, retryDelay: 300 });
  } catch {
    // Temp dir của OS tự dọn sau.
  }
});

test('mcp: git_commits khi repo lỗi → isError, không giết server', async () => {
  const cfg = makeCfg({ gitRepos: ['Z:\\khong-ton-tai\\repo'] });
  await withServer(cfg, async ({ call }) => {
    const r = await call('tools/call', { name: 'git_commits', arguments: {} });
    assert.equal(r.result.isError, true);
    const r2 = await call('ping', {});
    assert.ok(r2.result !== undefined, 'server phải còn sống sau một tool lỗi');
  });
});

test('mcp: export_pdf không có PDF sidecar → isError nhắc đúng chỗ chạy', async () => {
  await withServer(makeCfg(), async ({ call }) => {
    const r = await call('tools/call', {
      name: 'export_pdf',
      arguments: { markdown: '# Báo cáo\n\nNội dung.', outPath: 'C:\\tmp\\x.pdf' },
    });
    assert.equal(r.result.isError, true);
    assert.match(r.result.content[0].text, /PDF sidecar/);
  });
});

test('mcp: method lạ → lỗi -32601 đúng chuẩn JSON-RPC', async () => {
  await withServer(makeCfg(), async ({ call }) => {
    const r = await call('cach-nao-do', {});
    assert.equal(r.error.code, -32601);
  });
});