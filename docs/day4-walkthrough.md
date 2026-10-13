# Day 4 完成记录与开发讲解

日期：2026-10-13，Asia/Kuala_Lumpur。范围依据原 D4「身份隔离与文件上传」和 API 契约：两个种子用户、API Key 与 owner 校验、文档上传和状态查询、共享文件、大小/类型限制、哈希去重和越权测试。

本文解释最终实现、验证证据、失败边界和你独立开发时的注意事项。机器可读证据在 `docs/day4-results.json`。工程任务完成不等于开发者已经掌握；最后的个人演示与自查需要你亲自完成。本次不提交 Git、不推送 GitHub、不公开部署。

## 1. 从已有工程推进，而非重做 Day 3

开始时仓库已有 Day 3 的未提交变更，包括数据库、迁移、配置、脚本、文档和测试；AGENTS.md 也已有修改。先读取 Git 状态、文件和契约，保留这些工作与历史结果。`sources/` 当前不存在，没有创建或修改同步来源目录。没有改动 Day 1/2 的独立模型实验。

原 API 只有 health，内部 DocumentService 已能把文档和任务原子登记，并按 owner 查询。D4 在这条链路上增加真实 HTTP 身份和文件边界，没有创建第二套 ORM、队列或医学专用平台规则。未新增或升级依赖；使用已锁定的 FastAPI、Starlette、AnyIO、python-multipart、SQLAlchemy、psycopg、Alembic、Pydantic 和测试工具。

## 2. 交付物与文件职责

| 文件 | 最终职责 |
|---|---|
| app/main.py | 应用工厂、数据库生命周期、挂载业务路由及中间件，保留 health |
| app/api/documents.py | 身份依赖、multipart 边界、请求转换、响应模型、安全错误映射 |
| app/api/upload_limits.py | Starlette 原始请求体大小限制、413 JSON 适配、意外业务错误保护 |
| app/services/auth.py | Key 格式、SHA-256 摘要、数据库认证、启用状态、可信 Principal |
| app/storage/files.py | 字节/字符限制、编码与格式初筛、独占文件保存、核对和候选清理 |
| app/services/uploads.py | 串联文件保存与数据库登记，明确补偿规则 |
| app/services/documents.py | 文档/任务/文件引用同事务保存、去重、冲突、查询、任务上限 |
| app/db/models.py、repositories.py | 字段、数据库约束、带 owner 的查询和用户行锁 |
| migrations/versions/0002_auth_and_files.py | 从 0001 增量升级、回滚；不伪造历史文件 |
| scripts/day4.py | prepare、seed、serve、demo、test、shared-check、audit、offline-check、read |
| tests/test_day4.py | 快速测试，真实临时文件；数据库替身明确标注 |
| tests/test_day4_database.py | 真实 PostgreSQL 与 API 测试、并发、迁移、提交确认丢失注入 |
| scripts/dev.sh、.gitignore | 唯一缓存清单、缓存路径、凭据与上传数据的保留规则 |
| .env.example、README、api-contract-v1.md | 空值配置模板、复现步骤、已实现与未来行为、补充错误契约 |

Day 3 测试中的当前迁移版本随 head 更新到 0002；原 0001 迁移和历史 Day 3 验证 JSON 保留。

## 3. 输入、输出与完整调用顺序

输入是一个 multipart 文件，加 source_id、title、source_url、license、license_evidence_url；publication_year、version_label、research_dates 可选。身份来自一个 X-API-Key 请求头，owner、文件哈希、摄取版本和存储路径由服务端决定，外部表单中的这些字段拒绝。来源 URL 只保存，不发起网络抓取。

上传输出为 202，只有 document_id、job_id、document_status、job_status。初次登记为 UPLOADED/PENDING。状态查询输出 document_id、status、index_version、error，不包含 Key、owner、文件路径、原文或 ORM 对象。

调用链是：请求体外层大小保护 → Key 验证得到 Principal → 有时限的 multipart 解析 → 元数据校验 → 在线程中分块保存文件并算哈希 → 使用独立 Session 登记文档/任务/文件引用 → 提交成功后返回 → 查询时带 owner 过滤并核对文件。

