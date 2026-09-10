"""私聊命令路由（注册表）：按注册顺序匹配，先到先得；未命中交回 _submit。

每个命令一个函数（bot, open_id, text）；业务实现只依赖 bot 的公开接口
（store/roles/settings/router/send_*），不再挤在 feishu.py 的 if/elif 里。
新增命令 = 加一条 @command 注册，不改主文件。
"""
from __future__ import annotations

import datetime as dt
import re
import time
from typing import Callable

from boss_secretary.core import allowance as AL
from boss_secretary.core import audit as AU
from boss_secretary.core import budget as BG
from boss_secretary.core import contract as CT
from boss_secretary.core import extract as E
from boss_secretary.core import llm as L
from boss_secretary.core import notify
from boss_secretary.core import router as R
from boss_secretary.core import seal as SL
from boss_secretary.core import specs as SP
from boss_secretary.core import supplier as SUP
from boss_secretary.core import travel as TR
from boss_secretary.core.cards import (allowance_card, contract_review_card,
                                       loan_card, payment_card, seal_card,
                                       supplier_card, trip_card)

TICKET_ID_RE = re.compile(r"T\d{8}-[0-9A-F]{6}")
CONTRACT_ID_RE = re.compile(r"C\d{8}-[0-9A-F]{6}")
SUPPLIER_ID_RE = re.compile(r"S\d{8}-[0-9A-F]{6}")
LOAN_ID_RE = re.compile(r"L\d{8}-[0-9A-F]{6}")
SEAL_ID_RE = re.compile(r"Y\d{8}-[0-9A-F]{6}")

Matcher = Callable[[str], bool]
Handler = Callable[..., str]
COMMANDS: list[tuple[str, Matcher, Handler]] = []


def command(name: str, match: Matcher):
    def deco(fn: Handler) -> Handler:
        COMMANDS.append((name, match, fn))
        return fn
    return deco


def route(bot, open_id: str, text: str) -> str | None:
    """命中命令返回回复文本；未命中返回 None（调用方走 _submit）。"""
    text = (text or "").strip()
    for _, match, handler in COMMANDS:
        if match(text):
            return handler(bot, open_id, text)
    return None


def _emp(bot, open_id: str) -> dict:
    return bot.get_or_create_employee(open_id)


# ── 预算 ─────────────────────────────────────────────────────

@command("预算", lambda t: t.startswith("预算"))
def budget(bot, open_id: str, text: str) -> str:
    parsed = BG.parse_budget_text(text)
    if parsed is None:
        return "用法：`预算` 总览 | `预算 部门 2026-09 50000` 设置（全司用 *）"
    privileged = open_id in (bot.roles.get("boss"), bot.roles.get("finance"))
    month = parsed.get("month") or dt.date.today().strftime("%Y-%m")
    if parsed["action"] == "set":
        if not privileged:
            return "仅老板/财务可设置预算"
        BG.set_budget(bot.store.conn, parsed["dept"], parsed["month"],
                      parsed["amount"], created_by=open_id)
        return f"预算已设置：{parsed['dept']} {parsed['month']} {parsed['amount']:.0f} 元"
    if parsed["action"] == "query":
        return BG.to_table(BG.overview(bot.store.conn, month), month) \
            if parsed["dept"] == "*" else \
            BG.to_table([r for r in BG.overview(bot.store.conn, month)
                         if r["dept"] == parsed["dept"]], month)
    return BG.to_table(BG.overview(bot.store.conn, month), month)


# ── 供应商 ───────────────────────────────────────────────────

@command("供应商", lambda t: t.startswith(
    ("供应商", "拉黑", "变更账户", "移出黑名单")))
