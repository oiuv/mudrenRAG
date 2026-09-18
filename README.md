# mudrenRAG

为 Dify 提供 mud.ren 论坛内容检索的外部知识库 API。同步脚本从 MySQL 生成向量与关键词索引；检索服务默认使用 DashScope `qwen3.7-text-embedding-flash` + FAISS 向量召回，以及本地 BM25 关键词召回，通过 RRF 融合并去重，从论坛 API 获取正文后，使用 `qwen3.7-text-rerank` 重排。回答生成由 Dify 完成。

## 功能

- `POST /retrieval`：Bearer 鉴权、知识库 ID 校验、top_k、重排得分阈值、元数据筛选。
- 向量与重排模型、接口地址均可通过 `.env` 配置；默认开启向量 + BM25 混合检索和模型重排，每路召回窗口及重排候选上限均为 20，top_k 更大时随之扩大；实际数量受索引大小、关键词命中及筛选结果限制，最终返回不超过 top_k 条。
- BM25 使用 BM25L 变体和 jieba 中文分词；保留英文、函数名及文件路径，并拆分 snake_case / camelCase，支持术语与代码关键词检索。关键词索引覆盖完整标题和正文。
- 增量向量化：扫描当前可见帖子，复用内容未变的向量，只为新增和修改的帖子调用向量 API；删除、封禁和恢复可见的帖子都会在下次同步体现。
- 同步失败时保留旧版本。向量、BM25 分词数据与帖子映射保存在单个原子替换的快照中，保持两路检索对应同一批内容。
- 服务在后续请求中自动检查并加载新快照，无需重启。加载失败时继续使用上一份有效快照并记录错误。
- 异步向量请求、正文连接复用、全局受限并发和超时控制。
- 正文获取失败且无法返回结果时响应 HTTP 502；没有匹配结果时返回 HTTP 200 和 `{"records":[]}`。
- `GET /health` 返回索引数量及状态，不调用 MySQL、DashScope 或论坛 API。

## 安装与启动

