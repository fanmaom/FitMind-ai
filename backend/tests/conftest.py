"""测试环境准备。

测试不依赖真实 .env——在导入任何应用模块之前就把环境变量设好，
这样 CI 里没有 .env 文件也能跑。
"""

import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://fitness:fitness@localhost:5432/fitness",
)
os.environ.setdefault("JWT_SECRET", "t" * 32)
os.environ.setdefault("LLM_API_KEY", "sk-test-not-a-real-key")
os.environ.setdefault("LLM_MODEL", "test-model")
