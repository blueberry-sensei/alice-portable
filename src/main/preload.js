'use strict';

const { contextBridge, ipcRenderer } = require('electron');

/**
 * Cầu duy nhất giữa UI và main.
 *
 * `contextIsolation: true` + `nodeIntegration: false`: renderer không có `require`,
 * không đụng được filesystem. Nó chỉ gọi được đúng những hàm liệt kê dưới đây —
 * một app chat mà renderer có toàn quyền node là một app chat có thể bị chính nội
 * dung nó hiển thị điều khiển.
 */
contextBridge.exposeInMainWorld('alice', {
  status: () => ipcRenderer.invoke('alice:status'),
  history: (limit) => ipcRenderer.invoke('alice:history', limit),
  search: (q) => ipcRenderer.invoke('alice:search', q),
  models: () => ipcRenderer.invoke('alice:models'),
  send: (text) => ipcRenderer.invoke('alice:send', text),
  cancel: () => ipcRenderer.invoke('alice:cancel'),
  getAvatar: () => ipcRenderer.invoke('alice:avatar:get'),
  pickAvatar: () => ipcRenderer.invoke('alice:avatar:pick'),
  resetAvatar: () => ipcRenderer.invoke('alice:avatar:reset'),

  // Key đi một chiều: renderer GỬI vào được, nhưng không có đường nào đọc ngược ra.
  setApiKey: (provider, key) => ipcRenderer.invoke('alice:auth:set', { provider, key }),

  // Các Alice trong app này.
  aliceList: () => ipcRenderer.invoke('alice:alice:list'),
  aliceCreate: (data) => ipcRenderer.invoke('alice:alice:create', data),
  aliceSelect: (id) => ipcRenderer.invoke('alice:alice:select', id),
  aliceRemove: (id) => ipcRenderer.invoke('alice:alice:remove', id),
  aliceSetModel: (id, model) => ipcRenderer.invoke('alice:alice:set-model', id, model),
  onAliceChanged: (cb) => ipcRenderer.on('alice:alice-changed', (_e, payload) => cb(payload)),

  // Public: biến Alice thành máy chủ (trang web chat).
  publicToggle: (id, data) => ipcRenderer.invoke('alice:public:toggle', id, data),
  publicInfo: (id) => ipcRenderer.invoke('alice:public:info', id),
  publicSetMode: (id, mode) => ipcRenderer.invoke('alice:public:set-mode', id, mode),
  publicCodeRotate: (id) => ipcRenderer.invoke('alice:public:code:rotate', id),
  publicAccountAdd: (id, data) => ipcRenderer.invoke('alice:public:account:add', id, data),
  publicAccountRemove: (id, username) => ipcRenderer.invoke('alice:public:account:remove', id, username),
  clipboardWrite: (text) => ipcRenderer.invoke('alice:clipboard:write', text),

  // Chia sẻ ra Internet qua cloudflared — không mở port nào trên router.
  tunnelStatus: (id) => ipcRenderer.invoke('alice:tunnel:status', id),
  tunnelDownload: (id) => ipcRenderer.invoke('alice:tunnel:download', id),
  tunnelToggle: (id, enabled) => ipcRenderer.invoke('alice:tunnel:toggle', id, enabled),
  onTunnelProgress: (cb) => ipcRenderer.on('alice:tunnel-progress', (_e, p) => cb(p)),

  // Chẩn đoán: nhật ký lỗi (tail) + mở thư mục logs + transcript gần đây kèm meta.
  debugLog: () => ipcRenderer.invoke('alice:debug:log'),
  debugOpen: () => ipcRenderer.invoke('alice:debug:open'),
  debugTranscript: (limit) => ipcRenderer.invoke('alice:debug:transcript', limit),

  getSettings: () => ipcRenderer.invoke('alice:settings:get'),
  setSettings: (patch) => ipcRenderer.invoke('alice:settings:set', patch),

  // Cuộc trò chuyện + lịch hẹn + tắt hẳn.
  clearChat: () => ipcRenderer.invoke('alice:chat:clear'),
  schedList: () => ipcRenderer.invoke('alice:sched:list'),
  schedAdd: (data) => ipcRenderer.invoke('alice:sched:add', data),
  schedUpdate: (id, patch) => ipcRenderer.invoke('alice:sched:update', id, patch),
  schedRemove: (id) => ipcRenderer.invoke('alice:sched:remove', id),
  shutdown: () => ipcRenderer.invoke('alice:shutdown'),

  // Cập nhật: kiểm tra + mở trang tải (không tự tải/cài — chốt của Bệ hạ).
  updateCheck: () => ipcRenderer.invoke('alice:update:check'),
  updateOpen: (url) => ipcRenderer.invoke('alice:update:open', url),
  onUpdate: (cb) => ipcRenderer.on('alice:update', (_e, status) => cb(status)),

  onStream: (cb) => ipcRenderer.on('alice:stream', (_e, payload) => cb(payload)),
  onReady: (cb) => ipcRenderer.on('alice:ready', () => cb()),
  onBusy: (cb) => ipcRenderer.on('alice:busy', (_e, msg) => cb(msg)),
  onBrainError: (cb) => ipcRenderer.on('alice:brain-error', (_e, msg) => cb(msg)),
  onFatal: (cb) => ipcRenderer.on('alice:fatal', (_e, msg) => cb(msg)),
});
