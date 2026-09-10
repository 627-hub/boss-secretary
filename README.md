# Boss Secretary（老板秘书系统）

一句话：**员工在飞书私聊发起报销/采购/合同/用印/差旅/借款，AI 完成抽取与合规审查，按你公司自己的责任矩阵走完审批、打款与归档**——替代传统 OA 的轻量内控系统。

## 两个核心设计

**流程可本地化适配** —— 审批链由 DMN 责任矩阵决定（Excel 编辑、版本化、干跑预览）。金额分级、审批人、AI 直批阈值，改成你公司的规则即可上线；流程编排（YAML）与决策规则（矩阵）分离，**改规则不改代码**。

```text
老板改 Excel → boss-matrix import → 干跑报告(新旧对比) → commit 生效
```

**功能模块可添加** —— 一切审核的本质是三要素契约（`core/specs.py`）：**预算（事前）× 事由/标的（发生）× 交付物文件（发票/报价单/合同）**。新增一类单据 = 一份 DocSpec + 一个抽取函数，摄取/追问/附件门/预算检查/审计管线零改动。现有报销/采购/合同/用印/差旅/借款六类即按此模式扩展。

| 三要素 | 报销 | 采购 | 合同 | 用印 |
|--------|------|------|------|------|
| 预算 | 部门/全司预算 + 额度优先核销 | 部门/全司预算 | 登记时占用检查 | — | 出差预估+报销联动 | 借款=待冲销负债 |
| 事由/标的 | 事由 reason | 采购标的 title | 合同标的 title | 用印文件 | 出差事由+期间 | 借款事由 |
| 交付物 | 发票（图片/PDF/验真） | 合同/PO/报价单 | 合同文件 PDF（全文 AI 审查） | 用印文件 | 差旅申请单（事前） | 借款事由+放款凭证 |

