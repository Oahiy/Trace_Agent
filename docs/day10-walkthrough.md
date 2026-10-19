# Day 10：可追溯向量入库、精确检索与独立开发讲解

计划日期：2026-10-19，Asia/Kuala_Lumpur。实际执行时间与数量见 `docs/day10-results.json`。

## 1. 今天的交付和边界

完成一份锁定公开 PDF 的真实上传、解析、74 个片段的 Embedding、PostgreSQL 持久化、READY 状态收尾，以及带用户和年份条件的精确检索。检索返回原文、来源和物理页等定位，不生成研究答案或医疗建议。

新增本地显式索引/检索入口，没有新增公开 HTTP 路由。现有 RQ worker 仍只执行解析；不能把本次显式索引称为上传后全自动摄取。`POST /research`、研究工作流、公开 retry、医学拒答和生产部署仍未实现。

输入是可信身份对应的 owner、文档 UUID、现有片段和固定模型规格；检索另接收问题、可选文档范围、发表年份区间和 1–5 的结果上限。公开身份仍由 API Key 决定，客户端不能传 owner。演示仅写 `traceagent_test`，应用库原记录、文件和凭据保留。`sources/` 没有修改，当前工作目录没有该目录。

Day 8 的开发者人工复核和第三条真实冲突用例仍待完成。今天核对第 5 页原文上下文并验证 dev-02 命中，属于代理执行的局部复核，不能代替开发者签名、全量语料冻结或盲测。

## 2. 阅读代码的顺序

| 文件 | 职责 | 阅读时要回答的问题 |
|---|---|---|
| `app/retrieval/contracts.py` | 索引规格、版本、检索输入、安全错误 | 哪些改变会让旧向量不再兼容？ |
| `app/db/models.py`、`migrations/versions/0004_embeddings.py` | 三张新表、外键、唯一约束和迁移 | 哪些不变量由数据库保护？ |
| `app/db/embeddings.py` | 规格保存、向量写入、含 owner 的精确查询 | 绕过 service 时，仓库是否仍防越权？ |
| `app/services/indexing.py` | 执行权、预算持久化、分批调用、原子发布 | 崩溃、超时、重复和迟到结果如何处理？ |
| `app/services/ingestion.py` | 兼容“解析成功、索引失败”的片段保留 | 为什么 FAILED 不再必然意味着没有片段？ |
| `app/services/retrieval.py` | 输入校验、权限与条件、证据完整性、数据快照 | 为什么模型调用前后都要检查范围？ |
| `scripts/day10.py` | 本地入口、真实演示、隔离库保护和安全报告 | 哪些操作接触真实服务？ |
| `tests/test_day10*.py` | 确定性边界与真实数据库故障回归 | 测试在证明哪一层，不能证明什么？ |

## 3. 三张表分别解决什么问题

`embedding_profiles` 保存模型名、完整权重 digest、768 维、`nomic-task-prefix-v1`、归一化约定和 `ingest-v1`。这些字段的规范 JSON 生成短版本 `e1-5741880e77b9a1e922904924`。现有 `documents.index_version` 只有 32 字符，因此完整 digest 单独存储。读取时比较完整规格，不能只相信短 ID。

`chunk_embeddings` 以 `(chunk_id,index_version)` 为主键，保存 `vector(768)` 和片段原文 SHA-256。外键指向已有片段与规格；片段删除时其向量级联删除。原文、PDF 页码和 TXT 偏移继续取自 chunks，避免复制后漂移。数据库拒绝零向量；应用另外拒绝数量错误、布尔值、NaN/Infinity、错误维度和非单位向量。

`index_tasks` 为一个摄取父任务保存一次显式索引执行：execution_id、状态、开始/截止/完成时间、安全错误和预算快照。它与 parse_tasks 分开，避免把已经成功的解析阶段改成失败。当前索引尝试为一次，失败终止，不实现手动重试或自动重试调度。

