# Chuẩn bench Kite v1 — đặc tả đóng băng

> **Trạng thái: ĐÓNG BĂNG (v1).** Mọi thay đổi sau đây đều phải qua version mới
> (mục 7), không sửa lặng lẽ tài liệu này: đổi ngưỡng, đổi công thức, đổi
> trường bắt buộc, đổi tập bài gác cổng.
>
> Mã thi hành đặc tả này: `poc/kaggle_worker/bench_standard.py` (validator)
> và `poc/kaggle_worker/bench_criteria.py` (khung tiêu chí). Tài liệu này là
> văn bản con người đọc được của đúng hai module đó — chỗ nào hai bên lệch
> nhau thì **code là chuẩn sau cùng cho máy, tài liệu này là chuẩn sau cùng
> cho người đọc**, và lệch đó là một lỗi phải sửa (báo agent trưởng Gói 7),
> không tự chọn một bên.
>
> Gốc: plan chính thức `ke-hoach-toi-uu-ui-kite.md` (Gói 7, Q9/Q11) +
> hợp đồng `kite-ui-build/HOP-DONG-GIAO-TIEP.md` (mục Gói 7). Chuẩn này mượn
> hình thư mục của Terminal-Bench và hình `repo` + `base_commit` của
> SWE-bench, nhưng là chuẩn riêng của Kite: bài chấm hệ thống Kite qua
> đường thật (lead nhận → tách task → worker trong sandbox → patch ra),
> trên 8 trục.

## 1. Thư mục bài

Một bài là một thư mục, đặt tên theo `id` của bài:

```
<bench-root>/<task-id>/
  task.yaml          # bắt buộc: mô tả bài (mục 2)
  files/             # điểm bắt đầu cho kind=code|qa|terminal (hoặc thay bằng repo+base_commit)
  tests/             # test ẩn: bắt buộc với kind=code|repo|terminal; xem mục 2
  solution.*         # tùy chọn: bản sửa tham chiếu, chỉ để kiểm thử nội bộ của Kite
```

Quy tắc:

- `task.yaml` bắt buộc, luôn ở gốc thư mục bài.
- Điểm bắt đầu là **một trong hai**: thư mục `files/` (repo mẫu viết sẵn,
  test trong đó đang fail) **hoặc** cặp `repo` + `base_commit` trong
  `task.yaml` (repo thật, checkout ở commit cha của bản sửa — kiểu
  SWE-bench, ánh xạ sang `RepoTask` đã có trong `bench.py`).
  `kind=repo` bắt buộc dùng `repo` + `base_commit`; các kind còn lại được
  dùng `files/` thay cho repo.
- `tests/` là **test ẩn**: agent không bao giờ thấy. Kite chỉ ghi chúng vào
  workspace sau khi agent xong và đã chạy test của chính nó (theo đúng
  `bench.py`: `add_hidden_tests` / `hidden_files`). Bài đạt chỉ khi test
  ẩn cũng qua.
- `solution.*` tùy chọn, chỉ để Kite tự kiểm fixture của mình (bài mẫu
  phải fail lúc giao, pass với bản sửa tham chiếu). Không bao giờ đưa
  `solution.*` cho agent.
- Bài **đa lượt hội thoại** (nhiều vòng hỏi-đáp với agent) **NGOÀI v1**:
  validator không cấm trường nào cho nó, nhưng không có kind nào diễn tả
  nó và driver v1 không chạy nó. Thêm kind đa lượt = version mới.

## 2. `task.yaml` — mọi trường

Kiểu dữ liệu: YAML mapping ở cấp cao nhất (dùng `yaml.safe_load`). Khóa lạ
ở cấp cao nhất được phép và bị bỏ qua (để importer Terminal-Bench /
SWE-bench mang thêm metadata), nhưng mọi trường dưới đây nếu có thì phải
đúng kiểu — validator không im lặng bỏ qua trường sai kiểu.

### 2.1 Trường bắt buộc (mọi bài)

| Trường | Kiểu | Quy tắc |
|---|---|---|
| `id` | chuỗi | Không rỗng, tối đa 128 ký tự, chỉ chữ/số/`. _ -`, bắt đầu bằng chữ/số. Duy nhất trong một tập bài. |
| `goal` | chuỗi | Không rỗng. Mô tả việc cần làm, viết như giao cho agent (tiếng Việt hoặc tiếng Anh theo `language`). |
| `kind` | enum | Một trong `code`, `repo`, `scenario`, `qa`, `terminal` (mục 2.3). |
| `language` | enum | `vi` hoặc `en`. Ngôn ngữ của `goal` và của câu trả lời kỳ vọng. |
| `axes` | danh sách | Không rỗng, tập con của 8 trục (mục 3), không trùng. Bài chỉ chấm đúng các trục khai ở đây. |
| `difficulty` | chuỗi không rỗng hoặc số dương | Mức khó do tác giả bài đặt (vd `"dễ"` / `"khó"` / `3`). v1 không chuẩn hóa thang difficulty — chỉ dùng để lọc/hiển thị, không vào công thức điểm. |
| `timeouts` | mapping | Không rỗng; mọi giá trị là số giây dương (vd `{setup: 600, test: 300}`). Tên khóa tự do (`setup`, `test`, ...), giá trị nào cũng phải dương. |
| `environment` | mapping | Xem mục 2.4. |

