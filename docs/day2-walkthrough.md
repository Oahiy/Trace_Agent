# Day 2 完成记录与开发讲解

日期：2026-10-11，Asia/Kuala_Lumpur。今日目标是锁定工程基础与接口，为后续数据库、摄取和 RAG 提供可验证的起点。完整机器可读结果在 `docs/day2-results.json`。文中的未来接口预期与当天实际运行结果分开记录。

最终实际结果：原环境和隔离虚拟环境均为 119 项通过、1 项真实 Docker 测试跳过，失败/错误为 0；Ruff 检查及 11 个 Python 文件的格式检查通过。真实健康 HTTP、关闭进程后的连接失败、三份 PDF 的下载恢复/哈希/定位检查通过。七条问题草案有七处证据定位，已由程序核对；草案由 Codex 整理，开发者人工语义核对仍待完成。当天未运行真实模型质量评测，未推送 GitHub。

## 1. 完成范围与交付物

| 当天任务 | 完成内容 | 证据或入口 |
|---|---|---|
| 检查现状与保留工作 | 读取 AGENTS、Git 修改、Day 1 代码与学习草稿；保留已有暂存状态 | 本文与验证记录 |
| 依赖锁定 | 核心直接依赖改为已有具体版本；新增配置管理的直接声明、Ruff 开发依赖 | pyproject.toml、uv.lock |
| 配置 | 环境变量解析、默认值、资源边界、本机模型地址、启动失败脱敏 | app/core/config.py、.env.example |
| Python 分层实验 | 输入模型、不可变内部对象、仓库协议、内存仓库、业务层、独立演示 | app/experiments/day2.py、scripts/day2.py |
| 接口定稿 | 八个接口的输入、输出、状态、身份、幂等、错误、预算与实施状态 | docs/api-contract-v1.md、app/api/schemas.py |
| pytest 与 Ruff | 成功、边界、失败、API 兼容、缓存管理和来源验证测试 | tests/test_day2.py、tests/test_sources.py |
| 基础 CI | 固定 action commit、uv 版本、Python 版本、锁定安装和共同检查入口 | .github/workflows/ci.yml、scripts/check.sh |
| 真实资料 | 三份文本型 PDF、来源日期、许可、哈希和物理页码定位 | docs/day2-sources.json |
| 问题草案 | 七条有证据定位或明确拒答预期、待人工核对的草案 | evals/med/day2-draft.json |
| 缓存管理 | 唯一清单、新增缓存路径、gitignore 验证 | scripts/dev.sh、.cache/manifest.json、.gitignore |
| 复现与讲解 | 原环境/隔离环境检查、真实 HTTP、下载恢复与文档 | README、本文、结果 JSON |

没有推送 GitHub，也没有创建远端仓库、提交或修改已有暂存内容。没有编辑 AGENTS.md 或 learn.py。`sources/` 在当前仓库不存在，整个任务没有创建、编辑或移动同步来源目录。三份公开 PDF 放在忽略的缓存中，版本化文档保存来源与验证摘要。

## 2. 从现有代码开始，而不是重建项目

Day 1 已有 FastAPI 健康接口、HTTPX 模型调用实验、PDF 提取、pytest 和缓存 wrapper。因此 Day 2 沿用这些能力。新增真实业务接口、数据库、队列与 RAG 会超出今天的范围，它们按 D3 之后的日程实现。

你的 learn.py 已经有 DocumentCreate、Document、Protocol、MemoryDocumentRepository 和 DocumentService。我保留草稿，在独立实验模块中整理了同一条调用链，并补足空白输入、长度、失败和别名修改等边界。它没有被接入上传 API，因此不会把学习用内存存储误当作正式持久化。

启用 Ruff 后，Day 1 的三个文件需要调整导入排序与格式。我先保留格式前快照，再整理；移除顶层 import 节点后对比语法树，三个文件的其余逻辑一致，同时重跑 Day 1 回归。没有趁机改模型行为、PDF 规则或容器实验。learn.py 明确排除在 Ruff 范围外。

