"""责任矩阵评估器（DMN 语义：决策表 + Hit Policy + FEEL-lite 条件子集）。

纯函数、无 IO：evaluate()/validate() 可单测、可离线回放（PRD §7）。
CLI:
  python3 -m boss_secretary.matrix lint  config/matrix/reimburse_v1.yaml
  python3 -m boss_secretary.matrix eval  config/matrix/reimburse_v1.yaml --ctx '{"amount":300,...}'
  python3 -m boss_secretary.matrix show  config/matrix/reimburse_v1.yaml
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

ANY = "*"
FIRST = "FIRST"
UNIQUE = "UNIQUE"
FALLBACK_ACTION = "MANUAL_REVIEW"

_COND_TRANS = str.maketrans({"≤": "<=", "≥": ">=", "≦": "<", "≧": ">",
                             "＝": "==", "－": "-"})


def _normalize_cond(s: str) -> str:
    return unicodedata.normalize("NFKC", s).translate(_COND_TRANS).strip()

_NUM = r"-?\d+(?:\.\d+)?"
_RANGE_RE = re.compile(rf"^({_NUM})\s*-\s*({_NUM})$")
_CMP_RE = re.compile(rf"^(<=|>=|==|<|>)\s*({_NUM})$")
_SET_RE = re.compile(r"^in\s*[（(](.+)[）)]$", re.S)


class MatrixError(Exception):
    pass


class MatrixLintError(MatrixError):
    pass


class NoMatchError(MatrixError):
    pass


class UniqueViolationError(MatrixError):
    pass


@dataclass(frozen=True)
class Interval:
    lo: tuple[float, bool]
    hi: tuple[float, bool]

    def overlaps(self, other: "Interval") -> bool:
        if self.hi[0] < other.lo[0] or other.hi[0] < self.lo[0]:
            return False
        if self.hi[0] == other.lo[0] and not (self.hi[1] and other.lo[1]):
            return False
        if other.hi[0] == self.lo[0] and not (other.hi[1] and self.lo[1]):
            return False
        return True


_FULL = Interval((float("-inf"), False), (float("inf"), False))


@dataclass(frozen=True)
class AnyCell:
    raw: str = ANY

    def matches(self, value: Any) -> bool:
        return True

    def interval(self) -> Interval:
        return _FULL

    def vals(self) -> tuple:
        return ()


@dataclass(frozen=True)
class CompareCell:
    op: str
    value: float
    raw: str

    def matches(self, value: Any) -> bool:
        if value is None:
            return False
        v = float(value)
        return {"<=": v <= self.value, ">=": v >= self.value, "<": v < self.value,
                ">": v > self.value, "==": v == self.value}[self.op]

    def interval(self) -> Interval:
        neg, pos = float("-inf"), float("inf")
        if self.op == "<=":
            return Interval((neg, False), (self.value, True))
        if self.op == "<":
            return Interval((neg, False), (self.value, False))
        if self.op == ">=":
            return Interval((self.value, True), (pos, False))
        if self.op == ">":
            return Interval((self.value, False), (pos, False))
        return Interval((self.value, True), (self.value, True))

    def vals(self) -> tuple:
        return ()


@dataclass(frozen=True)
class RangeCell:
    lo: float
    hi: float
    raw: str

    def matches(self, value: Any) -> bool:
        if value is None:
            return False
        v = float(value)
        return self.lo <= v <= self.hi

    def interval(self) -> Interval:
        return Interval((self.lo, True), (self.hi, True))

    def vals(self) -> tuple:
        return ()


@dataclass(frozen=True)
class SetCell:
    values: tuple[str, ...]
    raw: str

    def matches(self, value: Any) -> bool:
        if value is None:
            return False
        return str(value) in self.values or value in self.values

    def interval(self) -> Interval:
        raise MatrixError("集合列无区间语义")

    def vals(self) -> tuple:
        return self.values


@dataclass(frozen=True)
class ExactCell:
    values: tuple[str, ...]
    raw: str

    def matches(self, value: Any) -> bool:
        if value is None:
            return False
        return str(value) in self.values or value in self.values

    def interval(self) -> Interval:
        raise MatrixError("精确值列无区间语义")

    def vals(self) -> tuple:
        return self.values


def parse_cell(raw: Any, col_type: str = "string"):
    if raw is None:
        return AnyCell()
    s = _normalize_cond(str(raw))
    if s == "" or s == ANY:
        return AnyCell()
    if col_type == "number":
        m = _RANGE_RE.match(s)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            if lo > hi:
                raise MatrixLintError(f"数字区间左右颠倒: {s}")
            return RangeCell(lo=lo, hi=hi, raw=s)
        m = _CMP_RE.match(s)
        if m:
            return CompareCell(op=m.group(1), value=float(m.group(2)), raw=s)
        try:
            return CompareCell(op="==", value=float(s), raw=s)
        except ValueError:
            raise MatrixLintError(f"数字列条件无法解析: {s}") from None
    m = _SET_RE.match(s)
    if m:
        vals = tuple(v.strip() for v in re.split(r"[,，、]", m.group(1)) if v.strip())
        if not vals:
            raise MatrixLintError(f"集合条件为空: {s}")
        return SetCell(values=vals, raw=s)
    return ExactCell(values=(s,), raw=s)


@dataclass(frozen=True)
class Rule:
    id: str
    cells: Mapping[str, Any]
    action: str
    notify: tuple[str, ...] = ()


@dataclass(frozen=True)
class Matrix:
    name: str
    version: int
    hit_policy: str
    inputs: Mapping[str, dict]
    rules: tuple[Rule, ...]
    source: str = ""


def from_dict(data: dict, source: str = "") -> Matrix:
    name = data.get("matrix") or data.get("name")
    if not name:
        raise MatrixLintError("缺少 matrix 名称")
    version = int(data.get("version", 0))
    if version <= 0:
        raise MatrixLintError("version 必须为正整数")
    policy = str(data.get("hit_policy", FIRST)).upper()
    if policy not in (FIRST, UNIQUE):
        raise MatrixLintError(f"hit_policy 仅支持 {FIRST}/{UNIQUE}: {policy}")
    inputs = {}
    for it in data.get("inputs", []):
        col = it["name"]
        inputs[col] = {"type": it.get("type", "string"), "enum": it.get("enum"),
                       "header": it.get("header")}
    if not inputs:
        raise MatrixLintError("inputs 为空")
    rules = []
    for i, r in enumerate(data.get("rules", []), 1):
        rid = str(r.get("id") or f"R{i:02d}")
        then = r.get("then") or {}
        action = then.get("action")
        if not action:
            raise MatrixLintError(f"{rid}: 缺少 then.action")
        notify = tuple(then.get("notify") or ())
        cells = {}
        for col, raw in (r.get("cells") or {}).items():
            if col not in inputs:
                raise MatrixLintError(f"{rid}: cells 引用未声明输入列 {col}")
            cells[col] = parse_cell(raw, inputs[col]["type"])
        rules.append(Rule(id=rid, cells=cells, action=action, notify=notify))
    if not rules:
        raise MatrixLintError("rules 为空")
    return Matrix(name=name, version=version, hit_policy=policy,
                  inputs=inputs, rules=tuple(rules), source=source)


def load(path: str | Path) -> Matrix:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return from_dict(data, source=str(p))


def _cells_overlap(a, b) -> bool:
    if isinstance(a, AnyCell) or isinstance(b, AnyCell):
        return True
    num = (CompareCell, RangeCell)
    if isinstance(a, num) and isinstance(b, num):
        return a.interval().overlaps(b.interval())
    if isinstance(a, num) or isinstance(b, num):
        return False
    av, bv = a.vals(), b.vals()
    if not av or not bv:
        return True
    return bool(set(av) & set(bv))


def _rule_overlap(r1: Rule, r2: Rule) -> bool:
    shared = set(r1.cells) | set(r2.cells)
    for col in shared:
        c1 = r1.cells.get(col)
        c2 = r2.cells.get(col)
        if c1 is None or c2 is None:
            continue
        if not _cells_overlap(c1, c2):
            return False
    return True


def lint(m: Matrix) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    errors: list[str] = []
    warnings: list[str] = []
    overlaps: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    seen_cells: dict[tuple, str] = {}
    for r in m.rules:
        if r.id in seen_ids:
            errors.append(f"规则 id 重复: {r.id}")
        seen_ids.add(r.id)
        sig = tuple(sorted((c, getattr(cell, "raw")) for c, cell in r.cells.items()))
        if not sig:
            continue
        if sig in seen_cells:
            errors.append(f"{r.id} 与 {seen_cells[sig]} 条件完全相同（死规则）")
        else:
            seen_cells[sig] = r.id
        for col, cell in r.cells.items():
            enum = (m.inputs.get(col) or {}).get("enum")
            if enum and cell.vals():
                bad = [v for v in cell.vals() if v not in enum]
                if bad:
                    errors.append(f"{r.id}: 列 {col} 取值 {bad} 不在枚举 {enum} 内")
    for i in range(len(m.rules)):
        for j in range(i + 1, len(m.rules)):
            r1, r2 = m.rules[i], m.rules[j]
            if _rule_overlap(r1, r2):
                overlaps.append((r1.id, r2.id))
                if m.hit_policy == UNIQUE:
                    errors.append(f"UNIQUE 冲突: {r1.id} ∩ {r2.id} 存在共同命中输入")
    if not any(all(isinstance(c, AnyCell) for c in r.cells.values()) for r in m.rules):
        warnings.append("缺少兜底行（全 * 行）：运行时未命中将回落 MANUAL_REVIEW")
    return errors, warnings, overlaps


@dataclass(frozen=True)
class Decision:
    matrix_name: str
    matrix_version: int
    hit_rule_id: str | None
    action: str
    notify: tuple[str, ...]
    matched_rows: tuple[str, ...]


def rule_matches(rule: Rule, ctx: Mapping[str, Any]) -> bool:
    return all(cell.matches(ctx.get(col)) for col, cell in rule.cells.items())


def evaluate(m: Matrix, ctx: Mapping[str, Any]) -> Decision:
    matched: list[Rule] = []
    for r in m.rules:
        if rule_matches(r, ctx):
            matched.append(r)
            if m.hit_policy == FIRST:
                break
    if not matched:
        raise NoMatchError(f"{m.name} v{m.version}: 无命中行, ctx={dict(ctx)}")
    if m.hit_policy == UNIQUE and len(matched) > 1:
        raise UniqueViolationError(
            f"{m.name} v{m.version}: 多行命中 {tuple(r.id for r in matched)}")
    hit = matched[0]
    return Decision(matrix_name=m.name, matrix_version=m.version,
                    hit_rule_id=hit.id, action=hit.action, notify=hit.notify,
                    matched_rows=tuple(r.id for r in matched))


def to_table(m: Matrix) -> str:
    cols = list(m.inputs.keys())
    header = ["规则", *cols, "→ 动作", "抄送"]
    lines = ["| " + " | ".join(header) + " |",
             "|" + "---|".join([""] * (len(header) + 1))]
    for r in m.rules:
        row = [r.id]
        for c in cols:
            row.append(r.cells[c].raw if c in r.cells else ANY)
        row.append(r.action)
        row.append(",".join(r.notify) or "-")
        lines.append("| " + " | ".join(row) + " |")
    lines.append(f"\nhit_policy={m.hit_policy}  version=v{m.version}  rules={len(m.rules)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="boss_secretary.matrix")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("lint", "eval", "show"):
        sp = sub.add_parser(name)
        sp.add_argument("yaml_path")
        if name == "eval":
            sp.add_argument("--ctx", required=True, help="单据上下文 JSON")
    imp = sub.add_parser("import", help="xlsx → 校验 → (写 yaml / 干跑)")
    imp.add_argument("xlsx_path")
    imp.add_argument("--out", help="编译后 yaml 输出路径（默认同名 .yaml）")
    imp.add_argument("--yes", action="store_true", help="写入 yaml 文件")
    imp.add_argument("--replay", help="JSONL 回放文件（每行一个 ctx）")
    imp.add_argument("--old", help="旧版矩阵 yaml（干跑对比）")
    imp.add_argument("--grid", action="store_true", help="用边界值网格合成干跑样本")
    exp = sub.add_parser("export", help="yaml → 可编辑 xlsx 模板")
    exp.add_argument("yaml_path")
    exp.add_argument("--xlsx", required=True)
    com = sub.add_parser("commit", help="xlsx 校验通过后落库激活（版本递增+审计）")
    com.add_argument("xlsx_path")
    com.add_argument("--db", default="data/secretary.db")
    com.add_argument("--operator", default="boss")
    com.add_argument("--audit-dir", default="data/audit")
    args = p.parse_args(argv)

    if args.cmd in ("import", "export", "commit"):
        from boss_secretary.core import matrix_import as MI
        if args.cmd == "export":
            m = load(args.yaml_path)
            out = MI.export_to_xlsx(m, args.xlsx)
            print(f"模板已生成: {out}（Sheet1 编辑后 import/commit）")
            return 0
        if args.cmd == "commit":
            v = MI.commit(args.xlsx_path, args.db, args.operator, args.audit_dir)
            print(f"已激活: v{v}（旧版退役，diff 已写入 audit）")
            return 0
        px = MI.read_xlsx(args.xlsx_path)
        m, errors, warnings, overlaps = MI.validate(px)
        for e in errors:
            print(f"[FAIL] {e}")
        for w in warnings:
            print(f"[WARN] {w}")
        print(f"[INFO] v{m.version} 共 {len(m.rules)} 行，重叠 {len(overlaps)} 组"
              f"（{m.hit_policy} 策略下行序即优先级）")
        if args.replay or args.grid:
            old = load(args.old) if args.old else None
            ctxs = ([json.loads(l) for l in open(args.replay, encoding="utf-8")
                     if l.strip()] if args.replay else MI.boundary_contexts(m))
            print()
            print(MI.dry_run(m, old, ctxs))
        if errors:
            return 1
        if args.yes:
            out = Path(args.out or Path(args.xlsx_path).with_suffix(".yaml"))
            from boss_secretary.core.matrix_import import xlsx_to_matrix_dict
            out.write_text(yaml.safe_dump(xlsx_to_matrix_dict(px), allow_unicode=True,
                                          sort_keys=False), encoding="utf-8")
            print(f"[OK] 编译结果写入 {out}")
        else:
            print("[DRY] 加 --yes 写出 yaml；确认无误后用 commit 落库激活")
        return 0

    m = load(args.yaml_path)
    if args.cmd == "show":
        print(to_table(m))
        return 0
    if args.cmd == "lint":
        errors, warnings, overlaps = lint(m)
        for e in errors:
            print(f"[FAIL] {e}")
        for w in warnings:
            print(f"[WARN] {w}")
        print(f"[INFO] 重叠行对 {len(overlaps)} 组: {overlaps}"
              f"（{m.hit_policy} 策略下由行序决定优先级）" if m.hit_policy == FIRST
              else f"[INFO] 重叠行对 {len(overlaps)} 组")
        return 1 if errors else 0
    ctx = json.loads(args.ctx)
    try:
        d = evaluate(m, ctx)
    except NoMatchError:
        print(json.dumps({"action": FALLBACK_ACTION, "hit_rule_id": None,
                          "note": "未命中任何行，回落人工复核"}, ensure_ascii=False))
        return 0
    except UniqueViolationError as e:
        print(f"[FAIL] {e}")
        return 2
    print(json.dumps({"hit_rule_id": d.hit_rule_id, "action": d.action,
                      "notify": list(d.notify), "matrix_version": d.matrix_version,
                      "matched_rows": list(d.matched_rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