### 2.2 Trường tùy chọn theo trục / theo kind

| Trường | Kiểu | Khi nào có |
|---|---|---|
| `par_seconds` | số dương (giây) | **Bắt buộc khi `axes` có Tốc độ** (mục 3). Thời gian chuẩn để làm xong bài. |
| `lesson_ref` | chuỗi không rỗng | **Bắt buộc khi `axes` có Trí nhớ** (mục 3). Trỏ tới bài học trong tài liệu kinh nghiệm (vd `lessons/2026-11-20.md#mean`) mà bài này kiểm xem hệ có áp dụng không. |
| `expected_files` | danh sách chuỗi không rỗng | **Bắt buộc khi `axes` có Phân tích hoặc Đúng việc** (mục 3). Các file bài làm đúng phải chạm tới (dùng để kiểm "làm đúng việc" thay vì sửa lung tung). |
| `forbid_tools` | danh sách chuỗi | Tùy chọn. Tool agent không được gọi trong bài này (vd `["bash"]`). Khác `scenario.forbid`: đây là cấm ở cấp bài, áp cho mọi đường chạy của bài. |
| `scenario` | mapping | **Bắt buộc khi `kind=scenario`** (mục 2.5). |
| `fault` | mapping | Tùy chọn, cho trục Ổn định (mục 4). `{type: <chuỗi không rỗng>, ...}` — vd `{type: kill-gpu}`: bài chích lỗi để đo khả năng phục hồi. Nội dung ngoài `type` là tự do ở v1. |
| `repo` | chuỗi không rỗng | **Bắt buộc khi `kind=repo`**; đi cùng `base_commit`. Các kind khác cũng được khai cặp này thay cho `files/`. |
| `base_commit` | chuỗi | SHA git, 7–40 ký tự hex. **Bắt buộc khi `kind=repo`**; luôn đi cùng `repo` (khai một mà thiếu một là invalid). |

### 2.3 `kind` — năm loại bài

| `kind` | Chạy bằng gì (đã có trong `bench.py`) | Cần `tests/` | Ghi chú |
|---|---|---|---|
| `code` | `CodeTask` + `score_code` | Có | Repo mẫu nhỏ, test đang fail. |
| `repo` | `RepoTask` + `score_code` | Có | Repo thật ở `base_commit`, test ẩn lấy từ commit sửa thật. |
| `scenario` | `Scenario` + `score_scenario` | Không | Kịch bản agent trưởng, kết quả tool viết sẵn, chấm xác định. Cần khối `scenario:` (mục 2.5). |
| `qa` | Checker riêng của bài (worker (b) định nghĩa) | Không | Hỏi-đáp gắn với task: bài chỉ đạt khi ánh xạ được thành task qua đường Kite thật (theo quyết định Q9-phụ: bench hỏi-đáp thuần ngoài Gói 7 đợt đầu, trừ khi ánh xạ được thành task). |
| `terminal` | Như `code`, chạy lệnh trong terminal | Có | Bài dạng Terminal-Bench thuần (lệnh + kiểm trạng thái cuối). |

### 2.4 `environment:` — setup và khai báo mạng duy nhất

```yaml
environment:
  setup: "pip install -q -r requirements.txt"   # tùy chọn, chuỗi không rỗng
  dockerfile: "Dockerfile"                      # tùy chọn, chuỗi không rỗng
  requires:
    gpu: true                                    # tùy chọn, phải là true/false
  network:
    hosts: ["api.example.com"]                   # xem mục 5
```

Quy tắc:

- `requires.gpu` (nếu có) phải là boolean. `requires` **không được** chứa
  khóa `network` — khai báo mạng duy nhất ở `environment.network` (mục 5);
  `requires.network` là invalid.
- `setup` / `dockerfile` (nếu có) phải là chuỗi không rỗng.
- `network` (nếu có) là mapping chỉ chứa đúng khóa `hosts` (mục 5).

### 2.5 Khối `scenario:` — gương của `bench.Scenario`

