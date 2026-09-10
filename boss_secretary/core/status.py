"""状态常量唯一事实源。

各模块曾各自定义 PENDING/ACTIVE/REJECTED，值还漂移过（供应商准入的 PENDING
实际是 "pending_review"）。这里按命名空间集中定义；各模块保留原常量名做别名，
值一律不改，保证既有数据库数据兼容。

新增状态先加到这里；业务模块里不再出现重复定义。
"""


class Ticket:
    """报销/采购主流程（大写终态机，router.TRANSITIONS 使用）。"""
    DRAFT = "DRAFT"
    REVIEWING = "REVIEWING"
    AUTO_APPROVED = "AUTO_APPROVED"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    PAID = "PAID"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"
    WITHDRAWN = "WITHDRAWN"
    CANCELLED = "CANCELLED"


class Doc:
    """文档类单据通用生命周期（小写）。"""
    PENDING = "pending"
    PENDING_REVIEW = "pending_review"
    REVIEWING = "reviewing"
    ACTIVE = "active"
    APPROVED = "approved"
    REJECTED = "rejected"
    CLOSED = "closed"
    EXPIRED = "expired"
    REVOKED = "revoked"
    SUSPENDED = "suspended"
    BLACKLISTED = "blacklisted"
    VOIDED = "voided"
    USED = "used"
    RENEWED = "renewed"
    EXPIRING = "expiring"
    EXHAUSTED = "EXHAUSTED"
    FROZEN = "frozen"
    PAID_OUT = "paid_out"
    OPEN = "open"


class Payment:
    """付款单。"""
    PENDING = "pending"
    PAID = "paid"
    REJECTED = "rejected"