你开发时先读取 Git 状态和真实文件，区分“已有修改”“今天修改”和“计划功能”。不要直接让 AI 对全仓库重构，也不要为了干净的 Git 状态把已有工作 reset 掉。

## 3. 依赖与环境：能运行和能复现是两件事

直接依赖锁定为 FastAPI 0.141.1、HTTPX 0.28.1、Pydantic 2.13.4、Pydantic Settings 2.15.0、pypdf 6.20.0；开发依赖为 pytest 9.1.1 和 Ruff 0.17.0。Python 实际版本为 3.12.14，uv 为 0.12.24。

Pydantic Settings 原来作为间接依赖已安装，今天代码直接导入它，所以明确声明为直接依赖。Ruff 是唯一新安装的包，用于检查和格式化。没有引入数据库 ORM、队列框架、LangChain、LangGraph 或多供应商模型 SDK。

`pyproject.toml` 描述项目意图；`uv.lock` 固定完整解析结果。普通运行使用 `--locked`，声明与锁文件不同步时应失败，而不是偷偷升级。需要升级时主动修改声明、更新锁文件并跑相关回归。当前精确版本只是本次可复现基线，不意味着永远不更新或已经完成全面漏洞审计。

第一次隔离环境离线安装失败：部分已安装的包没有对应下载缓存。这个失败没有被记为通过。我按锁定版本补齐缓存后在 `.cache/day2/ci-env/` 创建独立虚拟环境，并运行与 CI 相同的命令。它证明当前 macOS/Python 环境的锁定安装与检查可复现，不能替代 GitHub Ubuntu runner 的真实执行。

重跑：

```bash
bash scripts/dev.sh uv sync --locked --python 3.12.14 --dev
bash scripts/check.sh
bash scripts/dev.sh env UV_PROJECT_ENVIRONMENT=.cache/day2/ci-env uv sync --locked --offline --python .venv/bin/python --dev
bash scripts/dev.sh env UV_PROJECT_ENVIRONMENT=.cache/day2/ci-env bash scripts/check.sh
```

离线复现依赖已完整缓存；新电脑或缓存清理后先用联网的锁定安装。安装虚拟环境不会启动 API 或真实模型。

## 4. 配置的输入、输出与失败

输入是带 TRACEAGENT_ 前缀的环境变量。输出是经过验证、不可变的 Settings 对象，保存在应用的 `state.settings`。create_app 可接收预先创建的 Settings，便于测试；默认在应用创建时加载环境。

正常启动的调用关系是 Uvicorn 导入 app.main → 创建应用并校验配置 → 接收请求 → FastAPI 匹配 GET /health → 函数返回字典 → 序列化为 JSON。配置在创建时形成快照，修改环境变量后需要重新创建应用或重启进程，不应在每个请求里重新读取。今天没有数据库连接等资源，未来资源初始化/释放应通过 lifespan 管理，测试也要真正执行生命周期。

| 配置 | 默认值 | 有效边界 |
|---|---|---|
| LOCAL_MODEL_URL | http://127.0.0.1:11434 | 只允许本机 HTTP origin，不含账号密码、路径、query、fragment |
| MODEL_TIMEOUT_SECONDS | 30 | 大于 0、不超过 120、有限数 |
| MAX_FILE_BYTES | 5242880 | 1 到 5 MiB |
| MAX_PDF_PAGES | 50 | 1 到 50 |
| MAX_TEXT_CHARACTERS | 200000 | 1 到 200000 |
| MAX_QUESTION_CHARACTERS | 2000 | 1 到 2000 |

环境变量本来是字符串，所以配置必须允许受控解析，例如 `"2.5"` 变成超时数值。空环境值被忽略，使用默认值；负数、NaN、无穷、超过资源上限或不允许的模型 URL 会在应用创建前失败。最终审查发现 urllib 的 URL 解析会忽略部分空白/控制字符，单看解析后的 host 会接受原始格式有问题的地址；已显式拒绝 URL 内空白与控制字符，并加了换行、制表符、前导空格、C0 和 DEL 五条环境回归测试，以及直接构造配置时的 NUL 检查。

