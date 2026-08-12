# brain-source — nguồn brain đóng băng cho bản portable

Thư mục này chứa **nguồn thuần Python** của Alice Brain, copy từ hai repo riêng:

| Thư mục | Gốc | Giấy phép |
|---|---|---|
| `sag_api/` | `blueberry-sensei/alice-brain` → `apps/api/sag_api` | MIT |
| `sag_agent/` | `blueberry-sensei/alice-brain` → `apps/api/sag_agent` | MIT |
| `alicecore/` | `blueberry-sensei/alice-core` → `src/alicecore` | MIT |

Vì sao vendor thay vì clone: hai repo nguồn là **private**, còn CI của
alice-portable là **public** — không clone được. Cách `alice-coding` dùng
(image Docker GHCR public) không áp dụng được vì image là binary Linux, còn
bản portable cần cài wheel đúng từng hệ điều hành.

**Giá phải trả: nguồn ở đây đóng băng theo release.** Sau mỗi lần brain đổi,
chạy lại lệnh sync rồi mới build/release:

```
npm run brain:sync-source
```

Bản nguồn hiện tại ghi trong [VERSION.txt](VERSION.txt) (commit hash của hai repo).

**Không sửa tay file trong thư mục này** — bản sửa sẽ bị lệch khỏi nguồn thật
và mất đi ở lượt sync tiếp theo.