推荐 Python 3.12，支持 Python 3.10 及以上（依赖需有对应平台的安装包）。Windows 可使用下文的 `start_server.bat` 自动准备环境、同步数据并启动服务；以下命令适用于手动安装或 Linux/macOS：

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
```

首次部署复制 `.env.example` 为 `.env`；已有部署按示例补充配置项，保留自己的密钥和数据库设置。进程环境变量优先于 `.env`，布尔开关接受 `true/false` 或 `1/0`。配置如下：

| 配置 | 说明 |
| --- | --- |
| HOST | API 监听地址，默认 0.0.0.0；仅本机访问时设置 127.0.0.1，支持 IPv6 |
| PORT | API 监听端口，默认 8008，范围 1–65535 |
| DIFY_API_KEY | Dify 请求此服务使用的密钥 |
| DASHSCOPE_API_KEY | 阿里云 DashScope 密钥，向量和重排共用 |
| EMBEDDING_MODEL | 向量模型，默认 qwen3.7-text-embedding-flash |
| EMBEDDING_DIMENSION | 向量维度，默认 1024；默认模型支持 256、512、768、1024 |
| EMBEDDING_BASE_URL | OpenAI 兼容向量接口地址，默认 https://dashscope.aliyuncs.com/compatible-mode/v1 |
| RERANK_MODEL | 重排模型，默认 qwen3.7-text-rerank |
| RERANK_API_URL | DashScope 原生重排完整 URL，默认见 .env.example |
| RERANK_ENABLED | 默认 true；false 时按混合检索融合得分排序（同时关闭 BM25 则按向量距离） |
| RERANK_CANDIDATES | 重排候选上限，默认 20，范围 1–500；生效上限为 max(top_k, RERANK_CANDIDATES)，关闭重排时不使用 |
| RERANK_TIMEOUT | 重排请求的网络阶段超时秒数，默认 30，范围 (0, 120] |
| BM25_ENABLED | 默认 true；false 时关闭关键词召回，保留向量召回及独立的重排开关 |
| BM25_CANDIDATES | 关键词召回窗口，默认 20，范围 1–500；实际窗口计算见下文，关闭 BM25 时不使用 |
| VECTOR_CANDIDATES | 混合检索的向量召回窗口，默认 20，范围 1–500；关闭 BM25 时不使用 |
| RRF_K | RRF 排名平滑常数，默认 60，范围 1–1000；越大时两路共同命中的优势越明显 |
| KNOWLEDGE_ID | 与 Dify 配置的外部知识库 ID 一致，示例为 mud-ren-forum |
| DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME | 论坛 MySQL，只有同步脚本使用 |
| DATA_DIR | 数据目录，默认项目下 data，可使用绝对路径 |
| FORUM_API_BASE_URL | 正文接口地址，默认 https://api.mud.ren |
| FORUM_BASE_URL | 引用链接地址，默认 https://bbs.mud.ren |
| HTTP_TIMEOUT | 正文 HTTP 请求的网络阶段超时秒数，默认 15，范围 (0, 120] |
| FETCH_CONCURRENCY | 每个服务进程的正文请求并发上限，默认 5，范围 1–32 |
| INDEX_RELOAD_INTERVAL | 请求触发的索引变更检查间隔，默认 5 秒，范围 0–3600；0 表示每次检查，不是数据库同步间隔 |

没有元数据筛选时，候选目标数 N 在开启重排时为 `max(top_k, RERANK_CANDIDATES)`，关闭重排时为 `top_k`。混合检索两路窗口分别取 `max(N, VECTOR_CANDIDATES)` 和 `max(N, BM25_CANDIDATES)`；关闭 BM25 时只召回 N 个向量候选。窗口受帖子总数限制，关键词命中可能少于窗口大小；开启元数据筛选时扩展到整个索引，避免筛选结果被初始窗口截断。

已有部署如果没有设置 `KNOWLEDGE_ID`，继续兼容任意非空 ID，同时输出启动提示。配置后，错误的 ID 返回 HTTP 404 / error_code 2001。

手动同步和启动使用以下命令；后续仅更新数据时只执行第一条：

```bash
python scripts/sync_data.py
python -m app.server
```

`python -m app.server` 自动读取项目 `.env` 中的 `HOST`、`PORT`，缺省监听 `0.0.0.0:8008`；不会安装依赖或同步数据。直接使用 Uvicorn 命令行时，需自行传入 `--host` / `--port`，不会自动采用这两个项目配置项。

### Windows 自动启动

安装 Python 后，双击 `start_server.bat`，或在项目目录执行：

```powershell
.\start_server.bat
```

脚本依次完成：

1. 优先使用项目 `.venv`；不存在时选择 Python（优先 3.12）并创建虚拟环境。
2. 补齐 pip，按 `requirements.txt` 安装缺失或版本不符合要求的包，随后检查依赖兼容性。已满足要求的版本保留，不执行全量升级或强制重装。
3. 校验配置并运行一次普通增量同步，向量与 BM25 数据一起更新。
4. 同步成功后启动 Uvicorn，按 `.env` 的 `HOST`、`PORT` 监听（默认 `0.0.0.0:8008`）。

首次缺少 `.env` 且进程环境变量配置不完整时，会从中文注释的 `.env.example` 创建模板并停止；填写密钥与数据库配置后再次运行即可。已有 `.env` 保留。完全通过环境变量配置时无需创建 `.env`。数据库密码允许为空，但模板中的占位值需替换。

任何安装、配置校验或同步步骤失败都会停止启动并显示原因，不会使用旧索引继续启动新服务；同步失败时已发布快照仍保留。数据库为空时可生成空索引并正常启动。已有 `.venv` 损坏或 Python 版本过低时会报错，请修复该环境后重试。

双击运行结束后窗口会等待按键；从终端或自动化程序调用时可使用 `.\start_server.bat --no-pause` 保留退出码且不等待按键。`--help` 显示使用说明。启动脚本只在启动前同步一次，持续运行期间的定期更新仍需配置计划任务。

依赖处理遵循 [pip 默认保留满足要求的已安装版本](https://pip.pypa.io/en/stable/cli/pip_install/#overview) 的行为；下载缺失依赖时需要访问配置的 Python 包源。

### Dify 连接配置

在 Dify 中配置：

- API 端点：`http://<服务器IP>:<PORT>`，默认端口 8008，不附加 `/retrieval`。`0.0.0.0` 是监听地址，Dify 应填写能访问的实际服务器地址。
- API 密钥：`DIFY_API_KEY`。
- 外部知识库 ID：与 `KNOWLEDGE_ID` 一致。

## 同步、升级与恢复

交互式召回测试运行 `.venv\Scripts\python.exe scripts/query_knowledge.py`，自动读取 `.env` 密钥；使用 `--top-k 5 --score 0.5` 调整参数，`-q "中文问题"` 执行单次查询。

可直接照着操作的 Windows 任务计划、Linux cron、接口自测与错误码说明见 [同步与接口排查操作指南](docs/operations.md)。

新版使用 `data/knowledge.npz` 保存 FAISS 索引、BM25 分词数据及分词器版本、帖子映射、内容指纹和向量模型名称及维度。每次同步遍历数据库当前未删除、未封禁的帖子，数据库扫描量与帖子总量有关，但只为变更内容生成向量。同步进程使用文件锁，避免同时发布多个更新。若向量兼容接口把批量结果的序号全部返回为 0，脚本会丢弃无法确认对应关系的批量结果，逐条重试并在本轮后续同步中使用单条请求；首批会额外调用一次向量接口，确保帖子与向量不会错配。

