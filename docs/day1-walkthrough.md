# Day 1 完成记录与开发讲解

日期：2026-10-10，时区 Asia/Kuala_Lumpur。今天的可演示成果是一组可以独立重跑的依赖与接口实验。输入包括 HTTP 请求、项目介绍、工具请求、短文本和两份公开 PDF；输出包括健康响应、结构化工具调用、向量、带物理页码的文本和验证记录。

实际执行了 42 项自动化检查，全部通过。其中包含真实 Docker 集成检查；模型失败检查使用固定模拟响应，另行执行了真实本地模型实验。真实生成请求 6 次，Embedding 请求 1 次，云 API 预算和实际支出均为 0。费用不包含电费、磁盘和硬件成本。

**结论有明确边界：环境与接口验证通过；完整 RAG 尚未实现。一次工具后续摘要补述了工具未返回的信息，人工支持检查未通过，不能作为 MedEvidence 研究答案。**

机器可读的完整结果在 `docs/day1-results.json`，包括原始模型输出、工具观察、Token、耗时、模型 digest、文件哈希和测试结果。这个文件中的模型自然语言是实验原始输出，不是经过证据支持验收的医学输出。

## 1. 先检查仓库，而后决定修改范围

初始业务代码只有 `app/main.py`，已有 `/health`。Python 虚拟环境、uv 和 FastAPI 已存在。`.gitignore` 同时存在暂存与未暂存修改，`AGENTS.md` 也有你已有的修改。

我保留了这些已有工作，没有修改 `AGENTS.md`、现有 API 的业务行为或 `sources/`。当前磁盘上原计划的 Markdown 文件不存在，仍有 `docs/implementation-plan-30-days.html`，因此用该计划及本聊天确认的 D1 任务作为依据。

新增实验放在 `app/experiments/day1.py`，命令入口放在 `scripts/day1.py`，测试放在 `tests/test_day1.py`。它们没有被接入公开 API，也没有提前加入数据库、队列或完整 RAG。通用实验模块没有医学业务规则；MedEvidence 的研究范围和资料清单保存在文档中。

你开发时首先要把“已有代码”和“计划中的功能”分清。AI 容易基于描述假设某些目录、接口或依赖已经存在。实际检查文件、版本和 Git diff，可以避免覆盖别人的改动，也能避免使用不适合当前阶段的设计。

## 2. API：从请求到响应

设计很小：Uvicorn 接收本机 HTTP 请求，FastAPI 按方法与路径找到函数，函数返回 Python 字典，框架生成 JSON 响应。

| 输入或情况 | 预期 | 实际验证 |
|---|---|---|
| `GET /health` | 200，`{"status":"ok"}` | 真实 HTTP 通过，自动化也通过 |
| 不存在的路径 | 404 | 真实 HTTP 和自动化通过 |
| `POST /health` | 405 | 自动化通过 |
| 停止本次 API 进程后再次请求 | 连接失败 | 真实 HTTP 通过 |

真实检查使用一个动态选取的本机端口，启动自己的 API 子进程，等待服务就绪，完成请求后在 `finally` 中停止进程。启动等待有上限，停止失败时会强制结束。它不会关闭你其他终端里的服务。

相关知识包括 Python 导入、函数、字典、装饰器、HTTP 方法、状态码、JSON、服务启动和进程清理。`@app.get` 注册路由，函数名本身不会决定 URL。`/health` 成功能证明该进程和该路由工作，不能证明模型、数据库或 worker 正常；依赖就绪检查属于后续 `/ready` 的职责。

测试中用 HTTPX 的 ASGITransport 直接请求应用，另用真实 HTTP 检查网络与进程。ASGITransport 不会自动执行应用 lifespan；当前应用没有 startup 资源，所以够用，后续加入数据库等启动资源时需要明确测试其生命周期。

## 3. 容器：分别检查 CLI、运行时和实际执行

本机为 Apple Silicon，16 GiB 内存。通过 Homebrew 安装 Colima、Docker CLI、Compose 和 Ollama。Colima 使用 macOS VZ 虚拟化提供 Linux Docker 运行时，配置为 2 CPU、2 GiB 内存，未启动 Kubernetes。

