"""定时任务调度（PRD F10/F17 落地）：日报/月度异常扫查/额度过期/超时升级。

内嵌于机器人进程（线程），每 60s tick；状态持久化 data/scheduler_state.json
（重启不重复触发）。时间均为本地时间。
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_STATE = "data/scheduler_state.json"


@dataclass
class Job:
    name: str
    kind: str                    # daily | monthly | hourly
    at: str = "09:00"            # daily/monthly 的 HH:MM
    day: int = 1                 # monthly 的几号
    fn: Callable[[], str] = None
    last_key: str = ""


class Scheduler:
    def __init__(self, jobs: list[Job], state_path: str | Path = DEFAULT_STATE,
                 heartbeat_path: str | Path = "data/heartbeat"):
        self.jobs = jobs
        self.state_path = Path(state_path)
        self.heartbeat_path = Path(heartbeat_path)
        self._load()

    def _load(self) -> None:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                for j in self.jobs:
                    j.last_key = data.get(j.name, "")
            except Exception:
                pass

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({j.name: j.last_key for j in self.jobs}, ensure_ascii=False),
            encoding="utf-8")

    def due(self, job: Job, now: dt.datetime) -> bool:
        if job.kind == "daily":
            key = now.strftime("%Y-%m-%d")
            return now.strftime("%H:%M") >= job.at and job.last_key != key
        if job.kind == "monthly":
            key = now.strftime("%Y-%m")
            return (now.day == job.day and now.strftime("%H:%M") >= job.at
                    and job.last_key != key)
        if job.kind == "hourly":
            key = now.strftime("%Y-%m-%d %H")
            return job.last_key != key
        return False

    def tick(self, now: dt.datetime | None = None) -> list[str]:
        now = now or dt.datetime.now()
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat_path.write_text(now.isoformat(timespec="seconds"),
                                           encoding="utf-8")
        except OSError:
            pass
        ran = []
        for job in self.jobs:
            if not self.due(job, now):
                continue
            try:
                msg = job.fn() or ""
                ran.append(f"{job.name}: {msg[:80]}")
            except Exception as e:
                ran.append(f"{job.name}: ERROR {type(e).__name__}: {e}")
            job.last_key = self._key(job, now)
            self._save()
        return ran

    def _key(self, job: Job, now: dt.datetime) -> str:
        if job.kind == "daily":
            return now.strftime("%Y-%m-%d")
        if job.kind == "monthly":
            return now.strftime("%Y-%m")
        return now.strftime("%Y-%m-%d %H")

    def loop_forever(self, interval: int = 60, stop: threading.Event | None = None):
        while not (stop and stop.is_set()):
            for line in self.tick():
                print(f"[scheduler] {line}")
            time.sleep(interval)


def start_background(scheduler: Scheduler, interval: int = 60) -> threading.Thread:
    t = threading.Thread(target=scheduler.loop_forever, kwargs={"interval": interval},
                         daemon=True)
    t.start()
    return t