Tên trường giữ nguyên tên trong `bench.py` (`Scenario` ~dòng 200) để
validator và `score_scenario` (~dòng 359) nói cùng một ngôn ngữ:

```yaml
scenario:
  prompt: "Giao task ..."            # bắt buộc, chuỗi không rỗng
  expect: ["start_task"]             # bắt buộc, danh sách tên tool; rỗng = trả lời không cần tool
  category: "giao việc"              # tùy chọn, chuỗi không rỗng
  args: {repo: "kite-sandbox"}       # tùy chọn, mapping tên-tham-số -> regex (chuỗi)
  results: {task_status: {...}}      # tùy chọn, mapping: kết quả tool viết sẵn
  must: ["chat-bench-0001"]          # tùy chọn, danh sách regex câu trả lời phải chứa
  must_not: ["đã áp dụng xong"]      # tùy chọn, danh sách regex câu trả lời không được chứa
  forbid: ["bash"]                   # tùy chọn, danh sách tool không bao giờ được gọi
  count: {start_task: 2}             # tùy chọn, mapping tên-tool -> số nguyên >= 1
  need: ["send_instruction"]         # tùy chọn, danh sách tool phải được gọi
  optional: false                    # tùy chọn, true/false
```

## 3. Tám trục và bảng trường bắt buộc theo trục

8 trục v1 (đúng thứ tự này trên radar — `bench_criteria.AXES`):

`Coding`, `Phân tích`, `Suy luận`, `Tốc độ`, `Đúng việc`, `Chi phí`,
`Ổn định`, `Trí nhớ`.

| Trục | Trường bắt buộc khi khai trục | Cách chấm trong một lần chạy (mục 6) |
|---|---|---|
| Coding | — | Theo bài (pass → 100, fail → 0). |
| Phân tích | `expected_files` | Theo bài (pass → 100, fail → 0). |
| Suy luận | — | Bài `scenario`: điểm `score_scenario` × 100. |
| Tốc độ | `par_seconds` | `100 × min(1, par_seconds / wall_seconds)` (mục 6). |
| Đúng việc | `expected_files` | Theo bài (pass → 100, fail → 0); kiểm thêm file đổi có nằm trong `expected_files`. |
| Chi phí | — | Cấp lần chạy (mục 6), không chấm theo bài. |
| Ổn định | — | Cấp lần chạy + bài `fault:` tùy chọn (mục 4). Không bài đơn nào diễn tả được một mình. |
| Trí nhớ | `lesson_ref` | Theo bài (pass → 100, fail → 0); bài phải đòi áp dụng đúng bài học ở `lesson_ref`. |

**Luật validator:** trục nào khai trong `axes:` mà thiếu trường bắt buộc
của nó thì **cả bài invalid** — lỗi có cấu trúc
`{field: <tên trường>, code: "missing_required_for_axis", message: ...}`
(liệt kê đủ các trục cần trường đó). Không có ngoại lệ "khai trục rồi
bổ sung trường sau".

## 4. Trục Ổn định — đo ở cấp lần chạy

Không bài đơn nào diễn tả được Ổn định, nên v1 đo nó ở cấp lần chạy:

1. **Mọi bài trong lần chạy đều đóng góp** một điểm Ổn định theo bài:
   `100` nếu bài chạy xong mà không gặp lỗi hạ tầng phải chạy lại
   (lỗi thuộc `INFRA_ERRORS` trong `bench.py` — worker mất, hết context,
   ...), `0` nếu bài phải chạy lại vì lỗi hạ tầng.
2. **Bài chích lỗi tùy chọn** (`fault:`): bài khai `fault:` chấm Ổn định
   bằng pass/fail của chính nó (hệ phục hồi / thất bại êm → pass → 100),
   thay cho điểm theo quy tắc 1.
3. Điểm trục Ổn định của lần chạy = trung bình các điểm theo bài ở trên
   (như mọi trục khác — mục 6).

## 5. Chế độ chạy có mạng — `network.hosts`

Khai báo mạng **duy nhất** ở `environment.network.hosts`. Đây là khai báo,
không phải cho phép: chủ duyệt từng bench có mạng trước khi chạy (worker
(c) làm UI xác nhận + kiểm hosts khớp khai báo; quyết định duyệt + host +
log egress ghi vào journal).

```yaml
environment:
  network:
    hosts: ["api.example.com", "pypi.org"]
```

Quy tắc validator (áp cho mọi entry, sai một entry là cả bài invalid):

- **Hostname trần**: không scheme (`https://...` là sai), không đường dẫn,
  không cổng (`host:443` là sai), không khoảng trắng.
