# 职业技能标准映射后端

两所合作院校互认焊接等技能等级证书时，名称相近的等级在**操作范围**与**考核证据**上并不等同。
本服务保存各国标准版本、能力单元、前置关系与考核证据，让映射提案逐项说明覆盖、差距与依据，
在双方专家达到签署条件后发布；部分互认、标准换版、提案撤回或并发会签都形成新版本，
历史证书始终指向签发时采用的映射，任意两版可比较、可复算。

零第三方依赖：Python 3.11 标准库 + SQLite。

## 领域模型与不变量

对应 `domain/contract.json` 的五个状态（导入 → 提案 → 会审 → 发布 → 更新）与四个不变量：

- **标准版本**：`standards` 有 `DRAFT / EFFECTIVE / SUPERSEDED` 生命周期。生效时对整个能力图谱
  （能力单元、操作范围、考核证据、前置关系）计算规范化 SHA-256 并冻结；生效后不可变，
  换版走 `new-version` 产生新版本，旧版自动置为 SUPERSEDED 且数据保留。
- **能力图谱**：能力单元含操作范围 `scope` 与等级标签；考核证据区分
  `PRACTICAL/THEORY/DOCUMENT/OBSERVATION`；前置关系分必修/建议，导入时校验引用存在且无环。
- **双方会签**：提案经 `提交会审` 后，A/B 两方专家组各自签署。会签携带 `expected_revision`
  乐观版本号；重复签署、过期版本返回 `409 concurrent_signoff`。两方齐备时在同一写临界区内
  原子完成“校验→固化发布行→置发布态”。
- **部分互认**：每个映射项结论为 `FULL / PARTIAL / NONE`。`PARTIAL` 必须逐项登记差距
  （范围更窄、证据缺失、证据类型不等同、前置未达成），所有项必须填写依据；
  `FULL` 不得带差距；提交会审强制覆盖全部来源能力单元。

### 版本链与历史可追溯

- 每次双方会签通过生成不可变 `publishes` 版本（含冻结的映射行、差距与 `mapping_hash`），
  上一版标记 `superseded_at`。发布后撤回生成 `WITHDRAWAL` 新版本，不删除任何历史。
- 标准换版后的更新提案通过 `based_on_publish_id` 接续版本链，并按编码继承旧映射；
  其中“目标缺少证据”类差距是对旧图谱的事实断言，换版后**保守丢弃**，强制专家重判。
- 证书签发时钉住 `publish_id` 与当时的 `mapping_hash`；查询时沿后继链报告当前映射是否已变化，
  但证书永远解析回签发时的版本。
- `GET /api/compare?a=&b=` 对任意两版逐行给出新增/移除/变更/不变；
  `GET /api/publishes/{id}/recompute` 从冻结行重建规范化载荷并复算摘要，发现篡改即不一致。

## 目录

- `domain/contract.json`：领域角色、状态、约束与样例。
- `src/domain_contract/`：契约读取与确定性校验（原有）。
- `src/skillmap/domain/`：枚举、错误、规范化序列化与摘要。
- `src/skillmap/db/schema.py`：SQLite 建表脚本。
- `src/skillmap/services/`：标准版本、映射提案、发布/证书、版本比较四个领域服务。
- `src/skillmap/api.py`、`__main__.py`：HTTP API 与启动入口。
- `tools/demo.py`：中德焊接互认端到端演示（并发会签、换版、比较、撤回）。
- `tests/`：46 个回归测试（领域规则、并发会签、版本链、HTTP 集成）。

## 运行

```bash
# 端到端演示
PYTHONPATH=src python3 tools/demo.py

# 启动 HTTP 服务
PYTHONPATH=src python3 -m skillmap --host 127.0.0.1 --port 8080 --db skillmap.db

# 测试 / 编译 / 契约检查
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/jurisdictions` | 建立合作方管辖区 |
| POST | `/api/standards` | 导入标准版本（可随 `effective_at` 直接生效） |
| POST | `/api/standards/{id}/effective` | 草稿生效并冻结图谱，旧版自动替代 |
| POST | `/api/standards/{id}/new-version` | 基于生效版本起草换版 |
| GET | `/api/standards[?code=]` | 标准版本与能力图谱 |
| POST | `/api/proposals` | 创建逐项映射提案（含 items/gaps/rationale） |
| POST | `/api/proposals/{id}/revisions` | 修订，产生新版本 |
| POST | `/api/proposals/{id}/submit` | 提交会审（强制逐项全覆盖） |
| POST | `/api/proposals/{id}/request-changes` | 退回修改，会签作废 |
| POST | `/api/proposals/{id}/signoffs` | 一方会签（`party` A/B，`expected_revision`）；双签原子发布 |
| POST | `/api/proposals/{id}/withdraw` | 撤回；已发布则生成撤销版本 |
| POST | `/api/updates` | 基于历史发布与换版后标准起草更新提案 |
| GET | `/api/proposals[/{id}]` | 提案、修订、会签、事件流 |
| GET | `/api/publishes/{id}` | 不可变发布版本 |
| GET | `/api/history?source=&target=` | 标准对的完整发布链 |
| GET | `/api/current?source=&target=` | 最新发布（可能是撤销版本） |
| GET | `/api/publishes/{id}/recompute` | 复算摘要并比对 |
| GET | `/api/compare?a=&b=` | 任意两版逐行比较 |
| POST | `/api/certificates` | 依据指定发布版本签发证书（钉版） |
| GET | `/api/certificates[/{no}]` | 证书及其签发时映射、当前是否变化 |
