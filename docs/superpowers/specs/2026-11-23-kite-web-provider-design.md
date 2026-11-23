# Kite Web + Provider — Thiết kế

**Trạng thái:** Đã duyệt (Han, 2026-11-23)
**Cập nhật cho:** `2026-11-17-provider-extensible-kite-design.md`. Những chỗ spec cũ nói khác, spec này thắng. Cụ thể: provider trả tiền được phép, nhưng chỉ khi chủ máy tự thêm và đặt hạn mức; triển khai tự động bằng CI/CD là mục tiêu, không còn là non-goal.

## 1. Mục tiêu

Biến Kite thành một **web chạy trên trình duyệt**:
- **Một chủ duy nhất** đăng nhập.
- Cấu hình **provider** và **worker** ngay trên web.
- Chat, giao task, theo dõi worker và agent trên một **trang tổng quan 3D**.
- Toàn bộ được **CI/CD tự triển khai** lên bất kỳ máy ảo nào. Máy đầu tiên là EC2.

Những cấu hình Kite cần để chạy được (Docker, Postgres, Temporal, gateway, sandbox, secret, HTTPS) do **bộ cài tự làm**. Người dùng chỉ cấu hình trên web những gì là của riêng họ: provider, worker, repo, thông báo.

## 2. Quyết định đã chốt

| Chủ đề | Quyết định |
|---|---|
| Người dùng | **1 chủ** mỗi bản Kite. Web có một tài khoản admin. Người khác (ví dụ chủ notebook kaggle-an) chỉ cho mượn worker bằng worker token, không đăng nhập web. |
| Đóng gói | **Docker Compose**: mọi thành phần là container. Sandbox vẫn là container riêng, không Internet. |
| Cập nhật | **Máy tự kéo bản mới**. CI chỉ gắn nhãn khi test xanh; máy ảo kiểm tra định kỳ, kéo về, kiểm tra sức khỏe, lỗi thì quay về bản cũ. Không mở SSH cho GitHub. |
| Truy cập | **Chọn lúc cài**: Internet (HTTPS, đăng nhập) hoặc chỉ mạng riêng (Tailscale, SSH tunnel). Bản của Han: Internet. |
| HTTPS khi chưa có tên miền | `sslip.io`: tên dạng `<IP-viết-bằng-gạch-ngang>.sslip.io` trỏ về IP, để Caddy lấy được chứng chỉ Let's Encrypt. Có tên miền rồi thì đổi một dòng cấu hình. |
| Máy ảo | Máy mới **t3.medium** (4 GB RAM), có **Elastic IP**. Máy cũ (t3.small) vẫn dùng để phát triển cho tới khi máy mới chạy ổn. |
| Repo | `23521549-lang/kite` để public, nên image trên GHCR cũng public. Không bao giờ có secret trong repo. |
| Thứ tự | **Provider → Web → Triển khai** (CI/CD, hạ tầng làm sau cùng). |
| Kaggle | Mỗi người một tài khoản Kaggle, không dùng nhiều tài khoản để nhân quota (đúng điều khoản Kaggle). |
| Push | Kite không bao giờ push, merge hay mở PR. Áp patch và commit chỉ khi chủ bấm. |

## 3. Kiến trúc

```text
Trình duyệt (chủ)
  └─ Caddy (HTTPS, cổng 443)
       ├─ /            Web FE (React, file tĩnh)
       ├─ /api, /ws    Web BE (FastAPI) ── Postgres (dữ liệu web, provider, worker, chi phí)
       │                 ├─ Kite core: KiteOps, task store, journal, memory, bench
       │                 ├─ Temporal (task bền vững) ── Postgres
       │                 ├─ OpenCode server (chat agent trưởng, dùng lại cấu hình hiện có)
       │                 └─ Trình duyệt từ xa (Chromium + màn hình truyền lên web), chỉ bật khi cần
       └─ /worker/*    Gateway: hàng đợi việc chung cho mọi worker
                         ├─ Worker kéo việc (pull): notebook Kaggle
                         └─ Worker chuyển tiếp (relay, Kite tự chạy): OpenAI-compatible, Anthropic,
                            máy GPU thuê, server tự chạy
Sandbox (Docker, không Internet): agent code và test, nói chuyện duy nhất với gateway.
```

