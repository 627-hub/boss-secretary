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

import base64
import datetime as dt
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import lark_oapi as lark
from lark_oapi.api.im.v1 import (CreateMessageRequest, CreateMessageRequestBody,
                                 GetMessageResourceRequest,
                                 ReplyMessageRequest, ReplyMessageRequestBody)

from boss_secretary.core import allowance as AL
from boss_secretary.core import budget as BG
from boss_secretary.core import compliance as C
from boss_secretary.core import invoice_verify as IV
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


def paid_card(ticket_id: str, amount: Any) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green",
                   "title": {"tag": "plain_text", "content": f"打款确认 {ticket_id}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": f"审批已通过，待打款 **{amount if amount is not None else '-'} 元**\n"
                                               f"确认后本单关闭并通知员工。"}},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "确认打款"},
                 "type": "primary",
                 "value": {"action": "paid", "ticket_id": ticket_id, "role": "finance"}}],
             }]}


class FeishuEventBridge:
    """把 router 事件翻译成飞书消息/卡片：提交→审批卡片；通过→打款卡片+告知员工；
    打款→关闭通知；驳回/撤回/作废/升级→状态告知。"""

    def __init__(self, bot: "SecretaryBot"):
        self.bot = bot

    def _resolve(self, role: str, ticket: Mapping) -> str | None:
        fn = self.bot.router.role_resolvers.get(role)
        return fn(ticket) if fn else self.bot.roles.get(role) or None

    def send(self, event: str, ticket: Mapping, to: Sequence[str]) -> None:
        tid = ticket.get("ticket_id")
        emp = ticket.get("employee_id")
        try:
            if event == "ticket.submitted":
                for role in ticket.get("approvers") or []:
                    uid = self._resolve(role, ticket)
                    if uid:
                        self.bot.send_card(uid, approval_card(
                            tid, ticket.get("amount"), ticket.get("reason"), role))
                    else:
                        self.bot.send_text(emp, f"提示：审批角色 {role} 未配置 open_id，"
                                                f"单据 {tid} 无法推送卡片")
            elif event == "ticket.approved":
                for uid in dict.fromkeys(list(to) + [self._resolve("finance", ticket) or ""]):
                    if uid:
                        self.bot.send_card(uid, paid_card(tid, ticket.get("amount")))
                if emp:
                    self.bot.send_text(emp, f"单据 {tid} 审批通过，待财务打款")
            elif event == "ticket.paid":
                if emp:
                    self.bot.send_text(emp, f"✅ 单据 {tid} 已打款，本单关闭")
            elif event == "ticket.auto_approved":
                for uid in dict.fromkeys([self._resolve("finance", ticket) or ""]):
                    if uid:
                        self.bot.send_card(uid, paid_card(tid, ticket.get("amount")))
                if emp:
                    self.bot.send_text(emp, f"单据 {tid} 已自动通过，待财务打款")
            elif event in ("ticket.rejected", "ticket.withdrawn", "ticket.cancelled",
                           "ticket.escalated"):
                if emp:
                    self.bot.send_text(emp, f"单据 {tid} 状态更新: "
                                            f"{event.removeprefix('ticket.')}")
        except Exception as e:
            print(f"[feishu] 事件桥异常 {event}: {type(e).__name__}: {e}")


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
                                   notifier=FeishuEventBridge(self),
                                   role_resolvers=resolvers)
        self.roles = (s.get("feishu") or {}).get("roles") or {}
        self._pending: dict[str, dict] = {}
        self._last_verify: dict[str, dict] = {}
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
        if not resp.success():
            print(f"[feishu] 发送失败 {resp.code}: {resp.msg} | 收件人 {open_id}")
        else:
            print(f"[feishu] 已回复 {open_id}: {text[:60]!r}")

    def send_card(self, open_id: str, card: dict) -> None:
        req = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
            .request_body(CreateMessageRequestBody.builder()
                          .receive_id(open_id).msg_type("interactive")
                          .content(json.dumps(card, ensure_ascii=False)).build()) \
            .build()
        resp = self._client().im.v1.message.create(req)
        if not resp.success():
            print(f"[feishu] 卡片发送失败 {resp.code}: {resp.msg}")

    def download_resource(self, file_key: str, message_id: str,
                          rtype: str) -> bytes | None:
        req = GetMessageResourceRequest.builder() \
            .message_id(message_id).file_key(file_key).type(rtype).build()
        resp = self._client().im.v1.message_resource.get(req)
        if not resp.success():
            print(f"[feishu] 资源下载失败({rtype}) {resp.code}: {resp.msg}")
            return None
        f = resp.file
        return f.read() if hasattr(f, "read") else f

    def download_image(self, image_key: str, message_id: str) -> bytes | None:
        return self.download_resource(image_key, message_id, "image")

    def download_file(self, file_key: str, message_id: str) -> bytes | None:
        return self.download_resource(file_key, message_id, "file")

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
    def handle_image(self, sender_open_id: str, image_key: str,
                     message_id: str) -> str:
        import base64 as b64mod
        data = self.download_image(image_key, message_id)
        if not data:
            return "图片下载失败，请重发"
        emp = self.get_or_create_employee(sender_open_id)
        try:
            inv = E.extract_invoice_image(b64mod.b64encode(data).decode(),
                                          settings=self.settings)
        except L.LLMError as e:
            print(f"[feishu] 发票图片识别失败: {e}")
            return f"发票识别失败（AI 视觉）: {str(e)[:120]}"
        self._last_verify[sender_open_id] = IV.verify(inv, image_bytes=data,
                                                      settings=self.settings)
        return self._merge_invoice(sender_open_id, emp, inv, "图片")

    def handle_file(self, sender_open_id: str, file_key: str,
                    message_id: str, filename: str) -> str:
        data = self.download_file(file_key, message_id)
        if not data:
            return "文件下载失败，请重发"
        emp = self.get_or_create_employee(sender_open_id)
        if not filename.lower().endswith(".pdf"):
            return f"暂只支持 PDF 电子发票（收到 {filename}），图片发票请直接发图"
        try:
            inv = E.extract_invoice_pdf(data, settings=self.settings)
        except L.LLMError as e:
            print(f"[feishu] PDF 发票识别失败: {e}")
            return f"PDF 发票解析失败: {str(e)[:120]}"
        self._last_verify[sender_open_id] = IV.verify(inv, image_bytes=None,
                                                      settings=self.settings)
        sig_note = ""
        if inv.get("e_signature") is True:
            sig_note = "（已检测到电子签章 ✓）"
        elif inv.get("e_signature") is False:
            sig_note = "（⚠ 未检测到电子签章，请人工核验）"
        return self._merge_invoice(sender_open_id, emp, inv, f"PDF{sig_note}")

    def _merge_invoice(self, sender_open_id: str, emp: Mapping, inv: Mapping,
                       source: str) -> str:
        inv = {k: v for k, v in inv.items() if v not in (None, "")}
        if not inv.get("invoice_no") and not inv.get("invoice_amount"):
            return f"{source}发票未识别出要素（号码/金额均空），请确认图片清晰后重发"
        pending = self._pending.get(sender_open_id)
        now = time.time()
        if pending and now - pending["ts"] > 600:
            pending = None
        base = pending["ctx"] if pending else {}
        ctx = {**base, **{k: v for k, v in inv.items() if k != "pdf_pages"},
               "currency": base.get("currency", "CNY")}
        if pending is None:
            if not ctx.get("amount"):
                ctx["amount"] = ctx.get("invoice_amount")
            if not ctx.get("occurred_at") and ctx.get("invoice_date"):
                ctx["occurred_at"] = ctx["invoice_date"]
        et = inv.get("expense_type")
        if et and not ctx.get("expense_type"):
            ctx["expense_type"] = et
        head = f"发票要素已识别（{source}）：号码 {ctx.get('invoice_no') or '-'}，" \
               f"金额 {ctx.get('invoice_amount') or '-'} 元"
        vr = self._last_verify.pop(sender_open_id, None)
        if vr:
            head += "\n── AI 形式验真 ──\n" + IV.to_text(vr)
        result = self._ingest(sender_open_id, emp, ctx, source)
        return head + "\n" + result if result.startswith(("请补充", "⚠")) else result

    def _finalize(self, sender_open_id: str, emp: Mapping, ctx: Mapping) -> str:
        b = BG.check(self.store.conn, emp.get("dept_id"),
                     (ctx.get("occurred_at") or dt.date.today().isoformat())[:7],
                     extra=float(ctx.get("amount") or 0), settings=self.settings)
        budget_warn = ""
        if b["checked"] and not b["ok"]:
            base = (f"⚠ 超预算：{b['dept']} {b['month']} 已用 {b['used']:.0f}/"
                    f"预算 {b['budget']:.0f}，本单 {ctx.get('amount')} 元")
            if b["block"]:
                return base + "，已拦截（settings.budgets.block_over）。请联系老板调整预算"
            budget_warn = base + "\n"
        tid = self.router.create_ticket(ctx, emp)
        print(f"[feishu] 已建单 {tid}")
        rule_results = C.run_rules(ctx, self.store.history(ctx))
        summary = C.summarize(rule_results)
        try:
            rv = E.review_with_llm(ctx, rule_results, settings=self.settings)
            print(f"[feishu] LLM 审查: {rv['verdict']} conf={rv['confidence']} "
                  f"| {rv['evidence'][:1]}")
        except L.LLMError as e:
            print(f"[feishu] LLM 审查不可用: {e}")
            rv = {"verdict": C.WARN, "confidence": 0.0,
                  "evidence": [f"LLM 审查不可用: {e}"], "suggestions": []}
        blocked = summary["overall"] == C.FAIL or rv["verdict"] == C.FAIL
        al, over = AL.match(self.store.conn, emp["user_id"], ctx)
        if al and over <= 0 and not blocked:
            self.router.allowance_auto_approve(tid, al["allowance_id"])
            rem = AL.consume(self.store.conn, al["allowance_id"],
                             float(ctx.get("amount") or 0))
            self._after_review(tid, R.ReviewOutcome(
                tuple(rule_results), summary, None, R.AUTO_APPROVED, (), "", ()))
            return budget_warn + (f"✅ 额度内核销（{al['allowance_id']}：{ctx.get('amount')}元，"
                    f"额度余 {rem} 元）。已提交财务打款；如有异议回复「撤回 {tid}」")
        outcome = self.router.run_review(tid, llm_verdict=rv["verdict"])
        self._after_review(tid, outcome)
        if outcome.next_status == R.AUTO_APPROVED:
            self._send_paid_card_if_finance(tid)
            return budget_warn + (f"✅ 已自动通过（{tid}，{ctx.get('amount')}元）。"
                    f"待财务打款；如有异议回复「撤回 {tid}」")
        if outcome.next_status == R.REJECTED:
            return f"❌ 单据 {tid} 未通过：{'; '.join(rv['suggestions']) or '见规则审查'}"
        return budget_warn + (f"已受理 {tid}（{ctx.get('amount')}元），"
                f"进入审批：{'、'.join(outcome.approver_roles) or '-'}")

    def _send_paid_card_if_finance(self, ticket_id: str) -> None:
        f_uid = self.roles.get("finance")
        if f_uid:
            t = self.store.get(ticket_id) or {}
            self.send_card(f_uid, paid_card(ticket_id, t.get("amount")))

    def _progress(self, open_id: str) -> str:
        rows = self.store.conn.execute(
            "SELECT ticket_id, status, amount FROM tickets WHERE employee_id=?"
            " ORDER BY rowid DESC LIMIT 10", (open_id,)).fetchall()
        if not rows:
            return "暂无报销单。直接发我一句报销描述即可，例如：9月5号打车98块，滴滴出行发票"
        lines = [f"{tid}  {st}  {amt if amt is not None else '-'}元"
                 for tid, st, amt in rows]
        return "\n".join(["你的报销单："] + lines)

    def _budget_command(self, sender_open_id: str, text: str) -> str:
        parsed = BG.parse_budget_text(text)
        if parsed is None:
            return "用法：`预算` 总览 | `预算 部门 2026-09 50000` 设置（全司用 *）"
        privileged = sender_open_id in (self.roles.get("boss"), self.roles.get("finance"))
        month = parsed.get("month") or dt.date.today().strftime("%Y-%m")
        if parsed["action"] == "set":
            if not privileged:
                return "仅老板/财务可设置预算"
            BG.set_budget(self.store.conn, parsed["dept"], parsed["month"],
                          parsed["amount"], created_by=sender_open_id)
            return f"预算已设置：{parsed['dept']} {parsed['month']} {parsed['amount']:.0f} 元"
        if parsed["action"] == "query":
            return BG.to_table(BG.overview(self.store.conn, month), month) \
                if parsed["dept"] == "*" else \
                BG.to_table([r for r in BG.overview(self.store.conn, month)
                             if r["dept"] == parsed["dept"]], month)
        return BG.to_table(BG.overview(self.store.conn, month), month)

    def _allowance_request(self, sender_open_id: str, emp: Mapping, text: str) -> str:
        parsed = AL.extract_allowance(text, settings=self.settings)
        if not parsed.get("amount"):
            return "请说明额度金额，例如：申请打车额度200元，今晚加班打车用"
        req = AL.create_request(self.store.conn, emp["user_id"],
                                parsed["category"], float(parsed["amount"]),
                                reason=parsed.get("reason", ""),
                                created_by=sender_open_id,
                                expense_types=parsed.get("expense_types", ()),
                                cfg=self.settings.get("allowances"))
        role = req["required_role"]
        uid = self.roles.get(role)
        if not uid:
            return f"额度申请 {req['allowance_id']} 已记录，但审批人 {role} 未配置 open_id"
        card = {"config": {"wide_screen_mode": True},
                "header": {"template": "purple",
                           "title": {"tag": "plain_text",
                                     "content": f"额度审批 {req['allowance_id']}"}},
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md",
                                            "content": f"**员工**：{emp['user_id']}\n"
                                                       f"**类型**：{req['category']}\n"
                                                       f"**额度**：{req['amount']} 元\n"
                                                       f"**用途**：{req['reason'] or '-'}\n"
                                                       f"**有效期**：{req['expires_at']}"}},
                    {"tag": "action", "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "批准"},
                         "type": "primary",
                         "value": {"action": "allowance_approve",
                                   "allowance_id": req["allowance_id"]}},
                        {"tag": "button", "text": {"tag": "plain_text", "content": "拒绝"},
                         "type": "danger",
                         "value": {"action": "allowance_reject",
                                   "allowance_id": req["allowance_id"]}}]}]}
        self.send_card(uid, card)
        return (f"额度申请已提交 {req['allowance_id']}（{req['category']} "
                f"{req['amount']} 元），等待 {role} 审批；生效后在此额度内报销免逐单审批")

    def _submit(self, sender_open_id: str, emp: Mapping, text: str) -> str:
        print(f"[feishu] 抽取开始 sender={sender_open_id} text={text[:40]!r}")
        try:
            ctx = E.extract_ticket(text, settings=self.settings)
        except L.LLMError as e:
            print(f"[feishu] 抽取失败: {e}")
            return f"解析失败（AI 抽取）: {str(e)[:120]}"
        print(f"[feishu] 抽取结果: {ctx}")
        return self._ingest(sender_open_id, emp, ctx, "文字")

    def _ingest(self, sender_open_id: str, emp: Mapping, ctx: Mapping,
                source: str) -> str:
        pending = self._pending.get(sender_open_id)
        now = time.time()
        if pending and now - pending["ts"] > 600:
            pending = None
        if pending:
            ctx = {**pending["ctx"], **{k: v for k, v in ctx.items() if v not in (None, "")}}
        missing = E.missing_required(ctx)
        if missing:
            self._pending[sender_open_id] = {"ctx": ctx, "ts": now}
            name_map = {"amount": "金额", "occurred_at": "发生日期", "reason": "事由",
                        "expense_type": "费用类型", "invoice_no": "发票号"}
            return "请补充：" + "、".join(name_map.get(k, k) for k in missing) + \
                   "（回复内容将自动并入；输入「取消」放弃）"
        self._pending.pop(sender_open_id, None)
        issues = E.cross_check_invoice(ctx)
        vr = self._last_verify.pop(sender_open_id, None)
        if vr:
            issues += [f"{i['rule']}: {i['detail']}" for i in vr["structural"] + vr["qr_issues"]
                       if i["level"] == IV.FAIL]
        if issues:
            self._pending[sender_open_id] = {"ctx": ctx, "ts": now}
            return "⚠ 验真与核验发现问题：" + "；".join(issues) + \
                   "\n如确认无误回复「按此提交」，或重新发送修正信息（「取消」放弃）"
        return self._finalize(sender_open_id, emp, ctx)

    def handle_text(self, sender_open_id: str, text: str) -> str:
        if (text or "").strip() == "按此提交":
            pending = self._pending.get(sender_open_id)
            if not pending:
                return "没有待提交的单据"
            self._pending.pop(sender_open_id, None)
            return self._finalize(sender_open_id,
                                  self.get_or_create_employee(sender_open_id),
                                  pending["ctx"])
        return self._route_command(sender_open_id, text)

    def _route_command(self, sender_open_id: str, text: str) -> str:
        text = (text or "").strip()
        if text.startswith("预算"):
            return self._budget_command(sender_open_id, text)
        emp = self.get_or_create_employee(sender_open_id)
        m = TICKET_ID_RE.search(text)
        if m and ("撤回" in text or "作废" in text):
            try:
                self.router.withdraw(m.group(0), sender_open_id)
                self._pending.pop(sender_open_id, None)
                return f"已撤回 {m.group(0)}"
            except R.RouterError as e:
                return f"撤回失败: {e}"
        if m and ("打款" in text):
            tid = m.group(0)
            if self.roles.get("finance") and sender_open_id != self.roles["finance"]:
                return "仅财务可确认打款"
            try:
                self.router.mark_paid(tid, sender_open_id, "finance")
                return f"✅ {tid} 已确认打款，单据关闭"
            except R.RouterError as e:
                return f"打款确认失败: {e}"
        if text in ("进度", "我的报销", "查进度"):
            return self._progress(sender_open_id)
        if text in ("额度", "我的额度"):
            return AL.to_table(AL.list_for(self.store.conn, sender_open_id))
        if any(k in text for k in ("额度", "备用金", "预算")):
            return self._allowance_request(sender_open_id, emp, text)
        if text in ("取消", "不报了"):
            self._pending.pop(sender_open_id, None)
            return "已放弃当前待补单据"
        return self._submit(sender_open_id, emp, text)

    def _after_review(self, ticket_id: str, outcome: R.ReviewOutcome) -> None:
        t = self.store.get(ticket_id) or {}
        for role in outcome.approver_roles:
            uid = (self.router.role_resolvers.get(role) or (lambda t_: None))(t) \
                or self.roles.get(role)
            if not uid:
                self.send_text(t.get("employee_id", ""),
                               f"提示：审批角色 {role} 未在 settings.yaml feishu.roles 配置，"
                               f"单据 {ticket_id} 无法推送卡片")

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
            if action == "allowance_approve":
                a_pre = AL.get(self.store.conn, value.get("allowance_id"))
                if a_pre:
                    emp_dept = self.store.conn.execute(
                        "SELECT dept_id FROM employees WHERE feishu_user_id=?",
                        (a_pre["employee_id"],)).fetchone()
                    b = BG.check(self.store.conn, emp_dept[0] if emp_dept else None,
                                 dt.date.today().strftime("%Y-%m"),
                                 extra=float(a_pre["total_amount"]),
                                 settings=self.settings)
                    if b["checked"] and not b["ok"] and b["block"]:
                        return (f"额度批准被预算检查拦截：{b['dept']} {b['month']} "
                                f"剩余 {b['remaining']:.0f} 元，本额度 {a_pre['total_amount']:.0f} 元。"
                                f"请先调整预算（预算 {b['dept']} {b['month']} 金额）")
            if action in ("allowance_approve", "allowance_reject"):
                a = AL.decide(self.store.conn, value.get("allowance_id"),
                              open_id, approve=(action == "allowance_approve"))
                if a is None:
                    return "额度申请不存在或已处理"
                if a["status"] == AL.ACTIVE:
                    self.send_text(a["employee_id"],
                                   f"✅ 额度已生效 {a['allowance_id']}：{a['category']} "
                                   f"{a['total_amount']} 元（至 {str(a['expires_at'])[:10]}）。"
                                   f"在此额度内报销免逐单审批")
                    return f"已批准 {a['allowance_id']}"
                self.send_text(a["employee_id"],
                               f"额度申请 {a['allowance_id']} 未获批准")
                return "已拒绝"
            if action == "paid":
                if self.roles.get("finance") and open_id != self.roles["finance"]:
                    return "仅财务可确认打款"
                self.router.mark_paid(ticket_id, open_id, "finance")
                return "✅ 已确认打款，单据关闭"
            return f"未知动作: {action}"
        except R.RouterError as e:
            return f"操作失败: {e}"

    # ── 事件注册 ──────────────────────────────────────────────
    def build_event_handler(self) -> lark.EventDispatcherHandler:
        bot = self
        seen_events: dict[str, float] = {}

        def _dedup(event_id: str) -> bool:
            now = time.time()
            for k in [k for k, ts in seen_events.items() if now - ts > 300]:
                seen_events.pop(k, None)
            if event_id and event_id in seen_events:
                return True
            if event_id:
                seen_events[event_id] = now
            return False

        def _process_message(sender: str, text: str) -> None:
            try:
                reply = bot.handle_text(sender, text)
                bot.send_text(sender, reply)
            except Exception as e:
                print(f"[feishu] 消息处理异常: {type(e).__name__}: {e}")
                try:
                    bot.send_text(sender, f"处理出错: {type(e).__name__}，请稍后重试")
                except Exception:
                    pass

        def on_message(data: lark.P2ImMessageReceiveV1) -> None:
            try:
                header = data.header
                if _dedup(getattr(header, "event_id", "")):
                    print("[feishu] 重投事件已去重")
                    return
                msg = data.event.message
                sender = data.event.sender.sender_id.open_id
                mtype = msg.message_type
                content = json.loads(msg.content) if msg.content else {}
                if mtype == "text":
                    text = content.get("text", "")
                    print(f"[feishu] 收到消息 sender={sender} text={text[:50]!r}")
                    threading.Thread(target=_process_message, args=(sender, text),
                                     daemon=True).start()
                elif mtype == "image":
                    print(f"[feishu] 收到图片 sender={sender}")
                    threading.Thread(
                        target=lambda: _process_image(sender, content.get("image_key"),
                                                      msg.message_id),
                        daemon=True).start()
                elif mtype == "file":
                    print(f"[feishu] 收到文件 sender={sender} {content.get('filename')}")
                    threading.Thread(
                        target=lambda: _process_file(sender, content.get("file_key"),
                                                     msg.message_id,
                                                     content.get("filename", "")),
                        daemon=True).start()
                elif mtype == "post":
                    threading.Thread(
                        target=lambda: _process_post(sender, content), daemon=True
                    ).start()
            except Exception as e:
                print(f"[feishu] 消息处理异常: {type(e).__name__}: {e}")

        def _process_image(sender: str, image_key: str | None, message_id: str) -> None:
            try:
                if not image_key:
                    return
                bot.send_text(sender, bot.handle_image(sender, image_key, message_id))
            except Exception as e:
                print(f"[feishu] 图片处理异常: {type(e).__name__}: {e}")

        def _process_file(sender: str, file_key: str | None, message_id: str,
                          filename: str) -> None:
            try:
                if not file_key:
                    return
                bot.send_text(sender, bot.handle_file(sender, file_key, message_id,
                                                      filename))
            except Exception as e:
                print(f"[feishu] 文件处理异常: {type(e).__name__}: {e}")

        def _process_post(sender: str, content: Mapping) -> None:
            try:
                import re as _re
                texts = _re.findall(r'"text"\s*:\s*"([^"]+)"', json.dumps(content,
                                                                          ensure_ascii=False))
                if texts:
                    bot.send_text(sender, bot.handle_text(sender, " ".join(texts)))
            except Exception as e:
                print(f"[feishu] 富文本处理异常: {type(e).__name__}: {e}")

        def _process_card(operator: str, value: Mapping) -> None:
            try:
                bot.send_text(operator, bot.on_card_action(operator, value))
            except Exception as e:
                print(f"[feishu] 卡片回调异常: {type(e).__name__}: {e}")

        def on_card(data: lark.P2CardActionTrigger) -> None:
            try:
                header = data.header
                if _dedup(getattr(header, "event_id", "")):
                    return
                operator = data.event.operator.open_id
                value = data.event.action.value or {}
                print(f"[feishu] 卡片回调 operator={operator} value={value}")
                threading.Thread(target=_process_card, args=(operator, dict(value)),
                                 daemon=True).start()
            except Exception as e:
                print(f"[feishu] 卡片回调异常: {type(e).__name__}: {e}")

        return (lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(on_message)
                .register_p2_card_action_trigger(on_card)
                .build())

    def _job_daily_report(self) -> str:
        from boss_secretary.report import daily as D

        class TextNotifier:
            def __init__(self, b):
                self.bot = b

            def send(self, event, ticket, to):
                for uid in to:
                    if uid:
                        self.bot.send_text(uid, ticket.get("body") or event)

        finance = [self.roles["finance"]] if self.roles.get("finance") else []
        D.send_all(self.store, TextNotifier(self), boss_user_id=self.roles.get("boss"),
                   finance_user_ids=finance)
        return "日报已发送"

    def _job_anomaly(self) -> str:
        from boss_secretary.core import anomaly as A
        events = A.sweep(self.store, cfg_path="config/anomaly.yaml")
        bad = [e for e in events if e.severity in (A.WARN, A.ALERT)]
        boss = self.roles.get("boss")
        if boss:
            head = f"月度异常扫查（{A.last_completed_period(dt.date.today())}）"
            self.send_text(boss, head + "\n" + (A.to_table(bad) if bad else "无 WARN/ALERT 事件"))
        return f"{len(events)} 事件"

    def _job_expire(self) -> str:
        return f"{AL.expire_sweep(self.store.conn)} 个额度过期"

    def _job_timeouts(self) -> str:
        return f"{len(self.router.check_timeouts())} 单超时升级"

    def run(self) -> None:
        from boss_secretary.core.scheduler import Job, Scheduler, start_background
        jobs = [
            Job("daily_report", "daily",
                at=(self.settings.get("daily_report") or {}).get("time", "18:00"),
                fn=self._job_daily_report),
            Job("anomaly_monthly", "monthly", at="09:00", day=1, fn=self._job_anomaly),
            Job("allowance_expire", "daily", at="08:00", fn=self._job_expire),
            Job("timeout_check", "hourly", fn=self._job_timeouts),
        ]
        self.scheduler = Scheduler(jobs)
        start_background(self.scheduler)
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
