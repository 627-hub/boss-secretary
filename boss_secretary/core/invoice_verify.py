"""发票验真（分层，PRD Q7 的免费实现层）。

L1 形式校验 structural_check：号码结构（数电票 20 位 / 旧版 10+8 位）、
   号码内年份 ↔ 开票日期一致、金额>0、开票方非空。
L2 二维码交叉核验 decode_invoice_qr：税控设备打印的 QR 与 OCR 要素互核
   （号码/金额/日期），不一致 = 高风险（OCR 错或票伪）。
L3 税局真查验：官方平台有验证码、第三方接口付费 —— verify_provider 留
   配置化 adapter（settings.invoice_verify.provider），未配置则 SKIP。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Mapping, Sequence

DSDP_NO = re.compile(r"^\d{20}$")              # 数电票: 20 位全数字
OLD_CODE = re.compile(r"^\d{10}$|^\d{12}$")    # 旧版发票代码
OLD_NO = re.compile(r"^\d{8}$")                # 旧版发票号码

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


def structural_check(inv: Mapping[str, Any]) -> list[dict]:
    issues: list[dict] = []
    no = str(inv.get("invoice_no") or "")
    code = str(inv.get("invoice_code") or "")
    date = str(inv.get("invoice_date") or "")
    amount = inv.get("invoice_amount")
    seller = inv.get("invoice_seller")

    if not no:
        issues.append({"level": WARN, "rule": "号码缺失", "detail": "未识别出发票号码"})
    elif DSDP_NO.match(no):
        issues.append({"level": PASS, "rule": "数电票号码", "detail": f"{no}（20 位，全数字）"})
    elif OLD_NO.match(no):
        if code and OLD_CODE.match(code):
            m = re.match(r"^\d{2}", code[0:6][4:6]) if False else None
            yy = code[4:6] if len(code) == 10 else code[4:6]
            try:
                year = int("20" + yy) if len(code) >= 6 else None
            except ValueError:
                year = None
            if year and date[:4] and int(date[:4]) != year:
                issues.append({"level": FAIL,
                               "rule": "代码年份↔开票日期不一致",
                               "detail": f"代码年份 {year} vs 发票日期 {date[:4]}"})
            else:
                issues.append({"level": PASS, "rule": "旧版代码/号码结构",
                               "detail": f"代码 {code} + 号码 {no}"})
        else:
            issues.append({"level": WARN, "rule": "旧版号码缺代码",
                           "detail": f"号码 {no} 但代码缺失/结构异常({code or '空'})"})
    else:
        issues.append({"level": FAIL, "rule": "号码结构异常",
                       "detail": f"{no} 既非数电票(20位)也非旧版(8位)"})

    if amount is None or float(amount) <= 0:
        issues.append({"level": WARN, "rule": "金额缺失/非正", "detail": str(amount)})
    if not seller:
        issues.append({"level": WARN, "rule": "开票方缺失",
                       "detail": "数电票/电子发票应有开票方（销售方）名称"})
    return issues


# ── 二维码（税控打印，与 OCR 互核）────────────────────────────

def parse_invoice_qr_payload(payload: str) -> dict | None:
    """解析常见发票二维码内容：JSON / 旧版逗号串。"""
    payload = (payload or "").strip()
    if not payload:
        return None
    try:
        obj = json.loads(payload)
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                kk = str(k)
                if "金额" in kk or kk.lower() in ("amount", "totalamount", "total"):
                    out["amount"] = _f(v)
                elif "号码" in kk or kk.lower() in ("number", "invoiceno"):
                    out["invoice_no"] = str(v)
                elif "代码" in kk or kk.lower() in ("code", "invoicecode"):
                    out["invoice_code"] = str(v)
                elif "日期" in kk or kk.lower() in ("date", "invoicedate"):
                    out["invoice_date"] = _d(v)
                elif "校验" in kk or kk.lower().startswith("check"):
                    out["check_code"] = str(v)
            if out:
                out["format"] = "json"
                return out
    except Exception:
        pass
    parts = [p.strip() for p in payload.split(",")]
    if len(parts) >= 5 and parts[0] in ("01", "02", "04", "10", "12"):
        # 版本, 票种, 发票代码, 发票号码, 金额, 开票日期, 校验码...
        out: dict[str, Any] = {"format": "csv", "version": parts[0],
                               "kind": parts[1],
                               "invoice_code": parts[2],
                               "invoice_no": parts[3],
                               "amount": _f(parts[4])}
        if len(parts) >= 6:
            out["invoice_date"] = _d(parts[5])
        if len(parts) >= 7:
            out["check_code"] = parts[6]
        return out
    # 数电票 QR 常见 URL 形式，含 20 位号码
    m = re.search(r"(\d{20})", payload)
    if m:
        out = {"format": "url", "invoice_no": m.group(1)}
        m2 = re.search(r"(?<!\d)(20\d{2}[-/.]\d{2}[-/.]\d{2})(?!\d)", payload)
        if m2:
            out["invoice_date"] = _d(m2.group(1))
        m3 = re.search(r"(?:金额|amount)[=:：]\s*(\d+(?:\.\d+)?)", payload, re.I)
        if m3:
            out["amount"] = _f(m3.group(1))
        return out
    return None


def _f(v: Any) -> float | None:
    try:
        return round(float(str(v).replace("¥", "").replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def _d(v: Any) -> str | None:
    s = str(v).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def decode_invoice_qr(image_bytes: bytes) -> dict | None:
    import cv2
    import numpy as np
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    det = cv2.QRCodeDetector()
    payload, _, _ = det.detectAndDecode(img)
    if not payload:
        big = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        payload, _, _ = det.detectAndDecode(big)
    if not payload:
        return None
    return parse_invoice_qr_payload(payload)


def cross_check_qr(inv: Mapping[str, Any], qr: Mapping[str, Any]) -> list[dict]:
    issues: list[dict] = []
    for key, label in (("invoice_no", "发票号码"), ("invoice_code", "发票代码"),
                       ("invoice_date", "开票日期"), ("amount", "金额")):
        qv, ov = qr.get(key), inv.get(key)
        if qv in (None, "") or ov in (None, ""):
            continue
        if key == "amount":
            if abs(float(qv) - float(ov)) > 0.01:
                issues.append({"level": FAIL, "rule": f"二维码↔识别 {label}不一致",
                               "detail": f"QR {qv} vs 识别 {ov}"})
        elif str(qv) != str(ov):
            issues.append({"level": FAIL, "rule": f"二维码↔识别 {label}不一致",
                           "detail": f"QR {qv} vs 识别 {ov}"})
    return issues


# ── L3 第三方真查验 adapter（付费接口，配置化预留）──────────────

def verify_provider(inv: Mapping[str, Any], settings: Mapping | None = None) -> dict:
    cfg = (settings or {}).get("invoice_verify") or {}
    provider = cfg.get("provider", "none")
    if provider == "none" or not cfg.get("api_key"):
        return {"provider": "none", "level": SKIP,
                "detail": "税局真查验未配置（官方平台需验证码；第三方接口付费）。"
                          "可在 settings.invoice_verify 配置 provider"}
    # 预留: 不同供应商报文不同, 统一走 OpenAI 兼容风格? 不——直接 requests。
    # 具体供应商接入时在此实现（百望/票易通等），报文字段以供应商文档为准。
    return {"provider": provider, "level": SKIP,
            "detail": f"provider={provider} 的报文实现待接入（schema 因供应商而异）"}


def verify(invoice: Mapping[str, Any], image_bytes: bytes | None = None,
           settings: Mapping | None = None) -> dict:
    """汇总各层结果：{"structural": [...], "qr": {...}|None, "qr_issues": [...],
    "provider": {...}, "overall": PASS/WARN/FAIL}"""
    structural = structural_check(invoice)
    qr_payload = None
    qr_issues: list[dict] = []
    if image_bytes:
        qr_payload = decode_invoice_qr(image_bytes)
        if qr_payload:
            qr_issues = cross_check_qr(invoice, qr_payload)
    provider = verify_provider(invoice, settings)
    levels = [i["level"] for i in structural + qr_issues if i["level"] in (WARN, FAIL)] \
        + [provider["level"]]
    overall = FAIL if FAIL in levels else WARN if WARN in levels else PASS
    return {"structural": structural, "qr": qr_payload, "qr_issues": qr_issues,
            "provider": provider, "overall": overall}


def to_text(result: Mapping) -> str:
    lines = []
    for i in result["structural"]:
        mark = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "SKIP": "-"}.get(i["level"], "?")
        lines.append(f"{mark} {i['rule']}: {i['detail']}")
    if result["qr"]:
        lines.append(f"✓ 二维码解析成功（{result['qr'].get('format')}）")
        if not result["qr_issues"]:
            lines.append("✓ 二维码与识别要素互核一致")
    for i in result["qr_issues"]:
        lines.append(f"✗ {i['rule']}: {i['detail']}")
    lines.append(f"- 第三方查验: {result['provider']['detail']}")
    return "\n".join(lines)