实际版本是 Colima 0.10.3、Lima 2.2.1、Docker CLI 29.9.0、Docker Engine 29.5.2、Compose 5.6.0。`--disk 10` 是数据盘配置，Lima 还使用独立的默认 20 GiB 根盘；这是虚拟磁盘容量，不等于立刻占用同样大小的宿主磁盘空间。

宿主安装还引入了 Homebrew 管理的 MLX、MLX-C、Python 3.14 和 OpenSSL 4 等依赖。TraceAgent 的 `.venv` 仍然使用 Python 3.12.14。软件安装位于 Homebrew 标准目录，项目下载缓存和实验数据位于 `.cache/`；删除项目缓存不会卸载软件。

测试使用 Alpine 3.22.1，实际执行时固定完整镜像 digest：

```text
alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1
```

| 检查 | 实际结果 |
|---|---|
| 输出固定字符串 | `traceagent-container-ok`，退出码 0 |
| 容器未挂载宿主项目文件 | 隔离检查退出码 0 |
| 不存在的容器内命令 | 预期退出码 127 |
| 测试容器生命周期 | `--rm` 自动移除 |
| 网络 | 测试容器使用 `--network none` |
| Docker socket | 位于项目 `.cache/colima/default/docker.sock` |

最初边界检查误用了 `/bin/test`，该 Alpine 镜像没有这个独立可执行文件。我根据退出码定位原因，改为通过 `/bin/sh` 执行内置 `test`，加入真实容器回归检查并实际重跑通过。

你需要注意：CLI 有版本输出不代表 daemon 可用；镜像标签可能变化，digest 能标识具体内容；宿主路径和容器路径属于不同文件系统；Compose 能启动容器也不等于其中服务已经就绪。D3 的数据库需要独立健康检查、迁移和事务测试。

## 4. 本地模型生成：限制先于外部调用

按你的选择，服务使用本地 Ollama 0.40.2，仅访问 `127.0.0.1:11434`，关闭云功能。生成标签是 `qwen3:1.7b`，量化为 Q4_K_M。运行时元数据报告参数规模为 2.0B；记录同时保留标签和元数据，不能只凭标签推断实际模型内容。

生成模型 digest：

```text
8f68893c685c3ddff2aa3fffce2aa60a30bb2da65ca488b61fff134a4d1730e7
```

输入是一段 TraceAgent 项目介绍，要求两句英文总结；实际输出为两句。首次生成请求约 1.416 秒。模型输出长度上限为 256 Token，上下文配置为 4096 Token，temperature 为 0，关闭 thinking 和流式输出。这里只做短实验，不表示后续文档可以无限放进上下文。

每个完整实验进程最多 6 次生成请求和 2 次 Embedding 请求。实际使用 6 次生成、1 次 Embedding。生成输入累计 908 Token，输出累计 158 Token，Embedding 输入 47 Token，数据来自 Ollama 响应。Embedding 没有输出 Token 计数字段，保存为 null，不伪造为零。

调用用现有 HTTPX，显式声明为项目直接依赖。每次请求使用独立 AsyncClient 并关闭，通过 `asyncio.timeout(30)` 设总等待截止时间，同时设置 HTTPX 阶段超时。HTTPX 的 connect/read 等超时本身不能代替总请求截止时间。客户端取消等待不能单独证明模型服务已经停止推理；后续任务执行还需要独立处理服务端取消和资源释放。

失败请求先消耗调用次数，然后才发出请求，避免失败循环绕过预算。本实验不自动重试。鉴权或 HTTP 错误、连接失败、超时、错误 JSON、空输出等映射为固定错误码，不把供应商原始 body 或异常字符串传给调用者。

空白输入、HTTP 401、无效 JSON、错误响应形状、超时和预算耗尽已经用 MockTransport 检查。总截止时间检查用短测试期限取消一个缓慢模拟请求。这些证明程序的失败处理，不能当成真实模型质量评测。真实本地模型请求另有原始记录。