def supplier(bot, open_id: str, text: str) -> str:
    privileged = open_id in (bot.roles.get("boss"), bot.roles.get("finance"))
    audit_ok = open_id in (bot.roles.get("audit"), bot.roles.get("boss"))
    parts = text.split()
    sub = parts[0]
    if sub == "供应商" and len(parts) == 1:
        return SUP.to_table(SUP.list_all(bot.store.conn))
    if sub == "供应商列表":
        st = parts[1] if len(parts) > 1 else None
        return SUP.to_table(SUP.list_all(bot.store.conn, status=st))
    if sub == "供应商" and len(parts) >= 2:
        m = SUPPLIER_ID_RE.search(text)
        if m:
            c = SUP.get(bot.store.conn, m.group(0))
            if not c:
                return f"供应商不存在: {m.group(0)}"
            chg = SUP.changes(bot.store.conn, m.group(0))
            chg_lines = "\n".join(f"  {x['at']} {x['by']} 改 {x['field']}: "
                                   f"{str(x['old'])[:14]}→{str(x['new'])[:14]}"
                                   for x in chg[:5]) or "  （无变更记录）"
            return (f"{c['supplier_id']} [{c['status']}] {c['name']}\n"
                    f"信用代码：{c.get('uscc') or '-'}\n"
                    f"银行：{c.get('bank_name') or '-'} 尾号"
                    f"{str(c.get('bank_account') or '')[-4:] or '-'}\n"
                    f"变更记录：\n{chg_lines}")
    if text.startswith("供应商登记"):
        name = re.search(r"登记合同?\s*供应商\s*(\S+)", text) or \
               re.search(r"供应商登记\s+(\S+)", text)
        nm = name.group(1) if name else None
        if not nm:
            return ("用法：供应商登记 XX有限公司 [信用代码] [联系人]\n"
                    "银行账户信息请财务在审批通过后录入（变更需重审批）")
        uscc = (re.search(r"(\d{18})", text) or (None, None))[1]
        try:
            c = SUP.create_request(bot.store.conn, name=nm, uscc=uscc,
                                   reason="准入申请", created_by=open_id)
        except ValueError as e:
            return str(e)
        for role in ("finance", "boss"):
            uid = bot.roles.get(role)
            if uid:
                notify.send_card(bot, uid, supplier_card(c["supplier_id"],
                                                 nm, c.get("uscc")))
        return f"供应商准入申请 {c['supplier_id']}（{nm}）已提交，等待 finance+boss 会签"
    if text.startswith("变更账户"):
        if not privileged:
            return "仅财务/老板可发起账户变更"
        m = SUPPLIER_ID_RE.search(text)
        fields = re.findall(r"(开户行|账号)\s*[:：]?\s*(\S+)", text)
        if not m or not fields:
            return "用法：变更账户 S20260907-XXXXXX 开户行:XX银行 账号:6222..."
        sid = m.group(0)
        for fname, val in fields:
            field = "bank_name" if fname == "开户行" else "bank_account"
            r = SUP.update_field(bot.store.conn, sid, field, val,
                                 changed_by=open_id)
            if r["re_review"]:
                for role in ("finance", "boss"):
                    uid = bot.roles.get(role)
                    if uid:
                        notify.send_card(bot, uid, supplier_card(
                            sid, SUP.get(bot.store.conn, sid)["name"], None))
        return ("账户变更已记录（高危变更→重审批，卡片已重推）。"
                "审批通过前该供应商付款建议暂停")
    if text.startswith("拉黑"):
        if not audit_ok:
            return "仅审计/老板可拉黑供应商"
        m = SUPPLIER_ID_RE.search(text)
        reason = text.replace("拉黑", "").replace(m.group(0) if m else "", "").strip()
        if not m:
            return "用法：拉黑 S20260907-XXXXXX 原因"
        try:
            SUP.blacklist(bot.store.conn, m.group(0), reason or "未说明",
                          actor_id=open_id)
        except ValueError as e:
            return str(e)
        return f"⛔ 已拉黑 {m.group(0)}（{reason}）——采购/合同/付款全链路拦截生效"
    if text.startswith("移出黑名单"):
        if not audit_ok:
            return "仅审计/老板可移出黑名单"
        m = SUPPLIER_ID_RE.search(text)
        if not m:
            return "用法：移出黑名单 S20260907-XXXXXX"
        try:
            SUP.unblacklist(bot.store.conn, m.group(0), open_id)
        except ValueError as e:
            return str(e)
        return f"已移出黑名单 {m.group(0)}"
    return "供应商命令：供应商 / 供应商列表 [状态] / 供应商登记 XX有限公司 / " \
           "变更账户 S-xxx 开户行:X 账号:Y / 拉黑 S-xxx 原因 / 移出黑名单 S-xxx"


