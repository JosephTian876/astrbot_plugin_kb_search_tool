# 更新日志

## 0.3.0 - 2026-10-27

注入前判定 provider 的工具能力 + 配置值健壮性。**默认开启**，新增 1 个配置键；
关掉 `respect_provider_tool_support` 后的行为见下面「与 0.2.0 的关系」。

> **版本号说明**：本批改动最初记为 0.2.1，但从未发布（没有 tag、市场未收录、生产装的还是
> 0.2.0）；按 SemVer 这属于 minor（新增配置键 + 默认行为变化），R4 定稿为 **0.3.0** ——
> 两个号合并成这一个版本，未被采用的那个不会发布。

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
- **非标准 `func_tool` 不再每请求刷 ERROR**（破例修掉一条与开关无关的刷屏路径，见下节）。

### 与 0.2.0 的关系（承诺范围，0.3.0 收窄并改写）

关掉 `respect_provider_tool_support` 后：

> **不查询 provider（0 次）**、**注入决策与 0.2.0 完全一致**、**日志行为不相对于 0.2.0
> 变化** —— 除下面两处**已声明**的差异（都不影响判定结果，且都只在 DEBUG 级或异常输入下
> 可见）：
>
> 1. **非标准 `func_tool`**（别的插件把 `req.func_tool` 换成了缺 `get_tool`/`add_tool`
>    的对象）：0.2.0 是「每请求一条带 traceback 的 ERROR」，0.3.0 是「一次性 WARNING +
>    跳过注入」。这是**降噪**，也是本插件唯一会无限刷 ERROR 的路径。
> 2. 「注入成功」那条 DEBUG 末尾多了 `判定对象=…`（开关关闭时写
>    `未查询（respect_provider_tool_support=false）`）。

为什么改口径：旧版本写的是"142/1260 个场景观测 JSON **SHA256 完全相同**"。那套矩阵
harness 在仓库外、谁也无法复跑，而它恰好唯一挡住了上面那条 ERROR 刷屏路径；而被它
保护的读者（想退回旧行为的用户）在意的是"结果一样"，不是"日志字节一样"。现在这条承诺
由仓库内的 `test_29` 直接钉住（14 个场景：7 种 `modalities` × 有无知识库，断言 provider
查询 0 次、注入结果相同、日志级别/条数/文案逐条相同），离线矩阵对拍降级为**一次性**的
开发期证据（不入库，不作为承诺的一部分）。


### 配置健壮性

手改配置（JSON 里的字符串 `"false"`、拼错的键名）此前会**静默**反转或忽略用户意图，现在
变成可观测的：

- 布尔配置项（`enable` / `only_when_kb_configured` / `respect_provider_tool_support`）走
  **显式解析**：真布尔值、`1`/`0`、`"1"`/`"0"`/`"true"`/`"false"`/`"yes"`/`"no"`/
  `"on"`/`"off"`（忽略大小写与首尾空白）。此前 `bool("false")` 为 `True`，把闸门留在了
  "开"，用户以为已经关掉。**这一半是真实风险的来源**：实测核心只删 schema 之外的键、
  不纠正类型（`tests/probe_core_config.py`，4.23.0 / 4.29.1 一致：插件拿到的仍是字符串
  `"false"`）。
- 认不出的值 → **回落默认值**（不是"按 Python 真值"）并 WARNING 一次，文案带键名、实际值与
  所用默认值。
- 未知配置键 → 一次性 WARNING 列出键名与可用键。**诚实说明它是兜底**：正常加载路径上核心的
  `AstrBotConfig` 已经先把 schema 之外的键删掉并打一条 INFO（4.29.1 `Config key removed:
  <key>`；4.23.0 `检查到配置项 <key> 不存在，将从当前配置中删除`）并写回文件，所以这条
  WARNING 在日常安装里一般不会出现；它守的是"核心把原始 dict 直接交给插件"（测试、第三方
  加载器）与将来核心行为变化。
- 真布尔值路径**行为不变**（与上面同一条承诺：注入决策与日志行为相对于 0.2.0 的变化只有
  本版已声明的那两处）；新日志只出现在"配置异常"场景，且都只报一次（发生在加载期，
  不按请求刷屏）。另有一次性的离线矩阵对拍（1260 场景，不入库、不作为承诺的一部分）：
  开关关闭时 provider 查询 **0 次**。