迁移 0004 新增三张表，不重写历史文档或片段。运行账号新增这三张表的 CRUD 权限，仍没有建表权限。Day 3 的迁移命令改为读取实际 revision，修复原先一直报告 0003 的旧常量；查询使用迁移账号，业务账号仍无权读取 alembic_version。最终命令复现发现了账号误用，已修正，并用真实数据库权限回归与模拟未来 revision 测试分别验证。

## 4. 索引时怎样划分事务

1. 在短事务中锁定用户和文档，检查用户活跃、解析阶段成功、文档 PARSED、父任务 PENDING。读取固定输入快照，建立唯一 execution_id、300 秒以内的截止和预算记录；父任务进入 RUNNING、attempt_count=1。
2. 事务关闭后核验原文件，按最多八条分批 await Day 9 的 Embedding 适配器。模型等待期间没有占用数据库连接；测试实际检查连接池并验证。
3. 每次真实推理请求前先把次数与保守用量预留写入数据库。自动追加的一次请求也经过这个入口。每批结束再保存已知实际统计和调用记录。
4. 全部向量在内存中收齐、验证规格与单位长度，并再次核验原文件。
5. 最终短事务重新检查用户、执行标识、截止、元数据、文件引用及全部片段；写入完整向量，再一次性提交 document READY、index_tasks SUCCEEDED 和父 job SUCCEEDED。

没有把远程模型调用放进 SQL 事务。持有数据库锁等待网络会占用连接、延长锁时间，并放大故障。同步 SQL 调用仍在当前本地异步入口中执行；已有连接、锁和语句超时限制每次数据库操作。它不是高并发异步数据库实现，也不是硬实时截止保证。

本地显式索引使父任务从 PENDING 直接进入 RUNNING，不假报一次不存在的队列投递。QUEUED 仍属于已有真实解析投递流程；公开上传/状态响应形状保持不变。

## 5. 状态、失败与恢复

| 情况 | 文档 | 解析阶段 | 索引阶段 | 摄取父任务 | 向量 |
|---|---|---|---|---|---|
| 解析完成，未索引 | PARSED | SUCCEEDED | 不存在 | PENDING | 无 |
| 索引执行中 | PARSED | SUCCEEDED | RUNNING | RUNNING | 尚未发布 |
| 完整索引提交 | READY | SUCCEEDED | SUCCEEDED | SUCCEEDED | 全部 |
| 已知模型/向量/截止/取消/文件失败 | FAILED | SUCCEEDED | FAILED | FAILED | 无；保留原片段 |
| 数据库提交结果不确定 | 不能凭异常推断 | 不覆盖 | 核对数据库 | 核对数据库 | 可能已完整提交，也可能未提交 |

原解析服务把“FAILED 且存在 chunks”统一视为数据损坏。现在只有同时存在成功 parse_tasks、失败 index_tasks、失败父任务和一致安全错误时，才承认这种合法的索引失败；其他异常组合仍拒绝。回归测试覆盖合法保留和非法组合，不能简单删除原完整性检查。

同文档成功重放会核对规格、片段集合、原文哈希、向量完整性、成功任务与原文件，不再次调用模型。失败重放返回既有失败，不重置次数或生成新执行。

未过期 RUNNING 的重复执行拒绝，不能并发再跑。进程意外退出后没有后台自动修复；在截止之后再次调用同一索引入口会把过期执行记录为 index_timeout。若遇到不一致的完成状态，拒绝覆盖并要求核对。人工篡改处理版本等数据异常也需要明确修复，不承诺任意损坏都可自动恢复。

如果最终 COMMIT 已成功但确认丢失，调用者得到 database_unavailable。再次调用时会核对并复用已提交索引；不能捕获数据库异常后盲写 FAILED。若写入事务回滚，片段保留、向量整体回滚，执行记录保留 RUNNING 待期限核对。这两条路径都用真实 PostgreSQL 加故障注入验证。

取消会保留已预留预算并记录 index_cancelled，然后继续传播取消。客户端取消不证明服务端推理已经停止。模型身份核对也不能原子证明响应必然来自某份权重，仍保留 Day 9 的限制。

## 6. 为什么先限制一份文档

每批最多 8 条，预算对象最多 32 次 Embedding 请求。当前预检为每批预留最多两次请求空间，因此最多接受 16 批、128 个片段。74 个片段为 10 批，首次真实演示实际调用 10 次，不代表人为压成 10 次预算。