你开发时要独立解释：为什么失败也计入预算、哪类错误可以重试、哪里关闭客户端、超时后怎样结束，以及报告中的实际 Token 与估算费用有何区别。当前同步 CLI 内部使用 `asyncio.run`；后续接入异步 FastAPI handler 时，应提供可直接 await 的适配器，不能在已有事件循环里直接照搬这个调用方式。

## 5. 工具调用：模型提出请求，程序决定执行

模型收到两个工具的 JSON Schema：`search_evidence(query)` 和 `read_source(source_id)`。它们返回固定合成工程资料，用于验证调用协议，不属于真实医学证据。

真实实验的两个选择分别是：

```json
{"name":"search_evidence","arguments":{"query":"research methods"}}
{"name":"read_source","arguments":{"source_id":"demo-1"}}
```

程序提取结构化调用，检查白名单，用 Pydantic 校验参数，再执行对应函数，将结果作为 tool 消息发回模型。没有使用 `eval`、任意函数名执行或客户端提供的文件路径。模型没有被强制指定某一个工具；程序根据任务检查实际选择是否正确。

参数采用严格类型，拒绝额外字段；query 不能为空白，也不能超过 2000 字符。读取只允许固定测试 ID `demo-1`。测试覆盖正常调用、缺字段、空白、错误类型、额外 owner 字段、超长 query、路径式 ID、未知工具和无效调用形状。普通问候的真实请求没有产生工具调用。

**人工复核发现的失败：** search 工具只返回来源 ID，模型后续摘要却列举了观察中不存在的研究方法类别。Prompt 中已有“不要编造”，仍未阻止这次补述。原始输出完整保留，人工支持检查标记 failed，`research_answer_accepted` 为 false。

因此工具选择、参数合法、执行成功、返回非空文字是接口层验收；结论是否由证据支持需要另外验证。今天没有建设完整的 MedEvidence 答案发布流程，该输出没有作为医疗研究答案发布。D12 必须实现证据绑定和不支持结论的删除或拒答，D13/D14 必须用真实资料评测；如果小模型仍达不到要求，需要基于失败证据调整模型或流程。

模型循环也必须有终止条件。本实验只执行一次工具，再请求最终回复；额外工具调用被拒绝。两个工具返回值均有 `synthetic: true`，避免和真实资料混淆。真实业务后续需要服务端身份、owner 过滤、只读权限和总预算，今天不宣称它们已经完成。

## 6. Embedding：固定模型、维度和处理方式

Embedding 使用 `nomic-embed-text:v1.5`，运行时为 137M、F16，实际返回 768 维。digest 为：

```text
0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f
```

输入为两段意思接近的 FastAPI 英文描述和一段香蕉描述。三段都加 `search_document:` 前缀，保持处理一致。实际结果：同主题余弦相似度约 0.946854，无关文本约 0.449339。

程序检查返回数量、非空维度、维度一致、数值有限和非零向量。Python bool 是 int 的子类，校验显式排除布尔值。空输入、维度不一致、零向量、NaN、布尔元素等有自动化检查。

这三段文本只确认向量服务及计算可运行，不证明医学召回率。后续检索时应按模型要求区分 query/document 前缀，保存模型、维度、处理版本和索引版本；更换模型或维度后需要重新生成对应向量。余弦相似度衡量方向接近程度，不能表示结论正确或证据充分。

## 7. 真实 PDF：内容、日期、定位和许可分别检查

主题确定为系统综述报告中的方法定位和 PRISMA 版本差异。资料和演示查询用英文，学习说明用中文。两份材料均来自 PLOS 官方出版页，页面明确标示 Creative Commons Attribution 许可，并记录作者与来源。

| 材料 | 实际发表时间 | PDF 大小与页数 | 提取字符数 | 人工核对定位 |
|---|---|---|---:|---|
| PRISMA 2009 | 2009-07-21 | 159810 字节，6 页 | 36324 | 两个片段均在物理第 2 页 |
| PRISMA 2020 | 2021-03-29 | 825448 字节，15 页 | 61579 | 方法标题在第 3 页，使用限制在第 5 页 |

