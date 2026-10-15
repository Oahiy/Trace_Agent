# TraceAgent API 契约 v1

定稿日期：2026-10-11，实施状态更新：2026-10-15。Day 6 增加 `/ready`、可配置的上传后队列投递和后台解析阶段；保留原上传与状态查询字段形状。研究、完整摄取/Embedding 和公开 retry 尚未实现。下面的占位 UUID 不表示真实记录，未来接口样例不能计入已经通过的功能。`app/api/schemas.py` 已实现并测试研究请求与统一错误的结构模型。

## 身份、输入与兼容性

除 `/health` 和 `/ready` 外，业务接口将要求 `X-API-Key`。服务端验证身份并决定 owner；请求体不能包含 owner、待执行函数、供应商地址或宿主文件路径。文档、任务、run ID 使用服务端生成的小写规范 UUID 字符串。学习实验中的 `doc-001` 不属于正式 ID 格式。

缺失或无效身份返回 401；其他用户的资源与不存在的资源均返回 404，避免泄露资源是否存在。查询与工具执行必须都带 owner 条件，不能依赖模型“自行遵守”。D4 上传与文档状态接口已经接入真实身份及数据库 owner 过滤。API Key 是安全随机生成的 43 字符 URL-safe 字符串，数据库仅保存 SHA-256 摘要；重复身份头拒绝，停用用户不能认证，401 带 `WWW-Authenticate: APIKey`。

现有 `/health` 成功响应，以及框架的 404 `{"detail":"Not Found"}`、405 `{"detail":"Method Not Allowed"}` 和 Allow 头保持不变。新业务接口按下列错误结构实现；不在 D2 静默替换现有错误响应。统一所有路由错误时必须明确记录契约版本与迁移。

```json
{"error":{"code":"invalid_request","message":"Request does not satisfy the contract."}}
```

消息采用固定安全文本。不返回请求原文、API Key、异常字符串、栈、SQL、供应商 body、数据库地址或患者信息。校验错误只提供安全字段位置与错误类别时，需要单独扩展并测试契约；不能直接转发 `ValidationError.errors()` 中的 input 或 context。

## 已定稿接口与样例

以下示例 UUID 为占位样例，不表示数据库已有记录。每项同时给出成功和重要失败行为。

### GET /health —— 已实现

输入：无。成功：200，`{"status":"ok"}`。失败演示：`POST /health` 返回 405，Allow 包含 GET；未知路径返回 404。API 进程停止后的 HTTP 请求连接失败。health 只检查路由存活，不访问模型、数据库或队列。

### GET /ready —— Day 6 已实现

输入：无。并发检查数据库、Redis 和目标队列真实 worker 心跳，不通过推理检查模型。每项最多等待 1 秒，整个检查最多 3 秒，不自动重试。心跳须在 10 秒内、状态 idle/busy、无 death 标记、符合 JSON 协议，并匹配数据库和上传目录作用域。Redis 可连接本身不构成就绪。未启用队列时为 503。

成功：200，`{"status":"ready"}`。失败：503，`{"error":{"code":"dependencies_unavailable","message":"Service dependencies are unavailable."}}`。响应与日志均不包含连接串。Day 6 已验证生命周期、连接握手不回应、服务停机及心跳过期；此检查不证明每条任务、全部配置或文件都可执行。

### POST /documents —— Day 4 已实现上传登记

输入：multipart/form-data，包括 `file`、`source_id`、`title`、`source_url`、`license`、`license_evidence_url`，可选 `publication_year`、`version_label`、`research_dates`。来源 URL 作为追溯元数据保存，不触发服务器任意 URL 抓取。年份未知保存 null，不能从标题补造。

限制：单文件 5 MiB；仅 UTF-8 TXT 与文本型 PDF；PDF 最多 50 页；提取最多 200000 字符；拒绝扫描件、加密、损坏文件和空文本。不使用客户端文件名生成路径。配置只允许在这些上限内收紧限制。身份、上传与解析分别在 D4/D5 实现。

成功：202：

