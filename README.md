# TraceAgent

TraceAgent 是用于可追溯资料研究的 Python 后端项目，MedEvidence 是首个证据检索场景。当前完成 Day 2 的配置、分层实验、接口定稿、本地质量检查与首批语料准备，公开 API 保留 `GET /health -> {"status":"ok"}`。数据库、上传、研究工作流与医学拒答尚未实现。

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