`/v1` và `/kite/*` của gateway vẫn chỉ mở nội bộ. Ra Internet chỉ có web (qua đăng nhập) và `/worker/*` (qua worker token).

## 4. Provider và worker

### 4.1 Khái niệm
- **Provider**: một tài khoản hoặc điểm kết nối tới nơi chạy model. Ví dụ: tài khoản Kaggle, key OpenAI, key Anthropic, key RunPod, URL server tự chạy.
- **Worker**: một provider cộng với cấu hình chạy (model, context, số việc chạy cùng lúc, giá, hạn mức), có công tắc **Bật/Tắt**. Một provider có thể sinh nhiều worker nếu điều khoản của provider cho phép.

### 4.2 Loại provider đợt đầu

| Loại | Thông tin cần | Cách nối | Chi phí |
|---|---|---|---|
| Kaggle | Kaggle API token, đăng nhập Kaggle trong trình duyệt của Kite, notebook | Notebook kéo việc (pull) | Miễn phí, quota GPU theo tuần |
| OpenAI-compatible | Base URL, key, danh sách model. Phủ OpenAI, OpenRouter, Groq, DeepSeek, Gemini (endpoint tương thích), vLLM, Ollama… | Relay gọi `/chat/completions` | Theo token |
| Anthropic (Claude) | Key, model | Relay qua adapter riêng (Messages API, tool use, streaming) | Theo token |
| Máy GPU thuê (RunPod…) | Key của nơi thuê, mẫu máy, image chạy vLLM hoặc llama.cpp | Kite bật/tắt máy qua API nơi thuê, relay gọi URL của máy | Theo giờ máy |

Loại nào không nằm trong bảng thì thêm bằng **OpenAI-compatible**, nếu nó có endpoint tương thích. Mỗi loại mới là một adapter riêng, có test với API giả trước khi bật.

### 4.3 Worker
- **Cấu hình:**
  - provider và model;
  - context tối đa, số việc chạy cùng lúc;
  - giá (theo token hoặc theo giờ) và hạn mức chi theo ngày hoặc tháng;
  - số phút rảnh thì tự tắt (Kaggle và máy thuê; mặc định 30);
  - tùy chọn **"tự bật khi tôi chat"**: giữ đúng luật p22, chỉ bật khi chủ đang chat hoặc có task giao từ chat.
- **Công tắc** Bật/Tắt do chủ điều khiển. Bên cạnh hiện **tình trạng thật**: tắt, đang khởi động (kèm lý do, ví dụ Kaggle đang xếp hàng), rảnh, bận, lỗi, hết quota hoặc hạn mức.
- Worker tự tắt vì rảnh thì công tắc tự về Tắt.

### 4.4 Bật worker theo loại
- **API (OpenAI-compatible, Anthropic):** gọi kiểm tra key, ví dụ lấy danh sách model. Qua thì worker rảnh ngay.
- **Kaggle:**
  1. Kite mở trang notebook trong **trình duyệt của Kite** (mục 4.8).
  2. Bấm Save Version, chọn Save & Run All.
  3. Notebook khởi động, lấy worker token từ Kaggle Secrets, tải code worker mới nhất từ Kite, rồi kết nối.
  - Công tắc **"Kite tự bấm"**, mặc định **tắt**: chủ tự bấm trong trình duyệt của Kite. Bật lên thì Kite tự bấm, chỉ hiện trình duyệt khi Kaggle đòi đăng nhập hoặc captcha. Bật là chấp nhận rủi ro điều khoản (mục 9).
  - Đẩy qua API (`kaggle kernels push`, token per-launch) giữ lại làm đường dự phòng.
- **Máy GPU thuê:** Kite gọi API nơi thuê để bật máy, chờ URL trả lời, rồi worker rảnh. Tắt thì gọi API dừng máy.
- **Server tự chạy:** gọi thử URL. Kite không bật hay tắt được loại này.

