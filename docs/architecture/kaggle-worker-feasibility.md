# Kaggle worker feasibility: capability matrix

**Reviewed:** 2026-11-16  
**Decision:** **CONDITIONAL** — notebook inference works, but a supported, headless AWS-to-Kaggle multi-turn request/response path has not been established. Keep Kaggle behind a replaceable provider interface. Do not implement a production Kaggle provider or claim seamless interactive inference yet.

## Capability matrix

| Capability | Status | Evidence and limit |
|---|---|---|
| Start an interactive session | Documented and observed via the Kaggle editor; MCP start is documented but not observed | The editor starts a session when a cell is run. We observed T4 x2 on the dedicated test notebook. Kaggle MCP documents `create_notebook_session`; it returns a long-running operation. |
| Choose accelerator | Documented; T4 x2 observed in this account's test sessions | The UI exposes accelerator settings; MCP's session request has `machine_shape`. The API does not document that a requested shape is guaranteed to be available. |
| Poll readiness/status | Documented, not observed through API | MCP `get_notebook_session_status` returns `QUEUED`, `RUNNING`, `COMPLETE`, `ERROR`, `CANCEL_REQUESTED`, `CANCEL_ACKNOWLEDGED`, or `NEW_SCRIPT`. |
| Send multiple arbitrary prompts to one running session from AWS | **Unverified / not established** | The published Kaggle MCP notebook tools list create, status, cancel, session output, and file download; they do not document executing a cell or submitting a request to an already running session. `save_notebook` runs source top-to-bottom as a notebook run. Kaggle documents an experimental Jupyter Server for external editors (Colab/VS Code) using a compatible URL from the UI, but not an AWS headless request API or programmatic URL discovery. |
| Stop/cancel | Documented and observed through UI; MCP cancellation documented but not observed | We stopped each test session in the UI and confirmed `Draft Session off`. MCP documents `cancel_notebook_session`. |
| Retrieve logs/output | Documented, not observed through API | MCP can list session output and logs, download a notebook output file or output ZIP. This is sufficient for completed runs/artifacts; it does not establish prompt-by-prompt response retrieval from an interactive kernel. |
| Batch execution | Documented | Kaggle CLI supports `kernels push`, `status`, `logs`, and `output`. `push` starts a run; notebook versions execute from top to bottom. This is a batch workflow, not a low-latency tool loop. |
| Session duration / idle behavior | Documented with an apparent idle-time discrepancy; not independently measured here | Kaggle's Notebooks page says GPU/CPU sessions can run up to 12 hours and mentions 20 minutes of editor inactivity; Efficient GPU Usage guidance warns an interactive session can remain active for up to 60 minutes after the window closes. Treat idle cleanup as unreliable and always stop explicitly. |
| Weekly quota visibility | Documented, not checked for remaining account quota | Kaggle CLI documents `kaggle quota` for weekly GPU/TPU hours; the editor/account surfaces also display usage. This run recorded model/session time but did not read the remaining account quota. |
| Idle invariant | Observed in tested notebook | After each experiment the dedicated notebook showed `Draft Session off (run a cell to start)`. No other notebook was started or changed. |

## Interpretation

- The notebook editor can run a model and retain process state across several cell executions. That is useful for experiments and manually assisted work.
- The documented CLI/API has a batch path and a session lifecycle/status path. The official API surface reviewed here does not show how an AWS controller sends a code cell or model prompt to the running interactive kernel and receives its response.
- Kaggle's external-editor connection is explicitly labeled experimental. It may support a manually connected VS Code/Jupyter workflow, but it does not prove an unattended AWS worker integration or its token lifecycle.
- Therefore the project can proceed with provider-neutral controller/checkpoint work and a fake provider. Keep Kaggle as an experimental/manual backend until a supported two-request path, output retrieval, cancellation, and clean resume are demonstrated end-to-end.
- Do not use public tunnels, hidden endpoints, multiple accounts, or keep-alive techniques to bridge this gap.

## Official sources

- [Kaggle MCP tool reference](https://www.kaggle.com/docs/mcp) — interactive session creation, status, cancellation, and output tool definitions.
- [Kaggle Notebooks documentation](https://www.kaggle.com/docs/notebooks) — interactive editor, batch notebook versions, experimental Jupyter Server and external editor connection.
- [Kaggle Public API documentation](https://www.kaggle.com/docs/api) — CLI/API overview and `kernels` operations.
- [Kaggle CLI kernels reference](https://github.com/Kaggle/kaggle-cli/blob/main/skills/references/kernels.md) — push, status, logs, outputs, and kernel lifecycle commands.
- [Kaggle Efficient GPU Usage](https://www.kaggle.com/docs/efficient-gpu-usage) — advice to cancel unneeded runs and stop interactive sessions.
- [Kaggle CLI quota reference](https://github.com/Kaggle/kaggle-cli/blob/main/src/kaggle/cli.py) — `kaggle quota` reports weekly GPU and TPU accelerator quota.

## Next gate

Implement and test the fake-provider controller and durable checkpoint on AWS/Linux-compatible semantics. Before writing a Kaggle adapter, run a deliberate two-request experiment through only the documented external-editor/Jupyter connection and verify how AWS can obtain the connection details securely, retrieve both responses, cancel the session, and resume using the checkpoint. If the only workable flow needs manual URL copying or an undocumented endpoint, keep Kaggle out of unattended MVP automation.

## Official-source recheck (2026-11-17)

The official [Kaggle MCP reference](https://www.kaggle.com/docs/mcp) now documents a broader lifecycle surface than the earlier audit recorded: create a notebook session, check its status, cancel a session, list session output/logs, download session output, and save notebook source for a top-to-bottom run. This makes documented orchestration of **batch jobs** more plausible, but it still does not document an operation to execute an arbitrary prompt/cell inside an already-running session and return that cell's response. The official [Kaggle CLI kernel reference](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md) documents push/run, status, and output retrieval. It does not establish a low-latency persistent inference loop from AWS. Kaggle's [Notebook documentation](https://www.kaggle.com/docs/notebooks) calls the external Jupyter Server integration experimental and describes connecting from Colab/VS Code, not a headless AWS worker API.

**Policy gate remains unresolved.** Kaggle's [Terms of Use](https://www.kaggle.com/terms) say Services are for a user's own internal, personal, non-commercial use and not for the benefit of a third party; they also prohibit controlling or operating more than one active account. The terms do not answer whether an independently owned collaborator may voluntarily contribute notebook GPU compute to this specific shared coding project. Do not interpret separate ownership or notebook collaboration as authorization to pool compute. Obtain explicit Kaggle guidance before enabling collaborator-owned workers; never create or rotate accounts for one person.

**Decision:** keep `CONDITIONAL`, and keep all real Kaggle workers disabled. The official docs suggest a possible checkpointed batch path, but not a verified efficient multi-turn worker. Re-running top-to-bottom after each prompt would reload model state and consume more quota; the persistent-session code-execution and cancellation path has not been experimentally verified from AWS. Do not spend quota or touch any notebook to test this without a separately planned, isolated experiment.