# ── 用印 ─────────────────────────────────────────────────────

@command("用印", lambda t: t.startswith(("用印", "建章")))
def seal(bot, open_id: str, text: str) -> str:
    if text == "建章":
        if open_id != bot.roles.get("boss"):
            return "仅老板可初始化印章"
        return "用法：建章 印章名称（如：建章 公司公章）"
    if text.startswith("建章 "):
        if open_id != bot.roles.get("boss"):
            return "仅老板可初始化印章"
        name = text.replace("建章", "").strip()
        if not name:
            return "请输入印章名称，如：建章 公司公章"
        s = SL.create_seal(bot.store.conn, name=name,
                           custodian=bot.roles.get("boss") or open_id)
        return f"印章已建：{name}（{s['seal_id']}，保管人=老板）"
    if text in ("用印台账",):
        is_priv = open_id in (bot.roles.get("audit"), bot.roles.get("boss"))
        rows = SL.list_requests(bot.store.conn,
                                applicant=None if is_priv else open_id)
        return SL.to_table(rows)
    if text.startswith("用印"):
        # 用印申请 公章 XX销售合同 2份 关联C-xxx
        m = re.match(r"用印(?:申请)?\s*(\S+)\s+(\S+?)(?:\s*(\d+)份)?"
                     r"(?:\s*关联\s*(C\d{8}-[0-9A-F]{6}))?\s*$", text)
        if not m:
            return ("用法：用印申请 公章 XX销售合同 2份 [关联C-xxx]\n"
                    "可用印章：" + "、".join(sl["name"] for sl in
                                            SL.seal_list(bot.store.conn)) or "-")
        seal_obj = SL.find_seal_by_name(bot.store.conn, m.group(1))
        if not seal_obj:
            return f"印章「{m.group(1)}」不存在或未启用（建章 印章名称 初始化）"
        try:
            r = SL.request(
                bot.store.conn, seal_id=seal_obj["seal_id"],
                applicant=open_id, doc_title=m.group(2),
                doc_type="合同" if "合同" in m.group(2) else "其他",
                copies=int(m.group(3) or 1), reason="",
                contract_id=m.group(4))
        except ValueError as e:
            return f"⛔ {e}"
        role = r["approver_role"]
        uid = bot.roles.get(role)
        if not uid:
            return f"用印申请 {r['request_id']} 已记录，但审批人 {role} 未配置"
        if uid == open_id:
            notify.send_card(bot, uid, seal_card(
                r["request_id"], seal_obj["name"], m.group(2),
                int(m.group(3) or 1), open_id,
                note="⚠ 申请人与审批人相同（自批），已在台账标注"))
        else:
            notify.send_card(bot, uid, seal_card(
                r["request_id"], seal_obj["name"], m.group(2),
                int(m.group(3) or 1), open_id))
        return (f"用印申请 {r['request_id']} 已提交（{seal_obj['name']}，"
                f"{m.group(2)} ×{int(m.group(3) or 1)}），等待 {role} 审批")
    if text.startswith("用印 ") and "已用" in text:
        m = SEAL_ID_RE.search(text)
        if not m:
            return "用法：用印 Y20260907-XXXXXX 已用"
        try:
            r = SL.get_request(bot.store.conn, m.group(0))
            seal_name = r and r["seal_name"]
            role = SL.approver_role_for(seal_name or "")
            if open_id not in (bot.roles.get(role), bot.roles.get("boss"),
                               r["applicant"]):
                return "仅审批人/保管人/申请人可确认用印完成"
            SL.mark_used(bot.store.conn, m.group(0), open_id)
        except ValueError as e:
            return f"确认失败: {e}"
        return f"✅ {m.group(0)} 已确认用印，台账留痕"
    return ("用印命令：用印申请 印章名 文件名 [N份] [关联C-xxx] | "
            "用印 Y-xxx 已用 | 用印台账 | 建章 印章名（老板）")


