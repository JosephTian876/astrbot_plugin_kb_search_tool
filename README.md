# 知识库检索常驻 · astrbot_plugin_kb_search_tool

让内置的知识库检索工具 `astr_kb_search` **始终可用**，同时**保留知识库自动注入**。

---

## 解决的问题

AstrBot 核心的 `astr_main_agent._apply_kb()` 是 **if/else 二选一**：

```python
if not config.kb_agentic_mode:
    if req.prompt is None or not req.prompt.strip():
        return                      # ← 纯附件消息（只发视频/图片）直接跳过检索
    ... 自动把 KB 结果注入 extra_user_content_parts
else:
    req.func_tool.add_tool(KnowledgeBaseQueryTool)   # ← 给工具，但不再自动注入
```

于是只有两个都不完美的选项：

| | 自动注入 | `astr_kb_search` 工具 |
|---|---|---|
| `kb_agentic_mode=false` | ✅ 有文字时 | ❌ **没有** |
| `kb_agentic_mode=true` | ❌ 没有 | ✅ 有 |

**本插件让你两者兼得**：保持 `kb_agentic_mode=false`，由本插件在
`on_llm_request` 阶段把 `astr_kb_search` 直接塞进 `req.func_tool`。

---

## 为什么这很重要（实测教训）

某部署的 persona 里写着：

```
任何有关设定、剧情的内容，先查知识库再回答
你的记忆来自知识库文件夹 `冬马和纱知识库_TE/`
文件夹速览：- `70_附录_边界速查` ...
冲突时的权威顺序：`70_附录` ＞ 游戏原文亲历(30/50) ＞ ...
```

persona 把知识库描述成一个**文件夹树**，却没给路径、也没给工具；而
`kb_agentic_mode=false` 让 `astr_kb_search` 未注册。

**结果：模型把「查知识库」理解成「搜磁盘」**，用 `astrbot_execute_python` +
`os.walk` 扫遍 `C:\` 与 `D:\`：

```
 1. grep "哪怕本人不在那里"              ← 它在视频里看到的台词
 2. os.walk(D:\AstrBot)
 8. Get-ChildItem D:\ -Recurse | Select-String "..."
20. os.walk(用户主目录) 找视频文件名
36. Get-ChildItem C:\, D:\ -Depth 4 -Filter "*知识边界矩阵*"
50. pypdf 打开 WA2 全文本 PDF
```

跑满 **30 步工具调用、耗时 12 分钟**才回复，且**连续复现三次**
（04:23 / 05:04 / 06:22）。

装了本插件后，模型说「查知识库」时有真正的工具可用，**不必碰文件系统**。

---

## 安装

把本插件目录放进 AstrBot 的 `data/plugins/`，重启或重载即可。无第三方依赖。

**保持 `kb_agentic_mode=false`** —— 这样自动注入照常工作，工具由本插件提供。

---

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 总开关 |
| `only_when_kb_configured` | `true` | 仅在 `kb_names` 非空时注入工具，避免给模型一个查不到东西的工具 |

---

## 与核心的关系

本插件**不修改**核心的任何行为，只在请求上追加一个工具。核心在
`kb_agentic_mode=true` 时也会注册同名工具，此时本插件检测到已存在便不再重复添加。

启动时会自检内置工具是否可取得，取不到会打 ERROR 并停用自身。

---

## 许可

MIT
