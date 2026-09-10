import json
import os
from pathlib import Path
import uuid


class ServiceLease:
    def __init__(self, directory: Path, instance_id: str | None = None):
        self.directory = directory.resolve()
        self.stream = None
        self.identifier = uuid.UUID(instance_id).hex if instance_id else uuid.uuid4().hex

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stream = (self.directory / "service.lock").open("a+b")
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError("Service is already running for this data directory") from None
        return self

    def publish(self):
        temporary = self.directory / "service-state.tmp"
        temporary.write_text(json.dumps({"pid": os.getpid(), "instance": self.identifier}), encoding="utf-8")
        temporary.replace(self.directory / "service-state.json")

    def stop_requested(self):
        return (self.directory / f"stop-{self.identifier}").exists()

    def __exit__(self, *args):
        try:
            (self.directory / "service-state.json").unlink(missing_ok=True)
            (self.directory / f"stop-{self.identifier}").unlink(missing_ok=True)
        finally:
            if self.stream:
                self.stream.close()
                self.stream = None


def service_running(directory: Path) -> bool:
    try:
        with ServiceLease(directory):
            return False
    except RuntimeError:
        return True


def request_stop(directory: Path) -> str:
    if not service_running(directory):
        return "not_running"
    try:
        state = json.loads((directory / "service-state.json").read_text(encoding="utf-8"))
        identifier = uuid.UUID(hex=state["instance"]).hex
    except (OSError, ValueError, KeyError):
        return "starting_retry"
    (directory / f"stop-{identifier}").touch()
    return "stop_requested"