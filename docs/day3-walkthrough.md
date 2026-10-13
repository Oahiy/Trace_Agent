# Day 3 完成记录与开发讲解

日期：2026-10-12，Asia/Kuala_Lumpur。目标是把 Day 2 的内存仓库练习推进到可持久化、可迁移、可回滚的最小后端链路。原计划的 D3 范围为 PostgreSQL/pgvector、users/documents/chunks/jobs 最小表、Alembic、空库迁移与回滚测试。

工程结果：真实完整测试 196 项通过，0 失败、0 错误、0 跳过；普通检查在独立虚拟环境中为 152 项通过、44 项明确跳过。Ruff 检查与 21 个 Python 文件格式检查通过。真实停机错误约 0.013 秒返回；数据库恢复后原文档与任务 ID 保持不变。没有调用真实模型、推送 GitHub、提交 Git 或公开部署。你的 AGENTS.md 已有改动被保留。开发者能否独立解释这些代码，还需要你亲自演示和回答末尾问题。

机器可读证据在 `docs/day3-results.json`。以下命令从项目根目录执行；验证摘要是本次实际运行结果，不代表以后每次运行都自动通过。

## 1. 交付物与范围

| 交付物 | 文件 | 作用 |
|---|---|---|
| 本机数据库 | compose.yaml | 固定镜像 digest、loopback 端口、健康检查、持久数据卷、安全日志配置 |
| 数据库配置 | app/db/config.py | 显式 URL、秘密包装、配置校验、有限超时 |
| 连接与生命周期 | app/db/session.py | Engine、有限连接池、每操作独立 Session、最终释放 |
| 表结构 | app/db/models.py | 四张业务表及数据库约束 |
| 持久化操作 | app/db/repositories.py | 查询、保存、owner 条件；不自行 commit |
| 业务操作 | app/services/documents.py | 元数据校验、去重、冲突、文档和任务同事务保存 |
| 结构迁移 | alembic.ini、migrations/ | 初始迁移、pgvector 扩展、升级和降级 |
| 演示与复现 | scripts/day3.py | 凭据准备、启动、迁移、演示、真实测试、停机检查、停止 |
| 测试 | tests/test_day3.py、tests/test_database.py | 单元与真实 PostgreSQL 成功/边界/失败检查 |
| 缓存 | scripts/dev.sh、.gitignore | 扩展唯一缓存清单与忽略规则 |
| 文档与证据 | README、本文、day3-results.json、API 契约 | 入口、解释、真实验证和阶段边界 |

没有新增 ORM 框架之外的应用抽象、前端、队列、LangGraph 或第三业务场景。Day 2 实验保持独立；今天的仓库操作不把内存字典冒充数据库。`sources/` 当前不存在，任务没有创建或修改同步来源目录。

## 2. 先确定输入、输出和失败

内部输入是合成文档的来源标识、标题、公开来源 URL、许可与依据 URL、可空发表年份、版本标签、研究日期说明、文件 SHA-256 和摄取版本。owner 由可信内部调用者传入，不在 payload 中。额外字段和错误类型拒绝，标题等去空白后检查长度，年份不接受 bool 或字符串。传入已有 Pydantic 实例也重新校验，防止 `model_copy(update=...)` 绕过边界。

输出是不可变 Registration：文档 UUID、任务 UUID、两个状态及 reused。首次保存为 UPLOADED/PENDING；同 owner、同哈希、同摄取版本、同元数据复用原文档和原任务。元数据不同抛固定 source_metadata_conflict，原记录不覆盖。查询返回不可变 StoredDocument 或 None；存储故障不会返回 None。

这些是内部存储行为。今天没有上传和保存真实文件，合成 UPLOADED 记录不能算上传验收。Day 4 的 HTTP 边界必须验证身份、从文件字节计算哈希并保存文件，不能相信客户端提供的 owner、哈希或处理版本。来源 URL 仅保存为元数据，不会被服务器抓取。

