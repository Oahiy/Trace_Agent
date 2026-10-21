# TraceAgent

## Day 12：证据质量评测与安全回归

已交付六题开发集的真实 PDF/HTTP/独立 worker/固定模型评测、引用/条件/状态自动检查、绑定实际观察的代理语义复核和质量退出码。新增 51 项测试；完整真实依赖验证 762 passed、0 skipped。修复 PDF 换行绕过已有注入检查的问题，安全比较不改原文，工作流为 `med-extractive-v3`。

**质量门槛未通过**：六题两题通过本次检查、四题失败；五道应答题只有一道完整有效。来源级 Recall@5/MRR@5 均为 0.8，不能替代相关性与完整性判断。开发者本人复核、第三条真实冲突和长文全量索引缺口继续保留。

沿用下方环境准备后串行执行，最后评测以保留隔离演示状态：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day12 test
bash scripts/dev.sh .venv/bin/python -m scripts.day6 test --report-label local
bash scripts/check.sh
bash scripts/dev.sh .venv/bin/python -m scripts.day12 evaluate
# 逐题复核并记录匹配观察哈希后；本次实际退出 2，保留质量失败。
bash scripts/dev.sh .venv/bin/python -m scripts.day12 review --require-quality
```

新运行观察变化后，旧复核应被拒绝，不能自动改哈希取得通过。完整步骤、逐题根因、独立开发注意事项及风险见 [Day 12 讲解](docs/day12-walkthrough.md)，实际证据见 [Day 12 结果](docs/day12-results.json)。唯一被忽略的 `.cache/manifest.json` 管理 `.cache/day12/`；原应用数据、上传、凭据和卷保留。

## Day 11：异步研究闭环

已接入经过 API Key 认证的 `POST /research` 与 `GET /research/{job_id}`，由独立 RQ worker 执行检索、上下文选择、结构化句子选择与服务端原文引用绑定。研究输入、执行权、预算与结果持久化；重复请求复用，越权 404，证据不足/医学越界拒答，模型/预算/超时失败返回安全状态。现有上传与文档状态契约保持。

结果目前为研究方法学的单段摘录摘要，完整支持性、冲突识别与医学安全仍待后续验收；不把这一闭环描述成已通过全语料或临床使用门槛。任务不会自动索引尚未 READY 的资料。

复现先沿用 Day 10 的环境启动命令，执行迁移至 0005 和 Redis 启动，然后串行运行：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day11 test
bash scripts/dev.sh .venv/bin/python -m scripts.day6 test --report-label local
bash scripts/check.sh
bash scripts/dev.sh .venv/bin/python -m scripts.day11 demo
```

真实 demo 自动管理自己启动的临时模型服务、API 和 worker，使用 traceagent_test/Redis DB 1；测试会清理隔离数据，保留演示状态时最后运行 demo。详细设计、故障处理、缓存、测试层次、学习路线和剩余风险见 [Day 11 讲解](docs/day11-walkthrough.md)，实际记录见 [Day 11 结果](docs/day11-results.json)。唯一缓存清单仍为被忽略的 `.cache/manifest.json`，本日缓存为 `.cache/day11/`。

## 历史交付记录