一次测试准备失败也保留在问题记录中：最初试图把 NUL 写入环境变量，操作系统在应用校验之前就拒绝了它。环境测试改用可写入的 C0 字符；NUL 单独通过 Python 构造配置验证。不要把测试框架或操作系统准备失败误诊为业务校验失败。

`load_settings` 捕获 Pydantic ValidationError，只抛出固定的 `invalid_configuration`，不传播环境值。hide_input_in_errors 进一步收紧错误字符串，但不能把它误认为 `.errors()` 中永远没有输入；公共接口仍不能直接返回验证对象。当前配置不包含秘密字段；将来添加 Key 要继续使用不回显的失败边界。

`.env.example` 只有空模板。应用故意不自动加载 `.env`，需要通过环境明确提供配置，避免当前目录中的文件悄悄改变行为。超时或模型地址变化只影响未来接入的适配器；Day 1 的独立模型实验仍用固定参数，不能声称它已经自动遵循新配置。

测试已覆盖默认值、环境覆盖、空值回退、恰好有效的边界、非法端点、负数与非有限值、配置不可变，以及 `.env` 不被隐式读取。

## 5. 文档分层实验的设计

```text
外部 payload
  → DocumentCreate.model_validate
  → 内部 Document
  → DocumentService.create
  → repository.save
  → 返回创建结果

查询 ID → DocumentService.get → repository.get → Document 或 None
```

输入使用合成工程文字，不含真实患者数据。title 去首尾空白后为 1–200 字符，content 为 1–200000 字符；错误类型和额外字段被拒绝。外部 payload 不能提供 id 或 owner，内部调用者在练习中提供 `doc-001`。正式 UUID 与用户身份后续由服务端生成、验证。

DocumentCreate 负责外部校验。Document 是 frozen dataclass，保存内部值。Protocol 规定 save/get 能力，业务层只依赖这个约定。内存仓库用字典实现，不能假称它提供 SQL 事务、进程间共享或重启恢复。业务层不读取 HTTP 请求，也不决定 HTTP 状态码。

`create_document` 是实验边界：先校验，再转换，最后进入 service。顺序很重要。失败测试使用记录 save 次数的仓库，证明非法输入没有触发保存，不只是看见一个异常。不存在的 ID 返回 None，由未来 API 层决定是否映射成 404。

重复 ID 的 save 是创建语义：抛出 `document_already_exists` 并保留原文档，避免静默覆盖。这个约定仅适用于学习实验；正式文件去重、owner 和摄取版本规则已另写入接口契约。

仓库协议要求适配器把底层失败转成固定 RepositoryError。测试仓库主动抛出 `document_store_unavailable`，业务层不会返回“创建成功”或把读取失败当成 None。将来数据库适配器必须负责回滚与异常映射；普通编程错误不能仅靠 catch Exception 就获得正确恢复。

