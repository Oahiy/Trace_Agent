# Day 11：异步研究任务、受控 RAG 与独立开发讲解

日期：2026-10-20，Asia/Kuala_Lumpur。实际完成 67 项专项、711 项全量真实依赖测试（均无失败与跳过）；基础检查 526 passed、185 skipped，跳过项另在全量运行中执行。真实模型演示通过。详细结果见 `docs/day11-results.json`；详细原始报告在统一缓存清单管理的 `.cache/day11/`。

## 1. 本日交付与验收边界

新增经过 API Key 认证的 `POST /research` 和 `GET /research/{job_id}`。创建持久任务后投递真实 RQ，独立进程执行权限检查、检索、上下文选择、结构化生成、原文绑定与结果提交。重放、冲突、身份隔离、共享预算、超时、取消和显式核对均有测试。

本日结果是**带来源的单段摘录摘要**，不发布模型自由改写的医学结论。输入支持问题、可选文档 UUID、发表年份范围与幂等键；身份由认证决定。结果保留原问题、请求条件、实际资料范围、排除原因、索引/证据快照、来源、许可、日期和页码或字符定位。

今天只用锁定 PRISMA 2020 文档和 dev-02 验证一条真实成功路径，另验证年份过滤空集与医学请求拒答。不能据此宣称全语料质量、跨文档综合、冲突识别、医学安全、盲测、生产部署或开发者本人独立掌握已经通过。Day 8 人工复核和第三条真实冲突用例仍待完成。

公开 retry 和 trace 接口、自动完整摄取调度、复杂冲突处理仍按后续排期推进。研究服务不自动索引 PARSED 文档；真实 demo 的索引是显式调用，研究 worker 消费 READY 证据。

## 2. 先理解输入、输出与三种结果

1. `POST /research` 成功返回 202、job_id、run_id 和当前状态。202 表示已接受任务，不等于研究完成。队列确认后可返回 QUEUED；并发 worker 更快时可观察到更后面的状态。
2. `GET /research/{job_id}` 查询成功为 200，任务状态可以是 PENDING、QUEUED、RUNNING、SUCCEEDED 或 FAILED。
3. 正常答案是 SUCCEEDED + `result.kind=answer`；证据不足、越界医学请求或无效引用被拒绝发布时，是 SUCCEEDED + `kind=refused`，claims 为空。
4. 模型、预算、上下文、执行或证据漂移故障是 FAILED，result=null，error 为固定 `{code,message}`。200 只表示成功查到状态。

请求遵守原 v1 契约：问题 trim 后 1–2000 字符，文档最多十五个规范 UUID、不重复、明确空列表非法；年份严格整数、包含端点、不倒置；幂等键 1–64 字符。另明确限制 JSON 请求为 16 KiB、读取最多 30 秒，拒绝重复键、NaN、非法 UTF-8、问题中的代理码点和非法控制字符。完整 Schema 已出现在 OpenAPI 中。请求体不能提供 owner、模型地址、函数或文件路径。

缺失/非法身份为 401，不存在与他人资源均为 404；同键不同输入为 409；超过五个未结束任务为 429；数据库或队列不可用为安全 503。已有 /health、/ready、上传与文档状态响应形状、404/405 和 Allow 保留。

## 3. 文件与职责：建议按这个顺序读

