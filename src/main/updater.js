'use strict';

/**
 * Kiểm tra phiên bản mới — so với release mới nhất trên GitHub.
 *
 * Bệ hạ chốt (2026-08-13): KHÔNG ép update, KHÔNG tự tải/cài. App chỉ:
 *   - kiểm tra nền lúc mở (lỗi mạng thì im lặng — không spam);
 *   - nếu có bản mới → banner + nút mở trang tải (dữ liệu giữ nguyên khi cài đè).
 */

const REPO = 'blueberry-sensei/alice-portable';
const API = `https://api.github.com/repos/${REPO}/releases/latest`;
const FALLBACK_URL = `https://github.com/${REPO}/releases/latest`;

/** "v0.1.4" → [0, 1, 4]. Không hiểu thì trả null — không so được thì đừng kết luận. */
function parseVersion(v) {
  const m = String(v || '').replace(/^v/i, '').match(/^(\d+)\.(\d+)\.(\d+)/);
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

/** a > b theo semver 3 thành phần. */
function isNewer(a, b) {
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return false;
}

async function fetchLatest() {
  const res = await fetch(API, {
    headers: { 'User-Agent': 'alice-portable-updater', Accept: 'application/vnd.github+json' },
    signal: AbortSignal.timeout(15000),
  });
  if (!res.ok) throw new Error(`GitHub trả HTTP ${res.status}`);
  const data = await res.json();
  return {
    tag: String(data.tag_name || ''),
    url: String(data.html_url || FALLBACK_URL),
  };
}

class Updater {
  constructor() {
    this.hasUpdate = false;
    this.current = null;
    this.latest = null;
    this.url = null;
    this.error = null;
    this.checked = false;
  }

  /** Kiểm tra một lần, không ném — lỗi mạng chỉ ghi lại, không làm phiền app. */
  async check(currentVersion) {
    try {
      const latest = await fetchLatest();
      const cur = parseVersion(currentVersion);
      const lat = parseVersion(latest.tag);
      this.hasUpdate = Boolean(cur && lat && isNewer(lat, cur));
      this.current = currentVersion;
      this.latest = latest.tag || null;
      this.url = latest.url;
      this.error = null;
      this.checked = true;
    } catch (err) {
      this.hasUpdate = false;
      this.error = err.message;
      this.checked = false;
    }
    return this.status();
  }

  status() {
    return {
      checked: this.checked,
      hasUpdate: this.hasUpdate,
      current: this.current,
      latest: this.latest,
      url: this.url,
      error: this.error,
    };
  }
}

module.exports = { Updater, parseVersion, isNewer };
