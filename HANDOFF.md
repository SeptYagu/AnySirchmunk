# AnySirchmunk 交接：审查报告核验 + AnyTXT v1 专项适配

日期：2026-09-15（第二轮）

分支：`main`　上游基线：Sirchmunk `3c7ee54f93fa198db2020a3ab850356f2dacff72`

本轮性质：**只做核验与方案，不修改实现代码**。本轮交付物是本文件。

被核验对象：`daefa4c`（`fix: harden AnyTXT request safety and delivery`）及其新增的上一版 `HANDOFF.md`
所列全部发现与"已修复"声明。

## 0. 结论摘要

1. **审查报告基本属实**：7 条发现中 6 条完全属实且修复真实生效，1 条（文档一致性）**部分属实**——
   默认值与信封已改，但 `docs/requirements.md` §8、`docs/anytxt-capabilities.md` §1/§7 仍停留在
   1.3.2477 + `9920` 的旧基线和一条已被推翻的结论。
2. **独立复现全部通过**：39 个契约测试（Python 3.14.3 与 3.13.12 各跑一次）、补丁在锁定 commit 上
   `apply --check`/应用/`py_compile`/`--reverse --check`、交付适配器与仓库源码逐字节一致（仅行尾
   CRLF/LF 差异）、`baseline.json` 两个 requirements 哈希 MATCH。
3. **反空转验证通过**：把当前测试跑在**旧实现**（`daefa4c^`）上有 5 个用例失败；把互斥逻辑从闸门里
   删掉后有 3 个并发用例失败（并打印出真实重叠 `{'search': 3, 'fragment': 1}`）。断言不是空转。
4. **本轮另发现 8 项问题**（2 项 P2 与 v1 迁移直接相关）：`SearchPage.count` 解析后从未被消费、
   v1 业务码 `result.errno` 未检查、`ANYTXT_SEARCH_LIMIT` 无 1..300 校验、片段文本携带 AnyTXT
   高亮标记 `*<<*`/`*>>*`、`valid_received` 死代码等。
5. **v1 专项适配评估已完成**，方案见 §4：核心不是"换个 URL"，而是把 v1 的
   `search` 精确计数、原生 `&`/`|`/`!`/`"短语"` 语法、`status`、`getFragmentAll` 用起来，
   同时删掉 `legacy` 的全部代码路径、配置项、文档与探针选项。

## 1. 核验方法

| 手段 | 目的 |
| --- | --- |
| 逐条对照 `daefa4c` 的 diff、仓库实际源码与补丁文本 | 判断"已修复"是否落地 |
| 在临时 worktree（`git worktree add --detach <锁定sha>`）上 apply / compile / reverse | 复现交付声明 |
| `unittest discover`（3.14.3 + 3.13.12） | 复现"39 个测试通过" |
| 把**当前**测试跑在**旧实现**上（`git show daefa4c^:...` 放入同构目录树） | 反空转：证明新断言依赖新行为 |
| 定向删除互斥条件后重跑并发用例 | 证伪：证明测试真能抓到回归 |
| 对真实 1.3.3541 端点做只读探测（顺序、低负载、记录 PID） | 独立确认 v1 契约与 `status`/`count`/表达式事实 |
| 读上游 `text_retriever.py` 交叉验证 `fallback_used` 语义 | 无测试覆盖项的源码级核查 |

复现命令（可原样重跑）：

```bash
# 测试
python -m unittest discover -s tests            # 39 tests OK
# 补丁三重校验
git worktree add --detach <tmp> 3c7ee54f93fa198db2020a3ab850356f2dacff72
git -C <tmp> apply --check --whitespace=error-all patches/sirchmunk-3c7ee54-anytxt.patch
git -C <tmp> apply --whitespace=error-all patches/sirchmunk-3c7ee54-anytxt.patch
python -m py_compile <tmp>/src/sirchmunk/{agentic/tools.py,cli/cli.py,search.py,retrieve/anytxt_retriever.py}
git -C <tmp> apply --reverse --check --whitespace=error-all patches/sirchmunk-3c7ee54-anytxt.patch
# 反空转
git show daefa4c^:src/sirchmunk/retrieve/anytxt_retriever.py > <tree>/src/sirchmunk/retrieve/anytxt_retriever.py
python -m unittest discover -s <tree>/tests -t <tree>
```

