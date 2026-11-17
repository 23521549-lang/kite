# Kaggle Worker Feasibility — Initial Documentation Audit

**Date:** 2026-11-16  
**Status:** Interactive inference not yet proven; do not build the full assistant on this assumption.

## Question

Can the AWS-hosted Harness² coding loop start a Kaggle GPU worker only when a task arrives, make multiple model calls to Qwen in that same worker session, then stop it and resume later from an AWS checkpoint?

## Findings from official documentation

| Capability | Current evidence | Assessment |
|---|---|---|
| Submit notebook/script to Kaggle for execution | Kaggle CLI `kernels push` uploads code and attempts to run the kernel; accelerator and timeout options are documented. | Documented batch execution. |
| Poll a kernel run | `kernels status` reports whether the latest run is running, complete, or failed. | Documented. |
| Retrieve run output | `kernels output` downloads artifacts from the latest or specified version. | Documented batch result retrieval. |
| Interactive notebook session lifecycle | Kaggle MCP docs list create and cancel notebook session operations. | Session control is documented, but this alone does not establish arbitrary code execution or model-call transport from AWS. |
| Send multiple inference requests to a running session | Not established by the reviewed Kaggle CLI kernel docs or readable MCP reference. | Critical unknown. |
| Stop a CLI-submitted kernel run | The reviewed CLI kernel reference has no `cancel`/`stop` command documented. MCP session cancellation is listed, but its relation to CLI batch runs is unclear. | Critical unknown; must prove supported cancellation before relying on quota-saving shutdown. |
| GPU allocation and quota | Kaggle documents a weekly GPU quota (about 30 hours, potentially variable); notebook docs list P100 and T4 x2 configurations, with availability subject to account/session conditions. | Do not assume every run receives T4; measure actual account allocation and quota. |

## Technical conclusion

The existing public CLI documentation supports **on-demand batch notebook jobs**, status polling, and output retrieval. It does not, on its own, prove the persistent interactive inference endpoint required for a Claude Code-like tool loop. A batch job that receives a whole task and returns a patch could be explored as a degraded architecture, but it is not equivalent to AWS Harness² making repeated live calls to a Kaggle-hosted Qwen model.

The Kaggle MCP reference separately exposes create/cancel notebook-session operations. Before selecting that route, verify that the currently available Kaggle MCP/API can execute or communicate with a running session in a supported way and that it can be cancelled cleanly. Do not use public tunnels, keep-alive tricks, or unsupported endpoints to bridge the gap.

## Go/no-go criteria for the next probe

Mark the interactive design `GO` only after observing all of these on one account and one GPU session:

1. AWS starts the session only after a task is submitted.
2. The allocated accelerator is identified and model loading completes.
3. AWS sends two separate prompts and receives two separate responses within that same session.
4. The controller can stop/cancel the session through a documented interface.
5. Kaggle reports no active GPU session after stop.
6. A forced interruption can be resumed using an AWS checkpoint, without relying on model memory.

If only batch execution is available, mark the interactive route `NO-GO` and present the batch-worker tradeoffs before implementing it.

## Sources

