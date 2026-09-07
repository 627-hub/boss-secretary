import datetime as dt

from boss_secretary.core import compliance as C

TODAY = dt.date(2026, 9, 6)


def cfg(**overrides):
    c = C.default_config()
    for rid, patch in overrides.items():
        c[rid]["params"].update(patch)
    return c


def test_r1_amount():
    assert C.r1_amount({"amount": 300}, {}).verdict == "PASS"
    assert C.r1_amount({"amount": 0}, {}).verdict == "FAIL"
    assert C.r1_amount({"amount": -5}, {}).verdict == "FAIL"
    assert C.r1_amount({"amount": 200000}, {"max_amount": 100000}).verdict == "FAIL"
    assert C.r1_amount({}, {}).verdict == "SKIP"


def test_r2_duplicate_invoice():
    today = TODAY
    history = [
        {"ticket_id": "T0", "invoice_no": "12345", "occurred_at": "2026-09-01"},
        {"ticket_id": "T9", "invoice_no": "99999", "occurred_at": "2026-05-01"},
    ]
    r = C.r2_duplicate_invoice({"invoice_no": "12345", "occurred_at": "2026-09-03"},
                               history, {"lookback_days": 90}, today)
    assert r.verdict == "FAIL" and "T0" in r.evidence
    r = C.r2_duplicate_invoice({"invoice_no": "12345", "occurred_at": "2026-12-01"},
                               history, {"lookback_days": 90}, today)
    assert r.verdict == "PASS"
    r = C.r2_duplicate_invoice({"invoice_no": "88888"}, history, {}, today)
    assert r.verdict == "PASS"
    r = C.r2_duplicate_invoice({}, history, {}, today)
    assert r.verdict == "SKIP"


def test_r3_duplicate_expense():
    history = [{"ticket_id": "T0", "employee_id": "e1", "amount": 300,
                "invoice_seller": "滴滴出行", "occurred_at": "2026-09-04"}]
    ctx = {"ticket_id": "T1", "employee_id": "e1", "amount": 300,
           "invoice_seller": "滴滴出行", "occurred_at": "2026-09-06"}
    assert C.r3_duplicate_expense(ctx, history, {"window_days": 3}, TODAY).verdict == "WARN"
    ctx2 = {**ctx, "occurred_at": "2026-09-10"}
    assert C.r3_duplicate_expense(ctx2, history, {"window_days": 3}, TODAY).verdict == "PASS"
    ctx3 = {**ctx, "invoice_seller": "高德打车"}
    assert C.r3_duplicate_expense(ctx3, history, {}, TODAY).verdict == "PASS"
    ctx4 = {**ctx, "employee_id": "e2"}
    assert C.r3_duplicate_expense(ctx4, history, {}, TODAY).verdict == "PASS"
    assert C.r3_duplicate_expense({"employee_id": "e1"}, history, {}, TODAY).verdict == "SKIP"


def test_r4_type_limit():
    p = {"limits": {"交通": 200, "餐饮": 200}, "per_person_types": ["餐饮"]}
    assert C.r4_type_limit({"expense_type": "交通", "amount": 150}, p).verdict == "PASS"
    assert C.r4_type_limit({"expense_type": "交通", "amount": 250}, p).verdict == "WARN"
    r = C.r4_type_limit({"expense_type": "餐饮", "amount": 400, "headcount": 2}, p)
    assert r.verdict == "PASS" and "2人" in r.evidence
    assert C.r4_type_limit({"expense_type": "餐饮", "amount": 500, "headcount": 2}, p).verdict == "WARN"
    assert C.r4_type_limit({"expense_type": "其他", "amount": 9999}, p).verdict == "PASS"
    assert C.r4_type_limit({"expense_type": "交通"}, p).verdict == "SKIP"


def test_r5_date_reasonable():
    p = {"max_age_days": 90, "max_future_days": 0}
    assert C.r5_date_reasonable({"occurred_at": "2026-09-06"}, p, TODAY).verdict == "PASS"
    assert C.r5_date_reasonable({"occurred_at": "2026-06-10"}, p, TODAY).verdict == "PASS"
    assert C.r5_date_reasonable({"occurred_at": "2026-01-01"}, p, TODAY).verdict == "WARN"
    assert C.r5_date_reasonable({"occurred_at": "2026-09-08"}, p, TODAY).verdict == "WARN"
    assert C.r5_date_reasonable({"occurred_at": "2026/9/5"}, p, TODAY).verdict == "PASS"
    r = C.r5_date_reasonable({"occurred_at": "垃圾"}, p, TODAY)
    assert r.verdict == "WARN" and "无法解析" in r.evidence
    assert C.r5_date_reasonable({}, p, TODAY).verdict == "SKIP"


