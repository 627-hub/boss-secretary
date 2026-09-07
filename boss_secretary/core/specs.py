"""统一审核契约（三要素模型）：预算 × 事由/标的 × 交付物文件。

每类单据一份 DocSpec，摄取/核验/卡片文案全部由 spec 驱动：
  required_fields  结构化必填（缺 → 追问并自动并单）
  required_docs    必须交付的文件（无附件不放行；交付物按类型：发票/报价单/PO/合同）
  budget_check     预算维度（部门/全司月度预算；额度本身就是预算授权，优先于部门预算）
  reason_field    卡片与审查的"事由/标的"字段名
新增单据类型 = 新增一份 spec + 对应抽取函数，摄取管线零改动。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DocSpec:
    type: str
    display: str
    color: str
    reason_field: str
    required_fields: tuple[str, ...]
    required_docs: tuple[str, ...]          # 空元组 = 附件可选
    doc_prompt: str
    budget_check: bool
    field_names: dict = field(default_factory=dict)   # 追问文案映射

    def missing_names(self, missing: Sequence[str]) -> str:
        names = {"amount": "金额", "occurred_at": "发生日期", "reason": "事由",
                 "expense_type": "费用类型", "invoice_no": "发票号",
                 "title": "标的", "end_date": "结束日期"}
        names.update(self.field_names)
        return "、".join(names.get(k, k) for k in missing)


from typing import Sequence  # noqa: E402

SPECS: dict[str, DocSpec] = {
    "reimburse": DocSpec(
        type="reimburse", display="报销", color="orange", reason_field="reason",
        required_fields=("amount", "occurred_at", "reason", "expense_type",
                         "invoice_no"),
        required_docs=("invoice",),
        doc_prompt="请上传发票（图片或 PDF），识别后自动并入",
        budget_check=True,
        field_names={"invoice_no": "发票号"}),
    "procurement": DocSpec(
        type="procurement", display="采购", color="purple", reason_field="title",
        required_fields=("title", "amount"),
        required_docs=("合同", "PO", "报价单"),
        doc_prompt="请上传采购附件（**合同 / PO / 报价单** 任一，图片或 PDF/文档），"
                   "上传后自动提交审批",
        budget_check=True,
        field_names={"title": "采购标的"}),
    "contract": DocSpec(
        type="contract", display="合同", color="purple", reason_field="title",
        required_fields=("title", "amount", "end_date"),
        required_docs=("contract_file",),
        doc_prompt="请上传合同文件（PDF），完成 AI 全文审查与提交",
        budget_check=True,
        field_names={"title": "合同名称", "end_date": "结束日期"}),
}


def get_spec(type_: str) -> DocSpec:
    return SPECS.get(type_) or SPECS["reimburse"]
