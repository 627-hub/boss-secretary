import datetime as dt

import pytest

from boss_secretary.core.router import SQLiteTicketStore
from boss_secretary.report import monthly as MO


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    store = SQLiteTicketStore(tmp_path_factory.mktemp("m") / "t.db",
                              audit_dir=tmp_path_factory.mktemp("a"))
    c = store.conn
    rows = [
        ("T1", "e1", "D1", "PAID", 469, "餐饮", "2026-09-05", "2026-09-05T10:00", None),
        ("T2", "e2", "D1", "APPROVED", 300, "交通", "2026-09-06", "2026-09-06T10:00", None),
        ("T3", "e3", "D2", "AUTO_APPROVED", 98, "交通", "2026-09-06", "2026-09-06T11:00",
         "A1"),
        ("T4", "e4", "D2", "REJECTED", 700, "餐饮", "2026-09-06", "2026-09-06T12:00", None),
        ("T5", "e1", "D1", "APPROVED", 1000, "办公", "2026-08-20", "2026-08-20T10:00", None),
        ("T6", "e2", "D1", "APPROVED", 800, "办公", "2026-08-20", "2026-08-20T10:00", None),
        ("T7", "e1", "D1", "APPROVED", 600, "交通", "2026-07-18", "2026-07-18T10:00", None),
    ]
    for tid, emp, dept, st, amt, et, occ, created, al in rows:
        c.execute("INSERT INTO tickets(ticket_id, employee_id, dept_id, type, status,"
                  " sensitivity, amount, currency, expense_type, reason, occurred_at,"
                  " created_at, allowance_id)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, emp, dept, "reimburse", st, "normal", amt, "CNY", et,
                   "事由", occ, created, al))
    c.execute("INSERT INTO allowances(allowance_id, employee_id, category,"
              " total_amount, used_amount, status, expires_at)"
              " VALUES('A1','e3','打车',200,98,'active','2026-10-01')")
    c.commit()
    return c


def test_collect(conn):
    d = MO.collect(conn, "2026-09")
    assert d["month_count"] == 4
    assert d["amount"] == 867.0
    assert d["paid_amount"] == 469
    assert d["rejected_n"] == 1
    assert d["direct_approved_n"] == 1
    assert d["allowance_used_n"] == 1
    assert d["type_sum"]["餐饮"] == 469
    assert d["dept_sum"]["D1"] == 769
    assert len(d["trend"]) == 6


def test_charts_and_exports(conn, tmp_path):
    d = MO.collect(conn, "2026-09")
    imgs = MO.build_charts(d, tmp_path)
    assert len(imgs) >= 3 and all(p.exists() and p.stat().st_size > 1000 for p in imgs)
    docx = MO.export_docx(d, imgs, tmp_path / "月报.docx")
    pptx = MO.export_pptx(d, imgs, tmp_path / "月报.pptx")
    assert docx.exists() and pptx.exists()
    from docx import Document
    from pptx import Presentation
    assert "秘书月报" in Document(str(docx)).paragraphs[0].text
    prs = Presentation(str(pptx))
    assert "秘书月报" in prs.slides[0].shapes.title.text
    assert len(prs.slides.__iter__.__self__._sldIdLst) >= 3


def test_markdown_render(conn, tmp_path):
    d = MO.collect(conn, "2026-09")
    imgs = MO.build_charts(d, tmp_path)
    md = MO.render_markdown(d, imgs)
    assert "# 秘书月报 · 2026-09" in md
    assert "额度核销 1 单" in md
    assert "口径说明" in md


def test_generate_full(conn, tmp_path):
    r = MO.generate(conn, month="2026-09", outdir=tmp_path / "rep", fmt="both")
    assert "docx" in r and "pptx" in r and len(r["images"]) >= 3


def test_cli(tmp_path):
    import subprocess, sys, os
    db = tmp_path / "t.db"
    from boss_secretary.core.router import SQLiteTicketStore
    store = SQLiteTicketStore(db, audit_dir=tmp_path / "a")
    store.conn.execute("INSERT INTO tickets(ticket_id, employee_id, dept_id, type,"
                       " status, sensitivity, amount, currency, expense_type,"
                       " occurred_at, reason)"
                       " VALUES('T1','e1','D1','reimburse','APPROVED',"
                       "'normal',100,'CNY','交通','2026-09-05','x')")
    store.conn.commit()
    r = subprocess.run([sys.executable, "-m", "boss_secretary.report.monthly",
                        "--db", str(db), "--month", "2026-09",
                        "--out", str(tmp_path / "rep")],
                       capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": "."}, cwd=".")
    assert r.returncode == 0 and "月报生成完成" in r.stdout


def test_narrative_in_report(conn, tmp_path, monkeypatch):
    def fake_narrative(data, **kw):
        return "本月合计 867 元，餐饮与办公为主，全部为额定内支出。"
    monkeypatch.setattr(MO, "generate_narrative", fake_narrative)
    r = MO.generate(conn, month="2026-09", outdir=tmp_path / "rep", fmt="docx")
    from docx import Document
    texts = "\n".join(pp.text for pp in Document(str(r["docx"])).paragraphs)
    assert "AI 简述" in texts and "额定内支出" in texts
    data2 = {**MO.collect(conn, "2026-09"), "narrative": "测试叙事"}
    md2 = MO.render_markdown(data2, [])
    assert "测试叙事" in md2 and "AI 简述" in md2


def test_narrative_skips_silently_on_llm_error():
    from boss_secretary.core import llm as L

    def boom(msgs, **kw):
        raise L.LLMError("LLM 不可用")
    out = MO.generate_narrative({"month": "2026-09"}, llm_fn=boom)
    assert out is None