## 2. 核验结果

### 2.1 逐条判定

| # | 审查发现（摘要） | 声称的处理 | 判定 |
| --- | --- | --- | --- |
| 1 | 安装模板把默认 v1 方法与 legacy `9920` 配对，并把安全并发 2 覆盖为 4 | 两份模板统一为 `API_MODE=v1` + `9924/rpc` + 并发 2，补片段预算与全局根目录 | **属实** |
| 2 | `_KindGate` 属于单个客户端实例，并发请求可用不同客户端绕过互斥 | 改为按规范化 API URL 共享的进程级 `_EndpointGate` | **属实** |
| 3 | `urlopen` 的真实传输超时被包装后不会重试；`to_thread` 超时后底层线程仍运行并过早释放互斥 | 新增 `AnyTXTRequestTimeout`；超时只重试一次；闸门由工作线程持有到传输结束；排队请求收到取消标志后不再发送 | **属实** |
| 4 | 片段字符预算在 RPC 之后检查，达到上限仍继续请求，末片段截断也可能标记完整 | 请求前检查剩余字符，发生截断立即标记 `fragment_budget` 并停止后续片段请求；metadata 增加三类请求计数 | **属实** |
| 5 | 缺少 `fid` 或文件已不存在的索引记录仍进入证据链 | 通过范围/过滤后校验 `fid` 与真实文件，跳过并标记 `invalid_record`；AND/NOT 因集合不完整触发既有保护 | **属实** |
| 6 | `KeywordSearchTool` 日志与结构化返回对 `fallback_used` 计算不一致 | 两处均改为 `bool(fallback_reasons)` | **属实但无测试覆盖** |
| 7 | 需求、架构、README 同时保留 legacy 默认与 v1 默认的矛盾 | 已修当前默认值、方法信封、超时说明；探针默认改 v1 | **部分属实**（见 2.3） |

### 2.2 证据

- **#1**：补丁中恰有 2 处模板（`config/env.example`、`src/sirchmunk/cli/cli.py`），
  `+ANYTXT_API_MODE=v1`/`+ANYTXT_API_URL=http://127.0.0.1:9924/rpc`/`+ANYTXT_MAX_CONCURRENCY=2`/
  `+ANYTXT_MAX_FRAGMENT_REQUESTS=100` 各出现 2 次，无 `+ANYTXT_MAX_CONCURRENCY=4`。
  README「计划中的配置」、`requirements.md` FR-1/FR-2/FR-8 与之逐项一致。
- **#2/#3**：`_ENDPOINT_GATES`（`weakref.WeakValueDictionary` + 锁）+ `tighten()` 取最严值；
  `_post` 的 `finally: self._endpoint_gate.release()` 位于 `urlopen` 之后；
  旧实现是 `except (HTTPError, URLError, OSError, TimeoutError) → AnyTXTBackendError`（不重试）且
  `_KindGate` 挂在客户端实例上。
  **反空转**：当前测试跑在旧实现上 → `test_transport_timeout_is_retried_once` ERROR，
  `test_different_clients_share_the_endpoint_gate`、`test_timed_out_worker_keeps_cross_kind_gate_until_transport_finishes` FAIL。
  **证伪**：把 `acquire()` 里的 `self._kind != kind` 条件删掉（保留并发上限）后，
  3 个并发用例 FAIL 并打印 `GetResult and GetFragment were in flight together: [{'search': 3, 'fragment': 1}, ...]`。
