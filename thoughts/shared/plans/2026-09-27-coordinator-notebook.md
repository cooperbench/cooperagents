---
date: 2026-09-27 01:44:35 PDT
researcher: Codex
repository: cooperagents
branch: cooperagents-coordinator-training
git_commit: 70062abf165df84f30481b144f2d848bf8db77cd
last_updated: 2026-09-27
status: smoke-completed-protocol-issue-found
implementation_branch: codex/coordinator-notebook
scope: lean coordinator notebook experiment in cooperagents
---

# Coordinator 共享笔记：最小实现与对照实验

## Overview

在 **cooperagents** 中给 coordinator 增加 JSON 动作：更新短 Markdown 笔记、向 worker 发消息、
或不行动。笔记由 coordinator 独占写入，全文替换，保存在 host 的运行产物中，并通过只读目录挂载
供两个 worker 访问。更新后现有 poller 只提醒版本和明确路径，worker 用已有文件读取能力自行读取。
两个 worker 保留独立代码工作区；不自动把 notebook 全文注入 worker 上下文。

最小实现、离线验证和 Apptainer smoke 已完成；Docker 验证与模型效果实验尚未完成。范围不涉及 `cooperator-train`、`polar-ext` 的实现。
三阶段改为：最小功能 → 必要验证 → k=3 对照测量；效果测量是验收的一部分。

## 已确认的设计选择

| 选择 | 实现约定 |
| --- | --- |
| 笔记写入与状态 | 仅 coordinator 写；全文替换；职责、接口、待确认事项和交接保留在 Markdown，不做业务状态机 |
| **笔记访问（用户最新选择）** | 两个容器只读挂载同一目录，统一路径 `/coordination/notebook.md`；更新只发版本与路径提醒，由 worker 自行读取 |
| 动作接口与消息 | 解析模型返回的 JSON，由 harness 执行；复用已有通道，接通 worker 给 `coordinator` 的回复 |
| **1B：开局协调** | 先只读探索，worker 报告拟修改范围与接口；coordinator 调整建议，双方明确确认后再编辑有争议的共享区域。独立工作可继续，仅用 prompt 约定，无硬屏障 |
| **2A：预算提醒** | 模型根据真实剩余步数/时间决定是否提醒；harness 不发 80%/95% 自动提醒。漏提醒、晚提醒都是被评估的模型行为 |
| **3A：实验问题** | 修正后的 messages-only coordinator 对照同一个 coordinator + notebook；只改变 notebook 能力 |

采纳 reviewer 的同步初始调用、单一开关、简化锁与测试、明确模型和测量建议。
按用户最新选择保留目录挂载；poller 负责短通知，不采用 reviewer 建议的全文推送。
同步初始调用只能给出探索及分工建议，**不等于双方已经完成协调**。

## Hypothesis 与现有证据

**假设：** 长任务中，已确认的职责、接口和未解决依赖散落在消息里，可能在上下文压缩或后续修改后
失去一致性；持续维护一份短的当前约定，并提醒 worker 从固定路径读取，能减少违背约定的编辑及重复澄清，提高最终双 feature
通过率。这里要修复的是“共享约定缺少持续维护”，不是“发出的消息不够多”。

36-pair 审计发现 236 次压缩及旧消息原文消失，这是风险线索，不能证明模型实际忘记了约定。
旧 coordinator 还有观测缺失、离题提示、三次额度及结束后投递等问题；两臂都修复这些问题，
不能把这些修复的改善算作 notebook 收益。阶段 3 给出执行命令和预设判定规则。

历史 TK1 静态 shared contract、TK2 awareness、TK3 messaging、TK4 board、TK5 blocking waits
没有稳定突破同一分数平台，因此收益预期应保守。新方案与 TK1 的区别是**根据实际回复持续更新、
修正旧约定，并在版本变化和压缩后提醒重新读取**；不是又增加一份开局合同。
`SEAM_BACKLOG.md:1103` 已纠正“工具只要是可选就不会用”：充分说明后 send_message 使用明显增加，
但分数没有相应增加。挂载方案需要实际测量 worker 是否读取、是否使用，不能把文件可读当成内容已进入上下文。

## Current State Analysis