### 4.5 Giao việc
- **Worker nào đang bật và đang rảnh thì nhận việc.** Nhiều worker cùng rảnh thì ưu tiên miễn phí trước, trả tiền sau.
- **Một cuộc chat hoặc một task giữ nguyên worker từ đầu đến cuối**, vì mỗi worker có thể là model khác nhau. Chỉ khi worker chết hoặc hết quota/hạn mức thì chuyển sang worker khác, và ghi rõ việc chuyển vào nhật ký task.
- Không có worker nào bật: chat nhận ngay một dòng trạng thái (như p20), việc nằm chờ trong hàng đợi.

### 4.6 Quota và chi phí
- **Đếm:**
  - API: lấy `usage` token sau mỗi lần gọi, nhân với giá chủ nhập cho model đó.
  - Máy thuê: số giờ máy bật nhân với giá theo giờ.
  - Kaggle: `kaggle quota`.
- Mỗi khoản ghi vào **sổ chi phí** trong Postgres, theo worker và theo task.
- **Chạm hạn mức hoặc hết quota:** worker đó ngừng nhận việc. Việc đang chờ chuyển sang worker khác đang bật. Báo cho chủ qua web và ntfy: worker nào hết, còn bao nhiêu, khi nào làm mới.

### 4.7 Bên trong
- **Một hàng đợi chung:** mọi worker đều lấy việc từ gateway theo cùng một giao thức (`/worker/jobs`, heartbeat, session).
- **Worker chuyển tiếp (relay):** một tiến trình Kite chạy cho mỗi worker kiểu API hoặc máy thuê. Nó lấy việc từ hàng đợi rồi gọi API hoặc URL. Nhờ vậy cách chọn worker, đếm quota, báo lỗi, giữ session và journal dùng chung một chỗ với Kaggle.
- **Notebook Kaggle thu gọn thành bản khởi động:** token đọc từ Kaggle Secrets (có khi bấm tay hoặc bấm trong trình duyệt của Kite), code worker tải từ Kite mỗi lần chạy. Hết cảnh phải khởi động lại vì "notebook cũ".
- **Secret của provider:** lưu trong Postgres, mã hóa bằng khóa nằm trong file 0600 trên máy. Không bao giờ hiện lại đầy đủ trên web, không vào log, không vào prompt.

### 4.8 Trình duyệt Kaggle trong Kite
- Không nhúng thẳng trang kaggle.com vào web được, vì các trang lớn thường chặn bị nhúng. Thay vào đó là **một Chromium chạy trên server**, màn hình của nó truyền lên tab Worker. Chủ thao tác chuột và bàn phím như trình duyệt thường.
- Chủ đăng nhập Kaggle một lần. Phiên đăng nhập được lưu mã hóa, chỉ nằm trên server. Nên đăng nhập bằng **email và mật khẩu Kaggle**, vì đăng nhập bằng Google trong trình duyệt chạy trên server có thể bị Google chặn.
- **Dùng Save Version, không dùng Run All.** Save Version chạy độc lập tới 12 giờ, chạy xong có thể đóng trình duyệt (worker tự tắt sau khi rảnh). Run All trong phiên tương tác thì phải giữ editor mở, và Kaggle có thể tắt phiên khi không có thao tác.
- Chromium chỉ bật khi cần bật worker, rồi tắt (khoảng 0,5 GB RAM khi chạy).
- Nếu "Kite tự bấm" hỏng vì Kaggle đổi giao diện, chủ vẫn bấm tay được trong cùng màn hình đó.

## 5. Web

### 5.1 Các tab