Day 10 已完成锁定公开 PDF 的向量入库与可追溯精确检索：74 个片段、版本化 768 维向量、持久执行权/预算、原子 READY 与摄取父任务收尾，以及用户、年份和索引条件过滤。真实检索命中第 5 页预期证据；完整测试与运行证据见 [Day 10 记录](docs/day10-results.json)，设计、逐层解释和独立开发注意事项见 [Day 10 讲解](docs/day10-walkthrough.md)。索引目前为本地显式触发，队列仍仅执行解析；没有研究回答、全语料质量或公开部署验收。

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day3 migrate
bash scripts/dev.sh .venv/bin/python -m scripts.day10 test
# 先在另一个终端运行 bash scripts/dev.sh ollama serve。
bash scripts/dev.sh .venv/bin/python -m scripts.day10 demo
```

以上依赖已有 PostgreSQL 环境及锁定 PDF，首次准备见讲解。数据库测试与 demo 串行执行，最后运行 demo 可保留其隔离库展示状态。新增缓存沿用唯一 `.cache/manifest.json`，清单与 `.cache/day10/` 明确忽略；应用数据、上传文件和凭据继续随卷保留。Day 8 人工冻结缺口仍未关闭。

Day 9 已交付可复用的本机生成/Embedding 适配器、严格结构输出、固定模型 digest 与 768 维校验、共享截止、一次有限重试及独立用量预算。108 项新增测试通过；完整基础检查为 467 passed、111 skipped，跳过项未计为通过。真实模型完成两次生成和两次 Embedding 请求，输入估算与供应商实际统计分开保存。没有新依赖、公开 API 或数据库改动；预算目前为进程内对象，RAG/医学质量和持久化任务预算尚未验收。逐层解释、独立开发注意事项、官方依据和复现见 [Day 9 讲解](docs/day9-walkthrough.md)，实际证据见 [验证记录](docs/day9-results.json)。

```bash
bash scripts/dev.sh .venv/bin/python -m pytest tests/test_day9.py -q --basetemp=.cache/pytest/day9-tmp
bash scripts/check.sh
# 另一个终端先运行 bash scripts/dev.sh ollama serve，使用已有固定模型与锁定 PDF。
bash scripts/dev.sh .venv/bin/python -m scripts.day9 demo
```

Day 9 日志、演示和测试产物纳入唯一 `.cache/manifest.json` 的 `.cache/day9/` 条目；清单及该目录均明确忽略。不要删除随原数据库/Redis 卷保留的凭据和上传文件。Day 8 的人工复核及冲突用例缺口继续保留，文档仍未因适配器完成而改为 READY。

Day 8 已准备 10 份真实英文资料与 30 条固定评测标注（10 开发、20 验收），沿用现有解析器得到 151 页、779 个片段，55 个来源/评测锚点核验通过。新增离线检查与可重复准备命令，不新增依赖或公开接口。**完整人工冻结尚未通过**：开发者本人尚未复核，三条冲突候选中只有两条真实报告不一致，另一条是研究条件差异；严格门槛会明确失败。完整说明、独立开发注意事项与实际证据见 [Day 8 讲解](docs/day8-walkthrough.md)、[验证记录](docs/day8-results.json)。本日没有运行模型或全量数据库 seed。

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day8 check
bash scripts/dev.sh .venv/bin/python -m scripts.day8 prepare
bash scripts/dev.sh .venv/bin/python -m scripts.day8 prepare
bash scripts/check.sh
```

缺少锁定 PDF 时运行 `scripts.day8 prepare --download`（通过相同 dev 包装器）；已有损坏文件明确失败，不自动覆盖。`scripts.day8 check --require-acceptance` 当前预期非零退出，不能作为已通过的完整冻结验收。准备产物属于唯一 `.cache/manifest.json` 管理的 `.cache/day8/`，两者均明确忽略；它是离线语料快照，未绕过每用户五任务上限或将 PARSED 改为 READY。

Day 7 已完成本地后端摄取阶段验收：417 项完整测试通过、0 项跳过；真实 HTTP/Redis/RQ/PostgreSQL 链路、中文 TXT、公开 PDF 持久定位、重放、权限、配额和实际停机恢复均已验证。公开 PDF 得到 74 个片段，合成 TXT 得到 3 个片段；原应用表、8 个上传文件及凭据指纹前后保持一致。完整说明见 [Day 7 验收与独立开发讲解](docs/day7-walkthrough.md)，实际证据见 [Day 7 验证记录](docs/day7-results.json)。成功仍为 PARSED/解析阶段 SUCCEEDED，完整 ingest 父任务保持 PENDING；尚无 Embedding、完整 RAG、医学质量或生产发布验收。

依赖准备与完整顺序见 Day 7 讲解。核心验收依次为 `scripts.day6 test` → `scripts.day6 demo` → `scripts.day7` → `scripts.day6 offline-check` → `scripts.day6 audit`，均经 `bash scripts/dev.sh .venv/bin/python -m` 运行，不能并行。Day 7 的预览、报告与测试上传纳入唯一 `.cache/manifest.json`，清单及所管理目录明确忽略；应用文件和凭据仍须随数据卷保留。开发者本人的独立讲解能力需要通过文末自测亲自验证。