def test_r6_required():
    p = {"required": ["amount", "occurred_at", "reason", "expense_type", "invoice_no"]}
    full = {"amount": 1, "occurred_at": "2026-09-06", "reason": "x",
            "expense_type": "交通", "invoice_no": "1"}
    assert C.r6_required(full, p).verdict == "PASS"
    r = C.r6_required({"amount": 1}, p)
    assert r.verdict == "WARN" and "reason" in r.evidence and "invoice_no" in r.evidence


def test_r7_invoice_title():
    p = {"company_keywords": ["某某科技"]}
    assert C.r7_invoice_title({"invoice_seller": "北京某某科技有限公司"}, p).verdict == "PASS"
    assert C.r7_invoice_title({"invoice_seller": " 个人 "}, p).verdict == "WARN"
    assert C.r7_invoice_title({"invoice_seller": "某某餐饮"}, p).verdict == "WARN"
    assert C.r7_invoice_title({}, p).verdict == "SKIP"


def test_run_rules_order_and_disabled():
    results = C.run_rules({"amount": 150, "occurred_at": "2026-09-06", "reason": "打车",
                           "expense_type": "交通", "invoice_no": "10001",
                           "employee_id": "e1", "invoice_seller": "某某科技"},
                          history=[], config=cfg(**{"R7": {"company_keywords": ["某某科技"]}}),
                          today=TODAY)
    assert [r.rule_id for r in results] == [f"R{i}" for i in range(1, 8)]
    assert all(r.verdict in ("PASS", "SKIP") for r in results)
    c = C.default_config()
    c["R4"]["enabled"] = False
    results = C.run_rules({"expense_type": "交通", "amount": 999}, history=[],
                          config=c, today=TODAY)
    r4 = [r for r in results if r.rule_id == "R4"][0]
    assert r4.verdict == "SKIP" and "停用" in r4.evidence


def test_summarize_matrix_inputs():
    r_fail = C.run_rules({"amount": -1, "invoice_no": "12345",
                          "occurred_at": "2026-09-06"},
                         history=[{"ticket_id": "T0", "invoice_no": "12345",
                                   "occurred_at": "2026-09-01"}],
                         config=C.default_config(), today=TODAY)
    s = C.summarize(r_fail)
    assert s["overall"] == "FAIL" and "R1" in s["fail"] and "R2" in s["fail"]
    r_ok = C.run_rules({"amount": 150, "occurred_at": "2026-09-06", "reason": "地铁",
                        "expense_type": "交通", "invoice_no": "777",
                        "employee_id": "e1"}, history=[], today=TODAY)
    s = C.summarize(r_ok)
    assert s["overall"] in ("PASS", "WARN")


def test_load_config_merges_over_defaults(tmp_path):
    f = tmp_path / "rules.yaml"
    f.write_text("""
rules:
  R1:
    enabled: false
    params: {max_amount: 500}
  R4:
    params:
      limits: {交通: 999}
""", encoding="utf-8")
    c = C.load_config(f)
    assert c["R1"]["enabled"] is False
    assert c["R1"]["params"]["max_amount"] == 500
    assert c["R4"]["params"]["limits"]["交通"] == 999
    assert c["R5"]["params"]["max_age_days"] == 90


def test_cli_eval(tmp_path):
    import subprocess, sys, os
    r = subprocess.run(
        [sys.executable, "-m", "boss_secretary.compliance", "eval",
         "--ctx", '{"amount": 250, "expense_type": "交通", "occurred_at": "2026-09-06", "reason": "打车", "invoice_no": "5001"}',
         "--today", "2026-09-06"],
        capture_output=True, text=True,
        cwd=".", env={**os.environ, "PYTHONPATH": "."})
    assert r.returncode == 0
    assert "[WARN]" in r.stdout and '"overall": "WARN"' in r.stdout