- **Không wildcard**: `*` ở bất cứ đâu là sai (`*.example.com` sai).
- **Không IP trần**: `1.2.3.4`, `::1` đều sai — chỉ hostname.
- **Tối đa 10 entry** (`MAX_NETWORK_HOSTS = 10`, hằng số có tên trong
  `bench_standard.py`). Entry thứ 11 trở đi là invalid.
- **Rỗng là invalid**: `network: {hosts: []}` hoặc `network:` thiếu `hosts`
  đều làm bài invalid. Bài offline thì **bỏ hẳn khối `network:`**,
  không khai rỗng.
- Nhãn hostname: chữ/số/gạch nối, không bắt đầu/kết thúc bằng gạch nối,
  không nhãn rỗng, dài tối đa 253 ký tự. Tên một nhãn (`localhost`) được
  phép.

Thực thi (worker (c), không thuộc đặc tả này nhưng đặc tả ghi để khỏi
lệch): container chạy bench ở lại mạng `--internal`, egress proxy (tầng 2)
chỉ forward host đã khai báo; test/check giữ `network="none"`; setup giữ
luật hiện tại; override là bản sao theo từng task, không mutate settings
chung; bench có mạng **không vào tập gác cổng** (mục 8).

## 6. Khung tiêu chí — điểm, overall, mức Dreyfus

Mã thi hành: `poc/kaggle_worker/bench_criteria.py`
(`CRITERIA_VERSION = "v1"`).

### 6.1 Điểm theo bài (0–100)

| Loại bài | Điểm của bài trên mỗi trục nó khai |
|---|---|
| `code` / `repo` / `terminal` | `100` nếu `score_code` pass, `0` nếu fail (kể cả trượt test ẩn, sửa file test, lỗi check). |
| `scenario` | Điểm `score_scenario` × 100 (0–100). Áp cho trục Suy luận và mọi trục khác bài khai. |
| Bài khai Tốc độ | Riêng trục Tốc độ: `100 × min(1, par_seconds / wall_seconds)` — làm xong trong `par_seconds` được 100, chậm gấp đôi được 50. `wall_seconds` là thời gian toàn bộ lần chạy Kite của bài (không tính test ẩn). |
| Bài khai `fault:` | Riêng trục Ổn định: `100` nếu pass, `0` nếu fail. |

### 6.2 Điểm một trục trong một lần chạy

Trung bình (không trọng số) các bài chấm trục đó trong lần chạy, làm tròn
**1 chữ số thập phân, half-up** (`round_score`). Trục không có bài nào
chấm trong lần chạy thì điểm là `null` (không phải 0).

Sáu trục chấm theo bài: Coding, Phân tích, Suy luận, Tốc độ, Đúng việc,
Trí nhớ. Hai trục chấm ở **cấp lần chạy**: Chi phí, Ổn định.

- **Chi phí** (cấp lần chạy): `actual` = trung vị số phút GPU mỗi bài
  trong lần chạy (rơi về tổng token nếu không có dữ liệu GPU);
  `baseline` = cùng đại lượng ở lần chạy chính thức gần nhất của cùng
  tập gác cổng + cùng criteria version. Điểm = `100 × min(1, baseline /
  actual)`, làm tròn 1 chữ số thập phân. Lần chạy đầu tiên của một tập
  gác cổng chưa có baseline → trục Chi phí là `null` (lần chạy đó làm
  baseline, không có overall — xem mục 6.3).
- **Ổn định** (cấp lần chạy): theo mục 4.

### 6.3 Overall một lần chạy

Trung bình **không trọng số của cả 8 trục**, làm tròn **1 chữ số thập
phân, half-up**, rồi mới tra mức. **Cần đủ 8 trục**: thiếu một trục
(`null`) thì overall của lần chạy là `null` — không lấy trung bình trên
trục còn lại. Làm tròn dùng half-up (không dùng `round()` mặc định của
Python — nó làm tròn banker's, `19.95` thành `19.9` thay vì `20.0`,
làm bài rơi xuống mức thấp hơn ngay tại biên).

### 6.4 Thang Dreyfus — 5 mức, song ngữ, ngưỡng đều 20 điểm

| Mức | Tên EN | Tên VI | Khoảng (overall đã làm tròn) |
|---|---|---|---|
| L1 | Novice | Mới bắt đầu | [0, 20) |
| L2 | Advanced Beginner | Mới có kinh nghiệm | [20, 40) |
| L3 | Competent | Vững việc | [40, 60) |
| L4 | Proficient | Thành thạo | [60, 80) |
| L5 | Expert | Chuyên gia | [80, 100] |