演示：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day2
```

实际演示包括正常保存、不存在文档、空白输入拒绝、没有保存非法数据、可变对象别名和不可变对象，以及默认数值转换与严格文档输入。CLI 的意外错误映射成 `day2_probe_failed`，退出码为 1，不输出原始异常。

## 6. 如果由你开发，Python 应注意什么

第一，类型提示与运行时校验不同。dataclass 不会因为 title 注解为 str 就自动拒绝数字。Protocol 也不会自动执行类型检查或输入验证；结构满足 save/get 的对象不需要继承它才能用于这个设计。今天没加入静态类型检查工具，不将 Ruff 通过称为类型证明。

第二，严格类型与业务有效性不同。`"   "` 是合法字符串，但不是合法文档标题。严格模式负责拒绝错误类型，去空白和长度约束负责内容边界。Pydantic 默认会将某些输入转换，例如数字字符串变成整数；请求契约的年份采用严格 int，所以字符串年份和 bool 都拒绝。环境配置则需要把字符串解析成数值。

第三，Python 名称通常引用对象。原学习草稿的可变 Document 保存后，外部修改同一对象也会改变仓库看到的值。新实验使用 frozen dataclass，标题等字符串字段不能常规赋值，所以不会通过普通别名意外修改。frozen 不是权限机制，也不是对任意嵌套可变对象的深度冻结；若加入 list/dict 字段还需要重新设计。

第四，今天对合成 content 做 strip 只是校验练习。真正 PDF/TXT 摄取要保存原始内容、原文件哈希与定位信息，不能因为清洗文本就破坏字符偏移、页码或证据引用。原文和检索用规范文本应有明确对应关系。

第五，service.create 先 save 后返回；save 失败就不能返回对象作为成功证据。get 返回 None 表示没有数据，RepositoryError 表示无法访问，两者不能混为一谈。正式数据库操作要有明确的提交/回滚，而不是把写字典的行为直接复制到数据库。

## 7. 接口定稿与兼容性

详细规范在 api-contract-v1.md。八个接口逐项记录输入、输出、错误、身份和当前实施状态。今天已测试 ResearchRequest：非空问题、最多 15 个规范且不重复的 UUID、包含端点的发表年份区间、受限幂等键、拒绝客户端 owner。ErrorResponse 支持固定 error 和必要时已提交的 job_id。

这些结构检查不等于资源真实存在，也不等于用户有权限，更不等于医学结论正确。数据库查询、owner 过滤和证据支持检查仍需各自实现和测试。

现有 `/health` 的成功体保持不变；404/405 默认 JSON 和 Allow 头也有精确回归测试。新业务接口的统一错误采用固定安全文本。今天没有挂载假研究接口来返回占位答案，也没有把全局错误 handler 改到所有现有路由上。

状态约定区分 PENDING/QUEUED/RUNNING/SUCCEEDED/FAILED 与 UPLOADED/PARSED/READY/FAILED。200 表示成功查询，不保证研究成功；202 表示接受任务，不保证执行完成。拒答可以是 SUCCEEDED 的正常结果，服务故障才是 FAILED。

接口定稿发现并解决两处重要边界：投递失败保留 PENDING 并返回 503 与 job_id，不能假报 QUEUED；原草案 6000 输入 Token 高于 D1 的 4096 上下文配置，因此新适配器初值收紧为完整输入预算 2800、输出 1024，真实模板与 Token 验证留到 D9。这个调整记录在契约中，没有静默改变 Day 1 模型实验。

未来模型一次总截止 30 秒，摄取尝试 300 秒，研究尝试 180 秒。自动网络重试最多追加一次，任务手动重试最多追加一次；累计调用/Token/费用不能通过重试重置。今天定义预算而未实现 worker，不能将预算文字当成已运行的资源控制。

## 8. 测试、Ruff 与 CI 怎么完成

Ruff 使用 Python 3.12 目标、100 字符排版、E4/E7/E9/F/I 规则，关闭 preview。检查源代码、scripts、tests，排除 sources、缓存和学习草稿。规则负责导入/语法相关低级问题与一致排版，不取代行为测试、安全审查或类型检查。

pytest 包括 Day 1 回归与新测试。新测试不仅检查 happy path，还验证长度恰好到上限与越界、非法请求不保存、重复创建保留旧对象、仓库失败、配置启动失败、未知路径、错误方法、缓存 wrapper 的失败退出码、PDF 缓存缺失/变更/定位错误与下载哈希不符不覆盖旧缓存。模型失败测试与下载 MockTransport 是固定工程模拟；当天三份 PDF 验证和真实健康 HTTP 是独立真实检查。

CI 的固定 checkout 和 setup-uv commit 已通过官方仓库 tag 元数据核对。workflow 只有 contents: read，checkout 不保留凭证，无发布步骤；锁定 Python 3.12.14 与 uv 0.12.24，安装锁文件，再调用共同的 scripts/check.sh。job 限时十分钟。

check.sh 明确关闭 Docker opt-in，避免机器是否运行 daemon 改变基础 CI 结果。真实 Docker 检查在今天跳过，没有冒充通过。无需 Ollama 服务，也不产生真实模型调用费用。

GitHub 未推送，因此 Actions 未实际执行。已解析 YAML、核对执行入口和 SHA，并在当前机器的独立虚拟环境运行共同入口。这是可检查的本地 CI 准备，Linux runner 与首次联网下载仍需远端验证。若未来修改 workflow，不能只凭 YAML 解析成功声称 CI 成功。

## 9. 三份真实资料与七条问题

首批资料来自 PLOS：PRISMA 2009 statement、PRISMA 2020 statement、PRISMA 2009 explanation。分别为 6、15、28 个物理 PDF 页，文件与提取字符数都满足 D1 解析限制。三份都核对作者、发表时间、DOI、来源 URL 和 CC BY 许可依据，并固定 SHA-256。许可要求署名；没有从未明确的信息推断具体 CC BY 版本。

BMJ 的 PRISMA 2020 explanation HTML/PDF 下载返回 HTTPError，官方关联已确认，但没有把它计为解析成功的资料。改用 PLOS 2009 explanation 保持同一主题；未绕过访问限制。

question 草案覆盖单文档、范围限制、跨版本比较、条目解释、发表年份过滤、无答案和医学安全。五条证据型问题都有实际 PDF 页码与短定位锚点；无答案与安全问题明确预期拒答。2020 是版本名称，该 statement 在此语料的发表年份是 2021。版本变化不能直接标为真实医学冲突。

验证脚本检查固定文件哈希、页数与六个来源锚点；另检查七条草案中的七个证据定位。匹配对空白做规范化，以容忍 PDF 缺空格/断行，但保留原提取文本。锚点存在不能证明整个表格正确、复杂布局无误或每条语义结论都受支持。

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.verify_sources
bash scripts/dev.sh .venv/bin/python -m scripts.verify_sources --download
```

