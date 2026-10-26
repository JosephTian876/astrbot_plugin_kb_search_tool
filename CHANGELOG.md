# 更新日志

## 0.2.1 - 2026-10-27

注入前判定 provider 的工具能力。**默认开启**，新增 1 个配置键；关掉即逐字节回到 0.2.0。

### 行为

- 新增配置键 `respect_provider_tool_support`（默认 `true`）：在
  `enabled → 工具解析 → 同名判重 → 知识库判定` **之后**、真正 `add_tool` **之前**，读一次
  「当前会话实际使用的 provider」的「模型能力（`modalities`）」。
  - **只有** `modalities` 是「非空 list 且不含 `tool_use`」时才跳过注入，并 WARNING 一次
    （每 provider+model 一次）。判据与核心 `_func_tool_for_provider` 逐字对齐
    （4.29.1 `core/agent/runners/tool_loop_agent_runner.py:680-686`）。
  - `modalities` 为 `None`／`[]`／缺键／非 list，或取不到 provider、`provider_config` 不是
    dict、读取抛异常 —— 一律按「支持」处理、照常注入；这些「未知」路径只在 DEBUG 留一条
    （按原因去重），绝不 ERROR 刷屏。理由：核心 4.29 自己把空值当「未配置，默认支持全部
    模态」（同文件 `:665-669`），把空值当否定信号会让绝大多数没填「模型能力」的用户直接
    失去工具。
  - 图片请求（`req.image_urls` 非空）且主 provider 没有 `image` 模态时，核心会把这次请求切给
    fallback provider（`core/astr_main_agent.py:1412-1443`），能力不再是「当前 provider」，
    因此按未知处理、照常注入。
  - 会话级 `selected_provider`（WebUI / 开放 API 选模型）优先于会话默认 provider，与核心
    `_select_provider` 同序（`core/astr_main_agent.py:283-310`）。
- 判定**只读**：不改 `modalities`、不改 `selected_provider`、不写任何配置、不 monkeypatch。
- 开关关闭 == 0.2.0：142 个场景（2 种知识库 × 5 种 `func_tool` × 7 种 provider 形态 ×
  有无图片 + 2 种会话级知识库）下，与 `git show f149e10:main.py`（SHA256 与生产安装目录
  一致）的观测 JSON **SHA256 完全相同**（`B5A28FCA…B3141F3`）。开关开启时 37 个场景观测
  不同，其中**只有 4 个真的少掉工具**（`modalities=["text"]` + 无图片 + 有知识库；3 个是
  请求本来没有/只有别的工具，1 个是会话级知识库）：其余 27 个注入结果不变、只多一条
  DEBUG，6 个只多一次不可见的 provider 查询。

### 兼容

- `get_using_provider_async()` 是 **4.27.5** 才有的 API；本插件声明 `>=4.23`
  （`metadata.yaml`），因此做 `getattr` 能力探测并回退到 4.23–4.26 的同步
  `get_using_provider(umo=...)`；两者都没有时**静默**放行（不新增日志，保持 0.2.0 的
  成功路径只留 1 条 DEBUG）。真实 4.23.0 源码树的契约冒烟输出：
  `provider_api={async:false, sync:true, by_id:true}`、
  `provider_gate={modalities_text:skip, modalities_none:inject, via:sync}`；
  4.29.1：`async:true`、`via:async`。
- 同步回退走 `sp.get` 的专用同步 SQLite 连接（一次点查，实测约 1 ms，只阻塞调用线程、
  不碰 async 连接池），且只在 4.27.5 以下发生；4.27.5+ 走 async 版。

### 日志

- 新增 WARNING（每个 provider+model 一次）：
  `[KBTool] 当前 provider「<id>」（模型 <model>）的模型能力未勾选 tool_use，已跳过注入；如需强制注入请关闭 respect_provider_tool_support。`
- 新增 DEBUG（未知原因，按原因去重、每插件生命周期一次）：
  `[KBTool] 无法判定当前 provider 是否支持工具调用（<原因>），按支持处理。`
- 启动 INFO 与「注入成功」DEBUG 不变（`test_01` 仍断言成功路径恰好 1 条 DEBUG）。

### 配置与文档

- `_conf_schema.json` 新增 `respect_provider_tool_support`（`bool`，默认 `true`，hint 写明
  判据是「`modalities` 非空且不含 `tool_use`」、未知/空值一律放行、关闭后回到旧行为）。
