# Kite — Review & đề xuất cải tiến (có minh chứng)

Nov 18, 2026 · @ngoc han

## Tóm tắt

Control plane của Kite (Temporal, journal, lease, worktree, review gate) đã chắc, nhưng ba mắt xích quyết định chất lượng coding vẫn chưa nối: đường gọi model từ EC2 sang Kaggle, việc chọn model, và vòng kiểm chứng kết quả. Tài liệu này đề xuất một kiến trúc giữ Kaggle, chạy được nhiều worker, và có số liệu để đo.

Mục tiêu (đã xác nhận): Kite dùng để code dự án thật; cần coding và suy luận tốt, có cả số liệu benchmark lẫn độ tin cậy vận hành; kiến trúc nhiều worker; giữ Kaggle.

Năm kết luận chính:

1. **Đảo chiều kết nối Kaggle.** Notebook Kaggle tự gọi ra EC2 để nhận việc (pull/long-poll), giống cách GitHub self-hosted runner hoạt động. Không cần mở cổng vào Kaggle, không cần tunnel. Notebook được khởi động bằng `kaggle kernels push` có tài liệu chính thức. Đây là cách gỡ gate “AWS→Kaggle multi-turn” đang chặn cả dự án.
2. **Đổi roster model.** Trên 2×T4, model MoE nhanh hơn dense nhiều lần. Qwen3.6-35B-A3B (73.4% SWE-bench Verified theo nhà phát hành, 3B tham số active, có sẵn vision) nên là model chính; Devstral Small 2 làm model thứ hai. R1-Distill-32B, Qwen2.5-Coder-32B và Gemma 4 26B A4B nên bỏ khỏi vai coding.
3. **Sửa 6 lỗi đã tái hiện bằng test** (E1–E6): agent bị treo khi xin quyền chạy lệnh, quota GPU không bị trừ, capability không ảnh hưởng routing, `workflow start` không bao giờ được cấp worker, rule chặn `git push` bị vượt dễ dàng, và workflow chờ worker sẽ bị Temporal terminate sau khoảng 19–43 giờ.
4. **Chạy agent trong sandbox container mỗi task.** Hiện OpenCode chạy lệnh shell ngay trên máy control plane, cùng user đọc được file secret.
5. **Bộ điều phối theo loại task:** các worker làm những task khác nhau của cùng dự án. Router chọn worker đang rảnh, rồi chọn model có điểm cao nhất cho loại task đó sau khi đã trừ chi phí đổi model. Điểm của từng model được đo trên chính dự án.

Thứ tự ưu tiên: sửa lỗi nền (Pha 1) → pull-worker Kaggle + roster mới (Pha 2) → sandbox + vòng kiểm chứng (Pha 3) → bộ điều phối theo loại task (Pha 4). Mỗi pha có tiêu chí nghiệm thu đo được ở phần Lộ trình.

## Lỗi trong code hiện tại (đã tái hiện)

Cả 6 lỗi đều được tái hiện bằng file `test_evidence.py` chạy trên commit `1a6e274`. File dùng lại fake OpenCode server và SQLite store có sẵn trong repo. Kết quả: `6 passed in 3.68s`, nghĩa là cả 6 lỗi đều xảy ra thật. Test suite gốc của repo: 139/147 pass; 8 test Temporal hỏng vì sandbox không tải được Temporal test server, không phải lỗi code.