### 兼容

- `get_using_provider_async()` 是 **4.27.5** 才有的 API；本插件声明 `>=4.23`
  （`metadata.yaml`），因此做 `getattr` 能力探测并回退到 4.23–4.26 的同步
  `get_using_provider(umo=...)`；两者都没有时**静默**放行（不新增日志，保持 0.2.0 的
  成功路径只留 1 条 DEBUG）。真实 4.23.0 源码树的契约冒烟输出：
  `provider_api={async:false, sync:true, by_id:true}`、
  `provider_gate={modalities_text:skip, modalities_none:inject, via:sync}`；
  4.29.1：`async:true`、`via:async`。
- 同步回退走 `sp.get` 的专用同步 SQLite 连接（一次点查，**实测 median 1.6158 ms /
  p95 2.2301 ms**，只阻塞调用线程、不碰 async 连接池），且只在 4.27.5 以下发生；4.27.5+ 走
  async 版（实测 median 1.0791 ms）。**只有 4.23–4.26 这几个旧版本会走这条同步路径**：
  实测 100 次连续同步读期间事件循环的定时器只跑了 1 拍（最大间隔 159 ms），即那 1.6 ms/请求
  确实阻塞事件循环 —— 可接受的原因是它只出现在旧版本上，且不碰连接池、不会死锁。
  （R3 复验数据；初版写"约 1 ms"，偏乐观约 45%。）

### 日志

- 新增 WARNING（每个 provider+model 一次）：
  `[KBTool] 当前 provider「<id>」（模型 <model>）的模型能力未勾选 tool_use，已跳过注入；如需强制注入请关闭 respect_provider_tool_support。`
- 新增 DEBUG（未知原因，按原因去重、每插件生命周期一次）：
  `[KBTool] 无法判定当前 provider 是否支持工具调用（<原因>），按支持处理。`
  去重键是「异常类型 + 消息前 80 字」：同类型不同原因各留一条，不会只看到第一条。
  该去重集合**有 64 条上界**：键里含异常消息摘要，上游只要给出带可变内容的消息（端口、
  超时毫秒、umo、URL），每条都是一次新键 —— 上界防止它被外部输入无限喂大。到顶后清空
  重来（只影响"同一条 DEBUG 会不会再出现一次"，不影响任何判定）。
- 新增 WARNING（加载期各一次）：配置项的值无法识别（回落默认值）、配置里有无法识别的键。
- 新增 WARNING（每插件生命周期一次）：内置工具注册表返回 `None`（没抛异常）—— 此前是零日志
  静默失效。它与"抛异常"那条 ERROR 的文案不同：前者是名字没登记，后者是注册表/导入坏了。
- 「无可用知识库」那条 WARNING 的文案补上「**本条每个插件生命周期只提示一次**」。它一直是
  全局一次（不是每会话一次），旧文案只写"当前会话与主配置都没有可用知识库"，读者会以为
  第二个会话还会再报一条 —— 实际是静默的。
- 「注入成功」的 DEBUG 补上判定对象：
  `[KBTool] 注入 astr_kb_search，工具数=N，判定对象=<provider id>（模型 <model>）`。
  没查 provider 时**不编造**，写明原因：`未查询（respect_provider_tool_support=false）`、
  `未查询（静态预筛：已加载 provider 均未声明缺 tool_use）`、`未知（原因见上一条 DEBUG）`。
  （该行只在 DEBUG 级可见；默认 INFO 下开关关闭时的日志与 0.2.0 完全一致。）
- 启动 INFO 不变（`test_01` 仍断言成功路径恰好 1 条 DEBUG）。

### 性能

