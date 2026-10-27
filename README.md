# 知识库检索常驻 · astrbot_plugin_kb_search_tool

让 AstrBot 内置的知识库检索工具 `astr_kb_search` **在每次 LLM 请求里都可用**，
同时**保留知识库自动注入** —— 这是核心 `kb_agentic_mode` 二选一做不到的组合。

---

## 1. 适用前提

本插件只在下面三个条件同时成立时才有意义：

- **走内置 Agent**：`agent_runner.runner_type = local`。核心的知识库逻辑（自动注入与
  `kb_agentic_mode` 开关）只属于内置 Agent；Dify / Coze 等第三方 Agent 运行器不读它。
- **当前 provider 支持函数调用（tool use）**：核心自己的配置项也写着这个前提——
  「Agentic 知识库检索……需要模型支持函数调用能力」（`core/config/default.py`）。
  provider 的「模型能力」没勾「工具使用」（`tool_use`）时核心会清空工具，但清零点变过：
  - **< 4.23.5（实测 4.23.0 / 4.23.1 / 4.23.2）**：清空在构建期
    （`core/astr_main_agent.py` 的 `_modalities_fix`；4.23.0 在 `:826` 定义、`:1396` 调用），
    早于 `on_llm_request`（钩子在 `build_main_agent` 之后触发）—— 0.2.0 注入的工具
    **不会被清掉**，会带着工具发给一个不支持工具调用的 provider（后果取决于 provider SDK，
    可能直接报错）。
  - **≥4.23.5**：清空移到 runner（4.29.1：`core/agent/runners/tool_loop_agent_runner.py`
    的 `_func_tool_for_provider`），在 `reset()` 时执行，晚于钩子（`internal.py` 先
    触发钩子、再 `await reset_coro`）—— 注入的工具也会被丢掉。

  **0.3.0 起本插件自己尊重这个声明**：只有当「当前会话实际使用的 provider 的
  `modalities` 是**非空 list 且不含 `tool_use`**」时才跳过注入（WARNING 一次），其余
  情况（`None`／`[]`／没这个键／不是 list／取不到 provider／读取抛异常）一律按支持处理、
  照常注入。判据与核心 `_func_tool_for_provider` 逐字对齐；要回到 0.2.0 的「总是注入」，
  把 `respect_provider_tool_support` 关掉即可（见「配置项」与「适用范围与边界」）。
- **AstrBot >= 4.23**：`FunctionToolManager.get_builtin_tool()` 自该版本起才提供。

## 2. 解决的问题

核心的 `_apply_kb()` 是 **if/else 二选一**（`astr_main_agent.py`，4.23 → 4.29 一致）：

| `kb_agentic_mode` | 每次请求自动带上知识库结果 | 模型手上有 `astr_kb_search` |
|---|---|---|
| `false`（默认） | ✅ 有文字时 | ❌ 没有 |
| `true` | ❌ 没有 | ✅ 有 |

自动注入的**位置**随核心版本变过：**4.25 及更早**追加到 `system_prompt`，**4.26 起**改为
`extra_user_content_parts`（逐 tag 核对：4.23.0 / 4.24.5 / 4.25.6 用前者，4.26.0 / 4.29.1
用后者）。对使用者是同一件事：回答前先带上检索结果。

本插件在 `on_llm_request` 阶段把核心已经注册好的 `astr_kb_search` 追加进 `req.func_tool`，
**不改 `kb_agentic_mode`**。所以把它保持为 `false`，就能同时拿到上面两列的 ✅。

## 3. 用法

安装（任选其一）：

- **插件市场（若已收录）**：WebUI → 插件 → 插件市场 → 搜索「知识库检索常驻」→ 安装。
  市场收录有滞后，搜不到就用下面这种方式，它始终可用。
- **命令行（始终可用）**：

  ```bash
  cd <AstrBot>/data/plugins
  git clone https://github.com/JosephTian876/astrbot_plugin_kb_search_tool
  ```

装好后在 WebUI → 插件页面点该插件的「重载」即可生效，不需要重启 AstrBot。
无第三方 Python 依赖。

然后确认「配置 → AI → 知识库」里的 **Agentic 知识库检索（`kb_agentic_mode`）保持关闭**：
这样自动注入照常工作，工具由本插件补上。

