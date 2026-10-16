# Day 7 摄取阶段验收与独立开发讲解

验收日期：2026-10-16，时区 Asia/Kuala_Lumpur。Day 7 的范围是第一周后端摄取门槛：空库迁移、身份、上传、独立 worker、片段保存与状态。结果见 [实际验证记录](day7-results.json)。

## 1. 交付结论与边界

本日完整测试实际为 417 项通过、0 项跳过、0 项失败、0 项错误。包括原有 402 项与新增 15 项验收断言测试。真实 HTTP/Redis/PostgreSQL/RQ 演示、公开 PDF 持久片段检查、配额和停机恢复均已执行。

新增 scripts/day7.py 复用现有 API、解析器和 worker，补充可重复执行的真实来源验收。它不新增业务路由、迁移或依赖，不改变解析规格和公开 API 字段。没有发现需要修改业务实现的摄取缺陷；本日补充的是此前合成文件演示之外的真实来源与交付证据。

成功文档为 PARSED，parse_tasks 阶段为 SUCCEEDED，完整 ingest 父任务为 PENDING、attempt_count=0、index_version=null。Embedding 和完整 ingest 尚未完成，不能把阶段成功写成 READY 或父任务 SUCCEEDED。

工程验收不能验证开发者本人是否已经能够独立解释。下文提供阅读路线、演示稿和自测题；独立讲解能力仍需本人实际检验。没有模型回答、医学质量、远端部署或完整 TraceAgent 发布验收。

## 2. 输入、输出与验收条件

输入一是带 UTF-8 BOM、中文和 CRLF 的合成 TXT。原文件 4403 字节，解码后 2000 字符，形成 3 个片段。定位使用解码后 Python 字符串的半开区间，不是文件字节偏移；BOM 不计入文本位置。

输入二是来源清单中已经锁定 SHA-256 的 PRISMA 2020 公开 PDF：825448 字节、15 个物理页、提取 61579 字符、74 个片段。版本标签是 PRISMA 2020，发表年份是 2021，二者分别保存。来源 URL、许可与依据、标题和研究日期说明一起核对。没有修改或重新分发原 PDF。

输出包含文档/任务标识、片段数量、全部片段定位检查、两个持久片段锚点、重放和权限检查，以及应用数据前后摘要。原始 Key、连接串、全文和异常堆栈不进入验收结果。

通过条件：真实独立 worker 完成解析；每个保存片段能对应原文；来源元数据保持一致；重放复用文档、任务、执行标识和片段；越权/配额/来源冲突正确处理；真实故障可观察和恢复；原应用资料与凭据保持不变。

## 3. 实际怎样完成验收

1. 检查 Git、依赖与已有改动。当前分支 main；用户原有 AGENTS.md 修改保留，未纳入本日代码提交。sources/ 当前不存在，没有创建或修改同步来源目录。
2. 通过现有开发包装器离线核对锁定依赖。启动原 Colima VM、固定镜像的 PostgreSQL 和 Redis，保留原数据卷与凭据。首次 VM 启动受执行沙箱限制，使用获准的本机运行权限后启动成功。
3. 使用迁移账号核对现有迁移到 0003 与模型一致性，并恢复运行账号所需授权。应用账号不承担迁移职责。
4. 验收前，以只读、一致性事务计算五张应用表的行数与内容摘要；另在受保护缓存中记录上传文件和三份凭据的指纹。没有保存表中原始内容到报告。
5. 新增验收检查器及单元测试；先验证它确实会拒绝错误页码、错误正文、非法字符范围和空片段。
6. 运行完整真实依赖测试，包括空库降级/升级、约束、并发领取、确认丢失、超时、worker SIGKILL、迟到写入拒绝、身份和队列安全。
7. 执行现有 Day 6 HTTP 演示，验证合成 TXT/PDF、损坏 PDF、重放、owner 隔离及 worker 心跳。
8. 执行 Day 7 真实文件验收。只写 traceagent_test、Redis DB 1、测试队列及 .cache/day7/uploads/。用另一用户填满五个未结束任务，验证第六个新任务为 429，原任务重放仍允许。
9. 实际停止并恢复 Redis/PostgreSQL，检查健康、就绪、业务错误、保留 job_id 及同任务恢复。
10. 对应用文件执行只读 audit，并比对验收前后的表摘要、文件字节与凭据指纹，确认均未改变。记录结果、风险、第一周成果和岗位能力映射。

