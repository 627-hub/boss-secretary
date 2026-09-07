# Boss Secretary（老板秘书系统）

AI 秘书替代传统 OA：员工在飞书私聊发一句话/一张发票图 → AI 抽取 + 合规审查 → **责任矩阵（DMN 决策表）**路由审批 → 卡片审批/会签 → 财务打款确认 → 单据关闭。全程审计留痕。

```
员工私聊 ──▶ AI 抽取(文字/发票图/PDF) ──▶ 规则引擎 R1-R7 + LLM 复核
                                              │
                                    责任矩阵路由（谁审批/直批/额度核销）
                                              │
                        卡片审批(会签/超时升级) ──▶ 财务打款确认 ──▶ 关闭
                                              │
                              日报三版(公司/部门/财务) + 月度异常扫查
```

## 功能总览

| 能力 | 说明 |
|------|------|
| AI 抽取 | 自然语言/发票图片（视觉模型）/PDF 电子发票（含电子签章检测）→ 结构化单据；缺失自动追问，补充自动并单 |
| 合规审查 | 规则引擎 R1-R7（金额/重复发票/疑似重复/类型上限/日期/必填/抬头）+ LLM 复核（证据链），**fail-safe：AI 不可用降级人工** |
| 责任矩阵 | DMN 决策表（Excel 维护 + Hit Policy + 版本化 + 干跑报告），审批链/直批阈值老板可改 |
| 预算额度 | 员工申请 → 经理/老板批额度 → 额度内报销免逐单审批（审查不豁免）→ 自动核销扣减 |
| 预算检查 | 部门/全司月度预算（Excel 批量导入），额度审批+报销提交双检查点 |
| 全生命周期 | 发起→审批（会签/超时升级老板终裁）→ **打款确认才关闭**；撤回/作废/驳回重提 |
| 日报三版 | 公司版(老板)/部门版(经理，机密单只计数)/财务版(待付清单)，每天 18:00 自动推送 |
| 异常检测 | 月度扫查：费用趋势（四层下钻+贡献归因）+ 单价偏离（双向），ALERT 即时推送 |
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
pip install -e .
pytest        # 122 个测试，应全绿
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
| 每天 18:00 | 日报三版自动推送；每月 1 日异常扫查推送老板 |

## 日常运维

```bash
nohup python3 -u -m boss_secretary.feishu run > data/feishu_bot.log 2>&1 &   # 起服务
tail -f data/feishu_bot.log        # 看实时日志
kill $(pgrep -f boss_secretary.feishu)                                       # 停
```

建议用 launchd/systemd 托管常驻。数据全在本地：`data/secretary.db`（业务）+ `data/audit/`（审计/回放包），记得纳入备份。

## 架构与模块

| 模块 | 职责 |
|------|------|
| `core/matrix.py` + `matrix_import.py` | DMN 责任矩阵（评估器 + Excel 导入/干跑/版本化落库） |
| `core/compliance.py` | 规则引擎 R1-R7（纯函数可回放，阈值全可配） |
| `core/router.py` | 状态机 + 会签 + 超时升级 + 打款终态 + 审计 |
| `core/perm.py` | 角色×范围×密级（权限先于 LLM 上下文） |
| `core/extract.py` | AI 抽取（文本/图片/PDF）+ LLM 复核 + 交叉核验 |
| `core/allowance.py` / `budget.py` | 额度授权核销 / 预算检查 + Excel 导入 |
| `core/anomaly.py` | 月度异常扫查（趋势四层下钻 + 单价偏离） |
| `core/scheduler.py` | 定时任务（日报/扫查/过期/超时） |
| `report/daily.py` | 日报三版渲染 |
| `ingress/feishu.py` | 长连接收单 + 卡片审批 + 事件桥 |
| `core/llm.py` + `secrets.py` | OpenAI 兼容客户端（OpenRouter/vLLM/GLM）+ Keychain 密钥 |

PRD 全文：`docs/PRD-MVP.md`（责任矩阵语义、权限密级模型、异常检测方法、LLM 数据隔离等设计决策）。

## 安全模型

- **密钥**：`feishu.app_secret` / `llm.*.api_key` 存 macOS Keychain，`config/settings.yaml` 零明文；`boss-secrets migrate/set/get` 管理
- **LLM 数据隔离**：权限过滤发生在数据进模型之前——员工越狱的收益上限=其自身权限；机密单对经理只返回占位
- **审计**：`audit_log` 触发器级 append-only；每单可导出回放包（原始消息→抽取→规则→LLM→人工操作）
- **诚实边界**：Keychain 防磁盘盗取/备份扩散/仓库误提交，不防同用户恶意进程；飞书通道本身是第三方（更高级别保密需自建通道，见 PRD §L2）

## Roadmap

- [ ] 发票税局真查验（需第三方付费接口）
- [ ] 金蝶/用友凭证导出
- [ ] 月度统计报告 docx
- [ ] 企业微信/邮件入口、语音受理
- [ ] 本地 vLLM 部署指引（Qwen3.8-27B，数据完全不出内网）

## 开发模式与归属

本项目由 627-hub 发起并主导（需求定义、设计决策、验收测试、生产运维），
AI（GLM / Claude Code）作为编程助手参与实现。版权与许可证权利归 627-hub 所有。

## 免责

仅供研究学习与内部工具使用；输出不构成财务/法务意见。量化筛查有固有误报/漏报，最终判断请结合原始单据核实。

**Boss Secretary 公开源许可证**（非商业免费；商用须以同协议开源衍生作品，或取得版权人商业授权——详见 `LICENSE`）
