# 具身智能产业基金：项目评审与里程碑拨款后端

面向二十亿元具身智能产业基金的后端服务，覆盖高校成果转化、核心零部件、场景运营三类项目的
**申请版本化 → 利益回避分配 → 可解释评分/质询/投票/条件决策 → 申诉 → 合同里程碑证据与
双复核 → 幂等拨款 → 撤回/终止** 全流程。所有金额以整数分记账，业务历史只追加不改写，
每一步都进入哈希链审计，复式台账保证资金在任何状态下守恒。

仅依赖 Python 3.11+ 标准库（SQLite 内置），无需浏览器、外部数据库或第三方服务。

## 模块结构

| 文件 | 职责 |
|---|---|
| `src/industry_fund/contracts.py` | `Money` 整数分值对象、稳定指纹、基础契约 |
| `src/industry_fund/domain.py` | 申请版本、关联方、评委/分配、评分、质询、投票、条件、决策、申诉、合同里程碑、拨款、撤回、终止、台账分录、审计记录 |
| `src/industry_fund/inputs.py` | 外部输入严格校验（金额、材料、资金用途合计=申请额） |
| `src/industry_fund/repository.py` | SQLite 只追加表结构、串行化立即事务、哈希链落库与校验 |
| `src/industry_fund/ledger.py` | 复式记账：分录平衡校验、按来源扣减、任意日期资金快照、守恒自检 |
| `src/industry_fund/service.py` | 全部业务规则（状态机、回避、决策计票、里程碑门禁、幂等支付、终止冻结） |
| `src/industry_fund/api.py` | 标准库 JSON HTTP API（含 `Idempotency-Key` 头与业务级幂等键双重去重） |
| `src/industry_fund/clock.py` | 可冻结时钟，支持任意日期视图与测试 |

## 核心设计

### 申请材料版本化
- 技术路线、团队、资金用途、关联方整体指纹（SHA-256）；相同内容重复提交自动去重。
- 送评后修订材料：**关闭当前评审轮次并开启新一轮**，旧分数/投票只属于旧轮；
  系统对在任评委重新跑回避比对，新冲突者自动回避（recused），其旧票作废。
- 答复质询时记录所依据的材料版本号。

### 利益回避
- 评委维护任职机构与个人关系键；分配时逐条比对申请方关联方与团队，
  返回每条关系的命中依据与可解释结论；冲突评委可被分配（留痕）但**不能打分/投票**。
- 合资格评委少于 3 人时拒绝送决。

### 可解释决策
- 三类企业各有评分维度与权重（如成果转化 tech 40%）；每张分卡必须带理由。
- 投票一人一票一轮，唯一约束 + `BEGIN IMMEDIATE` 串行事务保证并发下只计一票。
- 结果含每人维度分/理由、维度均值、加权总分、票数统计；平票拒绝出决议；
  `conditional` 必须附带绑定里程碑的前置条件。

### 里程碑拨款
- 合同里程碑金额合计必须严格等于核准金额；证据提交 → 技术复核 → 财务复核 → 拨款。
- 技术复核失败**立即终止项目**，未释放额度全部冻结，后续拨款通道关闭。
- 条件未满足时财务复核/拨款被拒（状态 `blocked`）。
- 拨款必须带业务幂等键：同键重放返回原结果（`replayed=true`）；
  无键重试被里程碑状态与唯一约束拦截；并发支付实测只成功一笔。
- 已付款可撤回：钱从「已付」转入「冻结」，不能重复撤回。
- 终止时校验「待取消额度 == 剩余承诺」后才把承诺转冻结；争议结清可 `defrost` 回收为可用。

### 金额守恒
```
总出资 = 可用 + 已承诺 + 已冻结 + 已支付
```
- 每个台账事务借贷相等，否则整笔回滚；`verify()` 全量核对事务平衡、账户极性与恒等式。
- 承诺与支付均保留**来源维度**（政府/社会资本…），快照按来源给出构成。
- `GET /fund/snapshot?as_of=2026-10-05` 可查看任意日期（含日期按当日末刻处理）。

