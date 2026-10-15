# Day 5 完成记录与开发讲解

日期：2026-10-14，Asia/Kuala_Lumpur。目标是将 Day 4 的真实上传推进为“可解析、可定位、可回滚、可重复执行”的片段持久化链路。本文包括实际实现、证据范围、独立开发顺序与剩余风险。工程检查通过不等于你已掌握；最后的三个自查需要你亲自解释。

## 1. 今天交付了什么

输入是可信用户身份、已登记的 document_id，以及数据库关联的服务端文件引用；输出是有来源定位的 chunks 和 PARSED 状态。没有新增公开解析接口。开发命令读取本机受保护凭据，认证后调用内部服务；之后的 worker 可复用该服务。

实际演示：一份含中文、UTF-8 BOM、CRLF 的合成 TXT 得到 4 个片段；三份公开 PDF 分别得到 43、74、211 个片段，共 328 个 PDF 片段。四个文档均为 PARSED，index_version 为 null，整体 ingest job 仍为 PENDING。没有 Embedding、检索、模型回答、医学建议、队列、worker heartbeat 或完整 ready 验收。

完整本地测试和独立虚拟环境测试均为 343 项通过、0 跳过，均实际执行 81 项数据库检查。基础服务无关检查 261 项通过、82 项跳过；跳过包含 81 项真实数据库检查和 1 项容器检查，不能把跳过算通过。保留一项已有 TestClient/httpx 弃用警告。本次没有提交 Git、推送 GitHub 或公开部署。

## 2. 开始前检查与方案选择

开始时只有 AGENTS.md 的既有修改；保留该修改和 Day 1–4 历史记录，未修改 sources/。当前 sources/ 目录不存在，没有创建同步资料目录。现有数据库、迁移、文件存储、身份、配置和 API 契约继续使用；没有引入第二套持久化框架或医学专用平台分支。

检查了锁定和已安装版本：Python 3.12.14、FastAPI 0.141.1、Starlette 1.6.0、SQLAlchemy 2.1.4、psycopg 3.3.6、Alembic 1.20.0。唯一依赖变更为 pypdf[fonts] 6.20.0 → 6.20.1，没有新增依赖种类。

pypdf 6.20.1 是 2026-10-12 的正式安全补丁，修复循环树处理。升级前使用当时已安装的 6.20.0 保存了三份固定语料的页数、字符数和逐页文本 JSON 的 SHA-256，记录于 docs/day5-parser-baseline.json。升级后逐项比较完全一致，并验证六个锚点实际存在于正确页的 chunk。这个结果支持采用补丁，不代表所有 PDF 都没有差异，也没有旧版计时结果可用于性能比较。

保留普通文本提取、同步 SQLAlchemy 和现有服务分层。pypdf layout 模式有正式实现，但本次未做普通模式与 layout 的质量对照，不宣称某一种更优。Docling 有官方架构与技术报告，提供布局和表格能力；本次没有在本项目上验证收益，增加流水线与模型依赖不符合今天的最小交付，延期。没有凭版本新旧或营销性能替换架构。