**当前版本 [v1.0.0](https://github.com/627-hub/boss-secretary/releases)** · 245 个测试全绿

PRD 全文：`docs/PRD-MVP.md`（责任矩阵语义、权限密级模型、异常检测、LLM 数据隔离等设计决策）

## 功能总览

| 能力 | 说明 |
|------|------|
| AI 抽取 | 自然语言/发票图片（视觉模型）/PDF 电子发票（含电子签章检测）→ 结构化单据；缺失自动追问，补充自动并单 |
| 合规审查 | 规则引擎 R1-R7（金额/重复发票/疑似重复/类型上限/日期/必填/抬头）+ LLM 复核（证据链），**fail-safe：AI 不可用降级人工** |
| 发票验真 | 三层免费验真：结构校验（数电票20位/旧版规则）+ **二维码交叉核验**（税控QR↔OCR互核）+ PDF电子签章；税局真查验留第三方 adapter |
| 责任矩阵 | DMN 决策表（Excel 维护 + Hit Policy + 版本化 + 干跑报告），审批链/直批阈值老板可改 |
| 预算额度 | 员工申请 → 经理/老板批额度 → 额度内报销免逐单审批（审查不豁免）→ 自动核销扣减 |
| 预算检查 | 部门/全司月度预算（Excel 批量导入），额度审批+报销提交双检查点 |
| 全生命周期 | 发起→审批（会签/超时升级老板终裁）→ **打款确认才关闭**；撤回/作废/驳回重提 |
| 审批意见 | 同意可附意见，自动附送下一级查阅；驳回意见回传提交人与链路其他审批人（全审批类型卡片输入，回放包明文留痕） |
| 日报三版 | 公司版(老板)/部门版(经理，机密单只计数)/财务版(待付清单)，每天 18:00 自动推送 |
| 异常检测 | 月度扫查：费用趋势（四层下钻+贡献归因）+ 单价偏离（双向），ALERT 即时推送 |
| 商务模块 | 采购（复用审批流+独立矩阵）→ 合同登记+AI 法务审查（legal+boss 会签）→ 分期付款 → 到期提醒/一键续签 |
| 供应商主数据 | 准入会签(finance+boss)、银行账户变更强制重审批+变更台账、黑名单三道拦截闸（采购/合同/付款拒绝、报销开票方确认门） |
| 用印管理 | 印章台账(公章/合同章/财务章，保管人)、空白文件硬拒绝、合同章必关联已生效合同、审批按章类型路由、台账永久留痕(自批标注) |
| 差旅事前 | 出差申请(目的地/期间/预估)→审批→期间内报销自动关联，超预估 20% 进确认门 |
| 借款台账 | 申请→批准放款→报销冲销/财务核销→自动关闭；逾期 60 天月度提醒 |
| 内部审计 | AI 风险评分抽检队列(6因子)→回放包(append-only 证据链)→误报/属实闭环→风险名单；审计工作台命令(仅审计/老板) |
| 可靠性 | 心跳看门狗(独立进程，掉线飞书直告+恢复通知) + 每日自动备份(SQLite 在线 backup+审计目录，保留 N 份) |
| Telegram 渠道 | 第二渠道验证：长轮询无公网 IP，卡片降级为文本，与飞书共用同一引擎/权限/审计 |
| 企业微信渠道 | 自建应用+隧道方案：回调服务器(内网)+AES 加解密+主动发消息；管理后台需配可信 IP |
| Slack 渠道 | Socket Mode 长连接（无公网 IP）+ Block Kit 原生按钮卡片 + 文件收发；海外/出海团队首选 |
| AI 归因月报 | 月报头部 LLM 生成经营简述(只用给定数字)，LLM 不可用静默跳过 |
| 统一三要素 | 预算×事由/标的×交付物（DocSpec 契约）：所有审核类型共用摄取/追问/附件门/预算检查管线 |
| 安全 | 权限过滤先于 LLM 上下文、机密单经理只见占位、审计 append-only、密钥入 Keychain 零明文、20 条红队用例 |

## 下载

```bash
git clone https://github.com/627-hub/boss-secretary.git
cd boss-secretary
```

（或 Releases 页下载 zip 解压；纯 Python，无需编译。）

## 快速部署（约 15 分钟）

### 0. 前置要求

- Python ≥ 3.10
- 飞书**自建应用**（5 分钟，见下）
- LLM：OpenRouter key（免费模型即可跑）或内网 vLLM

### 1. 安装

```bash
pip install -e ".[all]"   # 含 Slack/企微/月报/发票二维码/PDF 可选依赖；最小安装用 -e .
pytest        # 245 个测试，应全绿
```

### 2. 飞书自建应用（[open.feishu.cn](https://open.feishu.cn) → 开发者后台）

1. 创建**企业自建应用** → 记下 `App ID` / `App Secret`
2. **添加应用能力**：机器人
3. **事件订阅**：订阅方式选「**长连接**」；添加事件「接收消息 im.message.receive_v1」
4. **权限管理**：开通 `im:message`（收发消息）、`im:resource`（下载图片/文件）、`contact:user.base:readonly`
5. **版本管理与发布**：创建版本并发布（自建应用管理员秒过）

### 3. 配置

```bash
cp config/settings.example.yaml config/settings.yaml
```

编辑 `config/settings.yaml`：填入 `feishu.app_id`；`feishu.roles.{boss,manager,finance}` 先留空（第 5 步拿 open_id）；LLM 二选一：

```yaml
llm:
  provider: cloud            # 或 local（vLLM 内网端点）
  cloud:
    base_url: "https://openrouter.ai/api/v1"
    api_key: ""              # 留空，下一步存 Keychain
    model_extract: "minimax/minimax-m3:free"
    model_vision: "minimax/minimax-m3:free"
```

密钥**不写文件**，存系统 Keychain：

```bash
boss-secrets set llm.cloud.api_key      # 粘贴 key（输入不回显）
boss-secrets set feishu.app_secret      # 粘贴 App Secret
boss-secrets list                       # 应显示两项已存(Keychain)
```

### 4. 初始化数据

```bash
boss-models data/secretary.db                                   # 建库（审计 append-only）
boss-matrix export config/matrix/reimburse_v1.yaml --xlsx config/matrix/reimburse_v1.xlsx
boss-matrix commit config/matrix/reimburse_v1.xlsx --db data/secretary.db   # 责任矩阵激活
boss-budget template --out config/budgets.xlsx                  # 预算模板(可选)
boss-budget import config/budgets.xlsx --db data/secretary.db   # 预算导入(可选)
```

### 4b. 可选：第二渠道（Telegram / 企业微信）

```bash
# Telegram（长轮询，无需公网 IP）：@BotFather 建 bot 拿 token
boss-secrets set channels.telegram.bot_token
boss-telegram
```

企业微信：自建应用+隧道方案（回调走公网隧道转回内网，管理后台需配可信 IP），
配置见 `config/settings.example.yaml` 的 `channels.wecom`，`boss-wecom` 启动。

### 5. 启动并拿 open_id

```bash
boss-feishu        # 长连接启动（无需公网 IP）
```

用**你自己的飞书**私聊机器人（搜应用名）发一句 `进度` —— 应回复「暂无报销单」；同时终端日志打印 `sender=ou_xxx`。让老板/经理/财务各私聊一次，把三个 `ou_xxx` 填进 `config/settings.yaml` 的 `feishu.roles`，重启 `boss-feishu`。

### 6. 验收

在飞书里依次试：

| 你发 | 预期 |
|------|------|
| `9月5号打车98块，滴滴出行发票` | 全绿 ≤500 → 「✅ 已自动通过」，待财务打款 |
| `[发一张发票图片]` | 识别要素 → 追问事由 → 回复后自动建单 |
| `申请打车额度200元，加班用` | 额度审批卡片 → 批准后额度内报销免逐单审批 |
| `预算 * 2026-09 100000` | 设置全司预算；`预算` 看总览 |
| 审批卡片点同意/财务点确认打款 | PAID 终态，员工收到「已打款，本单关闭」 |
| `审计` / `抽检` / `回放 T-xxx` | 审计工作台（仅审计/老板） |
| `出差申请 上海5天 预计3000元` | 出差审批→期间报销自动关联 |
| `借款申请 2000元 出差备用金` | 借款审批→放款→核销→逾期提醒 |
| 每天 18:00 / 每月 1 日 | 日报三版 / 异常扫查+月报 docx+pptx 自动推送 |

## 日常运维

```bash
nohup python3 -u -m boss_secretary.feishu run > data/feishu_bot.log 2>&1 &   # 飞书服务
nohup python3 -u -m boss_secretary.watchdog --interval 600 > data/watchdog.log 2>&1 &   # 看门狗(掉线告警, 与服务分开跑)
boss-daily --db data/secretary.db          # 手动日报
boss-voucher export --month 2026-09        # 金蝶凭证 CSV
boss-report --month 2026-09                # 月度报告 docx+pptx
tail -f data/feishu_bot.log                # 看实时日志
kill $(pgrep -f "boss_secretary.feishu|boss_secretary.watchdog")              # 停
```

建议用 launchd/systemd 托管常驻。数据全在本地：`data/secretary.db`（业务）+ `data/audit/`（审计/回放包），记得纳入备份。

## 架构与模块

| 模块 | 职责 |
|------|------|
| `core/matrix.py` + `matrix_import.py` | DMN 责任矩阵（评估器 + Excel 导入/干跑/版本化落库） |
| `core/compliance.py` | 规则引擎 R1-R7（纯函数可回放，阈值全可配） |
| `core/router.py` | 状态机 + 会签 + 超时升级 + 打款终态 + 审计 |
| `core/perm.py` | 角色×范围×密级（权限先于 LLM 上下文） |
| `core/approvals.py` | 统一审批动作层（意见采集/记录/附送/回传，全审批类型共用） |
| `core/extract.py` | AI 抽取（文本/图片/PDF）+ LLM 复核 + 交叉核验 |
| `core/allowance.py` / `budget.py` | 额度授权核销 / 预算检查 + Excel 导入 |
| `core/anomaly.py` | 月度异常扫查（趋势四层下钻 + 单价偏离） |
| `core/travel.py` | 差旅事前申请 + 借款台账 |
| `core/audit.py` | 内部审计：风险评分/抽检队列/回放包/风险名单 |
| `core/specs.py` | 统一审核三要素契约（DocSpec） |
| `core/db.py` / `core/status.py` | 数据访问共用件（时间戳/ID/行解码/字段更新）+ 状态常量唯一事实源 |
| `core/cards.py` | 渠道无关卡片组件层（飞书 JSON 作为 IR，文本/Slack 同源渲染） |
| `ingress/actions.py` / `commands.py` | 卡片动作注册表（16）/ 私聊命令注册表（22），ingress 分拆 |
| `core/policy.py` / `core/notify.py` | 决策通知策略（谁收到什么）+ 通知出口（异常隔离/去重） |
| `core/scheduler.py` | 定时任务（日报/月报/扫查/过期/超时/备份/逾期提醒） |
| `core/channel.py` | 渠道抽象（Envelope 契约 + ChannelAdapter + 会话分发） |
| `ingress/telegram.py` / `slack.py` / `wecom.py` | Telegram / Slack（Socket Mode）/ 企业微信 |
| `report/daily.py` | 日报三版渲染 |
| `ingress/feishu.py` | 长连接收单 + 卡片审批 + 事件桥 |
| `core/llm.py` + `secrets.py` | OpenAI 兼容客户端（OpenRouter/vLLM/GLM）+ Keychain 密钥 |

**当前版本 [v1.0.0](https://github.com/627-hub/boss-secretary/releases)** · 245 个测试全绿

PRD 全文：`docs/PRD-MVP.md`（责任矩阵语义、权限密级模型、异常检测方法、LLM 数据隔离等设计决策）。

## 安全模型

- **密钥**：`feishu.app_secret` / `llm.*.api_key` 存 macOS Keychain，`config/settings.yaml` 零明文；`boss-secrets migrate/set/get` 管理
- **LLM 数据隔离**：权限过滤发生在数据进模型之前——员工越狱的收益上限=其自身权限；机密单对经理只返回占位
- **审计**：`audit_log` 触发器级 append-only；每单可导出回放包（原始消息→抽取→规则→LLM→人工操作）
- **诚实边界**：Keychain 防磁盘盗取/备份扩散/仓库误提交，不防同用户恶意进程；飞书通道本身是第三方（更高级别保密需自建通道，见 PRD §L2）

## Roadmap

- [ ] 发票税局真查验（需第三方付费接口，adapter 已留位）
- [ ] 金蝶/用友 API 直连（当前标准 CSV 引入）
- [x] 企业微信渠道（v1.0.1 隧道方案）
- [ ] 邮件/钉钉入口、语音受理
- [ ] 本地 vLLM 部署指引（Qwen3.8-27B，数据完全不出内网）
- [ ] SoD 规则库系统化 / 制度 RAG 问答 / 收入侧内控

## 开发模式与归属

本项目由 627-hub 发起并主导（需求定义、设计决策、验收测试、生产运维），
AI（GLM / Claude Code）作为编程助手参与实现。版权与许可证权利归 627-hub 所有。

## 免责

仅供研究学习与内部工具使用；输出不构成财务/法务意见。量化筛查有固有误报/漏报，最终判断请结合原始单据核实。

**Boss Secretary 公开源许可证**（非商业免费；商用须以同协议开源衍生作品，或取得版权人商业授权——详见 `LICENSE`）