# ── 审计工作台 ───────────────────────────────────────────────

@command("审计", lambda t: t.startswith(
    ("审计", "抽检", "回放", "误报", "属实", "风险名单")))
def audit(bot, open_id: str, text: str) -> str:
    if open_id not in (bot.roles.get("audit"), bot.roles.get("boss")):
        return "仅审计/老板可使用审计工作台"
    month = dt.date.today().strftime("%Y-%m")
    if text in ("审计", "审计工作台"):
        return AU.workbench(bot.store.conn, month)
    if text == "抽检":
        q = AU.sampling_queue(bot.store.conn, month,
                              top_n=int((bot.settings.get("audit") or {})
                                        .get("sampling_top", 10)))
        return AU.queue_table(q)
    if text.startswith("抽检 ") or text.startswith("回放 "):
        tm = TICKET_ID_RE.search(text)
        if not tm:
            return "用法：抽检 T20260907-XXXXXX（生成回放包）"
        fp = AU.replay_package(bot.store.conn, tm.group(0),
                               bot.settings.get("storage", {})
                               .get("audit_dir", "data/audit"))
        if not fp:
            return f"单据不存在: {tm.group(0)}"
        return f"回放包已生成：{fp}"
    if text.startswith("误报 ") or text.startswith("属实 "):
        aid = re.search(r"\d+", text.split()[1] if len(text.split()) > 1 else "")
        if not aid:
            return "用法：误报 12 / 属实 12（异常事件编号）"
        status = "false_positive" if text.startswith("误报") else "confirmed"
        r = AU.set_anomaly_status(bot.store.conn, int(aid.group(0)), status)
        if r is None:
            return f"异常事件不存在: {aid.group(0)}"
        note = "已计入该主体风险名单" if status == "confirmed" \
            else "已标记负样本（阈值校准用）"
        return f"异常事件 #{r['anomaly_id']} → {status}，{note}"
    if text == "风险名单":
        rows = AU.risk_list(bot.store.conn)
        if not rows:
            return "（风险名单为空：无 confirmed≥2 的主体）"
        return "\n".join(f"  {r['subject']} confirmed×{r['confirmed']}"
                         for r in rows)
    return "审计命令：审计/抽检/回放 T-xxx/误报 N/属实 N/风险名单"


# ── 报销主流程 ───────────────────────────────────────────────

@command("撤回", lambda t: bool(TICKET_ID_RE.search(t))
         and ("撤回" in t or "作废" in t))
def withdraw(bot, open_id: str, text: str) -> str:
    tid = TICKET_ID_RE.search(text).group(0)
    try:
        bot.router.withdraw(tid, open_id)
        bot._pending.pop(open_id, None)
        return f"已撤回 {tid}"
    except R.RouterError as e:
        return f"撤回失败: {e}"


@command("打款", lambda t: bool(TICKET_ID_RE.search(t)) and "打款" in t)
def mark_paid(bot, open_id: str, text: str) -> str:
    tid = TICKET_ID_RE.search(text).group(0)
    if bot.roles.get("finance") and open_id != bot.roles["finance"]:
        return "仅财务可确认打款"
    try:
        bot.router.mark_paid(tid, open_id, "finance")
        return f"✅ {tid} 已确认打款，单据关闭"
    except R.RouterError as e:
        return f"打款确认失败: {e}"


@command("进度", lambda t: t in ("进度", "我的报销", "查进度"))
def progress(bot, open_id: str, text: str) -> str:
    rows = bot.store.conn.execute(
        "SELECT ticket_id, status, amount FROM tickets WHERE employee_id=?"
        " ORDER BY rowid DESC LIMIT 10", (open_id,)).fetchall()
    if not rows:
        return "暂无报销单。直接发我一句报销描述即可，例如：9月5号打车98块，滴滴出行发票"
    lines = [f"{tid}  {st}  {amt if amt is not None else '-'}元"
             for tid, st, amt in rows]
    return "\n".join(["你的报销单："] + lines)