Điểm đúng tại biên thuộc mức cao hơn (`20.0` là L2, `80.0` là L5...);
`100` là L5 (mức cuối đóng cả hai đầu). Tên EN + VI là hằng số dữ liệu
trong `bench_criteria.LEVELS`; key i18n trên giao diện là
`bench.level.<n>.name` / `bench.level.<n>.desc` (n = 1..5) — giao diện
dịch qua key này, không hard-code tên mức vào UI.

### 6.5 Overall chính thức

- Overall chính thức = trung bình (không trọng số, làm tròn 1 chữ số thập
  phân half-up) các lần chạy **cùng criteria version và cùng tập gác cổng**,
  tối thiểu **3 lần** (`MIN_OFFICIAL_RUNS = 3`).
- Dưới 3 lần: vẫn tính và hiển thị số overall, nhưng phải hiển thị
  **"chưa chính thức"** và **không hiển thị mức Dreyfus** của overall đó
  (mức từng lần chạy vẫn hiển thị).
- Mức Dreyfus của overall chính thức tra từ overall chính thức đã làm tròn.
- **Cờ `level_unstable`**: `true` khi các lần chạy dùng để tính overall
  chính thức không cùng nằm trong một mức Dreyfus (chúng cắt qua biên
  ngưỡng — vd một lần 79.9 (L4), một lần 80.0 (L5)). Cờ này được đóng dấu
  lên từng bản ghi lần chạy trong tập đó (xem mục 9). `false` khi mọi lần
  cùng một mức; `null` với lần chạy không thuộc tập đang xét (khác version
  / khác tập gác cổng / chưa có overall).
- Lần chạy khác criteria version **không bao giờ** được trung bình chung:
  đổi version là đổi thước đo.

## 7. Versioning tiêu chí

`criteria_version` hiện tại: **`v1`**.

Đổi bất cứ thứ nào sau đây thì phải ra version mới (`v2`, ...), không sửa
v1 tại chỗ:

- Ngưỡng mức Dreyfus (kể cả đổi 20 điểm đều sang ngưỡng khác).
- Công thức overall / công thức điểm theo bài / công thức Chi phí / Ổn định.
- Trường bắt buộc trong `task.yaml` (thêm/bớt/đổi nghĩa).
- Tập bài gác cổng (mục 8) — đổi danh sách bài là đổi thước đo.

Version mới:

- Hiển thị kèm mọi overall/mức (`criteria_version` trong mọi bản ghi và
  trên giao diện — mức không bao giờ hiện mà không kèm version).
- Overall chính thức chỉ trung bình các lần chạy cùng version (mục 6.5).
- Dữ liệu cũ giữ nguyên version cũ; không viết lại lịch sử sang version mới.

## 8. Tập bài "gác cổng" — cố định

- Tập gác cổng (`gate_set`, tên v1: **`gate-v1`**) là danh sách bài cố định
  dùng để chống thoái cấp: điểm overall chính thức chỉ tính trên tập này.
- Tập này **không đổi** trong một criteria version (đổi danh sách = đổi
  version, mục 7). Nội dung danh sách (từng `task-id`) do worker (b) chốt
  khi nạp bài — đặc tả này chỉ đóng luật, không liệt kê bài, vì bài còn
  đang được importer mang về.
- **Bench có mạng KHÔNG vào tập gác cổng**: nó phụ thuộc dịch vụ sống bên
  ngoài, không tái lập ổn định. Bench có mạng vẫn chạy, vẫn ghi điểm, vẫn
  đóng dấu `network:` — chỉ không được tính vào overall chính thức.
- Chạy bench lẻ (không thuộc tập gác cổng) vẫn hợp lệ để xem điểm từng
  trục, nhưng không đóng góp vào overall chính thức.

## 9. Đóng dấu mỗi lần chạy + schema `results.json` mở rộng

### 9.1 Đóng dấu (stamp)

Mỗi lần chạy đóng dấu (hàm `bench_criteria.make_run_stamp`):

```json
{
  "criteria_version": "v1",
  "system_config": {
    "worker": "kaggle-han",
    "model": "qwen3.6",
    "harness": "poc/kaggle_worker/bench.py",
    "harness2": "poc/kaggle_worker/harness2.py"
  },
  "network": {"mode": "offline", "hosts": []}
}
```

- `system_config`: worker/model đã chạy + phiên bản harness đã lái nó
  (`harness` = `bench.py`, `harness2` = `harness2.py` — ghi đủ cả hai để
  biết lần chạy dùng đường nào; thêm khóa khác được, bớt hai khóa này
  thì không đủ đóng dấu).