引用行号对应 frontmatter 中的提交；以下源码路径相对于 `src/cooperagents/`。

| 位置 | 当前事实与本次改动 |
| --- | --- |
| `harness.py:204,253,312` | 20 秒机械触发、每人三次、400 字符输出；命令未读取实际的 `extra.actions`。改为有任务上下文的结构化决策 |
| `harness.py:240,468` | queue → drain → poller → worker user message 已存在；复制再清空存在并发丢消息风险 |
| `harness.py:182,884,904` | `contract_first` 已有启动前同步模型调用的先例；新初始决策复用这个生命周期位置，不叠加旧合同 |
| `workers/mini_swe_worker.py:235,424,447` | 有 BusComm 与 register，没有可靠的结束通知；补回复接线和 finally |
| `vendor/mini_swe/agents/default.py:202,223,277,340,376` | run 清空 messages，task 跨压缩保留；poller 在 query 内部压缩前执行 |
| `harness.py:364,656,1654` | 有公开 completion 回调及模式 guard；join 是有条件的。扩展同一 guard，迁移回调语义，始终收尾已启动的线程 |
| `bus/memory.py:114,130` | InMemoryBus 支持未注册的 coordinator 收件箱，receive 有锁；当前 runner 使用它 |
| `bus/redis_bus.py:150` | Redis receive 的 LRANGE/DEL 存在独立竞态；本次实验不用 Redis，不扩展修复范围 |
| `env/docker.py:28` / `env/runtime.py:10` / `env/apptainer.py:19` | Docker 已支持 volumes；Apptainer 目前仅路由 `/cbshared`。增加明确的 notebook 目录参数，复用 Docker volumes，Apptainer 增加只读 bind |

旧 docstring 声称 9B 判断不如机械触发，这是需要正视的历史结果。改成模型决策是待验证的设计，
不是已证明的升级。本实验只回答 notebook 在该新 coordinator 上的增量作用，不能回答新旧策略谁更好。

## Phase 1: 最小动作协议与运行接线

### 一个协议、一个 notebook 开关

**Files:** `src/cooperagents/harness.py`, `src/cooperagents/types.py`,
`scripts/bench_compare.py`, `scripts/bench_programbench.py`。

沿用 `coordinator_complete(prompt) -> str`，用 stdlib `json` 加显式校验，不引入原生 tool calling 框架。

```json
{
  "actions": [
    {
      "action": "update_notebook",
      "content": "# Coordination\n\n## Proposed\n- agent1: inspect the parser and report intended edits.\n- agent2: inspect validation and report shared interfaces.\n\n## Pending\n- Both workers have yet to confirm ownership."
    },
    {
      "action": "send_message",
      "recipient": "agent1",
      "content": "Read /coordination/notebook.md, then inspect the code and send your proposed files and interfaces to agent2 and coordinator. Confirm overlapping responsibilities before editing those regions; independent work may continue."
    }
  ]
}
```

- 根对象仅有 `actions`；`{"actions":[]}` 是合法 no-op。拒绝未知字段、动作、类型及非 roster 收件人。
- 每批最多一次 notebook 更新、每个 worker 最多一条消息；保留既有 roster 遍历，不硬编码只有 agent1/2。
- notebook 最多 8,000 字符、消息最多 1,200 字符，要求非空；原始响应最多 65,536 字符，不截断 JSON。
  prompt 鼓励约 1–2 千字符的笔记。模型不指定路径、权限或版本号。
- **整批校验后才执行，先写笔记再发消息。** 写失败保留旧版，不投递本批消息；不做跨文件/消息事务系统。
- 新增单一 `TeamSpec.coordinator_notebook: bool = True` 和 CLI `--no-coordinator-notebook`；仅 coordinator 开启时有效。
  off 臂没有 update 动作、host 笔记、挂载/路径提醒或 coordinator 笔记输入；mode-invalid 动作明确拒绝，不能暗中忽略。
  两臂共用其他 prompt、上下文窗口、调用节奏、回复与执行逻辑；仅 notebook 说明及能力有差异。
  这测量持久笔记的整体增量，包含 coordinator 外部记忆及 worker 经提醒自行读取，不单独归因其中某一个环节。