TraceAgent 是用于可追溯资料研究的 Python 后端项目，MedEvidence 是首个证据检索场景。Day 6 交付了真实 Redis/RQ 后台解析、独立阶段记录、投递恢复、截止与进程清理，以及 `GET /ready`。解析成功为 PARSED、阶段 SUCCEEDED；完整 ingest 父任务仍为 PENDING，Embedding 完成前不返回 READY。完整讲解见 [Day 6 完成记录与开发讲解](docs/day6-walkthrough.md)，实际结果见 [验证记录](docs/day6-results.json)。

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day6 prepare
bash scripts/dev.sh .venv/bin/python -m scripts.day3 up
bash scripts/dev.sh .venv/bin/python -m scripts.day3 migrate
bash scripts/dev.sh .venv/bin/python -m scripts.day6 up
bash scripts/dev.sh .venv/bin/python -m scripts.day6 test
bash scripts/dev.sh .venv/bin/python -m scripts.day6 demo
bash scripts/dev.sh .venv/bin/python -m scripts.day6 offline-check
bash scripts/dev.sh .venv/bin/python -m scripts.day6 audit
bash scripts/check.sh
```

上述命令需要先安装锁定依赖并启动现有 Colima，完整首次准备与手动展示步骤见 Day 6 讲解。测试、demo、offline-check 使用同一隔离测试库，必须串行；停机检查会短暂停止项目依赖。新增缓存、凭据、测试环境和 Redis 卷登记于唯一 `.cache/manifest.json`，清单及其管理目录已忽略。保留 Redis 卷时保留 `.cache/day6/queue.env`；原应用上传文件、数据库和 API Key 凭据继续与数据库卷一起保留。reconcile 当前为显式命令，没有周期调度或失败阶段自动重试。验证记录保留运行时状态；后续 Git 提交和推送以仓库历史为准。尚未公开部署。

Day 5 完成了有截止时间的子进程解析、TXT/PDF 定位、确定性切分、片段原子保存与幂等重放。上传后为 UPLOADED/PENDING，本地解析成功后为 PARSED，Embedding 完成前不返回 READY。研究工作流与医学拒答按后续阶段实现。

Day 5 完整设计、失败处理、逐层讲解、独立开发注意事项和复现见 [完成记录与开发讲解](docs/day5-walkthrough.md)，实际证据见 [验证记录](docs/day5-results.json)。使用 pypdf 6.20.1 安全补丁；升级前后三份资料逐页文本哈希一致。三份 PDF 共 328 个片段；合成中文 TXT 另有 4 个片段。真实资料与合成文件验证不代表模型回答或医学质量验收。

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

公开 PDF 缓存缺失时先执行 `scripts.verify_sources --download`（通过相同 dev 包装器）。`day5 demo` 启动临时本机 API，演示后关闭；失败 HTTP 用例只针对隔离 `traceagent_test`，不删除应用中的历史记录或绕过五任务上限。不要同时运行两份数据库测试或与 demo 并行使用测试库。Day 5 新缓存、测试环境和 PDF 预览均登记在唯一 `.cache/manifest.json`，清单与所管理路径均被 Git 忽略；应用上传文件与凭据仍须随数据库卷保留。macOS 未能启用子进程地址空间上限，当前只适用于公开/合成资料的本地开发验证，不构成生产恶意文件隔离保证。停止服务使用 `day3 down` 和 `colima stop`，保留卷。验证记录保留运行时状态；后续 Git 提交和推送以仓库历史为准。尚未公开部署。

Day 4 完整解释见 [完成记录与开发讲解](docs/day4-walkthrough.md)，实际证据见 [验证记录](docs/day4-results.json)。在项目根目录复现：

```bash
bash scripts/dev.sh uv sync --locked --python 3.12.14 --dev
bash scripts/dev.sh .venv/bin/python -m scripts.day3 prepare
bash scripts/dev.sh .venv/bin/python -m scripts.day4 prepare
bash scripts/dev.sh colima start --cpu 2 --memory 2 --disk 10 --vm-type vz --mount "$PWD/.cache/day4/uploads:w" --ssh-config=false
bash scripts/dev.sh .venv/bin/python -m scripts.day3 up
bash scripts/dev.sh .venv/bin/python -m scripts.day3 migrate
bash scripts/dev.sh .venv/bin/python -m scripts.day4 seed
bash scripts/dev.sh .venv/bin/python -m scripts.day4 demo
bash scripts/dev.sh .venv/bin/python -m scripts.day4 shared-check
bash scripts/dev.sh .venv/bin/python -m scripts.day4 audit
bash scripts/dev.sh .venv/bin/python -m scripts.day4 test
bash scripts/check.sh
```

`demo` 自动启动临时本机 API，完成 TXT/PDF、权限、去重、失败、跨进程读取和 API 重启验证后关闭 API。端口 8000 已被占用时拒绝运行，不接管已有服务。手动展示用 `bash scripts/dev.sh .venv/bin/python -m scripts.day4 serve`，打开本机 `/docs`，Key 从受保护的本地凭据文件读取，勿粘贴到聊天或日志。`offline-check` 会短暂停止本项目数据库、验证业务 503 和 health 200、恢复数据库并重启临时 API验证；不要在其他操作使用该数据库时运行。

所有新增运行文件统一登记于被 Git 忽略的 `.cache/manifest.json`。上传文件位于 `.cache/day4/uploads/`，原始 API Key 位于 0600 的 `.cache/day4/auth.env`；数据库仅保存摘要。保留数据库卷时也须保留对应上传文件和两份凭据，不能把整个 `.cache/` 当作可随意删除的临时目录。只把 uploads 目录挂入 Colima，不挂整个项目或凭据目录；Day 4 共享检查不能沿用历史 Day 3 的 `--mount none`。停止时用 `day3 down` 和 `colima stop`，不加删卷选项。本地验证没有 Git 提交、推送或公开部署。

Day 3 完整解释见 [完成记录与开发讲解](docs/day3-walkthrough.md)，实际证据见 [验证记录](docs/day3-results.json)。本地演示不会推送 GitHub。

在项目根目录复现 Day 3：

```bash
bash scripts/dev.sh uv sync --locked --python 3.12.14 --dev
bash scripts/dev.sh colima start --cpu 2 --memory 2 --disk 10 --vm-type vz --mount none --ssh-config=false
bash scripts/dev.sh .venv/bin/python -m scripts.day3 prepare
bash scripts/dev.sh .venv/bin/python -m scripts.day3 up
bash scripts/dev.sh .venv/bin/python -m scripts.day3 migrate
bash scripts/dev.sh .venv/bin/python -m scripts.day3 demo
bash scripts/dev.sh .venv/bin/python -m scripts.day3 test
bash scripts/check.sh
```

`prepare` 只在凭据文件不存在时生成随机密码，文件权限为 0600；已有数据库卷保留时不要删除或重新生成该文件。`up` 会拉取固定 digest 的镜像，首次运行需要网络。`migrate` 使用开发迁移账号，应用演示使用没有建表权限的账号。真实数据库测试仅针对 `traceagent_test`，会清理测试数据与降级测试表；不针对应用库。普通 `check.sh` 明确关闭真实数据库/容器检查，真实检查使用 `day3 test`。远端 GitHub Actions 尚未执行。

检查真实停机失败、恢复后探测，并停止服务：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day3 offline-check
bash scripts/dev.sh .venv/bin/python -m scripts.day3 probe
bash scripts/dev.sh .venv/bin/python -m scripts.day3 down
bash scripts/dev.sh colima stop
```

