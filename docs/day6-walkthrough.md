# Day 6 完成记录与开发讲解

日期：2026-10-15，Asia/Kuala_Lumpur。今天完成真实队列驱动的后台解析、持久阶段状态、投递恢复、执行截止、进程清理及就绪检查。实际结果集中在 `docs/day6-results.json`；工程检查通过不等于你已独立掌握设计。

## 1. 交付与范围

输入是已认证用户的公开/合成 TXT/PDF 上传。队列消息只有规范 execution_id。输出是可追溯 chunks、文档 PARSED 和解析阶段 SUCCEEDED；失败保存安全错误码。解析规格仍为 ingest-v1：pypdf 6.20.1、1000 字符片段、100 字符重叠、PDF 不跨页、TXT 保留原解码字符串定位。

真实链路：HTTP 上传 → 文件和数据库登记 → RQ/Redis → 独立 worker → 独立执行进程 → Day 5 解析子进程 → PostgreSQL 原子保存结果。

没有 Embedding、检索、研究工作流、医学回答、公开 retry、模型调用或公开部署。验证阶段未提交或推送 Git；后续版本交付以 Git 历史为准。保留 Day 5 未提交改动和历史应用数据；`sources/` 不作修改，当前工作目录中没有该目录。

## 2. 三种状态

| 对象 | 责任 | 本日成功结果 |
| --- | --- | --- |
| documents | 资料处理程度 | PARSED，index_version=null |
| parse_tasks | 预解析阶段的投递和执行 | SUCCEEDED |
| jobs(operation=ingest) | 包括未来索引的完整摄取 | PENDING |

阶段状态为 PENDING → QUEUED → RUNNING → SUCCEEDED/FAILED。worker 可能在投递方写 QUEUED 前领取，因此允许 PENDING → RUNNING。投递确认只能条件更新仍为 PENDING/QUEUED 的同一执行，不能覆盖 RUNNING 或终态。

解析成功不等于完整摄取成功，RQ finished 也不等于 READY。RQ 函数可以正常返回业务 FAILED，运输层 finished 与数据库失败并不矛盾。

迁移 0003 新增 parse_tasks。新真实上传的文档、父任务和阶段在一个 SQL 事务中登记；历史记录不回填、不删除。阶段不额外占用户五任务配额。Day 5 本地解析可处理尚未投递的 PENDING 阶段，并一起完成阶段记录；不能绕过 QUEUED/RUNNING 阶段。

本日预解析阶段上限默认 300 秒，解析默认 60 秒、最多 120 秒。完整 ingest 尚未开始，父任务 attempt_count 仍为 0。完整摄取 300 秒总预算、跨阶段累计和重试预算仍须在索引接入时验证，不能给每个阶段重新分配完整总预算。

## 3. 文件与职责

| 文件 | 内容 |
| --- | --- |
| app/db/models.py、migrations/versions/0003_parse_tasks.py | 阶段字段、状态/时间约束、迁移 |
| app/db/repositories.py | owner 范围查询、用户及阶段锁 |
| app/services/documents.py | 真实登记同时创建阶段 |
| app/tasks/config.py、queue.py | 认证本机 broker、JSON 投递、确定 ID、去重、超时 |
| app/services/tasks.py | 原子领取、截止、投递和核对 |
| app/services/ingestion.py | 执行标识检查及片段/阶段原子保存 |
| app/tasks/worker.py、horse.py | 安全的独立进程入口、函数白名单、日志 |
| app/core/processes.py、app/ingestion/worker.py | 父进程消失后的清理 |
| app/services/readiness.py、app/main.py | 有界就绪探测及资源生命周期 |
| app/api/documents.py | 上传后投递与安全队列错误 |
| compose.day6.yaml、scripts/day6.py | 本机服务、演示、故障检查和复现 |
| tests/test_day6*.py | 快速、真实数据库、真实跨进程队列测试 |

## 4. 事务和投递恢复