## 3. 环境与新依赖为什么这样选

实际环境为 Python 3.12.14、uv 0.12.24、FastAPI 0.141.1、Pydantic 2.13.4、HTTPX 0.28.1。新增直接依赖为 SQLAlchemy 2.1.4、psycopg[binary] 3.3.6、Alembic 1.20.0，并更新 uv.lock。Mako 是 Alembic 的模板依赖；没有手动加入另一套连接池。

SQLAlchemy 负责表映射、查询与 Session；psycopg 是 PostgreSQL 驱动；Alembic 负责版本化结构变更。binary extra 提供本机预编译驱动，减少编译配置。SQLAlchemy 已提供连接池，所以不额外安装 psycopg_pool。使用同步 Session 延续原计划，未来接入异步 handler 时明确安排阻塞调用，不把 async def 当作数据库异步化。

镜像固定为 `pgvector/pgvector@sha256:2358fcba361ed2233a5ed81b5fe4ca779ccb304120ce531a3bf51c0ed7e2bc11`，本次使用 arm64，实际 PostgreSQL 18.6、pgvector 0.8.7。初始化、连接、迁移、全部数据库测试及独立虚拟环境安装均实际验证了这套组合。没有根据版本新旧宣称它更快。

## 4. Compose、健康检查与权限

数据库只映射本机 `127.0.0.1:55432`，容器内为 5432。Compose 通过 pg_isready 检查接受连接，再由应用账号实际查询；两者证明的事情不同。pg_isready 不能证明密码、表结构、扩展或 worker 正确。

数据保存在名为 traceagent-day3-postgres 的 Docker volume。PostgreSQL 18 数据布局位于 `/var/lib/postgresql` 下，所以卷挂载父目录。容器重建、停止和启动均保留卷；实际重启后两个 ID 与之前一致。卷存在于本项目 Colima VM，宿主机不需要额外挂载整个用户目录。

开发初始化和迁移使用 postgres 管理员；业务演示用 traceagent_app。后者没有 superuser、createdb、createrole 和 public schema 建表权限，只获得四张业务表的读写权限，不能读取或修改 alembic_version。它是共享后端账号，数据库没有实现逐用户 RLS；用户隔离依赖可信身份和带 owner 的查询，不能把这个账号的限制当成完整鉴权。

PostgreSQL 日志配置不记录错误 SQL、参数和详细失败行；应用不启用 SQL echo。凭据由安全随机源生成，位于 0600 文件，不出现在模板或返回 JSON。开发工具持有初始化权限；未来 API/worker 的生产秘密应只包含各自所需权限。

## 5. 四张表与知识对应关系

users 保存内部 UUID、active 和创建时间。Day 3 两个固定用户只是合成演示对象，尚未实现 API Key 验证。

documents 保存 owner 外键、来源、许可、日期、版本、文件哈希、摄取版本、状态、索引版本和安全错误码。`(owner_id,file_sha256,ingest_version)` 唯一，允许不同 owner 或不同摄取版本独立保存。`(id,owner_id)` 另设唯一约束，用于 jobs 的复合外键。发表年份可以为 NULL；READY 必须有 index_version，但仅有该字段仍不能证明 Embedding 完成。

chunks 保存 document 外键、摄取版本、从 0 开始的片段序号、正文、可选章节、定位与时间。同文档/版本/序号唯一。PDF 页码从 1 开始；TXT 使用非负起点和大于起点的终点。定位必须选择其中一种；NULL 的每个分支显式检查，防止 PostgreSQL CHECK 对 NULL/unknown 的语义放过不完整范围。本文约定 TXT 范围为 `[char_start,char_end)`；真实位置与内容、处理版本一致性在 Day 5 摄取时验证。

