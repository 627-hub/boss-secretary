"""决策通知策略测试：文案与送达对象。"""
from boss_secretary.core import policy as PL


class Target:
    def __init__(self):
        self.texts = []

    def send_text(self, uid, text):
        self.texts.append((uid, text))


def test_text_helpers():
    assert PL.comment_suffix("") == ""
    assert PL.comment_suffix("同意") == "；审批意见：同意"
    assert PL.reject_text("合同", "C1", "太贵") == "合同 C1 被拒绝：太贵"
    assert PL.reject_text("合同", "C1") == "合同 C1 被拒绝"
    assert PL.chain_text("合同", "C1", "boss", "太贵") == "合同 C1 已被老板拒绝：太贵"


def test_emit_approve_appends_suffix():
    t = Target()
    PL.emit(t, PL.DecisionNotice(label="合同", doc_id="C1", submitter="e1",
                                 approve=True, comment="条款已核",
                                 detail="✅ 合同生效"))
    assert t.texts == [("e1", "✅ 合同生效；审批意见：条款已核")]


def test_emit_approve_without_detail_is_silent():
    t = Target()
    PL.emit(t, PL.DecisionNotice(label="合同", doc_id="C1", submitter="e1"))
    assert t.texts == []


def test_emit_reject_submitter_and_chain_others():
    t = Target()
    PL.emit(t, PL.DecisionNotice(label="合同", doc_id="C1", submitter="e1",
                                 role="boss", approve=False, comment="超预算",
                                 others=["l1", "e1"]))
    assert ("e1", "合同 C1 被拒绝：超预算") in t.texts
    assert ("l1", "合同 C1 已被老板拒绝：超预算") in t.texts


def test_emit_survives_dead_target():
    class Dead(Target):
        def send_text(self, uid, text):
            raise RuntimeError("掉线")
    PL.emit(Dead(), PL.DecisionNotice(label="合同", doc_id="C1", submitter="e1",
                                      role="boss", approve=False, others=["l1"]))