- 保留 `coordinator=False` 总开关，不维护第二套旧 C2 实现。此次新增开关不是关闭 coordinator。

### 只读目录挂载与更新提醒

**Files:** `src/cooperagents/env/runtime.py`, `src/cooperagents/env/apptainer.py`，以及上述 harness 和两个 runner。

- `UnifiedHarness` 增加可选 `coordinator_notebook_path`。启用 notebook 时须传本次 run 的路径，
  在创建环境前校验。两个 runner 在既有产物目录下使用 `coordination/<run_id>/notebook.md`，
  不依赖是否启用 trajectory。runner 的环境 factory 捕获同一个文件的父目录；direct harness 调用方
  也负责提供路径和挂载，`EnvFactory(agent_id)` 签名不变。目录只含该 run 的 notebook，不暴露其他运行产物。
- 建立只列任务身份和待确认事项的 v0。唯一 writer 同目录写临时文件后 `os.replace`；失败清理临时文件，
  保留旧内容和版本。版本写入 Markdown 头部，成功后发布内存 `(version, content)`；相同内容不增版本。
  目录 0755，初始文件和每次替换文件均为 0644，使非 root worker 可读；容器端通过只读挂载限制写入。
- **挂载目录，而非单个文件**：两个容器都将同一个绝对 host 目录只读挂到 `/coordination`，文件路径固定为
  `/coordination/notebook.md`。这样 `os.replace` 后的新文件仍可从挂载目录读取，不会绑定在旧文件上。
- `task_environment(..., coordinator_dir=None)` 仅新增这个明确参数：Docker 分支追加
  `host_dir:/coordination:ro` 到已有 volumes；Apptainer 创建容器内挂载点并追加同样的只读 `--bind`。
  保留 `/cbshared` 原路由，不扩展通用 mount parser，不开启 team_roles。harness 在创建环境前建立目录和 v0。
- ProgramBench 直接使用 DockerEnv，其 factory 同样追加目录 volume，保留 adapter 的 user/network/setup。
  runner 的 main 传入现有产物目录；直接 run_team_once 未给目录则在既有 runs 根下使用唯一 run_id。
  dry-run 不创建目录/容器。开启 notebook 时，harness 在 worker 启动前验证各环境能读到初始化版本；
  缺挂载/不可读使运行明确失败。目录作为 artifact 保留，不随容器清理删除。
- 每个 worker 的第一次 poll，以及版本变化时，只发送短提醒，例如：
  `[coordinator] Notebook updated to v3. Read the latest file at /coordination/notebook.md (cat /coordination/notebook.md) before continuing work affected by the coordination agreement.`
  每步至多一条最新版本提醒；无变化不重发。提醒只有版本、路径和读取要求，不携带笔记全文。
- 原 task 保留固定路径、只读说明、读取时机和回复方式，提示 worker 开始工作前及更新后读取；
  仍用已有 bash/read 能力，不新增读取工具或强制读取屏障。每人记录已提醒版本及 `_compaction_count`，
  压缩后补发相同路径的读取提醒。poller 先于 query 内压缩执行，最迟下一步提醒，不保证 worker 真正读取。
- coordinator 每次输入含当前 notebook 全文；不增加无限增长的 coordinator 对话历史。
  消息记录生成时依据的版本，poller 先给路径提醒再展示行动要求；旧消息不能标成依据最新版本生成。
  worker 读取时可能已有更新版本，以文件头部版本为准；不保存供 worker 选择的历史版本集合。

### 启动、确认、观察和回复

**Files:** `src/cooperagents/harness.py`, `src/cooperagents/workers/mini_swe_worker.py`,
`src/cooperagents/planner.py`。

```text
验证配置与 roster → 建立可选 notebook → 创建独立环境并只读挂载、检查可读
  → 同步初始决策并排队 → 启动 monitor 与 worker futures
  → worker 探索/互通范围 → coordinator 根据回复调整 → 持续协调
```

- 预先按 roster 建队列，register 不清空；未注册不等于已结束。初始结果放入队列，不能提前改
  `agent.messages`。worker 步数/时间预算从实际启动开始；初始决策耗时计入 pair 时长。