jobs 保存 owner、文档关联、operation、status、attempt_count、幂等键与输入摘要、安全错误码及时间。ingest 必须关联文档，research 的单文档字段为空，后续研究范围由研究请求处理。`(document_id,owner_id)` 复合外键防止任务指向另一个用户的文档；`(document_id,operation)` 防止重复摄取任务；`(owner_id,operation,idempotency_key)` 约束未来幂等范围。NULL 幂等键允许普通摄取任务，非空键必须伴随输入摘要。

状态使用命名 CHECK 约束，合法集合与 API 契约一致。它们只限制可存的值，尚未实现合法状态转移、预算执行和 attempt 防迟到覆盖。数据库主键、外键、唯一约束、NULL、CHECK 与普通 B-tree 索引是今天要掌握的 SQL 基础；无需学习向量索引算法来完成本日交付。

## 6. 业务与持久化如何分层

`app/db/repositories.py` 只负责查询和保存。它们拿到同一个 Session，add 后 flush，绝不各自 commit。`app/services/documents.py` 校验输入、检查用户、处理去重并决定整个操作的事务边界。公开 API 今天未调用这些业务函数，原 health 行为没有变化。

一次首次登记：校验 payload → 开始 Session/事务 → 查询 active owner → 查同 owner 的指纹 → 生成 UUID → 保存并 flush 文档 → 保存并 flush 任务 → 离开事务上下文完成 commit → 返回成功。

flush 把变化发到数据库，可以得到数据库默认值并提前发现约束错误；它不等于 commit。文档 flush 成功后任务失败，整个事务会 rollback，文档也不存在。commit 本身失败同样不能返回 Registration 成功结果。测试用提交事件故障注入验证了这一点；注入不是一次真实服务端断电测试。

返回 DTO 在会话内构造，成功在提交后返回。读取也在会话内复制需要的值，避免把关闭 Session 后可能触发加载的 ORM 对象交给调用者。一个 Engine 可以管理连接池；每次业务操作的 Session 独立，两个并发线程不共享 Session。外部模型等待将来应放在事务之外。

## 7. 并发、去重与错误行为

先查再插入不能独立防并发重复。两个调用可能同时查不到记录，因此数据库唯一约束是最终裁决。测试用线程屏障让两个调用都通过预查询，再竞争插入；结果只有一个文档和一个任务，另一调用复用它们。

服务只识别命名的文档指纹唯一冲突，在原事务回滚后使用新 Session 读取获胜记录并比较元数据。这是一次冲突核对，没有重新执行失败写事务。其他完整性错误统一为 data_integrity_error；数据库或连接池错误为 database_unavailable；非法 owner 为 resource_not_found；元数据不一致为 source_metadata_conflict。存储中已有元数据损坏，也转成安全完整性错误。

查询同时包含 document_id 和 owner_id。跨用户和未知文档均读不到数据，但数据库故障抛出异常，不能伪装为“没有找到”。当前 owner 是内部可信参数；Day 4 HTTP 身份边界必须服务端确定它。

## 8. 超时、重试与生命周期

| 控制 | 默认值 | 实际验证 |
|---|---:|---|
| 连接建立 | 3 秒 | 真实拒绝连接及停机失败；不代表所有网络路径只等待 3 秒 |
| 连接池等待 | 2 秒，最多 2 个连接，无 overflow | 测试占满连接，使用 0.1 秒配置触发安全失败 |
| 单条 SQL | 3000 毫秒 | 100 毫秒配置取消 pg_sleep，并验证新 Session 可用 |
| 行锁等待 | 1000 毫秒 | 持有真实行锁，100 毫秒配置触发 55P03 |
| 空闲事务 | 5000 毫秒 | 1000 毫秒配置触发 25P03，随后重新连接成功 |
| 初始化 SQL | 10 秒，锁等待 1 秒 | 初始化操作成功，数值显式配置 |
| 工具子进程 | 常规 90 秒，读演示 15 秒，完整测试 240 秒 | 子进程超时不会原样返回命令或 stderr |