| 文件 | 职责 | 开发者应能回答 |
|---|---|---|
| `app/api/schemas.py` | 研究输入契约 | 为什么 bool 年份和空文档列表非法？ |
| `app/api/research.py` | 认证、受限 JSON、创建/查询 HTTP | 为什么返回 202 后还需要查询？ |
| `app/db/models.py`、迁移 0005 | research_runs 存储 | 哪些不变量由约束保护？ |
| `app/db/research.py` | owner 范围内的读取与锁定 | 直接调用 repository 是否仍隔离用户？ |
| `app/services/research.py` | 幂等、配额、执行权、预算、截止和事务 | 哪个状态允许领取，哪里允许发布？ |
| `app/services/retrieval.py` | 复用精确检索、共享预算、快照复核 | 为什么生成完成后还要检查证据？ |
| `app/domains/med/research.py` | MedEvidence 范围、上下文、句子选择与绑定 | 模型能决定哪些内容，哪些由服务端决定？ |
| `app/tasks/queue.py`、worker.py、horse.py | JSON 投递和固定 worker 入口 | 为什么 JSON 仍然需要函数白名单？ |
| `scripts/day11.py` | 隔离演示、测试、检查与核对 | 哪些操作访问真实服务，哪些使用替身？ |

API 层不做模型调用或 SQL 编排；repository 不自行提交；平台业务服务通过注入的 workflow 调用场景规则。医学词表、Prompt 和结果绑定留在 domains/med，不硬编码进共用持久化、队列或研究任务状态层。

## 4. 为什么同时有 job_id、run_id、execution_id 与数据快照

`jobs` 是业务状态唯一来源。新表 `research_runs` 以 job_id 为主键并级联关联 jobs，保存唯一 run_id、唯一 execution_id、规范输入、workflow_version、预算、检索证据、结果和排队/开始/截止/完成时间；不复制第二套 status。时间和结果约束由数据库保护，跨表业务不变量由服务和回归测试检查。

job_id 是客户端查询入口；run_id 标识这次研究运行；execution_id 是 worker 的执行权标识，重复或迟到执行不能覆盖结果；data_snapshot 标识本次资料、元数据和片段身份，不是数据库全局事务快照或数字签名。

幂等键唯一范围是 `(owner,operation,key)`。输入哈希包括 trim 后问题、排序的文档集合和年份，不包括幂等键本身，也不把运行时 READY 资料变化混入请求哈希。省略文档列表与 null 表示按执行时权限选择资料，实际范围另外保存。相同键与输入复用原任务，包含已有成功、失败和拒答；同键不同输入拒绝，不重置状态或预算。

创建时锁定用户行，串行检查该用户已有 ingest/research 的未结束配额及幂等记录；数据库唯一约束提供额外保护。重复请求先复用，再检查新建配额，不会因达到配额而拒绝已有任务。

迁移 0005 仅新增 research_runs；运行账号增加该表 CRUD，仍不赋予建表或读取 alembic_version 权限。迁移账号与应用账号继续分离。

## 5. 四段短事务与真实后台执行

1. 创建事务：校验活跃用户和显式文档所有权；创建 jobs 与 research_runs，状态 PENDING。
2. 关闭事务，再向 Redis 投递固定研究函数和 execution_id；确认后短事务推进 QUEUED。确认丢失可能已入队，保留 PENDING 和同一标识供重放/显式核对。
3. worker 领取事务：从数据库重读 owner、输入与版本，锁定任务，只允许未执行的 PENDING/QUEUED 进入 RUNNING，attempt_count=1，保存截止。
4. 模型 I/O 在事务之外。请求前短事务写预算；检索后短事务保存证据；结束时短事务复核执行权、活跃用户、输入哈希、版本、证据快照和截止，再原子保存结果及 SUCCEEDED。

最终复核也可能耗时，所以复核结束后再次检查截止。同步 SQL 和文件操作仍可能短暂阻塞事件循环，其时限由现有数据库配置限制；这不是硬实时保证或恶意文件隔离方案。

RQ 的唯一 job_id 不能替代业务执行权。Redis 丢数据、运输记录过期或重复投递时，数据库依然决定能不能执行。运输记录存在时还核对函数、参数、队列和 kwargs；不能因为某个 ID 存在就相信它代表预期工作。

worker 协议更新为 `tasks-v2-json`，horse 仅放行固定解析/研究入口，仍只接受一个规范执行标识。旧 parse-v1-json worker 不能使 /ready 返回 ready。模型地址和可收紧的超时由受控环境传入子进程，API Key 等无关环境不继承；凭据不进入 argv。

