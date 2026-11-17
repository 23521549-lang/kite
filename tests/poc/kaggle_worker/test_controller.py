from pathlib import Path
import sys
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
from poc.kaggle_worker.controller import WorkerController, WorkerError, WorkerFailed, WorkerStartTimeout
from poc.kaggle_worker.protocol import CheckpointRef, ModelRequest, ModelResponse, WorkerStatus


class FakeProvider:
    def __init__(self, statuses=None, events=None, stop_error_count=0):
        self.statuses = list(statuses or [WorkerStatus.READY])
        self.events = events if events is not None else []
        self.stop_error_count = stop_error_count
        self.start_calls, self.status_calls, self.invoke_calls, self.stop_calls = [], [], [], []

    def start(self, task_id):
        self.events.append(("start", task_id)); self.start_calls.append(task_id)
        return f"session-{task_id}"

    def status(self, session_id):
        self.events.append(("status", session_id)); self.status_calls.append(session_id)
        if len(self.statuses) > 1: return self.statuses.pop(0)
        return self.statuses[0]

    def invoke(self, session_id, request):
        self.events.append(("invoke", session_id, request.prompt)); self.invoke_calls.append((session_id, request))
        return ModelResponse(text=f"reply:{request.prompt}", model_id="fake-model")

    def stop(self, session_id):
        self.events.append(("stop", session_id)); self.stop_calls.append(session_id)
        if self.stop_error_count:
            self.stop_error_count -= 1
            raise OSError("temporary stop failure")


class FakeClock:
    def __init__(self): self.value = 0.0
    def monotonic(self): return self.value
    def sleep(self, duration): self.value += duration


def checkpoint_writer(events):
    def write(task_id, reason):
        events.append(("checkpoint", task_id, reason))
        return CheckpointRef(task_id=task_id, path=Path(f"{task_id}.json"))
    return write


def test_controller_module_is_present():
    assert (PROJECT_ROOT / "poc" / "kaggle_worker" / "controller.py").is_file()


def test_first_request_starts_worker_and_later_request_reuses_it():
    provider = FakeProvider(); controller = WorkerController(provider)
    assert controller.active_task_id is None
    assert controller.status is WorkerStatus.STOPPED
    assert provider.start_calls == []
    first = controller.run_request("task-1", ModelRequest(prompt="one"))
    second = controller.run_request("task-1", ModelRequest(prompt="two"))
    assert first.text == "reply:one" and second.text == "reply:two"
    assert provider.start_calls == ["task-1"] and len(provider.invoke_calls) == 2
    assert controller.status is WorkerStatus.READY


def test_one_active_task_is_enforced():
    provider = FakeProvider(); controller = WorkerController(provider)
    controller.run_request("task-1", ModelRequest(prompt="one"))
    with pytest.raises(WorkerError, match="task-1"):
        controller.run_request("task-2", ModelRequest(prompt="two"))
    assert provider.start_calls == ["task-1"]


def test_startup_waits_until_ready_before_invoking():
    provider = FakeProvider([WorkerStatus.STARTING, WorkerStatus.READY]); clock = FakeClock()
    controller = WorkerController(provider, sleep=clock.sleep, monotonic=clock.monotonic, poll_interval_seconds=0.1)
    response = controller.run_request("task-1", ModelRequest(prompt="hello"))
    assert response.text == "reply:hello"
    assert [event[0] for event in provider.events] == ["start", "status", "status", "invoke"]
    assert clock.value == pytest.approx(0.1)


def test_startup_timeout_stops_worker_and_preserves_resume_identity():
    provider = FakeProvider([WorkerStatus.STARTING]); clock = FakeClock()
    controller = WorkerController(provider, sleep=clock.sleep, monotonic=clock.monotonic, poll_interval_seconds=0.1, startup_timeout_seconds=0.25)
    with pytest.raises(WorkerStartTimeout) as error:
        controller.run_request("task-1", ModelRequest(prompt="hello"))
    assert error.value.task_id == "task-1" and error.value.session_id == "session-task-1"
    assert provider.stop_calls == ["session-task-1"] and controller.active_task_id is None