- **#4/#5**：diff 中预算检查从 RPC 之后移到之前、`fragments_exhausted` 置位、`_candidate` 新增两处
  `ValueError`、调用方 `except ValueError → invalid_record`。
  两个对应用例在旧实现上 FAIL。
  旁证：1.3.3541 的 `ATGUI_20260915-032858.log` 确有
  `Failded to get file[-8140523984612571312] info from index`（03:52/03:53），说明索引记录失效是真实现象，
  且出现负 `fid`。
- **#6**：`agentic/tools.py` 两处均为 `bool(fallback_reasons)`。
  源码交叉验证：上游 `text_retriever.py:578/617` 在 rga 回退时设置 `result["fallback_reason"]`，
  `:727-729` 将 `_search_backend`（`rga`/`rg`）与 `_fallback_reason` 写入每条 match。
  因此旧表达式 `"rg" in search_backends` 会**漏报** anytxt→rga 回退，新表达式是超集，语义正确。
  **但 7 项"新增回归覆盖"里没有任何一项覆盖它**（测试只断言 retriever 侧 metadata 的 `fallback_reason`）。
- **#7** 见下。

### 2.3 唯一未完全闭合项：#7 文档一致性

已改：README 配置块与前置条件、`requirements.md` FR-1/FR-2/FR-8、
`architecture.md` §4.2（v1 为主、legacy 仅作等价示意）、`anytxt-capabilities.md` §4.4，
`scripts/anytxt_stability_probe.py` 默认 `--api v1`。

未改（仍是旧基线，与 `baseline.json` 的 `verified_anytxt_version: 1.3.3541.0` 冲突）：

| 文件 | 位置 | 残留内容 |
| --- | --- | --- |
| `docs/requirements.md` | §8 标题与首条 | 「截至 2026-09-15，本机安装版本为 **1.3.2477**」「`127.0.0.1:9920` 的 JSON-RPC 服务可访问」 |
| `docs/anytxt-capabilities.md` | §1 核查环境 | `ATGUI.exe 1.3.2477.0`、「本地 JSON-RPC 服务监听 `127.0.0.1:9920`」 |
| `docs/anytxt-capabilities.md` | §7 第 1 条 | 「默认使用全局索引查询，不必按多个 OneDrive 目录拆分」——与 §5 自己修正后的结论直接矛盾 |
| `docs/anytxt-capabilities.md` | §4.4 表格 | 并发矩阵全部是 1.3.2477 的数据，未标注"仅 1.3.2477" |

判定：**报告关于"矛盾已修复"的说法只对了一半**——影响运行时的默认值确实统一了，但基线类文档仍自相矛盾。
这一项恰好是 §4 方案里必须一并处理的（弃用 legacy 后 §8/§1 会整段重写）。

### 2.4 独立复现结果（一次成型）

| 项目 | 结果 |
| --- | --- |
| 39 个契约测试 | Python **3.14.3**：39 OK / 3.49s；Python **3.13.12**：39 OK / 3.69s |
| 补丁 `apply --check --whitespace=error-all` | 通过；应用后改动面正是 5 个文件 |
| 4 个 Python 文件 `py_compile` | 通过（适配器与 `tools.py`/`cli.py`/`search.py`） |
| `apply --reverse --check` | 通过 |
| 补丁交付的适配器 vs 仓库 `src/.../anytxt_retriever.py` | 内容一致（仅 CRLF/LF 差异，规范化后逐字节相同） |
| `baseline.json` 哈希 | `requirements/core.txt`、`requirements/tests.txt` 均 **MATCH** |
| 真实 v1 健康检查（用仓库适配器打 `127.0.0.1:9924/rpc`） | `{'healthy': True, 'rpc_method': 'anytxt.v1.getResult', 'structured': True}` |
| 真实一次 retrieve（`partimento`、`path=["C:\\"]`、页 10、片段预算 3） | 0.99s、77 个候选、8 次 search + 3 次 fragment、`reason=fragment_request_budget`、`complete=False`（受测试预算所限，符合预期） |

### 2.5 未关闭门槛（复核一致）

三条均属实，可继续沿用：

