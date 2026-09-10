import pytest

from boss_secretary.core import invoice_verify as IV


def test_structural_dedp_valid():
    inv = {"invoice_no": "25000000000000000000", "invoice_date": "2025-12-15",
           "invoice_amount": 469, "invoice_seller": "测试商户"}
    out = IV.structural_check(inv)
    assert any(i["level"] == "PASS" and i["rule"] == "数电票号码" for i in out)


def test_structural_bad_number_is_fail():
    out = IV.structural_check({"invoice_no": "12345", "invoice_date": "2026-09-05",
                               "invoice_amount": 98, "invoice_seller": "滴滴"})
    assert any(i["level"] == "FAIL" and i["rule"] == "号码结构异常" for i in out)


def test_structural_old_style_year_mismatch():
    # 10位代码: 地区4位+年份2位(19→2019)+批次+票种
    inv = {"invoice_code": "039991900111", "invoice_no": "87654321",
           "invoice_date": "2026-09-05", "invoice_amount": 88.66}
    out = IV.structural_check(inv)
    assert any(i["level"] == "FAIL" and "不一致" in i["rule"] for i in out)


def test_qr_csv_format_parse_and_crosscheck():
    qr = IV.parse_invoice_qr_payload("01,10,039991900111,87654321,88.66,20200518,12345")
    assert qr["invoice_code"] == "039991900111"
    assert qr["invoice_no"] == "87654321"
    assert qr["amount"] == 88.66
    assert qr["invoice_date"] == "2020-05-18"
    issues = IV.cross_check_qr({"invoice_no": "87654321", "amount": 88.66}, qr)
    assert issues == []
    bad = IV.cross_check_qr({"invoice_no": "87654321", "amount": 999}, qr)
    assert bad and bad[0]["level"] == "FAIL" and "金额" in bad[0]["rule"]


def test_qr_json_and_dedp_url_formats():
    j = IV.parse_invoice_qr_payload('{"发票号码": "25000000000000000000", "金额": 469}')
    assert j["invoice_no"] == "25000000000000000000" and j["amount"] == 469
    u = IV.parse_invoice_qr_payload("https://inv-veri.chinatax.gov.cn/x?id=25000000000000000000&date=2025-12-15")
    assert u["invoice_no"] == "25000000000000000000"
    assert u["invoice_date"] == "2025-12-15"


def test_verify_overall_and_provider_skip():
    inv = {"invoice_no": "25000000000000000000", "invoice_date": "2025-12-15",
           "invoice_amount": 469, "invoice_seller": "测试商户"}
    r = IV.verify(inv, image_bytes=None, settings=None)
    assert r["overall"] in ("PASS", "WARN")
    assert r["provider"]["level"] == "SKIP"
    r2 = IV.verify({"invoice_no": "12345", "invoice_amount": None},
                   settings=None)
    assert r2["overall"] == "FAIL"


def test_to_text_render():
    r = IV.verify({"invoice_no": "25000000000000000000", "invoice_date": "2025-12-15",
                   "invoice_amount": 469, "invoice_seller": "测试商户"}, settings=None)
    text = IV.to_text(r)
    assert "数电票号码" in text and "第三方查验" in text
