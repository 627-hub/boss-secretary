"""飞书 ingress（长连接）：收消息 → AI 抽取 → 受理 → 审查 → 卡片审批回调 → 流程引擎。

设置（config/settings.yaml）：
  feishu.roles.boss/manager/finance = 审批人 open_id（卡片发给谁）
命令（私聊机器人）：
  直接发报销描述（"9月5号打车98块，滴滴发票"）
  "进度"                → 本人单据列表
  "撤回 T20260907-XXXX" → 撤回
运行: python3 -m boss_secretary.feishu run
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Mapping

import lark_oapi as lark
from lark_oapi.api.im.v1 import (CreateMessageRequest, CreateMessageRequestBody,
                                 ReplyMessageRequest, ReplyMessageRequestBody)

from boss_secretary.core import compliance as C
from boss_secretary.core import extract as E
from boss_secretary.core import llm as L
from boss_secretary.core import matrix as M
from boss_secretary.core import router as R
from boss_secretary.core.llm import load_settings

TICKET_ID_RE = re.compile(r"T\d{8}-[0-9A-F]{6}")


def approval_card(ticket_id: str, amount: Any, reason: Any, role: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": f"报销审批 {ticket_id}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": f"**金额**：{amount if amount is not None else '-'} 元\n"
                                               f"**事由**：{reason or '-'}\n"
                                               f"**审批角色**：{role}"}},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "同意"},
                 "type": "primary",
                 "value": {"action": "approve", "ticket_id": ticket_id, "role": role}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "驳回"},
                 "type": "danger",
                 "value": {"action": "reject", "ticket_id": ticket_id, "role": role}}],
             }]}


class SecretaryBot:
    def __init__(self, settings_path: str | None = None, *, settings: Mapping | None = None,
                 store=None, router_: R.Router | None = None):
        self.settings = settings or load_settings(settings_path)
        s = self.settings
        self.store = store or R.SQLiteTicketStore(
            s["storage"].get("db_path", "data/secretary.db"),
            audit_dir=s["storage"].get("audit_dir", "data/audit"))
        if router_ is not None:
            self.router = router_
        else:
            flow = R.load_flow(s.get("flow_config", "config/flows/reimburse.yaml"))
            mx = M.load(s["matrix"]["active"]["reimburse"])
            rules_cfg = C.load_config(s.get("rules_config", "config/rules.yaml"))
            role_map = (s.get("feishu") or {}).get("roles") or {}
            resolvers = {role: (lambda t, uid=uid: uid or None) for role, uid in role_map.items()}
            self.router = R.Router(flow, mx, self.store, rules_cfg,
                                   role_resolvers=resolvers)
        self.roles = (s.get("feishu") or {}).get("roles") or {}
        self.settings_path = settings_path

    # ── 飞书 API ──────────────────────────────────────────────
    def _client(self) -> lark.Client:
        if not hasattr(self, "_client_obj"):
            f = self.settings["feishu"]
            self._client_obj = (lark.Client.builder()
                                .app_id(f["app_id"]).app_secret(f["app_secret"]).build())
        return self._client_obj

    def send_text(self, open_id: str, text: str) -> None:
        req = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
            .request_body(CreateMessageRequestBody.builder()
                          .receive_id(open_id).msg_type("text")
                          .content(json.dumps({"text": text}, ensure_ascii=False)).build()) \
            .build()
        resp = self._client().im.v1.message.create(req)
        if not resp.success:
            print(f"[feishu] 发送失败 {resp.code}: {resp.msg}")

    def send_card(self, open_id: str, card: dict) -> None:
        req = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
            .request_body(CreateMessageRequestBody.builder()
                          .receive_id(open_id).msg_type("interactive")
                          .content(json.dumps(card, ensure_ascii=False)).build()) \
            .build()
        resp = self._client().im.v1.message.create(req)
        if not resp.success:
            print(f"[feishu] 卡片发送失败 {resp.code}: {resp.msg}")

    # ── 员工档案 ──────────────────────────────────────────────
    def get_or_create_employee(self, open_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT feishu_user_id, dept_id FROM employees WHERE feishu_user_id=?",
            (open_id,)).fetchone()
        if row:
            return {"user_id": row[0], "dept_id": row[1]}
        self.store.conn.execute(
            "INSERT INTO employees(feishu_user_id, role) VALUES(?, 'EMPLOYEE')",
            (open_id,))
        self.store.conn.commit()
        return {"user_id": open_id, "dept_id": None}

    # ── 消息处理 ──────────────────────────────────────────────
    def handle_text(self, sender_open_id: str, text: str) -> str:
        text = (text or "").strip()
        emp = self.get_or_create_employee(sender_open_id)
        m = TICKET_ID_RE.search(text)
        if m and ("撤回" in text or "作废" in text):
            try:
                self.router.withdraw(m.group(0), sender_open_id)
                return f"已撤回 {m.group(0)}"
            except R.RouterError as e:
                return f"撤回失败: {e}"
        if text in ("进度", "我的报销", "查进度"):
            return self._progress(sender_open_id)
        return self._submit(emp, text)

    def _progress(self, open_id: str) -> str:
        rows = self.store.conn.execute(
            "SELECT ticket_id, status, amount FROM tickets WHERE employee_id=?"
            " ORDER BY rowid DESC LIMIT 10", (open_id,)).fetchall()
        if not rows:
            return "暂无报销单。直接发我一句报销描述即可，例如：9月5号打车98块，滴滴出行发票"
        lines = [f"{tid}  {st}  {amt if amt is not None else '-'}元"
                 for tid, st, amt in rows]
        return "\n".join(["你的报销单："] + lines)

    def _submit(self, emp: Mapping, text: str) -> str:
        try:
            ctx = E.extract_ticket(text, settings=self.settings)
        except L.LLMError as e:
            return f"解析失败（AI 抽取）: {e}"
        missing = E.missing_required(ctx)
        if missing:
            name_map = {"amount": "金额", "occurred_at": "发生日期", "reason": "事由",
                        "expense_type": "费用类型", "invoice_no": "发票号"}
            return "请补充：" + "、".join(name_map.get(k, k) for k in missing)
        tid = self.router.create_ticket(ctx, emp)
        rule_results = C.run_rules(ctx, self.store.history(ctx))
        try:
            rv = E.review_with_llm(ctx, rule_results, settings=self.settings)
        except L.LLMError as e:
            rv = {"verdict": C.WARN, "confidence": 0.0,
                  "evidence": [f"LLM 审查不可用: {e}"], "suggestions": []}
        outcome = self.router.run_review(tid, llm_verdict=rv["verdict"])
        self._after_review(tid, outcome)
        status_s = outcome.next_status
        if status_s == R.AUTO_APPROVED:
            return f"✅ 已自动通过（{tid}，{ctx.get('amount')}元）。如有异议回复「撤回 {tid}」"
        if status_s == R.REJECTED:
            return f"❌ 单据 {tid} 未通过：{'; '.join(rv['suggestions']) or '见规则审查'}"
        pend = [r for r in outcome.approver_roles]
        return f"已受理 {tid}（{ctx.get('amount')}元），进入审批：{'、'.join(pend) or '-'}"

    def _after_review(self, ticket_id: str, outcome: R.ReviewOutcome) -> None:
        t = self.store.get(ticket_id) or {}
        for role in outcome.approver_roles:
            uid = (self.router.role_resolvers.get(role) or (lambda t_: None))(t)
            if not uid:
                self.send_text(t.get("employee_id", ""),
                               f"提示：审批角色 {role} 未在 settings.yaml feishu.roles 配置，"
                               f"单据 {ticket_id} 无法推送卡片")
                continue
            self.send_card(uid, approval_card(ticket_id, t.get("amount"),
                                              t.get("reason"), role))

    def on_card_action(self, open_id: str, value: Mapping) -> str:
        action = value.get("action")
        ticket_id = value.get("ticket_id")
        role = value.get("role")
        try:
            if action == "approve":
                st = self.router.approve(ticket_id, open_id, role)
                return f"已同意（当前：{st}）" if st != R.APPROVED else "✅ 会签完成，单据通过"
            if action == "reject":
                self.router.reject(ticket_id, open_id, role, "卡片驳回")
                return "已驳回"
            return f"未知动作: {action}"
        except R.RouterError as e:
            return f"操作失败: {e}"

    # ── 事件注册 ──────────────────────────────────────────────
    def build_event_handler(self) -> lark.EventDispatcherHandler:
        bot = self

        def on_message(data: lark.P2ImMessageReceiveV1) -> None:
            try:
                msg = data.event.message
                if msg.message_type != "text":
                    return
                sender = data.event.sender.sender_id.open_id
                text = json.loads(msg.content).get("text", "")
                print(f"[feishu] 收到消息 sender={sender} text={text[:50]!r}")
                reply = bot.handle_text(sender, text)
                bot.send_text(sender, reply)
            except Exception as e:
                print(f"[feishu] 消息处理异常: {type(e).__name__}: {e}")

        def on_card(data: lark.P2CardActionTrigger) -> None:
            try:
                operator = data.event.operator.open_id
                value = data.event.action.value or {}
                bot.send_text(operator, bot.on_card_action(operator, value))
            except Exception as e:
                print(f"[feishu] 卡片回调异常: {type(e).__name__}: {e}")

        return (lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(on_message)
                .register_p2_card_action_trigger(on_card)
                .build())

    def run(self) -> None:
        f = self.settings["feishu"]
        print(f"[feishu] 长连接启动… app_id={f['app_id']}")
        cli = lark.ws.Client(f["app_id"], f["app_secret"],
                             event_handler=self.build_event_handler(),
                             log_level=lark.LogLevel.WARNING)
        cli.start()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"
    settings_path = "config/settings.yaml"
    if len(argv) > 1 and not argv[1].startswith("-"):
        settings_path = argv[1]
    bot = SecretaryBot(settings_path)
    bot.run()
    return 0


import sys

if __name__ == "__main__":
    sys.exit(main())
