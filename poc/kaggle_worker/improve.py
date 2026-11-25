"""Vòng tự cải tiến: từ điểm trục bench yếu nhất thành đề xuất chờ duyệt (Gói 10).

Luồng: điểm trục bench (Gói 7 đóng dấu mỗi lần chạy) -> trục yếu nhất ->
đề xuất cải tiến (gợi ý từ tài liệu kinh nghiệm đã duyệt, nếu có) -> hàng chờ
``state_dir/memory/improve_proposals.json`` -> CHỦ DUYỆT mới tạo task trên repo
Kite qua đường tạo task thường (mặc định: hàng đợi ``kite task add``;
xem ``_default_create_task``). Kite không tự tạo task, không tự sửa code
của mình ngoài luồng này; cổng Áp/Commit giữ nguyên.

Reader điểm trục (``axis_scores``) thuộc Gói 10, định dạng lần chạy thuộc
Gói 7. Tên key JSON của từng trục CHƯA được Gói 7 ghim chữ, nên reader chuẩn
hóa cả hai dạng (tên hiển thị tiếng Việt và slug tiếng Anh) về slug chuẩn
nội bộ qua ``AXIS_KEYS``. Khi Gói 7 ghim tên key khác, chỉ sửa bảng
``AXIS_KEYS`` (và ``_AXIS_CONTAINERS`` nếu đổi tên trường chứa điểm trục),
không đổi gì khác.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
import uuid
from typing import Any, Callable

from .memory import repo_key

# Repo Kite mặc định để tạo task: thư mục làm việc hiện tại (checkout chứa
# file này), KHÔNG hard-code đường dẫn tuyệt đối của máy nào (cùng mẫu
# REPO_ROOT trong cli.py).
REPO_ROOT = Path(__file__).resolve().parents[2]

# 8 trục bench, theo hợp đồng Gói 7 (thứ tự này cũng là thứ tự hòa khi
# hai trục bằng điểm nhau ở weakest_axes).
AXES: tuple[str, ...] = (
    "coding",
    "phan_tich",
    "suy_luan",
    "toc_do",
    "dung_viec",
    "chi_phi",
    "on_dinh",
    "tri_nho",
)

AXIS_LABELS: dict[str, str] = {
    "coding": "Coding",
    "phan_tich": "Phân tích",
    "suy_luan": "Suy luận",
    "toc_do": "Tốc độ",
    "dung_viec": "Đúng việc",
    "chi_phi": "Chi phí",
    "on_dinh": "Ổn định",
    "tri_nho": "Trí nhớ",
}

# Bảng chuẩn hóa tên key JSON -> slug chuẩn nội bộ. Chấp nhận: slug chuẩn,
# tên hiển thị tiếng Việt (đúng dấu / không dấu / khác hoa-thường) và slug
# tiếng Anh rõ nghĩa nhất của từng trục. Gói 7 ghim key khác thì chỉ sửa
# bảng này.
AXIS_KEYS: dict[str, str] = {
    "coding": "coding",
    "phan_tich": "phan_tich",
    "phân tích": "phan_tich",
    "phan tich": "phan_tich",
    "analysis": "phan_tich",
    "suy_luan": "suy_luan",
    "suy luận": "suy_luan",
    "suy luan": "suy_luan",
    "reasoning": "suy_luan",
    "toc_do": "toc_do",
    "tốc độ": "toc_do",
    "toc do": "toc_do",
    "speed": "toc_do",
    "dung_viec": "dung_viec",
    "đúng việc": "dung_viec",
    "dung viec": "dung_viec",
    "relevance": "dung_viec",
    "chi_phi": "chi_phi",
    "chi phí": "chi_phi",
    "chi phi": "chi_phi",
    "cost": "chi_phi",
    "on_dinh": "on_dinh",
    "ổn định": "on_dinh",
    "on dinh": "on_dinh",
    "stability": "on_dinh",
    "tri_nho": "tri_nho",
    "trí nhớ": "tri_nho",
    "tri nho": "tri_nho",
    "memory": "tri_nho",
}


def _normalize_axis_key(key: Any) -> str | None:
    """Slug chuẩn nội bộ của một tên key JSON, hoặc None khi không phải trục."""
    if not isinstance(key, str):
        return None
    return AXIS_KEYS.get(key.strip().lower())


def _normalize_axis_map(raw: Any) -> dict[str, float]:
    """Chuẩn hóa một dict điểm trục (key VI/EN) về {slug: điểm số}."""
    scores: dict[str, float] = {}
    if not isinstance(raw, dict):
        return scores
    for key, value in raw.items():
        slug = _normalize_axis_key(key)
        if slug is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        scores[slug] = float(value)
    return scores


# Tên trường có thể chứa dict điểm trục trong một results.json. Gói 7 chưa
# ghim chữ nên đọc tolerant: thử lần lượt các tên này ở cấp lần chạy, rồi
# ở từng model trong "models", rồi key trục viết phẳng ở cấp lần chạy.
_AXIS_CONTAINERS = ("axes", "axis_scores", "scores")


def _run_axis_scores(data: Any) -> dict[str, float]:
    """Điểm từng trục của MỘT lần chạy (dict đã đọc từ results.json)."""
    if not isinstance(data, dict):
        return {}
    merged: dict[str, list[float]] = {}

    def _collect(raw: Any) -> None:
        for slug, value in _normalize_axis_map(raw).items():
            merged.setdefault(slug, []).append(value)

    for name in _AXIS_CONTAINERS:
        _collect(data.get(name))
    _collect(data)  # key trục viết phẳng ở cấp lần chạy
    models = data.get("models")
    if isinstance(models, dict):
        for entry in models.values():
            if isinstance(entry, dict):
                for name in _AXIS_CONTAINERS:
                    _collect(entry.get(name))
    # Cùng một trục xuất hiện ở nhiều chỗ trong một lần chạy thì lấy trung
    # bình (các chỗ đó phải khớp nhau; lệch nhau là dữ liệu lạ, không crash).
    return {slug: sum(values) / len(values) for slug, values in merged.items()}


def _iter_runs(state_dir: Path) -> list[dict]:
    """Mọi lần chạy bench đọc được, cũ trước: {version, started, scores}.

    Bỏ qua file hỏng (không đọc được / JSON lỗi / không phải dict) và thư
    mục không có results.json. Không bao giờ raise vì dữ liệu bench xấu.
    """
    runs: list[dict] = []
    try:
        paths = sorted((Path(state_dir) / "bench").glob("*/results.json"))
    except OSError:
        return runs
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        version = data.get("criteria_version")
        runs.append({
            "version": str(version) if version is not None else None,
            "started": str(data.get("started") or ""),
            "folder": path.parent.name,
            "scores": _run_axis_scores(data),
        })
    runs.sort(key=lambda run: (run["started"], run["folder"]))
    return runs


def _chosen_version(runs: list[dict], criteria_version: str | None) -> str | None:
    """Version được chọn: đúng version truyền vào, hoặc version của lần chạy
    mới nhất (cũ trước -> mới nhất nằm cuối danh sách đã sắp)."""
    if criteria_version is not None:
        return criteria_version
    return runs[-1]["version"] if runs else None


def axis_scores(state_dir: Path, *, criteria_version: str | None = None) -> dict[str, float]:
    """Điểm trung bình từng trục (0-100, làm tròn 1 chữ số thập phân).

    Chỉ tính các lần chạy cùng một ``criteria_version`` (mặc định: version
    của lần chạy mới nhất còn đủ >=1 lần chạy). Điểm một trục = trung bình
    các lần chạy cùng version có trục đó. Chưa có lần chạy nào mang điểm
    trục thì trả ``{}`` (không lỗi).
    """
    runs = _iter_runs(Path(state_dir))
    if not runs:
        return {}
    version = _chosen_version(runs, criteria_version)
    chosen = [run for run in runs if run["version"] == version]
    if not chosen:
        return {}
    per_axis: dict[str, list[float]] = {}
    for run in chosen:
        for slug, value in run["scores"].items():
            per_axis.setdefault(slug, []).append(value)
    return {slug: round(sum(values) / len(values), 1) for slug, values in per_axis.items()}


def weakest_axes(scores: dict[str, float], *, n: int = 3) -> list[tuple[str, float]]:
    """Các trục yếu nhất, điểm thấp nhất trước (hòa thì theo thứ tự 8 trục)."""
    if n <= 0:
        return []
    order = {slug: index for index, slug in enumerate(AXES)}
    ranked = sorted(scores.items(), key=lambda item: (item[1], order.get(item[0], len(AXES))))
    return ranked[:n]


# --------------------------------------------------------------------------------------- đề xuất

# Hướng cải tiến chung cho từng trục, dùng khi tài liệu kinh nghiệm không có
# quy tắc nào liên quan tới trục đó. Không hard-code số chính sách: đây là
# gợi ý chữ, ngưỡng/điểm vẫn do bench (Gói 7) và chủ duyệt quyết.
AXIS_DIRECTIONS: dict[str, str] = {
    "coding": "thêm bài code khó hơn vào tập bench và xem lại các lỗi hay lặp trong patch",
    "phan_tich": "rèn bài phân tích yêu cầu trước khi viết code, kiểm tra file kỳ vọng của bài",
    "suy_luan": "rèn bài suy luận nhiều bước, yêu cầu nêu rõ giả thiết trước khi kết luận",
    "toc_do": "đo lại thời gian từng bước để tìm bước chậm nhất rồi tối ưu đúng chỗ đó",
    "dung_viec": "soát lại goal của bài bench với kết quả, siết tiêu chí 'đúng việc' khi chấm",
    "chi_phi": "xem lại chi phí theo model/worker của các lần chạy, đổi model rẻ hơn ở bài dễ",
    "on_dinh": "chạy lặp cùng bài để tìm lần chạy lệch, ghi lại điều kiện gây lệch",
    "tri_nho": "kiểm tra bài học/tài liệu kinh nghiệm có được nạp vào task không, bổ sung lesson_ref",
}

# Từ khóa để nối một quy tắc trong tài liệu kinh nghiệm với một trục.
AXIS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "coding": ("coding", "code", "viết code", "lập trình"),
    "phan_tich": ("phân tích", "analysis"),
    "suy_luan": ("suy luận", "reasoning", "suy luan"),
    "toc_do": ("tốc độ", "speed", "chậm", "nhanh"),
    "dung_viec": ("đúng việc", "relevance", "sai việc"),
    "chi_phi": ("chi phí", "cost", "giá", "tiền"),
    "on_dinh": ("ổn định", "stability", "lệch", "không ổn định"),
    "tri_nho": ("trí nhớ", "memory", "bài học", "kinh nghiệm"),
}


def _experience_rules(state_dir: Path, repo: str | Path) -> list[str]:
    """Các quy tắc trong tài liệu kinh nghiệm ĐÃ DUYỆT của repo, mỗi quy tắc một dòng.

    Đọc thẳng file ``state_dir/memory/<repo_key>.experience.md`` (cùng định
    danh repo với bài học trong memory.py); KHÔNG phụ thuộc module của
    worker (b). Chưa có tài liệu thì trả [].
    """
    path = Path(state_dir) / "memory" / f"{repo_key(repo)}.experience.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rules = []
    for line in text.splitlines():
        if line.startswith("- "):
            rule = line[2:].strip()
            if rule:
                rules.append(rule)
    return rules


def build_proposals(state_dir: Path, repo: str | Path, *, clock: Callable[[], float] = time.time) -> list[dict]:
    """Một đề xuất cho mỗi trục yếu nhất (mặc định 3 trục; xem weakest_axes).

    Mỗi đề xuất: ``{axis, score, criteria_version, proposal}`` với proposal
    là text tiếng Việt nêu trục yếu + điểm + hướng cải tiến gợi ý từ quy tắc
    liên quan trong tài liệu kinh nghiệm, hoặc hướng chung khi không có tài
    liệu. Chưa có điểm trục nào thì trả [] (không sinh đề xuất).
    """
    del clock  # đề xuất chưa đóng dấu thời gian; created_at ghi lúc vào hàng chờ
    state_dir = Path(state_dir)
    runs = _iter_runs(state_dir)
    scores = axis_scores(state_dir)
    if not scores:
        return []
    version = _chosen_version(runs, None)
    rules = _experience_rules(state_dir, repo)
    proposals: list[dict] = []
    for axis, score in weakest_axes(scores):
        label = AXIS_LABELS[axis]
        direction = AXIS_DIRECTIONS[axis]
        matched = [rule for rule in rules
                   if any(keyword in rule.lower() for keyword in AXIS_KEYWORDS[axis])][:2]
        text = (f"Trục {label} đang yếu ({score}/100, criteria {version}). "
                f"Hướng cải tiến: {direction}.")
        if matched:
            quoted = "; ".join(f"“{rule}”" for rule in matched)
            text += f" Tài liệu kinh nghiệm ghi: {quoted} — áp quy tắc này khi cải tiến."
        proposals.append({"axis": axis, "score": score,
                          "criteria_version": version, "proposal": text})
    return proposals


# --------------------------------------------------------------------------------------- hàng chờ

QUEUE_FILENAME = "improve_proposals.json"


def _queue_path(state_dir: Path) -> Path:
    return Path(state_dir) / "memory" / QUEUE_FILENAME


def _load_proposals(state_dir: Path) -> list[dict]:
    """Mọi entry trong hàng chờ; file chưa có/hỏng thì [] (không crash)."""
    try:
        data = json.loads(_queue_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [dict(entry) for entry in data] if isinstance(data, list) else []


def _save_queue(state_dir: Path, entries: list[dict]) -> None:
    """Ghi hàng chờ nguyên tử, cùng mẫu memory._save_pending: file .tmp +
    os.replace, mode 0600 (không bao giờ ghi đè trực tiếp)."""
    path = _queue_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(entries, stream, indent=1, ensure_ascii=False)
    os.replace(temp, path)


def list_proposals(state_dir: Path) -> list[dict]:
    """Mọi đề xuất trong hàng chờ, kèm trạng thái (pending/approved/rejected/task_created)."""
    return _load_proposals(Path(state_dir))


def save_proposals(state_dir: Path, proposals: list[dict],
                   *, clock: Callable[[], float] = time.time) -> list[dict]:
    """Thêm đề xuất mới vào hàng chờ, KHÔNG ghi đè entry cũ.

    Mỗi đề xuất vào hàng chờ thành entry ``{id, repo, axis, score,
    criteria_version, proposal, created_at, status, task_id}`` với status
    "pending". ``repo`` lấy từ đề xuất (khóa "repo") nếu có, mặc định là
    repo Kite (thư mục làm việc hiện tại, REPO_ROOT). Trả các entry mới.
    """
    state_dir = Path(state_dir)
    entries = _load_proposals(state_dir)
    new_entries: list[dict] = []
    for proposal in proposals:
        entry = {
            "id": uuid.uuid4().hex[:8],
            "repo": str(proposal.get("repo") or REPO_ROOT),
            "axis": proposal.get("axis"),
            "score": proposal.get("score"),
            "criteria_version": proposal.get("criteria_version"),
            "proposal": proposal.get("proposal"),
            "created_at": clock(),
            "status": "pending",
            "task_id": None,
        }
        entries.append(entry)
        new_entries.append(dict(entry))
    _save_queue(state_dir, entries)
    return new_entries


def _find_proposal(entries: list[dict], proposal_id: str) -> dict:
    for entry in entries:
        if entry.get("id") == proposal_id:
            return entry
    raise ValueError(f"không có đề xuất cải tiến {proposal_id} trong hàng chờ")


def reject_proposal(state_dir: Path, proposal_id: str) -> dict:
    """Bỏ một đề xuất: entry chuyển trạng thái "rejected" (giữ lại bản ghi,
    không xóa khỏi file — trạng thái này nằm trong hợp đồng Gói 10)."""
    state_dir = Path(state_dir)
    entries = _load_proposals(state_dir)
    entry = _find_proposal(entries, proposal_id)
    entry["status"] = "rejected"
    _save_queue(state_dir, entries)
    return dict(entry)


def _enqueue_task(state_dir: Path, repo: str | Path, goal: str,
                  *, clock: Callable[[], float] = time.time) -> dict:
    """Đường tạo task thường, cùng hàm ``kite task add`` dùng (cli.py:
    ``_scheduled_task`` + ``store.enqueue``): chỉ xếp task vào hàng đợi,
    KHÔNG chạy task, KHÔNG áp/commit. Task xếp hàng rồi vẫn chạy qua
    pipeline thường (sandbox, kiểm chứng, patch chờ duyệt).

    DB là ``state_dir/scheduler.sqlite3`` (cùng mặc định ``_db_path`` của
    cli: scheduler.sqlite3 cạnh config); repo_head lấy bằng ``git rev-parse
    HEAD`` trong repo, không bịa.
    """
    import subprocess

    from .protocol import ScheduledTask
    from .store import SQLiteSchedulerStore

    repo_path = Path(repo).expanduser().resolve()
    head = subprocess.check_output(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"], text=True).strip()
    task_id = uuid.uuid4().hex
    task = ScheduledTask(
        task_id=task_id, role="coder", model_id=None, repo_head=head,
        branch=f"agent/{task_id}",
        payload_json=json.dumps({"goal": goal}, ensure_ascii=False, sort_keys=True),
        estimated_gpu_seconds=900, priority=0, created_at=clock(), task_type="coding",
        required_capabilities=(),
    )
    with SQLiteSchedulerStore(Path(state_dir) / "scheduler.sqlite3") as store:
        store.enqueue(task)
    return {"task_id": task_id}


def _resolve_create_task(ops: Any, state_dir: Path, clock: Callable[[], float]):
    """Hàm tạo task (repo, goal) -> {"task_id": ...} từ ``ops``.

    ``ops`` có thể là: None (mặc định: hàng đợi ``kite task add`` qua
    ``_enqueue_task``); một callable trực tiếp; một mapping có khóa
    "create_task"; hoặc một object có thuộc tính ``create_task``. Nơi nào
    đã có KiteOps (API/MCP của worker (b)) thì truyền create_task gọi
    ``KiteOps.start_task`` — đường đó thêm bước khởi động workflow Temporal
    nên chỉ dùng khi có gateway/Temporal thật, không dùng ở CLI.
    """
    if ops is None:
        return lambda repo, goal: _enqueue_task(state_dir, repo, goal, clock=clock)
    if callable(ops):
        return ops
    if isinstance(ops, dict) and callable(ops.get("create_task")):
        return ops["create_task"]
    create_task = getattr(ops, "create_task", None)
    if callable(create_task):
        return create_task
    raise ValueError("ops phải có create_task(repo, goal) -> {'task_id': ...}")


def approve_proposal(state_dir: Path, proposal_id: str, *,
                     ops: Any = None, clock: Callable[[], float] = time.time) -> dict:
    """Duyệt một đề xuất: tạo task trên repo Kite rồi entry chuyển "task_created".

    Goal của task = text đề xuất + ghi rõ nguồn (trục bench, điểm, criteria
    version). Chỉ entry "pending" mới duyệt được; tạo task lỗi thì entry ở
    nguyên "pending" và lỗi được ném ra (không ghi task_id giả). KHÔNG tự
    chạy task, KHÔNG tự áp/commit — task chạy qua pipeline thường.
    """
    state_dir = Path(state_dir)
    entries = _load_proposals(state_dir)
    entry = _find_proposal(entries, proposal_id)
    if entry.get("status") != "pending":
        raise ValueError(
            f"đề xuất {proposal_id} đã ở trạng thái {entry.get('status')}, không duyệt được")
    label = AXIS_LABELS.get(str(entry.get("axis")), str(entry.get("axis")))
    goal = (f"{entry.get('proposal')}\n\n(Nguồn: trục bench {label} "
            f"{entry.get('score')}/100, criteria {entry.get('criteria_version')}; "
            f"đề xuất cải tiến đã duyệt.)")
    create_task = _resolve_create_task(ops, state_dir, clock)
    result = create_task(str(entry.get("repo")), goal)
    task_id = result.get("task_id") if isinstance(result, dict) else result
    if not task_id or not isinstance(task_id, str):
        raise ValueError("create_task phải trả {'task_id': ...}")
    entry["status"] = "task_created"
    entry["task_id"] = task_id
    _save_queue(state_dir, entries)
    return dict(entry)



# --------------------------------------------------------------------------------------- CLI

def register_improve_commands(commands) -> None:
    """Đăng ký nhóm ``improve`` vào subparsers cấp cao của ``cli._parser()``
    (cùng mẫu nhóm ``memory`` ở cli.py:102-115).

    Agent trưởng gắn vào ``cli.py`` (xem snippet ở cuối file này): gọi hàm
    này trong ``_parser()`` sau khi tạo ``commands``. ``commands`` là đối
    tượng ``add_subparsers(dest="command")`` của parser ``kite``.
    """
    improve_parser = commands.add_parser(
        "improve", help="đề xuất cải tiến từ điểm trục bench (chủ duyệt mới tạo task)")
    sub = improve_parser.add_subparsers(dest="improve_command", required=True)
    sub.add_parser("list", help="đề xuất cải tiến đang chờ duyệt")
    approve = sub.add_parser(
        "approve", help="duyệt một đề xuất: tạo task trên repo Kite qua đường tạo task thường")
    approve.add_argument("proposal_id", help="id hiện trong `improve list`")
    reject = sub.add_parser("reject", help="bỏ một đề xuất cải tiến")
    reject.add_argument("proposal_id", help="id hiện trong `improve list`")


def _improve_command(args: argparse.Namespace) -> int:
    """Dispatcher cho ``kite improve list | approve <id> | reject <id>``.

    Output qua ``cli._emit`` (import lười từ cli để tránh vòng lặp import:
    cli.py chỉ import improve lười — lúc gắn vào cli, import improve trong
    ``_parser()``/``_operator_command`` chứ không ở đầu file — nên lúc
    ``_improve_command`` chạy thì cli đã nạp xong; improve.py không import
    cli ở cấp module nên ``import improve`` độc lập vẫn nhẹ). Lỗi ValueError
    (không có id, đã duyệt rồi...) đổi thành ConfigError như _memory_command.
    """
    from .cli import _emit
    from .config import ConfigError

    state_dir = args.config.parent
    if args.improve_command == "list":
        _emit({"proposals": list_proposals(state_dir)})
        return 0
    try:
        if args.improve_command == "approve":
            done = approve_proposal(state_dir, args.proposal_id)
        elif args.improve_command == "reject":
            done = reject_proposal(state_dir, args.proposal_id)
        else:  # pragma: no cover - required=True ở parser chặn trường hợp này
            raise ConfigError(f"lệnh improve không rõ: {args.improve_command}")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    _emit(done)
    return 0


# ---------------------------------------------------------------------------------------
# Snippet gắn vào cli.py lúc ghép (agent trưởng làm, worker (c) KHÔNG sửa cli.py):
#
#   # 1. Trong _parser(), sau dòng tạo `commands` (cli.py, sau `commands =
#   #    parser.add_subparsers(dest="command", required=True)`):
#   from .improve import register_improve_commands
#   register_improve_commands(commands)
#
#   # 2. Trong _run(), thêm "improve" vào tập lệnh đi qua _operator_command
#   #    (dòng `if args.command in {...}`):
#   #    ... "memory", "update", "web", "busy", "stack", "tasks", "improve"}:
#
#   # 3. Trong _operator_command(), cạnh nhánh memory (sau `if args.command
#   #    == "memory": return _memory_command(args)`):
#   if args.command == "improve":
#       from .improve import _improve_command
#       return _improve_command(args)
#
# Cả 3 import đều lười (trong hàm) nên không tạo vòng lặp import với
# `from .cli import _emit` lười ở _improve_command.
