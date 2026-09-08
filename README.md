# FitMind AI

一个有长期记忆的 AI 健身助理。你可以用自然语言记录训练和身体数据、生成训练或饮食计划，并在后续对话中继续使用已经保存的信息。

## 可以做什么

- 记录训练，例如：`卧推 80kg 5x5`
- 记录体重和其他身体数据
- 生成增力、减脂和配餐计划
- 查询训练历史与身体变化趋势
- 记住目标、偏好、伤病和忌口
- 自动整理对话中产生的待办事项

## 快速开始

### 1. 准备环境

请先安装：

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- Git

### 2. 下载项目

```bash
git clone https://github.com/fanmaom/FitMind-ai.git
cd FitMind-ai
```

### 3. 配置模型

复制环境变量模板：

```bash
cp .env.example .env
```

打开 `.env`，至少填写下面三项：

```dotenv
LLM_BASE_URL=你的 OpenAI 兼容接口地址
LLM_API_KEY=你的 API Key
LLM_MODEL=模型名称
```

建议同时生成新的登录密钥：

```bash
openssl rand -hex 32
```

把生成结果填入 `.env`：

```dotenv
JWT_SECRET=刚才生成的随机字符串
```

如果没有配置模型，项目仍能启动，但对话能力会受到限制。

### 4. 启动

确保 Docker Desktop 正在运行，然后执行：

```bash
./start.sh
```

首次启动需要下载依赖和构建镜像，可能需要几分钟。完成后访问：

- Web：<http://localhost:3000>
- API 文档：<http://localhost:8000/docs>

演示账号：

```text
账号：demo@fitmind.cn
密码：demo123456
```

## 使用示例

登录后可以依次尝试：

```text
卧推 80kg 5x5
```

```text
我不吃香菜，工作日午饭在公司解决
```

```text
按 82kg、TDEE 2770 帮我做 8 周碳循环减脂计划
```

```text
办公室午饭怎么配？
```

```text
查询我最近的卧推训练记录
```

待办和长期记忆由后台异步处理，对话后等待几秒再刷新右侧面板即可看到结果。

## 常用命令

```bash
./start.sh              # 构建并启动全部服务
./start.sh --no-build   # 不重新构建，直接启动
./start.sh --no-seed    # 启动但不导入演示数据
make ps                 # 查看服务状态
make logs               # 查看全部日志
make logs-worker        # 查看待办与记忆处理日志
make down               # 停止服务并保留数据
make test               # 运行后端测试
```

修改代码或 `.env` 中的前端 API 地址后，请重新执行 `./start.sh`，不要使用 `--no-build`。

## 修改端口

默认端口被占用时，可以修改 `.env`：

```dotenv
WEB_PORT=3100
API_PORT=8100
POSTGRES_PORT=5434
NEXT_PUBLIC_API_BASE=http://localhost:8100/api/v1
CORS_ORIGINS=http://localhost:3100,http://127.0.0.1:3100
```

修改后重新运行：

```bash
./start.sh
```

## 常见问题

### Docker 没有启动

先打开 Docker Desktop，等待它完全启动，再运行 `./start.sh`。

### 端口已被占用

修改 `.env` 中的 `WEB_PORT`、`API_PORT` 和 `POSTGRES_PORT`，然后重新构建。

### 对话没有调用模型

检查 `.env` 中的 `LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_MODEL`，然后查看日志：

```bash
make logs
```

### 待办或长期记忆没有立即出现

这些内容由 worker 异步处理。等待 2–5 秒后刷新右侧面板；仍未出现时运行：

```bash
make logs-worker
```

## 安全提示

- 不要提交 `.env` 或任何 API Key。
- `.env.example` 只用于展示配置格式。
- 项目提供的饮食和训练建议仅供参考，不能替代医生或专业教练的意见。