- **1B 是软协议**：先读代码，再用已有 `send_message` 将拟改文件/区域与共享接口发给 peer 和 `coordinator`。
  有争议的共享区域先协调；等待期间做只读分析、验证或明确独立工作，不使用 `wait:true`。
  不加 ACK parser、代码区域写权限控制或阶段状态机；双方确认由模型维护，可能不遵守，需在轨迹中检查。
- 笔记建议四段：职责/范围、接口/依赖、待解决事项、验证/交接。用 proposed、worker-reported、confirmed、verified
  区分证据；不能从沉默推断确认，也不能把 worker 自述通过当成实际验证。
- 在既有 worker 工具说明里清楚列出 coordinator 收件人，不另开旧 `tool_protocol` 的首次动作规则。
  `coordinator` 为保留身份。每 tick drain bus 回复到**一个本地 pending list**；截取本次输入前缀，
  合法决策成功执行（含 no-op）后才移除此前缀；失败保留，后到回复留给下轮。
- 决策输入包括任务、roster、近期动作/结果、编辑文件、结束状态、pending 回复、剩余步数/时间和可选 notebook。
  从 `extra.actions` 取动作，仅缺字段时 fallback `tool_calls`；结果用 `extra.raw_output/returncode`。
  按 tool_call_id 或同一动作批次配对，未观察到的结果标未知，不用两个尾部列表盲目 zip。
- 最近每人保留 6 个动作，命令及结果各最多 1,000 字符，任务每人最多 6,000 字符，裁剪明确标记。
  Git 查询保留短 timeout，正确解析路径，排除已知 harness 文件；同文件仅称“可能重叠”，不宣称冲突已发生。
- 保留 20 秒 tick；有新步骤、回复或结束才决策，不加第二层 60 秒调度器和预算阈值绕行。
  每次最多一个串行 pair 决策，无三次终身额度，允许 no-op；长请求会延后下轮，不承诺严格实时。
  失败不推进已处理观察标记，未处理回复仍使下一 tick 可重试，不能因为“已经看过”就永远跳过。
- **2A：** 每次输入读取 `agent.n_calls`、`config.step_limit`、`config.wall_deadline` 计算真实剩余量，
  无限制时注明无上限，不推测 token 余额。不加自动提醒、阈值状态或 poller 预算消息。
  没有新进展的长命令期间可能不再决策；此限制和模型漏提醒都要如实报告。

### 最小生命周期及错误行为

- 一个小锁保护队列、结束标记及内存快照读取。enqueue、drain、mark_finished 使用同一锁，
  drain 原子取走；mark_finished 清空并记录未消费消息。模型调用、Git 查询、文件 I/O 都在锁外。
- worker `finally` 中 mark_finished，先于 `agent_end`。模型返回后入队时重新检查目标是否结束及 stop，
  迟到消息丢弃并记录；不再读 ended worker 环境。不建立 stop/replace 锁顺序：join 前最后一次 host
  写入无害，完成后不向 ended worker 投递即可。
- monitor 在初始同步调用结束后才启动；`finish()` 始终 join **已启动**的线程，先于 ExitStack 环境清理。
  保留 600 秒 join 上限，超时明确失败，不宣称取消底层请求；删除 ready event 与 startup wait。
- 默认 coordinator client 复用一次创建的 client，单独设置 SDK timeout=60、max_retries=0；
  `_default_planner_complete` 仅加可选参数，不改变其他 planner 默认。SDK timeout 不等于严格墙钟截止。
- 默认模型非法 JSON 时整批无副作用，trace 写拒绝原因，下轮输入简短错误；无额外格式重试。
  传输失败同样记录并保留回复；开局失败仍靠固定 worker 协调说明启动，不补发模型未决定的机械提示。
  文件及 journal 失败使 run 明确失败。注入 callback 保留“错误/非法结果使 run 清理后失败”的契约。
- callback 首次在 harness 调用线程执行，随后在 monitor thread 串行执行；调用方须提供支持该模式的
  client 并限制请求时长。迁移 JSON 和线程语义的文档/测试；不声称语义兼容，不替调用方重复记录 SDK。