## 4. 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 总开关。关闭后本插件不介入任何请求。 |
| `only_when_kb_configured` | `true` | 只在「当前会话或主配置里**配置了非空**的知识库」时注入。判定顺序与核心检索一致：会话级 `kb_config.kb_ids` 优先、主配置 `kb_names` 兜底（`core/tools/knowledge_base_tools.py`）。**不校验知识库是否存在或是否有内容**（那要读核心私有面，本插件不碰），所以 `kb_ids` 指向已删除的知识库时仍会注入，此时工具会返回 `No relevant knowledge found.`。关闭后总是注入。 |
| `respect_provider_tool_support` | `true` | 注入前读一次「当前会话实际使用的 provider」的「模型能力（`modalities`）」：**只有**它是「非空 list 且不含 `tool_use`」时才跳过注入并 WARNING 一次（每个 provider+model 一次）。`modalities` 为 `None`／空 list／缺键／非 list、取不到 provider、读取抛异常 —— 一律按支持处理、照常注入（fail-open，与核心「未配置=默认支持全部模态」一致）；图片请求可能被核心切给 fallback provider 时同样按支持处理。关掉即回到 0.2.0 的「总是注入」。**注意「配置 ≠ 真实能力」**：模型其实支持函数调用、只是没勾「工具使用」时也会被跳过 —— 看到那条 WARNING 就去 WebUI 的 provider 设置里确认；4.23–4.26 没有 `get_using_provider_async()`，会回退同步查询（见「适用范围与边界」）。 |

> `kb_agentic_mode=true` 时工具由核心自己添加（`core/astr_main_agent.py` 的 `_apply_kb`
> 的 else 分支），本插件检测到同名工具已存在就什么都不做 —— 这种配置下上面三个开关都
> 不改变结果，也不会去查 provider。

**三个开关的取值写法**：WebUI 里是开关，只会写 `true`/`false`。手改 JSON 时，插件认这些
写法（忽略大小写与首尾空白）：真布尔值、`1`/`0`、`"1"`/`"0"`/`"true"`/`"false"`/`"yes"`/
`"no"`/`"on"`/`"off"`。**认不出的值**（例如 `"FALSE!"`、`2`、`null`、`[]`、`1.5`）不会被
当成"真值"：插件用该项的**默认值**，并在加载日志里 WARNING 一次，写明键名、实际值和所用
默认值。这两类告警都只出现在加载/重载时，不按请求重复。

> 为什么手改的字符串也要当回事：核心会把 `_conf_schema.json` 之外的键删掉并打一条 INFO
> （`Config key removed`），但**不会**纠正类型 —— 写成 `"false"` 的值会原样交到插件手里。
> 实测（`tests/probe_core_config.py`，4.23.0 与 4.29.1 一致）：插件拿到的仍是字符串
> `"false"`；按 Python 的 `bool("false")` 语义那是 `True`，即"关掉闸门"被静默反转成"打开"。
> 这就是本插件改用显式白名单解析的原因。

## 5. 如何确认它在工作

1. **加载日志**（控制台或 `data/logs/`）：`[KBTool] 已加载 | 启用=True | 工具=astr_kb_search`
   —— 这行说明插件起来了；显示 `启用=False` 则说明插件被自己的开关关掉了。
2. **注入日志**：把本插件的日志级别调到 DEBUG，每次请求都会出现
   `[KBTool] 注入 astr_kb_search，工具数=N`（默认 INFO 下不输出，不构成噪音）。
   - **AstrBot ≥ 4.26.8**：WebUI → 插件 → 该插件 → 日志级别 → DEBUG
     （"按插件设置日志级别"这个能力 4.26.8 才引入：`LogManager.set_plugin_log_level`，
     `core/log.py`；WebUI 入口在 `dashboard/api/plugins.py`）。
   - **AstrBot 4.23.0–4.26.7**：没有按插件调级别的入口。请把「配置 → 系统配置 →
     控制台日志级别」改成 DEBUG（配置键 `log_level`，`core/config/default.py`）——
     效果相同，但会影响全局输出，看完记得改回 INFO。
