FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# uv/uvx：运行 Python 生态的 MCP server（uvx mcp-server-time 之类）。
#
# 只装 uv 不装 Node，是因为两者体积差一个量级——uv 装完约 40MB，Node + npm 要
# 200MB 以上。npx 形式的 MCP server 更常见，但让镜像大三倍去支持一个可选功能
# 不划算；需要 npx 系 server 时在这里补一行 Node 安装即可。
#
# 不配置 MCP_SERVERS 时这一层完全用不到。
RUN pip install --no-cache-dir uv==0.5.11

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