@command("我的额度", lambda t: t in ("额度", "我的额度"))
def my_allowances(bot, open_id: str, text: str) -> str:
    return AL.to_table(AL.list_for(bot.store.conn, open_id))


@command("导出凭证", lambda t: t.startswith("导出凭证"))
def export_voucher(bot, open_id: str, text: str) -> str:
    if open_id not in (bot.roles.get("finance"), bot.roles.get("boss")):
        return "仅财务/老板可导出凭证"
    from boss_secretary.finance import export as FE
    month = text.replace("导出凭证", "").strip() or dt.date.today().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return "月份格式：导出凭证 2026-09"
    out = f"data/vouchers_{month}.csv"
    result = FE.export_csv(bot.store.conn, out, month=month,
                           settings=bot.settings)
    return (f"📊 {FE.summary_text(result)}\n"
            f"未打款单为计提凭证（借费用/贷应付），已打款单含打款凭证。"
            f"文件在服务器 data/ 目录，可直接金蝶引入")


@command("额度申请", lambda t: any(k in t for k in ("额度", "备用金", "预算")))
def allowance_request(bot, open_id: str, text: str) -> str:
    emp = _emp(bot, open_id)
    parsed = AL.extract_allowance(text, settings=bot.settings)
    if not parsed.get("amount"):
        return "请说明额度金额，例如：申请打车额度200元，今晚加班打车用"
    req = AL.create_request(bot.store.conn, emp["user_id"],
                            parsed["category"], float(parsed["amount"]),
                            reason=parsed.get("reason", ""),
                            created_by=open_id,
                            expense_types=parsed.get("expense_types", ()),
                            cfg=bot.settings.get("allowances"))
    role = req["required_role"]
    uid = bot.roles.get(role)
    if not uid:
        return f"额度申请 {req['allowance_id']} 已记录，但审批人 {role} 未配置 open_id"
    notify.send_card(bot, uid, allowance_card(req, emp["user_id"]))
    return (f"额度申请已提交 {req['allowance_id']}（{req['category']} "
            f"{req['amount']} 元），等待 {role} 审批；生效后在此额度内报销免逐单审批")


# ── 合同 / 付款 ──────────────────────────────────────────────

@command("登记合同", lambda t: t.startswith("登记合同"))
def contract_register(bot, open_id: str, text: str) -> str:
    emp = _emp(bot, open_id)
    spec = SP.get_spec("contract")
    print(f"[feishu] 合同登记: {text[:40]!r}")
    try:
        c = E.get_extractor("contract")(text, settings=bot.settings)
    except L.LLMError as e:
        return f"合同要素解析失败: {str(e)[:120]}"
    print(f"[feishu] 合同抽取结果: {c}")
    c.setdefault("attachments", [])
    gate = bot._supplier_gate(c.get("supplier"))
    if gate and gate.startswith("⛔"):
        return gate
    missing = [k for k in spec.required_fields
               if k not in ("amount",) and not c.get(k)]
    if not c.get("amount"):
        missing = ["amount"] + missing
    bot._pending[open_id] = {"kind": "contract", "ctx": c, "ts": time.time()}
    if missing:
        return f"请补充：{spec.missing_names(missing)}（回复自动并入；「取消」放弃）"
    return ("合同要素已登记。请上传**合同文件（PDF）**完成 AI 全文审查并提交"
            "（无文件则按要素审查；「取消」放弃）")


@command("续签", lambda t: t.startswith("续签"))
def contract_renew(bot, open_id: str, text: str) -> str:
    cm = CONTRACT_ID_RE.search(text)
    if not cm:
        return "用法：续签 C20260907-XXXXXX [新结束日期 YYYY-MM-DD]"
    dm = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    old_c = CT.get(bot.store.conn, cm.group(0))
    if not old_c:
        return f"合同不存在: {cm.group(0)}"
    new_end = dm.group(1) if dm else (dt.date.today() + dt.timedelta(days=365)).isoformat()
    new_id = CT.renew(bot.store.conn, cm.group(0), new_end, actor_id=open_id)
    for role in ("legal", "boss"):
        uid = bot.roles.get(role)
        if uid:
            notify.send_card(bot, uid, contract_review_card(
                new_id, old_c["title"], old_c.get("supplier"),
                old_c.get("amount"), [{"level": "低", "clause": "续签",
                                       "note": f"续自 {cm.group(0)}，新期限至 {new_end}"}]))
    return (f"续签合同 {new_id} 已创建（至 {new_end}），"
            f"原合同 {cm.group(0)} 标记 renewed；等待 legal+boss 会签生效")