默认不自动重试数据库写入。连接、等待和单条 SQL 时限不是整个业务操作总截止时间。内部 probe 进行了多个检查，不满足完整 ready 契约的每依赖 1 秒/整体 3 秒要求，因此不挂载为 ready。后续 API 和 worker 接入时继续按契约实现整体截止时间。

`open_database` 在 finally 中 dispose Engine；Session 上下文释放连接，事务上下文提交或回滚。真实测试验证退出后池中连接归零。本次演示每次重新创建 Engine；尚未宣称长期应用连接池跨数据库重启能无失败恢复。

## 9. Alembic 怎么完成与怎么审查

从空应用库和 models 元数据生成 0001 候选迁移，人工检查字段、类型、nullable、默认值、约束、索引和依赖顺序，并补充 pgvector 扩展安装。没有用运行时 create_all 代替迁移。

upgrade 先安装扩展，再按 users → documents → chunks/jobs 顺序创建。downgrade 按相反依赖顺序删除业务表和索引，保留共享 vector 扩展；alembic_version 表仍存在但没有业务 revision。真实测试执行降级到 base、检查业务表不存在、升级到 head、检查四张表和约束、核对 revision=0001，再执行 alembic check。

自动生成和 alembic check 有检测限制，不能证明每条 CHECK 表达式语义正确；所以额外对非法记录验证具体约束名。迁移降级可能删除数据，仅在独立测试库执行。事务回滚撤销一次业务操作；迁移降级改变结构；备份恢复解决数据恢复，它们是三件不同的事。

当前修订只在本机开发。后续已经共享或部署的迁移应新增修订，不重新改写历史文件。生产迁移账号也应进一步收紧，本日 postgres 是明确的本机开发初始化权限。

## 10. 已执行测试，哪些是模拟

完整 suite 共 196 项，真实数据库/网络检查 43 项，原有真实 Docker 检查 1 项。普通检查明确关闭这两类服务检查，结果为 152 passed、44 skipped；跳过不是通过。完整入口开启两类检查，196 passed、0 skipped。原环境与独立锁定环境均实际执行相同完整 suite，具体结果看 JSON。

实际覆盖：提交后新 Session/新进程可读；重复输入复用；来源冲突保留原值；不同 owner/摄取版本独立；未知与禁用用户不写入；文档和任务回滚；直接绕过业务预查仍受唯一约束保护；并发竞争；跨 owner 复合外键；非法状态、年份、哈希、计数和幂等字段；合法/非法 chunk 定位与重复序号；空库迁移；四种等待控制与连接故障；生命周期；运行账号不能建表或访问迁移版本表；配置脱敏；凭据权限、重入不覆盖、符号链接拒绝；缓存登记与 Git 忽略；旧 API 回归。

提交失败用 SQLAlchemy 事件注入；任务写入失败由测试临时改变任务状态，引发真实 PostgreSQL CHECK。这些故障注入覆盖失败路径，不等于生产网络故障全集。Day 1 的模型测试仍含 HTTPX mock；今天没有真实模型或医学质量评测。pgvector 的合成三维向量自距离为零，只证明扩展运算可用，不证明检索召回率。

## 11. 实际遇到的问题与修正

1. 沙箱下容器启动读取宿主虚拟化信息失败，依赖解析也发生 DNS 失败。必要操作获得执行权限后成功；失败尝试没有计为通过。
2. 首次 prepare 使用了 Path.open 不支持的 opener 参数，返回固定失败且没有输出密码。改为 os.open 独占创建/0600，再使用 fdopen；回归测试验证创建、重复调用保留、权限错误和符号链接拒绝。
3. 审查补充了 SQLAlchemy ArgumentError 的配置错误转换，避免非法 URL 逃出脱敏边界；测试覆盖不是 URL、错误驱动、远端、缺失/非法端口和 query。
4. 候选迁移生成后的 Ruff 导入顺序检查失败。整理导入与模板，再格式化、检查；没有改变旧 Day 1 行为。
5. 授权审查发现通用表授权会包含 alembic_version。改为仅四张业务表，并验证应用账号不能访问迁移版本表。
6. 约束测试审查发现已有任务的唯一约束可能掩盖跨 owner 外键错误。调整测试前置数据，并断言具体约束名。
7. 独立环境普通检查出现 1 个失败：一个标记为 database 的网络测试没有依赖数据库 fixture，所以仅靠 fixture 跳过不生效。给整个模块增加显式 skipif，check.sh 也明确关闭真实检查；重新执行后普通入口通过，真实入口依然执行全部检查。pytest marker 只分类，默认不会自动跳过。