- **静态预筛（O1）**：provider 闸门前先做一次纯内存判断 —— 「配置里的 chat provider 条目」与
  「已加载的 chat provider 实例」（`context.get_all_providers()`）**id 集合完全一致**，且两边
  都没有声明「非空且缺 `tool_use`」时，无论核心这次解析到哪个 provider，判定都只能是"支持"，
  于是那次数据库点查被整段跳过。
  - 实测（真 4.29.1 + 真 SQLite，5 轮交替取同轮配对差的中位数，
    见 `work\kb-030-artifacts\bench_p2_1_prefilter.py`；两次独立跑的数字）：
    - provider 闸门单独：**1.1247 / 1.1975 ms → 0.0026 / 0.0027 ms**，
      省 **1.1222 / 1.1948 ms** 每请求（同机时段抖动约 ±10%，两次都量到同一量级）。
    - 两个闸门都开（生产默认）：**2.1638 / 2.2945 ms → 1.1296 / 1.0600 ms**，
      省 **1.05 / 1.20 ms** 每请求；剩下那 1.1 ms 是知识库那次查询，本版不动它。
    - 有 1 个 provider 声明 `["text","image"]` 时（预筛必须退回）：
      2.3361 / 2.2510 ms vs 基线 2.1638 / 2.2945 ms，配对差 −0.16 / +0.01 ms ——
      预筛扫描本身约 0.4 µs，落在噪声里，**没有回归**。
    - R8 复测（同一台机器，真 4.29.1，2000 次采样）：预筛入口 **median 0.0029 ms /
      p95 0.0031 ms**（多读一次 `get_config(umo)` 后从 0.0004 → 0.0029 ms），
      被替换掉的那次点查 **median 1.2098 ms / p95 1.7109 ms**。
  - 保守性（可验证的边界，不是"绝不"）：预筛只可能"退回查 DB"，**不会改变"判定为不支持"的
    语义**。会话级 `selected_provider` 存在时直接不做预筛（它可能解析到
    `get_all_providers()` 里没有的非 chat provider）；`get_all_providers` 缺失、抛异常、
    返回空、返回值不是可数序列，配置里没有 `provider` 段，两边 id 集合不一致，或任一
    provider 声明了 `tool_use` 缺失，都照旧走完整判定。
  - **为什么要求"配置 id 集合 == 加载表 id 集合"**（0.3.0 修复的一个真实缺口）：核心删改
    provider 时先把实例移出 `provider_insts`（4.29.1 `core/provider/manager.py:883`）、再
    `await terminate()`（`:901`，openai 源是 `await client.close()`，真的会让出事件循环）、
    最后才 `del inst_map[...]`（`:908`）。这个**亚毫秒窗口**（实测 median 0.020 ms /
    max 0.071 ms）里 `get_all_providers()` 已看不到它，而核心解析 provider 用的
    `inst_map.get()` 仍能返回它 —— 只扫 `get_all_providers()` 的预筛会"证明支持 → 跳过
    点查 → 把工具注入给一个声明不支持的 provider"（真 4.29.1 上可复现）。配置与加载表由
    不同代码路径更新（`delete_provider` 先 terminate 再改配置，`:925`；`update_provider`
    先改配置再 reload，`:950`），所以窗口期两边 id 集合必然对不上，预筛自动回落。
    **残留边界**：第三方代码绕过 `ProviderManager` 直接往 `inst_map` 塞一个配置里没有的
    实例、同时把配置条目删掉时，预筛看不到它（本插件不读 `inst_map`，那是私有面）。
  - 不新增任何日志、不改变任何判定结果：静默预筛，命中与否只看"有没有那次点查"。

### 配置与文档

- `_conf_schema.json` 新增 `respect_provider_tool_support`（`bool`，默认 `true`）。
- 面板 `hint` 重新定位为"**问号按钮悬停时能读完的摘要**"：三个 hint 从合计 619 字压到
  **150 字**（`respect_provider_tool_support` 76 字 / `only_when_kb_configured` 46 字 /
  `enable` 28 字），判据全文移到 README「配置项」下的「完整判据」小节。关键项
  `respect_provider_tool_support` 加 `"obvious_hint": true`（面板会显示 ‼️）。
  合法性已在两个版本上核对：4.23.0 与 4.29.x 的 dashboard 都读 `obvious_hint`
  （`dashboard/src/components/shared/AstrBotConfig.vue`），而核心的
  `AstrBotConfig._config_schema_to_default_config` 只读 `type`/`default`/`items`，
  多余字段不会被拒绝（4.23.0 同款实现）。
- README：配置表新增一行；新增「完整判据」小节；「自查」补两条日志的含义与一张
  **「症状 → 排查三步」**表（症状优先，用户是带着"工具没出现"来的）；
  「边界」补 `modalities` 的跨版本语义、4.23–4.26 的同步回退、图片 fallback；
  「与核心的关系」「版本兼容」同步，并说明静态预筛。
- README「配置项」补一段取值约定（哪些写法算布尔值、认不出会怎样、键名拼错会怎样）。

### 测试