版本名“2020”和发表年份“2021”是不同字段。2009 文献标记为历史版本，不能把版本演进直接写成临床证据冲突。研究开发日期也保存在元数据：例如 2020 版本的 2018 年讨论会及 2019–2020 年修订；两份资料均不是临床试验。

BMJ 的原下载地址返回 403。我没有绕过访问控制，改用同一 PRISMA 2020 论文的 PLOS 官方开放版本，记录新的 DOI 和 URL。两份文件下载到 `.cache/day1/`，没有改动 `sources/`。

提取保持每页独立，物理页码从 1 开始。空页仍保留页码，因此后续定位不会因跳过空页而偏移。程序记录 SHA-256、字节数、页数、字符数、来源元数据和解析版本。哈希用于识别文件内容变化，不证明论文可信或许可有效。

使用 pypdf 6.20.0 的 fonts 可选依赖，fontTools 4.66.1 用于这两份 PDF 的 CFF 字体编码。最初缺少字体依赖，出现解析警告；补依赖后重跑通过。第一次定位实验也发现预设锚点措辞与原文不一致，我查看提取内容和原文页面，修正锚点，而非修改原文或随意降低检索条件。

从 bundled runtime 使用 pypdfium2 渲染原文，核对了 2009 版第 1、2 页以及 2020 版第 1、3、5 页，检查来源、发表信息、标题、文本片段与页码。自动检索锚点只是辅助，视觉核对不能省略。

当前限制是 5 MiB、50 页、20 万提取字符；拒绝加密文件、损坏文件及没有可提取文字的文件。合成测试覆盖空页、图片型页、损坏、加密、超大文件、页数上限和文本量上限。合成测试 PDF 不计入真实资料数量。

后续摄取还需要独立 worker、解析执行超时和进程级资源限制。文件大小和字符上限不能完全阻止 PDF 解压或解析阶段过量耗资源。今天两份公开、人工核对的文件不是生产级恶意上传安全测试。pypdf 也不提供 OCR；扫描件不能因请求成功就标记为摄取成功。

这两份是初始方法资料；最终 10–15 份语料和包含无答案、跨文档、条件差异等类别的评测集仍按 D2–D8 建立，不能用今天两份替代最终数据验收。

## 8. 缓存、依赖、复现和清理

你要求统一管理额外缓存。我将可删除的开发数据集中到 `.cache/`，用 `scripts/dev.sh` 设置工具路径，并维护 `.cache/manifest.json`。整个目录已被 `.gitignore` 忽略，根目录权限为 0700；manifest 自然也被忽略。缓存里包含本地 VM 的临时 SSH 私钥，不应发送、展示或提交。

| 子路径 | 内容 |
|---|---|
| `.cache/uv/` | Python 包下载缓存 |
| `.cache/python/` | Python 字节码 |
| `.cache/pytest/` | 测试缓存及临时 fixture |
| `.cache/tmp/` | 常规临时文件 |
| `.cache/homebrew/` | 软件包下载和后续安装日志 |
| `.cache/xdg/` | 运行时缓存，包括 Colima 镜像下载 |
| `.cache/colima/`、`.cache/lima/` | 虚拟机配置、数据、日志和 socket |
| `.cache/docker/` | 此项目 Docker context 与 CLI 配置 |
| `.cache/ollama/models/` | 本地模型文件 |
| `.cache/day1/` | PDF、渲染页、逐页文本、过程日志和原始报告 |

缓存 wrapper 先创建 Colima 配置目录。该版本检测指定目录是否存在，不存在时会回退到用户目录；初次启动暴露了这个行为。我停止本次 VM，确认用户目录中的配置确实是本次刚创建的，迁入统一缓存，再启动验证。后续取消默认宿主目录挂载与额外 SSH 配置写入，并撤回本次自动增加的旧 SSH Include。缓存目录创建与已有 Docker context 不被覆盖都有回归检查。

