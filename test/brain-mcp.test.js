'use strict';

/**
 * Nghiệm thu brain: MCP stdio thật sự bắt tay được và phơi ra đúng bộ tool recall.
 *
 * `D-0053` mục 2 cấm giảm năng lực recall khi bỏ Docker. Câu đó chỉ kiểm được bằng
 * cách bắt tay thật với server — "python import được" mới chỉ chứng minh file có
 * mặt, chưa chứng minh tool nào chạy.
 *
 * Đây cũng chính xác là thứ opencode sẽ làm khi đọc `opencode.json`: spawn
 * `<python nhúng> -m sag_api.mcp.server` rồi nói JSON-RPC qua stdin/stdout.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { spawn } = require('node:child_process');

const { BrainSidecar } = require('../src/main/brain/sidecar');

const brain = new BrainSidecar({ enabled: true, host: '127.0.0.1', port: 8931 });
const skip = brain.available ? false : 'chưa chạy scripts/bundle-brain.ps1';

// Bộ tool mà RETRIEVAL.md của Alice dựa vào. Thiếu một cái là recall khuyết một góc.
const MUST_HAVE = ['list_sources', 'search', 'grep', 'get_entity', 'list_documents', 'read'];

function mcpHandshake(cfg, timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    const [cmd, ...args] = cfg.command;
    const child = spawn(cmd, args, {
      env: { ...process.env, ...cfg.environment },
      windowsHide: true,
    });

    let buf = '';
    let stderr = '';
    const seen = new Map();
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`Quá ${timeoutMs}ms. stderr: ${stderr.slice(-800)}`));
    }, timeoutMs);

    const say = (obj) => child.stdin.write(JSON.stringify(obj) + '\n');

    child.stdout.on('data', (b) => {
      buf += b.toString('utf8');
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line.startsWith('{')) continue;
        let msg;
        try { msg = JSON.parse(line); } catch { continue; }
        if (msg.id != null) seen.set(msg.id, msg);

        if (msg.id === 1) {
          say({ jsonrpc: '2.0', method: 'notifications/initialized' });
          say({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
        }
        if (msg.id === 2) {
          // Gọi `search` THẬT. Snapshot LanceDB được chụp lúc container đang ghi
          // (`tar: file changed as we read it`), nên "37.241 file có mặt" không
          // chứng minh được gì — chỉ một truy vấn chạy được mới chứng minh.
          say({
            jsonrpc: '2.0', id: 3, method: 'tools/call',
            params: { name: 'search', arguments: { query: 'quyết định của Bệ hạ về engine', top_k: 3 } },
          });
        }
        if (msg.id === 3) {
          clearTimeout(timer);
          child.kill();
          resolve({ init: seen.get(1), tools: seen.get(2), search: msg, stderr });
        }
      }
    });

    child.stderr.on('data', (b) => { stderr += b.toString('utf8'); });
    child.on('error', (e) => { clearTimeout(timer); reject(e); });
    child.on('close', () => {
      clearTimeout(timer);
      if (!seen.has(3)) {
        const got = seen.has(2) ? 'tools/list xong nhưng search chết' : 'chưa tới được tools/list';
        reject(new Error(`Server thoát sớm (${got}). stderr: ${stderr.slice(-1200)}`));
      }
    });

    say({
      jsonrpc: '2.0', id: 1, method: 'initialize',
      params: {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'alice-portable-test', version: '0.1.0' },
      },
    });
  });
}

test('brain MCP: bắt tay được và có đủ tool recall', { skip, timeout: 180000 }, async () => {
  const cfg = brain.mcpConfig();
  assert.ok(cfg, 'mcpConfig() phải trả cấu hình khi brain đã đóng gói');

  // D-0053 mục 3: đường dẫn TUYỆT ĐỐI tới runtime nhúng, không phải "python" trần.
  assert.ok(fs.existsSync(cfg.command[0]), `command[0] phải là file có thật: ${cfg.command[0]}`);
  assert.notEqual(cfg.command[0], 'python', 'không được gọi python trần trên PATH (M-0035)');

  const { init, tools, search } = await mcpHandshake(cfg);

  assert.ok(init && init.result, 'initialize phải trả result');
  assert.ok(tools.result && Array.isArray(tools.result.tools), 'tools/list phải trả mảng tools');

  const names = tools.result.tools.map((t) => t.name);
  for (const want of MUST_HAVE) {
    assert.ok(names.includes(want), `thiếu tool "${want}" — recall bị khuyết. Có: ${names.join(', ')}`);
  }

  // Recall thật: có tool mà tra ra rỗng thì vẫn là brain rỗng (`D-0053` mục 2).
  assert.ok(search.result, `search phải trả result, nhận: ${JSON.stringify(search).slice(0, 400)}`);
  // MCP trả `isError: false` khi thành công (không phải bỏ trống) — so với `undefined`
  // là bắt nhầm một lượt chạy đúng thành lỗi.
  assert.notEqual(search.result.isError, true, `search báo lỗi: ${JSON.stringify(search.result).slice(0, 400)}`);
  const payload = JSON.stringify(search.result);
  assert.ok(payload.length > 200,
    `search trả gần như rỗng (${payload.length} ký tự) — brain có vỏ mà không có tri thức: ${payload.slice(0, 300)}`);
});