- 单测 **15 → 56**（+41）：`modalities` 判定表逐行覆盖 —— `modalities` 的 7 种形态
  （缺键/`None`/`[]`/字符串/dict/tuple/int）、取不到 provider、非聊天 provider、
  `ValueError` 路径、探测抛异常、旧版 Context 只有同步 API、Context 完全没有 provider API
  （静默）、`selected_provider` 三种走向、图片 fallback 三种组合、关掉知识库闸门仍注入、
  开关关闭、配置键 1:1。
- 单测 **56 → 144**（+88，R4 收口）：布尔白名单 3 键 × 15 种写法、认不出的值 3 键 × 10 种、
  字符串 `"false"` 真关闸门（provider 一次都不查）、未知键告警、`None` 工具告警与自愈、
  异常去重（同类型两种原因 / 同类型超过 80 字）、版本号 0.3.0 与文档一致性、schema 键 1:1。
- 单测 **145 → 209**（+64，R5/C2；本轮起点实测 145，上一行 R4 的 144 是历史数字，未改）：契约冒烟的假绿守卫（23 例，含"回落到另一棵树"的
  复刻）、审计文档不在仓库且零悬空引用、探针无本机绝对路径、静态预筛（跳过/退回/不得
  放行/无该方法/抛异常/selected_provider/开关关闭/纯函数真值表/拒绝 Mapping）、hint 长度
  与 `obvious_hint`、README 症状表与完整判据、等价承诺的措辞与可复验性、日志卫生（判定
  对象、不编造、标签不串请求、64 上界、无知识库文案）、非标准 `func_tool` 的三种形状。
- 契约冒烟新增两条真树断言（`Context` 的 provider 方法面；按该形状跑跳过/放行），
  4.29.1 与 4.23.0 均通过。
- 契约冒烟的**假绿**已修：父测试此前只看"子进程退出码 0 + 有 CONTRACT_OK"，而
  `ASTRBOT_SRC` 指错目录时子进程会静默回落到 venv 里已安装的 AstrBot —— 实测指向空目录
  仍报 `1 passed`（测到的是 4.28.1，不是你要的树）。现在父测试从子进程输出里抠出
  evidence JSON，用 `astrbot.__file__` 反查包路径，要求它**严格位于** `ASTRBOT_SRC` 之下，
  并顺带核对 `provider_api` / `provider_gate`。判定逻辑在 `tests/_contract_evidence.py`，
  由 `tests/test_contract_evidence.py` 在常规 `pytest -q` 里覆盖（放进会模块级 skip 的
  `contract_smoke.py` 就等于永不执行）。
- 全新跑过的三棵树：4.23.0（`via: sync`）、4.28.1、4.29.1（`via: async`）各 `1 passed`；
  空目录与"多一层（`…/app/astrbot`）"各 `1 failed` —— 绿与红都重新有意义了。
- 独立复验（R3，自建 harness，**不入库、不作为承诺的一部分**）：1260 场景 × 两实现，
  开关关闭时 provider 查询 **0 次**；真实 4.23.0 / 4.29.1 `Context` 上按版本选对 API，
  无 `AttributeError` 参与判定。
- `pytest -q` / `python -m compileall` / `uvx --from ruff==0.15.22 ruff check .` /
  `ruff format --check .` 全绿。

### 已知边界（明确不修）

- **「配置 ≠ 真实能力」**：`modalities` 是用户手填的；模型其实支持函数调用、只是没勾
  `tool_use` 时会被跳过（WARNING 会指出来）。这是「尊重配置」的固有代价，关掉开关即可恢复。
- **4.23.0 的 `modalities=[]`**：核心当「不支持」并清空，本插件按「未配置」放行 —— 同一
  配置值跨版本语义翻转，无法两全；4.23 用户请显式勾 `tool_use` 或升级。`modalities=None`
  在 4.23.0 会让核心自己在构建期抛 `TypeError`（发生在钩子之前），插件无法救。
- ~~**非标准 `func_tool`** 仍是 0.2.0 的行为：ERROR 一次 + 不注入~~ —— **0.3.0 已处理**：
  一次性 WARNING + 跳过注入，与开关无关，绝不每请求刷 ERROR（见「与 0.2.0 的关系」第 1 条）。
  这条是本版唯一破例修改的"非公开契约边缘"，理由是旧行为会成为唯一无限刷 ERROR 的路径。

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