已知过大的 Content-Length 可被外层提前返回 413，因此它可能优先于身份错误。普通无效身份不进入 multipart 解析。所有成功状态仅证明本阶段行为，不能由 UPLOADED 推导出已经切分、建立索引或生成研究答案。

## 4. 身份与权限：两次边界都不能省

prepare 使用 secrets.token_urlsafe(32) 生成两个独立、43 字符 URL-safe Key；只在凭据文件不存在时生成。文件为 0600，目录受保护；原始 Key 不进入参数、聊天输出或验证 JSON。seed 将 SHA-256 摘要写入用户表，两个用户沿用固定种子 UUID。

数据库摘要有唯一与格式约束。历史用户的摘要允许 null：它们不会因此自动获得登录凭据。重复 seed 不改变 ID 或 Key；不同摘要冲突会回滚，不静默轮换 Key；停用用户不会被 seed 重新启用。需要换 Key 时应有单独、明确的轮换操作，不通过删凭据文件来实现。

APIKeyHeader 只提取头并提供 OpenAPI 声明。AuthService 进一步校验格式、按摘要查找用户、用 secrets.compare_digest 比较摘要并检查 active。恒定时间比较是摘要比较步骤的属性，不表示数据库查询和整个认证过程具有恒定耗时。这些 Key 由高熵随机源生成，不能把这里的普通 SHA-256 方案照搬成用户密码存储方案。[Python secrets 官方说明](https://docs.python.org/3.12/library/secrets.html)、[FastAPI 安全工具](https://fastapi.tiangolo.com/reference/security/)

查询仍要执行 document_id AND owner_id。认证确定调用者身份，授权限制资料范围；B 的 Key 有效，不代表 B 能读 A 的文档。跨用户与不存在记录使用相同 404；记录存在但数据库/文件故障返回服务错误。任务登记前再次检查用户启用状态，避免只相信早先 HTTP 验证。

## 5. multipart、大小、字符与时限

上传采用 Request 加显式解析，不直接把 UploadFile/Form 声明放在函数参数中。检查已安装 FastAPI 的路由代码后确认，自动表单路径可能先读请求体，再解依赖；显式解析使本路由的顺序可验证。OpenAPI 的 multipart 描述手工补齐，所以本机 /docs 仍可上传文件。

Starlette 1.6.0 的 RequestBodyLimitMiddleware 统计原始接收字节，包括 multipart 开销；它不能替代单文件大小限制。外层上限是 max_file_bytes + 64 KiB，内部仍累计文件实际字节，最多 5 MiB。缺少或低报 Content-Length 的 ASGI 流也有测试。413 默认可能是普通文本，因此额外适配为固定 JSON 错误，仅作用于上传路由，保留 health 和原框架 404/405。

最多一个文件、八个元数据字段；每个非文件字段最多 8192 字节。单字段的 max_part_size 并不是文件上限。重复或未知字段拒绝。明确解析完成回调，拒绝底层 finalize 未必拒绝的截断表单；畸形表单、字段/文件数量、超大字段采用安全 400，不返回解析器消息。[Starlette 请求限制](https://www.starlette.io/requests/)、[1.6.0 发布记录](https://github.com/Kludex/starlette/releases/tag/1.6.0)

使用 AnyIO 截止时间限制网络读取与表单解析，默认 30 秒，只能收紧。超时返回 408；测试在文件暂存已开始后注入延迟，并确认所有解析器文件句柄关闭。已安装解析器的异常路径会关闭暂存文件，完整表单在 finally 中关闭。

表单值首先是字符串。publication_year 四位十进制字符串转换为 int，省略/空字符串转 None，然后进入严格 Pydantic 模型；不能让 strict=True 的内部模型直接期待表单给出 Python int。其他可选字段可以省略，明确空值仍按字段规则校验。业务函数也重新校验元数据实例，防止 model_copy(update=...) 绕过边界。

磁盘操作和同步数据库操作在线程中执行，一个操作拥有自己的 Session，不把它们直接阻塞在 async 处理函数里。解析截止不等于磁盘/数据库整体业务截止；现有连接、连接池、SQL、锁和空闲事务超时也不能合并声称成一个总截止。没有自动重试业务提交，客户端可通过相同上传恢复去重结果。

## 6. 文件保存：原字节、独占名字和明确耐久性

文件 MIME 仅接受 text/plain、application/pdf，允许 MIME 参数。客户端文件名只作为上传协议信息，不决定保存路径，演示刻意使用 ../../ 开头的名字。服务器产生随机十六进制存储键，正式路径只有固定根目录加该键；存储键校验、私有权限和 O_NOFOLLOW 防止已有符号链接被误用。

每次最多读 64 KiB，累计字节，同时计算 SHA-256。TXT 用增量 UTF-8 解码器，允许 BOM，正确处理跨读取块的中文字符，拒绝非法编码、结尾不完整字符、空白文本和 NUL 等控制字符。字符数最多 200000，可配置收紧；字节上限和字符上限分别校验。

哈希与保存内容使用原始字节，不对文本去 BOM、去空白、归一化后再保存。语义相近但字节不同的文件可能有不同哈希，这是可追溯文件去重的预期；不能把它误称为语义去重。

PDF 只做起始格式标识检查；文本型、页数、加密、损坏、扫描件、提取字符上限在 D5 完整摄取时判断。今天成功上传的真实文本 PDF 由固定合成文本生成；5 MiB 填充 PDF 只用于大小边界，不作为解析成功证据。

先以 O_EXCL 创建 0600 的本请求 .part 文件，写完 flush/fsync。再使用同目录、同文件系统的独占硬链接发布为 .bin，移除 .part 并 fsync 目录。发布不会覆盖已有正式文件。审查中分别增加了临时名碰撞、正式名碰撞和目录 fsync 失败的回归测试：只清理确认由当前调用创建的路径，保留已有文件。这依赖本机文件系统支持硬链接和目录 fsync，已在实际环境验证；换成对象存储时应重新设计，不能照搬。[Python 文件系统接口](https://docs.python.org/3.12/library/os.html)

## 7. 文件与数据库之间没有共同事务

上传文件完成后才开启登记事务；避免慢上传一直占用连接和锁。数据库内文档、任务、文件引用一起 commit 或 rollback。Repository 只执行读写和 flush，不自行 commit；服务退出事务上下文后才返回成功。[SQLAlchemy 2.1 事务说明](https://docs.sqlalchemy.org/en/21/orm/session_transaction.html)

| 失败点 | 已采取的行为 |
|---|---|
| 编码、格式、大小、文件写入或发布失败 | 不进行数据库登记；清理当前调用创建的文件 |
| 元数据冲突、用户失效、配额或已确认完整性拒绝 | 数据库回滚；只清理本请求候选 |
| SQLAlchemy 数据库异常，可能包含提交确认丢失 | 返回安全 503；保留候选，不能假定没有提交 |
| 重复请求成功复用旧记录 | 删除本请求新候选；验证原引用文件 |
| 原文件缺失、损坏、长度变化或符号链接 | 返回 storage_unavailable；不伪装成 404 或静默覆盖 |
| 清理本身失败 | 返回安全存储错误，候选由 audit 列出，不假报清理完成 |

真实数据库测试还做了一个明确的故障注入：先真实 commit，再模拟丢失确认。API 返回 503，但数据库记录和文件都保留；恢复正常后重试得到原文档 ID，并只留下一个文件。这证明补偿逻辑处理该模型，不能把它说成实际网络中已经制造了 COMMIT 确认丢失。

audit 是本机操作命令，用应用账号只读核对文件引用、大小与哈希，列出缺失/损坏、未引用候选和 .part 数量，不自动删除。存在未引用候选不必然代表可立即删除：应先停止上传、确认没有正在提交的操作、恢复数据库并重跑核对。失败核对使用非零退出码，已有回归测试。丢失或损坏的已引用文件需要恢复对应备份或后续明确修复流程。

## 8. 去重、并发与五任务上限

唯一键继续是 owner_id + file_sha256 + ingest_version。同用户、同字节和同摄取版本比较规范化后的全部来源元数据；一致复用，冲突 409，不覆盖旧来源。不同用户拥有独立记录和文件，没有公开跨用户去重结果。

摄取版本是服务端 ingest-v1，不能由外部表单提供。处理流程变化时需要有意识地更新版本和记录迁移策略，不能只为绕过冲突而随意改版本。

对真实上传的登记，在用户行上加锁，串行执行配额计数与创建。同 owner 已有五个未结束任务时拒绝第六个新上传，重复请求先查复用，因此仍能返回已有结果。计数包含 PENDING/QUEUED/RUNNING，未来研究任务也须采用同一边界，不能只在 HTTP 层先数再创建。

数据库唯一约束提供最后防线，保留 D3 同时创建时的冲突协调逻辑。已测试两个相同文件并发后只有一个记录、任务和正式文件，也测试六个不同文件并发时最多五个成功。D3 可信内部合成登记保持兼容，不作为公开上传入口。

## 9. 迁移与旧记录

0002 在 users 增加 api_key_digest，在 documents 增加 storage_key、file_size_bytes、file_media_type。新字段可空以保留旧记录，约束要求文件引用要么三项全空，要么三项完整且合法，存储键唯一。文件大小和类型也有数据库约束，不只靠 Python 判断。

实际执行了应用库升级和 Alembic check；测试库执行空库升级/回滚、0001→0002 保留用户记录、降级再升级与非法字段约束。迁移用原开发迁移账号，应用继续使用无 DDL 权限的账号。

0002 downgrade 会移除认证摘要和文件引用，所以只作为隔离测试或明确迁移操作使用，不应在保留真实上传数据的应用库随意执行。旧 D3 合成记录没有文件，保持数据库原状态，但公开状态接口不会将它当作真实上传；返回安全 storage_unavailable。历史 JSON 是历史证据，没有改写为今天的结果。

## 10. 真实共享文件与本机演示

D3 的 Colima 配置禁用了宿主挂载。D4 在已有 VM 上只添加 .cache/day4/uploads 的必要挂载，保留数据库卷；没有挂载整个项目或 .cache（其中包含凭据）。正式存储键独立于根目录，未来消费者可在 /data/uploads 下定位同一个文件。

实际用固定 digest 的 Alpine 临时容器，在无网络、只读根文件系统、只读 uploads 挂载、去掉能力、禁止提权的设置下读取相同 SHA-256；写入被拒绝，宿主未产生写入文件。它证明当前文件共享可用，真实 worker 尚未实现。[Colima 配置](https://colima.run/docs/configuration/)、[Docker bind mount](https://docs.docker.com/engine/storage/bind-mounts/)

demo 自动启动 loopback API，验证 TXT、PDF、去重、元数据冲突、两用户、无效 Key、未知年份、空文件、错误编码、类型、5 MiB 边界、独立进程读取；然后重启 API 再查原状态。端口占用时拒绝接管，finally 关闭自身进程。日志留在统一缓存目录，不输出 Key。

offline-check 实际停止本项目数据库：业务返回 503 database_unavailable，health 仍为 200，故障响应约 0.004 秒。恢复数据库后重启临时 API，再查原文档成功。此结果只说明本机停机场景，不保证任意网络问题都同样快，也不证明已有连接池会无缝自动恢复。

## 11. 实际测试、证据与审查

最终本地与独立锁定环境的完整测试均为 277 项通过，0 失败、0 错误、0 跳过。普通检查在两套环境均为 215 项通过、62 项明确跳过。完整测试包含 61 项真实数据库及其故障注入检查和一项真实 Docker 检查；D4 新增 81 项，其中快速测试 63 项、数据库测试 18 项。详细证据由 day4-results.json 保存，不能把跳过计成通过。

普通 check 明确关闭真实数据库/容器检查；它负责格式、静态检查、快速回归，不证明外部服务可用。真实数据库测试只使用 traceagent_test，会删除测试数据和降级测试表，测试函数先校验数据库名；应用库数据保留。其他测试使用合成资料，不含患者数据。

源码审查覆盖：正确性（提交后成功、去重、版本、配额）、错误处理（确认丢失与补偿、故障状态）、测试（成功/边界/失败和并发）、安全（可信身份、路径、资源、错误和日志）、可维护性（HTTP/业务/持久化分层、既有代码兼容）。没有 P0/P1 遗留项在本轮验收范围内被发现，不能由此宣称已证明不存在所有缺陷。

一条未隐藏的警告：Starlette TestClient 对现有 HTTPX 测试后端发出弃用提示，测试仍通过。本轮保留锁定环境，不为消除提示额外安装 httpx2；后续更换测试后端时要按当时官方兼容要求验证。真实 HTTP 演示不依赖 TestClient。

## 12. 唯一缓存清单、Git 忽略与数据保留

scripts/dev.sh 在每次开发命令前维护 .cache/manifest.json。清单不保存任何凭据值；.gitignore 同时忽略 .cache/、显式清单路径和 day4 子目录。实际检查了清单、auth.env、uploads 和测试报告的忽略规则。

| 位置 | 内容与处理 |
|---|---|
| .cache/day4/auth.env | 原始两个 API Key，0600；保留数据库摘要时一起保留 |
| .cache/day4/uploads/ | 正式上传文件；保留数据库引用时一起保留 |
| .cache/day4/*.json、*.xml、*.txt、api.log | 本地运行证据、报告和日志；可重跑生成 |
| .cache/day4/ci-env/ | 独立锁定环境；可重建 |
| .cache/pytest/、python/、ruff/、tmp/ | 测试临时目录、字节码、工具缓存；由 wrapper 管理 |
| .cache/day3/database.env 与数据库卷 | 原数据库凭据和持久数据；继续保留 |
| .cache/colima/、lima/、docker/ | VM、容器配置及 VM 内镜像/卷；清单保留位置与生命周期 |
| OS 临时 IPC | Colima/Lima 的系统临时 socket，由服务停机释放，清单说明生命周期 |
| docs/day4-results.json、本文 | 正式交付证据与讲解，不是可删缓存 |

把目录命名为 .cache 不会让里面的数据库、上传文件和原始 Key 自动变成可删数据。不要保留数据库却删除关联文件或重建凭据，不要推送 .cache，不要用删 VM 或删数据库卷来解决挂载问题。备份时要把数据库、上传文件和受保护凭据作为一致的保留集合；今天未实现自动备份或生产恢复方案。

## 13. 复现与个人展示

以下命令在项目根目录执行。先安装锁定环境、准备本机数据、启动并迁移：

```bash
bash scripts/dev.sh uv sync --locked --python 3.12.14 --dev
bash scripts/dev.sh .venv/bin/python -m scripts.day3 prepare
bash scripts/dev.sh .venv/bin/python -m scripts.day4 prepare
bash scripts/dev.sh colima start --cpu 2 --memory 2 --disk 10 --vm-type vz --mount "$PWD/.cache/day4/uploads:w" --ssh-config=false
bash scripts/dev.sh .venv/bin/python -m scripts.day3 up
bash scripts/dev.sh .venv/bin/python -m scripts.day3 migrate
bash scripts/dev.sh .venv/bin/python -m scripts.day4 seed
```

验证和演示：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day4 demo
bash scripts/dev.sh .venv/bin/python -m scripts.day4 shared-check
bash scripts/dev.sh .venv/bin/python -m scripts.day4 audit
bash scripts/dev.sh .venv/bin/python -m scripts.day4 test
bash scripts/check.sh
```

独立环境复现：

```bash
bash scripts/dev.sh env UV_PROJECT_ENVIRONMENT=.cache/day4/ci-env uv sync --locked --offline --python .venv/bin/python --dev
bash scripts/dev.sh env UV_PROJECT_ENVIRONMENT=.cache/day4/ci-env bash scripts/check.sh
bash scripts/dev.sh .cache/day4/ci-env/bin/python -m scripts.day4 test --report-label isolated
```

offline 安装需要已经存在锁定包缓存；缺失时明确去掉 offline 重新下载，不能把安装失败当成验证成功。真实测试不要同时运行两份，二者共享隔离测试库。

手动展示：`bash scripts/dev.sh .venv/bin/python -m scripts.day4 serve`，打开 http://127.0.0.1:8000/docs 。通过本机受保护的 auth.env 使用各自 Key，先 A 上传并查询，再 B 查 A 的 ID。不要把 Key 粘贴到聊天、截图、历史命令参数、演示文档或日志。prepare/seed 不会打印 Key。资料使用公开文档或合成文本，保留来源和许可，不用真实患者资料。

停机演练和清理服务：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day4 offline-check
bash scripts/dev.sh .venv/bin/python -m scripts.day3 down
bash scripts/dev.sh colima stop
```

先关闭手动 serve 的终端，再停止数据库/VM。down 保留卷，不加删除卷选项。本次自动演示 API 已关闭，最终服务状态以实际验证 JSON 为准。

## 14. 当前方案检查与剩余边界

检查日期为 2026-10-13；版本见实际结果。保留 FastAPI 0.141.1、Starlette 1.6.0、python-multipart 0.0.32 和同步 SQLAlchemy 2.1.4。Starlette 内置请求体限制有正式发布与本机实现证据，已通过有/无长度头、错误 JSON 和资源清理验证后采用，没有另引入上传框架。完整请求使用显式解析，业务阻塞操作使用框架已有线程执行能力。

较新 FastAPI 0.143.0 主要涉及遥测配置；Starlette 1.7.0 包含 multipart 错误处理修复与明确标为实验性的遥测接口。本轮畸形输入已在锁定版本下验证安全失败，没有证据需要为上传接口迁移整个栈；决定保留当前版本、延期遥测和测试后端迁移。没有用新版本号、营销或未测性能作为替换理由。[FastAPI 发布说明](https://fastapi.tiangolo.com/release-notes/)、[Starlette 1.7.0 发布说明](https://github.com/Kludex/starlette/releases/tag/1.7.0)

D4 仍是本机、两个种子用户的演示边界。尚无公开部署、完整用户登录/Key 轮换、全局连接/上传并发限流、磁盘总配额、自动孤儿清理、备份、生产恶意 PDF 解析隔离和真实 worker。单次 5 MiB 与每用户五任务不是全局资源控制。新数据库 URL 目前仍限本机，D6 引入容器 worker 时要明确检验受控服务地址和挂载配置。

PDF 完整解析/切分/定位、Embedding/RAG、真实模型与医学引用/拒答、队列投递、worker heartbeat、完整 ready、状态转移和研究重试仍按后续阶段验收。没有真实模型调用、医学质量结论或远端 GitHub Actions 结果。保留当前缺失文件为安全失败是有意设计，后续恢复不能无声替换证据版本。

## 15. 如果你独立开发，应该怎样推进

先画输入、输出、成功和失败表，指出哪些值由客户端提供、哪些必须来自可信服务端。第一步亲自完成一个 A 上传 TXT、查自己状态的小链路；第二步加入 B 的越权验证；第三步实现去重和持久化失败；最后补充 multipart、并发与文件故障。每个小步骤都运行对应检查，再推进下一层。

AI 可以帮助查官方资料、起草迁移和接口、整理测试参数、审查异常路径。你必须独立检查：数据库约束是否真的失败、Session/commit 边界、SQL 的 owner 条件、每个清理路径是否只属于当前请求、Key 是否泄露、mock 证明范围、PDF 上传与摄取的差别。不要让 AI 为了让测试变绿把真实数据库换成内存字典、放宽限制或静默更改接口。

面试准备优先是 Python/backend 的输入转换、文件 IO、HTTP 错误、身份、事务和并发；LLM 应用侧是原始资料、处理版本、来源元数据与后续引用追溯；agent 侧是可信身份必须传到工具和数据访问边界。AI Coding 准备包括需求拆解、给 AI 明确边界、审查改动、根据失败证据调试、设计不同层级测试、提交可复现结果。这些是准备优先级，不是对某家公司题目的保证。

实际演示时，可以回答这些追问：为什么 API Key 摘要适用于这里的高熵随机 Key？为什么参数上的 Depends 不能自动证明上传前认证？为什么两个请求的前置查重不足以替代唯一约束？为什么数据库提交确认丢失时不能删文件？为什么 status 的 200 不表示摄取成功？为什么临时容器可读不等于真实 worker 已验收？

请你亲自完成三项短自查：

1. 从 Key 到数据库查询，owner 在哪两处被确定/校验，为什么模型或请求体不能改变它？
2. 文档 commit 已成功但客户端收到 503 时，文件如何保留，相同上传如何恢复原结果？
3. 哪些结论由真实 PostgreSQL、真实 HTTP、真实共享容器证明，哪些只是 mock 或故障注入，哪些仍未验证？