```json
{"document_id":"12345678-1234-4234-8234-123456789abc","job_id":"22345678-1234-4234-8234-123456789abc","document_status":"UPLOADED","job_status":"PENDING"}
```

返回 PENDING 表示记录已经持久化，尚未承诺队列投递成功。首次投递明确失败时按原计划返回 503 `queue_unavailable`，错误体附已提交的 job_id，任务保留 PENDING，允许同一请求重试或修复命令继续；不能假报 QUEUED。同 owner、同文件哈希与同摄取版本复用已有文档和任务；不能跨 owner 复用可见资源。相同文件但来源元数据冲突返回 409 `source_metadata_conflict`，不能静默覆盖。文件过大返回 413 `file_too_large`；类型不支持返回 415 `unsupported_file_type`；元数据或编码不合法返回 422 `invalid_request`。异步解析失败体现在文档/任务状态中，不伪装成上传阶段已经完成解析。

### GET /documents/{id}/status —— Day 4 已实现

输入：文档 UUID 与身份。成功：200：

```json
{"document_id":"12345678-1234-4234-8234-123456789abc","status":"READY","index_version":"v1","error":null}
```

合法文档失败状态仍返回 200，例如 `{"document_id":"12345678-1234-4234-8234-123456789abc","status":"FAILED","index_version":null,"error":"no_extractable_text"}`。不存在或越权返回 404 `resource_not_found`。READY 必须意味着当前版本 Embedding 已完成，不等同于 PARSED。

Day 5 兼容性勘误：上述文档状态的 `error` 保持 Day 4 已实现的 `str | null`。原文的 `{code,message}` 示例与已实现响应模型不一致，今天修正文档，不改变运行中的字段形状。请求本身的 4xx/5xx 仍采用顶层 `{error:{code,message}}`；研究结果的未来结构不因此改变。未知内部文档错误码仍映射为 `internal_error`。

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

Day 3 更新（2026-10-12）：PostgreSQL/pgvector、users/documents/chunks/jobs 表、迁移、内部文档与任务原子保存、owner 查询过滤和真实数据库测试已实现。内部登记使用可信调用者提供的 owner、合成文件哈希和摄取版本，不是公开上传接口；演示中的 UPLOADED 仅验证记录状态，不证明已经保存文件。公开 API 仍只有 `/health`，其成功、404、405 与 Allow 契约保持不变。

数据库内部探测验证连接、扩展版本和合成向量运算。它没有作为 `/ready` 挂载；完整 `/ready` 仍需要 D6 的 Redis 和真实 worker heartbeat，并按上述时限单独实现。任务表的状态与幂等字段约束不等于队列、状态迁移、研究输入规范哈希或 retry 已实现。D4 从真实身份确定 owner，并从文件字节计算哈希；D5 从真实解析结果验证 chunk 定位与处理版本。详细执行证据见 `docs/day3-results.json`。

Day 2 历史验证：健康接口回归、配置成功/边界/失败、研究输入结构成功/边界/失败、独立文档实验。此后的身份、数据库与上传验证分别记录在 Day 3/4 证据中。队列、工作流、retry、真实 RAG 和医学拒答仍未实现，未来样例不能计入已通过功能。

## Day 4 实施细节与明确补充（2026-10-13）

上传路由先完成身份验证，再显式解析 multipart；解析到暂存文件不等于正式保存。整个请求体上限为配置的文件上限加 64 KiB（包括所有元数据与 multipart 开销），真实接收字节计数不能被缺失或低报的 Content-Length 绕过。已知过大的 Content-Length 可能在外层返回 413，优先于内层身份错误。最多一个文件、八个元数据字段；单个非文件字段最多 8192 字节。拒绝重复字段、未知字段、未完成或畸形 multipart。网络读取及表单解析总截止默认 30 秒，可配置收紧；它不是磁盘保存和数据库提交的整体截止时间。

缺少来源等必填元数据、非法字段、年份或 TXT 编码返回 422 `invalid_request`。年份必须是四位十进制字符串，省略或空字符串保存 null；其他可选元数据字段可省略，明确提交空字符串仍需通过字段校验。截断/畸形 multipart、缺少 boundary、超出解析字段/文件数量或字段字节上限返回 400 `invalid_request`。解析超时返回 408 `upload_timeout`。TXT 允许 UTF-8 BOM，拒绝空白文本、NUL 等非法控制字符，字符上限为配置值（最多 200000）；哈希和保存内容仍使用原始字节。

