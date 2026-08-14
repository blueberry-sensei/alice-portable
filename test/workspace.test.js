'use strict';

/**
 * Workspace sinh lúc chạy — chỗ `D-0053` mục 3 và `M-0035` gặp nhau.
 *
 * `M-0035`: `chrome-devtools-mcp` chết vì `npx` đi tìm `node` trong PATH của máy.
 * Bài học không phải "nâng node" mà là **đừng để PATH của máy quyết định app chạy
 * bằng runtime nào**. Nên `opencode.json` do app sinh phải chứa đường dẫn tuyệt đối
 * tới binary NHÚNG, và test này canh đúng điều đó.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

// `config.js` đọc ROOT lúc require, nên phải đặt biến TRƯỚC khi nạp module.
const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-ws-'));
process.env.ALICE_PORTABLE_ROOT = sandbox;

const config = require('../src/main/config');
const { provisionWorkspace, buildAgentsMd, buildOpencodeJson, buildClaudeMcpJson } = require('../src/main/alice');

test('workspace sinh AGENTS.md + opencode.json, đường dẫn MCP là tuyệt đối', () => {
  const brainMcp = {
    type: 'local',
    command: [path.join(sandbox, 'runtime', 'brain', 'python', 'python.exe'), '-m', 'sag_api.mcp.server'],
    environment: { PYTHONPATH: path.join(sandbox, 'runtime', 'brain', 'app') },
    enabled: true,
  };
  const dir = provisionWorkspace({ model: null }, { brainMcp });

  const agents = fs.readFileSync(path.join(dir, 'AGENTS.md'), 'utf8');
  const cfg = JSON.parse(fs.readFileSync(path.join(dir, 'opencode.json'), 'utf8'));

  assert.ok(path.isAbsolute(cfg.mcp.brain.command[0]),
    'command[0] phải tuyệt đối — PATH của máy không được quyết định runtime nào chạy (M-0035)');
  assert.ok(!cfg.mcp.brain.command.includes('npx'), 'không bao giờ dùng npx (D-0053 mục 3)');
  assert.equal(cfg.mcp.brain.command[0].includes(sandbox), true, 'phải trỏ vào runtime NHÚNG trong app');
  assert.deepEqual(cfg.instructions, ['AGENTS.md']);

  // Khối "không có lượt sau" là bản vá kiến trúc cho M-0036: `opencode run` là một
  // lượt rồi thoát, nên câu "để tôi chờ task nền" biến thành câu trả lời cuối cùng.
  assert.match(agents, /KHÔNG CÓ LƯỢT SAU|Không có ai đánh thức bạn lại/i,
    'AGENTS.md phải nói rõ hình dạng vòng đời — model không tự đoán được (M-0036)');
  assert.match(agents, /TIẾP NỐI HỘI THOẠI/,
    'AGENTS.md phải dạy Alice cách đọc khối mồi, nếu không nó sẽ chào lại từ đầu');
});

test('không có knowledge/ALICE.md thì nói thẳng, không diễn như có đủ luật', () => {
  const md = buildAgentsMd(path.join(sandbox, 'khong-ton-tai'));
  assert.match(md, /Chưa nạp được hiến pháp đầy đủ/,
    'thiếu hiến pháp mà im lặng chạy tiếp là kiểu hỏng khó thấy nhất');
});

test('không khai MCP nào khi brain chưa sẵn sàng — thà thiếu còn hơn trỏ vào chỗ trống', () => {
  const cfg = buildOpencodeJson({ model: null }, { brainMcp: null });
  assert.deepEqual(cfg.mcp, {});
});

test('provisionWorkspace: sinh hook SessionStart cho Claude Code — không matcher, bắt cả compact', () => {
  const dir = provisionWorkspace(
    { contextCeiling: 1000, windowRatio: 0.6, compactRatio: 0.8, keepVerbatim: 4 },
    { dir: path.join(sandbox, 'ws-claude-hook') }
  );

  const settingsPath = path.join(dir, '.claude', 'settings.json');
  assert.ok(fs.existsSync(settingsPath), 'phải sinh .claude/settings.json');
  const settings = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
  const hookEntry = settings.hooks.SessionStart[0];
  assert.equal(hookEntry.matcher, undefined, 'bỏ trống matcher — bắt mọi nguồn kể cả compact');
  assert.match(hookEntry.hooks[0].command, /reload-skill\.js/);

  const hookScript = path.join(dir, '.claude-hooks', 'reload-skill.js');
  assert.ok(fs.existsSync(hookScript), 'phải sinh script hook');
});

test('provisionWorkspace: hook reload-skill.js in ĐÚNG nội dung AGENTS.md ra stdout', () => {
  const dir = provisionWorkspace(
    { contextCeiling: 1000, windowRatio: 0.6, compactRatio: 0.8, keepVerbatim: 4 },
    { dir: path.join(sandbox, 'ws-claude-hook-2') }
  );
  const hookScript = path.join(dir, '.claude-hooks', 'reload-skill.js');
  const out = require('node:child_process').execFileSync(
    process.execPath, [hookScript], { cwd: dir, encoding: 'utf8' }
  );
  const agentsMd = fs.readFileSync(path.join(dir, 'AGENTS.md'), 'utf8');
  assert.match(out, /<alice-workspace-reload>/);
  assert.ok(out.includes(agentsMd.trim().slice(0, 200)), 'phải chứa nội dung AGENTS.md');
});

test('.mcp.json: Claude Code KHÔNG đọc opencode.json — phải sinh file riêng, đúng định dạng', () => {
  const brainMcp = {
    type: 'local',
    command: [path.join(sandbox, 'runtime', 'brain', 'python', 'python.exe'), '-m', 'sag_api.mcp.server'],
    environment: { PYTHONPATH: path.join(sandbox, 'runtime', 'brain', 'app') },
    enabled: true,
  };
  const reportMcp = {
    type: 'local',
    command: [process.execPath, path.join(sandbox, 'mcp-server.js')],
    environment: { ELECTRON_RUN_AS_NODE: '1', REPORT_CONFIG_FILE: path.join(sandbox, 'report.json') },
    enabled: true,
  };
  const dir = provisionWorkspace({ model: null }, { brainMcp, reportMcp, dir: path.join(sandbox, 'ws-mcpjson') });

  const mcp = JSON.parse(fs.readFileSync(path.join(dir, '.mcp.json'), 'utf8'));
  assert.ok(mcp.mcpServers, 'phải có mcpServers (định dạng Claude Code)');
  assert.ok(mcp.mcpServers.brain, 'brain phải có mặt');
  assert.ok(mcp.mcpServers.report, 'report phải có mặt');
  assert.ok(path.isAbsolute(mcp.mcpServers.report.command),
    'command phải tuyệt đối — PATH của máy không được quyết định (M-0035)');
  assert.deepEqual(mcp.mcpServers.report.args, [path.join(sandbox, 'mcp-server.js')],
    'args phải tách khỏi command');
  assert.deepEqual(mcp.mcpServers.report.env.REPORT_CONFIG_FILE, path.join(sandbox, 'report.json'));
  assert.equal(mcp.mcpServers.report.env.ELECTRON_RUN_AS_NODE, '1');
});

test('.mcp.json: không có server nào thì KHÔNG sinh file — Claude Code không quét rác', () => {
  const dir = provisionWorkspace({ model: null }, { dir: path.join(sandbox, 'ws-mcpjson-empty') });
  assert.equal(fs.existsSync(path.join(dir, '.mcp.json')), false, 'file rỗng là rác quét mỗi lần mở');
});

test('.mcp.json: entry không hợp lệ (type lạ, command rỗng) bị bỏ qua', () => {
  const mcp = buildClaudeMcpJson({ brainMcp: { type: 'remote', url: 'x' }, reportMcp: { type: 'local', command: [] } });
  assert.equal(mcp, null, 'cả hai đều không chuyển được sang định dạng Claude → không có file');
});

test.after(() => {
  fs.rmSync(sandbox, { recursive: true, force: true });
});
