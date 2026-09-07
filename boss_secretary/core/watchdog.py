"""看门狗：独立于机器人进程的掉线告警。

原理：bot 的调度器每 tick 刷新心跳文件；本进程独立运行，检查心跳文件年龄，
超阈值 → 直接调用飞书 API（tenant_token + 发消息，不依赖 bot）向老板告警；
恢复后发「已恢复」。单次事故只告警一次（状态文件去重）。

启动（与 bot 分开跑，或交给 launchd/cron）:
  python3 -m boss_secretary.watchdog --interval 600 --max-age 3600
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import requests

from boss_secretary.core.llm import load_settings

DEFAULT_STATE = "data/watchdog_state.json"


class WatchdogError(Exception):
    pass


def _feishu_token(app_id: str, app_secret: str) -> str:
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret}, timeout=15)
    d = r.json()
    if d.get("code") != 0:
        raise WatchdogError(f"token 获取失败: {d.get('msg')}")
    return d["tenant_access_token"]


def send_message(token: str, receive_id: str, text: str) -> None:
    requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        headers={"Authorization": f"Bearer {token}"},
        json={"receive_id": receive_id, "msg_type": "text",
              "content": json.dumps({"text": text}, ensure_ascii=False)},
        timeout=15)


def check_stale(heartbeat_path: Path, max_age_minutes: int,
                now: dt.datetime | None = None) -> dict:
    """纯函数便于测试: {"stale": bool, "age_min": float, "ever_existed": bool}"""
    now = now or dt.datetime.now()
    p = Path(heartbeat_path)
    if not p.exists():
        return {"stale": True, "age_min": None, "ever_existed": False}
    try:
        ts = dt.datetime.fromisoformat(p.read_text(encoding="utf-8").strip())
        age = (now - ts).total_seconds() / 60
    except (ValueError, OSError):
        return {"stale": True, "age_min": None, "ever_existed": True}
    return {"stale": age > max_age_minutes, "age_min": round(age, 1),
            "ever_existed": True}


def run_loop(settings_path: str = "config/settings.yaml", interval: int = 600,
             max_age: int = 3600, state_path: str = DEFAULT_STATE,
             once: bool = False) -> None:
    settings = load_settings(settings_path)
    boss = ((settings.get("feishu") or {}).get("roles") or {}).get("boss")
    if not boss:
        raise WatchdogError("settings.feishu.roles.boss 未配置，看门狗无法告警")
    f = settings["feishu"]
    sp = Path(state_path)
    state = {"alerted": False}
    if sp.exists():
        try:
            state.update(json.loads(sp.read_text(encoding="utf-8")))
        except Exception:
            pass
    while True:
        try:
            r = check_stale("data/heartbeat", max_age_minutes=max_age / 60)
            if r["stale"] and not state["alerted"]:
                token = _feishu_token(f["app_id"], f["app_secret"])
                age_s = f"心跳缺失/已 {r['age_min']} 分钟未更新" \
                    if r["age_min"] is not None else "心跳文件不存在（bot 可能从未启动）"
                send_message(token, boss,
                             f"🚨 秘书掉线告警：{age_s}（阈值 "
                             f"{max_age // 60} 分钟）。请检查 bot 进程："
                             f"boss-feishu 重启")
                state["alerted"] = True
                sp.parent.mkdir(parents=True, exist_ok=True)
                sp.write_text(json.dumps(state), encoding="utf-8")
                print(f"[watchdog] {dt.datetime.now():%H:%M} 告警已发")
            elif not r["stale"] and state["alerted"]:
                token = _feishu_token(f["app_id"], f["app_secret"])
                send_message(token, boss, "✅ 秘书已恢复在线")
                state["alerted"] = False
                sp.write_text(json.dumps(state), encoding="utf-8")
                print(f"[watchdog] {dt.datetime.now():%H:%M} 恢复通知已发")
        except Exception as e:
            print(f"[watchdog] 异常: {type(e).__name__}: {e}")
        if once:
            break
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="boss_secretary.watchdog")
    p.add_argument("--settings", default="config/settings.yaml")
    p.add_argument("--interval", type=int, default=600, help="检查间隔秒")
    p.add_argument("--max-age", type=int, default=3600, help="心跳超时秒")
    p.add_argument("--once", action="store_true", help="只检查一次（测试/cron）")
    args = p.parse_args(argv)
    run_loop(args.settings, args.interval, args.max_age, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