- 扩展 `harness.py:1654` 现有 guard：静态唯一 roster、mini_swe、coop_tools、shared_workspace、no-seed；
  排除 team_roles、adaptive、decompose、best-of-N、contract_first 和动态 helper（`allow_spawn_tool`）。
  不误用默认 True 的 `allow_spawn`。repair/integrator 不加入本 coordinator roster。

## Phase 2: 必要验证与记录

**Files:** 新增 `tests/test_coordinator.py`，扩展现有 `tests/test_harness.py`, `tests/test_sampling.py`, `tests/test_trajectory.py`,
`tests/test_bench_budget.py`, `tests/test_frozen_harness.py`, `tests/test_apptainer_env.py`；更新
`scripts/nlp_cluster/dummy_smoke.py`, `docs/trajectory-collection.md`, `README.md`。

自动验证集中为七组行为，不给每个内部 helper 建一套测试：

- [x] 非法整批无副作用；失败写入保留旧文件/版本，相关消息不发送。
- [x] 并发 drain 不丢消息；worker 结束后不再入队，待投递消息被记录并清理。
- [x] 回复在调用/格式失败后仍存在；合法 no-op 才可消费；新到回复不被清空。
- [x] 一个离线真实 worker-loop smoke 覆盖同步启动、双向消息、版本/压缩路径提醒及始终 join；提醒不注入全文。
- [x] notebook off 完全关闭笔记通道而保留同一消息实现；初始和后续模型消息均进入实际 worker 请求。
- [x] native/XML 动作与结果进入观察，剩余预算事实正确且不自动生成预算提醒。
- [x] 两个 runner 将同一 run 目录传给 harness/factory，Docker/Apptainer 构造只读目录挂载；产物隔离、ProgramBench dry-run、公开 callback 与既有轨迹回放仍成立。