3. **几条关键日志的含义**（都自带下一步动作）：
   - `[KBTool] 取不到内置工具 astr_kb_search（后续请求会自动重试）: ...`
     → 核心的内置工具注册表不可用（版本低于 4.23，或某个内置工具模块导入失败）。
     环境修好后下一个请求会自动恢复，不需要重装插件。
   - `[KBTool] 当前会话与主配置都没有可用知识库，已跳过注入工具；...`
     → 不是故障。插件只判断"配置是否非空"（不校验知识库是否存在/有内容）：要么去配置里
     选知识库，要么关掉本插件的 `only_when_kb_configured`。
   - `[KBTool] 当前 provider「...」（模型 ...）的模型能力未勾选 tool_use，已跳过注入；...`
     → 不是故障。这个 provider 的「模型能力」没勾「工具使用」，插件按你的配置尊重它。
     去 WebUI 的 provider 设置里勾上「工具使用」；若模型其实支持、只是配置没勾，就关掉
     本插件的 `respect_provider_tool_support`。同一 provider+model 只提醒一次。
   - （DEBUG）`[KBTool] 无法判定当前 provider 是否支持工具调用（...），按支持处理。`
     → 正常路径：`modalities` 未配置/格式非法/取不到 provider/读取失败时按支持处理
     （fail-open）。默认 INFO 下不可见；同一个原因只记一条（去重键含异常类型与消息前
     80 字，所以同类异常的不同原因都会留痕）。
   - `[KBTool] 配置项 <键> 的值无法识别（...），已按默认 <true/false> 处理；...`
     → 配置被手改成了插件认不出的写法（例如 `"FALSE!"`、`2`、`null`）。插件按默认值继续
     跑；照文案里列出的写法改成合法值即可。
   - `[KBTool] 配置里有无法识别的键：<键名>（已忽略）；可用键只有：...`
     → 多半是拼写错误。**正常安装里一般看不到这条**：核心加载插件配置时会先删掉 schema
     之外的键并自己打一条 INFO（`Config key removed: ...`）；这条只在核心把原始 dict
     直接交给插件时出现（例如测试/第三方加载器）。看到时按列出的可用键改名，或直接在
     WebUI 的插件配置页里改。
   - `[KBTool] 内置工具注册表返回 None（没有抛异常），即当前核心没有登记 ...`
     → 核心能正常导入但就是没有 `astr_kb_search` 这个名字（实测核心只会抛异常，所以这
     属理论分支）。插件会一直自动重试，环境修好后无需重载。
4. **行为验证（最可靠）**：在对话里发「用知识库检索工具查一下 XXX」，
   模型这次的工具调用里出现 `astr_kb_search` 即为生效。

## 6. 适用范围与边界

- **只覆盖内置 Agent 的主对话链路**（`runner_type = local`）。cron 定时任务与后台任务
  唤醒**不经过** `on_llm_request`（它们直接调 `build_main_agent`：`core/cron/manager.py`、
  `core/astr_agent_tool_exec.py`），本插件对它们无效。第三方 Agent 运行器
  （dify/coze/dashscope/deerflow）**会**触发 `on_llm_request`
  （`core/pipeline/process_stage/method/agent_sub_stages/third_party.py`），但它们的
  runner 不读 `req.func_tool`，注入进去没人用 —— 对它们同样无效，真正的原因是
  "runner 不看工具列表"，不是"钩子不执行"。
- **在面板里「停用」该内置工具不会阻止注入**：核心的 `get_builtin_tool()` 与工具集序列化
  都不看 `active`，`kb_agentic_mode = true` 的核心路径同理。
- **`modalities` 的语义随核心版本翻转，所以只认一种否定信号**：`modalities=[]` 在 4.29.1
  是「未配置，默认支持全部模态」（`tool_loop_agent_runner.py:665-669` 的注释与 `and modalities`
  条件），在 4.24.5 / 4.23.0 却是「不支持」并会清空工具。本插件因此**只**把「非空 list 且
  不含 `tool_use`」当否定信号，`None`／`[]`／缺键一律放行 —— 代价是 4.23 上 `[]` 仍可能被
  核心清空后又被插件塞回去。这个差异还意味着：**`modalities=None` 在 4.23.0 会让核心
  自己在构建期抛 `TypeError`**（发生在钩子之前），这不是插件能救的。
- **4.23–4.26 没有 `get_using_provider_async()`**（4.27.5 才引入）：插件用 `getattr` 能力
  探测后回退同步 `get_using_provider(umo=...)`。同步版内部走 `sp.get` 的专用同步 SQLite
  连接做一次点查，**实测 median 1.6158 ms（p95 2.2301 ms / p99 2.5916 ms）**，只阻塞调用
  线程（即事件循环线程）、不碰 async 连接池、不会死锁 —— 实测那 1.6 ms 期间同 loop 上的
  其他协程确实得不到调度（100 次连续同步读时定时器只跑了 1 拍）。**只有 4.23–4.26 这几个
  旧版本会走这条同步路径**（4.27.5+ 走 async 版，实测 median 1.0791 ms、不阻塞），所以
  这不是当前生产的常态成本。两方法都不存在时插件静默放行（照常注入，也不新增日志）。
- **图片请求会按未知处理**：`req.image_urls` 非空且主 provider 的 `modalities` 不含 `image`
  时，核心会把这次请求切给 fallback provider（`core/astr_main_agent.py:1412-1443`），
  「当前 provider 不支持工具」就不再等于「这次请求的 provider 不支持工具」，插件按支持处理。
