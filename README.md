# mudrenRAG

一个为 Dify 平台提供的外部知识库 API 服务，基于 `mud.ren` 论坛内容，使用 RAG 技术实现。

## ✨ 功能特性

- **Dify 兼容**: 完全实现了 Dify 的外部知识库 API 规范，可无缝接入 Dify 应用。
- **高效检索**: 使用 FAISS 进行高效的向量相似度检索，保证查询性能。
- **增量更新**: 支持高效的增量更新，只为新帖子创建向量，节省时间和成本。
- **灵活维护**: 支持通过删除索引文件自动触发全量重建，方便数据清理和重构。
- **云端向量**: 使用阿里云 DashScope `text-embedding-v4` 模型生成文本向量。
- **一键启动**: 提供 Windows 批处理脚本，方便快速启动服务。

## 🛠️ 技术栈

- **后端**: Python, FastAPI
- **向量检索**: FAISS
- **向量生成**: 阿里云 DashScope
- **数据库**: MySQL
- **部署**: Docker

## 🚀 如何使用

#### 1. 克隆与安装依赖

```bash
# 克隆您的仓库
git clone <your-repo-url>
cd mudrenRAG

# 安装 Python 依赖
pip install -r requirements.txt
```

#### 2. 配置环境变量

将 `.env.example` 文件复制一份，重命名为 `.env`，并填入您的真实配置信息：

- `DIFY_API_KEY`: 用于保护 API 的密钥，需要填入 Dify。
- `DASHSCOPE_API_KEY`: 您的阿里云 DashScope API Key。
- `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`: 您论坛数据库的连接信息。

#### 3. 同步知识库

运行数据同步脚本来生成或更新向量索引。

- **首次运行或全量更新**:
  如果 `data/` 目录不存在，脚本会自动进行一次完整的、从头开始的同步。
  ```bash
  python scripts/sync_data.py
  ```

- **日常增量更新**:
  当 `data/` 目录已存在时，再次运行脚本将只会处理新增的帖子。
  ```bash
  python scripts/sync_data.py
  ```

#### 4. 启动 API 服务

- **Windows**: 直接双击 `start_server.bat` 文件。
- **其他系统或手动启动**:
  ```bash
  uvicorn app.main:app --host 0.0.0.0 --port 8008
  ```

#### 5. 连接到 Dify

在 Dify 的知识库设置中，添加一个新的外部知识库：

- **API 端点**: `http://<您的服务器IP地址>:8008` (Dify会自动在末尾添加 /retrieval)
- **API 密钥**: 填入您在 `.env` 文件中设置的 `DIFY_API_KEY`。
- **知识库ID**: 任意唯一的字符串，例如 `mud-ren-forum`。

## 📁 项目结构

```
/mudrenRAG
|-- app/              # FastAPI API 服务
|   |-- main.py       # API 主程序
|   |-- models.py     # Pydantic 数据模型
|-- scripts/
|   |-- sync_data.py  # 数据同步与向量化脚本
|-- data/             # (自动生成) 存放索引文件
|-- .env.example      # 环境变量示例
|-- .gitattributes    # Git 换行符配置
|-- .gitignore        # Git 忽略配置
|-- requirements.txt  # Python 依赖
|-- Dockerfile        # Docker 部署文件
|-- start_server.bat  # Windows 启动脚本
```