文件 MIME 仅接受 text/plain、application/pdf（允许 MIME 参数）。不信任扩展名或 MIME 标签：TXT 严格解码；PDF 验证起始格式标识。PDF 文本型、50 页上限、损坏、加密、扫描件和提取字符数量的最终检查在 D5 摄取阶段；D4 接收 PDF 不表示解析通过。5 MiB 边界测试中的填充 PDF 只验证上传大小，不是有效文本 PDF 的证明。正式文件名由服务端随机生成，公开请求/响应没有文件路径、存储键、owner 或客户端哈希字段。

新增错误：数据库不可用为 503 `database_unavailable`；引用文件缺失/损坏或存储故障为 503 `storage_unavailable`；每 owner 已有五个 PENDING/QUEUED/RUNNING 任务时，新上传为 429 `too_many_pending_tasks`（重复请求仍可复用）；不满足内部数据约束或意外业务异常为 500 `internal_error`。消息固定，不转发异常内容。原 `/health`、未知路径和错误方法的 404/405 与 Allow 头保持已有结构。

每次上传使用独立候选存储键。文件先完成写入和 fsync，再通过同文件系统的独占硬链接原子发布；不会覆盖已有文件。随后文档、任务、文件引用在一个数据库事务中提交。确认回滚的业务拒绝清理本请求候选；SQLAlchemy 存储异常可能包含 COMMIT 确认丢失，因此保留候选，待数据库恢复后只读 audit 核对。复用成功时清理当前请求候选，并验证原引用文件。缺失或损坏的原文件不会被重试静默覆盖，需要从备份恢复或后续明确修复流程。

状态查询同时校验 owner 与 UUID，再核对引用文件大小/哈希。不存在与其他 owner 均 404；存在但文件不可用为 503，不能误报不存在或成功。FAILED 文档的成功查询仍为 200；不在安全错误白名单的内部错误码被替换为 internal_error。D3 合成、无文件引用的历史记录保留，但通过该接口查询返回 storage_unavailable，避免把内部元数据演示当成真实上传。

今天保存的任务保持 PENDING；没有投递队列，也不返回 QUEUED。上传目录通过受限宿主 bind mount 与临时容器共享，并已验证相同哈希和只读写入拒绝；真正 worker 的读取、摄取和状态变化在 D5/D6 验收。文件引用与凭据保留规则、真实验证及剩余边界见 `docs/day4-walkthrough.md` 和 `docs/day4-results.json`。

## Day 5 解析阶段（2026-10-14）

通过本地 `scripts.day5 parse <document_id> --owner A|B` 使用受保护凭据认证，再调用内部 IngestionService；没有新增公开解析路由、队列或 worker。输入不含文件路径。先核验同一份原始字节的大小与 SHA-256，再交给隔离子进程。解析成功的全部 chunks 和文档 PARSED 在一个短事务内提交；index_version 保持 null，整体摄取 job 仍为 PENDING。该命令不接管 QUEUED/RUNNING 任务。解析失败原子保存文档 FAILED、任务 FAILED 和安全错误码；数据库保存失败时不宣称失败状态已经持久化。

初始 ingest-v1 解析规格固定为 pypdf 6.20.1、strict=True、普通提取模式、1000 字符片段、100 字符重叠、PDF 不跨页、TXT UTF-8-sig 解码后保留原始换行和空白。Day 4 的 ingest-v1 尚无解析/片段，这是第一次确定其解析规格；安全补丁对三份已固定语料的逐页文本哈希无变化。后续变更解析器、规范化或切分行为须使用新的处理版本；当前解析器拒绝其他版本，不无声改写旧证据。

TXT 范围为 Python 解码后字符串的 [char_start,char_end)，不含 BOM，不是 UTF-8 字节偏移。PDF 页码为从 1 开始的物理页；空白页不压缩编号。所有片段可通过 document_id 追溯来源、许可、年份、版本和研究日期；未知 section 保持 null。