- README：配置表新增一行；「自查」补两条日志的含义；「边界」补 `modalities` 的跨版本语义、
  4.23–4.26 的同步回退、图片 fallback；「与核心的关系」「版本兼容」同步。

### 测试

- 单测 **15 → 56**（+41）：`AUDIT-KB.md` §3.2 判定表逐行覆盖 —— `modalities` 的 7 种形态
  （缺键/`None`/`[]`/字符串/dict/tuple/int）、取不到 provider、非聊天 provider、
  `ValueError` 路径、探测抛异常、旧版 Context 只有同步 API、Context 完全没有 provider API
  （静默）、`selected_provider` 三种走向、图片 fallback 三种组合、关掉知识库闸门仍注入、
  开关关闭、配置键 1:1。
- 契约冒烟新增两条真树断言（`Context` 的 provider 方法面；按该形状跑跳过/放行），
  4.29.1 与 4.23.0 均通过。
- `pytest -q` / `python -m compileall` / `uvx --from ruff==0.15.22 ruff check .` /
  `ruff format --check .` 全绿。

### 已知边界（明确不修）

- **「配置 ≠ 真实能力」**：`modalities` 是用户手填的；模型其实支持函数调用、只是没勾
  `tool_use` 时会被跳过（WARNING 会指出来）。这是「尊重配置」的固有代价，关掉开关即可恢复。
- **4.23.0 的 `modalities=[]`**：核心当「不支持」并清空，本插件按「未配置」放行 —— 同一
  配置值跨版本语义翻转，无法两全；4.23 用户请显式勾 `tool_use` 或升级。`modalities=None`
  在 4.23.0 会让核心自己在构建期抛 `TypeError`（发生在钩子之前），插件无法救。
- **非标准 `func_tool`**（别的插件换成缺 `get_tool` 的对象）仍是 0.2.0 的行为：ERROR 一次 +
  不注入。本轮刻意不加防御，否则「开关关闭时逐字节等价 0.2.0」不成立（`AUDIT-KB.md` §7
  第 6 行把这条列为「不修」）。

## 0.2.0 - 2026-10-26

行为修正 + 可维护性加固。**配置键不变，无需迁移。**

### 修复

- `only_when_kb_configured` 现在与核心 `retrieve_knowledge_base` 的判定顺序一致：
  会话级知识库设置（`kb_config.kb_ids`）优先，没有该设置时才看主配置 `kb_names`；
  读取配置失败时按「有知识库」处理（fail-open）。此前只读默认配置的 `kb_names`，
  导致两种错判：只配了会话级知识库的会话被拒绝注入；会话里显式关掉知识库
  （`kb_ids=[]`）却照样注入。
- 取不到内置工具时不再永久停用自己：之前启动自检失败一次，整个进程寿命内都静默不注入；
  现在每个请求都会重试，工具恢复后自动开始注入，ERROR 只打一条。
- `ToolSet` 改从 `astrbot.api` 公开面导入：导入失败不再静默降级成「永不注入」
  （那种情况下日志还会说「已确认可用」）。

### 日志

- 启动只保留一行 INFO（去掉版本号——核心加载插件时已经打过）。
- 注入成功在 DEBUG 留痕（默认 INFO 下不可见）：`[KBTool] 注入 astr_kb_search，工具数=N`。
- 失败日志改成可行动的：说明该确认什么、修好后会不会自动恢复。
- 删掉每次加载都打的「提示」行，以及 `terminate()` 里的重复日志（核心已打）。

### 仓库与文档

- 删除 `metadata.yaml` 的 `description`（核心从不读取该字段），`desc` 收敛为一句话；
  `tags` 增补 `knowledge-base`、`astr_kb_search`。
- 删掉已废弃的 `@register` 装饰器与自读 `metadata.yaml` 的版本号代码：
  版本号的单一事实源就是 `metadata.yaml`。
- 新增 `tests/`（脱网、不启动 AstrBot、不写任何真实数据目录），`.gitignore` 忽略 `data/`。
- README 按「前提 → 解决的问题 → 用法 → 配置 → 自查 → 边界 → 背景 → 与核心的关系」重写；
  修正「自动注入写到哪里」的版本区间：切换点是 **4.26.0**（不是 4.28）。

### 兼容性

需要 **AstrBot >= 4.23**（`FunctionToolManager.get_builtin_tool()` 自该版本起提供）。
本轮已在 4.23.0 与 4.29.1 的真实源码上验证：公开面导入、钩子注册（`priority=-310000`）、
真实 `ProviderRequest` 注入、会话级知识库判定均通过。仓库里可用
`tests/contract_smoke.py` 复跑（设置 `ASTRBOT_SRC` 指向源码树，见 README「开发」）。