- `network`: `{"mode": "offline", "hosts": []}` khi chạy offline;
  `{"mode": "allowlist", "hosts": [...]}` khi chạy có mạng — hosts là
  danh sách đã được chủ duyệt cho lần chạy đó (phải khớp khai báo trong
  `task.yaml`, worker (c) kiểm).
- Thêm `gate_set` (mục 8) vào bản ghi lần chạy (không nằm trong stamp mà
  nằm cùng cấp với nó — xem mục 9.2).

### 9.2 Schema mở rộng cho `results.json`

**Tương thích:** `run_bench` / `history` trong `bench.py` hôm nay ghi
`{"started", "finished", "models": {model: {speed, lead, code}}, ...}`.
Schema mới **chỉ thêm**, không sửa các khóa cũ: code cũ đọc file cũ vẫn
chạy; code mới đọc file cũ thì coi phần `criteria` là vắng (lần chạy
trước v1 không có điểm trục/overall).

Phần mới là khóa **`criteria`** ở cấp cao nhất, do driver (worker (c))
ghi thêm khi chạy bench v1:

```json
{
  "started": "2026-11-25 21:00",
  "finished": "2026-11-25 21:40",
  "models": { "...": {"speed": {}, "lead": [], "code": []} },
  "criteria": {
    "criteria_version": "v1",
    "gate_set": "gate-v1",
    "runs": [
      {
        "run_id": "bench-20261125-2100-qwen3.6",
        "started": "2026-11-25 21:00",
        "criteria_version": "v1",
        "gate_set": "gate-v1",
        "system_config": {"worker": "kaggle-han", "model": "qwen3.6",
                          "harness": "poc/kaggle_worker/bench.py",
                          "harness2": "poc/kaggle_worker/harness2.py"},
        "network": {"mode": "offline", "hosts": []},
        "tasks": [
          {"task_id": "C1-bugfix", "axes": ["Coding"], "score": 100.0},
          {"task_id": "L01", "axes": ["Suy luận"], "score": 87.5}
        ],
        "axes": {"Coding": 82.5, "Phân tích": 70.0, "Suy luận": 68.0,
                 "Tốc độ": 75.0, "Đúng việc": 80.0, "Chi phí": 90.0,
                 "Ổn định": 100.0, "Trí nhớ": 60.0},
        "overall": 78.2,
        "level": {"level": 4, "name_en": "Proficient", "name_vi": "Thành thạo",
                  "name_i18n_key": "bench.level.4.name",
                  "desc_i18n_key": "bench.level.4.desc",
                  "range": [60, 80]},
        "level_unstable": false
      }
    ],
    "official": {
      "criteria_version": "v1",
      "gate_set": "gate-v1",
      "overall": 77.8,
      "official": true,
      "runs": 3,
      "excluded_runs": 0,
      "level": {"level": 4, "name_en": "Proficient", "name_vi": "Thành thạo",
                "name_i18n_key": "bench.level.4.name",
                "desc_i18n_key": "bench.level.4.desc",
                "range": [60, 80]},
      "level_unstable": false
    }
  }
}
```

Quy ước trường:

| Trường | Kiểu | Ghi chú |
|---|---|---|
| `criteria.runs[]` | danh sách | Mỗi phần tử là một lần chạy (một model, một tập gác cổng, một thời điểm). |
| `runs[].run_id` / `started` | chuỗi | Do driver đặt (duy nhất trong thư mục bench). |
| `runs[].tasks[]` | danh sách | Điểm từng bài: `task_id`, `axes` (trục bài này chấm), `score` (0–100, điểm chung của bài; riêng trục Tốc độ tính theo mục 6.1 — driver ghi `score` chung và `scores_by_axis` khi khác nhau, xem dưới). |
| `runs[].tasks[].scores_by_axis` | mapping, tùy chọn | Chỉ ghi khi điểm theo trục khác điểm chung (vd bài khai Tốc độ: `{"Tốc độ": 50.0}` trong khi `score` chung là 100). |
| `runs[].axes` | mapping 8 trục → số/`null` | Điểm từng trục (mục 6.2). Khóa là đúng tên tiếng Việt trong mục 3. |
| `runs[].overall` | số/`null` | Mục 6.3. |
| `runs[].level` | object/`null` | `level_to_dict(level_for(overall))`; `null` khi overall `null`. |
| `runs[].level_unstable` | true/false/`null` | Mục 6.5; `null` khi chưa xét trong tập nào. |
| `criteria.official` | object | Đúng output của `bench_criteria.official_overall` (mục 10). |

`GET /api/bench/history` (đã có) mở rộng tương tự, mỗi dòng lịch sử thêm:
`criteria_version`, `gate_set`, `overall`, `level`, `level_unstable`,
`radar` (mảng `radar_data` — mục 10), và một khối `official` ở cấp cao
nhất của response. Các trường cũ của response giữ nguyên.

