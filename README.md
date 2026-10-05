# 具身智能产业基金：项目评审与里程碑拨款后端

二十亿元具身智能产业基金的项目评审与里程碑拨款系统。覆盖三类标的（高校成果转化、
核心零部件、场景运营）的全生命周期：

- 申请材料**版本化**保存技术路线、团队、资金用途、关联方（版本哈希链）；
- 按**回避关系**自动分配评委（机构重合 + 主动申报关系），利益相关者不能评分；
- 可解释的多维评分、质询问答、与分数区间一致的条件性决策，以及决策申诉；
- 通过项目按合同里程碑提交证据，经**技术 + 财务双重复核**后冻结核定额度并放款；
- 技术里程碑失败立即释放承诺额度并阻断后续拨款；
- 材料变更、并发投票、申诉、拨款撤回、项目终止全程**金额守恒、历史不可改写**；
- 重复请求（幂等键或支付流水号）**绝不可能多付一笔**；
- 管理层可按**任一历史日期**查看承诺、已付、冻结、可用资金，并追溯到资金来源。

仅依赖 Python 3.11+ 标准库（SQLite 作为仅追加事件存储），无需外部数据库或浏览器。

## 架构

```
HTTP API (api.py, http.server)
        │
FundService (service.py)            ── 命令处理：加载聚合→业务校验→追加事件；只读查询
        │
EventStore (events.py)              ── SQLite 仅追加事件表 + 全局 sha256 哈希链
        │                             BEGIN IMMEDIATE 串行事务 / 幂等键 / 支付流水号唯一
        ├── domain.py                ── 事件类型、聚合归约（Case / Milestone / Fund / Pool）
        ├── ledger.py                ── 派生只读复式台账（可用/承诺/冻结/已付 + 来源 FIFO）
        └── money.py                 ── 整数"分"记账，Decimal 量化，杜绝浮点误差
```

所有写操作都是**事件溯源**：状态只能由事件流归约得到；`events` 表上有触发器禁止
UPDATE/DELETE，事件按全局序号以 `sha256(规范JSON(事件体) || 前一事件哈希)` 串接，
`verify_chain` 可随时发现任何历史改写。台账是纯派生投影，可随时整体重建。

### 资金科目与守恒

```
可用 available ──签约──▶ 已承诺 committed ──双复核通过──▶ 冻结 frozen ──放款──▶ 已付 paid
       ▲                     │                            │                    │
       └─────────────────────┴────────────────────────────┴──── 撤回 ──────────┘
                     失败 / 财务核减 / 终止 原路释放
```

恒等式（任意时刻，含任一历史日期）：

```
来源总额 total = 可用 + 已承诺 + 冻结 + 已付
```

合同签署时按来源顺序 FIFO 占用可用额度，并把来源占用预分配到每个里程碑；冻结、失败、
核减、终止、撤回都按该里程碑的原始来源占用原路流转，因此每个科目余额都能追溯到具体
出资方。

## 运行

```bash
# 测试
PYTHONPATH=src python -m unittest discover -s tests -v

# 编译检查
python -m compileall -q src tests run_cli.py

# 命令行冒烟（内存式演示完整流程）
python run_cli.py

# 启动 HTTP 服务
PYTHONPATH=src python -m industry_fund --db fund.db --host 127.0.0.1 --port 8080
```

## HTTP 接口

