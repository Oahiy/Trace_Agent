# TraceAgent API 契约 v1

定稿日期：2026-10-11。此文档约定后续实现行为。Day 2 实际公开接口只有 `/health`；其余接口尚未挂载，不能将下面样例当作已经运行的结果。`app/api/schemas.py` 已实现并测试研究请求与统一错误的结构模型。

## 身份、输入与兼容性

除 `/health` 和 `/ready` 外，业务接口将要求 `X-API-Key`。服务端验证身份并决定 owner；请求体不能包含 owner、待执行函数、供应商地址或宿主文件路径。文档、任务、run ID 使用服务端生成的小写规范 UUID 字符串。学习实验中的 `doc-001` 不属于正式 ID 格式。

缺失或无效身份返回 401；其他用户的资源与不存在的资源均返回 404，避免泄露资源是否存在。查询与工具执行必须都带 owner 条件，不能依赖模型“自行遵守”。身份实现与集成测试安排在 D4。

现有 `/health` 成功响应，以及框架的 404 `{"detail":"Not Found"}`、405 `{"detail":"Method Not Allowed"}` 和 Allow 头保持不变。新业务接口按下列错误结构实现；不在 D2 静默替换现有错误响应。统一所有路由错误时必须明确记录契约版本与迁移。

```json
{"error":{"code":"invalid_request","message":"Request does not satisfy the contract."}}
```

消息采用固定安全文本。不返回请求原文、API Key、异常字符串、栈、SQL、供应商 body、数据库地址或患者信息。校验错误只提供安全字段位置与错误类别时，需要单独扩展并测试契约；不能直接转发 `ValidationError.errors()` 中的 input 或 context。

## 已定稿接口与样例

以下示例 UUID 为占位样例，不表示数据库已有记录。每项同时给出成功和重要失败行为。

### GET /health —— 已实现

输入：无。成功：200，`{"status":"ok"}`。失败演示：`POST /health` 返回 405，Allow 包含 GET；未知路径返回 404。API 进程停止后的 HTTP 请求连接失败。health 只检查路由存活，不访问模型、数据库或队列。

### GET /ready —— 待实现

输入：无。短时检查数据库、Redis 和 worker 的明确就绪信号，不通过 expensive 推理检查模型。每项最多等待 1 秒，整个检查最多 3 秒，不自动重试。worker 就绪判据必须在 D6 与实际 heartbeat 一起实现；不能把 Redis 可连接当作 worker 可执行。

成功：200，`{"status":"ready"}`。失败：503，`{"error":{"code":"dependencies_unavailable","message":"Service dependencies are unavailable."}}`。响应与日志均不包含连接串。D3/D6 实现时必须验证实际生命周期及超时；今天只是契约。

### POST /documents —— 待实现

输入：multipart/form-data，包括 `file`、`source_id`、`title`、`source_url`、`license`、`license_evidence_url`，可选 `publication_year`、`version_label`、`research_dates`。来源 URL 作为追溯元数据保存，不触发服务器任意 URL 抓取。年份未知保存 null，不能从标题补造。

限制：单文件 5 MiB；仅 UTF-8 TXT 与文本型 PDF；PDF 最多 50 页；提取最多 200000 字符；拒绝扫描件、加密、损坏文件和空文本。不使用客户端文件名生成路径。配置只允许在这些上限内收紧限制。身份、上传与解析分别在 D4/D5 实现。

成功：202：

```json
{"document_id":"12345678-1234-4234-8234-123456789abc","job_id":"22345678-1234-4234-8234-123456789abc","document_status":"UPLOADED","job_status":"PENDING"}
```