wrapper 不覆盖现有 Docker 配置，只在缺失时创建；manifest 先写临时文件再替换，避免并发读取半份 JSON。每次执行工具都应通过 wrapper，否则工具可能回到系统默认缓存路径。运行时可能使用操作系统临时 IPC 路径，服务停止后释放；Homebrew 安装目录及 Ollama 本机身份配置属于软件自身持久配置，不冒充项目缓存。仓库原有 `app/__pycache__/main.cpython-312.pyc` 创建于 2026-08-25，未移动或删除；本次新增 Python 字节码统一写入 `.cache/python/`。

依赖方面新增 pypdf 的 fonts 支持和 pytest；现有 HTTPX 作为直接依赖显式声明。没有安装 Ollama Python SDK，REST 调用继续使用已存在的 HTTPX。`pyproject.toml` 和 `uv.lock` 不再被忽略，因为本次增加的依赖必须能够复现；虚拟环境仍被忽略。其他原有 ignore 规则及暂存内容保留。

在项目根目录安装锁定环境：

```bash
bash scripts/dev.sh uv sync --locked --python 3.12.14
```

模型服务在一个独立终端前台启动：

```bash
bash scripts/dev.sh ollama serve
```

另一个终端准备运行时、镜像和模型；本次文件已经保存在缓存，重跑无需重新拉取全部内容：

```bash
bash scripts/dev.sh colima start --cpu 2 --memory 2 --disk 10 --vm-type vz --mount none --ssh-config=false
bash scripts/dev.sh docker pull alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1
bash scripts/dev.sh ollama pull qwen3:1.7b
bash scripts/dev.sh ollama pull nomic-embed-text:v1.5
```

重新下载 PDF（仅在缓存文件缺失时需要；命令使用公开出版地址）：

```bash
bash scripts/dev.sh curl --fail --location --max-time 60 --output .cache/day1/prisma-2009.pdf 'https://journals.plos.org/plosmedicine/article/file?id=10.1371/journal.pmed.1000097&type=printable'
bash scripts/dev.sh curl --fail --location --max-time 60 --output .cache/day1/prisma-2020.pdf 'https://journals.plos.org/plosmedicine/article/file?id=10.1371/journal.pmed.1003583&type=printable'
```