### 不可改写历史
- 材料版本、质询答复（一次答复不可改）、分数、投票、决策（申诉成立则置 void 但不删）、
  里程碑事件、拨款与撤回全部追加。
- 审计日志为哈希链：每条含前一条（含其负载指纹）的 SHA-256；改任何字段都会被
  `verify()` 发现（测试中有直接改库被检出的用例）。

## 运行测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

共 26 个用例，覆盖版本去重与不可变、回避（含材料修订触发）、最少评委数、并发投票只计一票、
平票/条件决策、申诉成立撤销承诺并允许重决、合同金额校验、条件门禁、技术失败终止冻结、
幂等与并发只付一次、重复撤回拒绝、终止守恒、日期快照、哈希链篡改检出及 HTTP 全链路。

## 编译检查

```bash
python3 -m compileall -q src tests run_cli.py
```

## 命令行冒烟

```bash
python3 run_cli.py
```

输出一笔从出资、回避分配、三评委评分投票、决策签约、双复核到首笔拨款的完整结果，
并附资金快照、守恒/哈希链校验结果与审计记录数。

## HTTP API

启动：

```bash
PYTHONPATH=src python3 -m industry_fund.api --host 127.0.0.1 --port 8080 --db fund.db
```

路由一览（均为 JSON）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/reviewers` | 登记评委（专长、任职机构、关系键） |
| POST | `/fund/sources` | 登记出资来源与金额 |
| POST | `/cases` | 立项 |
| POST | `/cases/{code}/applications` | 提交材料版本（相同内容去重） |
| POST | `/cases/{code}/lock` | 锁定送评、开启第 1 轮 |
| POST | `/cases/{code}/assignments` | 分配评委并返回回避比对 |
| POST | `/cases/{code}/scores/{reviewer}` | 提交带理由的维度评分 |
| POST | `/cases/{code}/questions` | 提出质询 |
| POST | `/cases/{code}/questions/{id}` | 答复质询（不可改） |
| POST | `/cases/{code}/ballots/{reviewer}` | 投票 |
| POST | `/cases/{code}/decision` | 作出决议（可带条件/核准额） |
| POST | `/cases/{code}/appeals` | 申诉 |
| POST | `/appeals/{id}/ruling` | 申诉裁定 |
| POST | `/cases/{code}/contract` | 签订里程碑合同 |
| POST | `/cases/{code}/conditions` | 确认前置条件满足 |
| POST | `/cases/{code}/milestones/{n}/evidence` | 提交里程碑证据 |
| POST | `/cases/{code}/milestones/{n}/tech-review` | 技术复核（失败即终止） |
| POST | `/cases/{code}/milestones/{n}/finance-review` | 财务复核 |
| POST | `/cases/{code}/milestones/{n}/disbursements` | 拨款（业务幂等键；可用 `Idempotency-Key` 头） |
| POST | `/cases/{code}/milestones/{n}/recall` | 撤回拨款 |
| POST | `/cases/{code}/terminate` | 终止项目 |
| POST | `/cases/{code}/defrost` | 冻结资金争议结清后回收 |
| GET | `/cases/{code}` | 项目全量审批历史 |
| GET | `/fund/snapshot?as_of=...` | 任意日期资金视图（含来源构成） |
| GET | `/audit` | 审计链记录与完整性/守恒校验 |

错误响应统一为 `{"error":{"code":...,"message":...}}`，状态码：
400 校验、404 不存在、409 状态/并发冲突、422 守恒失败。

### 快速示例

```bash
curl -s localhost:8080/fund/snapshot | python3 -m json.tool
curl -s -X POST localhost:8080/fund/sources \
  -H 'Content-Type: application/json' \
  -d '{"source":"gov","name":"引导基金","amount":"2000000000"}'
```