上传沿用 Day 4 的认证、大小/格式检查、原子文件发布和 SHA-256 核验。文件先保存，文档/父任务/阶段一起提交；SQL 事务结束后才访问 Redis。JSON 不放文件正文、路径、owner、Key、数据库地址或客户端指定的函数。

明确投递失败返回 503 queue_unavailable，顶层附已提交 job_id，文件和 PENDING 阶段保留。parse_tasks 承担最小的持久投递意图；恢复由显式 reconcile 完成，今天没有后台定时调度器。

| 故障窗口 | 处理 |
| --- | --- |
| 数据库明确回滚 | 沿用候选文件清理，不保留假登记 |
| COMMIT 成功但确认丢失 | 保留文件；同请求重放和只读 audit 核对 |
| 数据库提交后、投递前进程退出 | 从持久 PENDING 阶段恢复相同 execution_id |
| Redis 接受后响应丢失 | 确定 ID、入队去重及数据库幂等配合恢复 |
| worker 先完成、投递方后确认 | 条件更新防止终态倒退 |
| 测试 broker 数据全部丢失 | 从 PostgreSQL PENDING/QUEUED 重建相同 ID 消息 |
| worker 执行中退出 | 保留 RUNNING/deadline，截止后核对为安全失败 |
| 保存结果时数据库故障 | 不谎报失败已保存；核对提交结果或等待截止终止 |

已有 failed/stopped/canceled/finished 运输记录、但数据库阶段尚未开始时，核对不会盲目重跑，而会保存阶段失败。Redis 去重随记录删除/过期而失效，因此不能代替数据库执行权与片段唯一约束。

## 5. 并发、幂等与停用用户

锁顺序为用户 → 文档 → 阶段。一个短事务验证身份状态、父任务、版本、执行标识、阶段及排队时间，然后保存 RUNNING、started_at、deadline_at。重复消息遇到执行中或终态返回 ignored，不延长截止时间。

释放事务后才验证文件并解析。保存前再次核验用户、执行标识、RUNNING、截止、文件引用和元数据。chunks、文档状态和阶段终态一次提交；旧 token 或过期结果不能写入片段。长解析和 Redis 等待均不占 SQL 事务。

用户排队后停用：worker 原子记录安全失败，不读取文件。执行中停用：保存时拒绝，已领取的内部执行仍可记录安全失败。公开接口继续拒绝停用用户。

幂等表示重复处理后的持久结果稳定，不代表任何故障下计算恰好执行一次。领取确认丢失等情况可以安全失败，不用重复保存掩盖故障。

## 6. 截止、重试和进程

| 项目 | 实现值 |
| --- | --- |
| producer Redis 连接/命令等待 | 各 0.25 秒，显式零自动重试 |
| worker Redis 连接/socket | 0.25/7 秒，后者覆盖约 5 秒的阻塞取队列 |
| 阶段排队 | 从 created_at 算，最多 1800 秒，可收紧 |
| 阶段执行 | 从领取算，最多 300 秒，可收紧至 2 秒 |
| 解析子进程 | 默认 60 秒、最多 120 秒 |
| worker 心跳 | 空闲约每 5 秒，执行中约每 2 秒 |
| 心跳新鲜窗口 | 10 秒，强杀后不保证瞬间离线 |
| /ready 单项/整体 | 单项 0.9 秒，整体 2.8 秒，Redis 清理另限 0.1 秒 |

producer 的 socket 超时不是整条多命令路径的统一硬截止。/ready 有单项和整体截止，并测试过真实 TCP 接受连接但不回应 PostgreSQL 握手的情况。

不启用 RQ Retry、scheduler、cron 或失败阶段自动重跑。投递恢复使用同一 execution_id，不新增尝试、不重置 created_at；真正执行后失败的阶段不会被恢复命令重新领取。公开 research retry 仍未实现。

检查已安装 RQ 源码发现默认 SpawnWorker 把含密码的连接参数拼进 python -c。SafeSpawnWorker 改用固定模块，argv 只有必要 ID；配置经受保护环境传递，种子 API Key 和迁移凭据不继承，文件/页数/字符收紧上限明确继承。