## 12. 完整复现与安全清理

```bash
bash scripts/dev.sh uv sync --locked --python 3.12.14 --dev
bash scripts/dev.sh colima start --cpu 2 --memory 2 --disk 10 --vm-type vz --mount none --ssh-config=false
bash scripts/dev.sh .venv/bin/python -m scripts.day3 prepare
bash scripts/dev.sh .venv/bin/python -m scripts.day3 up
bash scripts/dev.sh .venv/bin/python -m scripts.day3 migrate
bash scripts/dev.sh .venv/bin/python -m scripts.day3 demo
bash scripts/dev.sh .venv/bin/python -m scripts.day3 test
bash scripts/check.sh
bash scripts/dev.sh .venv/bin/python -m scripts.day3 offline-check
bash scripts/dev.sh .venv/bin/python -m scripts.day3 probe
```

独立环境复现：

```bash
bash scripts/dev.sh env UV_PROJECT_ENVIRONMENT=.cache/day3/ci-env uv sync --locked --offline --python .venv/bin/python --dev
bash scripts/dev.sh env UV_PROJECT_ENVIRONMENT=.cache/day3/ci-env bash scripts/check.sh
bash scripts/dev.sh .cache/day3/ci-env/bin/python -m scripts.day3 test
```

离线安装依赖已缓存；新电脑先联网锁定安装。数据库第一次 up 也需要拉取镜像。测试使用管理员连接到受名称保护的 traceagent_test，仅清理这个测试库的业务表；应用库 traceagent 保留演示数据。

唯一库存文件是 `.cache/manifest.json`，由 wrapper 原子生成。扩展登记 Day 3 凭据、日志/报告、独立环境、数据库卷与秘密文件的保留规则；清单不包含密码值。清单、整个 .cache 和明确的 Day 3 路径都写入 gitignore，已实际运行 git check-ignore。凭据文件 0600、缓存根目录 0700；提交候选文件和交付文档检查中未发现本次真实密码值。这是针对本次凭据的检查，不是完整安全审计。