| Tab | Nội dung |
|---|---|
| **Tổng quan** | Văn phòng 3D (mục 5.2). Thanh trên cùng: quota, chi phí hôm nay và tháng này, số việc đang chờ. |
| **Chat** | Chat như với Claude. Lệnh Kite hiện thành thẻ: giao task, tiến độ, kết quả, nút Áp/Commit để chủ xác nhận. |
| **Task** | Danh sách task (đang chạy, xong, lỗi). Chi tiết mỗi task gồm: dòng thời gian các bước, kết quả test (bằng chứng), diff, worker và model đã dùng, chi phí. Nút: Áp, Commit, Hủy, Tạm dừng, Tiếp tục, Gửi thêm chỉ dẫn, đánh giá good/bad. |
| **Repo** | Repo đã kết nối (GitHub URL kèm token chỉ đọc, hoặc đường dẫn trên server), cách cài và chạy test (`.kite.yml`), điều Kite nhớ cho repo. |
| **Provider** | Thêm provider theo loại (4.2), kiểm tra kết nối, quota/chi phí, sửa hoặc xóa. |
| **Worker** | Tạo worker (4.3), công tắc Bật/Tắt, tình trạng thật, trình duyệt Kaggle (4.8), nhật ký bật/tắt. |
| **Lịch sử/Journal** | Dòng sự kiện (bật worker, lỗi, task, chi phí), biểu đồ request, token, tốc độ, lỗi theo ngày. |
| **Bench** | Chạy bench (bộ đo, model, worker), bảng các lần đo (như `kite bench history`), so sánh hai lần đo. |
| **Cài đặt** | Tài khoản (đổi mật khẩu, 2FA), ntfy, điều Kite nhớ cho mọi cuộc chat, cập nhật Kite (phiên bản, nút cập nhật), kiểu truy cập, dọn ổ đĩa, kiểm tra sức khỏe (doctor). |

### 5.2 Tổng quan 3D
- **Kiểu nhìn:** văn phòng 3D isometric, làm bằng React Three Fiber.
- **Worker = một khu bàn làm việc**, có biển tên provider và đèn trạng thái (4.3).
- **Agent = một nhân vật:** agent trưởng (chat), agent code, agent review, đường nhanh. Nhân vật ngồi ở khu của worker đang chạy nó.
- **Hoạt động lấy từ bước thật của task**, đẩy về trình duyệt qua WebSocket: đọc code, viết bản sửa, chạy test, review, chờ worker, xong hoặc lỗi.
- **Nguyên tắc: chỉ vẽ trạng thái thật**, không có chuyển động trang trí giả. Không có việc thì nhân vật ngồi yên hoặc rời bàn.
- Bấm vào nhân vật hoặc khu bàn thì mở bảng chi tiết task hoặc worker.
- Máy yếu hoặc điện thoại có chế độ xem danh sách 2D.
- Mô hình 3D chỉ dùng tài nguyên có giấy phép rõ ràng (ví dụ CC0). Không lấy lại hình nhân vật của dự án khác khi chưa rõ giấy phép.
- **Tham khảo:** Claw3D (văn phòng 3D cho agent, React Three Fiber, MIT), AI Office (diorama isometric, React + Three.js, MIT; chỉ là giả lập), Pixel Agents (hình cử động theo đúng việc agent đang làm), agents-home (Zustand dùng chung cho thế giới 3D và giao diện).

### 5.3 Chat
- Đợt đầu dùng **server chạy nền của OpenCode** làm backend chat, giữ nguyên cấu hình agent trưởng và tool MCP của Kite. Chat trên web và opencode trên terminal dùng chung một cách làm.
- Web hiện tin nhắn streaming và hiện lệnh tool thành thẻ. Những thao tác ghi (áp patch, commit, cập nhật Kite, bench) luôn có nút xác nhận của chủ.
- Sau này mới cân nhắc tự chạy vòng agent trưởng trong BE, nếu OpenCode server không đáp ứng được.

### 5.4 Đăng nhập và bảo mật
- **Tài khoản admin** tạo lúc cài. Mật khẩu băm bằng argon2. Bắt buộc **2FA (TOTP)**, khóa đăng nhập tạm thời sau nhiều lần sai.
- **Cookie phiên:** HttpOnly, Secure, SameSite=Strict; có chống CSRF cho mọi thao tác ghi.
- **Bắt buộc HTTPS.** Firewall chỉ mở 443; SSH chỉ cho IP của chủ.
- Gateway `/v1` và `/kite/*` không ra Internet. Web BE gọi chúng qua mạng nội bộ.
- **Nhật ký kiểm tra (audit log)** ghi các thao tác nhạy cảm: thêm hoặc sửa provider, bật worker trả tiền, áp patch, commit, cập nhật, đăng nhập.
- **IP "chỉ mình biết" không phải lớp bảo vệ.** Bot quét toàn bộ Internet, nên lớp bảo vệ là HTTPS, đăng nhập, 2FA và firewall.