升级到混合检索时，在项目虚拟环境中执行：

```bash
python -m pip install -r requirements.txt
python scripts/sync_data.py
```

同步成功后重启 API。普通同步会补齐旧快照的 BM25 数据；向量模型、维度和内容指纹一致时复用已有向量。启用 BM25 时缺少关键词索引会明确报错；如需暂时兼容旧索引，可设置 `BM25_ENABLED=false`。之后的数据更新仍使用相同同步命令，两路索引一起发布、一起热加载。

切换向量模型或维度后，运行普通同步即可自动重新生成全部向量；即使帖子内容没有变化，也不会复用不同模型的向量。切换模型或维度会触发全量向量调用；仅补齐 BM25 数据不会为可复用的帖子额外调用向量模型。API 只加载与当前配置匹配的索引；同步完成后重启服务，使新的模型配置生效。仅切换重排模型无需重建索引。

旧版双文件索引被识别为 text-embedding-v4；旧版快照保留其记录的模型名称。若要暂时继续使用旧索引，请配置 `EMBEDDING_MODEL=text-embedding-v4`、`EMBEDDING_DIMENSION=1024`，并在尚未补齐 BM25 数据时设置 `BM25_ENABLED=false`。新默认模型不能直接使用旧模型索引。旧版双文件缺少内容指纹，首次新版同步会重新生成向量，即使继续使用同一模型；发布成功后旧文件仍保留。

如需强制重新生成全部向量，执行：

```bash
python scripts/sync_data.py --full
```

全量重建成功前，已发布索引保持有效。任一批次失败或响应向量数量、维度异常，同步退出码为 1，不发布不完整数据；重跑会重新处理本次尚未发布的变更。普通同步遇到损坏的现有快照会明确报错，使用 `--full` 恢复。

项目没有内置定时同步器。`start_server.bat` 在每次启动 API 前自动扫描 MySQL 并同步一次；直接运行 Uvicorn 或执行检索请求不会触发数据库同步。服务持续运行期间，新增、修改和删除的帖子仍需手动运行同步脚本，或由 Windows 任务计划程序 / Linux cron 定期更新。定时任务应使用项目虚拟环境的 Python 和脚本绝对路径，工作目录设为项目目录；普通更新使用 `scripts/sync_data.py`，无需每次加 `--full`。`BM25_ENABLED` 只控制查询阶段，同步脚本始终生成两路索引，方便后续切换。

同步成功后，服务在下一次达到检查间隔的请求中加载新索引。新快照损坏时继续服务旧内存版本，`/health` 返回 `degraded`；该状态不代表外部接口健康。数据更新只需同步并等待热加载；修改 `.env`、模型配置或应用代码后需要重启 API。

## 请求与筛选

```json
{
  "knowledge_id": "mud-ren-forum",
  "query": "如何编写 MUD 技能？",
  "retrieval_setting": {
    "top_k": 3,
    "score_threshold": 0.0
  },
  "metadata_condition": {
    "logical_operator": "and",
    "conditions": [
      {"name": "thread_id", "comparison_operator": ">", "value": 100},
      {"name": "author", "comparison_operator": "in", "value": ["作者甲", "作者乙"]}
    ]
  }
}
```

- `top_k`：1–100 的整数；阈值：0–1；查询不能为空，最长 8192 个字符。
- 支持筛选的元数据：`thread_id`（数字）、`url`、`author`、`published_at`。
- `name` 为字符串；`value` 支持字符串、数字或字符串数组。无效参数返回 HTTP 422，使用顶层 `error_code` / `error_msg`。
- 支持 Dify 文档列出的全部比较运算符。`in/not in` 使用字符串数组，数值运算使用数字，`before/after` 使用 ISO 8601 日期或时间；没有时区的时间按 UTC 解释。
- 缺失字段只匹配 `empty`，不匹配否定运算；空条件列表不做筛选。字符串比较区分大小写。
- 两路召回通过 RRF 按排名融合，重复帖子只保留一条，不直接相加向量距离和 BM25 原始分数。关键词没有命中时仍保留向量候选。
- 筛选先于重排执行。有元数据条件时，扩展两路检索范围，再按融合排名检查候选，直到收集足够结果或遍历完索引；严格条件可能增加正文请求和延迟。
- 启用重排时，返回的 score 来自重排模型，score_threshold 在重排完成后应用；向量召回阶段不使用该阈值。分数表示本次请求内的相关程度，不是概率。
- 关闭重排且启用 BM25 时，score 为归一化 RRF 分数：`sum(1 / (RRF_K + rank)) / (2 / (RRF_K + 1))`，rank 从 1 开始，仅计算实际命中的路。取值 0–1，两路均排名第一时为 1，仅一路排名第一时为 0.5；它不是模型相似度。
- 同时关闭 BM25 和重排时，score 为 `1 / (1 + L2平方距离)`。关闭重排时阈值在正文获取前应用；切换排序方式后需重新调整 Dify 阈值，建议先设为 0 观察召回结果。
- 每次重排统一发送所有候选，避免混合不同请求的相对得分。每篇输入最多取标题与正文拼接后的 8192 字符，再按 120000 UTF-8 字节的保守总预算扣除每个候选对应的查询字节数并均分截断文档，每篇文档最多 30000 字节；返回给 Dify 的正文保持完整。查询过长、无法容纳指定候选数时返回 HTTP 422，可缩短查询或减少候选数。
- 重排失败或返回非法结果时响应 HTTP 502 / error_code 5005，不静默跳过重排。没有符合最终阈值的结果时正常返回空数组。