下载只允许已选出版社 HTTPS origin、最多 5 MiB、每源 30 秒总截止、20 秒阶段超时，不自动重试/跟随重定向。下载先验哈希，再用临时文件原子替换；不匹配保留旧缓存并失败。来源更新需人工审核后更新清单，不能脚本悄悄接受新内容。这个 CLI 不是公开任意 URL 抓取接口。

这些是待人工确认的问题草案与来源验证，未调用真实模型作答。尚未冻结最终 10–15 份语料与 30 条评测，也未验证医学拒答或真实冲突处理。D8 前继续准备；不凭七条草案宣布 RAG 验收通过。

## 10. 缓存如何统一管理

唯一清单是 `.cache/manifest.json`，由 scripts/dev.sh 每次运行原子生成。清单按用途管理缓存目录，而非保存文件内容或命令参数，避免把秘密写入清单。新缓存均在它声明的目录下。

最后核对实际目录时发现旧清单漏列了已设置的 XDG 缓存目录；已补充 xdg/ 并加入 wrapper 回归断言。核对也按父目录处理 ollama/models/ 等嵌套用途，最终所有缓存目录都被清单覆盖。

| 路径 | 内容 |
|---|---|
| .cache/uv/ | 包下载缓存 |
| .cache/uv-python/ | wrapper 调用时安装的 managed Python |
| .cache/python/ | Python 字节码 |
| .cache/pytest/ | pytest 缓存、临时目录 |
| .cache/ruff/ | Ruff 缓存 |
| .cache/tmp/、xdg/ | 工具临时文件与通用缓存 |
| .cache/day2/ | 下载、解析文本、官方元数据、日志、格式前快照、独立虚拟环境与测试 XML |
| .cache/day1/ | 保留 Day 1 的公开 PDF、日志和模型记录 |
| 其余已有用途 | 模型、Colima/Lima、Docker 配置、Homebrew 下载/日志 |

`.cache/` 和清单文件均明确写入 .gitignore；实际 git check-ignore 验证了清单、新测试 XML、Ruff 缓存、真实 .env 被忽略，而 .env.example 可以提交。已有 .venv 保留，新增独立虚拟环境放在缓存中。

不要直接删除整个 .cache：其中有模型与 VM 数据。先停止对应服务，按用途清理；文档验证摘要是长期交付证据，保存在 docs 中。已有 wrapper 会设置本机模型地址与禁用云选项，检查或下载也通过它运行。未通过 wrapper 手工运行工具仍可能使用系统默认路径；不能宣称整个操作系统的所有缓存都由本项目管理。

## 11. 验收、风险和下一步