超过该容量时，在创建索引执行前明确返回 index_budget_exhausted；文档维持 PARSED、父任务维持 PENDING。这是预检拒绝，不是已执行任务失败，仍占未结束配额。不要提高配额、删除历史任务或循环创建预算对象绕过限制；未来需要明确的容量/分片与失败收尾设计后再推进大文档。

779 个片段全量索引、十份资料 seed 和完整检索评测都没有执行。当前一次索引预算持久化不等于未来研究任务跨手动重试的预算已经完成。

## 7. 检索如何保持可追溯和权限

输入严格校验：问题去首尾空白后 1–2000 字符；文档 ID 是不重复的规范 UUID，最多 15 个，显式空数组拒绝；年份为严格整数且区间不倒置；limit 为 1–5。字段 owner、任意 URL、模型名和 SQL 都不接受。

先检查活跃用户与显式文档所有权。其他用户的 ID 与不存在的 ID 同样抛 resource_not_found，并且不发起模型请求。省略文档列表时仅枚举自己的资料；当前内部入口上限为 100 份，超过明确拒绝，后续需单独分页设计。

只考虑 READY 且兼容当前索引的文档。年份过滤包含端点；有年份条件时排除未知年份，并返回原因。PRISMA 2020 的版本名含 2020，但发表年份是 2021，所以过滤发表年份 2020 得到空集。空集直接返回，不调用模型，也不生成答案。

非空时，用 `search_query:` 生成问题向量；文档使用 `search_document:`。SQL 使用绑定参数、owner、活跃用户、READY、文档集合、年份和索引/解析版本条件，再按 `<=>` 余弦距离升序取结果。同分使用文档 ID、ordinal 和 chunk_id 稳定排序。仓库本身也带 owner 条件，不能只依赖上层先查一遍身份。

向量调用前后都核对资料范围、元数据、片段身份和哈希。返回条件、实际资料范围、排除原因、来源文件 SHA-256、来源/许可/年份/版本/研究日期、原文与物理页或字符范围、索引规格及数据快照。数据快照由资料和片段身份确定，不包括运行耗时与用量，因此相同证据可复核；它不是整个数据库的事务快照或可信数字签名。

距离只排序证据候选，不证明结论受支持，也不是概率或置信度。来源命中、正确片段命中、结论支持和医学安全必须分别验收。将来接入回答生成时，无支持内容必须拒绝；不能因为总能返回最近邻就认定有答案。

## 8. 测试和真实实验分别证明什么

`tests/test_day10.py` 检查输入、版本、向量数值边界、报告覆盖及旧迁移状态回归。`tests/test_day10_database.py` 用真实 PostgreSQL，推理主要为确定性替身或 HTTPX MockTransport；包括三维运算、768 维往返、原子提交、唯一/零向量约束、失败保留、并发、取消、超时、权限、排序、规格/原文漂移、预先落盘、有限重试、提交确认丢失和迁移往返。

真实 demo 使用锁定 PDF、真实本机 Ollama 0.40.2、真实 PostgreSQL，以及真实本机 HTTP 上传/状态查询。检索与索引为内部 service 调用，没有假称 HTTP /research 已上线。dev-02 第 5 页预期证据进入 Top 5；具体距离、耗时、Token 与请求次数以执行记录为准。它只是一道固定开发题的冒烟检查，不能计算全语料召回率或医学回答支持率。

完整测试实际数量、通过/跳过和耗时见结果 JSON。基础 check 明确关闭真实数据库、Redis/worker 和容器测试，跳过不能算通过；另行执行 day6 test 启用真实依赖。远端 CI 状态需在推送后单独检查，本地结果不能冒充 GitHub Actions 结果。

## 9. 在自己电脑上复现

已有锁定依赖与此前安全凭据时，在项目根目录依次执行：

