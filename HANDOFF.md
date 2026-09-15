# AnySirchmunk 交接：v1-only 适配审查修复后的剩余工作

日期：2026-09-15（第四轮代码审查修复）

分支：`main`　上游基线：Sirchmunk `3c7ee54f93fa198db2020a3ab850356f2dacff72`

## 0. 一句话状态

**AnyTXT v1-only 适配已完成并验收**：接口只剩 `anytxt.v1.*`（`127.0.0.1:9924/rpc`，1.3.3541+），
legacy `9920` 的全部代码路径、配置项、探针选项、文档与交付模板都已移除；契约测试 39 → 62；
FAST 与 DEEP 在完整 Sirchmunk 环境通过，全程零连接失败、零超时，服务 PID 未变。
第四轮又修复了分页完整性、预算回退、候选上限、非法 `fid` 和安装验证五项问题；剩余工作见 §3。

历史文档（本文件的前两版）：

- 「核验 `daefa4c` 审查报告 + v1 专项适配方案」：`git show ae3c28d:HANDOFF.md`
- 「第一轮修复交接」：`git show daefa4c:HANDOFF.md`

## 1. 上一轮审查报告的核验结论（已完成，留档）

| # | 审查发现 | 判定 |
| --- | --- | --- |
| 1 | 安装模板把 v1 方法与 legacy `9920` 配对，并把并发 2 覆盖为 4 | 属实，已修 |
| 2 | `_KindGate` 属实例级，跨客户端可绕过互斥 | 属实，已改为进程级 `_EndpointGate` |
| 3 | 传输超时被包装后不重试；超时线程过早放锁 | 属实，已修 |
| 4 | 片段字符预算事后检查 | 属实，已修 |
| 5 | 缺 `fid` / 文件失效记录仍进证据链 | 属实，已修 |
| 6 | `fallback_used` 两处计算不一致 | 属实但**无测试覆盖**，语义已按上游源码核对 |
| 7 | 文档 legacy/v1 默认矛盾 | 当时**部分属实**，本轮已彻底清零 |

同轮的独立核实手段与完整证据（反空转、证伪、补丁三重校验、baseline 哈希、真实端点探测）
见 `git show ae3c28d:HANDOFF.md`。

## 2. 本轮（v1-only 适配）完成情况

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 | 契约固化（`errno`、协议错误分类、`limit` 1..300 校验、片段标记、null 片段、删死代码）+ 移除 legacy | ✅ `083226d` |
| P1 | 文档与基线同步（requirements §8 / capabilities §1、§4、§5、§7、§8 / architecture §4.2 / README / baseline.json） | ✅ `6c76da6` |
| P2 | 用 v1 能力替换妥协：`search` 精确总数判定完整性、`status` 就绪检查、`order=3` 确定性分页 | ✅ `083226d` |
| P3 | 服务端表达式取代客户端集合运算；`getFragmentAll` 削减片段请求 | ⬜ 未做 |
| P4 | 30 查询召回与性能基准；运行时能力探测状态机 | ⬜ 未做 |
| — | 验收记录 | ✅ `2720999` |

### 2.1 第四轮代码审查修复

- 精确计数现在按每个 scope 的唯一 `fid` 对账；跨页部分重叠会标记
  `overlapping_page`，唯一记录数与总数不一致会标记 `incomplete_enumeration`，不再以原始行数误报完整。
- 请求或候选预算使 AND/NOT 不完整时不再启动 `rga` 回退；返回
  `exact_logic_incomplete`，并只保留可证明安全的子集（未搜索完全部 AND 项或任何不完整 NOT 均不返回候选）。
- 候选预算按规范化路径后的唯一文件计数，只在尝试加入第 N+1 个唯一候选时触发；恰好达到上限不再误报截断。
- 非字符串/空 `fid` 在记录验证阶段标为 `invalid_record`，页签名也始终可哈希，不再让整个查询抛 `TypeError`。
- `scripts/verify.ps1 -SirchmunkPath` 现在核对锁定 HEAD，并要求当前补丁可反向预检；旧补丁、部分补丁或漂移安装不能仅靠编译通过。
- 以上各有回归测试；当前契约测试共 62 个，补丁对锁定基线的 apply check 通过。

关键取证（都改变了实现，详见 `docs/anytxt-capabilities.md` §4.1–4.7）：

- `result.errno` 是业务状态：未索引卷与无法解析的 `fid` 都返回 `errno = 1` + 空载荷，
  而参数错误走 JSON-RPC `-32602`。旧代码只读 `output`，会把"目录不可检索"当成"该目录无命中"，
  甚至仍把结果标记为完整。
- `anytxt.v1.search` 的精确总数受 `filterExt` 约束（同目录同关键词：`*`=77 / `*.pdf`=64 / `pdf;docx`=65）。
- 验收：FAST 55s、DEEP 190s（6 轮 ReAct、Phase 2 共 1399 个候选）、零失败、PID 42368 全程不变。

## 3. 剩余工作

### 3.1 P3-①：用服务端表达式取代客户端集合运算

现状：多关键词的 AND/OR/NOT 在客户端对"已收集的文件集合"求交并差，因此非预算原因造成集合不完整时
`logic in {and, not}` 会抛 `AnyTXTIncompleteResults` 并按范围回退 rga；预算受限时禁止回退并返回明确的不完整状态。

价值：v1 原生支持 `a & b !c` 与 `"短语"`（实测 D 盘 `partimento & fugue !mozart` → 32）。
改成表达式后一次请求即可表达多词逻辑，请求数从"每词每卷"降到"每表达式每卷"，
并解除"集合不完整就必须回退"的限制。

