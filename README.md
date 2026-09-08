# FitMind AI · 有记忆的健身助理

一个具备长期记忆的全栈 AI 个人助理。用户用自然语言对话，助理会维护档案、召回长期事实、
把自己给出的建议沉淀成可勾选的待办、查询训练日志、生成增力/减脂周期，并按居家、办公室
或外食场景给出配餐。所有数字都来自确定性函数，不由模型口算。

- 前端：Next.js 16 + React 19 + Tailwind + Zustand
- 后端：FastAPI + SQLAlchemy 2 异步 + PostgreSQL 16 + pgvector
- 模型：OpenAI 兼容网关（对话 + embedding），支持配置备用模型

---

## 目录

- [快速开始](#快速开始)
- [Demo 剧本](#demo-剧本)
- [架构总览](#架构总览)
- [考察点对照](#考察点对照)
- [记忆系统设计](#记忆系统设计)
- [上下文管理与 Token 控制](#上下文管理与-token-控制)
- [工具调用](#工具调用)
- [不确定输出的兜底与降级](#不确定输出的兜底与降级)
- [用户数据隔离与隐私](#用户数据隔离与隐私)
- [API 接口](#api-接口)
- [项目结构](#项目结构)
- [本机开发](#本机开发)
- [测试](#测试)
- [环境变量](#环境变量)
- [已知限制](#已知限制)

---

## 快速开始

只需要 Docker Desktop / Docker Engine 与 Compose。

```bash
cp .env.example .env    # 编辑：填写模型服务，并按自己的环境调整端口
./start.sh
```

打开 <http://localhost:3000>，用 `demo@fitmind.cn` / `demo123456` 登录。

`start.sh` 会依次：检测 Docker 与 Compose 命令形态（新版子命令与旧版独立版都支持）→
校验 `.env` 配置项 → 构建并启动全部服务 → 等 API 健康检查通过（含数据库迁移）→
灌入演示数据 → 打印访问地址。脚本幂等，重复执行安全。首次没有 `.env` 时它会
自动从 `.env.example` 生成并告诉你要填哪三项。

```bash
./start.sh --no-build   # 没改代码，只重启（更快）
./start.sh --no-seed    # 不灌演示数据
./start.sh --help
```

| 服务 | 默认地址 | 在自己的环境中如何修改 |
|---|---|---|
| Web | <http://localhost:3000> | `.env` 中改 `WEB_PORT` |
| API | <http://localhost:8000> | `.env` 中改 `API_PORT` |
| API 文档 | <http://localhost:8000/docs> | 随 `API_PORT` 自动变化 |
| PostgreSQL | `localhost:5433` | `.env` 中改 `POSTGRES_PORT`；只在需要用数据库客户端直连时使用 |

### 给其他使用者的配置说明

拿到源码后，每个人都应在**自己的电脑**创建 `.env`；这个文件不纳入 Git 或发布包，
因此不会包含你的模型密钥或本机端口。

1. 复制模板：`cp .env.example .env`。
2. 填写自己的 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`，并用
   `openssl rand -hex 32` 生成自己的 `JWT_SECRET`。
3. 若默认端口被占用，修改 `WEB_PORT`、`API_PORT`、`POSTGRES_PORT`。例如：

   ```dotenv
   WEB_PORT=3100
   API_PORT=8100
   POSTGRES_PORT=5434
   NEXT_PUBLIC_API_BASE=http://localhost:8100/api/v1
   CORS_ORIGINS=http://localhost:3100,http://127.0.0.1:3100
   ```

   前端 API 地址和 CORS 必须与改后的 Web/API 端口对应；`NEXT_PUBLIC_API_BASE`
   在前端构建时写入，所以改完端口后需要运行不带 `--no-build` 的 `./start.sh` 重新构建。
4. 运行 `./start.sh`。脚本会使用 `.env` 中的端口检查冲突，并在完成后打印实际访问地址。

如果部署到服务器或用域名访问，把 `NEXT_PUBLIC_API_BASE` 改为浏览器可访问的 API 地址
（例如 `https://assistant.example.com/api/v1`），并把站点地址加入 `CORS_ORIGINS`。不要把
`.env`、任何 API Key 或导出的数据库数据提交到仓库或发送给他人。

### 生成可分发源码包

在仓库根目录运行 `./package-source.sh`，会在仓库根目录生成 `FitMind-AI-source.zip`。解压后会得到
独立的 `FitMind-AI-source/` 项目目录。该包会保留
源码、Docker 配置、`.env.example` 和文档，但主动排除 `.env`、Git 历史、依赖目录、构建
缓存、日志、个人图片和题目附件。把 ZIP 发给对方后，对方按上面的「给其他使用者的配置说明」
创建自己的 `.env` 即可运行。

数据库迁移不需要手动执行——`server` 容器的启动命令是
`alembic upgrade head && uvicorn ...`，每次启动自动推到最新版本。

**改了代码必须重建镜像**：三个服务都把源码打进镜像，`web` 还把
`NEXT_PUBLIC_API_BASE` 作为构建参数烧进产物。`./start.sh`（不带 `--no-build`）
和 `make up` 都已带 `--build`。

### Makefile

`Makefile` 收了日常命令，`make` 或 `make help` 看全部：

```bash
make up            # 等价于 docker-compose up -d --build
make logs-worker   # 只看 worker（排查待办/记忆抽取时用这个）
make ps            # 容器状态
make down          # 停止服务，保留数据卷
make test          # 后端全量测试
make check         # 后端测试 + 覆盖率 + 前端类型检查与构建
make clean         # 清理构建产物
```

装的是新版 Compose 插件时，用 `make COMPOSE="docker compose" up` 覆盖即可。

---

## Demo 剧本

登录演示账号后依次输入：

1. `卧推 80kg 5x5` — 快路径直接落库，不经过模型，几十毫秒返回训练卡片。
2. `我不吃香菜，工作日午饭在公司解决` — 抽取为长期事实；若模型推断出敏感字段
   （目标、伤病）会先征询确认再写入。
3. `按 82kg、TDEE 2770 帮我做 8 周碳循环减脂计划` — 返回减脂卡片与周变化率。
4. `办公室午饭怎么配？` — 自动读取上一步记住的场景与忌口生成配餐。
5. `我想一边快速减脂一边刷新卧推 PR` — 返回目标冲突判定与可点击的备选方案。
6. 长回复生成过程中点击**停止**按钮 — 立即中断，已生成的半句保留并标注「已停止」。
7. 右侧面板切到**待办** — 第 3、4 步里助理提出的建议已经异步沉淀成可勾选事项。
   点条目上的「来源」会滚动高亮回原始那条助理消息。

待办是异步抽取的（worker 2 秒轮询），聊完等 2–5 秒再刷新面板。

---

## 架构总览

```mermaid
flowchart LR
  Web[Next.js Web] -->|JWT + SSE| API[FastAPI]
  API --> Agent[Agent 循环]
  Agent --> Tools[工具注册表]
  Agent --> LLM[OpenAI 兼容网关]
  Tools --> Domain[领域纯函数层]
  Tools --> PG[(PostgreSQL + pgvector)]
  API --> Queue[(jobs 队列)]
  Worker[抽取 Worker] --> Queue
  Worker --> LLM
  Worker --> PG
  PG --> L1[L1 profiles 档案]
  PG --> L2[L2 memories 事实]
  PG --> L3[L3 logs 日志]
  PG --> A[action_items 待办]
```

三条设计主线：

**读写分层。** 日志永远不进 prompt，只能由工具聚合查询；事实必须低于余弦距离阈值
才召回；档案每轮全量注入。三条通道刻意不混，避免"把所有能查到的都塞进上下文"。

**计算不交给模型。** 所有数字（TDEE、营养素克数、1RM、周期重量、距目标天数）
都由 `core/domain/` 下的纯函数算出。模型只负责理解意图、编排工具、组织语言。

**异步旁路不影响主链路。** 事实抽取、待办抽取和会话摘要都在回复发送**之后**
投递到 PostgreSQL 队列，由独立 worker 消费。抽取失败、模型抽风、判重出错，
都不会影响用户已经看到的那次回复。

队列有两道保障。**看门狗**回收卡死的 `running`：worker 在执行途中硬退出
（SIGKILL、OOM）时任务会永久停在 `running`，而 `claim_jobs` 只捞 `pending`，
没有任何机制会再碰它——抽取就此丢失，不报错也不重试。回收时照常累加
`attempts`，否则一个必然把 worker 打死的任务会被无限回收、无限打死 worker，
堵住整个队列。**优雅退出**让 `docker stop` 变成"跑完手上这批再退"：正在执行的
任务被立即标为可重试，而不是留在 `running` 等十分钟后才被回收。

实测停机行为：

| | 修复前 | 修复后 |
|---|---|---|
| `worker` 停止耗时 | 等满宽限期 | **1 秒** |
| `worker` 退出码 | **137**（SIGKILL） | **0** |
| `server` 停止耗时 | 等满宽限期 | **0 秒** |

`server` 的根因在 compose 的 `sh -c "alembic ... && uvicorn ..."`：shell 是
PID 1 而它**不转发 SIGTERM**。加 `exec` 让 uvicorn 顶替 shell 成为 PID 1 即可。

---

## 考察点对照

| 考察方向 | 实现要点 | 主要位置 |
|---|---|---|
| 记忆写入时机 | 回复完成后异步投递，中断的回合不投递 | `services/chat_service.py` |
| 记忆存储结构 | L1 档案 JSONB / L2 事实 + 向量 / L3 结构化日志 / 待办独立表 | `models/`, `alembic/versions/` |
| 记忆检索策略 | 距离阈值召回而非纯 top-k；续问句补一路上文 query；档案全量；日志只走工具 | `core/memory/facts.py`, `core/memory/query.py` |
| 上下文管理 | 三段式压缩 + 会话滚动摘要 + 缓存友好的 message 排序 | `core/agent/context.py` |
| Token 控制 | 预算估算 + 历史 token 上限 + 每轮日志记录预算构成 | `core/agent/context.py` |
| 工具注册与路由 | 装饰器注册表 + JSON Schema 校验 + 快路径绕过模型 | `core/tools/registry.py`, `core/agent/fast_path.py` |
| 工具稳定性 | 单工具超时、异常隔离、结果结构化 | `core/tools/registry.py`, `core/agent/loop.py` |
| 兜底与降级 | L0–L5 六级阶梯，最后一级不依赖模型 | `core/agent/loop.py`, `core/agent/rule_fallback.py` |
| 不稳定输出处理 | JSON 解析容错 + 判定调用重试 + 置信度阈值丢弃 | `core/llm/judge.py`, `core/memory/extractor.py` |
| 用户数据隔离 | PostgreSQL 行级安全 + 三角色分离 + JWT | `scripts/init_db.sql`, `core/database.py` |
| 接口设计 | REST + SSE 流式，OpenAPI 自动生成前端类型 | `api/v1/` |
| 生成中断 | 热路径纯内存查询；跨进程靠 PG LISTEN/NOTIFY 广播 | `services/interrupt.py` |
| 系统可扩展性 | 新增本地工具=新增一个文件；双向 MCP：能接别人的 server，也能被别人当 server 用 | `core/tools/registry.py`, `core/mcp/` |
| 可扩展性 | 新增工具一个装饰器；新增记忆类型一个 job handler | `core/tools/registry.py`, `core/jobs/worker.py` |
| 输出安全 | 内部标识符三层拦截：提示禁令 + 反馈用人话 + 流式脱敏 | `core/agent/scrub.py`, `core/agent/glossary.py` |
| 测试与质量 | 970 个测试，93% 覆盖率，迁移往返验证 | `tests/` |

---

## 记忆系统设计

### 五类记忆，各有各的通道

| 层 | 存什么 | 写入时机 | 如何进入 prompt |
|---|---|---|---|
| **L1 档案** | 身高体重年龄、目标、活动系数、伤病、忌口 | 工具显式写入，敏感字段先征询 | 每轮全量注入 |
| **L2 事实** | 稳定的用户特征（偏好、场景、成绩、限制） | 回复后异步抽取 | 按向量相关度召回，有距离阈值 |
| **L3 日志** | 训练记录、体重曲线 | 工具写入（含快路径） | **永不注入**，只能由工具聚合查询 |
| **会话摘要** | 计划前提、中途调整、已给过的结论、被否掉的方案 | 回复后异步增量摘要 | 仅当压缩确实丢弃了历史时注入 |
| **待办** | 助理提出的、用户要去做的事 | 回复后异步抽取 | **永不注入**，只在面板消费 |

L3 不进 prompt 是有意的：日志是会无限增长的时间序列，塞进上下文既爆 token 又
让模型去做它算不准的聚合。待办也不进 prompt——否则模型会把自己上一轮的建议
当成既定事实复述。

会话摘要是**会话级**的，L2 事实是**用户级**的：前者随会话结束就不再增长，
后者跨会话累积。判据也不同——摘要收"这次讨论的来龙去脉"，L2 只收"跨会话
仍然成立的稳定特征"。混在一起会让 L2 被大量临时上下文污染。

### 写入时机：宁可漏记，不可错记

抽取在回复发送之后异步执行，用户无感知。三道闸门：

1. **置信度阈值**：低于 0.7 直接丢弃（`extractor.py` 的 `MIN_CONFIDENCE`）。
2. **中断不抽取**：用户点了停止的回合，一条抽取任务都不投递。从半句话里抽长期
   事实会污染记忆库，抽待办更糟——会变成用户根本没读完的待跟进事项。
3. **敏感字段先征询**：模型推断出的目标、伤病不直接落库，先返回确认话术。
   `update_profile` 工具的 `confirmed` 参数控制这个语义。

### 检索策略：阈值而非 top-k

```
MAX_RECALL_DISTANCE = 0.55   # 召回"相关内容"
SIMILARITY_THRESHOLD = 0.35  # 冲突消解判定"同一件事"
CANDIDATE_DISTANCE = 0.45    # 待办判重的候选粗筛门
```

只取 top-k 会有个隐蔽问题：没有阈值时，**无关记忆也必然会占满 k 个位置**。
实测相关事实的余弦距离约 0.31，无关内容从约 0.69 起，中间有明显间隔，
所以 0.55 能干净切开。

### 续问句的召回：上文作为独立第二路

阈值切得再准也有个前提——query 得有信号。多轮里用户的话天然残缺，
「那个周三换一天行吗」要跟「每周三晚上固定加班」对上，但"周三"被
"换一天行吗"稀释，距离 0.576 刚好越过阈值线，召不回。

直觉方案是把上文拼在当前句前面合成一条 query。实测证明这是错的：

| 当前句 | 原句单路 | 拼接单路 | 原句 + 上文双路 |
|---|---|---|---|
| 那午饭呢？ | **0.479 ✓** | 0.641 ✗ | **0.479 ✓** |
| 那有什么不能放的吗 | 0.718 ✗ | 0.685 ✗ | 0.718 ✗ |
| 这样安排我肩膀受得了吗 | **0.469 ✓** | 0.536 ✓ | **0.469 ✓** |
| 那个周三换一天行吗 | 0.576 ✗ | **0.455 ✓** | **0.470 ✓** |
| **命中** | **2/4** | **2/4** | **3/4** |

看第一行：「那午饭呢？」原句 0.479 本来召得回，拼上上文变 0.641 反而召不回。
短句里关键词的权重占比极高（"午饭"几乎就是整句），掺进上一轮几十个字会把它
稀释掉。拼接是在用"补充信息"换"信号强度"，而这笔交易并不总划算——它换回
一个、又赔掉一个，净收益零。

所以上文作为**独立的第二路 query**，取两路最小距离。这样原句那一路始终在，
**严格不劣于**原来的单路召回：只可能多召回，不可能少召回。这个性质比多命中
一条更重要——优化召回不该以在别处引入回归为代价，它被钉成
`test_facts.py::TestMultiQueryRecall::test_adding_query_never_loses_recall`。

两路共用一次批量 embedding 调用，不增加网络往返；只在句子确实依赖上文时
才开第二路（`core/memory/query.py` 的 `is_referential`），语义自足的句子
保持单路，避免把无关事实拉进阈值内。

第二行两种方案都救不了（"不能放的"→"不吃香菜"语义跳跃太大），那需要 LLM
query 改写，而这条召回在用户等回复的主链路上，不适合再插一次同步模型调用。

### 冲突消解：旧事实不删，用失效指针串联

新事实与已有事实距离低于 0.35 时，调 LLM 判定四种关系之一：

| 关系 | 处理 |
|---|---|
| `supersede` | 新的与旧的矛盾 → 旧的 `superseded_by` 指向新的 |
| `update` | 同一件事、新的更完整 → 同上 |
| `duplicate` | 完全同义 → 不新增 |
| `independent` | 两件事 → 各自保留 |

物理上不删除，便于追溯助理当初为何做出某个判断。用户主动删除才真删。

### 待办判重：为什么向量单独不够

待办的去重比事实更难，因为教练建议高度模板化。实测同一条卧推加重建议
与其他建议的余弦距离：

| 距离 | 内容 | 应判定 |
|---|---|---|
| 0.088 | 卧推工作重量提到 82.5kg | 重复 |
| 0.129 | 把卧推的重量加上去到 82.5 | 重复 |
| 0.254 | 把**深蹲**工作重量加到 120kg | **不同** |
| 0.314 | 下周把卧推加到 82.5 **公斤** | 重复 |
| 0.742+ | 蛋白补到 140g / 每晚早睡 / 买乳清蛋白 | 不同 |

关键是 **0.254 < 0.314**：「同模板不同动作」比「同动作不同说法」更近，两类样本
区间重叠，**不存在能分开它们的阈值**。误判方向也很糟——深蹲的建议会被卧推的
悄悄吞掉，用户永远不知道助理提过。

所以向量只负责把 0.742 那一档排除掉（`CANDIDATE_DISTANCE = 0.45`），剩下的
候选交给 LLM 判定同异。这几个实测数字在 `tests/test_actions.py::TestThresholdGeometry`
里被钉成断言，谁想把阈值收紧当判重线用会先让测试失败。

待办判重只跟未结项比较：`done` 代表已完成，同类建议下个周期再提是合理的；
`ignored` 代表用户明确不要，必须参与判重，否则再提就是骚扰。

### 判定调用很贵，所以要缓存

实测单次判定 **2.8–4.3 秒**（真实网关），而 `judge_json` 内部最多重试 3 次。
一轮抽 3 条待办、每条 3 个候选就是 9 次判定，接近 30 秒。重复的部分不少：
job 失败后按 30/120/600 秒退避重跑会把整批重新判一遍，同一批内候选高度重叠，
用户连续几轮聊同一个动作时也会反复比对同一条已有项。

判定对同一对输入是稳定的（纯映射，不依赖库里的当前状态），所以缓存是安全的。
实测效果：

```
第一遍（全部未命中）  4283 ms + 2785 ms + 3792 ms = 10.86 s
第二遍（退避重试）    0.04 ms + 0.01 ms + 0.00 ms =  0.0001 s
并发同一对 × 4        只发 1 次调用（未去重会是 4 次）
```

几个刻意的选择：

| 决策 | 理由 |
|---|---|
| 进程内 LRU，不用 Redis / 落库 | 判定结果没有持久化价值，worker 重启后重判一次只是多花几秒。为省几次调用引入部署依赖或加一张表不值得 |
| 自己写而不用 `functools.lru_cache` | 它不支持协程——缓存的是尚未 await 的 coroutine，第二次命中拿到的是**已消费过的** coroutine，await 会抛 `cannot reuse already awaited coroutine` |
| 并发同 key 用 Future 等待 | 否则同一对输入的并发请求各发一次，几秒的调用打三次换同一个答案 |
| **失败不缓存** | 判定失败往往是暂时的（空输出、限流），缓存失败会把一次偶发故障固化成永久故障 |
| key 保留方向 | `supersede` 的语义是"旧的失效"，把两个方向当成同一个 key 会让缓存返回反向结论，新记忆被旧记忆顶掉 |
| 两个判定点各一个实例 | key 空间不同，混用会互相挤占容量，也让命中率没法分开看 |

另外加了一道**逐字相同短路**：文本完全一样就直接判重，不问模型。这在 job
退避重试里很常见——同一条建议被重新抽出来、和上次写进去的那条逐字比对。
让用户看到两条完全一样的待办不是"保守"，那是 bug。

worker 空闲时周期性打一行命中率（`判定缓存 待办判重={...} 事实消解={...}`）
——没有这个数字就只能猜这层缓存有没有用。

### 可追溯、可编辑、可删除

- 事实、待办都记录来源消息 id，面板上可以点回原对话
- 档案在面板里表单式直接编辑，改完回写
- 事实和待办都能删除
- 待办额外支持三态流转：`pending` / `done` / `ignored`，可以重新打开

---

## 上下文管理与 Token 控制

### message 排序即缓存策略

Prompt 缓存是**前缀匹配**，前面变一个字节后面全部失效。所以顺序固定为：

```
1. 系统提示        ← 恒定，作为缓存前缀
2. 档案 + 今天日期 + 召回事实   ← "今天是几号"每天变，必须放在缓存断点之后
3. 压缩后的历史
4. 当前用户消息    ← 永远最后
```

把 `今天是 X 月 X 日` 写进系统提示是个常见错误：它每天变一次，会让整个前缀
每天全部失效。

### 三段式历史压缩

`compress_history()`：最近 `AGENT_HISTORY_WINDOW` 轮原样保留，更早的在超出
3000 token 预算时从最旧开始丢弃。数据库层另有一道 LIMIT（`HISTORY_FETCH_LIMIT`），
避免长会话每轮把全部消息搬进内存再逐条估算 token。

### 被丢弃的部分不能就这么消失

"敢丢是因为该记的已经进 L2 了"只在 L2 的判据范围内成立。L2 存的是**用户明确
说过的稳定事实**（不吃香菜、右肩有伤），而长对话里丢掉的往往是另一类：

- 这个计划当初按什么前提排的（82kg、TDEE 2770、8 周）
- 中途因为什么调整过（第三周说出差，训练挪到周末）
- 助理已经解释过、不必重复的结论
- 用户明确否掉的方案

这些既不是稳定事实（会随计划变，写进 L2 还会触发冲突消解），也不是训练日志
（L3 只收结构化记录），**两条通道都不收**。丢了之后模型会重新问用户已经说过
的话，或者忘记某个约束又提一遍被否掉的方案。

所以补第三条通道：会话滚动摘要（`core/memory/summary.py`）。

| 决策 | 理由 |
|---|---|
| **异步生成**，worker 在回合结束后更新 | 这条路径在用户等回复上，不能加同步 LLM 调用。代价是摘要滞后一轮，而滞后的那轮恰好还在保留窗口里，不构成缺口 |
| **增量**，用 `covered_seq` 记录覆盖到哪条 | 否则每次要拿全部历史重摘一遍，既浪费又让摘要随调用次数漂移 |
| **只在压缩确实丢了东西时才注入** | 没丢就注入是白烧 token，而且摘要与还在上下文里的原文重复，模型会在两份说法之间摇摆 |
| **只在会话长到可能丢东西时才投 job** | 每轮都投等于给每次对话白加一次 LLM 调用 |
| 注入时标注「比原文更旧，冲突以原文为准」 | 摘要天生滞后；不标注模型会把里面过时的中间结论当成现状复述 |
| 硬截断到 600 字 | 模型经常无视长度要求，不截断摘要会一轮轮膨胀到比它省下的历史还长 |
| 空输出抛 `LLMError` 交队列重试 | 与事实抽取同一个坑：推理模型思考预算耗尽时返回 200 + 空正文，当成"没什么可摘要的"会让功能静默退化成"从不摘要" |

中断的回合不投摘要任务——和抽取任务同理，半句话会把没说完的结论写成既定事实。

**三个机制缺一个，其余就跛脚**：只压缩不记忆会失忆，只记忆不压缩会爆上下文，
只丢不摘会忘记计划的前提。

### 预算可观测

每轮日志记录一行预算构成，便于定位是哪一块在膨胀：

```
上下文预算 total=385 system=342 profile=25 facts=0 summary=0 history=6
```

估算用 `estimate_tokens()`（中文约 1 字 1 token，其余约 4 字符 1 token）。
只用于预算控制，精确值由 API 返回的 `usage` 记录到 `llm_usage` 表。

---

## 工具调用

### 铁律：所有计算走工具

模型算 TDEE 会错，算营养素克数会错，算「距目标还有多少天」更会错，而且错得
很自信。在健身场景这是致命的——吃错热量，一个月白练。

所以划一条硬线：**模型只负责理解意图、编排工具、组织语言；所有数字来自
`core/domain/` 下的确定性纯函数。** 这不是等它错了再兜底，而是从一开始
不给它算错的机会。

### 16 个工具

| 类别 | 工具 |
|---|---|
| 计算 | `calc_energy_baseline` `calc_macros` `estimate_one_rm` `project_goal` |
| 计划生成 | `plan_strength_cycle` `plan_cut_phase` `plan_meals` `check_plan_conflict` `get_plan_detail` `get_plan_day` |
| 记录（写 L3/L1） | `log_workout` `log_body_metric` `update_profile` |
| 查询 | `query_workout_history` `query_body_trend` `search_food` |

### 注册与路由

新增一个工具只需要一个装饰器——注册表自动生成 JSON Schema 交给模型，
并在调用时校验参数：

```python
@tool(name="calc_macros", label="计算营养素分配", description="...")
async def calc_macros(args: CalcMacrosArgs, ctx: ToolContext) -> dict:
    ...
```

`label` 是这个工具的**用户可见**说法，必填。`name` 只在系统内部流转：前端状态行、
失败反馈、流式脱敏一律用 `label`（见下节）。

### 挂载外部 MCP 工具

想让助理用上本仓库没写的能力（查天气、读日历、搜文献），不必写代码——配置一个
MCP server 即可。格式与 Claude Desktop / Cursor 一致，可以直接把现成配置贴过来：

```bash
MCP_SERVERS={"mcpServers":{"time":{"command":"uvx","args":["mcp-server-time"],"readonly":true}}}
```

接进来之后，MCP 工具与本地工具**在系统里完全等价**：同一套 schema 给模型、同一套
参数校验、同一套失败反馈、同一套流式脱敏、同一套降级策略。**Agent 循环一行都没改。**

脱敏能自动覆盖是因为词表从注册表现取（`scrub.py: build_vocab`）——所以
"新增工具零改动"这条性质延伸到了外部工具，不需要在脱敏层另立一张表。

几个必须在桥接层解决的问题：

| 问题 | 处理 |
|---|---|
| 名字冲突 | 加 `mcp__<server>__` 前缀。双下划线分隔，与工具名自身的单下划线区分，才能可靠还原来源 |
| **MCP 没有 label 这个概念** | 用 description 首句生成，放不下时退回工具名的可读形式（`get_current_time` → `get current time`）。缺了用户会看到"正在 mcp__weather__get_forecast…" |
| **MCP 不表达副作用** | `readOnlyHint` 是后加的可选注解，绝大多数 server 不提供。默认按写操作对待，要标只读必须配置里显式声明——降级层据此决定能否重试，误判成只读会导致重复写入 |
| Schema 不能直接用 | 注册表要 Pydantic 模型，MCP 给裸 JSON Schema。包一个透传模型：**交给模型的是 server 的原始 schema**（用 Any 拼的那个没类型没枚举值，模型填不对参数），校验只查必填项 |
| **超时不能与本地工具共用** | 本地工具全是纯计算或一次查库，3 秒很宽松；MCP 在另一个进程还要走网络，同一个 3 秒会让一大半正常调用变成超时——而那种超时看起来和真故障一模一样。`ToolSpec.timeout_s` 按工具区分 |

stdio 上的 JSON-RPC 有两个坑，都会表现为难定位的随机故障：

- **必须用 `readline()` 而不是 `read(n)`**：stdout 是流，不保证一次读到整行。
  粘包后 `json.loads` 报 "Extra data"，完全指不到真正的原因。
- **stderr 必须单独接管**：MCP server 常把日志写 stderr，不读它管道缓冲区满了
  之后子进程会**阻塞在写日志上**，表现为工具调用随机超时。

失败一律降级：npx 没装、网络不通、server 自己崩了都很正常，任何单个 server 的
失败只记日志然后跳过——用户宁可少几个工具，也不能因为一个可选的外部依赖连不上
就打不开页面。配置写错（JSON 笔误）同样只告警，不阻塞启动。

**已知限制（有意的取舍）**：

- 镜像只预装了 `uv`（约 40MB），所以 `uvx` 系 server 开箱可用；`npx` 系更常见，
  但 Node + npm 要 200MB 以上，为一个可选功能让镜像大三倍不划算。需要的话在
  `backend/Dockerfile` 补一行 Node 安装。
- 只实现 stdio 传输，没做 HTTP/SSE。绝大多数现成 server 以 stdio 分发，而 HTTP
  形式要额外部署服务、配鉴权、管生命周期。传输层在 `MCPClient` 里是独立一段，
  将来加 HTTP 只需换掉收发。
- **MCP server 在进程外，拿不到 `ToolContext`**，因此不受 RLS 保护。它能看到的
  只有模型传给它的参数，但那些参数可能含用户数据——挂载不受信任的 server 等于
  把这部分数据交出去。这是 MCP 的固有属性，不是这里的实现缺陷。

### 反向：把这个助理挂给别的 MCP 客户端

上一节是「我接别人的」，这一节是「别人接我的」——两个方向是完全独立的两套实现，
消息名相同但角色相反（一个主动发请求，一个被动读请求），代码几乎没有可复用的。

配好之后可以在 Claude Desktop / Cursor 里直接问「我上次卧推多少」，数据仍然在
这个系统里，只是多了一个入口：

```json
{
  "mcpServers": {
    "fitmind": {
      "command": "python",
      "args": ["-m", "app.mcp_stdio"],
      "cwd": "/绝对路径/backend",
      "env": {
        "FITMIND_TOKEN": "<登录后拿到的 access token>",
        "DATABASE_URL": "postgresql+asyncpg://fitness_app:***@localhost:5433/fitness",
        "JWT_SECRET": "<与服务端一致>"
      }
    }
  }
}
```

`DATABASE_URL` 要指向**真正存着你数据的那个库**。用 Docker 起的话，容器内是
`postgres:5432`，从本机连要用映射到宿主机的端口（本项目默认 `5433`）——填成
`5432` 大概率连到本机另一个库上。填错不用担心记不住，启动时会拦下来，见下文第 4 条。

#### 四个必须处理好的问题

**1. stdout 是协议通道，一行日志就能毁掉它。**

MCP stdio 用 stdout 逐行传 JSON-RPC，而 `logger` 默认写 stdout，且 `load_tools()`
在注册工具时就会打一行 INFO。那一行会直接混进协议流，客户端报 "unexpected token"
——而真正的原因是一条毫不相关的日志。

所以 `route_to_stderr()` 必须在**导入任何业务模块之前**执行。`app/mcp_stdio.py`
里那几个 import 的位置是刻意的，不是没整理。

**2. 身份从哪来。**

MCP stdio 是单用户本地进程模型，没有登录概念。而这个系统里每个工具都要
`user_id`——数据隔离靠 PG RLS 强制，没有它一行都读不到。

解法是从 `FITMIND_TOKEN` 读一个已签发的 access token，启动时换成 `user_id` 并绑定
RLS 上下文（与 `api/deps.py` 同一套做法）。每次工具调用开一个新 session：MCP server
是长驻进程，一个 session 用几小时会累积未回滚的事务状态，而这里没有 Web 框架的
请求边界来兜底。

**3. 默认只暴露只读工具。**

理由不是"怕出 bug"，而是风险与收益不对称：

- 收益侧：外部客户端要的是「查我的数据」。写操作在对话式界面里本来就该由本系统
  自己的 UI 承担——那里有确认流程、有卡片反馈、有撤销的余地。
- 风险侧：token 明文躺在客户端配置文件里。泄漏后果是「数据被改」还是「数据被读」，
  差别很大。

白名单按 `ToolSpec.readonly` 自动筛，不手写名单——手写的迟早和新增工具脱节。
要开写操作得显式设 `FITMIND_MCP_ALLOW_WRITE=1`，启动时会打一条警告。

**4. 连错库必须在启动时炸掉。**

这是我自己踩的坑，也是这个功能里唯一一个**会让系统说假话**的失败模式。

这个入口是独立进程，`DATABASE_URL` 自己配。填错库之后没有任何一步会报错：token
用 `JWT_SECRET` 解得开（密钥和库是两个独立配置）、RLS 上下文绑得上、SQL 也执行成功
——只是每张表都返回零行。

于是客户端的模型看到空结果，把它转述成「你还没有任何训练记录」。**这比崩溃危险得多**：
它是一句听起来完全正常的话，用户没有任何线索知道它是错的。我第一次验证时就以为
是 RLS 出了 bug，查了半天才发现是自己连错库。

所以 `preflight()` 在 `serve()` 之前确认「这个库里真的有这个用户」，不满足就退出：

```
$ FITMIND_TOKEN=... python -m app.mcp_stdio     # DATABASE_URL 指向了错的库
退出码 3
ERROR - token 对应的用户在数据库 localhost:5432/fitness 里不存在（user=bf48664a-...）。

最常见的原因是连错库：这个入口是独立进程，它的 DATABASE_URL 必须指向真正存着
你数据的那个库。容器内是 postgres:5432，从本机连要用映射到宿主机的端口。

这里选择直接退出，是因为放行的后果更糟——每个工具都会返回空结果且不报任何错，
客户端的模型会把它转述成「你还没有任何记录」，一句听起来完全正常的假话。
```

几个刻意的选择：

| 处理 | 原因 |
|---|---|
| 诊断里带上库的 `host:port/dbname` | 不说清「你现在连的是哪个库」，用户只能靠猜 |
| 但抹掉密码 | stderr 会被 MCP 客户端收进它自己的日志文件 |
| 「连不上」「没有 users 表」「没这个用户」分成三句话 | 三者都表现为查不到，但一个要改连接串、一个要跑迁移、一个要改库 |
| 退出码分 2（凭据）和 3（连错库） | 排查方向完全不同 |
| 自检有 10 秒超时 | 挂死比报错更难查——客户端只显示「server 未启动」，没有任何输出可看 |
| token 无效时提示「和已过期报错相同」 | `JWT_SECRET` 与签发方不一致时，报错和过期一模一样，两个都得对 |

#### 另外几个细节

| 处理 | 原因 |
|---|---|
| 不转发外部 MCP 工具 | 它们是这个进程从别的 server 借来的，再转出去会形成一条谁也说不清的调用链，而且那些 server 的副作用我们无从判断 |
| 「不存在」与「不允许」回同一句话 | 区分了就等于告诉调用方"这个工具存在但你不能用"，是一条不必要的信息泄漏 |
| 参数错误回 `isError` 而非 JSON-RPC error | 前者是"工具执行失败"，客户端的模型会看到并自行改参数；后者会被当成传输层故障 |
| 异常只回类型名 | 异常里可能带 SQL 片段、表名、连接串。全貌留在日志 |
| 单条消息失败不退出进程 | 退出会让客户端失去所有工具，而问题可能只是一条畸形请求 |
| 响应必须 `flush()` | stdout 接管道时是块缓冲的，不 flush 响应会攒到进程退出——客户端表现为"发了请求没有任何回应"然后超时 |
| 用 `StreamReader` 而非 `input()` | 后者阻塞事件循环，会让工具里的数据库查询永远不返回 |

**安全边界**：这个入口拿着 token 就等于拿着那个用户的身份。它适合「自己在本机把
自己的数据接进常用客户端」，不适合分发给他人。要做多用户场景需要 MCP 的 HTTP
传输 + OAuth，那是另一个量级的工作。

### 稳定性处理

- **快路径**：`卧推 80kg 5x5` 这类明确的记录意图由正则直接命中工具，
  全程不碰模型，几十毫秒返回。既省钱又快，还绕开了模型可能的误解。
- **单工具超时**：`AGENT_TOOL_TIMEOUT_S`（默认 3 秒），超时不拖死整轮。
- **超时后必须回滚**：`asyncio.wait_for` 超时会取消协程，而被取消的协程可能
  正卡在一条 SQL 上。asyncpg 连接被留在"事务已开始、语句未完成"的状态，
  之后对**同一个 session** 的任何操作都抛 `PendingRollbackError`。影响远超
  "这一个工具没算出来"——本轮剩下的工具全失败，连收尾时把助理消息落库的那次
  commit 也失败，用户的整个回合凭空消失。而超时被转成一句温和的文本反馈，
  把这个故障完全掩盖了。所以超时与异常分支都显式 `rollback()`；回滚本身
  失败时（连接真断了）改口让模型停手，别再调工具。
  写入类工具各自 `commit`，所以回滚不会撤销已记录的训练。
- **参数校验**：Schema 不匹配抛 `ToolValidationError`，把错误回喂给模型让它改，
  而不是直接失败。校验在执行之前发生，没碰数据库，因此**不**回滚——多余的
  回滚会把同一事务里前面工具做的事撤掉。
- **幂等**：`client_message_id` 去重，同一条消息重复提交只处理一次；
  日志写入也做了重复检测。

### 同轮多工具为什么不并发

`loop.py` 里同一轮的多个工具是**串行**执行的。看起来是个明显的优化点，
实测后否决：

| 工具 | 单次耗时 |
|---|---|
| `calc_energy_baseline` | 0.0018 ms |
| `calc_macros` | 0.0036 ms |
| `estimate_1rm` | 0.0011 ms |
| `project_goal` | 0.0022 ms |

16 个工具里 12 个都碰数据库，而它们共享同一个 `AsyncSession`——asyncpg 连接
不允许两个协程同时使用，并发这些工具会直接炸 `another operation is in
progress`。真正能安全并发的只剩上面 4 个纯计算工具，而它们耗时在**微秒级**，
省下来的时间比测量噪声还小，模型单轮响应却是秒级。

要让并发有意义，得给每个工具单独开 session——那会引出跨 session 的事务边界与
RLS 绑定问题，为了微秒级收益不值得。**这条路记在这里，是为了让下一个想优化
它的人不必重新测一遍。**

### 内部名字不出现在回复里

内部标识符曾经从三个口子漏到用户眼前：模型自己写出 `` `plan_strength_cycle` ``；
注入提示的档案是 JSON，`weight_kg`、`cut`、`office` 被模型照抄；前端状态行对
未登记的工具直接显示「正在调用 calc_energy_baseline…」。

用户看不懂这些名字，看到只会觉得系统在漏东西。所以堵在三层，缺一层都不够：

| 层 | 做法 | 位置 |
|---|---|---|
| 输入侧 | 档案按中文名 + 中文取值渲染，模型手上没有内部名字可抄 | `core/agent/context.py`, `core/agent/glossary.py` |
| 反馈侧 | 工具失败反馈说 `label` 不说 `name`，并明写"不要转述"；征询语、降级文案同样用中文字段名 | `core/agent/loop.py`, `core/agent/rule_fallback.py` |
| 输出侧 | 流式脱敏：把词表内的标识符换成人话，词表外的记日志 | `core/agent/scrub.py` |

系统提示里也有一条硬性规则禁止这么说，但提示只是"要求"——**"绝不泄漏"是产品
承诺，得有一层确定性代码来兑现**，那一层就是输出侧脱敏。

脱敏的难点全在流式：`log_workout` 会被切成任意几段吐出来，逐段替换必然漏。
做法是**先按边界扣住可能没写完的尾巴，再替换已经确定的部分**——顺序反了就会把
`log_workout_v2` 的前半截当成 `log_workout` 换掉。回合结束必须 flush，
否则以标识符结尾的回复会被吞掉一截。

词表从注册表现取，新增工具仍然是"新增一个文件、别处零改动"；`@tool` 的 `label`
必填，漏了直接报错，而不是悄悄退化成显示内部名。

---

## 不确定输出的兜底与降级

### 六级降级阶梯

```
L0 正常     主模型 + 完整工具
L1 重试     同模型退避重试
L2 换模型   切到 LLM_FALLBACK_MODEL
L3 缩范围   只保留核心工具，减少模型出错面
L4 规则兜底 完全不依赖模型，结果来自领域纯函数
L5 诚实失败 明确告知，保留用户输入
```

前端会在回复下方标注本次启用了第几级，不静默降级；被截断的回复同样带提示
（见下文「截断必须让用户看得见」）。

`LLM_FALLBACK_MODEL` 建议选与主模型**不同厂商或架构**的模型，降低同源故障
概率；留空则跳过 L2。

**空输出也算失败。** 推理模型的思考 token 与正文共享 `max_tokens`，预算耗尽时
返回的是 HTTP 200 + 空正文 + `finish_reason=length`，不是报错。当成"这轮说完了"
处理，用户看到的就是工具跑完了、档案也写了，回答却停在"先把你的档案建好"然后
没有下文——比报错更难查。所以 `with_fallback.py` 把"一个字和一个工具调用都没
产出"的尝试判为失败，交给 L1/L2/L3 重试；此时下游还没收到任何内容，重试不会
重复输出。同理，异步抽取拿到空输出时**抛异常交给 job 队列按退避重试**，而不是
当成"这轮没有可记的事实"——后者会让记忆功能静默退化成"从不记忆"。

### 截断必须让用户看得见

"空输出"能靠重试解决，"说了一半"不能——正文已经推给用户了，重发会重复他已经
看见的字。所以这一路只能如实告诉他这句话没说完。

有两种截断，此前**都只写日志**：

| 成因 | 用户看到的 | 该做什么 |
|---|---|---|
| `finish_reason=length`：撞上单次输出上限 | 一句半截话 | 回复「继续」接着写 |
| Agent 轮次用尽（`AGENT_MAX_TURNS`） | 话说完了但事情没做完 | 把问题拆成几步分别问 |

不提示的后果比报错更糟：报错至少能看出出了问题，而半截话**看起来就像模型答完
了**，用户会基于不完整的信息去训练。

两种成因分开报而不是合并成一句"回答不完整"——它们能做的事相反。第二种情况下
用户说「继续」还是会撞上同一个轮次上限，把他引向无效操作。

截断标记同时**落库**，不只发 SSE：断线重连走 `list_messages` 取回历史，只发不
存的话重连后那句半截话又变得"看起来正常"了。正常消息不带这个键，避免每条消息
的 meta 里多两个 `false`。

### 判定调用的重试

这是一个踩过坑才发现的问题：**推理模型在 token 预算不足时返回空字符串，
而不是报错。** 实测同一对输入：

```
max_tokens=  64 → ''
max_tokens= 128 → ''
max_tokens= 512 → '{"relation": "update"}'
max_tokens=1024 → ''
max_tokens=2048 → '{"relation": "update"}'
```

注意 1024 返回空而 512 有结果——**思考长度有方差，调大数字治不了本，必须重试。**

危险在于失效是静默的：调用方通常有个 `except` 兜底，于是功能悄悄退化成
「从不判定」，日志里一行报错都没有。`core/llm/judge.py` 把重试、空输出检测、
JSON 解析容错收在一处，记忆冲突消解和待办判重共用。判定彻底失败时抛
`JudgeError` 而不是返回默认值，让调用方显式选择降级方向。

### 抽取输出的容错

- 代码块围栏（```json）自动剥离
- **空输出**（思考预算耗尽）抛异常，由 job 队列退避重试——它和"确实没有可记的
  事实（`[]`）"是两件事，混为一谈等于静默失效
- JSON 解析失败记警告后跳过本次，不让异步 job 炸掉
- 非法 category 落到 `general`，置信度裁剪到 [0, 1]
- 单轮待办上限 3 条：模型无视 `max_items` 是常事，一轮冒出十条待办面板就没法用了

---

## 生成中断

用户点「停止」时前端打一个 interrupt 请求，Agent 循环在推每个事件之前查一次
旗子，见旗即收尾。中断实现为「**不再消费事件**」，`AgentLoop` 零改动。

**键是 `conversation_id` 而不是 message id**：前端在收到 `message_start` 之前
就可能点停止（手快，或首个 token 迟迟不来），那时它手上只有 `conversation_id`。
用 message id 会漏掉最该中断的那一段等待。

### 热路径必须是内存查询

`is_requested` 的调用频率是**每个 token 一次**。做成数据库查询意味着每个 token
一条 SELECT——这是热路径上最不该出现的东西。所以旗子始终存在进程内存里。

但只有内存就无法跨进程：多 uvicorn worker 或多副本部署时，interrupt 请求落到
另一个进程就失效了——界面显示「已停止」，实际还在烧 token。这是最坏的一种失败。

解法是 **PostgreSQL LISTEN/NOTIFY**：写本地 set 的同时广播一条通知，每个进程
的监听器收到后写进自己的 set。热路径成本一点没变，跨进程语义补上了。数据库
已经在那里，不需要引入 Redis。

| 决策 | 理由 |
|---|---|
| 请求走 `await`，清理也走 `await` | fire-and-forget 时进程恰好在返回后关闭（部署、重启）会把通知丢掉 |
| **清理也要广播** | 不广播则另一个进程的旗子一直留着，把该会话的**下一个**回合一启动就杀掉——「上次点了停止，之后第一条消息永远没反应」的跨进程版本 |
| 监听连接记录所属事件循环 | asyncpg 连接绑在创建它的循环上，`uvicorn --reload` 换循环后复用会抛 `future belongs to a different loop`，必须重建 |
| 监听起不来只告警 | 退化成单进程行为（本进程内中断照常工作），比因为一个辅助通道连不上就整个服务起不来要好 |
| 脏 payload 不打死监听器 | 那会让整个进程失去跨进程中断能力 |

### 中断后的收尾

- 已执行完的工具写入**不回滚**——记进去的训练不该凭空消失，只停后续步骤
- **不投递抽取任务**：从半句话里抽长期事实会污染 L2，抽待办更糟——半截建议会
  变成一条用户根本没读完的待跟进事项
- `interrupted` 与 `done` 同等纳入历史：用户已经看见那半句，模型也必须看见，
  否则下一轮会跟屏幕上还挂着的半句自相矛盾
- 脱敏缓冲要 flush：用户已经看见的半句必须和落库的一致
- 前端 `stop()` 顺序为「先 POST interrupt 插旗 → 再 abort → 再本地标记」，
  反序会造成假中断

---

## 计划表导入

用户手上的计划多半是一份 Excel——健身博主发的模板、教练给的表格。让他对着
表格一天天口述给助理是荒谬的：一份 4 周计划有 28 天 × 6 个字段，任何人都会在
第三天放弃。

上传 xlsx，解析成结构化计划落进 `plans`，之后可以直接问「今天吃多少」。

### 真实文件是脏的

拿来做验证的样例（网上流传的碳循环模板）第 1 周有 **9 列**而其余三周是 8 列，
多出来那列夹在"一"和"二"之间，值是 138.4 / 111.2 / 53.4 / 1491——像是有人在
旁边试算了一版没删掉。

按"第 2..8 列就是周一到周日"硬读，第 1 周会**整周错位一天且不报错**：数字都在
合理范围内，导入完看起来一切正常。所以列位置一律由**表头的星期字符**决定，
认不出的列直接丢掉。

解析中踩到的另外两个坑，都钉成了回归测试：

| 现象 | 根因 |
|---|---|
| 热量与缺口两列同时变 `None` | `"缺口/盈余(kcal)"` 含 `kcal`，被热量规则先匹配，覆盖掉真正的热量值。行标签匹配改为按特异性排序 |
| 全部 28 天的缺口被当脏数据丢弃 | 缺口是热量差，减脂计划里本来就是负数。上下界统一按 0 起判是错的 |

### 预览与确认分两步

导入会改两样东西：新增一份计划，以及**可能覆盖档案里的身体数据**。第二样是
危险的——网上流传的模板都带着原作者的参数（样例是 91kg / 180cm / 25 岁），
直接写进去等于把别人的身体数据变成用户自己的，而档案每轮都注入 prompt，
之后所有热量计算都会按错的体重算。

所以上传只解析、不落库，把整张表摆出来让用户核对。这也是数据进库前唯一能
发现解析错位的机会。

| 决策 | 理由 |
|---|---|
| 档案更新默认**不勾** | 默认值的方向比它省下的一次点击重要得多 |
| `target_kg` / `goal` **完全不导入** | 那是"用户想要什么"，不该由一份下载来的模板决定 |
| 预览结果不缓存、确认时重传文件 | 缓存需要带过期的临时存储，还要处理"用户停留半小时后才点确认"；重传几十 KB 成本几乎为零 |
| 幂等按**内容指纹**而非文件字节 | 同一份计划另存一次字节就变了，而内容一模一样。用户不会理解为什么又多了一份 |
| 沿用 `plans.type="cut"`，靠 `payload.source` 区分来源 | 另起一个 type 会让每个读计划的地方都要记得查两种——而 `get_plan_detail` 里已经有一处漏查了 |

### 导入之后

`get_plan_day` 工具让助理能回答「今天吃多少」「这周三练什么」。日期到周次的
换算在纯函数里做，不交给模型——它不知道今天是周几，更不知道计划从哪天开始。

真机验证（今天周四）：

```
用户：我今天该吃多少？练什么？
工具：查询计划当日安排
助理：今天是休息日，不安排训练。饮食上按计划是低碳日：
      热量 1897 kcal，蛋白质 112 g，碳水 90 g，脂肪 121 g
```

与原表第 1 周周四逐格一致。

---

## 用户数据隔离与隐私

### PostgreSQL 行级安全（RLS）

隔离在**数据库层**，不靠应用层每个查询记得加 `WHERE user_id = ?`。
所有租户表启用 `FORCE ROW LEVEL SECURITY`：

```sql
CREATE POLICY tenant_isolation ON <table>
USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
```

请求进来时把 JWT 里的 user_id 绑到会话变量 `app.user_id`。忘记加 WHERE 条件
不会导致数据泄露——策略在数据库侧强制生效。

### 三角色分离

| 角色 | 权限 | 用途 |
|---|---|---|
| `fitness_admin` | 表所有者 | 迁移 |
| `fitness_app` | 受 RLS 约束 | API 服务 |
| `fitness_worker` | `BYPASSRLS` | worker 跨用户领取 jobs |

worker 必须跨用户读队列，所以需要独立角色，但它执行每个 job 时会**显式绑定
该 job 的用户**再操作数据，不是敞开跑。

`tests/test_rls_isolation.py` 专门验证连接池复用时会话变量不泄露——
这类 bug 只在连接被复用时才暴露。

### 其他

- 密码 bcrypt 哈希
- JWT 承载 user_id，`JWT_SECRET` 要求至少 32 字符（配置层强校验）
- 记忆、待办、档案全部用户可见、可删除
- 删除用户级联删除全部数据（外键 `ON DELETE CASCADE`）

---

## API 接口

全部前缀 `/api/v1`，除注册登录外都需要 `Authorization: Bearer <token>`。
完整 OpenAPI 文档在 <http://localhost:8000/docs>。

### 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` | 注册，返回 token |
| POST | `/auth/login` | 登录，返回 token |
| GET | `/me` | 当前用户 |

### 对话

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/conversations` | 新建会话 |
| GET | `/conversations` | 会话列表 |
| GET | `/conversations/{id}/messages` | 历史消息（断线重连用） |
| POST | `/conversations/{id}/messages` | **SSE 流式对话** |
| POST | `/conversations/{id}/interrupt` | 中断当前生成 |

SSE 事件类型：`message_start` `text_delta` `tool_start` `tool_result` `card`
`message_done` `interrupted` `error`。

`tool_start` / `tool_result` 只带 `label`（如 `{"label": "查询食物营养"}`）。
内部工具名和调用参数不出网关——前端拿不到，也就没机会显示成
「正在调用 `search_food`…」。

助理消息**先落库再推流**：客户端断连时服务端跑完仍会落盘，重连是「取回已生成的」
而不是「重新生成」。

### 记忆与档案

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/profile` | 读档案 |
| PATCH | `/profile` | 改档案（字段白名单校验） |
| GET | `/memories` | 事实列表 |
| DELETE | `/memories/{id}` | 删除事实 |

### 待办

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/action-items?status=pending` | 列表，可按状态过滤 |
| POST | `/action-items` | 手动新建（不参与判重） |
| PATCH | `/action-items/{id}` | 流转状态 `pending`/`done`/`ignored` |
| DELETE | `/action-items/{id}` | 删除 |
| GET | `/plans` | 我的计划列表（只回摘要） |
| POST | `/plans/import/preview` | 上传 xlsx，只解析不落库 |
| POST | `/plans/import` | 确认导入，`apply_profile` 决定是否更新档案 |

### 日志与用量

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/logs/workouts` | 记录训练 |
| GET | `/logs/workouts` | 训练历史 |
| GET | `/logs/body-metrics` | 体重曲线 |
| GET | `/usage/summary` | Token 用量与调用统计 |
| GET | `/health` | 健康检查（不带前缀） |

前端类型由 OpenAPI 自动生成，**禁止手改** `web/src/types/api.gen.ts`：

```bash
make gen           # 后端需在运行中
```

---

## 项目结构

```
backend/
  app/
    api/v1/            REST + SSE 端点（auth chat memory actions logs usage）
    core/
      agent/           Agent 循环、上下文组装、快路径、规则兜底、系统提示
      domain/          领域纯函数：能量 营养素 力量 配餐 冲突 推算
      tools/           16 个本地工具 + 注册表
      mcp/             双向 MCP：接外部 server（client）+ 供外部调用（server）
      memory/          L1 档案 / L2 事实 / 检索 query / 会话摘要 / 抽取 / 冲突消解 / embedding
      actions/         待办：抽取 / 判重 / 持久化
      jobs/            PostgreSQL 队列与 worker
      llm/             provider 抽象、备用模型、用量记录、判定调用
    models/            SQLAlchemy 模型
  alembic/versions/    14 个迁移，全部验证过 upgrade → downgrade → upgrade 往返
  tests/               970 个测试
  scripts/             init_db.sql（角色与 RLS）、seed_demo.py、seed_foods.py

web/src/
  app/                 Next.js App Router
  components/
    chat/              对话视图、消息、输入框（含停止按钮）
    cards/             8 类富卡片渲染
    memory-panel/      档案 / 待办 / 数据 / 训练 四面板
  hooks/useSSE.ts      SSE 流式解析
  stores/chat.ts       Zustand 对话状态
  types/api.gen.ts     OpenAPI 自动生成，勿手改
```

八类富卡片：`workout_logged` `metric_logged` `macros` `projection`
`strength_plan` `cut_plan` `meal_plan` `conflict`。未知卡片类型会降级为
可展开的 JSON，不白屏。

---

## 本机开发

要求 Python 3.12+、Node.js 22+、PostgreSQL 16 + pgvector。

```bash
# 数据库（也可以只起 compose 里的 postgres：make db）
createdb fitness

# 后端
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head
.venv/bin/python scripts/seed_foods.py
.venv/bin/uvicorn app.main:app --reload

# 另一个终端：抽取 worker（不起它，待办和长期事实不会出现）
cd backend && .venv/bin/python -m app.core.jobs.worker

# 前端
cd web
npm install
npm run gen        # 后端需在运行中
npm run dev
```

前端默认请求 `http://localhost:8000/api/v1`，可用 `NEXT_PUBLIC_API_BASE` 覆盖。

**注意**：本机开发的库（`.env` 里的 `DATABASE_URL`，默认 `localhost:5432`）
与 Docker 的库（`localhost:5433`）是两套独立数据，迁移要分别执行。

LLM 未配置时不会启动失败——数据库、认证、记录功能正常，对话会走 L4 规则兜底。
启动日志会明确告警。

---

## 测试

```bash
make test          # 970 passed
make cov           # 覆盖率
make check         # 后端测试 + 覆盖率 + 前端 tsc + next build
```

测试需要一个可连接的 PostgreSQL（`tests/conftest.py` 里的默认地址，
或环境变量 `DATABASE_URL`）。测试不依赖 `.env` 文件，也不访问外网——
embedding 与模型调用在单测里都有替身，真实网关只在少数标注过的用例里使用。

当前实测：

| 项 | 结果 |
|---|---|
| 后端测试 | **970 passed** |
| 总覆盖率 | **93%**（3495 statements，261 missed） |
| 领域纯函数模块 | 96–100% |
| 前端 | `tsc --noEmit` 通过，Next.js 16 production build 通过 |
| 迁移 | 14 个，全部验证 upgrade → downgrade → upgrade 往返 |

值得一提的几类测试：

- `test_rls_isolation.py` — 连接池复用时会话变量不泄露
- `test_actions.py::TestThresholdGeometry` — 把「向量单独判不了重」的实测结论钉成断言
- `test_facts.py::TestMultiQueryRecall` — 多路召回「加一路只可能多召回、不可能
  少召回」的不劣性，以及多路只发一次 embedding 调用
- `test_context.py::TestNoDuplicateCurrentMessage` — 本轮用户消息不在 prompt 里出现两遍
- `test_judge.py` — 推理模型返回空输出时的重试行为
- `test_fallback.py` / `test_rule_fallback.py` — 降级阶梯每一级
- `test_chat_interrupt.py` — 中断后不投递抽取任务、历史仍包含被中断的半句
- `test_scrub.py` — 内部标识符被切成任意几段吐出来时仍然脱敏（含逐字节切分）

---

## 环境变量

完整清单见 `.env.example`。必填三项：

| 变量 | 说明 |
|---|---|
| `LLM_BASE_URL` | OpenAI 兼容网关地址 |
| `LLM_API_KEY` | 网关 API Key |
| `LLM_MODEL` | 对话模型名 |

`DATABASE_URL` 和 `JWT_SECRET` 在 `.env.example` 里有可用默认值，
生产环境务必替换 `JWT_SECRET`（`openssl rand -hex 32`）。

其余可调项及默认值：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_FALLBACK_MODEL` | 空 | 备用模型，留空跳过 L2 降级 |
| `LLM_MAX_TOKENS` | 8192 | 单次调用输出上限。**推理模型的思考也算在内**，给小了正文会整段为空；这是上限不是用量 |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | 向量模型 |
| `EMBEDDING_DIM` | 1536 | 必须与向量模型一致，改了要重建索引 |
| `AGENT_MAX_TURNS` | 6 | Agent 循环最大轮数 |
| `AGENT_TOOL_TIMEOUT_S` | 3.0 | 单个工具超时 |
| `AGENT_HISTORY_WINDOW` | 6 | 历史保留轮数 |
| `WORKER_DATABASE_URL` | 空 | worker 专用连接，生产应指向 `BYPASSRLS` 角色 |
| `CORS_ORIGINS` | localhost:3000 | 逗号分隔 |

配置在 `core/config.py` 集中校验，缺失或不合法**启动即失败**，
不会跑到一半才炸。

---

## 已知限制

- 食物营养值用于产品演示，不替代包装标签、营养师或医疗建议。
- 余弦距离阈值基于当前 embedding 模型实测调校，**更换模型后必须重新测量**。
  README 里那几个距离数字就是测出来的，换模型后会失效。
- 续问句的上文补召回只解决"关键词被稀释"那一类。语义跳跃太大的（「那有什么
  不能放的吗」→「不吃香菜」，实测 0.718）两路都召不回，需要 LLM query 改写；
  但那要在用户等回复的主链路上多插一次同步模型调用，暂未做。
- 指代判定是词表规则（`core/memory/query.py`），命中不了的续问句退化成单路
  召回——与改动前行为一致，不会更差。
- 待办判重每条候选多一次 LLM 调用（实测 2.8–4.3 秒），只在向量粗筛命中近邻时
  发生，且结果走进程内 LRU 缓存。缓存是**进程内**的：多副本部署时各自独立，
  worker 重启即失效——重判一次只是多花几秒，不会出错。
- 待办溯源是降级版：目标消息在当前已加载会话里就滚动高亮，不在就展示原文摘录。
  完整的跨会话跳转需要先做会话列表 UI。
- 看门狗按固定阈值（10 分钟）判定卡死，不区分任务类型。如果将来加入正常耗时
  远超十分钟的任务类型，需要改成按类型配阈值，否则它会被误判成卡死并重复执行。
- Docker 首次构建需要访问 Python 与 npm 软件源；模型调用还需要能访问配置的网关。

---

## 文档

- [设计文档](docs/superpowers/specs/2026-08-24-fitness-agent-design.md) — 需求推导与技术选型
- [实现计划 Plan 1](docs/superpowers/plans/2026-08-24-fitness-agent.md) — 领域层、工具、Agent、记忆
- [实现计划 Plan 2](docs/superpowers/plans/2026-08-25-fitness-agent-plan2.md) — 计划工具、前端、交付打磨