复用 stub、LocalEnv、httpx MockTransport、临时目录和可控同步点，无模型/容器的检查先跑：

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest \
  tests/test_coordinator.py tests/test_harness.py tests/test_sampling.py tests/test_trajectory.py \
  tests/test_bench_budget.py tests/test_frozen_harness.py tests/test_apptainer_env.py
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
```

变更脚本另做定点 Ruff。随后在已配置环境扩展现有 dummy smoke：两个容器从相同路径读取 v0，
host 原子替换两次后均能读到新版本，写入被拒绝，非 root 也可读，最终 patch 不含 notebook；
分别验证 Docker 和 Apptainer 的实际挂载，环境不可用则标明未验收，不以 argv 单测替代。
检查真实请求中的短提醒、worker 自行读取的工具结果及 cleanup；不恢复 stop/replace 交错测试。
不为无关基线 lint/typecheck 问题扩大改动范围。
实现记录（2026-09-27，`codex/coordinator-notebook`）：

- 新增 `tests/test_coordinator.py` 承载七组行为中的 coordinator 检查，迁移旧 callback/mock 测试。
  真实 worker-loop smoke 用 LocalEnv、确定性模型与受控 monitor tick；两臂覆盖初始/后续消息、
  worker 回复、worker 自行读取、真实 emergency compaction 后的路径刷新、错误结束和轨迹回放。
  该测试没有实际容器挂载，不将其作为挂载验收。
- 完整相关离线套件 **73 passed**，命令见上方（包括新增的 `tests/test_coordinator.py`）。
- 所有变更 Python 文件的定点 Ruff、核心 5 个源码文件的定点 mypy、compileall 和 `git diff --check` 通过。
  全仓检查仍未通过：Ruff 有 4 个既有问题，format 有 15 个既有未格式化文件，mypy 有 8 个既有错误
  （trajectory、limited_process、terminalbench、mini_swe_worker）。用基准提交导出源码核对，均非本次新增；
  未扩大到无关清理。新增测试、迁移的 SDK/trajectory 测试与 coordinator 类已格式化。
- `dummy_smoke.py` 已迁移为同步初始 JSON 决策，并增加 v0→v1→v2 目录挂载读取、写入拒绝、
  非 root 读取及 patch 排除检查；在下方 NLP smoke 中完成了 Apptainer 验证。实施时本地 Docker socket 不存在，默认 SSH Docker context
  在沙箱中连接失败，Apptainer 不在 PATH 中。Docker 挂载验收仍待配置环境执行。
- 模型 pilot、JSON/schema 有效率、fixed10×k3 两臂与机制案例均未运行；没有效果或成本收益结论。
  不为本地实施启动或修改集群服务。跨 pair 协调仍仅是 future work。

NLP smoke 更新（2026-09-27）：确定性 job `17637578` 全部通过；真实 Qwen job `17637587`
以 `0:0` 完成，双 worker 各 30 步，读取与双向消息已验证，轨迹审计通过。
真实 coordinator 仅 2/3 返回合法，另一次 Markdown 中未转义换行导致 JSON 整批被拒绝；
同时观察到开局猜测文件范围与误标任务身份；协议确认的证据也需要进一步核查。
这不等于完成 pilot 或证明 notebook 有效。详见
[smoke 记录](../../../scripts/nlp_cluster/RESULTS_2026-09-27_NOTEBOOK_SMOKE.md)。
本次为 bounded smoke 给 NLP launcher 透传了 notebook 开关及步数/时间预算，新增两项提交器检查通过。

待验收：

- [x] 实际 Apptainer 挂载 smoke（含非 root、两次替换）：NLP job `17637578`，`COMPLETED / 0:0`。
- [ ] 实际 Docker 挂载 smoke。
- [ ] Qwen3.5-9B non-thinking 两臂 pilot，各至少 20 次真实模型返回。
- [ ] fixed10×k3 两臂完整评分、成本与机制报告，按预设规则决定是否扩样。

记录复用现有 journal：decision 输入/原输出、JSON/schema 拒绝、no-op、更新版本/全文、消息入队/取出、
回复与结束丢弃。保留 nudge ↔ `metrics.coordinator_events` 对应关系；notebook 路径提醒用单独 delivery 事件，
不冒充模型消息。现有 nudge 审计规则不变。区分通知送达、成功读取和后续采纳：核对 worker 工具结果与
实际 SDK 输入中的文件版本/内容；harness 的启动可读性检查不能算 worker 已读。

人工抽查初始探索/职责确认、实施中一次约定修订、收尾预算建议；记录违反 1B、错误确认、
重复无效干预和缺失预算提醒。没有某阶段建议也要报告，不能只展示成功样例。

## Phase 3: k=3 对照测量与去留决定

### 固定配置与协议有效性

两臂使用 **Qwen3.5-9B non-thinking** worker 与 coordinator；归档服务模型标识为 `qwen3.5-9b`。
固定 temperature=1、top_p=.95、top_k=20、presence_penalty=1.5，显式关闭两者 thinking，
记录实际服务版本、模型标识与采样；不因 schema 失败临时切更大模型。endpoint 由执行环境配置，不复用过期地址。

先在下述 fixed10 前两个 pair 做两臂 pilot，检查输出与通道。分别报告 JSON 可解析率、**整批 schema 有效率**
（合法响应数/非传输失败的模型返回数）、有效 no-op 比例、传输失败率和拒绝原因；按 startup/后续分别列出。
每臂至少 20 个真实返回再初筛，不足就依固定顺序扩展 pilot。预设 schema 有效率门槛为 95%，
这是工程门槛，不是历史统计结论；正式实验仍持续报告有效率。低于门槛先修 prompt/协议并重做双方 pilot，
不能归结为 notebook 无效。若模型从不写笔记，结论是“当前策略未使用 notebook”，不是笔记没有价值。

首轮用仓库**已有 fixed10，k=3**，共 60 个正式 pair-run，pilot 另计；不重跑无关 solo 臂。
这比 cb36×k3 的 216 个 pair-run 便宜，并能直接复用评分入口。历史 ±4 features 出自 qwen14，
只能支持需要重复测量，不能作为 fixed10/cb36 的已知方差。1000 步/3600 秒、git-share、completion gate、
repair 两次等在两臂固定；不与旧 50 步实验分数直接比较。保留机械合并/repair 算法，两臂都用 InMemoryBus。

### 可执行命令（实现新 flag 后，在已配置的 benchmark 环境执行）

以下为 Bash；现有 endpoint/凭据、CooperBench 路径、运行后端和镜像须已配置，不创建部署系统。
使用唯一输出目录，不用旧日志 resume 充当重复。没有模型 seed 配对能力，轮次号仅表示独立重复。

```bash
set -euo pipefail
export AZURE_OPENAI_DEPLOYMENT=qwen3.5-9b
export COOPER_TEMPERATURE_FORCE=1 COOPER_TOP_P=.95 COOPER_TOP_K=20
export COOPER_PRESENCE_PENALTY=1.5
export COOPER_CHAT_TEMPLATE_ENABLE_THINKING=false
export COOPER_WORKER_CHAT_TEMPLATE_ENABLE_THINKING=false
export NB_LOG_DIR="logs/notebook-ablation-$(date -u +%Y%m%dT%H%M%SZ)"
pairs=$(uv run python -c 'import sys; sys.path.insert(0,"scripts"); from evaluate_improvement import FIXED_PAIRS; print(" ".join(f"{r}:{t}:{a},{b}" for r,t,(a,b) in FIXED_PAIRS))')
for round in 1 2 3; do
  arms=(msg nb)
  if (( round % 2 == 0 )); then arms=(nb msg); fi
  for arm in "${arms[@]}"; do
    extra=()
    if [[ "$arm" == msg ]]; then extra+=(--no-coordinator-notebook); fi
    uv run python scripts/bench_compare.py --pairs $pairs \
      --team-only --max-agents 2 --no-seed --coop-tools --git-share --coordinator \
      --completion-gate --repair-integrator --repair-attempts 2 --repair-steps 25 \
      --step-limit 1000 --agent-time-limit 3600 --concurrency 1 --eval-concurrency 1 \
      --record-trajectory --log-dir "$NB_LOG_DIR" --team-name "$arm-r$round-team" "${extra[@]}"
  done
  uv run python scripts/evaluate_improvement.py --log-dir "$NB_LOG_DIR" \
    --baseline "msg-r$round" --candidate "nb-r$round"