1. 运行时版本/目录/分页/字面量/正则能力探测状态机未实现——`_validate_semantics` 仍直接拒绝
   case-sensitive/whole-word/invert/count 与 regex 模式。
2. 30 个固定查询的 recall@K 与性能基准未执行；README 最后一项仍未勾选，仓库内无基准产物。
3. 未在真实服务上跑压力探针；未在 1.3.3541 上重跑完整 FAST/DEEP 验收。

## 3. 本轮新发现（不属原报告）

| # | 级别 | 发现 | 证据 | 影响 |
| --- | --- | --- | --- | --- |
| N1 | P2 | `SearchPage.count` 被解析后**从未被任何代码读取** | `grep -n "page\.count"` 无命中；只有第 395 行构造 | v1 的精确 `count`（`anytxt.v1.search`）本可用于完整性判定，现在白丢 |
| N2 | P2 | v1 业务码 `result.errno` 未检查，只检查顶层 `error` | 源码第 328 行仅 `if result.get("error")` | 非 0 `errno` 会被当成"成功的空结果"。本次探测全部 `errno=0`，非 0 行为**尚未取证** |
| N3 | P2 | `ANYTXT_SEARCH_LIMIT` 无上限校验，而 v1 规定 `limit` ∈ [1,300] | 配置只用 `positive()`；官方文档「Must be between 1 and 300」 | 配 >300 会得到 `-32602`，且当前被归入通用 backend error（触发回退而非配置报错） |
| N4 | P3 | 片段文本原样携带 AnyTXT 高亮标记 `*<<*` / `*>>*` | 实测 `'the-art-of-*<<*partimento*>>*-history...'` | 标记进入 `lines.text` → 污染 Sirchmunk 证据链与知识簇；若原文本身含该串还会误判 |
| N5 | P3 | `valid_received` 是死代码 | 576 行赋值、587 行自增、无读取；自 `2cf524a` 起如此 | 可读性；也说明"页被完全过滤"的判断已改由 offset 语义承担 |
| N6 | P3 | 12:39 `ATGUI.exe` 静默退出一次 | PID 34680 → 42368（Restart on Crash 拉起），旧日志 `ATGUI_20260915-032858.log` 无任何错误记录；其后 6 次请求全部正常 | **因果未证**：与我第一发 legacy 请求时间重合，但随后同等 legacy 请求 0.05s 正常返回，无法归因。说明需要 `status` 预检与更明确的诊断 |
| N7 | P3 | 分页 `order` 恒为 0（默认序），分页顺序不稳定 | 源码固定 `"order": 0`；v1 支持 1..4 | 长结果集分页有 `repeated_page` 风险；`order=3`（路径升序）可让分页确定化 |
| N8 | P3 | `_EndpointGate` 语义需写进文档：`tighten()` 只收紧不放松、按 URL 分键、弱引用释放后才重置 | 源码 `_endpoint_gate` / `tighten` | 同进程出现两种并发配置时"最严者胜"；同一进程的两个端点各有独立闸门（v1-only 后不再相关） |

## 4. AnyTXT v1 专项适配：评估与方案

### 4.1 目标与边界

**目标**：适配器只支持 AnyTXT **1.3.3541+** 的 v1 接口（`anytxt.v1.*`，`http://127.0.0.1:9924/rpc`），
删除 `legacy`（`ATRpcServer.Searcher.V1.*`，`9920`）的全部代码路径、配置项、文档与探针选项；
并把 v1 独有能力用于**消除现有妥协**，而不是只换 URL。

**边界（本方案不做）**：不改 Sirchmunk 下游读取/证据/知识存储流程；不引入第三方依赖；
不把 `getText` 作为证据来源（证据仍以 Sirchmunk 读原文件为准）；不自动调用 `syncIndex`；
不接入 MCP 端点（`9924/mcp`）；不为 v1 引入批量请求。

### 4.2 v1 实测契约（1.3.3541，2026-09-15，只读探测）

