# Kimi Coding MCP

Kimi Coding MCP 是一个把 Kimi Coding Search / Fetch 接口封装成 MCP 工具的服务，适合部署到服务器后，以远程 MCP 的方式接入你的客户端。

它提供两个工具：

- `kimi_search`：调用 `POST https://api.kimi.com/coding/v1/search`
- `kimi_fetch`：调用 `POST https://api.kimi.com/coding/v1/fetch`

当前支持两种运行模式：

1. **兼容模式（默认）**
   - 与旧版本一致
   - 客户端可以直接传真实 `Kimi API key`
   - 也可以使用服务端 `KIMI_API_KEY`
2. **WebUI 托管模式**
   - 启用 SQLite、后台管理页、用户名/密码/session cookie
   - 客户端不能再直传真实 Kimi key
   - 客户端传的是**平台签发的用户 API**
   - 管理员维护实际调用密钥及其启用状态
   - `search` / `fetch` 独立 RPM 控制

核心实现入口是 `server.py`，WebUI 相关逻辑位于 `webui.py`、`db.py`、`auth.py` 等模块中。

## 1. 快速开始

安装依赖：

```bash
pip install -r requirements.txt
```

兼容模式启动：

```bash
python server.py --transport streamable-http --host 0.0.0.0 --port 8000
```

默认 MCP 地址：

```text
http://127.0.0.1:8000/mcp
```

如果部署在带 HTTPS 的域名上，对外地址通常是：

```text
https://your-domain.com/mcp
```

## 2. 工具说明

### `kimi_search`

输入参数：

```json
{
  "text_query": "食贫道 最新视频 2025 2026",
  "limit": 10,
  "enable_page_crawling": false,
  "timeout_seconds": 30
}
```

### `kimi_fetch`

输入参数：

```json
{
  "url": "https://search.bilibili.com/all?keyword=食贫道"
}
```

## 3. 运行模式

### 模式 A：兼容模式（默认）

特点：

- 不启用 WebUI
- 不启用 SQLite 管理面
- 保持现有 MCP 行为
- `X-Kimi-Api-Key` 表示真实 Kimi key

适合：

- 已有客户端配置不想改
- 单租户或老式“客户端自带真实 key”模式

### 模式 B：WebUI 托管模式

设置：

```bash
KIMI_WEBUI_ENABLED=true
```

特点：

- 启用 `/web/*` 后台页面
- 启用 SQLite 数据库
- 首次启动自动创建管理员账号
- 管理员维护：
  - 用户
  - 平台用户 API
- 实际调用密钥
- 实际调用密钥启用状态
  - 日志与系统设置
- 用户只能：
  - 登录后台
  - 创建/删除自己的用户 API
  - 查看自己的 API 使用情况和日志

注意：

- WebUI 托管模式下，`X-Kimi-Api-Key` 不再表示真实 Kimi key
- 它表示**平台签发的用户 API**
- 服务端会查库校验后，再从已启用的实际调用密钥池中选择一个密钥请求上游

## 4. Docker 部署

### 方式 A：不带代理，直接暴露端口

构建镜像：

```bash
docker build -t kimi-coding-mcp .
```

#### 兼容模式

单租户：

```bash
docker run -d \
  --name kimi-coding-mcp \
  -p 8000:8000 \
  -e KIMI_API_KEY=sk-kimi-你的key \
  kimi-coding-mcp
```

客户端自带真实 key：

```bash
docker run -d \
  --name kimi-coding-mcp \
  -p 8000:8000 \
  kimi-coding-mcp
```

#### WebUI 托管模式

```bash
docker run -d \
  --name kimi-coding-mcp \
  -p 8000:8000 \
  -e KIMI_WEBUI_ENABLED=true \
  -e KIMI_DB_PATH=data/app.db \
  -e KIMI_SESSION_SECRET=replace-with-random-secret \
  -e KIMI_BOOTSTRAP_ADMIN_USERNAME=admin \
  -e KIMI_BOOTSTRAP_ADMIN_PASSWORD=change-me \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  kimi-coding-mcp
```

部署完成后：

- MCP 地址：`http://你的服务器IP:8000/mcp`
- WebUI 登录页：`http://你的服务器IP:8000/web/login`

重要说明：