重排调用采用 DashScope 原生 `input/parameters` 请求和 `output.results` 响应格式；配置其他模型时需兼容此接口。若使用地域或业务空间专属域名，需同时配置向量与重排端点，以及对应的 API Key。修改配置后需要重启服务。

接口参考：[向量化](https://help.aliyun.com/zh/model-studio/embedding)、[文本排序](https://help.aliyun.com/zh/model-studio/text-rerank-api)。

协议参考：[Dify 外部知识库 API](https://docs.dify.ai/en/self-host/use-dify/knowledge/external-knowledge-api)。

## 正文接口故障

HTTPS 证书校验始终启用。证书过期、网络超时或接口数据异常会记录帖子 ID 和失败原因；没有可返回结果时，Dify 会收到 HTTP 502 / error_code 5003，而不会误显示为正常无命中。部分正文获取成功时可返回部分结果；404/410 帖子会跳过。论坛接口返回 429 时，本次检索返回 HTTP 503 / error_code 5006，并携带 `Retry-After`；进程会按上游要求进入冷却期（缺少有效等待时间时默认 60 秒），停止继续遍历候选，冷却期内的新查询也不会调用向量或正文接口。并发上限不等于每分钟配额，连续检索仍需遵守论坛 API 的限流规则。

若域名经过 WAF，检查运行服务的机器实际连接到的地址，以及对应节点证书；更新证书后需要让 Web 服务重新加载配置。

## Docker

镜像与启动脚本均读取 `HOST`、`PORT`，默认监听 `0.0.0.0:8008`。密钥及本地数据不进入镜像，运行时通过环境文件和数据卷提供：

```bash
docker build -t mudren-rag .
docker run --rm --env-file .env -v "<数据目录绝对路径>:/code/data" mudren-rag python scripts/sync_data.py
docker run -d --name mudren-rag --env-file .env -p 8008:8008 -v "<数据目录绝对路径>:/code/data" mudren-rag
```

以上端口映射适用于默认 `PORT=8008`；例如改为 `PORT=9009`，映射应为 `-p 9009:9009`（左侧可按需设为其他宿主机端口）。Dockerfile 的 `EXPOSE 8008` 仅声明默认端口，不限制实际监听。容器通常保持 `HOST=0.0.0.0`，以便通过端口映射访问。

容器中的 `DB_HOST` 需指向容器可访问的数据库地址。使用以上数据卷时，请保持 `DATA_DIR=data`。

`docker run --env-file` 使用原始 `KEY=value` 格式，会把值外围的引号作为值的一部分；示例文件已使用无外围引号的格式。旧 `.env` 用于 Docker 时需移除语法性外围引号，注释单独成行，不要改变密钥或密码本身的字符。详见 [Docker CLI 环境文件解析规则](https://github.com/docker/cli/blob/master/pkg/kvfile/kvfile.go)。

已有 Docker 部署更新代码后需重新构建镜像，用新镜像执行一次同步，再替换 API 容器，并保持相同数据卷。修改 `--env-file` 后需重新创建容器以载入新值；日常数据同步只需重复上述一次性同步容器命令，API 会热加载快照。

## 验证

线上联调使用 [接口自测示例](docs/operations.md#dify-接口自测)；以下命令用于运行本地自动化测试。

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

测试使用真实 FAISS、BM25、中文分词和本地临时文件，覆盖混合召回、融合去重、关键词单路命中、筛选与阈值、同步增删改及索引迁移/热加载；模拟数据库及外部 HTTP/向量服务，不需要密钥、不产生模型调用费用。

当前仍是单知识库、每篇帖子一个向量，尚无长文分块。向量输入截取标题和正文拼接后的前 8192 个字符，BM25 索引覆盖完整标题和正文。FAISS 使用精确 L2 检索，BM25 在进程内加载并扫描语料；大规模数据的内存和扫描成本需要单独评估。
