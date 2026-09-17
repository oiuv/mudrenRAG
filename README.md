# mudrenRAG

为 Dify 提供 mud.ren 论坛内容检索的外部知识库 API。同步脚本从 MySQL 生成向量索引；检索服务使用 DashScope `text-embedding-v4` 和 FAISS 搜索，再从论坛 API 获取正文。回答生成由 Dify 完成。

## 功能

- `POST /retrieval`：Bearer 鉴权、知识库 ID 校验、top_k、相似度阈值、元数据筛选。
- 增量向量化：扫描当前可见帖子，复用内容未变的向量，只为新增和修改的帖子调用向量 API；删除、封禁和恢复可见的帖子都会在下次同步体现。
- 同步失败时保留旧版本。索引与映射保存在单个原子替换的快照中，避免向量和帖子 ID 错配。
- 服务在后续请求中自动检查并加载新快照，无需重启。加载失败时继续使用上一份有效快照并记录错误。
- 异步向量请求、正文连接复用、全局受限并发和超时控制。
- 正文获取失败且无法返回结果时响应 HTTP 502；没有匹配结果时返回 HTTP 200 和 `{"records":[]}`。
- `GET /health` 返回索引数量及状态，不调用 MySQL、DashScope 或论坛 API。

## 安装与启动

推荐 Python 3.12，支持 Python 3.10 及以上（依赖需有对应平台的安装包）。

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，配置：

| 配置 | 说明 |
| --- | --- |
| DIFY_API_KEY | Dify 请求此服务使用的密钥 |
| DASHSCOPE_API_KEY | 阿里云 DashScope 密钥 |
| KNOWLEDGE_ID | 与 Dify 配置的外部知识库 ID 一致，示例为 mud-ren-forum |
| DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME | 论坛 MySQL，只有同步脚本使用 |
| DATA_DIR | 数据目录，默认项目下 data，可使用绝对路径 |
| FORUM_API_BASE_URL | 正文接口地址，默认 https://api.mud.ren |
| FORUM_BASE_URL | 引用链接地址，默认 https://bbs.mud.ren |
| HTTP_TIMEOUT | 单次 HTTP 网络阶段超时秒数，默认 15 |
| FETCH_CONCURRENCY | 每个服务进程的正文请求并发上限，默认 5，范围 1–32 |
| INDEX_RELOAD_INTERVAL | 请求触发的索引变更检查间隔，默认 5 秒；0 表示每次检查 |

已有部署如果没有设置 `KNOWLEDGE_ID`，继续兼容任意非空 ID，同时输出启动提示。配置后，错误的 ID 返回 HTTP 404 / error_code 2001。

首次同步和后续更新使用同一命令：

```bash
python scripts/sync_data.py
python -m uvicorn app.main:app --host 0.0.0.0 --port 8008
```

Windows 也可以双击 `start_server.bat`，优先使用项目 `.venv`。缺少密钥或没有有效索引时服务启动失败；请先完成配置和同步。数据库为空时，同步会生成可正常服务的空索引。

在 Dify 中配置：

- API 端点：`http://<服务器IP>:8008`，不附加 `/retrieval`。
- API 密钥：`DIFY_API_KEY`。
- 外部知识库 ID：与 `KNOWLEDGE_ID` 一致。

## 同步、升级与恢复

新版使用 `data/knowledge.npz` 保存 FAISS 索引、帖子映射和内容指纹。每次同步遍历数据库当前未删除、未封禁的帖子，数据库扫描量与帖子总量有关，但只为变更内容生成向量。同步进程使用文件锁，避免同时发布多个更新。

旧版的 `threads.index` 和 `id_mapping.json` 可继续被服务读取。首次新版同步会重新向量化旧数据，因为旧格式没有内容指纹；成功后切换至新快照，旧文件保留。如果旧索引已经错配，应执行一次全量重建：

```bash
python scripts/sync_data.py --full
```

全量重建成功前，已发布索引保持有效。任一批次失败或响应向量数量、维度异常，同步退出码为 1，不发布不完整数据；重跑会重新处理本次尚未发布的变更。普通同步遇到损坏的现有快照会明确报错，使用 `--full` 恢复。

同步成功后，服务在下一次达到检查间隔的请求中加载新索引。新快照损坏时继续服务旧内存版本，`/health` 返回 `degraded`；该状态不代表外部接口健康。同步频率由部署环境的定时任务决定。

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
- 筛选会继续检查后续相似候选，直到得到 top_k 个符合条件的结果或耗尽阈值以上候选。严格条件可能导致较多正文请求，增加延迟。
- 分数为 `1 / (1 + L2平方距离)`，不是概率。返回结果按分数降序排列。

协议参考：[Dify 外部知识库 API](https://docs.dify.ai/en/self-host/use-dify/knowledge/external-knowledge-api)。

## 正文接口故障

HTTPS 证书校验始终启用。证书过期、网络超时或接口数据异常会记录帖子 ID 和失败原因；没有可返回结果时，Dify 会收到 HTTP 502 / error_code 5003，而不会误显示为正常无命中。部分正文获取成功时可返回部分结果；404/410 帖子会跳过。

若域名经过 WAF，检查运行服务的机器实际连接到的地址，以及对应节点证书；更新证书后需要让 Web 服务重新加载配置。

## Docker

镜像、启动脚本和文档统一使用 8008 端口。密钥及本地数据不进入镜像，运行时通过环境文件和数据卷提供：

```bash
docker build -t mudren-rag .
docker run --rm --env-file .env -v "<数据目录绝对路径>:/code/data" mudren-rag python scripts/sync_data.py
docker run -d --name mudren-rag --env-file .env -p 8008:8008 -v "<数据目录绝对路径>:/code/data" mudren-rag
```

容器中的 `DB_HOST` 需指向容器可访问的数据库地址。使用以上数据卷时，请保持 `DATA_DIR=data`。

## 验证

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

测试使用真实 FAISS 和本地临时文件，模拟数据库及外部 HTTP/向量服务，不需要密钥、不产生模型调用费用。

当前仍是单知识库、每篇帖子一个向量，向量输入截取标题和正文拼接后的前 8192 个字符。没有长文分块、重排序或关键词混合检索；FAISS 使用精确 L2 检索，大规模数据的内存和扫描成本需要单独评估。