子进程解析截止默认 60 秒，可收紧且最多 120 秒；不自动重试。超时后 kill 并回收子进程，stderr 不向业务输出转发。子进程不继承数据库/Key 等环境值，启用 CPU 限制并禁止 core dump；512 MiB 地址空间限制在本机 macOS 不受支持，报告为未启用。另限制单页解码内容流 4 MiB、累计 16 MiB；这是对原文件/页数/文本上限的明确补充，先解码再判断本身不构成硬内存隔离。解析截止不是文件读取、数据库保存或整个摄取尝试的总截止；后者与任务预算由后续 worker 接入。

文档安全错误码包含 invalid_text、invalid_pdf、encrypted_pdf、too_many_pages、too_much_text、no_extractable_text、unsupported_image_page、parse_resource_limit、parse_timeout、parse_failed。空文本 PDF、加密和损坏文档拒绝；无文本且含直接图片 XObject 的页也拒绝。复杂嵌套图片、已有 OCR 层和混合扫描件没有完整检测保证；当前验收限于已核验文本型公开语料，不把少量可提取文本当成完整性证明。

重复解析 PARSED/READY 复用片段 ID；FAILED 复用既有失败且命令非零退出，不自动重试。提交确认丢失返回安全数据库错误，后续重放读取已提交结果。文件损坏/缺失时安全失败，不删除片段或自动替换证据。历史 Day 3 无文件引用的合成记录保留，无法作为真实文件解析。实测证据和边界见 docs/day5-results.json 与 docs/day5-walkthrough.md。

## Day 6 队列阶段与兼容性补充（2026-10-15）

启用 TRACEAGENT_QUEUE_ENABLED 并配置认证本机 broker 后，POST /documents 在持久登记完成后投递解析阶段。返回字段仍为 document_id、job_id、document_status、job_status；其中 job_status 指完整 ingest 父任务，本日保持 PENDING，解析成功文档为 PARSED、index_version=null，不返回 READY。接入完整摄取调度后的顶层 QUEUED/RUNNING/SUCCEEDED 仍属后续实现。原“只有队列确认才记录 QUEUED”本日应用于独立 parse_tasks 阶段，不静默把父任务改成解析任务。

新真实上传的 documents/jobs/parse_tasks 同事务保存，历史记录不回填。阶段通过内部命令 inspect/reconcile 查看，不新增公开任务查询或重试接口。阶段成功不增加父任务 attempt_count；完整摄取尝试和其累计预算尚未启动。阶段失败原子保存文档、父任务和阶段安全失败；片段及阶段成功也原子提交。

明确投递失败响应为 503：`{"error":{"code":"queue_unavailable","message":"Task queue is unavailable."},"job_id":"已提交的规范 UUID"}`。这里新增顶层 job_id 是原定恢复语义的具体字段位置。文件/记录保留，允许同请求重放或身份范围内的显式 reconcile。RQ 确认不能让已运行/完成阶段倒退。PENDING/QUEUED 阶段可用相同 execution_id 恢复，RUNNING 到期只失败，不自动重跑。阶段排队 1800 秒、执行最多 300 秒，可配置收紧；等待未来索引的父任务不属于丢失的解析队列任务。

文档 error 保持 str|null，新增安全码 task_execution_failed、queue_wait_timeout、task_timeout、ingest_state_conflict。现有上传/解析错误、身份隔离、404/405 与 Allow 保留。配置未启用队列的本地练习继续保存 PENDING 阶段，可用 Day 5 本地解析；QUEUED/RUNNING 不可绕过。每用户五个未结束父任务上限不变。

解析 worker 只接受固定函数、单个规范执行标识的 JSON 消息；owner、文档、版本和文件引用从数据库重新检查。用户停用后不执行或提交资料结果。数据库确认失败不伪造已持久化终态。reconcile 当前为显式运维动作，没有周期调度器；完整 ingest、Embedding、公开 retry、RAG、医学回答与生产隔离仍未验收。详见 docs/day6-walkthrough.md 和 docs/day6-results.json。