@command("付款", lambda t: t.startswith("付款 "))
def payment_create(bot, open_id: str, text: str) -> str:
    emp = _emp(bot, open_id)
    cm = CONTRACT_ID_RE.search(text)
    if not cm:
        return "用法：付款 C20260907-XXXXXX 金额 [第一期备注]"
    c = CT.get(bot.store.conn, cm.group(0))
    if not c:
        return f"合同不存在: {cm.group(0)}"
    if c["status"] not in (CT.ACTIVE, CT.EXPIRING):
        return f"合同 {cm.group(0)} 状态 {c['status']}，不可发起付款"
    nm = re.search(r"(\d+(?:\.\d+)?)\s*元", text)
    if not nm:
        return "请说明金额，例如：付款 C20260907-XXXXXX 5000元 第一期"
    gate = bot._supplier_gate(c.get("supplier"))
    if gate and gate.startswith("⛔"):
        return gate
    note = text.replace(nm.group(0), "").replace(cm.group(0), "").strip()
    pid = CT.create_payment(bot.store.conn, contract_id=cm.group(0),
                            procurement_id=None, amount=float(nm.group(1)),
                            employee_id=emp["user_id"], note=note[:30])
    f_uid = bot.roles.get("finance")
    if f_uid:
        notify.send_card(bot, f_uid, payment_card(pid, float(nm.group(1)), note[:30]))
    return (f"付款单 {pid} 已创建（{c['title']} {nm.group(1)}元，"
            f"合同已付 {CT.paid_total(bot.store.conn, cm.group(0)):.0f}/"
            f"{c.get('amount') or '-'}），待财务确认打款")


@command("合同查询", lambda t: bool(CONTRACT_ID_RE.search(t))
         and "合同" in t and len(t) < 25)
def contract_query(bot, open_id: str, text: str) -> str:
    cid = CONTRACT_ID_RE.search(text).group(0)
    c = CT.get(bot.store.conn, cid)
    if not c:
        return f"合同不存在: {cid}"
    paid = CT.paid_total(bot.store.conn, cid)
    return (f"{cid} [{c['status']}] {c['title']}\n供应商：{c.get('supplier') or '-'}\n"
            f"金额：{c.get('amount') or '-'} 元（已付 {paid:.0f}）\n"
            f"期限：{str(c.get('start_date'))[:10]} ~ {str(c.get('end_date'))[:10]}\n"
            f"付款条款：{c.get('payment_terms') or '-'}")


# ── 采购 / 差旅 / 借款 ───────────────────────────────────────

@command("采购", lambda t: "采购" in t)
def procurement_submit(bot, open_id: str, text: str) -> str:
    print(f"[feishu] 采购抽取: {text[:40]!r}")
    try:
        ctx = E.get_extractor("procurement")(text, settings=bot.settings)
    except L.LLMError as e:
        return f"采购单解析失败: {str(e)[:120]}"
    print(f"[feishu] 采购抽取结果: {ctx}")
    if not ctx.get("amount") or not ctx.get("title"):
        return "请补充采购标的和金额，例如：采购测试服务器 8000 元，供应商 XX 电脑"
    gate = bot._supplier_gate(ctx.get("supplier"))
    if gate and gate.startswith("⛔"):
        return gate
    ctx.setdefault("attachments", [])
    bot._pending[open_id] = {"kind": "procurement", "ctx": ctx, "ts": time.time()}
    return ("请上传采购附件（**合同 / PO / 报价单** 任一，图片或 PDF/文档），"
            "上传后自动提交审批；「取消」放弃")


@command("出差申请", lambda t: t.startswith("出差申请") or (
    t.startswith("出差") and "申请" not in t and len(t) > 2
    and any(k in t for k in ("天", "周", "出差"))))