- [Kaggle CLI kernel commands](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md) — `kernels push`, accelerator/timeout, status, and output commands.
- [Kaggle notebook documentation](https://www.kaggle.com/docs/notebooks) — notebook session limits and accelerator configurations.
- [Kaggle efficient GPU usage](https://www.kaggle.com/docs/efficient-gpu-usage) — weekly quota and usage guidance.
- [Kaggle MCP documentation](https://www.kaggle.com/docs/mcp) — notebook session create/cancel operations; the full tool reference must be checked during the live probe.

## Observed CPU notebook probe (2026-11-16)

A separate new Kaggle draft notebook was created for this probe. The existing user notebook and other sessions were not opened or changed.

- Accelerator: `None` / CPU; no GPU was selected.
- Session: started by running a minimal Python cell; Kaggle showed `Draft Session` running.
- Repeated execution: two separate cells ran in the same live session and returned `KITE_CPU_PROBE_OK` and `KITE_SECOND_REQUEST_OK`.
- Stop: `Run → Stop session` returned the new notebook session to `off (run a cell to start)`.
- Notebook version: not published; no model or project source was uploaded.

### What this proves

The browser notebook supports on-demand CPU session startup, sequential cell execution, and manual session stop. This validates a basic per-notebook lifecycle and confirms we can perform a no-GPU smoke check without affecting other sessions.

### What this does not prove

- It does not prove Kaggle has a supported remote multi-turn inference API callable from AWS/Harness².
- It does not prove a Kaggle GPU session can be programmatically started/stopped through the same control path.
- It does not prove Qwen model weights fit or run well on the GPU allocation.
- CPU notebook success does not imply GPU capacity or model inference will work; those require a separate GPU-only probe after the supported integration path is identified.

**Gate status:** CPU notebook lifecycle = `observed`; AWS-to-Kaggle model call and GPU/model fit = `unverified`.

## Observed GPU notebook probe (2026-11-16)

A separate GPU diagnostic was run in the same isolated draft notebook after the CPU probe. No model weights or project source were uploaded.

- Accelerator selected: `GPU T4 x2` (Kaggle displayed 29 GPU hours remaining before confirmation).
- The diagnostic cell completed successfully: `torch.cuda.is_available() == True`, `torch.cuda.device_count() == 2`, device 0 reported `Tesla T4`.
- Stop: `Run → Stop session` was issued; the notebook UI changed to `Draft Session off (run a cell to start)` and exposed `Start session`.
- The pre-existing user notebook and other sessions were not touched.

### What this proves

This account can start a separate notebook session with two visible T4 devices and execute basic CUDA/PyTorch diagnostics. The test session can be manually stopped from the Kaggle UI.

### What this does not prove

It does not prove an AWS/Harness² controller can start, execute multiple requests on, or stop the session through a supported remote API. It does not prove Qwen weights fit, inference throughput, or coding quality on T4 x2. The displayed quota was an observation at test time, not a guaranteed persistent allowance.

**Updated gate status:** CPU lifecycle = `observed`; Kaggle GPU allocation and CUDA visibility = `observed`; AWS-to-Kaggle remote model-call path, Qwen load/inference, and automated cancellation = `unverified`.

## Observed Qwen inference smoke run (2026-11-16)

A real model load and one coding prompt were tested in the isolated Kaggle draft with the T4 x2 accelerator. No Kite repository files or user project code were sent to Kaggle.

- Runtime: PyTorch `2.11.0+cu130`; two Tesla T4 devices, each reported 14.56 GiB.
- First attempt: `bitsandbytes==0.45.5` produced a CPU-only CUDA backend warning (`libbitsandbytes_cuda130.so` missing). The cell was canceled and the GPU session stopped.
- Compatible retry: `bitsandbytes==0.50.2` loaded CUDA support without that warning. Hugging Face downloaded/reconstructed about 15.2 GB of FP16 checkpoint files; model loading took 106.8 seconds.
- Model: `Qwen/Qwen2.5-Coder-7B-Instruct`, loaded with NF4 4-bit quantization through Transformers and `device_map='auto'`.
- Inference: one short coding prompt completed in 8.6 seconds and generated 106 tokens. Peak PyTorch allocated memory was 2.38 GiB on GPU 0 and 2.83 GiB on GPU 1 (these are allocator peaks, not total device usage).
- The generated response explained a palindrome function and showed a call example, but omitted the requested function definition. Therefore the model-load/inference smoke test **passed**, while this basic instruction-following/code-output check **failed**.
- The Kaggle session was stopped after the run; the notebook showed `Draft Session off (run a cell to start)`. The earlier user notebook and other sessions were not touched.

### Engineering implication

The Kaggle T4 x2 environment can technically load this 7B model with 4-bit quantization once the CUDA-compatible `bitsandbytes` package is installed. This does not establish sustained coding quality, latency across real Harness² tasks, or remote AWS orchestration. The missed function body is a quality warning; do not select this configuration as the autonomous engineer brain based on one smoke prompt. Next model evaluation should use a small fixed coding rubric (produce a complete patch, follow exact output format, and pass a deterministic execution check) before integration.

**Updated gate status:** CPU lifecycle = `observed`; T4 x2 CUDA = `observed`; Qwen2.5-Coder-7B 4-bit load and one inference = `observed`; initial coding-response correctness = `failed`; AWS-to-Kaggle request path and automated cancellation = `unverified`.

## Observed coding rubric comparison (2026-11-16)

A fixed three-task generation check compared `Qwen/Qwen2.5-Coder-7B-Instruct` and `Qwen/Qwen2.5-Coder-3B-Instruct` on the isolated Kaggle draft with T4 x2. The cell was run only after starting the draft session; it was stopped after completion. No Kite repository source or private project code was sent to Kaggle.

- Both models loaded with 4-bit NF4 quantization. In this cached-session run, 7B loaded in 14.6 seconds and generated the three answers in 3.4, 3.8, and 8.4 seconds. 3B loaded in 48.0 seconds and generated in 6.4, 4.6, and 11.9 seconds. These are single-run observations, not stable latency estimates.
- Tasks: palindrome normalization, stable unique values, and merge intervals. Each answer was requested as one Python function and screened by a restrictive AST allowlist before deterministic cases could execute.
- Both models generated plausible solutions but returned markdown fences. The palindrome answers also imported `re`; the unique answers used `set`; and interval answers used `lambda` and `max`. The harness rejected those forms under its deliberately restricted sandbox, so both registered 0/9. This is a **format/sandbox compatibility failure**, not evidence the algorithms are incorrect.
- The first benchmark attempt exposed a rubric aggregation bug (`KeyError: 'total'`) when the sandbox rejected code; the score calculation was fixed and the full comparison completed on rerun.
- The Kaggle session was stopped and the UI returned to `Draft Session off (run a cell to start)`.

### Engineering implication

This evaluation does not show a useful quality difference between 7B and 3B under this prompt and validator: both behaved similarly and neither obeyed “code only” output. For Harness², prioritize a robust response extractor (fences and leading imports), policy-based sandbox with carefully scoped safe builtins, and an iterative repair/test loop with explicit state summaries. Keep 7B as the first candidate for further evaluation because it is the stronger-sized variant, but do not treat this small rubric as proof of production quality or autonomous project-level competence. Test on representative repo-level tasks before selecting a default.

**Updated gate status:** Kaggle T4 x2 model load/inference for Qwen2.5-Coder 7B and 3B = `observed`; isolated three-task format/sandbox rubric = `failed for both`; repository-scale coding quality, persistent autonomous work, AWS-to-Kaggle request path, and automated session control = `unverified`.

## Revised checker and Qwen3.5 comparison (2026-11-16)

The isolated draft benchmark was rerun after relaxing the checker to accept fenced code, `re`, `set`, `lambda`, and `max`. A built-in checker self-test scored 9/9 before model evaluation.

- `Qwen/Qwen2.5-Coder-7B-Instruct` then passed all three tasks (9/9): palindrome normalization, stable unique values, and merge intervals. It generated fenced code, which the revised extractor handled. This remains a tiny deterministic rubric, not a repo-level quality result.
- `Qwen/Qwen3.5-9B` downloaded and loaded on T4 x2 (4-bit NF4); load took 159 seconds. It generated 256 tokens per task in about 27–33 seconds each, but spent most/all of the budget on a `Thinking Process` and did not finish code: 0/9. The current prompt also accidentally says the function name is `stable_unique` while the signature says `unique_preserving_order`; Qwen3.5 explicitly noticed this contradiction. Therefore this run is inconclusive about Qwen3.5's coding ability and demonstrates the benchmark prompt needs repair (canonical function-name field, coherent instruction, and model-appropriate thinking/stop controls).
- `Qwen/Qwen3-Coder-30B-A3B-Instruct` official Transformers weights began downloading/reconstructing on the same Kaggle session, but reached 57.6 GB / 61.1 GB and Kaggle warned about the disk limit. No inference was performed; the session was stopped to avoid disk overrun. A quantized GGUF with llama.cpp could be a different route, but has not been tested here.
- The Qwen3.5 session was stopped after the run; Kaggle showed `Draft Session off (run a cell to start)`. The separate existing user notebook and other sessions were not touched.

### Implication for choosing a brain

The first choice of Qwen2.5-Coder 3B/7B was a conservative fit-and-lifecycle probe, not a claim they were the strongest candidates. Qwen3-Coder is more directly aligned with coding/agentic use; however, its tested raw checkpoint path did not fit Kaggle's practical workspace/disk constraints. Qwen3.5-9B is a more general multimodal model and was not a clean coding comparison because of prompt conflict and long reasoning output. On current evidence, use Qwen2.5-Coder-7B as a baseline only; repair the benchmark before comparing Qwen3.5-9B, and evaluate a quantized Qwen3-Coder GGUF route separately. Do not select a production brain from the present 3-task score.

**Updated gate status:** Qwen2.5-Coder-7B revised-rubric result = `9/9 observed`; Qwen3.5-9B = `loaded but rubric run inconclusive, returned unfinished reasoning`; Qwen3-Coder-30B raw checkpoint = `not run, disk limit warning`; repo-scale quality and AWS-to-Kaggle orchestration remain `unverified`.

## Qwen3.5-9B rerun with corrected prompt (2026-11-16)

The coding prompt was made internally consistent, each task now names its expected function explicitly, and the official Qwen3.5 chat-template switch `enable_thinking=False` was used. This follows the model card's documented direct-response mode: https://huggingface.co/Qwen/Qwen3.5-9B

- Qwen3.5-9B loaded on T4 x2 in 37.2 seconds from the session's existing cache. The three generation steps took 5.5, 6.8, and 13.9 seconds; outputs were 46, 59, and 122 tokens.
- The model returned complete, plausible code for all three tasks, including the correct stable-unique function name and an interval sort with a lambda key. The earlier unfinished-thinking behavior did not recur.
- The script still reported 0/9 because the checker remained too restrictive: it rejected `str.join`, failed to expose `list`/`int` builtins needed to evaluate type annotations, and did not allow `ast.keyword` / `ast.Lambda` used by `sorted(..., key=...)`. Therefore **0/9 is a checker execution failure, not a valid score for Qwen3.5**. At the time of this run, fixture correctness was not yet checked. The subsequent CPU recheck below supersedes this status.
- The session was stopped after the run; Kaggle showed `Draft Session off (run a cell to start)`. The separate existing user notebook and other sessions were not touched.

### Next evaluation fix

The captured answers have now passed the CPU checker and input-immutability checks below. Before using this evaluator on untrusted repository code, isolate execution in a real sandbox; the AST allowlist is only a smoke-test guard. Next, expand the rubric to representative repository tasks and test repair/tool-use behavior. This three-task score does not establish production repo-level quality.

**Updated gate status:** Qwen3.5-9B load and complete code generation = `observed`; official non-thinking template switch = `observed`; Qwen3.5 first script score = `invalid; checker rejected generated syntax` (superseded by CPU recheck below); GPU session stop = `observed`; repo-scale quality and AWS-to-Kaggle orchestration remain `unverified`.

## Captured Qwen3.5 answers rechecked on CPU (2026-11-16)

To avoid another GPU run, the three exact Qwen3.5-9B answers captured in the Kaggle output were evaluated locally with the corrected AST allowlist and deterministic fixtures. The checker accepts code fences, `str.join`, typed `list[int]` annotations, `lambda`, and `sorted(..., key=...)`; code execution was limited to those answers under a small builtin allowlist.

- Palindrome: 3/3; inputs unchanged.
- Stable unique: 3/3; inputs unchanged.
- Merge intervals: 3/3; inputs unchanged.
- Total: **9/9**; all supplied inputs remained unchanged.
- No Kaggle inference or GPU session was started for this recheck.

This corrects the earlier Qwen3.5 checker-rejected 0/9 report. It still measures only three small functions and is not evidence of repository-scale autonomous engineering quality.

**Updated gate status:** Qwen3.5-9B captured-answer fixture score = `9/9 observed on CPU`; Qwen3.5 model load and direct generation = `observed on Kaggle T4 x2`; repo-scale quality and AWS-to-Kaggle orchestration remain `unverified`.