| 项目 | 实测结果 | 对适配器的意义 |
| --- | --- | --- |
| 信封 | `{"result": {"errno": 0, "data": {"input": {...}, "output": {...}}}}`，参数**直接放 `params`** | 去掉 `nested` 分支；`errno` 需处理（N2） |
| `anytxt.v1.status` | `output.return = true`，0.094s | 真正的健康检查，取代空查询探活 |
| `anytxt.v1.search` | `count = 77`（C: `partimento`）= **精确总数** | 可直接做完整性判定（N1） |
| `anytxt.v1.getResult` | `output.count` = **本页条数**（limit 2 → 2，limit 10 → 10）、`field = ["fid","lastModify","size","file"]`、`files` 为行数组、`fid` 是**十进制字符串** | 现有 `_normalise_files` 已兼容；`count` 不可当总数用 |
| `anytxt.v1.getFragment` | `output.text`；命中词被 `*<<*`/`*>>*` 包裹；非法 `fid` 返回 `text: null` 且 `errno = 0` | 需处理标记（N4）与 null 语义 |
| 表达式语法 | `partimento & fugue !mozart`（D:\）→ `count = 32` 正常 | 可服务端表达 AND/OR/NOT/短语 |
| 空 `filterDir` | 回显 `"filterDir": "C:"`——**1.3.3541 与 1.3.2477 行为一致** | `ANYTXT_GLOBAL_ROOTS` 逐卷查询仍是唯一正确表达（设计不受升级影响） |
| 参数校验 | `pattern=123` → `-32602 "'pattern' must be a string"`；缺 `fid` → `-32602 "Missing 'fid' parameter"` | 需把协议错误与传输错误分开分类 |
| 端点并存 | `9920` 与 `9924` 同一 ATGUI 进程监听；`9920` 单发请求 0.05s 正常返回 | legacy 目前仍"能用"，弃用是**主动收敛**而非"已经坏了" |
| legacy 信封 | 同为 `result.data.output`，参数在 `params.input`，方法名 `ATRpcServer.Searcher.V1.*` | 删除面清晰：仅方法名 + 参数嵌套差异 |

### 4.3 工作项

**A 组——契约固化（不改变外部行为，先做）**

| # | 改动 | 验收 |
| --- | --- | --- |
| A1 | 响应解码检查 `result.errno`，非 0 映射为明确的 `AnyTXTBackendError`（或新增 `AnyTXTBusinessError`） | 新增单测：构造 `errno != 0` 即报错，绝不返回空结果；并用取证实验确认真实非 0 场景（见 4.5） |
| A2 | 协议错误分类：`-32601`（方法不存在，暗示版本过旧/接口被关）、`-32602`（参数）与传输错误分开，日志给出可操作提示 | 单测覆盖两种错误码的文案与是否触发回退 |
| A3 | `ANYTXT_SEARCH_LIMIT` 在配置层校验 1..300，越界直接报配置错误 | 单测：301 / 0 均 `ValueError` |
| A4 | 片段文本规范化：默认剥离 `*<<*`/`*>>*`，原始文本另存 `_anytxt.raw_lines` | 单测：含标记的 fixture 断言 `lines.text` 无标记；含字面 `*<<*` 的正常文本不被误删（需先定义规则） |
| A5 | `text: null` 或非字符串不再当成 backend failure，改为"该文件无可返回片段"并继续（仍是 `complete=False` 的候选级原因） | 单测：null 片段计入新 reason，且不影响其它候选 |
| A6 | 删除死代码 `valid_received`；决定 `SearchPage.count` 的去向（A/B 组二选一） | `grep` 无残留；测试数量不回退 |

**B 组——移除 legacy（本轮新目标的硬要求）**