```bash
bash scripts/dev.sh uv sync --locked --python 3.12.14 --dev
bash scripts/dev.sh colima start --cpu 2 --memory 2 --disk 10 --vm-type vz --mount "$PWD/.cache/day4/uploads:w" --ssh-config=false
bash scripts/dev.sh .venv/bin/python -m scripts.day3 up
bash scripts/dev.sh .venv/bin/python -m scripts.day3 migrate
bash scripts/dev.sh .venv/bin/python -m scripts.day6 up
bash scripts/dev.sh .venv/bin/python -m scripts.day10 test
bash scripts/dev.sh .venv/bin/python -m scripts.day6 test --report-label local
bash scripts/check.sh
```

真实演示前，在另一个终端运行 `bash scripts/dev.sh ollama serve`，沿用已有固定模型。缺少锁定 PDF 时先运行 `scripts.day8 prepare --download`（通过相同 dev 包装器）；损坏文件不能自动覆盖。

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day10 demo
```

demo 使用隔离库和 `.cache/day10/uploads/`，不删除应用库记录。它会启动临时本机 API 并在结束后关闭；8000 被占用时拒绝运行，不接管已有服务。数据库测试与 demo 使用同一个隔离库，必须串行。测试可能清理之前的演示数据库记录，但缓存报告保留当次执行证据；需要保留可查询演示状态时，最后运行 demo。

实际应用资料已有 PARSED 文档时，本地显式操作：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day10 index <document_uuid> --owner A
bash scripts/dev.sh .venv/bin/python -m scripts.day10 search --owner A --question "Locate reporting guidance" --documents <document_uuid>
```

这两条默认使用应用库，只有自己确实打算推进已有文档时执行。不要复制尖括号占位符。Key 从受保护本地凭据读取，不粘贴到命令或聊天。完整结果保存在被忽略的 `.cache/day10/index.json` 或 search.json；终端只输出安全摘要。demo/test 返回非零表示失败，报告会覆盖此前成功，避免误读旧成绩。

## 10. 缓存、数据和 Git

唯一清单仍为 `.cache/manifest.json`，由 scripts/dev.sh 原子维护，新增 `day10_index_reports_logs_and_test_artifacts` 条目。该清单和整个 `.cache/day10/` 在 .gitignore 中明确忽略。没有第二份缓存清单。

新目录管理 Ollama 日志、测试输出/XML、原始演示报告、迁移前保留指纹和隔离上传文件。已有 uv/Python/pytest/Colima/Redis 数据继续由同一清单的原条目管理。永久脱敏交付记录放 docs；不提交向量、资料全文、完整模型日志、凭据、数据库地址或缓存。

Day 10 缓存中的上传文件只与 `traceagent_test` 配套；清理其文件时也要处理对应测试库，不能在仍要复用演示状态时单独删除文件。应用库对应的 `.cache/day4/uploads/`、数据库/Redis 凭据和 API Key 继续随原卷保留，绝不能删除整个 .cache 来“清缓存”。本次完成后停止本次启动的模型和容器服务，保留权重及数据卷。

提交明确选择本日文件，保留用户预先修改的 AGENTS.md；不使用 git add . 把无关修改一起提交。使用正常快进推送，不覆盖远端历史。

## 11. 当前方案核对与五项审查

2026-10-19 核对官方项目文档、版本及运行环境。新增并固定 pgvector Python 0.5.0；其发布包没有强制传递依赖，本次锁文件只增加此包。先用真实数据库验证三维距离和 768 维往返，再采用 SQLAlchemy 类型适配，减少自写转换。没有升级 SQLAlchemy 2.1.4、Psycopg 3.3.6、Ollama 0.40.2 或固定权重。

数据库保留 PostgreSQL 18.6/pgvector 0.8.7，采用精确检索基线。维护者提供 SQLAlchemy/Psycopg 3 集成及 RAG 示例，发行记录持续维护；这些支持可用性判断，不证明本项目检索质量。HNSW/IVFFlat 可以用召回换速度，但当前没有实测瓶颈，增加调参与过滤复杂度没有本日收益证据。混合全文检索、重排和换 Embedding 模型暂缓，先记录具体漏召回，再比较相同开发集上的正确性、召回、延迟、资源、兼容性与迁移成本。

官方依据：

