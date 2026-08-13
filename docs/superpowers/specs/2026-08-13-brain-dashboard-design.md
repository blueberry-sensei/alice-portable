# Brain Dashboard — vendor UI thật của Alice Brain vào Alice Portable

**Ngày:** 2026-08-13 · **Trạng thái:** approved (Bệ hạ xác nhận hướng A — vendor thật)

## Mục tiêu

Alice Portable là **bản thu nhỏ, dễ dùng hơn** của kiến trúc SAG (`blueberry-sensei/alice-brain`
+ `alice-core`), không phải một nhánh rẽ khác kiến trúc. Mỗi Alice có nút **"Xem Alice
Brain…"** mở đúng dashboard gốc (Next.js `apps/web`), trỏ vào **brain của CHÍNH Alice đó**,
với đủ 4 việc Bệ hạ cần — cả 4 đã có sẵn trong `apps/web`/`apps/api`, không phải xây mới:

1. **Nhiều model extract, tự chuyển nhà khi hết quota** — `Settings → Models`, đã có (đọc
   `SETUP.md` gốc: bảng "Timeout→thử lại cùng nhà / 429→chuyển nhà kế / sai key→tắt nhà đó").
2. **Export / Export kèm credential / Import config model** — đã có, API
   `POST /api/v1/system/config-transfer/export` (`kind: alice-model-config |
   sub-agent-config`, mã hoá bằng passphrase, "API không bao giờ trả credential
   plaintext") + `/config-transfer/import`.
3. **Tuỳ chỉnh embedding, không bắt buộc local** — đã có, `Settings → Models`; Alice Portable
   hiện tự đặt mặc định `bge-m3` (sidecar.js), người dùng đổi được qua chính UI này thay vì
   chỉ qua settings.json ẩn.
4. **Xem knowledge graph / danh sách tri thức / tiến trình extract** — đã có, route
   `app/(app)/knowledge/` (danh sách + đồ thị `3d-force-graph`) và `Settings → Telemetry`
   (bảng `extraction` — xem `TELEMETRY.md` gốc).

## Kiến trúc

Alice Portable **đã giải quyết xong nửa khó hơn**: `sag_api` (Python) đã nhúng sẵn qua
`BrainSidecar`, không cần PyInstaller freeze như `apps/desktop` gốc phải tự làm. Phần THIẾU
duy nhất là **Next.js** (`apps/web`).

```
Bấm "Xem Alice Brain…" (Alice X đang mở)
  → BrainSidecar của Alice X: đảm bảo `sag_api.desktop` (uvicorn) ĐANG CHẠY trên cổng cố định
  → NextDashboard: đảm bảo Next.js standalone server ĐANG CHẠY trên cổng web cố định,
    NEXT_PUBLIC_API_BASE đã đóng cứng lúc build trỏ đúng cổng sag_api.desktop ở trên
  → mở `http://127.0.0.1:<web-port>` trong trình duyệt hệ thống (shell.openExternal)
```

### Vì sao KHÔNG mở trong cửa sổ app (BrowserWindow riêng)
YAGNI cho v1 — `shell.openExternal` bằng đúng cơ chế Alice Portable đã dùng cho link ngoài
(`setWindowOpenHandler`). Mở tab trình duyệt thật cũng cho Bệ hạ phóng to/thu nhỏ/nhiều tab
tự nhiên hơn một BrowserWindow con Electron.

### Vì sao chỉ xem được MỘT Alice tại một thời điểm
`NEXT_PUBLIC_API_BASE` là **build-time value** của Next.js (xác nhận từ `next.config.mjs` +
`.env.local.example` gốc: `output: "standalone"`, biến `NEXT_PUBLIC_API_BASE` được Next.js
inline vào bundle lúc `next build`, không đọc lại lúc chạy). Alice Portable build Next.js
**một lần duy nhất**, cổng CỐ ĐỊNH; đổi Alice đang xem = tắt `sag_api.desktop` của Alice cũ,
bật của Alice mới trên ĐÚNG cổng đó. Giống hệt cách "Alice đang mở" đã là khái niệm toàn app
(chỉ một `engine`/`store` sống tại một thời điểm).

### Cổng — KHÔNG dùng lại 8931
`sidecar.js` hiện mặc định `SAG_DESKTOP_PORT=8931` — TRÙNG cổng mặc định của `PublicServer`
(`public-server.js`). Hai máy chủ này có thể chạy đồng thời cho hai Alice khác nhau
(một Alice đang public chat, Alice khác đang mở brain dashboard) → cổng trùng là tái diễn
đúng bug "hai tiến trình giành một cổng" đã sửa hôm nay. Đổi mặc định `SAG_DESKTOP_PORT` →
**`8932`**. Web port Next.js → **`8933`**.

## Việc phải làm

### 1. Vendor `apps/web` — build standalone, đóng gói vào `runtime/`
Tương tự cách `runtime/opencode/` và `runtime/brain/` đã có: thêm bước build
(`scripts/bundle-webui.ps1`, mirror `bundle-brain.ps1`) tải `apps/web` từ
`blueberry-sensei/alice-brain` (tag cố định, ghi vào `brain-source/VERSION.txt` kiểu tương
tự), `npm ci && npm run build` với `NEXT_PUBLIC_API_BASE=http://127.0.0.1:8932`, copy
`.next/standalone` + `.next/static` + `public/` vào `runtime/webui/`.

**Không vendor source `apps/web` vào repo** (khác cách làm với `brain-source` — source Python
thuần đọc được, còn Next.js build ra hàng nghìn file JS đã minify, vendor SOURCE vô nghĩa).
Chỉ vendor **BẢN ĐÃ BUILD** (`runtime/webui/`), giống `runtime/opencode/` (binary, không phải
source TypeScript của opencode).

### 2. `BrainSidecar` — thêm chế độ "desktop" (HTTP server), không chỉ MCP stdio
`sidecar.js` hiện chỉ có đường MCP stdio (`sag_api.mcp.server`). Thêm method
`startDesktop()`/`stopDesktop()` spawn `python -m sag_api.desktop` với
`SAG_DESKTOP_PORT=8932`, `SAG_DESKTOP_HOST=127.0.0.1`, cùng bộ env còn lại
(`SAG_DATABASE_URL`, `SAG_DATA_DIR`, embedding...) đã có sẵn trong `env()`.

### 3. `NextDashboard` — class mới, quản lý tiến trình Next.js standalone
`src/main/brain/webui.js`: spawn `runtime/webui/server.js` bằng CHÍNH Node của Electron
(`process.execPath` với `ELECTRON_RUN_AS_NODE=1`, giống cách `scripts/run-tests.ps1` chạy
test — không cần Node.js riêng, khỏi phình bản cài thêm một runtime). Cổng cố định 8933.

### 4. `main.js` — IPC `alice:brain:open`
- Dừng `sag_api.desktop` + Next.js của Alice ĐANG hiện (nếu có, khác Alice vừa bấm).
- Khởi `sag_api.desktop` của Alice X trên 8932 (đợi `/api/v1/system/ready` trả 200).
- Khởi Next.js standalone trên 8933 nếu chưa chạy (chỉ cần chạy MỘT LẦN, không cần khởi lại
  mỗi lần đổi Alice — chỉ API đổi).
- `shell.openExternal('http://127.0.0.1:8933')`.

### 5. UI — nút trong Dashboard card + Cài đặt
Nút "Xem Alice Brain…" — disable + tooltip khi `!brain.available` (bản cài thiếu
`runtime/webui`, vd bản build cũ). Đang mở thì hiện trạng thái "Đang khởi động…" tới khi
`ready`.

## Không làm trong lần này (YAGNI)
- Không tự động đồng bộ auth giữa Alice Portable và web UI — Bệ hạ tự đăng ký một tài khoản
  LOCAL (chỉ trên máy, `/register`) lần đầu mở dashboard của mỗi Alice, y hệt cách gốc hoạt
  động. Không tự tạo tài khoản hộ.
- Không mở nhiều dashboard cùng lúc (đa cổng động) — v1 một cổng cố định, một Alice một lượt.
- Không nhúng dashboard vào cửa sổ chính (BrowserWindow riêng) — mở bằng trình duyệt hệ
  thống.
- Không tự cập nhật `runtime/webui` theo release của `alice-brain` — cùng nhịp thủ công với
  `brain-source` hiện tại (`npm run brain:sync-source`-style, làm sau nếu cần).

## Rủi ro cần xác minh khi code (chưa build thật để kiểm chứng)
- `.next/standalone/server.js` có tự phục vụ `.next/static`/`public` hay cần copy tay theo
  đúng cấu trúc Next.js standalone docs — xác minh bằng một lần build + chạy thật ở Task đầu.
- Next.js standalone chạy bằng Electron's Node (`ELECTRON_RUN_AS_NODE=1`) có đủ API cần
  thiết không (một số bản Electron thiếu vài native module) — xác minh bằng chạy thật, có
  phương án lùi: bundle một Node.js binary riêng nếu không chạy được.
