# 模块化重构地图（MODULARIZATION）

> 目标：新增一类单据从「改 ~6 个文件」降到「≤3 个文件」（schema + DocSpec + 卡片配置）；
> 消灭逐模块复制的横切代码。
> 原则：保持 stdlib + sqlite，不引入 ORM / DI 框架；已有的模块化资产不重造。

## 1. 诊断（2026-09 实测）

| 症状 | 证据 | 代价 |
|------|------|------|
| **上帝对象** | `ingress/feishu.py` 1769 行（全项目 ~20%）；`_route_command` 30+ 分支、`on_card_action` 13 分支、7 张卡片手写 dict、51 处直接 `send_text/send_card` | 任何审批/命令改动都要翻这个文件；渠道适配各写一遍 |
| **数据访问重复** | `_now()`/`_id()` 在 contract/supplier/seal/travel/allowance 各写一遍；`dict(zip(...description...))` 行解码 5 处；手写 `UPDATE...SET` 27 处 | 每加一张表复制一套；格式容易漂移 |
| **状态常量漂移** | supplier `PENDING="pending_review"` vs seal `PENDING="pending"`；5 套同名常量各自定义 | 语义不一致，隐性 bug 来源 |
| **抽取器分散** | `extract.py` 7 个 `extract_*` 函数 + allowance.py/travel.py 内联 prompt | 新增单据类型要动 3 个文件 |
| **通知散落** | 51 处发送调用直接写在 handler 分支里 | 无法统一做审计、重试、渠道降级 |

## 2. 已有模块化资产（借力，不重造）

| 资产 | 作用 |
|------|------|
| `core/specs.py` | 三要素契约（DocSpec），单据类型元数据 |
| `core/perm.py` | 角色 × 范围 × 密级，权限集中判定 |
| `core/channel.py` | Envelope 契约 + ChannelAdapter，渠道抽象 |
| `core/scheduler.py` | Job 抽象 + 后台调度 |
| `core/approvals.py` | 统一审批动作层（意见采集/记录/附送/回传） |

## 3. 重构候选

### P0-1 `core/db.py`：数据访问底座

**现状**：`_now()`/`_id(prefix)` 在 5 个模块重复；`dict(zip([c[0] for c in
conn.execute("SELECT * FROM t LIMIT 1").description], row))` 行解码 5 处；
`UPDATE ... SET ... WHERE id=?` 手写 27 处。

**目标接口**：

```python
now() -> str                                   # ISO 秒级时间戳
new_id(prefix) -> str                          # C20260910-A1B2C3
row_to_dict(cursor, row) -> dict               # 行 → dict
fetch_one(conn, table, id_col, id_val) -> dict | None
update_fields(conn, table, id_col, id_val, *, commit=True, **fields)
```

表名/列名仅允许标识符（内部常量），非法即抛 `ValueError`，杜绝拼接注入。

**收益**：删 ~10 个重复私有函数、5 处行解码，27 处 UPDATE 减半。
**风险**：低（纯抽取，现有 216 测试即回归网）。

### P0-2 `core/status.py`：状态常量唯一事实源

**现状**：5 套 `PENDING/ACTIVE/REJECTED`，且值漂移（供应商准入的 `PENDING`
实际是 `"pending_review"`）。

**设计**：按命名空间分类，模块保留原常量名做别名（数据兼容，值不改）：

```python
class Ticket:   # 报销/采购主流程（大写终态机）
    DRAFT, REVIEWING, AUTO_APPROVED, SUBMITTED, APPROVED, PAID,
    REJECTED, ESCALATED, WITHDRAWN, CANCELLED

class Doc:      # 文档类通用（小写）
    PENDING, PENDING_REVIEW, REVIEWING, ACTIVE, APPROVED, REJECTED,
    CLOSED, EXPIRED, REVOKED, SUSPENDED, BLACKLISTED, VOIDED, USED,
    RENEWED, EXPIRING, EXHAUSTED, FROZEN, PAID_OUT, OPEN

class Payment:  # 付款单
    PENDING, PAID, REJECTED
```

**收益**：新增状态先加这里；模块内不再出现重复定义。
**风险**：低（值保持不变，仅收敛定义）。

### P1-1 `core/cards.py`：卡片组件层

7 张卡片（审批/打款/合同/供应商/用印/差旅/借款/额度）从 `feishu.py` 抽出，
提供原语：`info_div()` / `history_div()` / `decision_form()` / `card()`。
飞书卡片结构作为渠道无关 IR，`card_to_text`（文本）与 `card_to_blocks`
（Slack）吃同一结构。**预计 feishu.py -500 行。**

