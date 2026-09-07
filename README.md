# Boss Secretary（老板秘书系统）

AI 秘书替代传统 OA：员工在飞书私聊发起报销 → AI 抽取 + 合规审查 → **责任矩阵（DMN 决策表）**路由审批 → 状态回执 → 每日分版日报。

PRD：`docs/PRD-MVP.md`（含责任矩阵/权限密级/异常检测/本地 LLM 等全部设计决策）

## 当前进度

| 模块 | 状态 | 说明 |
|------|------|------|
| `boss_secretary/core/matrix.py` | ✅ | 责任矩阵评估器：DMN 语义决策表 + Hit Policy（FIRST 运行时 / UNIQUE 导入校验）+ FEEL-lite 条件 + 版本化，纯函数可离线回放 |
| `boss_secretary/core/matrix_import.py` | ✅ | Excel 导入器：三表结构（责任矩阵/枚举字典/变更记录）→ 校验（枚举/重叠/兜底）→ 边界值网格干跑（新旧动作分布对比）→ commit 落库（版本递增+旧版退役+unified diff 入 audit）；≤/≥ 等符号自动归一化 |
| `boss_secretary/core/compliance.py` | ✅ | 规则引擎 R1-R7（PRD §6.1）：金额/重复发票/疑似重复/类型上限（支持人均摊）/日期/必填/抬头，纯函数（历史注入不触库），`config/rules.yaml` 全参数可配可停用 |
| `boss_secretary/core/router.py` | ✅ | 流程引擎（PRD §4）：9 态状态机 + 审查编排（compliance→summarize→矩阵）+ 会签（并行网关）+ 超时升级（老板终裁）+ 撤回/作废/驳回重提（关联原单）+ fail-safe（LLM 缺失降 WARN、规则阻断压过直批）+ 全程审计 |
| `boss_secretary/report/daily.py` | ✅ | 日报分版渲染（PRD §2.1/F10）：公司版（老板）/部门版（经理，机密单只计数）/财务版（待付清单降序），权限经 perm 裁剪，`send_all` 对接通知协议，CLI 直出公司版 |
| `boss_secretary/core/anomaly.py` | ✅ | 批量异常检测（PRD §6.4）：A1 费用趋势（全司/部门/类型/员工四层，同比+3月滚动均值±2σ 双判据，贡献度下钻，连续 2 期升 ALERT）+ A2 单价偏离（±20%/±50% 双向，采购场景就绪）+ 抽检策略（标准品全量/非标品 TOP20%+随机）+ 结果落 anomalies 表；基线<3月不启用 | |
| `boss_secretary/core/perm.py` | ✅ | 权限层：角色×数据范围×单据密级；查询/日报/导出统一入口；机密单对经理只返回占位 |
| `boss_secretary/models.py` | ✅ | SQLite schema + audit_log append-only 触发器 |
| `tests/redteam/prompts.yaml` | ✅ | 20 条 LLM 越权红队用例（E2E 阶段执行，验收=0 泄露） |
| `ingress/feishu.py` | ⏳ | 等飞书自建应用凭证 |
| `core/extract.py` / `compliance.py` / `router.py` | ⏳ | 等本地 vLLM 端点（Qwen3.8-27B） |
| `report/daily.py` / `finance/export.py` | ⏳ | P1 |
| Excel 矩阵导入器 | ⏳ | `matrix.xlsx → yaml`（含 UNIQUE 校验+干跑报告） |

## 快速开始

```bash
pip install -r requirements.txt
pytest

# 责任矩阵全流程
python3 -m boss_secretary.matrix export config/matrix/reimburse_v1.yaml --xlsx config/matrix/reimburse_v1.xlsx  # 生成老板可编辑的 Excel
python3 -m boss_secretary.matrix import config/matrix/reimburse_v1.xlsx --grid     # 校验+边界值网格干跑
python3 -m boss_secretary.matrix import config/matrix/reimburse_v1.xlsx --replay data/replay.jsonl  # 用历史单据回放对比新旧
python3 -m boss_secretary.matrix commit config/matrix/reimburse_v1.xlsx --db data/secretary.db --operator boss  # 落库激活（版本递增+diff 入审计）

# 建库（audit_log append-only 由触发器强制）
python3 -m boss_secretary.models data/secretary.db

# 规则引擎单测（R1-R7，历史单据由调用方注入）
python3 -m boss_secretary.compliance eval --ctx '{"amount":98,"expense_type":"交通","occurred_at":"2026-09-05","reason":"打车","invoice_no":"5001"}' --today 2026-09-06
```

## 部署形态

- LLM：本地优先，vLLM 内网 OpenAI 兼容端点（默认 Qwen3.8-27B 量化，多模态覆盖发票图片）；云端 GLM 仅应急后手（settings 默认关闭）
- 通道：飞书长连接（WebSocket，无需公网 IP）
- 安全：权限过滤先于 LLM 上下文注入；audit 全量留痕 append-only；上线前红队 0 泄露
