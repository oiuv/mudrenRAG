# 同步与接口排查操作指南

本指南配合 [README](../README.md) 使用。Windows 的 `start_server.bat` 会自动准备虚拟环境、补齐依赖、同步一次数据并启动 API，详细步骤见 [Windows 自动启动](../README.md#windows-自动启动)。项目不会自动创建定时任务；服务持续运行期间可按以下示例配置周期更新。Windows 路径以 `C:\AI\mudrenRAG`、Linux 路径以 `/opt/mudrenRAG` 为例，请替换为实际部署位置。

## Windows 任务计划程序

先在 PowerShell 中确认普通同步成功：

```powershell
Set-Location 'C:\AI\mudrenRAG'
& '.\.venv\Scripts\python.exe' -u '.\scripts\sync_data.py'
if ($LASTEXITCODE -ne 0) { throw '同步失败，请查看上方日志。' }
```

在“任务计划程序”中创建任务，按下表填写：

| 项目 | 示例设置 |
| --- | --- |
| 名称 | mudrenRAG 知识库同步 |
| 运行账户 | 能读取项目 `.env`、访问 MySQL 和模型服务、写入 `DATA_DIR` 的账户 |
| 常规 | 若需退出登录后继续运行，选择“不管用户是否登录都要运行”并配置相应凭据 |
| 触发器 | 设置开始时间，每 30 分钟重复一次，持续时间为无限期 |
| 操作 | 启动程序 |
| 程序或脚本 | `C:\AI\mudrenRAG\.venv\Scripts\python.exe` |
| 添加参数 | `-u "C:\AI\mudrenRAG\scripts\sync_data.py"` |
| 起始于 | `C:\AI\mudrenRAG` |
| 已有任务正在运行时 | 不启动新实例 |

创建后手动运行一次任务，查看“上次运行结果”。脚本退出码为 0 表示完成（包括内容未变），1 表示失败或已有同步在运行；进一步排查可使用上面的 PowerShell 命令查看详细日志。

周期同步任务应直接调用 `scripts/sync_data.py`。`start_server.bat` 还会检查安装依赖并启动常驻 API，不适合作为周期同步任务；更新索引无需定时重启 API。同步脚本自动读取项目根目录的 `.env`，无需在任务中另外激活虚拟环境。任务账户已有的同名环境变量会覆盖 `.env`，手动执行与计划任务结果不同的时候应检查这一点。

任务的程序、参数和工作目录分别对应 [ExecAction 配置](https://learn.microsoft.com/en-us/windows/win32/taskschd/execaction)，重复执行由 [RepetitionPattern](https://learn.microsoft.com/en-us/windows/win32/taskschd/repetitionpattern) 控制。

## Linux cron

在部署账户下先执行一次普通同步，并创建日志目录：

```bash
cd /opt/mudrenRAG
mkdir -p logs
/opt/mudrenRAG/.venv/bin/python -u /opt/mudrenRAG/scripts/sync_data.py
```

确认同步成功后，运行 `crontab -e`，添加以下一行。该示例在每小时的第 0、30 分钟运行；`logs` 目录需已存在且可写。

```cron
*/30 * * * * cd /opt/mudrenRAG && /opt/mudrenRAG/.venv/bin/python -u /opt/mudrenRAG/scripts/sync_data.py >> /opt/mudrenRAG/logs/sync.log 2>&1
```

这是用户 crontab 格式，不需要在时间字段后添加用户名。确认系统 cron 服务正在运行；执行结果写入 `logs/sync.log`，可用下面的命令查看：

```bash
tail -n 100 /opt/mudrenRAG/logs/sync.log
```

同步脚本已有文件锁。若前一次尚未结束，新进程会记录 `Another synchronization is running` 并退出，不会同时发布索引；若频繁重叠，可增大运行间隔。定期轮转或清理同步日志，避免日志持续增长。时间字段语法见 [Cronie crontab 手册](https://github.com/cronie-crond/cronie/blob/master/man/crontab.5)。

## 确认数据更新生效

同步日志中的 `Published N threads` 表示新快照已发布，`No content changes` 表示无需发布。发布成功后，在达到 `INDEX_RELOAD_INTERVAL` 的请求中，API 会加载新快照。

从部署机器访问健康接口：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8008/health'
```

Linux 可执行：

```bash
curl --silent --show-error http://127.0.0.1:8008/health
```

返回格式如下，数量仅为示例：

```json
{"status": "ok", "indexed_threads": 123}
```

`ok` 表示本地快照加载正常；`degraded` 表示最近一次检查失败、服务正在使用上一份有效快照。修改已有帖子时，数量可能不变，应再查询该帖子新增的关键词。`/health` 不调用外部服务，不能用它判断向量、重排或论坛正文接口是否正常。

## Dify 接口自测

在服务启动且已有索引后执行。以下查询会调用实际配置的向量、重排及论坛接口；将查询内容替换为知识库中已知存在的术语，首次排查把 `score_threshold` 设为 0。

PowerShell 示例从输入读取 `DIFY_API_KEY`，并显式发送 UTF-8 JSON。将 `knowledge_id` 改为部署的 `KNOWLEDGE_ID`：

```powershell
$serviceUrl = 'http://127.0.0.1:8008'
$difySecret = Read-Host 'DIFY_API_KEY' -AsSecureString
$difyCredential = [System.Management.Automation.PSCredential]::new('dify', $difySecret)
$difyHeaders = @{ Authorization = 'Bearer ' + $difyCredential.GetNetworkCredential().Password }
$payload = @{
    knowledge_id = 'mud-ren-forum'
    query = '如何使用 query_temp？'
    retrieval_setting = @{ top_k = 3; score_threshold = 0.0 }
} | ConvertTo-Json -Depth 5
$result = Invoke-RestMethod -Uri ($serviceUrl + '/retrieval') -Method Post -Headers $difyHeaders -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) -TimeoutSec 120
$result | ConvertTo-Json -Depth 10
```

Linux Bash 示例：

```bash
read -r -s -p 'DIFY_API_KEY: ' dify_api_key
printf '\n'
curl --silent --show-error --include --max-time 120 \
  http://127.0.0.1:8008/retrieval \
  -H "Authorization: Bearer ${dify_api_key}" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "knowledge_id": "mud-ren-forum",
  "query": "如何使用 query_temp？",
  "retrieval_setting": {"top_k": 3, "score_threshold": 0.0}
}
JSON
unset dify_api_key
```

正常响应为 HTTP 200，顶层包含 `records` 数组。每条结果包含 `content`、`score`、`title` 和对象形式的 `metadata`；`{"records":[]}` 表示没有符合条件的结果。

本机测试通过后，再从 Dify 所在机器或容器测试实际注册的服务地址。Dify 中填服务基础地址，例如 `http://<可访问的服务器地址>:8008`，由 Dify 追加 `/retrieval`。Dify 在另一台机器或容器内运行时，其 `127.0.0.1` 指向自身。这里使用的是 `DIFY_API_KEY`，模型服务的密钥配置在服务端的 `DASHSCOPE_API_KEY`。

## 常见问题与错误码

| 现象 / HTTP / error_code | 含义与处理 |
| --- | --- |
| 自动启动停在依赖安装阶段 | 查看 pip 输出，检查包源连接及当前 Python 是否有可用依赖版本。脚本会自动补齐 pip，但不会绕过安装失败继续启动。 |
| 自动启动提示已创建 `.env` 或配置无效 | 按中文注释填写密钥与数据库配置，替换示例占位值后重新运行；进程环境变量优先于文件。 |
| 自动启动停在知识库同步阶段 | 检查同步日志中的数据库、向量接口或索引错误；失败会停止启动，修复后再次运行脚本。 |
| 启动提示 `BM25 index is missing` | 旧快照没有关键词数据。先运行普通同步再启动；暂时沿用旧快照时可设置 `BM25_ENABLED=false`，向量模型和维度仍需匹配。 |
| 启动提示模型或维度不匹配 | 确认 API 与同步脚本使用相同配置和数据目录，按当前模型运行普通同步，再重启 API。 |
| 403 / 1001 | Authorization 头缺失或格式错误，应为 `Bearer <DIFY_API_KEY>`。 |
| 403 / 1002 | Dify 请求密钥不匹配，检查 `DIFY_API_KEY` 及实际进程环境变量。 |
| 404 / 2001 | `knowledge_id` 与已配置的 `KNOWLEDGE_ID` 不符。 |
| 422 / 1003 | 请求字段、筛选条件或重排输入预算不符合要求。按 `error_msg` 修正；长查询可缩短查询或降低重排候选数 / top_k。 |
| 502 / 5002 | 查询向量化失败。检查向量模型、维度、接口地址、模型密钥与网络，查看服务端对应日志。 |
| 502 / 5003 | 正文获取失败且没有可返回结果。检查论坛 API、源站 / WAF 证书及网络。 |
| 502 / 5005 | 重排请求失败或响应格式不兼容。检查模型名称、原生重排接口地址及模型密钥；修复后重试。 |
| 503 / 5001 | 无法取得可用知识索引。检查数据目录、快照文件和同步结果。 |
| 503 / 5004 | 本地向量或 BM25 检索异常。检查服务端日志及快照，损坏时使用 `--full` 重建。 |
| 200，`records` 为空 | 先确认索引非空、同步已完成；将阈值设为 0、移除元数据筛选、使用已知帖子关键词重试。已删除正文会被跳过。 |
| `/health` 为 `degraded` | 检查 API 的索引加载日志，确认新快照的完整性、模型/维度和 BM25 数据。成功加载有效快照后恢复 `ok`。 |

若日志包含 `CERTIFICATE_VERIFY_FAILED: certificate has expired`，检查 API 运行环境实际连接的节点。域名经过 WAF 时，WAF 与源站证书均需分别检查；源站证书更新后让相应 Web 服务重新加载。服务始终启用证书校验。

重排出错时不会自动切换为融合排序；如需主动关闭重排，可设置 `RERANK_ENABLED=false` 并重启 API，此时应按 README 中的 RRF 分数说明重新设置 Dify 阈值。向量化失败也不会自动切换为纯 BM25 检索。