### 9.3 Radar

Radar vẽ 8 trục theo đúng thứ tự mục 3 (bát giác). Dữ liệu radar là output
của `bench_criteria.radar_data(axis_scores)`:
`[{"axis": "Coding", "key": "coding", "score": 82.5}, ...]` — trục chưa
chấm có `score: null` (hiện khoảng trống, không vẽ thành 0). Trang Bench
vẽ chồng 2 lưới để so hai kiến trúc (hai lần chạy / hai model) — đó là
việc hiển thị của worker (c), dữ liệu thì lấy từ hai bản ghi lần chạy.

## 10. Hợp đồng hàm cho worker (b) và (c)

### 10.1 `bench_criteria.py` — hàm thuần, không I/O

| Hàm | Vào / Ra |
|---|---|
| `CRITERIA_VERSION` | `"v1"`. |
| `AXES` | Tuple 8 tên trục (mục 3). `AXIS_KEYS`: tên trục → key ASCII (`coding`, `phan-tich`, `suy-luan`, `toc-do`, `dung-viec`, `chi-phi`, `on-dinh`, `tri-nho`). |
| `LEVEL_STEP = 20`, `MIN_OFFICIAL_RUNS = 3`, `ROUND_DIGITS = 1` | Hằng số chính sách có tên (không hard-code số vào công thức). |
| `LEVELS` | Tuple 5 `Level(number, name_en, name_vi, desc_en, desc_vi, low, high)`. |
| `round_score(value)` | Số → làm tròn 1 chữ số thập phân, half-up. |
| `axis_score(scores)` | Danh sách điểm theo bài (bỏ `None`) → trung bình làm tròn, hoặc `None` khi không có bài nào. |
| `run_overall(axis_scores)` | Mapping 8 trục → overall làm tròn, hoặc `None` khi thiếu trục. |
| `level_for(overall)` | Overall (đã làm tròn) → `Level` hoặc `None`. |
| `level_to_dict(level)` | `Level` → dict JSON như mục 9.2. |
| `level_i18n_key(number, field="name"\|"desc")` | → `"bench.level.<n>.name"` / `".desc"`. |
| `radar_data(axis_scores)` | Mapping 8 trục → danh sách 8 mục `{axis, key, score}` theo thứ tự radar. |
| `make_run_stamp(*, system_config, network=None)` | → stamp mục 9.1. `network=None` = offline. |
| `build_run_result(*, axis_scores, system_config, network=None, gate_set="gate-v1")` | → bản ghi lần chạy (mục 9.2, chưa có `run_id`/`started`/`tasks` — driver thêm). `level_unstable` là `None` ở đây. |
| `official_overall(runs, *, criteria_version="v1", gate_set=None)` | Danh sách bản ghi lần chạy → `{criteria_version, gate_set, overall, official, runs, excluded_runs, level, level_unstable}`. Chỉ trung bình các lần cùng version (và cùng `gate_set` khi truyền vào). |
| `annotate_runs(runs, *, criteria_version="v1", gate_set=None)` | → `(annotated_runs, official_summary)`: đóng `level_unstable` lên từng bản ghi. |

Ví dụ:

```python
from poc.kaggle_worker import bench_criteria as bc

record = bc.build_run_result(
    axis_scores={"Coding": 82.5, "Phân tích": 70.0, "Suy luận": 68.0,
                 "Tốc độ": 75.0, "Đúng việc": 80.0, "Chi phí": 90.0,
                 "Ổn định": 100.0, "Trí nhớ": 60.0},
    system_config={"worker": "kaggle-han", "model": "qwen3.6",
                   "harness": "poc/kaggle_worker/bench.py",
                   "harness2": "poc/kaggle_worker/harness2.py"})
# record["overall"] == 78.2 ; record["level"]["level"] == 4
annotated, official = bc.annotate_runs([record, record2, record3], gate_set="gate-v1")
# official == {"overall": ..., "official": True/False, "runs": 3, ...}
```

### 10.2 `bench_standard.py` — validator