`offline-check` 停止本项目数据库，验证固定错误及 5 秒以内的本机停机失败，然后恢复服务。它不等于任意网络故障都能在同一时间内结束。`down` 保留名为 `traceagent-day3-postgres` 的数据卷。缓存和凭据统一登记于 `.cache/manifest.json`，清单本身与 Day 3 缓存均被 Git 忽略；数据库卷位于清单登记的 Colima VM 中。详细清理与数据保留规则见 Day 3 讲解。

Day 2 的完整解释见 [完成记录与开发讲解](docs/day2-walkthrough.md)，实际证据见 [验证记录](docs/day2-results.json)，后续接口见 [API 契约 v1](docs/api-contract-v1.md)，三份真实资料见 [来源清单](docs/day2-sources.json)，七条待人工核对的问题草案见 [评测草案](evals/med/day2-draft.json)。

Day 2 日常开发与复现：

```bash
bash scripts/dev.sh uv sync --locked --python 3.12.14 --dev
bash scripts/check.sh
bash scripts/dev.sh .venv/bin/python -m scripts.day2
bash scripts/dev.sh .venv/bin/python -m scripts.verify_sources
```

缓存缺失时，恢复已锁定的公开 PDF 并重新验证：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.verify_sources --download
```

API 启动时校验环境配置。例如临时使用更短的模型超时：

```bash
bash scripts/dev.sh env TRACEAGENT_MODEL_TIMEOUT_SECONDS=20 .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`.env.example` 只有空值模板；应用不自动加载 `.env`。配置默认模型地址为本机 `127.0.0.1`，上限可以收紧，不能超过约定的文件/问题限制。今天的配置不会覆盖 Day 1 独立模型实验的固定参数。基础 CI 在 `.github/workflows/ci.yml`，与本地共用 `scripts/check.sh`；不依赖模型或容器。隔离环境中的相同检查已运行；历史验证报告保存生成时的状态，远端 Actions 状态以 GitHub 实际运行记录为准。