- 如果你重新部署后发现之前的用户、用户 API、实际调用密钥都丢失，通常不是程序主动删除了数据，而是 **SQLite 数据目录没有持久化**。
- 托管模式默认把数据库写到容器内的 `data/app.db`，因此部署时必须把容器内的 `/app/data` 挂载到宿主机或持久化卷。
- `docker run` 示例中的 `-v $(pwd)/data:/app/data` 或 `docker compose` 中的命名卷 `kimi_mcp_data:/app/data` 都是为了解决这个问题。
- 如果你使用的是无状态部署平台，但没有声明持久化卷，那么每次重建容器后数据库文件都会丢失。

### 方式 B：带代理，通过 HTTPS 域名访问

仓库里提供了 `compose.yaml` 和 `Caddyfile`。

1. 复制环境变量模板：

```bash
cp .env.production.example .env.production
```

2. 编辑 `.env.production`

#### 兼容模式

```bash
KIMI_WEBUI_ENABLED=false
KIMI_API_KEY=sk-kimi-你的key
APP_DOMAIN=kimi-mcp.example.com
```

#### WebUI 托管模式

```bash
KIMI_WEBUI_ENABLED=true
KIMI_API_KEY=
KIMI_DB_PATH=data/app.db
KIMI_SESSION_SECRET=replace-with-random-secret
KIMI_BOOTSTRAP_ADMIN_USERNAME=admin
KIMI_BOOTSTRAP_ADMIN_PASSWORD=change-me
APP_DOMAIN=kimi-mcp.example.com
```

3. 确保域名已解析到服务器公网 IP，并放行 `80` 与 `443`

4. 启动：

```bash
docker compose up -d --build
```

说明：

- `compose.yaml` 默认使用命名卷 `kimi_mcp_data` 持久化数据库文件。
- 日志目录默认使用命名卷 `kimi_mcp_logs` 持久化。
- 这样即使你重新 `docker compose up -d --build`，只要不显式删除卷，SQLite 数据仍会保留。

### 数据位置与备份建议

托管模式下的重要数据默认保存在以下固定路径：

- 数据库文件：`/app/data/app.db`
- 文件日志目录：`/app/logs/`

镜像的 `Dockerfile` 已经把下面两个目录声明为卷：

- `/app/data`
- `/app/logs`

这意味着：

- 如果你在 `docker run` 时不显式挂载目录或卷，Docker 仍会为这两个路径创建匿名卷。
- 如果你希望**可控地持久化、迁移和备份**，更推荐显式绑定宿主机目录或命名卷。

#### 推荐备份内容

至少备份：

- `/app/data/app.db`（用户、用户 API、Kimi 密钥、日志配置等核心数据）

可选备份：

- `/app/logs/`（文件日志，仅用于排障，通常不是必须恢复的数据）

#### 备份建议

1. **最简单方式：直接备份 SQLite 文件**
   - 把 `/app/data/app.db` 复制到安全位置
2. **部署前备份一次，升级前再备份一次**
3. **不要只依赖容器本身**
   - 重建容器不会自动替你导出数据库
4. **如果使用命名卷，也要定期把卷里的 `app.db` 导出到宿主机或对象存储**

#### 恢复建议

恢复时只需要把备份的 `app.db` 放回：

- `/app/data/app.db`

然后重新启动容器即可。

部署完成后：

- MCP 地址：`https://kimi-mcp.example.com/mcp`
- WebUI 登录页：`https://kimi-mcp.example.com/web/login`

## 5. WebUI 管理后台

### 首次启动

当 `KIMI_WEBUI_ENABLED=true` 时，服务会自动初始化 SQLite 数据库，并按环境变量创建管理员账号：

- `KIMI_BOOTSTRAP_ADMIN_USERNAME`
- `KIMI_BOOTSTRAP_ADMIN_PASSWORD`

如果该管理员已存在，则不会重复创建。

### 管理员能力

- 创建/禁用用户
- 重置用户密码
- 创建/删除/禁用用户 API
- 添加实际调用密钥
- 启用或禁用实际调用密钥
- 查看全局请求日志
- 调整日志等级和日志预览字节数

### 普通用户能力

- 登录后台
- 修改自己的密码
- 创建自己的用户 API
- 删除自己的用户 API
- 查看自己的日志和使用统计

## 6. 客户端配置

### 兼容模式：客户端传真实 Kimi key