执行验证：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day1 health
bash scripts/dev.sh .venv/bin/python -m scripts.day1 container
bash scripts/dev.sh .venv/bin/python -m scripts.day1 models
bash scripts/dev.sh .venv/bin/python -m scripts.day1 pdf
bash scripts/dev.sh env TRACEAGENT_CONTAINER_TESTS=1 .venv/bin/python -m pytest -q --basetemp=.cache/pytest/tmp --junitxml=.cache/day1/pytest.xml
bash scripts/dev.sh uv lock --check --offline
git diff --check
```

以上实验和检查已经执行。最后完整测试为 42 passed，0 skipped；锁定环境同步实际使用了 `uv sync --locked --offline --python 3.12.14`，确认已有缓存足以复现。未指定真实容器开关的运行会明确跳过该检查。CLI 的 `passed` 描述接口实验，不表示模型自然语言通过人工证据支持检查，人工结果在永久记录中单独保存。

逐次实验会更新 `.cache/day1/` 下相应报告；本次实际结果另外保存到版本化的 `docs/day1-results.json`，重跑不会自动覆盖它。重跑后应核对新结果再生成新的版本记录，避免把旧结果当成新结果。

交付时已经停止本次启动的 Ollama 服务和 Colima VM，API 实验进程也已自动结束；缓存和模型仍保留，重跑前按上面的命令启动。以后清理之前先在模型终端 Ctrl+C，再运行 `bash scripts/dev.sh colima stop`。不要删除运行中的 VM 文件。删除缓存会同时删除本地模型、测试镜像所在的虚拟机及 PDF，需要重新准备。没有对 GitHub 推送、创建 PR 或公开部署。

## 9. 2026-10-10 的实现方式核查

| 当前选择 | 比较与依据 | 决定 |
|---|---|---|
| Python 3.12.14、FastAPI 0.141.1、Pydantic 2.13.4 | 现有环境已可启动，接口很小；当天升级框架没有明确收益 | 保留现有版本并锁定 |
| HTTPX 0.28.1 | Starlette 1.6.0 对旧 TestClient 使用 HTTPX 给出弃用提示；原生 ASGITransport 支持当前检查，避免新增 httpx2 | 采用最小实验并实际验证；不忽略警告 |
| Ollama 0.40.2、Qwen3 小模型 | 官方 REST 工具调用示例支持该方式；本机内存可运行，云费用为零；实际发现摘要支持风险 | 采用接口实验，研究质量留待真实评测决定 |
| Nomic Embed v1.5 | 本地英文向量接口已实测；三条语句没有统计意义 | 采用最小实验，暂不宣称医学检索质量 |
| Colima 0.10.3/VZ | 项目只需要单机容器；CLI 配置可控，实际启动与隔离检查通过 | 采用，固定镜像 digest，不扩展编排 |
| pypdf 6.20.0/fonts | 两份文本型资料需要字体编码支持；OCR 和复杂表格解析超出本次范围 | 采用必要可选依赖，记录限制 |

官方依据：[FastAPI 首步](https://fastapi.tiangolo.com/tutorial/first-steps/)、[HTTPX Transport 与 lifespan](https://www.python-httpx.org/advanced/transports/)、[Ollama 工具调用](https://docs.ollama.com/capabilities/tool-calling)、[Ollama Embedding API](https://docs.ollama.com/api/embed)、[Ollama 版本发布](https://github.com/ollama/ollama/releases/tag/v0.40.2)、[Nomic 模型](https://ollama.com/library/nomic-embed-text)、[Colima FAQ 与目录设置](https://github.com/abiosoft/colima/blob/main/docs/FAQ.md)、[pypdf 提取边界](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[uv 缓存](https://docs.astral.sh/uv/concepts/cache/)。选择依据是当前兼容性、必要性及实际实验，没有用新颖程度替代项目收益，也没有根据未执行的比较宣称某方案性能更好。

## 10. 你自己开发时的责任与面试准备

今天对应 AI Coding 的要求分析、任务拆分、AI 协作、代码审查、故障定位、测试设计和可复现交付。对应开发能力是 Python/HTTP 基础、真实模型调用、结构化工具参数、向量服务、来源追溯及失败与资源限制。这些是准备优先级，不是对任何公司的笔试题承诺。

AI 可以起草工具 Schema、脚本、测试和文档；你必须独立判断接口契约，核对实际命令，检查 mock 是否冒充真实调用，审查秘密和权限路径，并逐条核对模型结论。模型返回来源 ID 后仍然可能编造内容，今天已经有实际例子。

审查时我检查了五个方面：正确性（保持 health 契约及页码）；错误处理（受控错误、截止时间和预算）；测试（成功、边界、失败及真实集成）；安全（只读白名单、参数类型、localhost、缓存秘密不进入 Git）；可维护性（实验与 API 分离、锁文件、统一缓存与复现说明）。

建议你按顺序阅读 `app/main.py` → `scripts/day1.py` → `app/experiments/day1.py` → `tests/test_day1.py`。先指出每一步的输入与输出，再找每个失败分支，最后说明哪个测试能发现它。看懂代码之后亲自重跑 health、容器和模型实验，并对照永久记录；不要只记住命令或让 AI 替你宣布通过。

今天仍未完成的后续能力包括数据库、事务、owner 隔离、RQ/worker、完整检索与引用验证、医学拒答、正式评测和部署。这是后续排期内容，不能把 Day 1 的接口实验当成这些验收。你是否能够独立解释本日设计，仍需要通过下面问题和实际演示确认。

1. 工具调用的参数合法，为什么仍然不能证明最终答案有依据？
2. 超时或空文本应该怎样终止，为什么失败请求也要消耗预算？
3. 哪些检查使用了 mock，哪些用了真实服务，它们分别能证明什么？