### 5.5 Công nghệ
- **BE:** Python 3.12, FastAPI, SQLAlchemy và Alembic (migration), WebSocket. Dùng lại trực tiếp `KiteOps`, task store, journal, memory và bench của Kite.
- **DB:** Postgres, có sẵn trong compose; database riêng cho web, tách khỏi database của Temporal.
- **FE:** React, TypeScript, Vite, React Three Fiber, drei, Zustand. Bộ component UI chọn khi bắt đầu FE.
- **Trình duyệt từ xa:** Chromium điều khiển bằng Playwright, màn hình truyền qua noVNC hoặc CDP screencast. Chọn cụ thể khi làm.

### 5.6 Dữ liệu chính (Postgres)
`users`, `sessions`, `providers` (loại, tên, secret đã mã hóa), `workers` (cấu hình, công tắc, tình trạng), `worker_events`, `spend_ledger`, `repos`, `settings`, `audit_log`.

Task, journal, memory và bench giữ chỗ lưu hiện tại trước, sau đó mới chuyển dần vào Postgres nếu cần.

**Đã chốt khi làm P3 (23/11):** worker và provider vẫn nằm trong `config.yaml`, key nằm trong file 0600, để lệnh `kite worker ...` và web luôn thấy cùng một danh sách; bảng `providers`, `workers`, `spend_ledger` chưa cần. Bảng của web (`users`, `sessions`, `login_attempts`, `audit_log`, `repos`, `settings`) dùng SQLite trong thư mục state khi chạy trên máy hiện tại, Postgres khi triển khai bằng compose (P6). Migration dùng bảng `schema_version` và các bước viết trong code thay cho Alembic, vì mới có một phiên bản schema.

## 6. Triển khai (làm sau cùng)
1. **Hạ tầng (theo cloud):** Terraform cho AWS tạo EC2 t3.medium, EBS cho `/var/lib/kite`, security group (443, SSH theo IP chủ), Elastic IP, IAM tối thiểu. State lưu trong S3. GitHub Actions đăng nhập AWS bằng OIDC, không giữ AWS key dài hạn. Máy có sẵn ở nơi khác thì bỏ qua bước này.
2. **Bộ cài (mọi máy Ubuntu):** chạy lại bao nhiêu lần cũng được. Cài Docker, tạo thư mục dữ liệu, sinh secret ngay trên máy, ghi cấu hình mặc định, chọn kiểu truy cập (sslip.io, tên miền, hoặc mạng riêng), dựng compose, cài dịch vụ tự cập nhật, tạo tài khoản admin. Máy mới thì cloud-init gọi bộ cài lúc khởi động lần đầu.
3. **CI:** test (đã có), build image (Kite, sandbox, web), đẩy GHCR với nhãn theo commit. Chỉ gắn nhãn `stable` khi test xanh trên `main`.
4. **CD:** dịch vụ trên máy kiểm tra nhãn `stable`, kéo image mới, `compose up`, kiểm tra sức khỏe, lỗi thì quay về bản trước. Có nút cập nhật trong tab Cài đặt.
5. **Chuyển máy:** đưa config, secret, journal, memory, cache thư viện từ máy cũ sang. Máy mới chạy ổn thì mới đổi; máy cũ tiếp tục làm máy phát triển.