### 二轮审查收口

第二轮独立复审（文档与核心事实的贴合度）+ 回归验证后的收口。**行为无变更**（除下面
标注的两处日志/类型层面的说明外），配置键与日志前缀不变。

- **文档（阻断项）**：README「如何确认它在工作」的"按插件调 DEBUG"补上版本门禁 ——
  这个能力 **4.26.8** 才引入（`LogManager.set_plugin_log_level`），4.23.0–4.26.7 请改用
  「配置 → 系统配置 → 控制台日志级别」。
- **文档**：`only_when_kb_configured` 的表述从"有可用知识库"改为"配置了非空的知识库"，
  并写明**不校验知识库是否存在/有内容**（`kb_ids` 指向已删除知识库时仍注入，工具会返回
  `No relevant knowledge found.`）；`_conf_schema.json` 的 hint 同步。
- **文档**：第三方 Agent 运行器（dify/coze/dashscope/deerflow）**会**触发 `on_llm_request`，
  只是它们的 runner 不读 `req.func_tool` —— 原描述"不经过钩子"是错的；同时写清
  `tool_use` 清零点在 4.23.5 前后分别位于构建期与 runner 期，对"插件注入的工具会不会被
  丢掉"影响不同。
- **文档**：安装段改为「插件市场（若已收录）」+「`git clone` 始终可用」。
- **代码（类型）**：`req` 改用公开面真类型 `astrbot.api.provider.ProviderRequest`，删掉
  `getattr` 与两处 `# type: ignore`（原注释"它不在公开面里"是事实错误）；注入日志的工具数
  改用 `len(getattr(tool_set, "tools", ()))`，不再假设自定义 `func_tool` 一定有 `tools`。
- **仓库**：新增 `.gitattributes`（统一行尾，让任何机器打出的 zip 字节一致）与最小
  `pyproject.toml`（ruff `select = E,F,W,I,BLE,RUF` + pytest `testpaths`），从此
  `# noqa: BLE001` 这类注释真的生效。
- **测试**：新增 `tests/contract_smoke.py`（真实核心契约冒烟，缺 `ASTRBOT_SRC` 时自动
  跳过）；替身 `ToolSet.add_tool` 与核心的"优先 active"语义逐条对齐；补三条断言
  （钩子关键字名、非 dict 会话配置、`astrbot_version` 可被 `SpecifierSet` 解析）。

## 0.1.3 - 2026-10-26

- 新增插件图标 `logo.png`：插件面板会显示该图标（市场侧随下次收录生效）。功能无变更。

## 0.1.2 - 2026-09-24

- 启动日志打印的版本号改为从 `metadata.yaml` 读取，不再硬编码，避免与发布版本脱节
  （0.2.0 起不再打版本号：核心加载插件时已经打过）

## 0.1.1 - 2026-09-24

按插件市场自动安全检查（LLM Guard）意见修复日志规范。**无功能变更。**

- `main.py`：移除导入失败时回退到 Python 内置日志模块的分支，直接使用 `astrbot.api`
  的 logger

规范要求日志记录器**必须且只能**从 `astrbot.api` 导入。本插件声明支持
AstrBot >= 4.23，该版本已提供 `astrbot.api.logger`，回退分支本就不必要。

安全检查结论为 `malicious=0, suspicious=0` —— 功能实现本身无害。

## 0.1.0 - 2026-09-23

首个版本。

- 在 `on_llm_request` 阶段把内置的 `astr_kb_search` 工具注入 `req.func_tool`，
  使其在每次 LLM 请求中始终可用
- **不修改 `kb_agentic_mode`**，因此知识库的自动注入不受影响 —— 两者兼得
  （核心的 `_apply_kb()` 是 if/else 二选一，本插件绕开了这个限制）
- 启动时自检内置工具是否可取得，取不到会打 ERROR 并停用自身
- 任何异常都被吞掉并记日志，**绝不打断正常对话**
- 配置项：`enable`（总开关）、`only_when_kb_configured`（仅在已配置知识库时注入）

### 兼容性

需要 **AstrBot >= 4.23**（`FunctionToolManager.get_builtin_tool()` 自该版本起提供）。

本插件是纯 `on_llm_request` 钩子，**与消息平台无关** —— 适用于任何适配器。