依据：[pypdf 发布说明](https://github.com/py-pdf/pypdf/releases/tag/6.20.1)、[文本提取及资源边界](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[Python 子进程](https://docs.python.org/3.12/library/subprocess.html)、[资源限制](https://docs.python.org/3.12/library/resource.html)、[SQLAlchemy 事务](https://docs.sqlalchemy.org/en/21/orm/session_basics.html)、[Docling 架构](https://docling-project.github.io/docling/concepts/architecture/)、[Docling 技术报告](https://arxiv.org/abs/2408.09869)。

## 3. 文件职责与设计

| 文件 | 职责 |
|---|---|
| app/ingestion/text.py | 无数据库/存储访问的文本提取、结构化结果、安全错误与确定性切分 |
| app/ingestion/worker.py | 私有子进程协议、资源限制、从 stdin 接收有界字节 |
| app/ingestion/parser.py | 启动子进程、截止、kill/reap、结果结构校验 |
| app/services/ingestion.py | 用户、版本、文件与状态校验；组织解析和原子保存 |
| app/db/repositories.py | 新增片段写入/按 owner 查询；文档查询可显式加锁 |
| app/storage/files.py | 新增 read_verified，同一次打开读取并核验实际解析字节 |
| app/api/documents.py | 扩展安全错误白名单，保留已实现的状态响应形状 |
| app/core/config.py、.env.example | 默认 60 秒、最多 120 秒的解析截止配置 |
| scripts/day5.py | 语料比较、演示、解析、片段查看、测试、只读审计与真实停机演练 |
| tests/test_day5.py | 46 项不依赖服务的解析、切分、进程与安全检查 |
| tests/test_day5_database.py | 20 项真实 PostgreSQL 集成检查 |

没有增加数据库表、迁移、向量列或新框架。片段的来源、许可、日期与版本通过 document_id 关联现有 documents 记录。section 没有可靠信息时为空，不用模型补造。

## 4. 一次调用怎样完成

1. 开发命令从受保护的本地凭据读取所选 A/B 的 Key，AuthService 验证摘要和启用状态，得到可信 owner。命令不接受原始 Key、任意数据库地址或文件路径参数。
2. 用 owner 和 document_id 查询文档，验证处理版本，读取文件引用与来源元数据。不存在、越权、停用用户使用固定的 resource_not_found；不会先读其他用户的文件。
3. 关闭数据库 Session，再读取文件。read_verified 通过受控目录描述符打开文件，拒绝符号链接与非普通文件，限制读取长度，并核验同一份字节的大小和 SHA-256。
4. 将这些已核验字节交给子进程解析，在父进程切分；这一段没有数据库 Session 或长事务。真实集成测试在解析入口检查连接池 checkedout 为 0。
5. 打开短事务，按固定顺序锁用户、文档，重新检查身份、状态、版本、元数据与文件引用。竞争请求若已完成，直接返回它的已有结果。
6. 保存全部 chunks 并将文档置为 PARSED，在同一事务提交。整体 job 继续 PENDING，index_version 为 null；解析失败则原子保存文档 FAILED、job FAILED 和固定错误码。

两个请求可以重复做解析计算，但保存阶段串行核对，同一文档只保留一组片段。今天没有实现任务调度锁或完整 attempt 状态机，不把这个行锁方案称为 worker 调度器。

## 5. TXT 编码、范围与切分规则

TXT 用 UTF-8-sig 严格解码，接受有/无 BOM。原文件保存和哈希继续使用原始字节；片段字符范围对应解码后、不含 BOM 的 Python 字符串。保留 CR、LF、空格和 Unicode，不使用 text-mode 文件读取造成换行转换。

范围采用 [char_start,char_end)，即原文[start:end] 必须与 content 完全相同。中文的 UTF-8 字节数通常与 Python 字符数不同，因此不能混用字节偏移和字符范围。拒绝 NUL、非法控制字符和不能写入 UTF-8 的孤立代理字符；跨进程结果也重新检查这些条件。

每片最多 1000 个字符，重叠 100 个字符；下一起点为当前终点减 100。到达最后字符后立即结束，不生成额外的尾部重复片段。空白窗口跳过，ordinal 从 0 连续编号。size 必须为整数且 overlap 满足 0 ≤ overlap < size，避免不前进或死循环。1000/100 是可复现基线，没有测出检索最优值；字符数不是 Token 数，后续 Embedding 还须核对模型输入限制。

PDF 每页独立切分、不跨页；一页多个片段保存相同物理页码，TXT 才保存字符范围。原 PDF 中间有空白页时不把它删出页序，后续页仍保持真实编号。

## 6. PDF 解析和资源控制

使用 PdfReader(strict=True)。先拒绝加密，再检查最多 50 页；累计提取最多 200000 字符。空白文档、损坏文件和不支持的图片页安全失败，不调用 OCR，不生成替代文本。

单页解码内容流另限 4 MiB，累计 16 MiB。这是明确记录的额外限制；解码本身发生在长度检查之前，因此它不能代替硬内存隔离。原始文件仍受 5 MiB 限制。

父进程使用隔离 Python 参数启动子进程，显式传入精简环境，不继承 Key、数据库地址、Python 配置或模型地址。输入通过 stdin；子进程 stderr 丢弃，stdout 只输出有界结构化结果。父进程检查页序、字段类型、空内容、字符数量及文本合法性，未知错误或非法协议变为 parse_failed。子进程退出、启动失败也不会暴露内部异常。

默认解析截止 60 秒，配置最多 120 秒，无自动重试。超时执行 kill 后 communicate 回收；真实测试启动了休眠子进程，确认它已经退出。另设置 CPU 时间限制、关闭 core dump，并尝试 512 MiB 地址空间上限。本机 macOS 返回不支持，实际 memory_limited=false。当前没有完整 OS 网络、文件系统或内存沙箱；不能处理任意恶意公开上传并据此宣称安全上线。解析截止也不是本地文件读取、数据库提交或整个摄取尝试的总截止，任务级 300 秒预算留给后续 worker 接入。

图片页判断仅覆盖无文本且含直接 Image XObject 的情况；嵌套 Form、已有 OCR 文本层与复杂混合扫描件不保证被完整识别。实际验收语料是已核验的文本型公开 PDF，不把“存在一些文字”当成整份文件完整提取的证明。

## 7. 状态、版本与公开接口

| 情况 | 文档/任务与对外结果 |
|---|---|
| 解析成功 | 文档 PARSED、index_version=null；整体 job=PENDING |
| 已 PARSED/READY 重放 | 核验文件，复用已有片段 ID；不降级 READY |
| 已 FAILED 重放 | 复用失败；命令非零退出，不自动重试 |
| 内容解析拒绝 | 同一事务保存文档与 job FAILED，chunks 为 0 |
| 片段写入中途失败 | 本次全部回滚，文档仍 UPLOADED、任务仍 PENDING |
| 数据库不可用/提交确认丢失 | 安全数据库错误，不声称失败已保存；恢复后重放核对 |
| 文件缺失、损坏或替换 | storage_unavailable，不删除已存片段或自动替换原证据 |
| 处理版本改变或任务已 QUEUED/RUNNING | ingest_state_conflict，不写入迟到结果 |

Day 4 的 ingest-v1 只上传登记、没有任何已实现解析输出；今天首次固定其 pypdf 6.20.1 与切分规格。升级前后固定语料文本哈希一致，因此没有改写既有 chunks，也没有创建重复版本。以后改变解析、规范化、切分规则时必须显式使用新版本。当前服务拒绝其他版本，不能对任意历史版本套用当前实现。

状态接口 error 保持 Day 4 的 str|null。发现原契约曾给出对象示例，现已明确记录勘误并修正示例，未改变已实现字段形状。文档 FAILED 的查询成功仍为 HTTP 200；请求自身的失败才用原有顶层 {error:{code,message}}。新增解析错误进入白名单；未知码依旧映射 internal_error。

## 8. 测试到底证明了什么

46 项新增快速检查覆盖中文/BOM/CRLF 定位、短片段与长段落、1000/100 边界、非法 overlap、页数和字符上限、空白页物理编号、损坏/加密/图片 PDF、循环页树、解码流限制、真实子进程与精简环境、真实超时回收、崩溃/非法协议/启动失败、危险文本、配置上限、文件哈希改变和 FIFO 拒绝、演示测试库保护、CLI 错误脱敏、缓存忽略。

20 项新增真实数据库检查覆盖实际 TXT/PDF 解析、元数据保留、四种真实解析失败、重复片段 ID、READY 不降级、owner/停用用户、文件损坏、第二片段违反真实数据库约束时整体回滚、数据库写入异常、并发解析、解析期间无连接、版本改变后拒绝迟到写入、RUNNING 任务保护、提交确认丢失后重放、仓库独立 owner 过滤、超时错误持久化、PARSED 文件丢失、其他处理版本保留。

测试层级严格区分：

- pypdf 与真实子进程检查是真实解析/进程，不是模型 mock。
- 并发保存使用固定解析结果和线程屏障来稳定复现竞争，事务和数据库都是真实 PostgreSQL。
- 写入异常是故障注入；提交确认丢失是在真实 COMMIT 之后注入，不是声称发生真实网络丢包。
- 状态 READY 是测试夹具，不代表已经运行 Embedding。
- 完整演示启动真实 loopback HTTP API，验证跨进程读取和重启；FastAPI TestClient 的其余测试不冒充真实网络。
- offline-check 确实停止本项目数据库，验证 health 200、业务 503、解析固定数据库错误，再恢复并使用新连接探测；不代表自动连接池恢复或任意网络故障的统一时限。
- 真实模型与医学质量评测均未执行，远端 GitHub Actions 未执行。

## 9. 真实资料与目视核对

| 资料标识 | 页数 | 字符数 | PDF 片段数 |
|---|---:|---:|---:|
| prisma-2009 | 6 | 36324 | 43 |
| prisma-2020 | 15 | 61579 | 74 |
| prisma-2009-explanation | 28 | 182083 | 211 |

六处固定锚点均实际出现在正确物理页的片段中；还用 PDF 技能和 pdftoppm 渲染并目视检查了物理页 2、5、8，分别核对 chunk ordinal 9、22、53 所在锚点。预览和记录在 .cache/day5/。这是三页抽查，没有逐页或逐表人工审阅，也没有证明段落阅读顺序、表格语义或医学正确性。

## 10. 开发中遇到的问题与处理

1. 沙箱最初不能解析包源域名，容器启动也缺少系统访问权限；按权限流程完成补丁下载与现有 VM 启动。没有把失败安装或未运行数据库测试算通过。
2. 演示最初在应用库新增失败文件被拒绝。只读核对发现历史 Day 3 合成任务也占未结束额度，A/B 在新增资料后均到五个。保留所有旧数据和上限，失败 HTTP 用例改用显式验证库名的 traceagent_test 与独立上传目录，并加回归检查防止错误指向应用库。成功资料仍保存在应用库。
3. 新的单次核验读取使用 O_NONBLOCK 后再 fstat，避免把恶意 FIFO 当作普通文件打开时先阻塞；有真实 FIFO 回归验证。正式文件存储仍保留原本独占写入/原子发布机制。
4. 状态错误示例与代码不一致，通过兼容性勘误解决；新解析错误不再被错误白名单吞为 internal_error。
5. 独立环境首次指定 3.12.14 离线安装时找不到受管理解释器。改为显式复用现有 .venv/bin/python 创建新的虚拟环境，验证其版本为 3.12.14。独立虚拟环境不等于全新机器或操作系统。

## 11. 如何复现和亲自展示

项目根目录执行；已有凭据不得删除或重建，已有数据库卷和上传文件一起保留：

```bash
bash scripts/dev.sh uv sync --locked --python 3.12.14 --dev
bash scripts/dev.sh .venv/bin/python -m scripts.day3 prepare
bash scripts/dev.sh .venv/bin/python -m scripts.day4 prepare
bash scripts/dev.sh colima start --cpu 2 --memory 2 --disk 10 --vm-type vz --mount "$PWD/.cache/day4/uploads:w" --ssh-config=false
bash scripts/dev.sh .venv/bin/python -m scripts.day3 up
bash scripts/dev.sh .venv/bin/python -m scripts.day3 migrate
bash scripts/dev.sh .venv/bin/python -m scripts.day4 seed
bash scripts/dev.sh .venv/bin/python -m scripts.day5 corpus
bash scripts/dev.sh .venv/bin/python -m scripts.day5 test
bash scripts/dev.sh .venv/bin/python -m scripts.day5 demo
bash scripts/dev.sh .venv/bin/python -m scripts.day5 audit
bash scripts/check.sh
```

缺少公开语料缓存时，先执行 `bash scripts/dev.sh .venv/bin/python -m scripts.verify_sources --download`；它只恢复固定公开来源并核验哈希，不任意抓取用户 URL。不要同时执行两份真实数据库测试，也不要让 demo 与测试并行使用 traceagent_test。

查看本次真实 TXT 的片段，或重复解析验证 ID 不变：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day5 inspect aa220fa2-0d2d-4081-9f9a-0e6ca2ef854b --owner A --show
bash scripts/dev.sh .venv/bin/python -m scripts.day5 parse aa220fa2-0d2d-4081-9f9a-0e6ca2ef854b --owner A
bash scripts/dev.sh .venv/bin/python -m scripts.day5 inspect 9011b953-bf99-475f-a616-76b0f08c21a8 --owner A
```

最后一条故意用 A 查询 B 的资料，应该安全失败且非零退出。换成 --owner B 才能查看。示例 ID 来自这次本机数据库；新机器使用 demo 输出的实际 ID，不能把旧 ID 当成新机器已有数据。

需要 HTTP 手动展示时，用既有 day4 serve 启动本机 API，在 /docs 查询 demo 返回的 ID；Key 只从本机受保护文件读取，不贴到聊天、日志或截图。--show 只用于已知公开/合成资料，会输出最多两个片段；不向演示加入患者资料。

独立环境：

```bash
bash scripts/dev.sh env UV_PROJECT_ENVIRONMENT=.cache/day5/ci-env uv sync --locked --offline --python .venv/bin/python --dev
bash scripts/dev.sh .cache/day5/ci-env/bin/python -m scripts.day5 test --report-label isolated
```

offline 需要已缓存包；没有时明确去掉 offline 下载，不把安装失败当测试成功。锁定解释器按版本无法离线定位时可显式指向已核对版本的现有解释器。

真实停机演练会短暂停止本项目数据库，不要与其他数据库操作并行：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day5 offline-check
bash scripts/dev.sh .venv/bin/python -m scripts.day3 down
bash scripts/dev.sh colima stop
```

这些停止命令保留卷。本次临时 API 已关闭；最终数据库/VM 状态以验证记录为准。

## 12. 统一缓存管理与保留规则

唯一清单为 .cache/manifest.json，生成源是 scripts/dev.sh。新增 Day 5 比较、临时报告、隔离环境、PDF 预览和失败用例上传均在 .cache/day5/；Python 字节码、工具缓存、测试临时文件沿用既有清单路径。复用的临时 API 日志在 .cache/day4/api.log，也属于该清单管理范围。

.gitignore 已明确忽略清单和 .cache/day5/，同时保留整个 .cache/ 的忽略规则；有实际 git check-ignore 和自动化检查。清单只记录路径和生命周期，不记录秘密值。

应用库引用的 .cache/day4/uploads/、.cache/day3/database.env、.cache/day4/auth.env 不能随便删除，必须与数据库卷一起保留。Day 5 failure-uploads 只关联可重建测试库，不应当成应用证据备份。模型与 VM 位于既有清单路径；停止服务再清理。docs/day5-results.json、解析基线和本文属于交付证据，不是缓存，应保留。

## 13. 如果你来开发，建议怎样推进

先独立写出输入、输出和三种结果，再画一条最小链路。先处理十几行中文 TXT，验证字节、字符和 [start,end)；然后做两页 PDF，验证空白页不会改变编号；最后才接数据库。每一步先能解释、再让 AI 扩展。

你必须独立检查的关键点：

- 边界：上传成功不保证内容可解析；PARSED 不等于 READY；查询成功 HTTP 200 不等于文档处理成功。
- 安全：owner 来自认证，不来自请求体或模型；存储键来自服务端；来源 URL 只是元数据。
- 定位：保留解码策略、换行和版本。不要随手 strip/替换连字符后继续使用旧字符范围；PDF 物理页不是刊物印刷页标签。
- 事务：模型或解析等待不占事务；片段和状态原子提交。失败状态自身也可能写不进去，必须说清真实已保存内容。
- 幂等：前置查重不足以处理并发，保存前还要锁定/重查，并有数据库唯一约束。重复解析不能生成新的证据 ID。
- 错误：只暴露固定码；不要为了调试把异常、SQL、源文本、Key 或完整环境打印出来。
- 资源：小压缩文件也可能有大解码流；超时线程仍可能继续运行。可终止进程也不自动成为完整沙箱。
- 版本：依赖锁、解析器、规范化、切分参数共同决定证据结果。改变处理行为时要考虑新版本和旧结果保留。
- 验证：给测试用例写出预期行为，再运行。看到 skipped 要解释依赖条件，不能改成无条件成功。
- 数据：测试库和应用库明确分开；不能为演示删除旧任务或提高配额；不能把数据库关联文件当可随意清理缓存。

AI 可以帮助搭骨架、组织参数化测试、查官方文档和检查异常分支。你必须独立审查生成的查询是否带 owner、事务是否正确、超时是否真停止、错误是否泄露、真实文件位置是否匹配。AI 生成的断言不等于可信评测，最后要用真实输入和真实依赖验证。

## 14. 与实习和 AI Coding 准备的关系

今天优先练 Python/backend 的文件 IO、Unicode、函数边界、子进程、SQL 事务和错误处理；LLM 应用侧练 RAG 之前的可靠资料输入与来源追溯；agent 侧练可信身份和持久状态的边界。尚未引入工具调用或 LangGraph，应用工程交付不要求先训练模型或复现算法研究。

AI Coding 准备包括把“支持 PDF”拆成可验收需求，给 AI 一个模块的明确输入/输出，审查差异，根据失败证据调试，区分测试层级，并交付可复现记录。这些是岗位准备优先级，不是保证某家公司考试一定覆盖的题目。

实用追问：为什么解析时不保持数据库事务？文件通过哈希核验后被替换会怎样？为什么重复解析不能重建 chunk ID？为什么 mock 成功不能证明真实 PostgreSQL 回滚？为什么六个锚点通过还不能宣称完整医学 RAG 质量？

## 15. 审查结论与剩余边界

正确性：核验真实字节、字符范围、物理页、版本与状态；并发保存及重放通过。错误处理：内容拒绝、进程失败、事务回滚、真实数据库停机与模糊提交均有证据。测试：成功、重要边界、失败与真实集成分别记录。安全：owner 查询、固定错误、精简子进程环境、符号链接/FIFO 拒绝与缓存权限已检查。可维护性：解析、编排、持久化和 HTTP 分层，无额外框架、迁移或医学专用平台规则。

剩余限制：macOS 硬内存上限未启用；不是生产恶意文件沙箱；复杂版面/表格/混合扫描/OCR 完整性未验收；切分参数未经检索效果比较；队列、任务 attempt/重试、任务总截止和过期 PENDING 核对仍待 D6 及后续阶段；没有 Embedding、完整 RAG、医学引用/拒答评测；没有远端 CI 或公开部署。当前 A/B 已各有五个未结束任务，新文件会 429，重复上传仍可复用，后续按真实任务生命周期推进，不能随意清空历史。没有已知阻塞本地 Day 5 范围的 P0/P1；公开部署前必须另行解决解析资源隔离等发布门槛。

请你亲自回答：

1. 为什么解析成功只能 PARSED，READY 还需要什么证据？
2. 第二个片段保存失败、或 COMMIT 已成功但确认丢失，分别会保留什么？
3. 哪些结果由真实 PDF、PostgreSQL、HTTP 和子进程证明，哪些只是故障注入，哪些仍没验证？
