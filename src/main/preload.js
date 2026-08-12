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

  // Chẩn đoán: nhật ký lỗi (tail) + mở thư mục logs + transcript gần đây kèm meta.
  debugLog: () => ipcRenderer.invoke('alice:debug:log'),
  debugOpen: () => ipcRenderer.invoke('alice:debug:open'),
  debugTranscript: (limit) => ipcRenderer.invoke('alice:debug:transcript', limit),

  getSettings: () => ipcRenderer.invoke('alice:settings:get'),
  setSettings: (patch) => ipcRenderer.invoke('alice:settings:set', patch),

  onStream: (cb) => ipcRenderer.on('alice:stream', (_e, payload) => cb(payload)),
  onReady: (cb) => ipcRenderer.on('alice:ready', () => cb()),
  onBusy: (cb) => ipcRenderer.on('alice:busy', (_e, msg) => cb(msg)),
  onBrainError: (cb) => ipcRenderer.on('alice:brain-error', (_e, msg) => cb(msg)),
  onFatal: (cb) => ipcRenderer.on('alice:fatal', (_e, msg) => cb(msg)),
});