停止：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day3 down
bash scripts/dev.sh colima stop
```

down 保留数据库卷，不使用 -v。保留卷时也保留 database.env，否则重新生成密码不会自动改变旧数据库里的密码。`.cache` 包括 VM 磁盘和本地模型，不能把整个目录当作无损可删的临时目录。先停止服务，按清单选择清理对象；测试报告和独立虚拟环境可以再生成，数据库卷与匹配凭据需要保留或先备份。长期交付证据在 docs；操作系统临时 IPC 由运行时管理，不宣称所有宿主文件均在项目缓存下。

## 13. 官方实现方式核查与阶段限制

核查日期：2026-10-12。官方 SQLAlchemy 将 2.1.4 列为当前稳定系列，2.0.54 为维护系列；Alembic 1.20.0 已发布并要求 SQLAlchemy >=2.0。新项目没有数据库旧代码，连接、迁移、约束和完整测试的最小实验通过后采用 2.1.4。依据是官方维护状态和实测兼容性，不声称新系列已拥有与多年旧系列相同的采用历史。

PostgreSQL 官方支持 18 系列，pgvector 发布 PG18 镜像和 0.8.7 修复。保留一库与精确检索方向，推迟 HNSW/IVFFlat、独立向量数据库和异步迁移。FastAPI 官方示例使用 SQLModel，但本项目需要明确业务/持久化分层，直接 SQLAlchemy 已满足；换包装层会增加本日验证和学习成本，没有找到足以支持切换的具体收益。现有 API/Pydantic/HTTPX 版本保留。

- [SQLAlchemy 发布与支持状态](https://www.sqlalchemy.org/download.html)
- [SQLAlchemy Session、事务与并发](https://docs.sqlalchemy.org/en/21/orm/session_basics.html)
- [SQLAlchemy psycopg 方言](https://docs.sqlalchemy.org/en/21/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg)
- [Alembic 发布记录](https://alembic.sqlalchemy.org/en/latest/changelog.html) 与 [自动生成限制](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
- [psycopg 稳定版本](https://pypi.org/project/psycopg/) 与 [安装](https://www.psycopg.org/psycopg3/docs/basic/install.html)
- [PostgreSQL 版本支持](https://www.postgresql.org/support/versioning/) 与 [等待和事务时限](https://www.postgresql.org/docs/18/runtime-config-client.html)
- [pgvector 项目、精确检索与 Docker 镜像](https://github.com/pgvector/pgvector)
- [Docker Compose 就绪检查](https://docs.docker.com/compose/how-tos/startup-order/)
- [FastAPI 数据库方案](https://fastapi.tiangolo.com/tutorial/sql-databases/)

当前交付是数据库基础，后续身份、文件上传/保存、真实摄取、Embedding、Redis/worker、完整 ready、研究幂等规范化、状态转移和医学引用/拒答仍需各阶段实现。没有患者数据、医疗建议或真实研究输出。工程测试不能证明真实来源可信、定位准确或医学回答安全。没有推送 GitHub，所以远端 Ubuntu Actions 没有执行；独立 macOS 环境验证不能代替它。

## 14. 如果你来开发，学习与实习准备怎么落实

按 config → session → models → repositories → service → migration → tests → CLI 的顺序阅读。先用自己的话描述输入/输出，再逐条指出失败如何结束，最后亲自运行演示并用新连接核对数据库。

今天的 Python/backend 重点是 SQL 查询、关系约束、对象与数据库记录的区别、Session 生命周期、事务原子性、并发竞争和安全错误。对 LLM 应用，文档与 chunk 的持久来源为未来引用验证提供基础；对 AI agent，持久任务状态为 worker 与失败恢复提供基础。这是应用工程准备，没有引入模型训练或算法研究任务。

AI Coding 重点是需求分析（如何证明持久/原子/隔离）、拆任务和 AI 协作（每次一个小模块）、代码审查（授权和事务归属）、调试（约束原因和测试开关）、测试设计（独立预期和故障注入）、可复现交付（空库与锁定环境）。它们是准备优先级，不保证某家公司一定这样考。

AI 可以起草配置、模型、迁移候选和测试。你必须独立确定约束、比较 API 契约、审查自动生成迁移、辨别 Mock 与真实验证、实际运行命令，并解释异常与提交顺序。不要因为测试多就接受质量结论：今天已经有“测试失败于另一条约束”的例子。

五项审查结果：正确性关注去重、关联、NULL 和状态；错误处理关注回滚、提交失败、固定错误与时限；测试关注真实约束、并发、空库和明确开关；安全关注 owner、授权、秘密和日志；可维护性关注小分层、少量依赖、迁移与共同入口。

实务面试追问可以这样准备：两个请求同时上传同一文件怎么办？任务写入或 commit 失败还能返回成功吗？为什么不能共享 Session？如何证明查询是数据库里的结果而非 ORM 的内存对象？应用能连数据库为什么仍可能不 ready？这些问题都能用本次代码与结果回答。

1. 谁拥有这次事务，为什么仓库 add 不能各自 commit，flush 又证明了什么？
2. 两个相同请求并发创建，以及数据库在提交时失败，各自如何结束？
3. 哪个真实测试证明没有半成功数据，哪些注入或 Mock 不能证明生产故障与医学质量？
