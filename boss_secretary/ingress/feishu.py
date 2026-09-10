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
import threading
import time
import uuid
from pathlib import Path
from typing import Mapping, Sequence

import lark_oapi as lark
from lark_oapi.api.im.v1 import (CreateMessageRequest, CreateMessageRequestBody,
                                 GetMessageResourceRequest)

from boss_secretary.core import allowance as AL
from boss_secretary.core import approvals as AP
from boss_secretary.core import budget as BG
from boss_secretary.core.cards import (approval_card, contract_review_card,
                                       paid_card)
from boss_secretary.core import compliance as C
from boss_secretary.core import contract as CT
from boss_secretary.core import travel as TR
from boss_secretary.core import invoice_verify as IV
from boss_secretary.core import extract as E
from boss_secretary.core import specs as SP
from boss_secretary.core import supplier as SUP
from boss_secretary.core import llm as L
from boss_secretary.core import matrix as M
from boss_secretary.core import notify
from boss_secretary.core import router as R
from boss_secretary.ingress import actions as ACTIONS
from boss_secretary.ingress.commands import TICKET_ID_RE, route as route_command
from boss_secretary.core.llm import load_settings
# TICKET_ID_RE re-exported for tests/back-compat (commands 内定义)


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
                hist = AP.history(self.bot.store.conn, "ticket", tid)
                for role in ticket.get("approvers") or []:
                    uid = self._resolve(role, ticket)
                    if uid:
                        notify.send_card(self.bot, uid, approval_card(
                            tid, ticket.get("amount"), ticket.get("reason"), role,
                            type_=ticket.get("type") or "reimburse", history=hist))
                    else:
                        notify.send_text(self.bot, emp, f"提示：审批角色 {role} 未配置 open_id，"
                                                f"单据 {tid} 无法推送卡片")
            elif event == "ticket.approval_progress":
                hist = AP.history(self.bot.store.conn, "ticket", tid)
                done = {a.get("role") for a in ticket.get("approvals") or []}
                for role in ticket.get("approvers") or []:
                    if role in done:
                        continue
                    uid = self._resolve(role, ticket)
                    if uid:
                        notify.send_card(self.bot, uid, approval_card(
                            tid, ticket.get("amount"), ticket.get("reason"), role,
                            type_=ticket.get("type") or "reimburse", history=hist))
            elif event == "ticket.approved":
                hist = AP.history(self.bot.store.conn, "ticket", tid)
                for uid in dict.fromkeys(list(to) + [self._resolve("finance", ticket) or ""]):
                    if uid:
                        notify.send_card(self.bot, uid, paid_card(tid, ticket.get("amount"),
                                                          history=hist))
                if emp:
                    notify.send_text(self.bot, emp, f"单据 {tid} 审批通过，待财务打款")
            elif event == "ticket.paid":
                if emp:
                    notify.send_text(self.bot, emp, f"✅ 单据 {tid} 已打款，本单关闭")
            elif event == "ticket.auto_approved":
                for uid in dict.fromkeys([self._resolve("finance", ticket) or ""]):
                    if uid:
                        notify.send_card(self.bot, uid, paid_card(tid, ticket.get("amount")))
                if emp:
                    notify.send_text(self.bot, emp, f"单据 {tid} 已自动通过，待财务打款")
            elif event == "ticket.rejected":
                hist = AP.history(self.bot.store.conn, "ticket", tid)
                rj = AP.latest_reject(hist)
                if rj:
                    msg = f"❌ 单据 {tid} 被{AP.role_label(rj.get('role'))}驳回"
                    if rj.get("comment"):
                        msg += f"：{rj['comment']}"
                else:
                    msg = f"❌ 单据 {tid} 未通过（规则/矩阵自动）"
                for uid in dict.fromkeys([*to, emp or ""]):
                    if uid:
                        notify.send_text(self.bot, uid, msg)
            elif event in ("ticket.withdrawn", "ticket.cancelled", "ticket.escalated"):
                if emp:
                    notify.send_text(self.bot, emp, f"单据 {tid} 状态更新: "
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
            mx_p = M.load(s["matrix"].get("active_procure",
                                          "config/matrix/procure_v1.yaml"))
            rules_cfg = C.load_config(s.get("rules_config", "config/rules.yaml"))
            role_map = (s.get("feishu") or {}).get("roles") or {}
            resolvers = {role: (lambda t, uid=uid: uid or None) for role, uid in role_map.items()}
            self.router = R.Router(flow, mx, self.store, rules_cfg,
                                   notifier=FeishuEventBridge(self),
                                   role_resolvers=resolvers)
            self.router_p = R.Router(flow, mx_p, self.store, rules_cfg,
                                     notifier=FeishuEventBridge(self),
                                     role_resolvers=resolvers)
        self.roles = (s.get("feishu") or {}).get("roles") or {}
        self._pending: dict[str, dict] = {}
        self._last_verify: dict[str, dict] = {}
        self._last_b64: dict[str, str] = {}
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

    def upload_file(self, path: str, file_type: str = "docx") -> str | None:
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
        fp = Path(path)
        if not fp.exists():
            return None
        ext = fp.suffix.lstrip(".") or "doc"
        req = CreateFileRequest.builder().request_body(
            CreateFileRequestBody.builder()
            .file_type(file_type if file_type in ("opus", "mp4", "pdf", "doc",
                                                  "docx", "xls", "ppt", "pptx")
                       else "doc")
            .file_name(fp.name).file(fp.read_bytes()).build()).build()
        resp = self._client().im.v1.file.create(req)
        if not resp.success():
            print(f"[feishu] 文件上传失败 {resp.code}: {resp.msg}")
            return None
        return resp.file_key

    def send_file(self, open_id: str, path: str, file_type: str = "docx") -> None:
        key = self.upload_file(path, file_type)
        if not key:
            return
        req = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
            .request_body(CreateMessageRequestBody.builder()
                          .receive_id(open_id).msg_type("file")
                          .content(json.dumps({"file_key": key},
                                              ensure_ascii=False)).build()) \
            .build()
        resp = self._client().im.v1.message.create(req)
        if not resp.success():
            print(f"[feishu] 文件消息发送失败 {resp.code}: {resp.msg}")
        else:
            print(f"[feishu] 文件已发送 {open_id}: {Path(path).name}")

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
        b64 = b64mod.b64encode(data).decode()
        self._last_b64[sender_open_id] = b64
        return self.handle_image_bytes(sender_open_id, data)

    def handle_image_bytes(self, sender_open_id: str, data: bytes) -> str:
        emp = self.get_or_create_employee(sender_open_id)
        pend = self._pending.get(sender_open_id)
        if pend and pend.get("kind") == "procurement":
            path = self._save_attachment(data, "报价单图片.jpg", sender_open_id)
            pend["ctx"].setdefault("attachments", []).append(path)
            return self._finalize_procurement(sender_open_id, emp, pend["ctx"])
        if pend and pend.get("kind") == "contract":
            path = self._save_attachment(data, "合同图片.jpg", sender_open_id)
            pend["ctx"].setdefault("attachments", []).append(path)
            return (f"合同图片已存档（{path}）。图片合同暂仅做要素审查——"
                    "如需 AI 全文审查请上传 PDF 版；重新发「登记合同 …」+ 本文件可重新提交"
                    ) if False else self._finalize_contract(
                sender_open_id, emp, pend["ctx"],
                full_text=None, source="要素（附图片合同存档）")
        try:
            b64 = getattr(self, "_last_b64", {}).pop(sender_open_id, None) or \
                __import__("base64").b64encode(data).decode()
            inv = E.extract_invoice_image(b64, settings=self.settings)
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
        return self.handle_file_bytes(sender_open_id, data, filename)

    def handle_file_bytes(self, sender_open_id: str, data: bytes,
                          filename: str) -> str:
        emp = self.get_or_create_employee(sender_open_id)
        pend = self._pending.get(sender_open_id)
        if pend and pend.get("kind") == "procurement":
            path = self._save_attachment(data, filename or "附件", sender_open_id)
            pend["ctx"].setdefault("attachments", []).append(path)
            return self._finalize_procurement(sender_open_id, emp, pend["ctx"])
        if pend and pend.get("kind") == "contract":
            path = self._save_attachment(data, filename or "合同文件", sender_open_id)
            pend["ctx"].setdefault("attachments", []).append(path)
            if filename.lower().endswith(".pdf"):
                try:
                    from pypdf import PdfReader
                    import io as _io
                    reader = PdfReader(_io.BytesIO(data))
                    full_text = "\n".join((pg.extract_text() or "")
                                          for pg in reader.pages)
                except Exception as e:
                    print(f"[feishu] 合同 PDF 文本提取失败: {e}")
                    full_text = None
                return self._finalize_contract(sender_open_id, emp, pend["ctx"],
                                               full_text=full_text, source="合同文件全文")
            return (f"已存档 {filename}（非 PDF，无法全文提取）。"
                    "以要素审查提交可回复「按此提交」，或上传 PDF 版重新审查")
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

    def _save_attachment(self, data: bytes, filename: str, sender: str) -> str:
        d = Path("data/attachments")
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", filename or "附件")
        fp = d / f"{dt.date.today():%Y%m%d}_{uuid.uuid4().hex[:4]}_{safe}"
        fp.write_bytes(data)
        return str(fp)

    def _finalize_procurement(self, sender_open_id: str, emp: Mapping,
                              ctx: Mapping) -> str:
        spec = SP.get_spec("procurement")
        b = BG.check(self.store.conn, emp.get("dept_id"),
                     dt.date.today().strftime("%Y-%m"),
                     extra=float(ctx.get("amount") or 0), settings=self.settings)
        budget_warn = ""
        if spec.budget_check and b["checked"] and not b["ok"]:
            base = (f"⚠ 超预算：{b['dept']} {b['month']} 已用 {b['used']:.0f}/"
                    f"预算 {b['budget']:.0f}，本单 {ctx.get('amount')} 元")
            if b["block"]:
                return base + "，已拦截。请联系老板调整预算"
            budget_warn = base + "\n"
        rule_results = C.run_rules({"amount": ctx["amount"],
                                    "expense_type": ctx.get("ptype")},
                                   self.store.history({"occurred_at":
                                                        dt.date.today().isoformat()}))
        summary = C.summarize(rule_results)
        try:
            rv = E.review_with_llm({"title": ctx["title"], "amount": ctx["amount"],
                                    "expense_type": ctx.get("ptype"),
                                    "reason": ctx.get("reason")},
                                   rule_results, settings=self.settings)
            print(f"[feishu] LLM 审查: {rv['verdict']}")
        except L.LLMError as e:
            rv = {"verdict": C.WARN, "confidence": 0.0,
                  "evidence": [f"LLM 审查不可用: {e}"], "suggestions": []}
        mctx = {"ptype": ctx.get("ptype"), "amount": ctx["amount"],
                "rule_result": summary["overall"], "llm_verdict": rv["verdict"]}
        try:
            decision = M.evaluate(self.router_p.matrix, mctx)
            action = decision.action
        except M.NoMatchError:
            action = "MANUAL_REVIEW"
        tid = self.router_p.create_ticket(ctx, emp, type_override="procurement")
        print(f"[feishu] 采购建单 {tid} → {action}")
        outcome = self.router_p.run_review(tid, llm_verdict=rv["verdict"])
        self._pending.pop(sender_open_id, None)
        if outcome.next_status == R.SUBMITTED or outcome.next_status == R.ESCALATED:
            return budget_warn + (f"采购单已受理 {tid}（{ctx['amount']}元，附件 "
                    f"{len(ctx.get('attachments') or [])} 个），进入审批："
                    f"{'、'.join(outcome.approver_roles) or '-'}；"
                    f"通过后可发「登记合同 XX合同 供应商 金额 起止日期 付款条款」")
        if outcome.next_status == R.AUTO_APPROVED:
            return budget_warn + f"✅ 采购单 {tid} 已通过（{ctx['amount']}元）"
        return f"采购单状态: {outcome.next_status}"

    def _finalize_contract(self, sender_open_id: str, emp: Mapping,
                           ctx: Mapping, full_text: str | None = None,
                           source: str = "要素") -> str:
        spec = SP.get_spec("contract")
        b = BG.check(self.store.conn, emp.get("dept_id"),
                     dt.date.today().strftime("%Y-%m"),
                     extra=float(ctx.get("amount") or 0), settings=self.settings)
        budget_warn = ""
        if spec.budget_check and b["checked"] and not b["ok"]:
            base = (f"⚠ 超预算：{b['dept']} {b['month']} 已用 {b['used']:.0f}/"
                    f"预算 {b['budget']:.0f}，合同 {ctx.get('amount')} 元")
            if b["block"]:
                return base + "，已拦截。请联系老板调整预算"
            budget_warn = base + "\n"
        points = self.settings.get("legal", {}).get("review_points") or []
        review_text = full_text or " ".join(str(v) for k, v in ctx.items()
                                            if k != "attachments" and v)
        try:
            rv = E.review_contract(review_text, points, settings=self.settings)
        except L.LLMError as e:
            rv = {"verdict": C.WARN, "risks": [], "missing": [f"AI 审查不可用: {e}"]}
        print(f"[feishu] 合同 AI 审查: {rv['verdict']} risks={len(rv['risks'])}")
        rv["review_source"] = source
        cid = CT.create(self.store.conn, employee_id=emp["user_id"],
                        dept_id=emp.get("dept_id"), title=ctx["title"],
                        supplier=ctx.get("supplier"), amount=ctx.get("amount"),
                        start_date=ctx.get("start_date"), end_date=ctx.get("end_date"),
                        payment_terms=ctx.get("payment_terms"), ai_review=rv,
                        evidence_file=(ctx.get("attachments") or [None])[-1])
        self._pending.pop(sender_open_id, None)
        for role in ("legal", "boss"):
            uid = self.roles.get(role)
            if uid:
                self.send_card(uid, contract_review_card(
                    cid, ctx["title"], ctx.get("supplier"), ctx.get("amount"),
                    rv["risks"] + [{"level": "提示", "clause": "缺失条款",
                                    "note": "、".join(rv["missing"])}]))
            else:
                self.send_text(sender_open_id,
                               f"提示：审批角色 {role} 未配置 open_id，合同 {cid} 无法推送")
        miss = f"；缺失条款：{'、'.join(rv['missing'])}" if rv.get("missing") else ""
        return budget_warn + (f"合同已提交 {cid}（AI 审查：{rv['verdict']}，"
                              f"风险 {len(rv['risks'])} 项{miss}，"
                              f"审查依据：{source}），等待 legal+boss 会签生效")

    def _supplier_gate(self, name: str | None) -> str | None:
        """三道拦截闸: 返回 None=放行; 返回字符串=拦截原因。报销场景用 WARN 放行。"""
        if not name:
            return None
        r = SUP.check_name(self.store.conn, name)
        if r["level"] == "FAIL":
            return f"⛔ {r['detail']}——已拦截"
        if r["level"] == "WARN":
            return f"⚠ {r['detail']}"
        return None

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
        trip = TR.trip_for(self.store.conn, emp.get("user_id"),
                           ctx.get("occurred_at") or "")
        trip_note = ""
        if trip:
            ctx["trip_id"] = trip["trip_id"]
            over = (float(ctx.get("amount") or 0) -
                    float(trip.get("estimate") or 0))
            if over > float(trip.get("estimate") or 0) * 0.2:
                issues.append(f"报销 {ctx.get('amount')} 元超出差预估 "
                              f"{trip.get('estimate')} 元 20%+（出差 {trip['trip_id']}）")
            else:
                ctx["trip_note"] = f"已关联出差 {trip['trip_id']}"
                trip_note = f"（已关联出差 {trip['trip_id']}）"
        seller_gate = self._supplier_gate(ctx.get("invoice_seller"))
        if seller_gate and seller_gate.startswith("⛔"):
            issues.append(seller_gate.replace("⛔ ", ""))
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
            emp = self.get_or_create_employee(sender_open_id)
            if pending.get("kind") == "contract":
                return self._finalize_contract(sender_open_id, emp, pending["ctx"],
                                               source="要素")
            if pending.get("kind") == "procurement":
                return self._finalize_procurement(sender_open_id, emp, pending["ctx"])
            return self._finalize(sender_open_id, emp, pending["ctx"])
        return self._route_command(sender_open_id, text)

    def _route_command(self, sender_open_id: str, text: str) -> str:
        result = route_command(self, sender_open_id, text)
        if result is not None:
            return result
        emp = self.get_or_create_employee(sender_open_id)
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

    def on_card_action(self, open_id: str, value: Mapping,
                       comment: str = "") -> str:
        return ACTIONS.dispatch(self, open_id, value, comment)

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

        def _process_card(operator: str, value: Mapping, comment: str = "") -> None:
            try:
                bot.send_text(operator, bot.on_card_action(operator, value, comment))
            except Exception as e:
                print(f"[feishu] 卡片回调异常: {type(e).__name__}: {e}")

        def on_card(data: lark.P2CardActionTrigger) -> None:
            try:
                header = data.header
                if _dedup(getattr(header, "event_id", "")):
                    return
                operator = data.event.operator.open_id
                value = data.event.action.value or {}
                comment = AP.parse_comment(
                    getattr(data.event.action, "form_value", None))
                print(f"[feishu] 卡片回调 operator={operator} value={value} "
                      f"comment={comment[:50]!r}")
                threading.Thread(target=_process_card,
                                 args=(operator, dict(value), comment),
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
                        notify.send_text(self.bot, uid, ticket.get("body") or event)

        finance = [self.roles["finance"]] if self.roles.get("finance") else []
        D.send_all(self.store, TextNotifier(self), boss_user_id=self.roles.get("boss"),
                   finance_user_ids=finance)
        return "日报已发送"

    def _job_anomaly(self) -> str:
        from boss_secretary.core import anomaly as A
        events = A.sweep(self.store, cfg_path="config/anomaly.yaml")
        bad = [e for e in events if e.severity in (A.WARN, A.ALERT)]
        head = f"月度异常扫查（{A.last_completed_period(dt.date.today())}）"
        body = head + "\n" + (A.to_table(bad) if bad else "无 WARN/ALERT 事件")
        for uid in filter(None, (self.roles.get("boss"), self.roles.get("audit"))):
            self.send_text(uid, body)
        return f"{len(events)} 事件"

    def _job_monthly_report(self) -> str:
        from boss_secretary.report import monthly as MO
        month = MO.last_completed_month()
        result = MO.generate(self.store.conn, month=month, fmt="both")
        boss = self.roles.get("boss")
        if boss:
            self.send_text(boss, f"📊 月报已生成（{month}）: "
                                 f"{result.get('docx', '')}\n{result.get('pptx', '')}")
            for key in ("docx", "pptx"):
                if result.get(key):
                    ft = "docx" if key == "docx" else "pptx"
                    self.send_file(boss, result[key], file_type=ft)
        return f"月报 {month} 已生成并发送"

    def _job_loan_overdue(self) -> str:
        od = TR.overdue_loans(self.store.conn, days=60)
        if not od:
            return "无逾期未结借款"
        for l in od:
            for uid in filter(None, (self.roles.get("boss"),
                                     self.roles.get("finance"), l.get("employee_id"))):
                self.send_text(uid, f"⚠ 借款未结提醒：{l['loan_id']} "
                                    f"{l['employee_id']} 借 {l['amount']:.0f} 元"
                                    f"（余 {TR.remaining(l):.0f}，已 {l['overdue_days']} 天），"
                                    f"报销冲销回复「核销借款 {l['loan_id']} 金额」")
        return f"{len(od)} 笔逾期提醒"

    def _job_backup(self) -> str:
        from boss_secretary.core import backup as BK
        b = self.settings.get("backup") or {}
        r = BK.backup(self.settings.get("storage", {}).get("db_path",
                      "data/secretary.db"), out_dir="data/backups",
                      keep=int(b.get("keep", 14)), dest_dir=b.get("dest_dir") or None,
                      items=b.get("items") or ["data/audit", "data/attachments",
                                               "data/reports"])
        return f"备份 {r['size'] // 1024} KB，保留 {r['kept']} 份"

    def _job_contract_expiry(self) -> str:
        days = int((self.settings.get("contracts") or {}).get("expiry_warn_days", 30))
        expiring = CT.expiring(self.store.conn, days=days)
        if not expiring:
            return "无临期合同"
        for c in expiring:
            msg = (f"⚠ 合同临期：{c['contract_id']} {c['title']} "
                   f"（{c.get('supplier') or '-'}）将于 {str(c.get('end_date'))[:10]} "
                   f"到期（剩 {c['days_left']} 天），已付 "
                   f"{CT.paid_total(self.store.conn, c['contract_id']):.0f}/"
                   f"{c.get('amount') or 0:.0f} 元。续签回复：续签 {c['contract_id']} 新结束日期")
            for uid in filter(None, (self.roles.get("boss"), c.get("employee_id"))):
                self.send_text(uid, msg)
        return f"{len(expiring)} 份临期合同提醒"

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
            Job("monthly_report", "monthly", at="09:10", day=1, fn=self._job_monthly_report),
            Job("allowance_expire", "daily", at="08:00", fn=self._job_expire),
            Job("timeout_check", "hourly", fn=self._job_timeouts),
            Job("contract_expiry", "daily", at="08:30", fn=self._job_contract_expiry),
            Job("loan_overdue", "monthly", at="09:20", day=1, fn=self._job_loan_overdue),
            Job("backup", "daily", at="03:00", fn=self._job_backup),
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