需要一并处理：

- `_validate_semantics` 的元字符集合要按 **v1 表达式语法**重定义（`&`、`|`、`!`、`"`、`(`、`)`），
  而不是现在的"是否像正则"；含这些字符的关键词要有明确的转义或拒绝策略——**这是本项最大的未知**。
- `logic` 到表达式字符串的映射，以及 OR 语义与现状（多关键词分别收集再合并）的差异。
- 决定是否保留客户端集合运算作为回退路径，或彻底删掉 `_combine`。
- 新增等价性测试：在固定语料上对照表达式结果与旧客户端运算结果。

风险：改成表达式后进入 rga 回退的路径会变少，一旦表达式语义判断错，错误表现为**静默漏结果**
而不是报错。建议先做只读对照（同一语料同时跑两种方式），再动代码。

### 3.2 P3-②：`anytxt.v1.getFragmentAll` 削减片段请求

实测一次请求返回 8 条片段（0.749s），逐条 `getFragment` 则要 8 次请求；DEEP 的片段请求是主要负载来源。
需要处理：`limit` 取值、多条片段如何映射为多个 `match` 事件、与片段字符/请求预算的交互。

### 3.3 P4-①：固定语料 30 查询召回与性能基准（README 唯一未勾选项）

这是**唯一还没关闭的发布门槛**。门槛定义见 `docs/architecture.md` 第 6 节（recall@K 不低于 rga、
热缓存检索 P95 不高于 rga 的 80%）。需要：固定的查询集与语料、AnyTXT 与 rga 同条件对照、
记录 recall@K / P50 / P95 / 请求数 / 片段字符数 / 回退率。`order=3` 已让分页可复现，这是做基准的前提。

### 3.4 P4-②：运行时能力探测状态机（FR-5A）

`_validate_semantics` 目前对正则、大小写、whole-word、count 一律保守拒绝。v1 的表达式语法已有官方文档，
状态机可以据此简化（至少"表达式语法"不必再探测）；仍需判定 `literal` 转义与是否存在独立搜索模式字段。

### 3.5 N6：AnyTXT 服务的一次静默退出仍未定位

2026-09-15 12:39，`ATGUI.exe` 在约 7 小时空闲后处理第一个请求时退出（PID 34680 → 42368），
日志无任何错误记录。该时刻与我发出的第一个 legacy 请求重合，但随后同等请求（legacy 与 v1 各若干）
都正常，**因果未证**。当前缓解：`status` 就绪检查、崩溃窗口同时匹配三种文案、核对 PID。
若要继续追：在 `ATGUI` 长时间空闲后发首个请求并观察 PID，或排查是否有定时索引维护任务。

### 3.6 明确不做（保留，避免反复讨论）

`getText` 作为证据来源（会改变证据语义）；自动 `syncIndex` / `ocr`；MCP 端点（`9924/mcp`）；
在 v1 上放宽跨类型互斥（属未验证配置）；JSON-RPC 批量请求（`getFragmentAll` 已覆盖主要收益）。

## 4. 验收与复现入口

```bash
# 契约测试（应为 62 个）
python -m unittest discover -s tests
# 补丁三重校验
git worktree add --detach <原生路径> 3c7ee54f93fa198db2020a3ab850356f2dacff72
git -C <wt> apply --check --whitespace=error-all patches/sirchmunk-3c7ee54-anytxt.patch
git -C <wt> apply --whitespace=error-all patches/sirchmunk-3c7ee54-anytxt.patch
python -m py_compile <wt>/src/sirchmunk/{agentic/tools.py,cli/cli.py,search.py,retrieve/anytxt_retriever.py}
git -C <wt> apply --reverse --check --whitespace=error-all patches/sirchmunk-3c7ee54-anytxt.patch
# 稳定性探针（v1 only）
python scripts/anytxt_stability_probe.py --requests 600 --concurrency 2 --fragments-per-search 3 --limit 300
```

端到端（需要 Sirchmunk 环境与 LLM 配置）：

```bash
export SIRCHMUNK_SEARCH_BACKEND=anytxt ANYTXT_API_URL=http://127.0.0.1:9924/rpc
export ANYTXT_GLOBAL_ROOTS='["C:\\","D:\\","E:\\"]' ANYTXT_MAX_CONCURRENCY=2
export SIRCHMUNK_WORK_PATH='D:\OneDrive\SirchmunkData' SIRCHMUNK_SEARCH_PATHS=''
sirchmunk search "partimento" --mode FAST --work-path 'D:\OneDrive\SirchmunkData' -v
```

## 5. 环境状态

- `C:\Users\12915\Projects\sirchmunk`：HEAD == 锁定 commit，已从可精确识别的历史补丁
  `daefa4c` 无冲突回滚，并部署 AnySirchmunk `35c72ea` 的当前补丁；62 个契约测试、
  四个集成文件编译和当前补丁反向预检均通过。部署前目标除旧补丁本身外没有其他改动或未跟踪文件。
- `D:\OneDrive\SirchmunkData`：工作目录；知识 parquet 已随 DEEP 验收更新（26 353 字节，13:15）。
- AnyTXT 1.3.3541，`ATGUI.exe` 当前 PID 42368，同时监听 `9920` 与 `9924`（只有 9924 被使用）。
- 临时探测脚本留在 `.workbuddy/tmp/`（gitignored），可作为契约证据重放。