### P1-2 ingress 分拆：动作/命令注册表

`feishu.py` 拆为 `feishu.py`（长连接与事件）+ `actions.py`（`ACTION_HANDLERS`
注册表替代 13 分支）+ `commands.py`（`@command("预算")` 注册表替代 30+ 分支）。
新增审批动作/命令不再改主文件。

### P1-3 抽取注册表

`extract.py` 增加「单据类型 → 抽取函数」注册表（动态解析模块全局，
测试 monkeypatch 依然生效）；出差内联 prompt 收编为 `extract_trip`。
ingress 只按类型取函数（`E.get_extractor("trip")`），不再散落函数名。
（DocSpec 不承载函数引用，避免 specs → extract 反向耦合。）

### P2-1 决策通知策略

「谁通知谁」原先散在各 handler。落地为 `core/policy.py`：
`DecisionNotice(label/doc_id/submitter/role/approve/comment/detail/others)` +
`emit()`——通过→提交人收正文 + 统一意见后缀；驳回→提交人 + 链路他人收统一文案。
覆盖额度/差旅/借款/用印/供应商/合同六类入口决策；报销/采购主流程保留 router
事件桥（会签进度与卡片更新逻辑不同，不硬套）。
（未做完整 ApprovalPolicy 状态机：各类型 decide 语义差异大，强抽象收益低。）

### P2-2 迁移版本化

DDL 保持集中（新库一次建全，全部 `IF NOT EXISTS`）；增量结构改为
`SCHEMA_VERSION` + `MIGRATIONS` + `PRAGMA user_version`，只跑一次，
兼容无版本号老库（列已存在则幂等跳过）。未拆「各模块 SCHEMA」：
建表顺序与单命令建库依赖集中式 DDL，拆分只有形式收益。

### P2-3 通知出口

`core/notify.py`：`send_text/send_card/send_texts`，异常隔离（单条失败只记日志，
不打断已落库的决策）+ 去重。actions / commands / 事件桥全部改走该出口。
未做重试/事件总线：stdlib + sqlite 定位下日志 + 人工补发足够，避免过度设计。

## 4. 验收标准

| 指标 | 现状 | 目标 |
|------|------|------|
| 新增单据类型改动文件数 | ~6（models/core/extract/feishu 卡片/动作/命令） | ≤3（schema + DocSpec + 卡片配置） |
| `feishu.py` 行数 | 1769 | 851（P1 完成） |
| 重复 `_now/_id/行解码` | 5+5 处 | 0（P0 完成） |
| 状态常量定义处 | 5 套 | 1（P0 完成） |
| 测试 | 216 | 245 全绿（每项重构含针对性用例） |

## 5. 不建议重构清单

- `matrix.py` / `compliance.py` / `perm.py` / `specs.py` / `channel.py` /
  `scheduler.py` / `report/`：职责已清晰，属已有模块化资产。
- 不引入 ORM / 依赖注入 / 事件总线框架：stdlib + sqlite 是部署优势（零依赖）。
- 不改现有数据库中的状态字符串值（供应商 `pending_review` 等历史数据兼容优先）。

## 6. 实施记录

### P0（本次）

- [x] `core/db.py`：now / new_id / row_to_dict / fetch_one / update_fields
- [x] `core/status.py`：Ticket / Doc / Payment 命名空间 + 各模块别名
- [x] router / contract / supplier / seal / travel / allowance 接入
- [x] 新增 `tests/test_db.py`；全量测试全绿

### P1（本次）

- [x] `core/cards.py` 卡片组件层（原语 + 9 张卡片；飞书/文本/Slack 同一 IR）
- [x] ingress 分拆：`actions.py`（16 个卡片动作注册表）+ `commands.py`（22 个私聊命令注册表）
- [x] 抽取注册表（`extract.py` 类型→函数动态解析）+ 出差提示词收编为 `extract_trip`
- [x] 新增 `tests/test_cards.py` / `test_actions.py` / `test_commands.py`
- 指标：`feishu.py` 1769 → 851 行；测试 222 → 234

### P2（本次）

- [x] `core/policy.py` 决策通知策略（六类入口决策的文案与送达统一）
- [x] `core/notify.py` 通知出口（异常隔离 + 去重），actions/commands/事件桥接入
- [x] `models.py` 版本化迁移（`SCHEMA_VERSION` + `MIGRATIONS` + `user_version`），真实旧库副本验证
- [x] 新增 `tests/test_policy.py` / `test_notify.py` + 迁移用例；全量 245 全绿
- 指标：通知文案模板 ~10 处 → policy 一处；审批决策层（actions/commands）直发 0，事件桥接入 notify