def test_failed_worker_is_stopped_and_task_id_is_preserved():
    provider = FakeProvider([WorkerStatus.FAILED]); controller = WorkerController(provider)
    with pytest.raises(WorkerFailed) as error:
        controller.run_request("task-1", ModelRequest(prompt="hello"))
    assert error.value.task_id == "task-1"
    assert provider.stop_calls == ["session-task-1"] and controller.active_task_id is None


def test_pause_checkpoints_before_stop_and_stop_is_not_repeated():
    events = []; provider = FakeProvider(events=events)
    controller = WorkerController(provider, checkpoint_writer=checkpoint_writer(events))
    controller.run_request("task-1", ModelRequest(prompt="hello"))
    ref = controller.pause("task-1", "approval required"); controller.complete("task-1")
    assert ref == CheckpointRef(task_id="task-1", path=Path("task-1.json"))
    assert events.index(("checkpoint", "task-1", "approval required")) < events.index(("stop", "session-task-1"))
    assert provider.stop_calls == ["session-task-1"] and controller.active_task_id is None
    assert controller.status is WorkerStatus.STOPPED


def test_stop_failure_keeps_session_for_safe_retry():
    events = []; provider = FakeProvider(events=events, stop_error_count=1)
    controller = WorkerController(provider, checkpoint_writer=checkpoint_writer(events))
    controller.run_request("task-1", ModelRequest(prompt="hello"))
    with pytest.raises(OSError, match="temporary stop failure"):
        controller.pause("task-1", "pause")
    assert controller.active_task_id == "task-1"
    controller.pause("task-1", "pause")
    assert provider.stop_calls == ["session-task-1", "session-task-1"] and controller.active_task_id is None


def test_pause_without_checkpoint_writer_does_not_stop_worker():
    provider = FakeProvider(); controller = WorkerController(provider)
    controller.run_request("task-1", ModelRequest(prompt="hello"))
    with pytest.raises(WorkerError, match="checkpoint"):
        controller.pause("task-1", "pause")
    assert provider.stop_calls == [] and controller.active_task_id == "task-1"


def test_idle_timeout_checkpoints_then_stops_once():
    events = []
    provider = FakeProvider(events=events)
    clock = FakeClock()
    controller = WorkerController(provider, checkpoint_writer=checkpoint_writer(events), sleep=clock.sleep, monotonic=clock.monotonic)
    controller.run_request("task-1", ModelRequest(prompt="hello"))

    assert callable(getattr(controller, "stop_if_idle", None))
    assert controller.stop_if_idle(5) is False
    clock.sleep(5)
    assert controller.stop_if_idle(5) is True
    assert callable(getattr(controller, "stop_if_idle", None))
    assert controller.stop_if_idle(5) is False
    assert events.index(("checkpoint", "task-1", "idle timeout")) < events.index(("stop", "session-task-1"))
    assert provider.stop_calls == ["session-task-1"]




def test_concurrent_requests_are_serialized_on_one_session():
    import threading

    class BlockingProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.first_entered = threading.Event()
            self.release_first = threading.Event()
            self.second_entered = threading.Event()
            self.guard = threading.Lock()
            self.in_flight = 0
            self.max_in_flight = 0
            self.call_count = 0

        def invoke(self, session_id, request):
            with self.guard:
                self.call_count += 1
                call_number = self.call_count
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            if call_number == 1:
                self.first_entered.set()
                self.release_first.wait(timeout=2)
            else:
                self.second_entered.set()
            with self.guard:
                self.in_flight -= 1
            return ModelResponse(text=f"reply:{request.prompt}")

    provider = BlockingProvider()
    controller = WorkerController(provider)
    first = threading.Thread(target=controller.run_request, args=("task-1", ModelRequest(prompt="one")))
    second = threading.Thread(target=controller.run_request, args=("task-1", ModelRequest(prompt="two")))
    first.start()
    assert provider.first_entered.wait(timeout=1)
    second.start()
    try:
        assert not provider.second_entered.wait(timeout=0.1)
    finally:
        provider.release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)
    assert provider.max_in_flight == 1
    assert not first.is_alive() and not second.is_alive()
