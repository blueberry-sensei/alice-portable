// Probe: gọi thẳng OpencodeEngine và in từng mốc, để thấy nó tắc ở đâu.
// Chạy bằng electron-as-node. Không phải test — không có assert.
const { OpencodeEngine } = require('../src/main/engine/opencode');
const os = require('node:os');
const fs = require('node:fs');
const path = require('node:path');

const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);

(async () => {
  const settings = {
    modelPreference: ['opencode/deepseek-v4-flash-free'],
    idleTimeoutMs: 45000,
  };
  const engine = new OpencodeEngine(settings);
  log('bin:', engine.binPath, '| source:', engine.binSource, '| available:', engine.available);

  log('gọi listModels()…');
  try {
    const models = await engine.listModels();
    log('models:', models.length, models.slice(0, 3).join(', '));
  } catch (e) {
    log('listModels HỎNG:', e.message);
  }

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-probe-'));
  log('gọi run() ở', dir);
  try {
    const out = await engine.run({
      message: 'Tra loi dung mot tu: ok',
      sessionId: null,
      model: 'opencode/deepseek-v4-flash-free',
      cwd: dir,
      onEvent: (ev) => log('  event:', ev.type),
    });
    log('XONG:', JSON.stringify({ session: out.sessionId, text: out.text, tokens: out.tokens }));
  } catch (e) {
    log('run HỎNG:', e.message);
  }
  process.exit(0);
})();
