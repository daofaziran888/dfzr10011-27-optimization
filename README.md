# 分布式采集调度引擎

面向大规模数据采集团队的分布式采集调度引擎。覆盖采集节点集群管理、任务分片调度、结果聚合归并、节点健康监控、采集流量均衡、断点续采恢复与全局采集看板的完整分布式采集链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   ├── app/migrations.py     迁移版本与字段变更注册表
│   ├── manage.py             迁移运维命令（预览/迁移/回填/校验）
│   ├── tests/                迁移与容量口径行为测试
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点集群 | `cluster` | 集群节点 | 节点编号、节点地址、节点角色 |
| 任务分片调度 | `shard` | 分片任务 | 分片编号、采集计划、分片范围 |
| 结果聚合归并 | `merge` | 聚合任务 | 任务编号、采集计划、分片总数 |
| 节点状态巡检 | `nodecheck` | 巡检记录 | 记录编号、节点编号、CPU使用率 |
| 采集流量均衡 | `balance` | 均衡策略 | 策略编号、负载算法、权重因子 |
| 断点续采恢复 | `resume` | 断点记录 | 断点编号、采集计划、断点位置 |
| 全局采集看板 | `dashboard` | 看板面板 | 面板编号、面板名称、显示指标 |
| 采集配置模板 | `crawlconf` | 配置模板 | 模板编号、模板名称、目标类型 |
| 采集数据源池 | `datasource` | 数据源条目 | 条目编号、数据源名称、源类型 |
| 采集日志解析 | `logparse` | 日志模板 | 模板编号、日志类型、解析规则 |
| 采集频率控制 | `ratelimit` | 频率规则 | 规则编号、目标站点、请求间隔 |
| 采集代理路由 | `proxy` | 代理配置 | 配置编号、代理地址、代理协议 |
| 频率规则迁移 | `migration` | 迁移任务 / 频率结果 | 迁移版本、结果版本、处理方案 |
| RPA执行记录 | `robot` | 执行记录 | 记录编号、流程模板、执行节点 |
| 采集结果校验 | `verify` | 校验规则 | 规则编号、采集计划、校验类型 |
| 采集操作审计 | `audit` | 审计记录 | 记录编号、操作类型、操作对象 |
| 采集存储分层 | `storage` | 存储策略 | 策略编号、存储层级、文件格式 |

## 频率规则迁移与代理容量

迁移把存量频率规则接到代理容量口径上，重算并发上限；**站点请求间隔与日配额是硬边界**，
迁移不会缩短间隔，也不会让并发超过日配额。旧配置缺 `并发上限` 时按 1 计，
不会因为缺少新字段而放开并发。

容量口径只有一处实现（`backend/app/services/capacity.py`），迁移清单、代理路由和
机器人下发都引用它：

```
单代理可用容量 = floor(并发上限 × 可用比例 × 延迟系数)
区域可用容量   = 区域内未关闭代理的可用容量之和
新并发上限     = min(区域可用容量, floor(日配额 × 间隔 / 86400))，再按配额硬边界收敛
```

### 一个站点只有一份生效结果

- 同一站点的重算串行执行，每次发布新版本，旧版本标记为「已覆盖」，不删除历史；
- `submission_id` 相同的重复提交只发布第一次的结果；
- 重算失败（间隔缺失、配额低于单并发需求、区域无可用代理）时保留原频率结果，
  规则标记「迁移失败」并记录原因；
- 迁移重启 / 断点续跑只处理还没处理的规则，同批次已失败且原因不变的规则不重复刷次数。

### 延迟突增导致的并发上限冲突

迁移完成时代理响应延迟相对基线突增（≥1.5 倍且高出 100ms 以上）且新旧并发上限不同，
结果进入「待决策」，暂按两者较小值执行。两种处理都能继续：

| 处理方案 | 生效并发上限 | 容量清单上的变化 |
| --- | --- | --- |
| 保留旧并发上限 | 旧上限 | 显示旧上限，失败重试数增加 |
| 立即采用新代理容量 | 新上限（受配额约束） | 显示新上限，待执行计划数量增加 |

决策后状态变为「已复核」，之后业务入口或代理变更引发重算时会沿用该方案。

### 数据流与联动

```
频率规则(ratelimit) ─迁移重算→ 采集频率结果 ─┬→ 代理容量复核清单 /capacity/review
                                             ├→ 代理路由输出     /api/proxy/capacity/routing
                                             └→ 机器人执行读取   /api/robot/capacity/dispatch
代理配置(proxy) ─可用比例/延迟变化→ 按区域联动重算频率结果
其他业务入口(crawlconf/datasource/verify/audit/robot) ─变更→ 联动重算相关站点
```

## 迁移版本口径

`backend/app/migrations.py` 是版本与字段变更的唯一注册表，代码里的迁移版本、
结构版本、容量口径都从这里取。构建、部署、本地示例和流水线通过 `MIGRATION_VERSION`
环境变量对齐同一版本：

- `backend/manage.py` / `make migrate`：断点续跑迁移（幂等）；
- `python -m app.build_check`：构建期校验版本一致，并在示例数据上试算迁移；
- `python -m app.version_check`：校验 Makefile、Dockerfile、compose、CI 声明的版本一致；
- `python3 manage.py verify`：校验处理后字段可复核、每站点只有一份当前结果、容量口径一致。

常用命令（根目录）：

```bash
make check      # 版本口径 + 构建自检 + 前端类型检查
make preview    # 迁移预览（不落库）
make migrate    # 本地迁移并回填历史
make verify     # 校验迁移结果
make test       # 后端迁移行为测试
make release    # check → build → migrate → verify
```

接口入口：

- `GET /api/migration/status`、`/versions`、`/preview`、`/results`、`/history`、`/runs`
- `POST /api/migration/run`（`mode=resume|restart|redo`，可带 `region`）
- `POST /api/migration/recalculate`（带 `submission_id` 去重）
- `POST /api/migration/conflicts/{rule_id}/decide`（`keep_old` / `adopt_new`）
- `GET /api/migration/capacity-review`、`/routing`、`/robot-dispatch`、`/capacity-queue`

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
