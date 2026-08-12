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
const { provisionWorkspace, buildAgentsMd, buildOpencodeJson } = require('../src/main/alice');

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

test.after(() => {
  fs.rmSync(sandbox, { recursive: true, force: true });
});