done
```

`measure.sh` 不透传这些 flag，且会跑 solo；不为本实验改它。`evaluate_improvement.py` 自动添加 `-team`，
默认即 fixed10。它的 verdict 是单轮启发式，仅参考，不代替三轮判定；全 pair 平均时长不等于共同成功任务耗时。

不新建评分框架；用既有 `load_scorecard` 加 stdlib 汇总。先检查完整性，避免把缺失产物默认当失败/零耗时：

```bash
uv run python - <<'REPORT'
import json, os, statistics, sys
from pathlib import Path
sys.path.insert(0, "scripts")
from evaluate_improvement import FIXED_PAIRS
from cooperagents.eval.dataset import WorkItem
from cooperagents.eval.scorecard import load_scorecard
root = Path(os.environ["NB_LOG_DIR"])
items = [WorkItem(repo=r, task_id=t, features=f) for r, t, f in FIXED_PAIRS]
deltas = []
for rep in range(1, 4):
    cards = []
    for arm in ("msg", "nb"):
        label = f"{arm}-r{rep}-team"
        for item in items:
            path = root / label / "team" / item.repo / str(item.task_id) / "_".join(f"f{f}" for f in sorted(item.features))
            ev = json.loads((path / "eval.json").read_text())
            result = json.loads((path / "result.json").read_text())
            assert isinstance(ev.get("both_passed"), bool), path
            assert all(isinstance(ev.get(k, {}).get("passed"), bool) for k in ("feature1", "feature2")), path
            assert result.get("duration_seconds", 0) > 0, path
        cards.append(load_scorecard(root, label, "team", items))
    a, b = cards
    delta = (b.passed-a.passed, sum(p.features_passed for p in b.pairs)-sum(p.features_passed for p in a.pairs))
    deltas.append(delta)
    print(rep, "msg/nb pairs", a.passed, b.passed, "delta pairs/features", delta, "seconds", a.avg_duration, b.avg_duration)
for i, name in enumerate(("pairs", "features")):
    values = [d[i] for d in deltas]
    print(name, "paired deltas", values, "mean", statistics.mean(values), "range", (min(values), max(values)))