测试、demo 和停机检查必须串行。历史应用文档不能为了演示被清空；测试中的降级和清理始终由 traceagent_test 保护条件约束。

## 4. 真实来源与人工抽查

程序逐一检查全部 3 个 TXT 和 74 个 PDF 持久片段。TXT 通过原始字节 UTF-8-sig 解码后的切片核对；PDF 使用片段声明的物理页文本核对，不用切分函数自己生成预期再自我比较。该检查证明保存片段与声明位置一致，不独立证明整份 PDF 的版面提取完整性。

两个固定来源锚点分别落在物理页 5/片段 ordinal 22、物理页 6/ordinal 27。使用 Poppler 渲染并目视查看第 5、6 页，确认锚点实际位于对应原文页，页脚物理编号与文件一致；TXT 抽查保留中文、BOM 解码和 CRLF 的定位语义。渲染图位于统一管理的 .cache/day7/，不提交 Git。

普通 PDF 提取仍可能存在连字、断行、表格和复杂版面问题。定位与锚点核对不构成医学证据质量认证，也不是完整语义检索评测。

## 5. 三种状态必须分开解释

| 对象 | 本阶段成功 | 失败语义 |
|---|---|---|
| 文档 documents | UPLOADED → PARSED；index_version=null | 已知解析失败保存 FAILED 与安全错误码 |
| 解析阶段 parse_tasks | PENDING → QUEUED/RUNNING → SUCCEEDED | 内容、执行或截止失败保存 FAILED |
| 完整 ingest 父任务 jobs | 保持 PENDING、attempt_count=0 | 本阶段解析失败时随文档和阶段原子保存 FAILED |

QUEUED 的确认与 worker 领取可能竞争，不能因迟到的投递确认让 RUNNING 或终态倒退。重复消息不能延长 deadline；旧 execution_id 或过期结果不能取得新的写入权。

等待未来索引的父任务 PENDING 是当前阶段的明确边界，不属于丢失的解析队列任务。它仍占每用户五个未结束任务的配额。本日真实验证了这个限制，没有用清库、改配额或假成功来绕过它。

## 6. 文件、数据库和队列的事务边界

| 步骤 | 已经承诺什么 | 出错后怎样处理 |
|---|---|---|
| 文件发布 | 服务器生成存储键并保存完整字节 | 确认回滚的业务拒绝可清理本次候选；不覆盖旧文件 |
| 上传登记事务 | 文档、父任务、解析阶段一起提交 | 数据库异常使用安全错误；模糊 COMMIT 不能据此删除候选 |
| Redis 投递 | 数据库已有执行意图，队列接受运输消息 | 投递失败保留记录与文件，503 附 job_id；允许同请求重放/显式核对 |
| 阶段领取短事务 | 校验身份、文档、版本、执行标识并领取 RUNNING | 重复或过期消息拒绝领取，截止不重置 |
| 文件验证与解析 | 在事务之外执行耗时工作 | 文件损坏、解析失败、超时使用固定错误码 |
| 结果保存短事务 | 再次核对身份、执行权、状态和截止，片段与终态一起提交 | 拒绝迟到写入；保存失败不谎报已经持久化失败 |

没有一个 SQL 事务能够覆盖本机文件系统、PostgreSQL 和 Redis。项目依靠持久意图、数据库约束、条件领取、幂等重放和显式恢复处理这些边界，不宣称全局 exactly-once。

## 7. 实际测试结果及证明范围