horse 检查函数只能为固定 execute_parse、一个参数、无 kwargs、目标队列一致。JSON 避免 pickle，函数白名单约束执行入口，Redis 认证仍然必需。每个执行进程创建自己的 DB 连接池，不跨进程共享 Session/连接。

horse 使用独立进程组，包含解析子进程。父 worker 消失后，horse 的监视线程终止进程组；解析子进程也监视自己的父进程。正常异常和超时继续 kill 并回收子进程。真实测试使用 SIGKILL/SIGSTOP 验证这些行为。

RQ 日志采用固定安全格式，运输失败保存 task_execution_failed，不保留原始参数或异常栈。调试应结合执行 ID、阶段状态和安全报告；固定格式日志本身不提供完整异常细节。

## 7. /ready 与安全边界

/health 保持原有存活检查。/ready 并发检查 PostgreSQL SELECT 1、Redis PING、目标队列真实心跳，不自动重试。worker 须为 idle/busy、无 death 标记、声明本日 JSON 协议，并匹配数据库与上传根目录作用域摘要。其他队列或另一份数据库/目录的 worker 不算就绪。

数据库探测使用已有 psycopg 可取消异步连接，未重写业务数据库层。旧连接池探测的超时不能满足单项 1 秒，因此不直接复用。API 自建池和队列客户端在关闭时释放，启动配置失败也清理已创建资源。

这是带新鲜度窗口的依赖检查，不证明每份文件完整、所有执行配置一致或恶意文件隔离。API 与 worker 应使用相同处理配置。未启用队列时 /ready 为 503，/health 可为 200。

Redis URL 只允许带认证的 127.0.0.1、显式端口、DB 0/1，拒绝 query 覆盖超时和任意主机。应用/测试 broker 分别 DB 0/1，队列也分开；测试清空前显式断言 DB 1。服务只绑定本机，固定镜像摘要、认证、AOF 和数据卷。公开错误固定，不返回 SQL、密码、堆栈或源正文；文档 error 仍为 str|null。

## 8. 测试证据的范围

快速测试覆盖配置、消息、去重、日志、启动参数、心跳边界、取消和缓存管理。真实数据库测试覆盖登记/保存原子性、竞争、迟到结果、截止、停用用户和本地命令隔离。跨进程测试使用真实 Redis、PostgreSQL、文件和 RQ worker。

实际故障包括：入队成功后人为丢弃确认、丢失测试 broker 数据后恢复、连接拒绝、数据库握手不回应、领取进程退出、worker SIGKILL、暂停解析后触发任务超时、心跳过期和未允许函数拒绝执行。推进数据库时间和部分服务异常是明确注入，不冒充真实等待 30 分钟或网络断电。

HTTP 演示覆盖中文 TXT、文本 PDF、损坏 PDF、重复上传、owner 隔离、阶段成功但父任务未完成、worker 启停。offline-check 实际停止并恢复 Redis/PostgreSQL，核对 503、保留任务、同 ID 恢复、health 200 和 ready 恢复。具体次数和耗时见结果文件。

没有模型调用、检索效果对比和医学答案评测。保留 Starlette 对 httpx TestClient 的弃用警告，没有为本日无关目标增加另一个 HTTP 库。

## 9. 本机复现与展示

在项目根目录，有现成 .venv 时：

```bash
bash scripts/dev.sh uv sync --locked --python .venv/bin/python --dev
bash scripts/dev.sh .venv/bin/python -m scripts.day3 prepare
bash scripts/dev.sh .venv/bin/python -m scripts.day4 prepare
bash scripts/dev.sh .venv/bin/python -m scripts.day6 prepare
bash scripts/dev.sh colima start --cpu 2 --memory 2 --disk 10 --vm-type vz --mount "$PWD/.cache/day4/uploads:w" --ssh-config=false
bash scripts/dev.sh .venv/bin/python -m scripts.day3 up
bash scripts/dev.sh .venv/bin/python -m scripts.day3 migrate
bash scripts/dev.sh .venv/bin/python -m scripts.day6 up
bash scripts/dev.sh .venv/bin/python -m scripts.day6 test
bash scripts/dev.sh .venv/bin/python -m scripts.day6 demo
bash scripts/dev.sh .venv/bin/python -m scripts.day6 offline-check
bash scripts/dev.sh .venv/bin/python -m scripts.day6 audit
bash scripts/check.sh
```

