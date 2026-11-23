"""Bootstrap notebook ("stub"): the Kaggle notebook keeps one small cell; the worker code comes from Kite.

The full worker notebook (kaggle_model_worker.ipynb) is long, and a copy saved on Kaggle went stale
after every update: a session started by hand ran old worker code until someone pasted the new
notebook in (step 2 of the full-test day, the "notebook cũ" warnings since). With the stub:

* The notebook on Kaggle holds only the stub cell (its datasets, GPU and Internet settings stay as
  set in the Kaggle UI).
* When it runs, the stub reads the worker token (``KITE_LAUNCH["token"]`` when Kite pushed this run
  with a per-launch token, else the Kaggle Secret ``KITE_WORKER_TOKEN``) and downloads the worker
  code from the gateway: ``GET /worker/code``, published through Caddy like the rest of ``/worker/*``.
* The gateway answers with the code cells of this repository's notebook, the WORKER_VERSION they
  carry, and the launch values for this run (which model to load first: the oldest request waiting
  for this worker; how long to stay idle). The stub runs the cells one by one, as Kaggle would.

So "Save Version" on kaggle.com, a click in Kite's own browser (browser.py) and a push through the
Kaggle API (launcher.py) all run the code that is on the gateway now.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

NOTEBOOK = Path(__file__).with_name("kaggle_model_worker.ipynb")
# A run that starts later than this after Kite filled in KITE_LAUNCH is not the run Kite asked for.
LAUNCH_WINDOW_SECONDS = 6 * 3600

STUB_SOURCE = '''# Kite: notebook khởi động. Code worker tải từ Kite mỗi lần chạy, nên không bao giờ là bản cũ.
# Giữ nguyên: GPU T4 x2, Internet bật, các dataset kite-llama-bin và kite-gguf-* (Add Input),
# Secret KITE_WORKER_TOKEN (Add-ons -> Secrets). Chạy: Save Version -> Save & Run All.
import json, os, time, urllib.error, urllib.request

KITE_GATEWAY = {gateway!r}   # gateway.public_url trên máy Kite
KITE_LAUNCH = {{}}   # Kite điền khi tự đẩy notebook này lên Kaggle; chạy tay: để trống

_kite_pushed = KITE_LAUNCH if KITE_LAUNCH and time.time() <= KITE_LAUNCH.get("expires", 0) else {{}}
KITE_TOKEN = _kite_pushed.get("token")
if not KITE_TOKEN:
    try:
        from kaggle_secrets import UserSecretsClient
        KITE_TOKEN = UserSecretsClient().get_secret("KITE_WORKER_TOKEN")
    except Exception as exc:
        raise RuntimeError("Không đọc được Secret KITE_WORKER_TOKEN: Add-ons -> Secrets, bật nó cho notebook "
                           "này (token in ra từ `kite worker token <worker>` trên máy Kite).") from exc
KITE_GATEWAY = (_kite_pushed.get("gateway_url") or KITE_GATEWAY).rstrip("/")
_kite_request = urllib.request.Request(KITE_GATEWAY + "/worker/code", headers={{
    "Authorization": "Bearer " + KITE_TOKEN,
    "X-Kite-Run-Type": os.environ.get("KAGGLE_KERNEL_RUN_TYPE", "")}})
for _kite_try in range(6):
    try:
        with urllib.request.urlopen(_kite_request, timeout=60) as _kite_answer:
            KITE_BUNDLE = json.loads(_kite_answer.read())
        break
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError("Kite không nhận token này (HTTP %d): kiểm tra Secret KITE_WORKER_TOKEN" % exc.code)
        if _kite_try == 5:
            raise
    except OSError:
        if _kite_try == 5:
            raise
    time.sleep(10 * (_kite_try + 1))
# Giá trị Kite đưa cho lần chạy này (model nạp trước, số phút rảnh thì tự tắt); bản Kite đẩy lên thắng.
KITE_STUB_LAUNCH = {{**KITE_BUNDLE.get("launch", {{}}), **_kite_pushed}}
print("Kite worker code", KITE_BUNDLE.get("version"), "-", len(KITE_BUNDLE["cells"]), "ô")
for _kite_i, _kite_source in enumerate(KITE_BUNDLE["cells"]):
    exec(compile(_kite_source, "<kite ô %d>" % _kite_i, "exec", dont_inherit=True), globals())
'''


def notebook_cells(notebook: Path = NOTEBOOK) -> list[str]:
    """The code cells of the full worker notebook, in order, exactly as saved."""
    nb = json.loads(Path(notebook).read_text(encoding="utf-8"))
    return ["".join(cell.get("source", [])) for cell in nb.get("cells", []) if cell.get("cell_type") == "code"]


def worker_bundle(launch: dict[str, Any], notebook: Path = NOTEBOOK) -> dict[str, Any]:
    """What ``GET /worker/code`` answers: the notebook's code cells and this run's launch values."""
    from .pull_worker import WORKER_VERSION

    return {"version": WORKER_VERSION, "cells": notebook_cells(notebook), "launch": launch}


def launch_for(worker_id: str, *, now: float, idle_minutes: int = 30, model: str | None = None,
               gateway_url: str | None = None) -> dict[str, Any]:
    """Launch values for a stub run: like the ones Kite writes into a notebook it pushes."""
    launch: dict[str, Any] = {"worker_id": worker_id, "idle_minutes": idle_minutes, "reason": "stub",
                              "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                              "expires": int(now + LAUNCH_WINDOW_SECONDS)}
    if model:
        launch["model"] = model
    if gateway_url:
        launch["gateway_url"] = gateway_url
    return launch


def stub_source(gateway_url: str) -> str:
    return STUB_SOURCE.format(gateway=gateway_url.rstrip("/"))


def stub_notebook(gateway_url: str) -> dict[str, Any]:
    """The whole stub notebook (nbformat 4): a short note and the stub cell."""
    note = ("# Kite worker (notebook khởi động)\n\nÔ dưới tải code worker mới nhất từ Kite rồi chạy nó. "
            "Đừng dán code worker vào đây: sửa trong repo Kite là đủ.")
    return {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": note.splitlines(keepends=True)},
            {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [],
             "source": stub_source(gateway_url).splitlines(keepends=True)},
        ],
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                     "language_info": {"name": "python"}},
        "nbformat": 4, "nbformat_minor": 5,
    }