实际测试数量、执行命令、隔离环境与来源恢复结果以 day2-results.json 为准。真实临时 API 检查结束后已停止其进程；今天没有启动模型、Docker VM 或付费服务。配置、分层实验、契约、检查入口、资料与问题草案均已交付。

保留的限制：GitHub Actions/Ubuntu 尚未运行；真实 Docker 检查今天跳过；数据库、队列、身份、RAG、医学拒答与工具工作流尚未实现；PDF 空格、连字、表格仍可能有提取缺陷；问题草案不足以证明医学质量；内存实验不提供持久性；配置与预算需要后续 handler/adapter/worker 实际接入。没有把这些后续阶段的工作说成今天已完成。

按五个审查维度，正确性检查输入/状态/版本；错误处理检查固定错误码与不假报成功；测试检查成功/边界/失败与真实/模拟区分；安全检查 owner 外部字段、秘密回显、下载范围与 gitignore；可维护性检查分层、少量依赖、文档契约与共同检查入口。

今天的实习准备重点是 Python 对象/类型、后端输入边界、失败分类、测试设计与可复现交付。AI Coding 重点是依据实际仓库拆任务、审查 AI 修改、用独立预期验证实现。AI 可辅助生成代码与测试，开发者必须自己决定边界、审查错误路径、跑检查、核对原文，并讲清楚设计。这里不承诺具体公司的考试题型。

开发者理解能力尚需你亲自讲解确认，工程测试通过不能代替这一点。建议不看代码回答：

1. Pydantic、dataclass、Protocol 分别负责什么，为什么类型提示不等于输入校验？
2. 非法输入、缺失文档和仓库失败怎样结束，为什么不能都返回 None？
3. 本地测试、下载 MockTransport、真实 PDF 和远端 CI 分别能证明什么？

## 12. 官方资料与实现方式核查

核查日期为 2026-10-11。沿用已运行的 Python 3.12.14、FastAPI 0.141.1、Pydantic 2.13.4 与 HTTPX 0.28.1；没有找到足以支持当天更换框架或测试方案的项目收益。FastAPI 0.143.0 的 OpenTelemetry 自动配置变化、Pydantic 2.14.0 的新 Python 支持不解决当前任务，保留版本可减少迁移与兼容验证工作；未来安全或实际功能需求出现时重新核查。

配置采用已安装的 Pydantic Settings 2.15.0，比散落的手写环境变量转换更集中，代价是要理解默认值、空值、解析和错误脱敏。Ruff 0.17.0 为 2026-10-09 发布的稳定版，使用基础规则并关闭 preview；用途是维护一致检查入口，不宣称它能验证语义或安全。uv 的官方 CI 实现指南提供了锁定安装和 action SHA 的可直接核验依据。本机独立环境验证兼容性，不推广未经验证的性能或延迟收益。

- [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- [Pydantic 严格模式](https://docs.pydantic.dev/latest/concepts/strict_mode/) 与 [校验器](https://docs.pydantic.dev/latest/concepts/validators/)
- [Python Protocol](https://docs.python.org/3.12/library/typing.html#typing.Protocol) 与 [dataclass](https://docs.python.org/3.12/library/dataclasses.html)
- [Python URL 解析安全说明](https://docs.python.org/3.12/library/urllib.parse.html#url-parsing-security)
- [Ruff 配置](https://docs.astral.sh/ruff/configuration/) 与 [稳定版本元数据](https://pypi.org/project/ruff/0.17.0/)
- [uv GitHub Actions 指南](https://docs.astral.sh/uv/guides/integration/github/)
- [FastAPI 发布记录](https://fastapi.tiangolo.com/release-notes/) 与 [错误处理](https://fastapi.tiangolo.com/tutorial/handling-errors/)
- [Pydantic 发布记录](https://docs.pydantic.dev/latest/changelog/)
- [HTTPX 超时](https://www.python-httpx.org/advanced/timeouts/) 与 [传输和 ASGI 测试](https://www.python-httpx.org/advanced/transports/)
- 真实资料、作者、许可与 DOI 链接逐项保存在 day2-sources.json，来源署名保留在清单中。