返回 PENDING 表示记录已经持久化，尚未承诺队列投递成功。首次投递明确失败时按原计划返回 503 `queue_unavailable`，错误体附已提交的 job_id，任务保留 PENDING，允许同一请求重试或修复命令继续；不能假报 QUEUED。同 owner、同文件哈希与同摄取版本复用已有文档和任务；不能跨 owner 复用可见资源。相同文件但来源元数据冲突返回 409 `source_metadata_conflict`，不能静默覆盖。文件过大返回 413 `file_too_large`；类型不支持返回 415 `unsupported_file_type`；元数据或编码不合法返回 422 `invalid_request`。异步解析失败体现在文档/任务状态中，不伪装成上传阶段已经完成解析。

### GET /documents/{id}/status —— 待实现

输入：文档 UUID 与身份。成功：200：

```json
{"document_id":"12345678-1234-4234-8234-123456789abc","status":"READY","index_version":"v1","error":null}
```

合法文档失败状态仍返回 200，例如 `{"document_id":"12345678-1234-4234-8234-123456789abc","status":"FAILED","index_version":null,"error":{"code":"no_extractable_text","message":"Document has no supported extractable text."}}`。不存在或越权返回 404 `resource_not_found`。READY 必须意味着当前版本 Embedding 已完成，不等同于 PARSED。

### POST /research —— 待实现，输入模型已测试

输入 JSON：

```json
{"question":"Compare search reporting across PRISMA versions.","document_ids":["12345678-1234-4234-8234-123456789abc"],"publication_year_from":2009,"publication_year_to":2021,"idempotency_key":"demo_1"}
```

问题去首尾空白后为 1–2000 字符。`document_ids` 省略或 null 表示该用户允许访问的全部 READY 文档；明确给空数组属于错误。最多 15 个 ID，规范 UUID，不允许重复。年份可独立提供，区间包含端点；不得倒置，不接受字符串年份或 bool。启用年份过滤时排除年份未知的资料，并在结果中解释。年份指发表年份，与研究日期、版本名称分别存储。运行配置如果收紧问题长度，handler 还必须执行对应的更严格限制。

幂等键位于请求体，长度 1–64，仅字母、数字、下划线和短横线。它在 `(owner, operation, idempotency_key)` 范围唯一。相同键与相同规范输入返回已有任务；相同键与不同输入返回 409 `idempotency_conflict`。文档列表按集合语义参与幂等哈希，服务端排序规范化；不以模型的自然语言输出作为幂等判断依据。

成功：202：

```json
{"job_id":"22345678-1234-4234-8234-123456789abc","run_id":"32345678-1234-4234-8234-123456789abc","status":"PENDING"}
```

非法请求返回 422 `invalid_request`；显式文档不存在或越权返回 404 `resource_not_found`。没有可用证据时最终结果为明确拒答，不能凭空生成引用。今天没有运行研究工作流或证明其医学安全行为。

### GET /research/{id} —— 待实现

`id` 是创建研究时返回的 job_id。成功查询：200。运行中返回身份允许的状态、原问题与条件；完成后返回 result。

```json
{"job_id":"22345678-1234-4234-8234-123456789abc","status":"SUCCEEDED","question":"Locate reporting guidance.","conditions":{"publication_year_from":2021,"publication_year_to":2021},"result":{"kind":"answer","used_document_ids":["12345678-1234-4234-8234-123456789abc"],"data_snapshot":"snapshot-v1","claims":[{"text":"A supported research summary.","citations":[{"source_id":"prisma-2020","chunk_id":"42345678-1234-4234-8234-123456789abc","pdf_page":5,"section":null,"publication_year":2021,"support_fragment":"Verified source fragment."}]}],"limitations":[],"conflicts":[]},"error":null}
```

此处自然语言是结构占位，不能视为实际证据。TXT 使用 section 或字符范围定位；PDF 保留物理页码。结果需回显实际资料范围、索引/数据快照和过滤排除原因。冲突必须附来源、条件、日期与限制。未经支持的结论删除或拒答。

拒答是正常完成：status 为 SUCCEEDED，result.kind 为 `refused`，reason 为 `insufficient_evidence` 或 `medical_request`，claims 为空并解释限制。服务错误则是 FAILED、result 为 null、error 为安全错误。不存在或越权返回 404 `resource_not_found`。200 表示成功查询状态，不表示研究结果成功。

