# Repo task 002: durable task checkpoints

Implement `CheckpointStore` in `src/kite_agent/checkpoint.py` so work can resume after a Kaggle worker or model is replaced.

A valid state is a JSON-compatible mapping with exactly these fields:
- `task_id`, `repo_head`, `goal`, `summary`: strings (task_id, repo_head, and goal must be non-empty; summary may be empty).
- `plan`, `completed_steps`, `changed_files`: lists of strings.

`save(state)` must validate the state, write a JSON envelope containing `schema_version: 1` and `state`, and atomically replace the destination using a temporary file in the same directory. Create parent directories when needed. Do not mutate the supplied mapping. Any temporary file must be cleaned up if saving fails.

`load(expected_repo_head=None)` must parse and validate the envelope and state, return the state mapping, and raise `CheckpointError` for missing files, malformed JSON, unsupported schema, malformed state, or repo-head mismatch. If expected_repo_head is supplied and differs from state.repo_head, raise `CheckpointError`; never delete or overwrite the checkpoint while loading.

Do not add dependencies. Run `python -m pytest -q` from the repo directory. Return the complete corrected `checkpoint.py` file only, without Markdown fences.