首次没有 .venv 时使用 `uv sync --locked --python 3.12.14 --dev`，允许下载解释器。已有解释器的离线隔离复现：

```bash
bash scripts/dev.sh env UV_PROJECT_ENVIRONMENT=.cache/day6/ci-env uv sync --locked --offline --python .venv/bin/python --dev
bash scripts/dev.sh .cache/day6/ci-env/bin/python -m scripts.day6 test --report-label isolated
```

测试、demo、offline-check 共用 traceagent_test，必须串行。测试会清理隔离表，demo 文件不算应用长期资料。offline-check 会短暂停止本项目依赖，不能与使用这些依赖的其他工作同时运行。

先运行 demo 为测试库准备用户，再在两个终端分别运行：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day6 worker --isolated-test
bash scripts/dev.sh .venv/bin/python -m scripts.day6 serve --isolated-test
```

打开本机 /docs，Key 只从受保护文件读取，不贴到聊天/日志。查看阶段：`day6 inspect <job_id> --isolated-test --owner A`；核对恢复：`day6 reconcile --isolated-test --owner A`，均通过相同包装器执行。省略 --isolated-test 使用应用库及原上传目录；历史用户五任务上限不变。

自动 demo 发现 8000 被占用会拒绝接管，结束后关闭临时 API/worker。停止依赖用 day6 down、day3 down 和 colima stop，均保留卷。

## 10. 统一缓存管理

唯一清单仍为 `.cache/manifest.json`，由 scripts/dev.sh 原子生成，显式写入 .gitignore。Day 6 新文件全部在 `.cache/day6/` 并显式忽略：queue.env、日志、测试/演示/停机报告、隔离环境和测试上传。字节码、pytest、uv、容器 VM 沿用既有清单路径。

queue.env 独占、拒绝 symlink、0600 创建，prepare 不轮换已有密码；保留 Redis 卷时保留凭据。测试上传可随测试库重建；应用 `.cache/day4/uploads/`、数据库凭据、API Key 和数据库卷必须一起保留。不要把整个 .cache 当作可随意删除的目录。容器临时配置在 tmpfs，随容器退出释放。清单不写秘密值；docs 中的说明和结果是交付证据，保留。

## 11. 方案检查：2026-10-15

保留 Python 3.12.14、FastAPI 0.141.1、SQLAlchemy 2.1.4、psycopg 3.3.6、pypdf 6.20.1。新增 rq==2.12.0、redis==8.1.0，分别用于后台执行和 Redis 客户端；RQ 传递依赖 croniter/python-dateutil/six，没有启用调度器。实际 Redis 8.2.10，镜像摘要固定。

- [RQ 发布](https://pypi.org/project/rq/)与 [redis-py 发布](https://pypi.org/project/redis/)：采用正式版本，锁定并以真实跨进程测试、隔离安装补证兼容性。
- [RQ worker](https://python-rq.org/docs/workers/)：采用 JSON 和 spawn；检查安装源码后替换含凭据的启动拼接。后续升级必须重跑进程、错误和运输回归。
- [RQ 去重](https://python-rq.org/docs/#unique-jobs)：使用已发布 2.8 起的能力，保留数据库幂等；不依赖新主版本文档中的其他功能。
- [Redis 重试和超时](https://redis.io/docs/latest/develop/clients/redis-py/produsage/)：覆盖默认行为，避免隐式重试拉长失败等待。
- [Psycopg 并发/取消](https://www.psycopg.org/psycopg3/docs/advanced/async.html)：采用已有异步能力探测，不跨进程共享连接，不迁移业务数据库架构。
- [FastAPI BackgroundTasks](https://fastapi.tiangolo.com/tutorial/background-tasks/#caveat)：简单，但不满足本日独立持久执行与恢复目标，暂不采用。
- [Procrastinate 事务入队](https://procrastinate.readthedocs.io/en/stable/howto/production/external_connection.html)：有减少双写窗口的潜力，但引入其他任务表与运行方式；未验证迁移收益，延后。

没有足以抵消兼容、迁移和排期成本的整体替换收益。本日没有吞吐排名或性能基准；无模型费用，新增延迟来自 Redis 和进程启动。质量依据为状态正确、失败可见和恢复可复现。

## 12. 你独立开发时要注意什么

先写输入、输出、状态和成功/边界/失败，再实现。先让短合成 TXT 跨进程执行；再加持久领取和幂等；最后故意断投递确认、停服务、杀进程。每次只引入一个新的故障边界。

1. 需求分析：上传、入队、解析保存、可检索分别承诺什么，接口说明必须准确。
2. 拆分和 AI 协作：一次交给 AI 一个有验收的小步骤，例如重复领取不能延长 deadline；审查后再接下一步。
3. 并发：前置查重、唯一约束、原子领取和最终 token 检查分别解决不同竞争，不能互相替代。
4. 事务：解析/网络等待不占事务；COMMIT 异常可能是确认丢失，不能据此删除已引用文件。
5. 超时：取消等待不等于停止计算；连接、命令、子进程和阶段截止都要分别验证。
6. 安全：owner 来源、固定入口、JSON、参数/日志/错误脱敏和依赖默认重试都需要亲自检查。
7. 测试：先写预期再运行，分清替身、真实依赖、故障注入与真实模型评测，skipped 不算通过。
8. 交付：锁文件、迁移、启停、测试库保护、缓存清单和风险都属于实现。

AI 可帮助骨架、参数化测试、官方资料检索和审查；你必须独立验证查询、锁、事务、超时及真实证据，并解释未覆盖内容。今天准备的是后端任务执行、RAG 摄取工程，以及 AI Coding 的需求拆分、调试、审查、测试与复现交付，不是算法研究或对具体公司试题的保证。

实际开发中发现并修正的问题也值得复盘：标准 `redis://:password@host` 的 username 是空字符串，校验不能只接受 None；JSON 会把 tuple 还原为 list，测试需匹配真实协议；启动配置失败也要关闭已创建数据库资源；执行进程的环境白名单要保留可收紧的解析上限；停用用户的后台失败记录不能再次被公开认证规则挡住。每项都有针对性回归。包含运行环境的测试夹具使用安全 repr，早期失败日志也做了脱敏；不能因为日志被忽略就允许它保存秘密。