def trip_request(bot, open_id: str, text: str) -> str:
    emp = _emp(bot, open_id)
    print(f"[feishu] 出差抽取: {text[:40]!r}")
    try:
        obj = E.get_extractor("trip")(text, settings=bot.settings)
    except L.LLMError as e:
        return f"出差申请解析失败: {str(e)[:120]}"
    print(f"[feishu] 出差抽取结果: {obj}")
    if not obj.get("destination") or not obj.get("estimate"):
        return "请补充目的地和预估金额，例如：出差申请 上海5天 预计3000元 见客户"
    t = TR.trip_request(bot.store.conn, employee_id=emp["user_id"],
                        dept_id=emp.get("dept_id"),
                        destination=obj.get("destination"),
                        reason=obj.get("reason") or "",
                        estimate=float(obj["estimate"]),
                        start_date=obj.get("start_date")
                        or dt.date.today().isoformat(),
                        end_date=obj.get("end_date")
                        or (dt.date.today() + dt.timedelta(days=7)).isoformat())
    role = "manager" if float(obj["estimate"]) <= 5000 else "boss"
    uid = bot.roles.get(role)
    if not uid:
        return f"出差申请 {t['trip_id']} 已记录，但审批人 {role} 未配置 open_id"
    notify.send_card(bot, uid, trip_card(t, emp["user_id"]))
    return (f"出差申请 {t['trip_id']} 已提交（{t['destination']}，预估 "
            f"{t['estimate']} 元），等待 {role} 审批；生效期间内报销自动关联")


@command("我的出差", lambda t: t in ("出差", "我的出差", "出差记录"))
def my_trips(bot, open_id: str, text: str) -> str:
    return TR.trip_table(TR.trip_list(bot.store.conn, open_id))


@command("借款申请", lambda t: t.startswith(("借款申请", "借款 ")))
def loan_request(bot, open_id: str, text: str) -> str:
    emp = _emp(bot, open_id)
    nm = re.search(r"(\d+(?:\.\d+)?)\s*元", text)
    if not nm:
        return "请说明金额，例如：借款申请 2000元 出差备用金"
    reason = text.replace(nm.group(0), "").replace("借款申请", "").strip()
    l = TR.loan_request(bot.store.conn, employee_id=emp["user_id"],
                        amount=float(nm.group(1)), reason=reason[:40])
    uid = bot.roles.get("boss")
    if not uid:
        return f"借款单 {l['loan_id']} 已记录，但审批人 boss 未配置"
    notify.send_card(bot, uid, loan_card(l, emp["user_id"]))
    return f"借款申请 {l['loan_id']}（{l['amount']} 元）已提交，等待 boss 审批并放款"


@command("我的借款", lambda t: t in ("借款", "我的借款"))
def my_loans(bot, open_id: str, text: str) -> str:
    return TR.loan_table(TR.loan_list(bot.store.conn, open_id))


@command("核销借款", lambda t: t.startswith("核销借款"))
def loan_offset(bot, open_id: str, text: str) -> str:
    if open_id not in (bot.roles.get("finance"), bot.roles.get("boss")):
        return "仅财务/老板可核销借款"
    m = LOAN_ID_RE.search(text)
    nm = re.search(r"(\d+(?:\.\d+)?)\s*元", text)
    if not m or not nm:
        return "用法：核销借款 L20260907-XXXXXX 500元"
    try:
        rem = TR.loan_offset(bot.store.conn, m.group(0),
                             float(nm.group(1)), by=open_id)
    except ValueError as e:
        return str(e)
    return f"核销完成，{m.group(0)} 余额 {rem} 元"


@command("借款用法", lambda t: t.startswith("借款") and "申请" not in t)
def loan_usage(bot, open_id: str, text: str) -> str:
    return "用法：借款申请 2000元 出差备用金"


@command("取消", lambda t: t in ("取消", "不报了"))
def cancel_pending(bot, open_id: str, text: str) -> str:
    bot._pending.pop(open_id, None)
    return "已放弃当前待补单据"
