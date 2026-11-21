"""kite cleanup: old clones, libraries and code maps go; results, logs and kept tasks stay."""

from __future__ import annotations

import os
from pathlib import Path

from poc.kaggle_worker import cleanup, cli

DAY = 86400


def touch_tree(root: Path, when: float) -> None:
    for path in [root, *root.rglob("*")]:
        os.utime(path, (when, when), follow_symlinks=False)


def make_run(parent: Path, name: str, when: float, *, deps_link: Path | None = None) -> Path:
    run = parent / name
    (run / "work" / ".git").mkdir(parents=True)
    (run / "work" / "app.py").write_text("x = 1\n" * 1000)
    (run / "graph").mkdir()
    (run / "graph" / "graph.json").write_text("{}" * 500)
    if deps_link is not None:
        (run / "deps").symlink_to(deps_link, target_is_directory=True)
    else:
        (run / "deps" / "pkg").mkdir(parents=True)
        (run / "deps" / "pkg" / "lib.py").write_text("y = 2\n" * 2000)
        os.chmod(run / "deps" / "pkg" / "lib.py", 0o444)           # read-only, like installed packages
    (run / "result.json").write_text("{}")
    (run / "agent.log").write_text("log")
    touch_tree(run, when)
    return run


def test_plan_and_remove_keep_results_recent_tasks_and_shared_libraries(tmp_path, capsys):
    now = 100 * DAY
    state = tmp_path / "state"
    cache = state / "deps-cache" / "key1"
    (cache / "site").mkdir(parents=True)
    (cache / "site" / "big.py").write_text("z" * 5000)
    old = make_run(state / "tasks", "old-task", now - 10 * DAY, deps_link=cache)
    new = make_run(state / "tasks", "new-task", now - 1 * DAY, deps_link=cache)
    bench = state / "bench" / "20261101-000000"
    run = make_run(bench / "runs", "bench-r1", now - 20 * DAY)
    (bench / "repos" / "q-1" / "R1").mkdir(parents=True)
    (bench / "repos" / "q-1" / "R1" / "f.py").write_text("a" * 3000)
    (bench / "results.json").write_text("{}")
    touch_tree(bench, now - 20 * DAY)
    touch_tree(cache.parent, now - 30 * DAY)

    items = cleanup.plan(state, days=7, now=now)
    paths = {item.path for item in items}
    assert {old / "work", old / "graph", old / "deps", run / "work", run / "deps", run / "graph",
            bench / "repos"} == paths
    assert next(i for i in items if i.path == old / "deps").size == 0          # only a link to the cache
    # The cache entry is still linked from a kept (recent) task: never planned, even when asked for.
    assert cache not in {i.path for i in cleanup.plan(state, days=7, now=now, deps_cache=True)}

    freed, errors = cleanup.remove(items)
    assert not errors and freed == sum(i.size for i in items) and freed > 10000
    assert (old / "result.json").exists() and (old / "agent.log").exists() and (bench / "results.json").exists()
    assert not (old / "work").exists() and not (old / "deps").exists() and not (run / "deps").exists()
    assert (new / "work").exists() and (cache / "site" / "big.py").exists()       # recent task, shared cache

    # Once no kept task links to it, an old cache entry can go too (only with deps_cache).
    (new / "deps").unlink()
    assert cache in {i.path for i in cleanup.plan(state, days=7, now=now, deps_cache=True)}
    assert cache not in {i.path for i in cleanup.plan(state, days=7, now=now)}


def test_cleanup_command_lists_first_and_removes_only_with_yes(tmp_path, capsys):
    state = tmp_path / "state"
    old = make_run(state / "sandbox", "sbx-1", 0)
    config = state / "config.yaml"
    assert cli.main(["--config", str(config), "cleanup"]) == 0
    out = capsys.readouterr().out
    assert "thêm --yes để xoá" in out and "sandbox: 3 thư mục" in out and (old / "work").exists()
    assert cli.main(["--config", str(config), "cleanup", "--yes"]) == 0
    assert "Đã xoá" in capsys.readouterr().out and not (old / "work").exists() and (old / "result.json").exists()