## 6. 上下文与真实实验带来的设计选择

沿用 Day 9 的 4096 上下文配置、2800 输入估算预检和严格结构输出。估算是序列化完整消息的 UTF-8 字节加模板余量，并非精确 tokenizer；Schema、系统指令、问题和证据全部计入。

上下文打包复用适配器同一个估算函数，按检索顺序尝试放入完整片段，装不下的 chunk_id 明确记录。超出 Message 本身长度限制的片段也作为不可装入处理，而不是引发未分类异常。全部装不下时生成前以 research_context_too_large 失败，不提高预算或静默裁剪证据。

首次真实实验让模型返回逐字 quote。固定小模型实际删除了 PDF 参考文献编号、还原连字符并改变标点，原文绑定正确地拒绝了它。另做一次有独立预算和记录的最小诊断实验，确认原因；记录在 `.cache/day11/context-experiment.json`，不冒充任务重放或质量成绩。

最终采用 `med-extractive-v2`：完整片段的文本按简单终句标点切分并编号，保留原字符；模型只返回 evidence_id 和整数句子编号。服务端在本次选入证据中查找原句，用原文生成 claim 与 citation。不存在的编号、过短或未终结的片段、医学或注入内容拒绝发布。简单标点切分不是语言学句法解析：参考文献紧接句号时，一个编号可能包含相邻多句；缩写和跨片段句子也可能造成保守拒答。

这消除了模型复写原文导致的漂移，也缩小了输出空间；它不证明模型选择的句子完整回答了问题。真实 dev-02 仍需核对原页上下文，未来要以开发集评估相关性、完整性、拒答和冲突。模型自由生成的日期、页码、网址、许可或限制从不作为来源元数据发布。

## 7. 医学边界与尚未完成的支持性验收

当前工作流仅服务研究方法与报告规范问题。中英文临床意图、超范围问题和明显注入请求保守拒答，拒答前不发起推理；所有显式文档权限依然检查。来源内容作为数据，Prompt 指令和最终绑定提供不同层面的约束。

关键词范围判断只是发展中的防线，可能误拒或漏判，不是完整医学安全分类器。摘录也可能被断章取义。当前结果限制明确说明需人工复核，且没有自动冲突评估；`conflicts=[]` 不能解读成资料一致。不能把这套单句原文绑定用于宣称诊断、处方、剂量或治疗推荐安全，更不能向真实患者场景开放。

本日真实成功用例只涉及 PRISMA 的用途限制，人工对照第 5 页；全语料冻结和开发者签名仍未被代理替代。后续 Day 12 的支持性、条件、冲突及安全测试不能省略。

## 8. 预算、超时与错误分流

首次检索和生成共用 DurableResearchBudget。本日收紧为一次正常生成和一次允许的适配器重试，最多两次生成请求；查询 Embedding 同样最多两次。它们均在原契约上限内，任务累计生成预算仍为 24000；输出上限收紧至 512。Embedding 计数和用量单独保存，实际统计未知保留 null，不能填零。

每次实际推理前先把次数和保守用量预留落库；成功后用已知统计结算。取消、失败、未知响应仍计入保留用量。适配器自动重试共享相同任务预算和截止；本日没有自动 RQ 重试或公开手动 retry。重复领取和完成重放不会新建预算执行。

研究尝试最多 180 秒，可由 TRACEAGENT_RESEARCH_TIMEOUT_SECONDS 或更短的队列执行上限收紧；外层尝试截止包围检索和生成，单次模型操作仍最多 30 秒，剩余任务时间更短时由外层取消。取消客户端不证明 Ollama 服务端推理已经停止。