| # | Lỗi | Vị trí | Output tái hiện | Hậu quả khi chạy thật |
| --- | --- | --- | --- | --- |
| E1 | Agent xin quyền chạy lệnh (`permission.asked`) nhưng không ai trả lời | `opencode.py:189-192, 508-523` | heartbeat có `APPROVAL_REQUIRED`; số request trả lời permission = 0; run kết thúc bằng `TimeoutError` | Session tạo với shell = `ask`, nên lần đầu model muốn chạy `pytest` là treo tới timeout 6 giờ, GPU Kaggle ngồi không |
| E2 | Task thành công không trừ quota GPU | `opencode.py:555`, `store.py:653` | 10 task × ước tính 900 s xong, `quota_remaining_seconds` vẫn = 3600 | Kiểm soát quota theo owner chỉ có tác dụng khi task fail; scheduler sẽ giao việc vượt 30 giờ GPU/tuần của Kaggle |
| E3 | `required_capabilities` không đi vào routing | `temporal_workflow.py:23`, `routing.py:23` | `RoutingRequest` chỉ có `role, model_id, estimated_gpu_seconds`; `WorkerProfile` không có trường capabilities | `--capability vision` vẫn có thể bị giao cho worker không có vision |
| E4 | `kite workflow start` không tạo task trong SQLite | `cli.py:186-195`, `store.py` `reserve_for_task` | `reserve_for_task(...)` trả `None`; nhánh `start` không gọi `enqueue` | Workflow kẹt mãi ở `WAITING_FOR_WORKER` dù có worker rảnh |
| E5 | Rule chặn `git push` bị vượt; shell chạy trực tiếp trên control plane | `opencode.py:191` | Theo kiểu khớp glob, chỉ `git push origin main` bị chặn; `git -C . push`, `git -c x=y push`, `sh -c 'git push'`, `cat …/secrets/kaggle_token` đều lọt | Một khi chuyển shell sang `allow` để hết treo (E1), model có thể push hoặc đọc secret. Không có container hay sandbox nào |
| E6 | Vòng chờ worker làm history Temporal tăng không giới hạn | `temporal_workflow.py:180-198` | Giới hạn cứng 51.200 event; 5–11 event mỗi vòng 15 s → bị terminate sau 19,4–42,7 giờ chờ | Khi quota Kaggle tuần đã hết, task chờ đến tuần sau sẽ bị Temporal giết |

Thiếu phần ghép nối để chạy end-to-end:

- Chưa có `KiteActivityBackend` bản production. Spec mục 12 cũng ghi nhận điều này.
- Không có code chuyển `providers` trong `config.yaml` thành `workers`/`owners` trong store, nên store luôn rỗng.
- `CodingRunResult` không mang kết quả test. Review bundle chỉ có patch và đoạn tóm tắt do model tự viết, không có bằng chứng kiểm chứng.
- Notebook Kaggle mình viết lượt trước đặt `ctx` 16K là **sai** (lỗi của mình): riêng lượt đầu, OpenCode đã gửi khoảng 7.500 token system prompt và định nghĩa tool, và hướng dẫn khuyến nghị dành sẵn 32K.

## Kiến trúc đề xuất: Kaggle pull-worker

