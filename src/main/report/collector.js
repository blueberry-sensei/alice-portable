'use strict';

/**
 * Bộ thu thập dữ liệu cho báo cáo tuần: git commits (local), tasks Plane (REST),
 * tin nhắn Google Chat (service account + JWT tự ký — KHÔNG cần google-auth-library,
 * chỉ `node:crypto`, phù hợp portable không cài dependency).
 *
 * Mọi hàm trả về `{ rows }` hoặc `{ error }` — không throw, để MCP server gói được
 * lỗi thành câu trả lời cho Alice.
 */

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');

/** Git log từ mốc `since` (YYYY-MM-DD), không merge commit. */
function gitLog(repo, since) {
  if (!repo) return { rows: [] };
  if (!fs.existsSync(path.join(repo, '.git'))) {
    return { error: `Không phải repo git: ${repo}` };
  }
  try {
    const out = execFileSync(
      'git',
      ['-C', repo, 'log', `--since=${since} 00:00:00`, '--no-merges',
        '--pretty=format:%h%x09%an%x09%aI%x09%s'],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true }
    );
    const rows = out.split('\n').filter(Boolean).map((line) => {
      const [hash, author, date, ...rest] = line.split('\t');
      return { hash, author, date: date || '', subject: rest.join('\t') };
    });
    return { rows };
  } catch (err) {
    return { error: `git log thất bại ở ${repo}: ${err.message}` };
  }
}

/** Tasks Plane từ workspace, lọc thủ công theo updated_at >= since (YYYY-MM-DD). */
async function planeIssues({ baseUrl, apiKey, workspace, since, pages = 5 }) {
  if (!apiKey || !workspace) return { error: 'Thiếu planeApiKey hoặc planeWorkspace trong cấu hình.' };
  const base = (baseUrl || 'https://api.plane.so').replace(/\/+$/, '');
  const out = [];
  for (let page = 1; page <= pages; page++) {
    let res;
    try {
      res = await fetch(`${base}/api/v1/workspaces/${encodeURIComponent(workspace)}/issues/?per_page=100&page=${page}`, {
        headers: { 'X-API-Key': apiKey },
        signal: AbortSignal.timeout(20000),
      });
    } catch (err) {
      return { error: `Không gọi được Plane (${base}): ${err.message}` };
    }
    if (!res.ok) {
      return { error: `Plane trả HTTP ${res.status} (page ${page}) — kiểm tra API key và tên workspace.` };
    }
    let body;
    try {
      body = await res.json();
    } catch {
      body = {};
    }
    const results = Array.isArray(body.results) ? body.results : [];
    for (const issue of results) {
      const updated = String(issue.updated_at || '');
      if (since && updated.slice(0, 10) < since) continue;
      out.push({
        identifier: issue.identifier || issue.id || '',
        name: issue.name || '',
        state: issue.state && issue.state.name ? issue.state.name : '',
        priority: issue.priority || '',
        assignees: Array.isArray(issue.assignees) ? issue.assignees.map((a) => a.display_name || a.first_name || '').filter(Boolean) : [],
        updatedAt: updated.slice(0, 16).replace('T', ' '),
        url: issue.id ? `https://app.plane.so/${workspace}/issues/${issue.id}` : '',
      });
    }
    if (!body.next_page_results) break;
    if (!Array.isArray(body.results) || body.results.length === 0) break;
  }
  return { rows: out };
}

function base64Url(buf) {
  return Buffer.from(buf).toString('base64url');
}

/**
 * JWT RS256 cho service account — tự ký bằng `node:crypto`, không google-auth-library.
 * Trả về access_token 1 tiếng.
 */
async function serviceAccountToken(cred, scopes) {
  const now = Math.floor(Date.now() / 1000);
  const header = base64Url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));
  const payload = base64Url(JSON.stringify({
    iss: cred.client_email,
    scope: scopes.join(' '),
    aud: cred.token_uri,
    iat: now,
    exp: now + 3600,
  }));
  const signingInput = `${header}.${payload}`;
  let signature;
  try {
    const signer = crypto.createSign('RSA-SHA256');
    signer.update(signingInput);
    signature = signer.sign(cred.private_key, 'base64url');
  } catch (err) {
    return { error: `Ký JWT thất bại — file service account có hợp lệ không? ${err.message}` };
  }

  let res;
  try {
    res = await fetch(cred.token_uri, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: `grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion=${encodeURIComponent(`${signingInput}.${signature}`)}`,
      signal: AbortSignal.timeout(20000),
    });
  } catch (err) {
    return { error: `Không lấy được token OAuth: ${err.message}` };
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    return { error: `Token endpoint trả HTTP ${res.status}${text ? `: ${text.slice(0, 300)}` : ''}` };
  }
  const data = await res.json().catch(() => ({}));
  if (!data.access_token) return { error: 'Token endpoint không trả access_token.' };
  return { token: data.access_token };
}

/**
 * Tin nhắn Google Chat từ `since` (YYYY-MM-DD), theo thứ tự thời gian.
 * `chatBaseUrl` chỉ để test trỏ vào server giả; mặc định là Google thật.
 */
async function chatMessages({ credentialsPath, space, since, chatBaseUrl = 'https://chat.googleapis.com', limit = 500 }) {
  if (!credentialsPath || !fs.existsSync(credentialsPath)) {
    return { error: `Không tìm thấy file service account: ${credentialsPath}` };
  }
  let cred;
  try {
    cred = JSON.parse(fs.readFileSync(credentialsPath, 'utf8'));
  } catch (err) {
    return { error: `Đọc file service account lỗi: ${err.message}` };
  }
  if (!cred.client_email || !cred.private_key || !cred.token_uri) {
    return { error: 'File service account thiếu client_email/private_key/token_uri.' };
  }

  const tok = await serviceAccountToken(cred, ['https://www.googleapis.com/auth/chat.messages.readonly']);
  if (tok.error) return tok;

  // `space` chấp nhận cả "spaces/AAAA..." lẫn id trần.
  const pathBase = /^spaces\//.test(space || '') ? space : `spaces/${space}`;
  const filter = since
    ? `createTime > "${since}T00:00:00Z"`
    : '';
  const out = [];
  let pageToken = '';
  const base = chatBaseUrl.replace(/\/+$/, '');
  for (let page = 0; page < 5 && out.length < limit; page++) {
    const params = new URLSearchParams({ pageSize: '100' });
    if (filter) params.set('filter', filter);
    if (pageToken) params.set('pageToken', pageToken);
    let res;
    try {
      res = await fetch(`${base}/v1/${pathBase}/messages?${params}`, {
        headers: { Authorization: `Bearer ${tok.token}` },
        signal: AbortSignal.timeout(20000),
      });
    } catch (err) {
      return { error: `Không gọi được Google Chat: ${err.message}` };
    }
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      return { error: `Google Chat trả HTTP ${res.status}${text ? `: ${text.slice(0, 300)}` : ''} — app có được thêm vào space không?` };
    }
    const data = await res.json().catch(() => ({}));
    const messages = Array.isArray(data.messages) ? data.messages : [];
    for (const m of messages) {
      const author = m.sender && (m.sender.displayName || m.sender.name) || '';
      out.push({
        text: String(m.text || ''),
        author,
        time: String(m.createTime || ''),
        thread: m.thread && m.thread.name || '',
      });
    }
    pageToken = data.nextPageToken || '';
    if (!pageToken) break;
  }
  out.sort((a, b) => (a.time < b.time ? -1 : 1));
  return { rows: out.slice(0, limit) };
}

module.exports = { gitLog, planeIssues, chatMessages, serviceAccountToken };