| # | 改动 | 验收 |
| --- | --- | --- |
| B1 | 删除 `API_MODES`、`api_mode`/`ANYTXT_API_MODE`、`_nested_params`、`SEARCH_METHOD`/`FRAGMENT_METHOD` 兼容常量，URL 默认值固定 `http://127.0.0.1:9924/rpc` | `grep -rn "legacy\|9920\|ATRpcServer" src/ tests/ scripts/` 在交付物中零命中（文档中的历史实测除外，需显式标注"历史"） |
| B2 | 删除相关测试（`test_legacy_keeps_the_input_envelope`、`test_invalid_api_mode_is_rejected`）并新增"仅 v1"护栏测试 | 测试总数变化可解释；新增护栏：默认 URL/方法名/无 `input` 嵌套 |
| B3 | 明确失败提示：`9924` 不可达时给出"需要 AnyTXT 1.3.3541+ 且启用本地 API"的可操作信息，而不是笼统的 `AnyTXT request failed` | 单测：连接拒绝 → 文案含版本与端点 |
| B4 | `scripts/anytxt_stability_probe.py` 移除 legacy 端点表与 `--api` 选项 | 探针仅 v1 可跑；历史 1.3.2477 对比数据保留在 capabilities 文档并标注版本 |
| B5 | 文档重写：`requirements.md` FR-1/FR-2/FR-8 + **§8 基线整段改为 1.3.3541 + v1**；`capabilities.md` §1/§4.4/§4.5/§7/§8；`architecture.md` §4.2；README 配置块、前置条件、已实现范围 | 解决 §2.3 的全部残留；文档内不再出现"legacy 可配置" |
| B6 | 补丁重新生成（5 文件不变，内容更新）并重跑三重校验 | 与既有流程一致：`apply --check` → py_compile → `--reverse --check` |

**C 组——用 v1 能力替换现有妥协（收益最大的部分）**

| # | 改动 | 收益 | 验收 |
| --- | --- | --- | --- |
| C1 | 用 `anytxt.v1.search` 拿精确总数：`complete` 判定改为"已收集候选数 vs 精确总数（按卷求和）"，`SearchPage.count` 改为页计数语义并显式命名 | 完整性判定从"分页猜"变成"可计算"，`repeated_page` 降级为异常信号 | 单测：总数 77 / 收集 30 → `complete=False` 且 reason 明确；总数全收到 → `complete=True` |
| C2 | 多关键词改用服务端表达式 `a & b !c`（含短语引号），保留客户端集合运算作为回退路径或直接删除 | 请求数从"每词每卷分页"降到"一次表达式每卷"；解除"集合不完整就拒绝精确 AND/NOT"的限制（当前会直接回退 rga） | 单测：3 个词的 AND/NOT 只发 1 次/卷；`logic` 语义与表达式映射的等价性测试；`_validate_semantics` 改为按 v1 语法校验元字符（`& \| ! " ( )`） |
| C3 | `health_check` 改用 `anytxt.v1.status`；`return=false` 表示索引尚未加载完成，给出独立 reason（区别于服务不可达） | 12:39 那类"服务在但没响应"的场景可诊断 | 单测：`return=false` → 不健康且原因可读 |
| C4 | 片段改用 `anytxt.v1.getFragmentAll`（`limit` 默认 10），一个候选一次请求产出多条 match 事件 | 直接削减 DEEP 的请求量（当前 421 候选 → 数百次 fragment），缓解已观测到的偶发停顿 | 单测：一次请求产出多片段事件；片段预算与字符预算仍独立生效 |
| C5 | 分页改用 `order=3`（路径升序）以确定化翻页（N7） | 消除分页重复/丢页风险，也是基准测试可复现的前提 | 单测：断言请求带 `order=3`；真实短查询验证顺序稳定 |

**D 组——明确不做（记录理由，防止后续反复）**

| 项 | 理由 |
| --- | --- |
| `anytxt.v1.getText` 作为证据来源 | 会改变证据语义（索引抽取文本 vs 原文件读取），影响知识簇与引用可靠性；保留为未来可选项 |
| `syncIndex` / `ocr` 自动调用 | 属于 AnyTXT 自身职责与运维动作，适配器不应隐式触发写操作 |
| MCP 端点（`9924/mcp`） | 与本项目形态无关；16 会话/300s 过期等约束不带来收益 |
| 在 v1 上放宽跨类互斥（`_EndpointGate` 的 kind 条件） | 1.3.3541 只验证过"并发 2 + 混合类型 600 请求通过"，放宽属**未验证配置**；除非新探针矩阵证明，否则保持现状 |
| 批量 JSON-RPC 请求 | `getFragmentAll` 已覆盖主要收益；批量的错误语义（部分失败）会增加复杂度 |