| 检查 | 结果 | 证明范围 |
|---|---|---|
| 新增检查器单元测试 | 15 passed | 成功、空白页/Unicode 边界、错误定位/内容、保护范围和失败报告 |
| 完整测试 | 417 passed，0 skipped，31.28 秒 | 包含真实 PostgreSQL、Redis、容器和独立 worker；亦包含明确 mock 和故障注入 |
| Day 6 HTTP 演示 | 19 项检查均通过 | 合成正常/失败文件、状态、重放、owner 与 worker 就绪 |
| Day 7 真实来源演示 | 23 项布尔检查均通过，77 个片段定位通过 | 真实公开 PDF、中文 TXT、来源/哈希、重放、认证、冲突、配额、应用表不变 |
| 实际停机/恢复 | 7 项检查均通过 | Redis/数据库真正停止，503/health 行为及原任务恢复 |
| 应用文件 audit | 8 个引用文件，无缺失/损坏/孤立候选/暂存文件 | 应用库与真实文件一致，审计只读 |
| 数据保留比对 | 表、文件、凭据均一致 | 完整验收期间原应用数据未改变 |
| 静态检查 | Ruff lint/format、离线锁检查、diff 空白检查通过 | 源码与锁文件卫生；不能代替真实集成 |

保留一个已有的 Starlette TestClient/httpx 弃用警告，不为消除与本日验收无关的警告新增 HTTP 库。没有增加或升级依赖。

HTTPX 替身、broker 替身、模拟时间和数据库异常注入不属于真实网络事故。worker SIGKILL、进程回收、真实服务停机属于实际执行检查。没有等待真实 30 分钟来验证所有排队超时；相关用例明确推进测试时钟/数据库时间。

未执行真实模型调用、RAG 召回率、引用支持率或医学回答评测；没有公开部署。417 项通过不能被表述为“完整 RAG 已完成”。

## 8. 如何重新运行

从项目根目录运行；已有 .venv 与下载缓存时：

```bash
bash scripts/dev.sh uv sync --locked --offline --python .venv/bin/python --dev
bash scripts/dev.sh .venv/bin/python -m scripts.day3 prepare
bash scripts/dev.sh .venv/bin/python -m scripts.day4 prepare
bash scripts/dev.sh .venv/bin/python -m scripts.day6 prepare
bash scripts/dev.sh colima start --cpu 2 --memory 2 --disk 10 --vm-type vz --mount "$PWD/.cache/day4/uploads:w" --ssh-config=false
bash scripts/dev.sh .venv/bin/python -m scripts.day3 up
bash scripts/dev.sh .venv/bin/python -m scripts.day3 migrate
bash scripts/dev.sh .venv/bin/python -m scripts.day6 up
bash scripts/dev.sh .venv/bin/python -m scripts.day6 test
bash scripts/dev.sh .venv/bin/python -m scripts.day6 demo
bash scripts/dev.sh .venv/bin/python -m scripts.day7
bash scripts/dev.sh .venv/bin/python -m scripts.day6 offline-check
bash scripts/dev.sh .venv/bin/python -m scripts.day6 audit
```

上述顺序在完整测试清理隔离夹具后建立演示。Day 6 与 Day 7 的测试上传使用不同目录，但共用测试库，队列也必须串行。Day 7 重放沿用相同文件/元数据；已有其他测试任务占满额度时会明确失败，不擅自删除数据。不要在保留这些演示数据时运行会清理测试库的另一套测试。

首次缺少 .venv 或缓存包时，使用 `uv sync --locked --python 3.12.14 --dev` 允许下载；公开 PDF 缓存缺失时使用 `scripts.verify_sources --download` 恢复并核对锁定哈希。其他首次前提、运行账号与文件共享说明沿用 Day 6 文档。

源码静态检查可复用 scripts/check.sh；该脚本另会运行服务无关测试并明确跳过集成检查。本日完整测试已覆盖全部 417 项，没有再把跳过项计为通过。修改业务后运行相关回归，必要时重新执行完整验收。