## 7. Thứ tự làm
| Đợt | Nội dung | Cần GPU? |
|---|---|---|
| P1. Provider lõi | Bảng `providers`, `workers`, `spend_ledger`; relay OpenAI-compatible; adapter Anthropic; giao việc theo worker rảnh, giữ worker theo chat/task, chuyển khi chết hoặc hết hạn mức; báo hết quota qua ntfy. Test bằng API giả. | Không. Thử thật với API thì cần key và tốn ít tiền. |
| P2. Kaggle mới và máy thuê | Notebook khởi động (Secrets + tải code), trình duyệt trong Kite và công tắc tự bấm, adapter RunPod. | Thử Kaggle thì cần |
| P3. Web BE | FastAPI, đăng nhập (admin + 2FA), API và WebSocket cho tất cả các tab, audit log. | Không |
| P4. Web FE | Khung trang và các tab Provider, Worker, Task, Repo, Journal, Bench, Cài đặt; Chat qua OpenCode server. | Không (thử chat thật thì cần) |
| P5. Tổng quan 3D | Văn phòng 3D lấy trạng thái thật, chế độ 2D. | Không |
| P6. Triển khai | Compose đầy đủ, bộ cài, CI build image, CD tự cập nhật, Terraform AWS, chuyển máy. | Không |

Mỗi đợt giao bằng patch như bây giờ. Mỗi đợt có test tự động; không có test nào gọi Kaggle thật, API trả tiền thật hay push.

## 8. Tiêu chí nghiệm thu
- **P1:** hai worker giả (một miễn phí, một trả tiền) cùng rảnh thì việc vào worker miễn phí. Worker trả tiền chạm hạn mức thì việc chuyển sang worker khác và có thông báo. Một task giữ nguyên worker từ đầu đến cuối. Secret không xuất hiện trong log hay journal.
- **P2:** notebook bản khởi động chạy được với token từ Secrets. Bật Kaggle từ tab Worker, chủ tự bấm, thì worker kết nối mà không cần đẩy qua API. Công tắc tự bấm tắt thì Kite không tự thao tác trên kaggle.com.
- **P3/P4:** không đăng nhập thì không gọi được API nào (trừ trang đăng nhập). Sai 2FA thì bị chặn. Áp và commit luôn cần chủ bấm.
- **P5:** mọi chuyển động 3D đều ứng với một sự kiện thật trong Kite; không có sự kiện thì không có chuyển động.
- **P6:** từ một máy Ubuntu trống, một lệnh cài là có Kite chạy qua HTTPS. Đẩy lên `main` với test xanh thì máy tự lên bản mới; bản lỗi thì tự quay về.

## 9. Rủi ro và việc còn mở
- **Điều khoản Kaggle:** phần lớn trang cấm truy cập tự động ngoài API chính thức. "Kite tự bấm" mặc định tắt; chủ đọc điều khoản trước khi bật. Mỗi người một tài khoản Kaggle.
- **Đăng nhập Google** trong trình duyệt chạy trên server có thể bị chặn, nên dùng email và mật khẩu Kaggle.
- **Kaggle đổi giao diện** thì cách tự bấm hỏng; bấm tay vẫn dùng được.
- **Chi phí:** máy t3.medium khoảng 30 USD/tháng, cộng máy cũ trong lúc chạy song song. API trả tiền chỉ tốn khi chủ thêm provider và bật worker.
- **OpenCode server** đổi API thì chat web phải sửa theo. Phiên bản OpenCode được ghim.
- **3D trên máy yếu:** có chế độ 2D.
- **Đã chốt (23/11):** FE dùng Mantine; RunPod: chủ tạo sẵn pod (image vLLM hoặc llama-server, cổng HTTP), Kite chỉ bật/tắt qua REST API v2 và đọc câu trả lời dạng stream (proxy RunPod cắt request im lặng quá 100 giây); trình duyệt Kaggle: Chromium + Playwright, màn hình qua CDP screencast trên WebSocket; mô hình 3D chỉ dùng tài nguyên CC0; tạm dùng sslip.io.
- **Còn mở:** tên miền riêng sau này.

## 10. Tham khảo
- Claw3D: https://github.com/iamlukethedev/claw3d
- AI Office: https://github.com/Gaurav2693/ai-office
- Pixel Agents: https://github.com/pixel-agents-hq/pixel-agents
- agents-home: https://github.com/Litas-dev/agents-home
- Kaggle Terms of Use: https://www.kaggle.com/terms
- OpenCode server: https://opencode.ai/docs/server/
- sslip.io: https://sslip.io