Thay vì tìm cách để EC2 gọi vào session Kaggle (hiện chưa có cách nào được Kaggle công bố hỗ trợ, nên feasibility doc ghi CONDITIONAL), hãy để notebook Kaggle tự gọi ra EC2 để nhận việc. Đây đúng là mô hình mà GitHub dùng cho self-hosted runner: runner chỉ cần kết nối ra ngoài, không cần kết nối chiều vào ([GitHub Docs](https://docs.github.com/en/enterprise-server@3.11/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners)). Runner giữ một HTTPS long poll tới GitHub khoảng 50 giây, hết giờ thì mở lại ([GitHub Docs](https://docs.github.com/fr/enterprise-cloud@latest/actions/hosting-your-own-runners/managing-self-hosted-runners/communicating-with-self-hosted-runners)).

&#91;embedded content: kiến trúc đề xuất · 4 thành phần EC2, 3 loại worker\]

Luồng một lần gọi model:

1. OpenCode trong sandbox gọi `POST /v1/chat/completions` tới gateway, giống gọi một provider bình thường. OpenCode hỗ trợ sẵn provider tương thích OpenAI qua `@ai-sdk/openai-compatible` với `baseURL` tuỳ chọn ([OpenCode providers](https://www.mintlify.com/anomalyco/opencode/providers)).
2. Gateway đưa request vào hàng đợi của worker đã được router gán cho task (lease đã có sẵn trong store).
3. Worker Kaggle đang long-poll `GET /worker/jobs?wait=50` nhận job, chạy trên `llama-server` 127.0.0.1 của chính nó, rồi `POST /worker/jobs/{id}/result` (stream từng đoạn nếu cần).
4. Gateway trả response cho OpenCode. Chi phí thêm là một vòng round-trip, rất nhỏ so với thời gian sinh token.

Vòng đời worker dùng API chính thức: `kaggle kernels push` đẩy notebook lên và chạy nó ([Kaggle CLI docs](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md)). Lệnh này nhận `--accelerator` (ví dụ `NvidiaTeslaT4`) và có tuỳ chọn thời gian chạy tối đa. Notebook tự thoát khi rảnh N phút, nên không phụ thuộc vào API cancel. Lưu ý: các bản push qua CLI mất Kaggle Secrets nhập trên UI; một dự án thực tế đã phải đính secret qua dataset private ([ví dụ](https://huggingface.co/spaces/cuilabs/bee/blob/main/scripts/push_kaggle_kernel.py)). Với Kite, token worker nên là token riêng, quyền hẹp, có hạn, xoay vòng mỗi lần push.

Vì sao cách này khớp với code sẵn có:

- Heartbeat của worker tới gateway cung cấp trực tiếp `status_observed_at` và `provider_ready`. Cơ chế fail-closed trong `routing.py` dùng được ngay, không cần viết lại.
- Nhiều worker chỉ là nhiều client cùng long-poll. Mỗi worker mang `owner_id` riêng, và quota/concurrency theo owner (đã có) vẫn áp dụng.
- **Model affinity:** worker báo model đang nạp; router ưu tiên worker đã nạp đúng model. Theo số đo của bạn, nạp Qwen3.5-9B mất 159 s lần đầu và 37 s khi đã có cache; model 20 GB còn lâu hơn, nên đổi model giữa chừng rất tốn.
- Gateway thay cho `Kaggle adapter` stub. Thêm provider khác chỉ cần viết một worker nói cùng giao thức pull.

Giới hạn cần nói rõ: một tài khoản Kaggle vẫn chỉ có một quota tuần (khoảng 30 giờ GPU, [Paperspace so sánh](https://blog.paperspace.com/gradient-kaggle-notebook-comparison/)). Muốn nhiều worker chạy song song thật thì cần tài khoản của người khác tự nguyện góp, hoặc provider khác. Cách thứ nhất vẫn nằm sau gate chính sách mà repo đã ghi.

### Bộ điều phối: chọn worker rảnh, rồi chọn model hợp với loại task

Mỗi task mang một `task_type`: phân tích, coding, review, vision, hoặc viết tài liệu. Các worker làm những task khác nhau trong cùng dự án. Bộ điều phối xử lý mỗi task theo 3 bước:

1. **Lọc worker đủ điều kiện:** còn chỗ trống (concurrency), owner hợp lệ, còn quota, heartbeat còn mới. `routing.py` đã làm sẵn phần này theo kiểu fail-closed.
2. **Chấm điểm từng model mà worker đó chạy được** (vừa VRAM và đĩa) theo công thức bên dưới.
3. **Chọn cặp (worker, model) có điểm cao nhất;** nếu bằng điểm thì chọn worker ít tải hơn. Nếu model được chọn khác model worker đang nạp, gateway gửi lệnh `load` cho worker trước khi gửi request.

```latex
\text{score}(w, m) = q(t, m) - \lambda \cdot \text{swap}(w, m)
```

- q(t, m): điểm của model m cho loại task t. Lấy từ bảng đo bằng Harness² trên task của chính dự án. Khi chưa đo, dùng điểm công bố làm giá trị tạm và đánh dấu rõ nguồn.
- swap(w, m): số giây để worker w có model m sẵn sàng. Bằng 0 nếu model đang nạp; bằng thời gian nạp từ đĩa nếu đã cache; bằng thời gian tải cộng nạp nếu chưa có.
- λ: hệ số đổi giây thành điểm. Chỉnh λ sao cho chỉ đổi model khi chênh lệch chất lượng đủ lớn để đáng giá.

Vì sao phải trừ chi phí đổi model: theo số đo của bạn, Qwen3.5-9B mất 159 s để nạp lần đầu và 37 s khi đã có cache. Model 20 GB còn lâu hơn, và đĩa Kaggle (khoảng 60 GB) chỉ cache được khoảng 2 model cỡ đó cùng lúc. Nếu task nào cũng nạp "model giỏi nhất" thì worker sẽ mất phần lớn thời gian để nạp model thay vì làm việc.

| Chỗ trong code | Hiện tại | Cần thành |
| --- | --- | --- |
| `ScheduledTask` / `CodingTaskInput` | `role`, `required_capabilities` (không được dùng) | thêm `task_type` |
| `WorkerProfile` | `model_ids`, coi như mọi model đều sẵn sàng | `loaded_model`, `cached_models`, `supported_models`, VRAM, đĩa |
| Bảng điểm model | chưa có | `model_skill(task_type, model)`: điểm, nguồn (đo hay công bố), ngày đo |
| `select_worker` | chọn worker ít tải nhất, lấy model đầu tiên | chấm điểm theo công thức trên |
| Heartbeat của worker | chưa có | báo model đang nạp, model đã cache, tok/s gần nhất |

## Chọn model cho 2×T4

Nên dùng Qwen3.6-35B-A3B làm model chính: nó có điểm SWE-bench Verified cao nhất trong nhóm vừa 30 GB VRAM, và vì là MoE chỉ 3B tham số active nên chạy nhanh trên T4. Roster 5 vai hiện tại có 3 model yếu hoặc chậm cho agentic coding.

| Model | SWE-bench Verified | Kiến trúc | File Q4 | Trần tốc độ lý thuyết trên T4\* | Đề xuất |
| --- | --- | --- | --- | --- | --- |
| [Qwen3.6-35B-A3B](https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF) | 73,4% (vendor) | MoE, 3B active, có vision | \~18–21 GB | \~175 tok/s | **Model chính**: thay kiến trúc sư, coder, review và vision |
| [Devstral Small 2](https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512) | 68,0% vendor / 56,4% harness mở | Dense 24B | 14,3 GB | \~22 tok/s | Model thứ hai, để có đa dạng ứng viên |
| Qwen3.5-27B | 75,0% (bảng của Qwen) / 72,4% (bảng của paper) | Dense 27B | \~16 GB | \~20 tok/s | Chỉ dùng nếu đo thấy tốc độ chấp nhận được |
| Gemma 4 26B A4B | 17,4% (bảng của Qwen) | MoE, \~4B active | \~16–18 GB | — | Bỏ khỏi vai coding |
| Qwen2.5-Coder-32B | thế hệ 2024, dưới Devstral đời đầu | Dense 32B | 19,9 GB | \~16 tok/s | Bỏ |
| DeepSeek-R1-Distill-Qwen-32B | model reasoning, không phải agent; mình không tìm thấy điểm SWE-bench agentic nào được công bố | Dense 32B | \~20 GB | \~16 tok/s | Bỏ |

\*Trần tốc độ = băng thông T4 (320 GB/s theo [NVIDIA](https://www.nvidia.com/en-in/data-center/tesla-t4/)) chia cho số byte trọng số đọc mỗi token. Với MoE, chỉ tính \~3B tham số active ở khoảng 4,8 bit (\~1,8 GB). Đây là giới hạn trên, không phải số đo. Ngoài thực tế, cộng đồng ghi nhận MoE 30B-A3B chạy nhanh hơn dense 32B trên cùng GPU khoảng 3–5 lần ([runaihome](https://runaihome.com/blog/qwen3-30b-a3b-local-ai-guide-2026/)). Pha 2 phải đo lại bằng `llama-bench` trên Kaggle.

Nguồn điểm số: Qwen3.6 đạt 73,4%, Gemma4-26B-A4B 17,4% và Qwen3.5-27B 75,0% lấy từ bảng benchmark của Qwen ([labellerr](https://www.labellerr.com/blog/qwen3-6-35b-a3b-open-source-ai-model/)). Con số của Gemma do đối thủ báo cáo, nên cần tự kiểm lại. Devstral Small 2 và Qwen3.5-27B lấy từ [arXiv 2604.00362](https://arxiv.org/pdf/2604.00362).

Bài học quan trọng nhất từ paper đó: **harness thay đổi điểm hàng chục phần trăm**. Devstral Small 2 từ 68,0% còn 56,4% khi chạy trên harness mở. gpt-oss-120b từ 62,4% công bố xuống 26% trên leaderboard. Tác giả chỉ tái hiện được con số công bố sau khi cung cấp đúng tool và định dạng chat gốc của model. Vì vậy điểm vendor chỉ dùng để lọc ứng viên; quyết định cuối cùng phải dựa trên benchmark chạy qua chính Kite (OpenCode + llama-server).

Vai embedding (BGE-M3/Nomic) chuyển về EC2 chạy CPU. Vai này cần luôn sẵn sàng, mà Kaggle thì không luôn bật.

## Harness: phần quyết định "code giỏi"

Với cùng một model, harness tốt hay dở có thể làm điểm lệch hàng chục phần trăm (xem phần Chọn model). Dưới đây là 6 thay đổi, theo thứ tự ưu tiên.

**1. Sandbox container cho mỗi task (sửa E1, E5).** Chạy cả OpenCode server lẫn shell của nó trong một container riêng cho từng task: chỉ mount worktree, không mount secret, chỉ nối mạng tới gateway qua một Docker network nội bộ. Mẫu lệnh phổ biến: `docker run --rm --network=none --cap-drop=ALL --security-opt=no-new-privileges -v ./project:/work` ([amux](https://amux.io/guides/ai-agent-sandboxing/)). OpenHands cũng chạy mỗi task trong một container được dựng riêng ([elest.io](https://blog.elest.io/openhands-give-your-coding-agent-its-own-sandbox-not-your-laptop/)). Khi đã có ranh giới này, shell trong container đặt được `allow`, nên hết treo. Rule chặn `git push` chỉ còn là lớp phòng vệ phụ, vì trong container không có credential GitHub và không có đường mạng ra ngoài. Lưu ý: container dùng chung kernel với máy chủ, nên bản thân nó không phải ranh giới bảo mật mạnh ([phân tích](https://codex.danielvaughan.com/2026/04/13/docker-sandboxes-codex-cli-microvm-isolation/)). Muốn chắc hơn thì chạy với runtime gVisor (`--runtime=runsc`).

**2. Đường duyệt quyền cho các thao tác vượt ra ngoài sandbox.** Khi có `permission.asked`: chuyển workflow sang trạng thái `AWAITING_APPROVAL`, checkpoint, dừng worker GPU đúng như spec đã yêu cầu, và thêm signal `kite workflow approve|deny`. Signal này gọi endpoint trả lời permission của OpenCode. Một client OpenCode ghi endpoint đó là `POST /api/session/{sessionID}/permission/{requestID}/reply` ([docs.rs](https://docs.rs/opencode-codes/latest/src/opencode_codes/client_async.rs.html)). Cần kiểm lại bằng OpenAPI spec mà server 2.3.0 của bạn tự công bố tại `/doc`.

**3. Kite tự kiểm chứng, không tin lời model.** Sau mỗi lượt OpenCode, Kite tự chạy lệnh test của repo trong sandbox, lưu exit code và log vào `CodingRunResult` và review bundle. Nếu đỏ, Kite đưa log lỗi lại làm chỉ dẫn, tối đa 2 lần. Các hệ thống hàng đầu như Trae và Agentless đều dùng regression test để loại patch sai trước khi chọn ([Trae](https://se-research.bytedance.com/blogs/trae-on-swe-bench-verified-71)). Đây cũng chính là "bằng chứng" bạn cần ở mỗi review bundle.

**4. Tuỳ chọn cho task khó: nhiều lần thử trên chính worker được giao.** Worker được giao chạy task 2–3 lần, mỗi lần trong một worktree riêng. Đây không phải cách dùng chính của nhiều worker. Kite loại ứng viên bằng test, rồi chọn patch tốt nhất. Số liệu đã công bố:

| Hệ thống | Không scaling | Có scaling | Nguồn |
| --- | --- | --- | --- |
| CWM (32B open-weight) | 53,9% | 65,8% | [arXiv 2510.02387](https://arxiv.org/pdf/2510.02387) |
| Kimi-K2 + EGSS | 63,2% | 72,2% | [arXiv 2602.05242](https://arxiv.org/html/2602.05242) |
| Mini-SE (Qwen3-32B) + R4P | 26,2% | 32,8% | [arXiv 2510.22775](https://www.arxiv.org/pdf/2510.22775) |

Trên Kaggle, k ứng viên tốn khoảng k lần giờ GPU. Vì vậy để mặc định k = 1, và chỉ bật k = 2–3 cho task khó hoặc khi lần đầu fail test.

**5. Context đủ lớn.** OpenCode 1.18 gửi 7.516 token ở lượt đầu với 10 tool mặc định; tắt 4 tool (`webfetch`, `task`, `todowrite`, `skill`) thì còn 5.335 token. Tác giả khuyến nghị dành 32K ([jacar.es](https://jacar.es/?p=7245)). Đặt `-c 32768` (hoặc 65536 nếu VRAM cho phép) và khai `limit.context` tương ứng trong `opencode.json`.

**6. Đúng định dạng tool-call gốc của model.** Chạy `llama-server --jinja` để áp chat template có tool của model. Theo paper gpt-oss, dùng sai định dạng gốc và thiếu tool là nguyên nhân chính làm tụt điểm. Ngoài ra, đặt sẵn file `AGENTS.md` trong repo của bạn (OpenCode và `inspect_task_workspace` đều đọc file này): ghi lệnh test, lệnh lint, và những vùng code không được sửa.

## Lộ trình và tiêu chí nghiệm thu

Pha 2 là pha gỡ nút thắt của cả dự án. Pha 1 làm trước vì không tốn GPU, và vì nếu thiếu nó thì Pha 2 có nối được Kaggle cũng không chạy được task nào (E4).

&#91;embedded content: lộ trình · 4 pha, mỗi pha một gate đo được\]

Mỗi gate là một con số hoặc một test, không phải ý kiến. Benchmark ở Pha 3–4 chạy qua chính Kite, vì điểm vendor không chuyển thẳng sang harness của bạn được.

## Rủi ro và điều chưa chứng minh

Bốn điều dưới đây vẫn là giả định, chưa phải bằng chứng. Phải đo trước khi dựa vào chúng.

- **Chính sách Kaggle.** Mình không tìm thấy điều khoản Kaggle nào nói rõ việc một kernel phục vụ inference cho ứng dụng bên ngoài là được hay không. Mô hình pull tránh được tunnel và cổng public, nhưng không tự động hợp lệ. Gate chính sách trong repo (một tài khoản, không xoay vòng, worker của cộng tác viên phải được Kaggle xác nhận) vẫn giữ nguyên. Nên hỏi Kaggle trước khi chạy thường xuyên.
- **Tốc độ thật trên T4.** Các con số tok/s trong tài liệu là trần lý thuyết tính từ băng thông. MoE của Qwen3.6 dùng attention lai, và có báo cáo llama.cpp chạy một số kiến trúc MoE mới chậm hơn kỳ vọng ([issue #19480](https://github.com/ggml-org/llama.cpp/issues/19480)). Gate Pha 2 bắt buộc phải đo.
- **API của OpenCode 2.3.0.** Các đường `/api/...` mà `opencode.py` đang gọi và endpoint trả lời permission chưa được thử trên server thật. Test hiện tại chạy với fake server, mà fake server được viết theo đúng giả định của code. Việc đầu tiên nên làm: chạy `opencode serve` trên EC2, lấy spec tại `/doc`, rồi so với từng endpoint đang dùng.
- **Điểm benchmark.** Số của Qwen3.6 và Gemma 4 đều do Qwen công bố. Chính paper được trích trong tài liệu này cho thấy điểm có thể lệch tới 36 điểm khi đổi harness. Kite cần bộ đo riêng của mình (Pha 3).

Tốn kém: với khoảng 30 giờ GPU/tuần, test-time scaling k = 3 làm số task mỗi tuần giảm khoảng 3 lần. Vì vậy k phải là tuỳ chọn theo từng task, không phải mặc định.

## Nguồn

- [GitHub Docs — About self-hosted runners](https://docs.github.com/en/enterprise-server@3.11/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners) · [Communicating with self-hosted runners](https://docs.github.com/fr/enterprise-cloud@latest/actions/hosting-your-own-runners/managing-self-hosted-runners/communicating-with-self-hosted-runners)
- [Kaggle CLI — kernels push](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md) · [Ví dụ push kernel GPU kèm secret dataset](https://huggingface.co/spaces/cuilabs/bee/blob/main/scripts/push_kaggle_kernel.py)
- [Temporal — Long-running workflows (giới hạn 51.200 event, Continue-As-New)](https://docs.temporal.io/blog/very-long-running-workflows) · [Self-hosted defaults](https://docs.temporal.io/self-hosted-guide/defaults)
- [arXiv 2604.00362 — In harmony with gpt-oss](https://arxiv.org/pdf/2604.00362)
- [Qwen3.6-35B-A3B GGUF](https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF) · [Bảng benchmark Qwen3.6 (labellerr)](https://www.labellerr.com/blog/qwen3-6-35b-a3b-open-source-ai-model/)
- [Devstral Small 2 24B (Hugging Face)](https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512) · [Kích thước bản local](https://www.developersdigest.tech/local-models/devstral-small-2-24b)
- [NVIDIA T4 specs](https://www.nvidia.com/en-in/data-center/tesla-t4/) · [MoE vs dense trên GPU (runaihome)](https://runaihome.com/blog/qwen3-30b-a3b-local-ai-guide-2026/) · [llama.cpp issue #19480](https://github.com/ggml-org/llama.cpp/issues/19480)
- [CWM, arXiv 2510.02387](https://arxiv.org/pdf/2510.02387) · [EGSS, arXiv 2602.05242](https://arxiv.org/html/2602.05242) · [R4P, arXiv 2510.22775](https://www.arxiv.org/pdf/2510.22775) · [Trae trên SWE-bench Verified](https://se-research.bytedance.com/blogs/trae-on-swe-bench-verified-71)
- [OpenCode providers (openai-compatible, llama.cpp)](https://www.mintlify.com/anomalyco/opencode/providers) · [Token OpenCode gửi ở lượt đầu (jacar.es)](https://jacar.es/?p=7245) · [Endpoint trả lời permission (docs.rs)](https://docs.rs/opencode-codes/latest/src/opencode_codes/client_async.rs.html)
- [Sandbox: amux](https://amux.io/guides/ai-agent-sandboxing/) · [OpenHands runtime (elest.io)](https://blog.elest.io/openhands-give-your-coding-agent-its-own-sandbox-not-your-laptop/) · [Container không phải ranh giới bảo mật](https://codex.danielvaughan.com/2026/04/13/docker-sandboxes-codex-cli-microvm-isolation/)
- [Kaggle GPU quota \~30 giờ/tuần (Paperspace)](https://blog.paperspace.com/gradient-kaggle-notebook-comparison/)
- Minh chứng code: `test_evidence.py` (6 test, chạy trên commit `1a6e274`), đính kèm trong chat.