所有写操作 `POST` + JSON；请求头 `X-Actor` 表示操作人，`Idempotency-Key` 实现命令级
幂等（同键 + 同请求体重放历史结果，不产生新事件；同键不同请求体返回 409）。
成功返回 201 与新产生的事件清单；领域错误返回 4xx 与 `{error, message}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/funds` | 设立基金（多个资金来源） |
| GET | `/funds/{id}` | 当前资金头寸（四科目 + 来源 + 项目维度） |
| GET | `/funds/{id}?as_of=2026-11-01` | 任一历史日期的资金头寸 |
| POST | `/reviewers` | 注册评委 |
| POST | `/reviewers/{id}/deactivate` | 停用评委 |
| POST | `/reviewers/{id}/relationships` | 申报回避关系 |
| POST | `/cases` | 立项 |
| POST | `/cases/{id}/applications` | 提交/变更申请材料（自动版本化） |
| POST | `/cases/{id}/rounds` | 开启评审轮 |
| POST | `/cases/{id}/assignments` | 按回避关系分配评委（`excluded` 给出排除原因） |
| POST | `/cases/{id}/scores` | 评委评分（技术/团队/市场/合规 四维，0-100） |
| POST | `/cases/{id}/abstentions` | 评委主动回避 |
| POST | `/cases/{id}/questions` `/answers` | 质询与答复（不可改写） |
| POST | `/cases/{id}/close-round` | 关轮（须全员表决、质询清零，产出分数汇总） |
| POST | `/cases/{id}/decision` | 决策（approved/conditional/rejected，须与分数区间一致） |
| POST | `/cases/{id}/conditions` | 确认条件性通过的前置条件已满足 |
| POST | `/cases/{id}/appeals` `/appeal-ruling` | 决策申诉与裁决（uphold/reopen） |
| POST | `/cases/{id}/contract` | 签合同（定义里程碑，占用承诺额度） |
| POST | `/cases/{id}/failure-flags` `/failure-waivers` | 技术失败标记 / 核查解除（标记期间冻结一切拨款） |
| POST | `/cases/{id}/termination` | 终止项目（释放全部未付额度，取消未结里程碑） |
| POST | `/cases/{id}/completion` | 全部里程碑了结后结项 |
| POST | `/milestones/{id}/evidence` | 提交证据（文件名 + 哈希） |
| POST | `/milestones/{id}/technical-review` | 技术复核（不通过即释放额度、阻断拨款） |
| POST | `/milestones/{id}/financial-review` | 财务复核（可核减；通过即冻结核定额） |
| POST | `/milestones/{id}/payment` | 放款（`payment_ref` 全局唯一，重复绝不二次付款） |
| POST | `/milestones/{id}/clawback` | 拨款撤回（支持部分、多次，超额拒绝） |
| GET | `/cases/{id}` `/milestones/{id}` | 聚合详情（含全部版本、轮次、证据、复核） |
| GET | `/aggregates/{id}/audit` | 不可改写的事件级审批历史（含哈希链） |
| POST | `/admin/verify-integrity` | 重算全局哈希链，验证历史未被改写 |

### 快速示例

```bash
curl -s localhost:8080/funds -H 'Content-Type: application/json' -d '{
  "fund_id":"fund-1","code":"F20","name":"具身智能基金",
  "sources":[{"source_id":"gov","name":"市财政","amount":"1200000000"},
             {"source_id":"lp","name":"社会资本","amount":"800000000"}],
  "actor":"admin"}'

curl -s 'localhost:8080/funds/fund-1?as_of=2026-12-31'
```

## 关键业务规则

- **回避**：评委所属机构与申请人/申请机构重合、评委本人或其已申报关系命中材料中的
  关联方，都会在分配时被排除并记录原因；手工指定应回避评委返回 403；未被分配的评委
  的评分/质询一律拒绝。
- **可解释决策**：评分按四维等权汇总（权重随事件留存），关轮产出各维均分与分数带；
  决策结果必须与阈值区间（默认 75 通过 / 60 条件通过）一致，杜绝背离分数的拍板。
- **材料冻结**：评审轮开启后材料即冻结；需要变更时开启新一轮评审，新版本带
  `prev_version_hash` 形成材料哈希链，历史版本永不被覆盖。
- **失败即止付**：技术复核不通过 → 里程碑失败、承诺额度立即释放、证据/复核/付款全部
  拒绝；项目级失败标记期间所有在途里程碑拨款中止，解除后方可恢复。
- **终止**：未付里程碑按 冻结→承诺→可用 的顺序留痕释放；已付款项不自动处理，需走
  撤回（clawback）流程，审计上责任清晰。
- **双重防重**：命令级幂等键（适合网络重试）+ 支付流水号数据库唯一约束（兜底），
  并发下由 `BEGIN IMMEDIATE` 单写事务串行化，重复请求不会多付一笔。

## 代码结构

| 文件 | 职责 |
|---|---|
| `src/industry_fund/events.py` | 仅追加事件存储、哈希链、事务、幂等 |
| `src/industry_fund/domain.py` | 事件类型、聚合状态机与归约器 |
| `src/industry_fund/ledger.py` | 复式台账投影、来源 FIFO、按日期头寸 |
| `src/industry_fund/service.py` | 全部命令与查询（业务规则权威实现） |
| `src/industry_fund/money.py` | 金额（整数分 / Decimal） |
| `src/industry_fund/api.py` | HTTP 路由与错误映射 |
| `src/industry_fund/contracts.py` | 初始版本保留的基础值对象契约 |
| `tests/` | 54 个单元/集成/并发测试 |
