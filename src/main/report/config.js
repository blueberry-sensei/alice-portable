'use strict';

/**
 * Cấu hình "Báo cáo tuần" của RIÊNG một Alice (mỗi Alice một folder một brain một
 * session — config này sống trong `<alice-home>/report.json`, không dính sang nhau).
 *
 * Định dạng đầy đủ:
 * {
 *   "googleServiceAccount": "C:\\path\\sa.json",  // hai dạng file, tự nhận theo
 *                                                  // field có mặt (xem
 *                                                  // report/collector.js#chatMessages):
 *                                                  //   - service account thật
 *                                                  //     (project thuộc Workspace
 *                                                  //     org, app đã join space) —
 *                                                  //     tải từ Cloud Console.
 *                                                  //   - hoặc file sinh bởi
 *                                                  //     scripts/chat-user-login.js
 *                                                  //     (project "personal
 *                                                  //     account" — Google khoá
 *                                                  //     "Join spaces" của mọi
 *                                                  //     Chat app trong trường
 *                                                  //     hợp này, đường service
 *                                                  //     account không dùng được).
 *   "googleSpace": "spaces/AAA...",               // Google Chat space id
 *   "planeBaseUrl": "https://api.plane.so",
 *   "planeApiKey": "pla_...",
 *   "planeWorkspace": "ten-workspace",
 *   "gitRepos": ["D:\\Work\\erp\\kos-erpnext", "D:\\Work\\erp\\kos-portal"],
 *   "templatePath": "",                           // mẫu báo cáo (markdown)
 *   "outputDir": "D:\\Work\\erp\\alice-portable",
 *   "outputName": "h661 _ HRM_Weekly Report",
 * }
 */

const fs = require('node:fs');
const path = require('node:path');

const DEFAULTS = {
  googleServiceAccount: '',
  googleSpace: '',
  planeBaseUrl: 'https://api.plane.so',
  planeApiKey: '',
  planeWorkspace: '',
  gitRepos: [],
  templatePath: '',
  outputDir: '',
  outputName: 'HRM_Weekly_Report',
};

function fileFor(baseDir) {
  return path.join(baseDir, 'report.json');
}

function load(baseDir) {
  try {
    const raw = JSON.parse(fs.readFileSync(fileFor(baseDir), 'utf8'));
    return normalize({ ...DEFAULTS, ...raw });
  } catch {
    return { ...DEFAULTS, gitRepos: [] };
  }
}

function normalize(cfg) {
  cfg.gitRepos = Array.isArray(cfg.gitRepos) ? cfg.gitRepos.filter(Boolean) : [];
  for (const key of Object.keys(DEFAULTS)) {
    if (key !== 'gitRepos' && typeof cfg[key] !== typeof DEFAULTS[key]) {
      cfg[key] = DEFAULTS[key];
    }
  }
  return cfg;
}

function save(baseDir, patch) {
  fs.mkdirSync(baseDir, { recursive: true });
  const next = normalize({ ...load(baseDir), ...patch });
  fs.writeFileSync(fileFor(baseDir), JSON.stringify(next, null, 2), 'utf8');
  return next;
}

/**
 * Mốc mặc định của báo cáo tuần: THỨ 5 TUẦN TRƯỚC — mọi ngày trong cùng một tuần
 * dương lịch (T2..CN) phải ra CÙNG một mốc, nếu không báo cáo làm sáng T3 lại khác
 * báo cáo làm chiều T5. Công thức: tuần này bắt đầu ở thứ 2, tuần trước cách đúng
 * 7 ngày, thứ 5 của tuần đó là +3 → `back = (getDay()+6)%7 + 4`.
 * Thứ 5(4)→7 ngày, Thứ 6(5)→8, T7(6)→9, CN(0)→10, T2(1)→4, T3(2)→5, T4(3)→6.
 * Đã kiểm bằng test với ngày cố định — đổi công thức là vỡ kiểm chứng lịch.
 */
function lastThursday(now = new Date()) {
  const d = new Date(now);
  const back = (d.getDay() + 6) % 7 + 4;
  d.setDate(d.getDate() - back);
  return d.toISOString().slice(0, 10);
}

/**
 * Entry MCP kiểu opencode cho server báo cáo (stdio, chạy bằng chính electron
 * trong chế độ node — không `npx`, không node trần trên PATH, D-0053 mục 3).
 * Server đọc `REPORT_CONFIG_FILE` tại mỗi lần gọi tool nên đổi settings không cần
 * khởi động lại.
 */
function buildReportMcp({ execPath, serverFile, configFile }) {
  if (!configFile || !serverFile) return null;
  return {
    type: 'local',
    command: [execPath, serverFile],
    environment: {
      ELECTRON_RUN_AS_NODE: '1',
      REPORT_CONFIG_FILE: configFile,
    },
    enabled: true,
    timeout: 180000,
  };
}

module.exports = { DEFAULTS, load, save, fileFor, lastThursday, buildReportMcp };
