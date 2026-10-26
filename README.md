# 知识库检索常驻 · astrbot_plugin_kb_search_tool

让 AstrBot 内置的知识库检索工具 `astr_kb_search` **在每次 LLM 请求里都可用**，
同时**保留知识库自动注入** —— 这是核心 `kb_agentic_mode` 二选一做不到的组合。

---

## 1. 适用前提

本插件只在下面三个条件同时成立时才有意义：

- **走内置 Agent**：`agent_runner.runner_type = local`。核心的知识库逻辑（自动注入与
  `kb_agentic_mode` 开关）只属于内置 Agent；Dify / Coze 等第三方 Agent 运行器不读它。
- **当前 provider 支持函数调用（tool use）**：核心自己的配置项也写着这个前提——
  「Agentic 知识库检索……需要模型支持函数调用能力」。而且 4.23.5 起，provider 的
  「模型能力」里若没勾选「工具使用」（`tool_use`），核心会把本次请求的全部工具丢弃
  （连本插件注入的这个也丢）。
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

- **插件市场**：WebUI → 插件 → 插件市场 → 搜索「知识库检索常驻」→ 安装。
- **命令行**：

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
| `only_when_kb_configured` | `true` | 只在「当前会话有可用知识库」时注入。判定顺序与核心检索一致：**会话级知识库设置优先**，主配置 `kb_names` 兜底；两者都没有时不注入。关闭后总是注入。 |

## 5. 如何确认它在工作

1. **加载日志**（控制台或 `data/logs/`）：`[KBTool] 已加载 | 启用=True | 工具=astr_kb_search`
   —— 这行说明插件起来了；显示 `启用=False` 则说明插件被自己的开关关掉了。
2. **注入日志**：把本插件的日志级别调到 DEBUG（WebUI → 插件 → 该插件），每次请求都会出现
   `[KBTool] 注入 astr_kb_search，工具数=N`。默认 INFO 级别下不输出，不构成噪音。
3. **两条失败日志的含义**（都自带下一步动作）：
   - `[KBTool] 取不到内置工具 astr_kb_search（后续请求会自动重试）: ...`
     → 核心的内置工具注册表不可用（版本低于 4.23，或某个内置工具模块导入失败）。
     环境修好后下一个请求会自动恢复，不需要重装插件。
   - `[KBTool] 当前会话与主配置都没有可用知识库，已跳过注入工具；...`
     → 不是故障。要么去配置里选知识库，要么关掉本插件的 `only_when_kb_configured`。
4. **行为验证（最可靠）**：在对话里发「用知识库检索工具查一下 XXX」，
   模型这次的工具调用里出现 `astr_kb_search` 即为生效。

## 6. 适用范围与边界

- **只覆盖内置 Agent 的主对话链路**（`runner_type = local`）。cron 定时任务、后台任务唤醒、
  第三方 Agent 运行器都不经过 `on_llm_request`，本插件对它们无效。
- **在面板里「停用」该内置工具不会阻止注入**：核心的 `get_builtin_tool()` 与工具集序列化
  都不看 `active`，`kb_agentic_mode = true` 的核心路径同理。
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
- 钩子 `priority = -310000`：核心按 priority 降序执行钩子，负数表示靠后，所以本插件排在
  普通插件（0）之后追加工具；但这**不保证绝对最后**。防同名覆盖靠的是判重，不是顺序。
- 生命周期：没有后台任务、定时器、文件句柄，因此没有 `terminate()` 清理逻辑。

### 版本兼容

| AstrBot | 结论 |
|---|---|
| 4.23.0 | **支持**（声明下界）：`sp` / `ToolSet` / `get_config(umo=)` / `get_builtin_tool()` 均在，已在真实源码树上跑通 |
| 4.24.5 / 4.25.6 / 4.26.8 / 4.27.5 | 本轮用到的公开面（`astrbot.api` 的 `ToolSet`/`sp`/`logger`）与注入位置已逐 tag 核对一致；未做端到端实跑 |
| 4.28.1 | **支持**，已在真实源码树上跑通 |
| 4.29.1 | **支持**（当前生产版本），已在真实源码树上跑通 |

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
导入期无 `DeprecationWarning`。它**默认跳过**（缺 `ASTRBOT_SRC`），不影响上面的
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