- [pgvector 精确/近似检索、过滤与距离](https://github.com/pgvector/pgvector)
- [pgvector 0.8.7 安全修复](https://www.postgresql.org/about/news/pgvector-087-released-3392/)
- [Python 维护者集成与示例](https://github.com/pgvector/pgvector-python)、[发布包](https://pypi.org/project/pgvector/0.5.0/)
- [SQLAlchemy 2.1 事务](https://docs.sqlalchemy.org/en/21/orm/session_transaction.html)
- [Nomic 作者的任务前缀](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)
- [Ollama Embedding 与截断](https://docs.ollama.com/api/embed)、[运行时发布记录](https://github.com/ollama/ollama/releases)
- [PostgreSQL 18 全文检索](https://www.postgresql.org/docs/18/textsearch-intro.html)

| 审查项 | 本日检查 |
|---|---|
| 正确性 | 向量与原片段一一对应、规格一致、距离方向、年份、原子完成和稳定快照 |
| 错误处理 | 预检、有限重试、截止、取消、迟到、数据漂移与 COMMIT 不确定分别处理 |
| 测试 | 成功/边界/失败、真实数据库、真实模型分开记录；跳过不算通过 |
| 安全 | API Key 身份、仓库 owner 条件、参数绑定、安全错误、缓存忽略、来源只读 |
| 可维护性 | 平台没有医学规则；存储、索引、检索与演示分层，保持原接口形状 |

当前检索为小规模正确性基线：调用前后读取所有候选资料的向量完整性会增加数据库和内存开销，不能直接宣称支持大规模生产。SQL 同步调用、文件读取与合作式取消也不等于恶意输入隔离或硬资源保证。完整 RAG、研究任务累计预算、人工复核、全语料评测和部署门槛仍有后续工作。

## 12. 如果由你独立开发，重点注意什么

先写输入、输出和状态表，再让 AI 写一个小切片。第一步只验证向量能往返数据库，第二步只索引一份文档，第三步加入检索与失败回归。不要先让 AI 一次性生成整个 RAG 系统。

你必须独立检查事务和 await 的位置、锁顺序、唯一约束、失败分支以及每个错误码。问清“发生在请求前、模型完成后、COMMIT 前还是 COMMIT 后”，这些位置的正确处理不同。次数预留必须在推理前落盘；结果未确认时保留未知，而不是补零。

不要把异常全部转成同一种业务失败。模型已知失败可以终止任务；数据库确认丢失则要先核对，盲写失败可能覆盖真实成功。不要删原片段解决索引失败，也不要把 PARSED 改成 READY 让演示看起来完成。

身份检查不能交给 Prompt。显式文档 ID 要先验权，查询 SQL 还要带 owner；相似度阈值不能代替权限。资料中的指令也只是资料内容，后续接入生成时仍需单独做间接注入防护。

版本名、发表年份、研究日期和检索日期分开理解。原文页码正确不等于结论正确，要读上下文并保留限制和冲突。固定一道开发题命中不等于全语料好用；不要用验收集反复调参后再称盲测。

审查真实 diff、执行测试、读原始报告，再提交明确文件。AI 可以起草代码、设计故障注入和整理证据；你要能独立解释结果与限制。今天对应 AI Coding 的需求分解、审查、调试、测试与可复现交付，对应实习中的 Python 后端事务、RAG 数据处理、任务状态和安全；这些是准备重点，不保证公司考察具体题目，也不涉及训练模型或算法研究。

三分钟口述顺序：先说一份文档与问题的输入；再解释领取与发布事务、中间的分批 Embedding 及逐次预算检查点；接着用索引失败与 COMMIT 确认丢失对比；最后展示第 5 页检索结果、年份空集及评测局限。这里提供可执行演示和口述提纲，没有代替开发者录制或通过口头考核。

自测：

1. 为什么推理请求前需要先落盘预算，重启后又为什么不能直接重新创建预算继续跑？
2. 第三批模型超时与最终 COMMIT 确认丢失，为什么不能采用相同失败处理？
3. 怎样分别证明 SQL 排序正确、找到了预期证据，以及生成结论得到证据支持？