停止服务并保留卷：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day6 down
bash scripts/dev.sh .venv/bin/python -m scripts.day3 down
bash scripts/dev.sh colima stop
```

不要添加删除卷的选项。现有端口被其他进程占用时，演示拒绝接管。

## 9. 如果由你开发，怎样拆成可理解的步骤

先手写输入、输出、三种状态及成功/边界/失败预期，再看实现。第一步只上传一个短 TXT；第二步确认独立 worker 写出片段；第三步核对定位与重放；最后加入公开 PDF、配额和故障。每一步保留一个能运行的小成果。

建议阅读顺序：app/api/documents.py → app/services/uploads.py 与 documents.py → app/services/tasks.py → app/tasks/worker.py → app/services/ingestion.py → app/db/repositories.py。解析细节再看 app/ingestion/parser.py 和 text.py。

- Python：理解 bytes 与 str、UTF-8-sig、半开切片、可变对象、闭包捕获、上下文管理器和异常分类。中文字符数与 UTF-8 字节数不同；不要清洗文本后继续使用旧偏移。
- HTTP：202 是接受处理；查询 200 可以包含业务 FAILED。401、404、409、429、503 分别对应身份、资源、冲突、配额和依赖问题。
- SQL：理解主键、复合外键、唯一约束、CHECK/NULL、事务回滚和行锁。先查重再插入仍有竞争，最终数据库约束不能省略。
- Session：请求和任务独立持有 Session，worker 进程独立创建连接池。解析、Redis 或未来模型等待不占 SQL 事务。
- 幂等：分别审查文件/处理版本、文档/任务、队列消息、数据库执行权和结果保存；仅设置 RQ unique 不能保证持久业务幂等。
- 截止：区分连接/命令等待、排队时限、解析子进程和阶段执行截止。取消等待不代表停止计算；需观察子进程实际结束与迟到结果拒绝。
- 安全：owner 来自认证；消息只传执行标识，数据库重新决定资源；JSON 仍需固定函数白名单和可信 Redis。响应、日志、进程参数和测试失败输出都不能泄露凭据。
- 测试：预期来自契约和人工样例。不能只断言 HTTP 200，也不能只让切分函数与自身输出比较。验证真实保存结果、第二次执行及失败后的残留状态。
- 交付：保留失败例、锁文件、环境前提、实际命令、跳过项和限制。一次 COMMIT 异常不能推出“数据库肯定没写入”。

AI 可辅助梳理调用链、起草参数化测试、查询官方资料和提出小补丁。你必须独立决定预期，阅读 diff，确认测试目标，实际运行，并解释哪些结果不能由测试推出。不要一次让 AI 重构整条摄取链再尝试理解。

## 10. 五项审查

正确性：真实字节哈希、元数据、TXT 范围与 PDF 物理页、三种状态、重放标识及配额均检查。错误处理：损坏文件、提交/投递边界、进程中断、过期结果与停机恢复均有对应证据。测试：成功、重要边界、失败、真实依赖与替身分别记录。安全：本机绑定、认证、owner、JSON/入口白名单、隔离测试目标和安全报告保持。可维护性：新脚本复用已有服务，只扩展验收与清单，不引入业务框架或新依赖。

## 11. 缓存统一管理

唯一清单仍为 .cache/manifest.json，由 scripts/dev.sh 原子生成；.gitignore 已有明确清单忽略，并新增 /.cache/day7/。没有建立第二份缓存管理清单。

Day 7 的验证报告、验收前后摘要、测试报告副本、PDF 页预览和 uploads 全部归入 .cache/day7/。复用 Day 3/4/6 启动器时产生的日志仍属于清单中原有目录。bytecode、pytest、uv、临时目录和 VM 继续由同一清单管理。运行日志与清单都不提交；docs 中的脱敏交付证据提交。

Day 7 测试上传可随测试库重建。应用上传文件、API Key、数据库凭据必须随数据库卷保留；Redis 凭据随其卷保留。不能把整个 .cache 当成随意可删的下载目录。本日没有删除应用数据或轮换凭据。

## 12. 2026-10-16 实现方式检查

保留 Python 3.12.14、FastAPI 0.141.1、SQLAlchemy 2.1.4、Alembic 1.20.0、RQ 2.12.0、redis-py 8.1.0、pypdf 6.20.1。安装版本与项目锁定一致。

- [RQ 正式发布](https://pypi.org/project/rq/2.12.0/)与 [worker 文档](https://python-rq.org/docs/workers/)支持现有 JSON/spawn 方向；SimpleWorker 缺少执行期间周期心跳等特性，不为简单微基准替换。在线文档含其他主版本功能，不能直接套到锁定版本。
- [FastAPI 后台任务说明](https://fastapi.tiangolo.com/tutorial/background-tasks/#caveat)区分轻量后台任务与独立执行工具。本项目需要持久状态和恢复，保留现有队列。
- [SQLAlchemy Session](https://docs.sqlalchemy.org/en/21/orm/session_basics.html)支持按执行单元管理 Session；保留同步业务访问和短事务，不为验收整体迁移异步栈。
- [SQLAlchemy 发布记录](https://docs.sqlalchemy.org/en/21/changelog/changelog_21.html)中 2.1.5、[Alembic 发布记录](https://alembic.sqlalchemy.org/en/latest/changelog.html)中 1.20.1 无发布日期，延期；保留现有空库结构/约束/权限测试。[迁移 head 核对](https://alembic.sqlalchemy.org/en/latest/cookbook.html#test-current-database-revision-is-at-head-s)不能代替实际 DDL 验证。
- [Procrastinate 外部连接](https://procrastinate.readthedocs.io/en/stable/howto/production/external_connection.html)提供业务写入与任务登记同事务的潜在优势。但本项目尚未验证驱动适配、迁移、维护和性能收益，延期；出现具体维护痛点后再做小实验。

没有发现足以抵消迁移、兼容回归和排期成本的适用替换收益。不新增依赖，无付费模型成本；没有新增吞吐、延迟对照或医学质量实验，不作性能优越性结论。

## 13. 第一周复盘与岗位证据

第一周已有可展示成果：真实模型最小可用性实验、锁定工程和契约、数据库迁移、身份隔离与上传、定位切分、后台执行及本日集成验收。主要证据是各日 results、真实演示与可复现命令，不以代码量和学习时长计分。

2026-10-16 查看雇主发布的 [Seeka Backend AI & Data Pipeline Engineer](https://jobs.smartrecruiters.com/SeekaTechnology/744000117565313-backend-ai-data-pipeline-engineer)。页面标为 Intern，同时要求一年以上相关经验；这里作为岗位能力参照，不保证符合申请条件或职位持续开放。岗位涉及 Python 数据处理、可恢复流水线及架构沟通，本周可用摄取、故障恢复和文档作为部分证据。向量检索与模型应用仍是后续待补能力；AWS、MongoDB 和知识图谱等要求不因此加入当前项目。

另一个 Bosch 实习搜索结果打开后显示 expired，未将其当作当前开放机会。没有代用户投递申请或联系雇主。个人学历、时间安排、地域和申请资格仍需本人核对。

AI Coding 准备重点是需求分析、按故障边界拆任务、审查 AI 补丁、设计回归和可复现交付。后端实习重点是 Python/HTTP/SQL、事务、并发与安全；LLM 应用/agent 实习则用这些能力支撑未来 RAG 和任务状态。这是应用工程训练，不是模型训练/算法研究任务，也不是任何公司考试范围的承诺。

三分钟演示：先说明输入与三种状态；展示上传、解析和片段定位；重复上传验证标识稳定；展示一次故障与恢复；最后给出真实测试报告和剩余边界。简历描述只能写已验证的摄取、隔离、追溯及恢复，不写完整医学 RAG/生产部署已经完成。

## 14. 未完成能力与风险

本地受限摄取门槛内，没有发现未解决 P0/P1。这个结论限于已经执行的用例，不代表无缺陷或生产发布通过。

- 完整 ingest、Embedding、READY、检索、研究回答和医学拒答尚未接入；完整尝试与累计预算仍待实现。
- 恢复核对是显式命令，没有周期调度或失败阶段公开 retry；操作人员必须执行恢复流程。等待索引的父任务继续占额度。
- macOS 没有可用的解析地址空间硬上限；进程截止和回收不等于生产恶意文件沙箱。
- 复杂表格、混合扫描/OCR、全部版面与全文提取完整性没有验收；公开资料片段定位不等于结论支持或医学安全。
- 没有多机时钟/网络分区、备份恢复演练、全局配额或公开部署验收。远端 CI 结果须以实际 GitHub 运行记录为准。
- Git 提交发生在验证记录之后，提交/推送的最终状态以 Git 历史与远端引用核对为准。SSH Key 需要本机解锁，不在聊天、日志或仓库中保存口令。

## 15. 独立自测

1. 为什么解析阶段 SUCCEEDED，完整 ingest 仍应 PENDING，文档也不能 READY？
2. 数据库提交后 Redis 确认丢失，怎样恢复才能避免删除已保存资料或重复产生结果？
3. 哪些检查必须使用真实数据库/worker，哪些可以用替身，它们分别不能证明什么？
