# AI 健身助理

一个具备记忆能力的个人 AI 助理，场景为健身：增力周期编排、减脂计划计算、场景化配餐。

- 设计文档：`docs/superpowers/specs/2026-08-24-fitness-agent-design.md`
- 实现计划：`docs/superpowers/plans/2026-08-24-fitness-agent.md`

## 当前进度

Plan 1（后端内核）已完成，T1–T20。Plan 2（记忆三层、计划生成、场景化配餐、前端）待做。

## 运行

### 方式一：Docker Compose

```bash
cp .env.example .env    # 填入 LLM_API_KEY / LLM_MODEL / LLM_BASE_URL
docker compose up
```

Postgres 映射在宿主机 **5433**（避开本机可能已有的 5432）。

### 方式二：本机

需要 Python 3.12+ 与 PostgreSQL 16 + pgvector。

```bash
# 1. 建库与扩展
createdb fitness
psql -d fitness -c "CREATE EXTENSION IF NOT EXISTS vector"
# 应用用非 superuser 角色连库——RLS 对 superuser 无效
psql -d postgres -c "CREATE ROLE fitness LOGIN PASSWORD 'fitness'"
psql -d postgres -c "ALTER DATABASE fitness OWNER TO fitness"

# 2. 装依赖
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. 迁移
cp ../.env.example ../.env    # 填 LLM_* 三项
.venv/bin/alembic upgrade head

# 4. 起服务
.venv/bin/uvicorn app.main:app --reload
```

`.env` 不填 `LLM_API_KEY` / `LLM_MODEL` 也能启动：对话会走 L4 规则兜底，
数据库、认证、记录功能完全正常。

## 试一下

```bash
B=http://localhost:8000
TOKEN=$(curl -s -X POST $B/api/v1/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"me@test.com","password":"pw123456"}' | jq -r .access_token)
CONV=$(curl -s -X POST $B/api/v1/conversations -H "Authorization: Bearer $TOKEN" | jq -r .id)

# 快路径：结构化输入直接入库，全程不碰模型，约 40ms
curl -N -X POST $B/api/v1/conversations/$CONV/messages \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"卧推 80kg 5x5"}'

# 正常路径：走模型 + 工具调用
curl -N -X POST $B/api/v1/conversations/$CONV/messages \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"我178cm 82kg 30岁男，中等活动量，想减脂，每天该吃多少"}'
```

## 测试

```bash
cd backend
.venv/bin/pytest -q                                          # 322 passed
.venv/bin/pytest tests/domain -q --cov=app/core/domain        # 100% 覆盖
```

## 已实现

| 模块 | 说明 |
|---|---|
| `core/domain/` | 纯计算：BMR/TDEE、三大营养素、1RM 与周期编排、目标冲突、体重推算。零依赖，100% 覆盖 |
| `core/tools/` | 工具注册表 + 8 个工具。新增工具 = 新增一个文件 |
| `core/llm/` | Provider 抽象、OpenAI 兼容实现、用量记录、降级阶梯 L1–L3 |
| `core/agent/` | 上下文组装与 Token 预算、主循环、快路径、L4 规则兜底 |
| RLS | Postgres 行级安全强制租户隔离，非 superuser 角色 + FORCE RLS |

## 降级阶梯

```
L0 正常      主模型 + 完整工具集
L1 重试      同模型退避 2 次
L2 换模型    切 LLM_FALLBACK_MODEL
L3 缩范围    工具集砍到核心 5 个
L4 规则兜底  完全绕过 LLM，数字与正常模式逐位一致
L5 诚实失败  明确告知 + 保留用户输入
```

L4 之所以能给出正确数字，是因为所有计算都在 `core/domain/` 的纯函数里——
模型不参与任何算术。这个决定最初是为了避免模型算错热量，顺带给了系统一条
不依赖 LLM 的生路。

## 已知限制

- 用户档案（L1 记忆）尚未接入，正常路径的 `profile` 目前是空字典 → Plan 2 T21
- 事实记忆（L2）与向量检索未实现 → Plan 2 T22–T24
- 计划生成、场景化配餐工具未实现 → Plan 2 T25–T29
- 前端未实现 → Plan 2 T30–T33