- 知识库检索依赖外部 **embedding / rerank** 服务。网络慢时核心可能触发 120 秒工具超时
  （日志为 `tool astr_kb_search execution timeout after 120 seconds`），属环境问题。
- 插件**不产生任何持久化数据**：不写文件、不建表、不改配置，因此重载/热重载都安全。

## 7. 背景：为什么会有这个插件

某部署的 persona 里写着「先查知识库再回答」，并把知识库描述成一棵**文件夹树**
（`冬马和纱知识库_TE/`、`70_附录_边界速查` …），却没给路径、也没给工具；而
`kb_agentic_mode=false` 让 `astr_kb_search` 根本没有注册。

**结果：模型把「查知识库」理解成「搜磁盘」**，用代码执行工具 `os.walk` 扫遍 `C:\` 与 `D:\`，
跑满 30 步工具调用、耗时 12 分钟才回复，同一现象连续复现三次。

本插件解决的是「**模型手上有工具**」这个必要条件：它不保证模型不再尝试读文件，
persona 里也不要再把知识库描述成目录树。

## 8. 与核心的关系

本插件**不修改核心的任何行为**，只往这次请求的 `func_tool` 里追加一个工具：
不改 `kb_agentic_mode`、不改工具实现、不 monkeypatch、不做持久化。

- `kb_agentic_mode = true` 时核心自己也会加同名工具；本插件先 `get_tool()` 判重，
  已存在就什么都不做 —— 不会出现两个同名工具，也不会替换核心那个实例。
- 0.3.0 起多了一步**只读**的能力判定：读「当前会话实际使用的 provider」的 `modalities`
  （会话级 `selected_provider` 优先，与核心 `_select_provider` 同序），据此决定这次要不要
  注入。只读，不改 provider、不改配置；关掉 `respect_provider_tool_support` 就完全跳过这一步。
- 钩子 `priority = -310000`：核心按 priority 降序执行钩子，负数表示靠后，所以本插件排在
  普通插件（0）之后追加工具；但这**不保证绝对最后**。判重保护的是"我们不去覆盖别人"；
  "被别人覆盖"只能靠排在后面，而那并不保证。
- 生命周期：没有后台任务、定时器、文件句柄，因此没有 `terminate()` 清理逻辑。

### 版本兼容

| AstrBot | 结论 |
|---|---|
| 4.23.0 | **支持**（声明下界）：`sp` / `ToolSet` / `get_config(umo=)` / `get_builtin_tool()` 均在；**没有 `get_using_provider_async()`**，0.3.0 的能力判定走同步 `get_using_provider` 回退，已在真实源码树上跑通 |
| 4.24.5 / 4.25.6 / 4.26.8 / 4.27.5 | 用到的公开面与注入位置已逐 tag 核对一致；`get_using_provider_async()` 自 4.27.5 起存在（更早版本自动回退同步）；未做端到端实跑 |
| 4.28.1 | **支持**，已在真实源码树上跑通 |
| 4.29.1 | **支持**（当前生产版本），已在真实源码树上跑通（能力判定走 async 版） |

## 开发

```bash
python -m pytest -q          # 脱网、不启动 AstrBot、不写任何真实数据目录
python -m ruff check .
python -m ruff format --check .
```

测试用 `tests/_astrbot_stub.py` 造了一个只含 `astrbot.api` 公开面的最小替身，
里面**没有** `astrbot.core`：插件一旦退化回私有导入，测试会立刻失败。
校验 `metadata.yaml` 需要 `PyYAML`（AstrBot 核心自身也依赖它）。

### 契约冒烟（可选，需要真实 AstrBot 源码）

`tests/contract_smoke.py` 在**真实核心**上验证：公开面导入、`star_map` 注册（删掉
`@register` 后仍被识别）、钩子 `priority`、真实 `ProviderRequest` 注入与实例同一性、
导入期无 `DeprecationWarning`，以及**版本差异**：真实 `Context` 到底有哪些 provider 查询
方法（4.23–4.26 没有 `get_using_provider_async`），并按那个形状跑一次「`modalities=["text"]`
→ 跳过 / `modalities=None` → 放行」。它**默认跳过**（缺 `ASTRBOT_SRC`），不影响上面的
`pytest -q`；设置环境变量后单独跑：

```powershell
$env:ASTRBOT_SRC = 'D:\AstrBot\backend\app'   # 4.29.1（生产源码，只读）
& $py -m pytest tests/contract_smoke.py -q

$env:ASTRBOT_SRC = '<导出目录>'                # 4.23.0（声明下界）
& $py -m pytest tests/contract_smoke.py -q
# 导出下界源码树：git archive v4.23.0 astrbot | tar -x -C <导出目录>
```

## 许可

MIT