离线隔离安装最初无法按版本发现已安装解释器，随后显式使用已验证的 .venv/bin/python，离线创建独立环境成功；未把首次失败当成通过。详细命令见上文。

## 13. 五项审查与剩余边界

正确性：阶段与父任务分开、条件更新、防迟到写入、片段原子提交。错误处理：保留投递意图、保存失败不伪造终态、截止核对、停用身份拒绝。测试：成功、边界、失败、并发、迁移、跨进程及停机恢复有实际报告。安全：认证、owner 查询、JSON/白名单、参数与日志保护、本机服务和测试库隔离。可维护性：复用原解析器和分层，只增两个直接依赖，记录 RQ 适配维护成本。

剩余边界：reconcile 由操作人员显式运行，没有自动周期核对；阶段失败没有手动重试；完整 ingest/Embedding/RAG 和模型累计预算尚未实现；父任务等索引仍占五任务额度。macOS 硬内存上限仍未启用，spawn/watchdog 不构成生产恶意文件沙箱。没有多机时钟/网络分区保证、备份恢复演练、全局资源配额或公开部署验收。工程通过不代表医学质量或生产发布通过。

请亲自回答：

1. 数据库提交但 Redis 响应丢失时，为什么不能删文件或直接生成新任务？
2. worker 被杀后，计算进程、心跳和数据库 RUNNING 状态分别怎样处理？
3. 哪些测试证明幂等与真实执行，哪些不能证明完整 RAG 或医学答案可靠？