| Hàm | Vào / Ra |
|---|---|
| `load_task_file(path)` | Đường dẫn `task.yaml` → `(BenchTask \| None, [ValidationError])`, kèm kiểm tra thư mục (`files/`/`tests/`). File thiếu/không đọc được = một lỗi `missing_file`, không raise. |
| `load_task_text(text)` | Chuỗi YAML → `(BenchTask \| None, [ValidationError])`. Lỗi cú pháp YAML = một lỗi `yaml_error`. |
| `validate_task_data(data)` | Mapping đã parse → `[ValidationError]` (không chạm filesystem). |
| `parse_task_data(data)` | Mapping → `BenchTask`, hoặc raise `BenchTaskInvalid` (mang `.errors`). |
| `validate_task_layout(task_dir, task)` | Kiểm `files/`/`repo`+`base_commit`/`tests/` trong thư mục bài. |
| `validate_network_hosts(hosts)` | Danh sách hosts → `[ValidationError]` (tái dùng cho server kiểm hosts lúc chủ duyệt bench có mạng). |
| `is_valid_hostname(host)` | Chuỗi → `True`/`False` theo mục 5. |
| `MAX_NETWORK_HOSTS = 10` | Hằng số chính sách có tên. |

`ValidationError` có 3 trường: `field` (vd `environment.network.hosts[2]`,
`par_seconds`), `code` (vd `missing_field`, `invalid_enum`,
`missing_required_for_axis`, `invalid_hostname`, `too_many_hosts`,
`empty_hosts`, `yaml_error`, `missing_file`), `message` (tiếng Việt, đọc
được). `BenchTask` (frozen dataclass) có `to_dict()`.

Ví dụ lỗi:

```python
task, errors = bs.load_task_file(Path("bench/t1/task.yaml"))
# errors == [ValidationError(field="par_seconds",
#   code="missing_required_for_axis",
#   message="trục 'Tốc độ' bắt buộc có trường 'par_seconds' ...")]
```

### 10.3 Đường chạy hiện có — tương thích

- `bench.py` giữ nguyên: `Scenario` / `score_scenario` / `CodeTask` /
  `RepoTask` / `score_code` / `run_bench` / `summarize` / `history` /
  `merge_results` và định dạng `results.json` cũ không đổi. Worker (b)
  chỉ thêm dispatch theo `kind` (gọi đường chạy sẵn có); worker (c) chỉ
  thêm khóa `criteria` (mục 9.2) và gate độc quyền.
- `POST /api/bench/run` giữ nguyên route; thêm xác nhận mạng theo hợp
  đồng (chủ xác nhận hosts, request mang `network_ack: true`, server kiểm
  hosts khớp khai báo bằng `validate_network_hosts` + so với khai báo
  trong `task.yaml`).

## 11. Câu hỏi còn mở (cho agent trưởng / Thuận)

1. `tests/` có bắt buộc với `kind=qa` không? v1 quyết: **không** (qa chấm
   bằng checker riêng). Nếu checker của qa cũng là test ẩn thì đổi thành
   bắt buộc = version mới.
2. Thang `difficulty` chưa chuẩn hóa (mục 2.1): v1 chỉ dùng để lọc/hiển
   thị. Chuẩn hóa thang (vd 1–5 với định nghĩa từng mức) = version mới.
3. Công thức Chi phí (mục 6.2) dùng lần chạy chính thức gần nhất làm
   baseline, nên lần chạy đầu tiên của một tập gác cổng không có overall.
   Nếu muốn overall ngay từ lần đầu thì cần một baseline tuyệt đối
   (ngân sách chi phí theo bài — thêm trường `cost_par` = version mới).

## 12. Ứng viên v2 (chưa chốt — ghi lúc tích hợp Gói 7, không nới v1)

1. **Thêm trường tùy chọn `fix_commit` cho `kind=repo`.** Lý do: v1 không có SHA commit sửa; `RepoTask.fix` hiện là placeholder = `base_commit`, nên `reference_lines` trả 0 và test ẩn lấy từ `tests/` của thư mục bài (dispatch trả kèm trong `outcome["hidden_files"]`), không gọi `hidden_files(task, source)` với bài v1. Phương án khác: v2 quy định test ẩn của `RepoTask` lấy từ thư mục bài thay vì git.
2. **Checker cho `kind=qa`** (thêm trường đáp án kỳ vọng/checker). Lý do: v1 không có trường nào mang đáp án nên bài qa không chấm được — dispatch hiện lỗi rõ ràng theo quyết định Q9-phụ (hỏi-đáp thuần ngoài đợt đầu).
3. **Tập gác cổng `gate-v1` chưa chốt.** Chỉ chốt khi đã có bài thật nhập về + chạy xanh; không chốt vội trên bài mẫu (`tb-hello` là ứng viên thật; `kite-swe-mini` có repo giả, không phải ứng viên).

**Quyết định của agent trưởng Gói 7 về §11 (lúc tích hợp):** (1) `tests/` không bắt buộc với `kind=qa` — giữ quyết định v1. (2) `difficulty` không chuẩn hóa ở v1. (3) Lần chạy đầu tiên của một tập gác cổng không có overall — CHẤP NHẬN (nêu cho Thuận khi trình).