REPORT
```

### 测量内容与预设去留规则

- **主结果：** 每轮官方 both_passed/10、feature/20，三轮差值及均值/范围，逐 pair 列变化。
  模型失败计入配置结果；基础设施故障、缺评分、grader timeout 单列，不静默排除或填零。
  基础设施补跑保留原记录并注明，完成六组后才作完整比较。
- **成本：** 从 SDK usage 汇总 worker、coordinator、summary、repair 全部 token/调用，包含启动决策和
  路径提醒、worker 读取工具调用和文件内容进入上下文的成本；额外读取照常占用既有步骤/时间预算。
  报告全 pair 时间、共同成功 pair 时间和 repair 次数；无共同成功样本记 N/A。
  未知价格不当成 $0，worker-only `total_cost` 不当总成本。
- **机制：** 核对“约定及依据 → 路径提醒进入请求 → worker 成功读取及版本 → 后续相关编辑/验证”，
  记录更新后的读取率、读取延迟、未读/读取失败/沿用旧版，关注压缩后违约编辑和重复澄清。
  至少给两个不同 pair 的可核查案例，包含反例；增加写入数、复述或工具使用不算成功。
  原文消失不等于遗忘，单例时间先后不等于反事实因果。
- **值得扩样的信号：** 三轮 pair 差值均不负、平均至少 +2/10，平均 feature 不下降，并有机制证据；
  全部推理 token 增幅不超过 20% 时作为低成本候选。+2/10、20% 是预设工程取舍，不是显著性检验。
  有收益但更昂贵时单列成本取舍；更小或不稳定的收益记 inconclusive，不强说有效或无效。
- **没有收益时：** 区分格式/执行失败、未使用笔记、使用但未改善。协议有效且无稳定收益时保留 off 路径，
  暂停新增 notebook 基建。有希望再用固定 cb36/k≥3 或更多重复确认，不把三轮初步证据当确定结论。
  cb36 已参与训练数据准备/分析，不自动称 held-out。

pilot、正式对照、原始轨迹与去留结论一并记入既有运行记录和 `docs/SEAM_BACKLOG.md`；历史记录不重写。
本计划不启动模型或集群作业；未来执行按获授权的阶段完成，不因文档模板机械地重复索要确认。

## Scope、迁移与未来工作

本次不做 worker 状态面板、业务状态机、硬等待、跨 pair 注册/升级、Redis 修复、原生工具框架、
训练或独立评估平台。跨 pair 的资源归属、接口版本、依赖和冲突升级只记为 future work。
只读目录挂载属于本次明确需求；保留同步启动、单一预算路径和简化生命周期，不恢复旧计划的额外硬化机制。

迁移本仓库 callback/mock/dummy responder；ProgramBench 只接入共同能力和产物路径，不加入效果比较。
NLP `submit.py/job.sh` 若用于部署本实验，只透传同一 boolean 并写 metadata/variant，不另建模式；
上述直接 benchmark 命令已能比较。外部调用方自行迁移 JSON、初始调用线程和 notebook path，不声称语义兼容。

## References

- 当前代码：`src/cooperagents/harness.py:204,240,364,468,656,884,904,1654`；`src/cooperagents/planner.py:152`。
- Worker/压缩：`src/cooperagents/workers/mini_swe_worker.py:235,424,447`；`src/cooperagents/vendor/mini_swe/agents/default.py:223,277,340,376`。
- 测量入口：`scripts/bench_compare.py`；`scripts/evaluate_improvement.py`；`src/cooperagents/eval/scorecard.py`。
- 项目循环与历史：`docs/SELF_IMPROVEMENT_LOOP.md`；`docs/SEAM_BACKLOG.md:1098,1103,1113`。
- 数据范围：`datasets/cb-mixture-36/README.md`；`scripts/nlp_cluster/RESULTS_2026-09-25_REPAIR.md`。
- 审计证据：运行 `20260927T023355Z-cooperbench-round1-7a17b8ad`，归档源代码 `7a17b8ad`；
  报告在相邻工作区 `polar-ext/artifacts/2026-09-26-cooperbench-repair-trajectories/reports/coordinator-analysis-20260927/REPORT.md`。
  此文件仅提供问题证据，不引入 polar-ext 实现。