### 4.4 实施顺序与阶段验收

| 阶段 | 内容 | 验收门槛 |
| --- | --- | --- |
| **P0** | A 组 + B1/B2/B3/B4（契约固化 + 移除 legacy 代码路径） | 39→新测试数全绿；`grep` 无 legacy 残留；真实 v1 端点一次 retrieve 正常；补丁三重校验通过 |
| **P1** | B5/B6（文档重写 + 补丁重生成） | §2.3 三处残留清零；文档基线=1.3.3541/v1；补丁可在锁定基线应用/编译/反向回滚 |
| **P2** | C1/C3/C5（计数、健康检查、确定性分页） | 完整性判定有明确单测；`status=false` 与不可达可区分；分页顺序确定 |
| **P3** | C2/C4（服务端表达式、多片段） | AND/NOT 不再必然回退 rga；DEEP 请求数下降（记录前后对比）；真实 DEEP 全程 PID 不变 |
| **P4** | 未关闭门槛：能力探测状态机（在新语法下大幅简化）、30 查询基准 | 基准有产物与结论；recall@K 与 P50/P95 达标或记录未达标 |

### 4.5 风险与待取证实验

| 风险 | 处理 |
| --- | --- |
| v1 稳定性只验证到 600 请求 × 并发 2 | 任何提高并发/页大小/片段批量的改动都必须先用 `anytxt_stability_probe.py` 复测并记录 PID 是否变化 |
| 非 0 `errno` 的表现**未知** | 待取证实验：`fid` 为负数/不存在、`filterDir` 指向未索引卷、`filterExt` 非法、`limit=0/301`。拿到真实响应后再定 A1 的分类 |
| 空 `filterDir` 语义未变（= `C:`） | 保持 `ANYTXT_GLOBAL_ROOTS` 逐卷查询；不得因为"升级了"而回退全局语义 |
| 删除 legacy 会提高最低版本要求 | README 前置条件已写 1.3.3541+；B3 的错误文案必须点明版本，避免"接口没开"被当成"服务挂了" |
| 服务可能静默退出（N6） | 加 `status` 预检；日志同时匹配三种崩溃文案（`WinError 10061` / `AnyTXT request failed` / `AnyTXT request timed out`）并核对 PID |
| 表达式语法引入新元字符 | `_validate_semantics` 需重新定义"字面量安全集"，并为含 `&`、`"`、`!` 的关键词保留转义或拒绝策略 |

## 5. 本轮未改动的东西（状态说明）

- `C:\Users\12915\Projects\sirchmunk` 仍是**旧补丁**状态：`config/env.example`、`agentic/tools.py`、
  `cli/cli.py`、`anytxt_retriever.py` 与最新补丁不一致（`search.py` 一致）。下次 E2E 前需
  `rollback.ps1` 后重新 `apply.ps1`，否则跑的是 `_KindGate` 旧版适配器。
- 用户 checkout 中 `web/package-lock.json` 的既有改动未被触碰。
- 本轮未修改任何实现代码、未重新生成补丁、未跑 E2E、未跑基准。临时 worktree 已删除。

## 6. 下一轮入口

1. 先做 P0（A 组 + 移除 legacy），因为这决定了后续所有文档与测试的形态。
2. A1/C1/C3 需要的取证实验在 §4.5，建议与 P0 一并做掉，避免二次返工。
3. 之前那份交接（`daefa4c` 提交内的旧 `HANDOFF.md`）内容仍然是该轮修复的有效记录，可用
   `git show daefa4c:HANDOFF.md` 取回；其"未关闭门槛"三条已并入本文 §2.5。