| 情况 | 行为 |
|---|---|
| 过滤后空集/模型明确无证据/无效引用 | 正常完成 refused，无 claims；空集不调用模型 |
| 医学/超范围请求 | 正常完成 refused；零推理请求 |
| 模型超时或连接/供应商故障 | FAILED + research_model_failed，不返回供应商 body |
| 无效 JSON/Schema/截断 | FAILED + research_output_invalid |
| 次数/Token 不足 | FAILED + research_budget_exhausted |
| 证据、权限或请求漂移 | 拒绝发布，记录对应固定错误 |
| 已失去执行权 | 忽略迟到结果，不覆盖其他执行 |
| 请求前预算落库失败 | 不发起该推理；保留安全数据库错误供核对 |
| worker 崩溃/硬退出 | 记录可能仍 RUNNING，截止后由显式 reconcile 失败收尾 |
| 最终 COMMIT 确认丢失 | 返回数据库不可用，不猜测已回滚，更不盲写 FAILED |

真实 PostgreSQL 故障注入分别验证：提交前失败会回滚结果，保留 RUNNING 和已扣预算待核对；提交后丢确认则数据已经 SUCCEEDED，后续重放不会再推理。显式 reconcile 可恢复未投递任务并终止过期任务，没有周期调度器。

## 9. 测试、复现与数据保留

测试分三层：test_day11.py 是确定性策略/契约/估算检查；test_day11_database.py 使用真实 PostgreSQL 配合推理替身与故障注入；test_day11_queue.py 使用真实 Redis、真实独立 worker 和本机 HTTP 推理替身，验证网络重试、用量、取消、SIGKILL 与恢复。真实 Ollama/PDF/HTTP 演示另行运行，不能由替身成绩替代。

准备已有锁定环境、数据库和 Redis 后，按顺序执行：

```bash
bash scripts/dev.sh uv sync --locked --python 3.12.14 --dev
bash scripts/dev.sh colima start --cpu 2 --memory 2 --disk 10 --vm-type vz --mount "$PWD/.cache/day4/uploads:w" --ssh-config=false
bash scripts/dev.sh .venv/bin/python -m scripts.day3 up
bash scripts/dev.sh .venv/bin/python -m scripts.day3 migrate
bash scripts/dev.sh .venv/bin/python -m scripts.day6 up
bash scripts/dev.sh .venv/bin/python -m scripts.day11 test
bash scripts/dev.sh .venv/bin/python -m scripts.day6 test --report-label local
bash scripts/check.sh
bash scripts/dev.sh .venv/bin/python -m scripts.day11 demo
```

demo 自动启动并关闭自己拥有的本机模型进程、临时 API 和 worker；若已有模型服务则复用并保留。8000 被占用时拒绝接管。锁定 PDF 缺失时先执行已有 Day 8 prepare --download，不能覆盖损坏文件冒充锁定来源。模型权重不在本日重新下载、升级或替换。

数据库测试、队列测试和 demo 共用 traceagent_test/Redis DB 1，必须串行；测试会清理隔离数据，需保留可查询演示状态时最后执行 demo。原应用库和上传、API Key、数据库/Redis 凭据及数据卷保留，不删除整个 .cache。

