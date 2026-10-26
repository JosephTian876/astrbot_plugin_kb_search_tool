# 更新日志

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