### POST /research/{id}/retry —— 待实现

输入：身份、job_id，无新的研究问题。仅自己的可重试 FAILED 任务可重新进入 PENDING。成功：202，返回 job_id、当前 attempt_id、status。正在执行时重复 retry 返回当前 attempt 信息，不创建并发重复执行；已经成功或不可重试失败返回 409 `task_not_retryable`；超过尝试上限返回 409 `retry_budget_exhausted`。越权或不存在返回 404。

外部 HTTP 重试和任务重试分别计数，失败调用也消耗累计预算。任务最多追加一个手动尝试，共两个尝试；不启用 RQ 自动重试/调度器。调用最多自动追加一次重试，仅对连接中断、超时或 429/502/503/504 等明确瞬态错误；身份、输入、引用校验和医学边界失败不可重试。所有重试计入同一任务累计预算并受尝试截止时间限制，不能通过 retry 重置。

### GET /runs/{run_id}/trace —— 待实现

成功：200，`{"run_id":"32345678-1234-4234-8234-123456789abc","steps":[],"usage":{"generation_calls":0,"embedding_calls":0,"input_tokens":null,"output_tokens":null}}`。steps 将记录工具名称、安全参数摘要、状态、耗时、尝试与固定错误码；不返回完整 Prompt、敏感原文或秘密。未知计数保存 null，不假造为零。不存在或越权返回 404 `resource_not_found`。

## 状态、事务与安全边界

任务：PENDING → QUEUED → RUNNING → SUCCEEDED 或 FAILED。投递失败保留可修复的 PENDING；只有队列确认后记录 QUEUED。文档：UPLOADED → PARSED → READY，失败转 FAILED。旧 attempt 的迟到结果不能覆盖新 attempt。

文件保存、数据库提交、队列投递不构成一个原子事务。先完成持久记录再投递，记录失败并提供恢复路径。每个请求/任务使用自己的数据库 Session；事务边界明确，模型等待不占用事务。D2 内存练习没有 SQL 事务，不宣称其提供数据库回滚或进程间一致性。

初始执行预算：模型总调用截止 30 秒；摄取尝试 300 秒；研究尝试 180 秒；每研究尝试最多 4 次生成请求、6 次工具尝试，含首次检索最多 2 轮。自动重试计入这些次数。每用户最多 5 个未结束任务，排队超过 30 分钟由核对流程标记失败。生成输入与输出累计最多 24000 Token，包含手动重试；Embedding 单独计数。实际统计缺失时标记未知，估算与供应商统计分开。云服务预算为 0，仅本机模型；不得自动切换付费端点。

D2 相对原计划的明确收紧：Day 1 本机模型实验配置上下文为 4096，因此未来适配器初始完整输入预算为 2800 Token、单次输出最多 1024 Token，替代原草案的 6000 输入/1200 输出。完整输入包括系统指令、历史、工具定义和证据，而非只计算文档。保留上下文余量并记录估算方式；D9 必须以实际适配器、模型模板与响应 Token 验证，不宣称估算能够保证精确上限。需要扩容时先做最小真实实验并记录契约版本。D1 独立实验仍为其原来的 256 输出、6 次生成限制；不把今天的文档预算假称为已接入 Day 1 代码。

MedEvidence 只支持证据检索和研究辅助，不提供诊断、处方、剂量或治疗推荐。医学限制与 Prompt 后续放入 `app/domains/med/`；通用配置、任务和持久化组件不硬编码这些规则。来源 ID 合法与结论确实受支持需要分别验收。

## 验证状态

已执行：健康接口回归、配置成功/边界/失败、研究输入结构成功/边界/失败、独立文档实验。未执行：未挂载接口的 HTTP、身份、数据库事务、队列、工作流、retry 和医学拒答测试。上述未来样例是验收预期，不能计入今天已通过的功能。