```json
{
  "mcpServers": {
    "kimi-coding-remote": {
      "type": "streamable_http",
      "url": "https://kimi-mcp.example.com/mcp",
      "headers": {
        "X-Kimi-Api-Key": "sk-kimi-替换成你的key"
      }
    }
  }
}
```

### WebUI 托管模式：客户端传平台用户 API

```json
{
  "mcpServers": {
    "kimi-coding-remote": {
      "type": "streamable_http",
      "url": "https://kimi-mcp.example.com/mcp",
      "headers": {
        "X-Kimi-Api-Key": "kimu_替换成后台生成的用户api"
      }
    }
  }
}
```

在托管模式下：

- 客户端不能再传真实 Kimi key
- 平台用户 API 校验通过后，服务端才会使用管理员维护且已启用的实际调用密钥调用上游

## 7. 环境变量

常用环境变量：

```bash
KIMI_API_KEY=sk-kimi-你的key
KIMI_BASE_URL=https://api.kimi.com/coding/v1
KIMI_USER_AGENT=KimiCLI/1.24.0
KIMI_MSH_PLATFORM=kimi_cli
KIMI_MSH_VERSION=1.24.0
KIMI_DEVICE_NAME=YOUR-PC
KIMI_DEVICE_MODEL=Windows 11 AMD64
KIMI_OS_VERSION=10.0.26200
KIMI_DEVICE_ID=自定义设备ID
KIMI_LOG_DIR=logs
KIMI_LOG_LEVEL=INFO
KIMI_LOG_MAX_BYTES=5242880
KIMI_LOG_BACKUP_COUNT=3
KIMI_LOG_PREVIEW_BYTES=100
KIMI_WEBUI_ENABLED=false
KIMI_DB_PATH=data/app.db
KIMI_SESSION_SECRET=replace-with-random-secret
KIMI_SESSION_COOKIE_NAME=kimi_admin_session
KIMI_SESSION_TTL_SECONDS=604800
KIMI_BOOTSTRAP_ADMIN_USERNAME=admin
KIMI_BOOTSTRAP_ADMIN_PASSWORD=admin123456
MCP_TRANSPORT=streamable-http
MCP_HOST=0.0.0.0
MCP_PORT=8000
MCP_STREAMABLE_HTTP_PATH=/mcp
```

说明：

- `KIMI_API_KEY` 在兼容模式下仍可作为服务端默认真实 key
- `KIMI_WEBUI_ENABLED=true` 时，推荐将 `KIMI_API_KEY` 留空，由后台托管实际调用密钥
- SQLite 默认写到 `data/app.db`
- WebUI session cookie 默认名为 `kimi_admin_session`

如果使用容器部署，对应容器内实际路径是：

- 数据库：`/app/data/app.db`
- 日志：`/app/logs/server.log`

## 8. 日志与限流

### 文件日志

日志默认写入 `logs/server.log`，并按大小轮转。日志会记录：

- 调用的 endpoint
- 入参预览
- 返回结果预览
- 状态码与耗时

### 审计日志

WebUI 托管模式下，调用会写入 SQLite 审计日志，用于后台页面查询。

### 限流

托管模式下，每个用户 API 可分别配置：

- `search_rpm`
- `fetch_rpm`

超限会返回错误，并记录审计日志。

## 9. 本地调试

### 本地 stdio 模式

```bash
python server.py --transport stdio
```

### 本地 HTTP 兼容模式

```bash
python server.py --transport streamable-http --host 0.0.0.0 --port 8000
```

### 本地 WebUI 托管模式

```bash
export KIMI_WEBUI_ENABLED=true
export KIMI_SESSION_SECRET=dev-secret
python server.py --transport streamable-http --host 0.0.0.0 --port 8000
```

然后访问：

- `http://127.0.0.1:8000/web/login`
- `http://127.0.0.1:8000/mcp`

## 10. 说明

- `kimi_search` 使用 `text_query`、`limit`、`enable_page_crawling`、`timeout_seconds` 请求搜索接口；如果响应是 JSON，会自动格式化。
- `kimi_fetch` 使用 `url` 请求抓取接口，并默认按 `Accept: text/markdown` 返回文本。
- WebUI 托管模式下，用户看不到真实 Kimi key，只能使用平台签发的用户 API。