完整过程、输入输出、失败检查、知识解释和开发注意事项见 [Day 1 完成与开发讲解](docs/day1-walkthrough.md)。实际结果见 [验证记录](docs/day1-results.json)，资料来源见 [来源清单](docs/day1-sources.json)。

在项目根目录安装锁定依赖：

```bash
bash scripts/dev.sh uv sync --locked --python 3.12.14
```

启动 API：

```bash
bash scripts/dev.sh .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

本地模型服务在另一个终端启动：

```bash
bash scripts/dev.sh ollama serve
```

首次准备模型和容器（需要已安装的 Ollama、Colima、Docker CLI、Compose）：

```bash
bash scripts/dev.sh ollama pull qwen3:1.7b
bash scripts/dev.sh ollama pull nomic-embed-text:v1.5
bash scripts/dev.sh colima start --cpu 2 --memory 2 --disk 10 --vm-type vz --mount none --ssh-config=false
bash scripts/dev.sh docker pull alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1
```

重跑已经执行过的验证：

```bash
bash scripts/dev.sh .venv/bin/python -m scripts.day1 health
bash scripts/dev.sh .venv/bin/python -m scripts.day1 container
bash scripts/dev.sh .venv/bin/python -m scripts.day1 models
bash scripts/dev.sh .venv/bin/python -m scripts.day1 pdf
bash scripts/dev.sh env TRACEAGENT_CONTAINER_TESTS=1 .venv/bin/python -m pytest -q --basetemp=.cache/pytest/tmp
```

PDF 检查需要来源清单中的两份缓存文件，重新下载方法见讲解文档。模型同名标签可能更新，重跑前比较验证记录中的完整 digest。未设置 `TRACEAGENT_CONTAINER_TESTS=1` 时，真实 Docker 测试会明确跳过，其他测试仍可执行。

`scripts/dev.sh` 统一设置缓存路径，并维护唯一清单 `.cache/manifest.json`，包括 Ruff、uv 管理的 Python、Day 2 下载/日志/格式前快照/隔离环境。`.cache/` 和清单文件均写入 `.gitignore`；不要提交模型、VM、临时日志、SSH 临时密钥或 `.env`。停止模型服务（所在终端 Ctrl+C）与 `bash scripts/dev.sh colima stop` 后再清理缓存；缓存里包含本地虚拟机数据，删除会丢失其中的测试环境。`docs/` 中的验证摘要属于交付证据，应保留。

工具调用验证通过，但一次工具后续摘要出现未获工具结果支持的补述，已在记录中标记为不接受的研究答案。今天的结果不代表完整 RAG、医学质量或发布验收通过。本次启动的 API、模型服务和 Colima VM 已停止，模型与缓存保留供复现。尚未部署公开服务。