本地应用任务可显式核对或检查：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day11 reconcile --owner A
bash scripts/dev.sh .venv/bin/python -m scripts.day11 inspect <job_uuid> --owner A
```

这两条默认访问应用库；只操作确实需要核对的自己任务。添加 --isolated-test 使用隔离库及 Day 11 上传目录。原始详细报告写缓存，终端只给安全摘要；失败覆盖之前报告，退出码非零。

## 10. 唯一缓存清单与 Git 交付

继续由 scripts/dev.sh 原子维护唯一 `.cache/manifest.json`，新增 day11_research_reports_logs_and_test_artifacts 条目；清单和 `.cache/day11/` 都明确 gitignore。缓存包括测试 XML/日志、隔离上传、真实演示和诊断报告、本机模型日志；复用的 API/worker 日志仍由已有 day4/day6 条目管理，没有第二份缓存清单。

永久 docs 结果仅保存脱敏配置、统计和必要演示结论。不提交凭据、完整 Prompt、全文、权重或向量；不读取或修改 sources 下的同步资料。提交只选择本日文件，保留用户原有 AGENTS.md 修改。GitHub 推送及远端 CI 是独立证据，本地成功不冒充远端工作流结果。

## 11. 方案核对与五项审查

2026-10-20 复用已核对的官方文档与发布记录：Python 3.12.14，FastAPI 0.141.1，Pydantic 2.13.4，SQLAlchemy 2.1.4，RQ 2.12.0，pgvector Python 0.5.0，固定 Ollama 0.40.2 与原权重。本日无新增依赖、无模型或数据库版本升级。

RQ 2.12 为正式发布，维护者 CHANGES 将 3.0 标为未发布且含 worker 子类破坏性变更；现有独立 worker 已有实测基础，因此保留。FastAPI 的进程内 BackgroundTasks 不替代本项目的持久研究队列。JSON Schema + Pydantic 继续复用；维护者的能力说明不等于医学支持性证据。Ollama 发布页有更新稳定版本，但没有本项目同题对照收益，升级延后。真实最小实验表明句子编号能解决当前逐字引用漂移，采用该局部方案，不扩大框架与依赖。

参考：[FastAPI 后台任务](https://fastapi.tiangolo.com/tutorial/background-tasks/)、[RQ 队列与超时](https://python-rq.org/docs/)、[RQ 变更记录](https://github.com/rq/rq/blob/master/CHANGES.md)、[SQLAlchemy 2.1 事务](https://docs.sqlalchemy.org/en/21/orm/session_transaction.html)、[Ollama 结构化输出](https://docs.ollama.com/capabilities/structured-outputs)、[Ollama 发布](https://github.com/ollama/ollama/releases)。

| 审查项 | 本日重点 |
|---|---|
| 正确性 | 幂等规范化、单一状态、执行权、原文绑定、条件/定位和提交原子性 |
| 错误处理 | 投递不确定、预算落库失败、截止、取消、崩溃、迟到和 COMMIT 不确定分开处理 |
| 测试 | 成功/边界/失败，替身/真实服务/真实模型分开记录；跳过不算通过 |
| 安全 | owner 同时在业务与查询保护、固定函数、JSON、受限请求、安全错误和敏感缓存忽略 |
| 可维护性 | 平台规则与医学规则分离、复用已有预算与检索，无新依赖或额外业务场景 |

## 12. 如果自己开发：怎样借助 AI 而保留独立能力

先自行写出接口的三类结果、状态图、事务边界和失败表，再让 AI 实现一个小切片：创建/查询、领取、预算、检索、生成、绑定依次推进。每一片都检查输入与故障，不把整条业务逻辑塞进 route。

AI 可以帮助写样板、列边界、构造故障和解释日志；你必须亲自确认幂等范围、owner 条件、错误是否持久化、预算是否在发送前扣除、测试是否真的触及独立 worker，以及引用是否回答问题。尤其不要把捕获异常后的“返回成功字典”当成正确性证明。

这里练习的 AI Coding 能力是需求分析、拆解协作、审查、调试、测试设计和复现交付；Python 后端能力是事务、约束、接口与异步任务；LLM 应用能力是上下文、结构化选择和证据验证；Agent 应用能力是共享预算、执行标识和终止条件。这些是岗位准备重点，不是对公司具体考题的保证，也不要求新增模型训练或研究型算法基础设施。

请亲自演示一次成功、一次空证据和一次失败，最后回答：

1. 幂等键、执行标识和数据快照分别保护什么？
2. worker 被杀或 COMMIT 确认丢失后，为什么不能直接重跑或盲写失败？
3. 单元测试、真实队列测试和真实模型实验，各自不能证明什么？
