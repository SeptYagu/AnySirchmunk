# AnySirchmunk 修复交接

日期：2026-09-15

分支：`main`

上游基线：Sirchmunk `3c7ee54f93fa198db2020a3ab850356f2dacff72`

## 本阶段目标

关闭 2026-09-15 项目审查发现的配置、并发、超时、预算和候选完整性问题，重新生成可应用于锁定 Sirchmunk 基线的补丁，并保留尚未完成的发布门槛。

## 审查发现与处理

| 优先级 | 发现 | 本阶段处理 |
| --- | --- | --- |
| P1 | 安装模板把默认 v1 方法与 legacy `9920` 端点错误配对，并把安全并发值从 2 覆盖为 4 | 已修复。仓库文档、`config/env.example` 补丁和 CLI `.env` 模板统一为 `ANYTXT_API_MODE=v1`、`http://127.0.0.1:9924/rpc`、并发 2，并补齐全局根目录及片段请求预算配置。legacy 示例必须同时设置模式和 URL。 |
| P1 | `_KindGate` 属于单个客户端实例，并发 SSE 请求可用不同客户端绕过 search/fragment 互斥 | 已修复。改为按规范化 API URL 共享的进程级 `_EndpointGate`，跨客户端限制同类并发并互斥不同 RPC 方法。 |
| P1 | `urlopen` 的真实传输超时被包装后不会重试；`asyncio.to_thread` 超时后底层线程仍可能运行并过早释放互斥 | 已修复。新增 `AnyTXTRequestTimeout` 分类，传输超时和外层等待超时均只重试一次。闸门由实际 HTTP 工作线程持有到传输结束；排队请求收到取消标志后不再发送。 |
| P2 | 片段字符预算在 RPC 之后检查，达到上限仍继续请求；最后一个片段被截断时可能仍标记完整 | 已修复。请求前检查剩余字符，发生截断立即标记 `fragment_budget` 并停止后续片段请求；metadata 增加 search/fragment/总请求数。 |
| P2 | 缺少 `fid` 或文件已不存在的索引记录仍进入证据链并可能标记完整 | 已修复。范围和过滤通过后验证 `fid` 与实际文件；无效记录被跳过并标记 `invalid_record`，AND/NOT 因不完整集合触发既有精确语义保护。 |
| P2 | `KeywordSearchTool` 日志和结构化返回对 `fallback_used` 的计算不一致 | 已修复。两处均以实际 `_fallback_reason` 判断。 |
| P2 | 需求、架构和 README 同时保留了 legacy 默认与 v1 默认的矛盾描述 | 已修复当前默认值、方法信封和超时说明；稳定性探针默认接口改为 v1。 |

## 新增回归覆盖

- 环境默认值必须形成 `v1 + 9924/rpc` 配对。
- 交付补丁中的两份 `.env` 模板必须包含安全 v1 默认值和片段请求预算。
- 两个独立 `AnyTXTClient` 不能使 search 与 fragment 重叠。
- 协程超时后仍在运行的 HTTP 工作线程必须继续阻止另一类 RPC。
- `urlopen` 直接抛出传输超时时必须重试一次。
- 片段字符截断必须停止后续请求并标记不完整。
- 缺少 `fid` 或文件失效的记录必须跳过并标记不完整。

## 验证

- `pwsh -NoProfile -File .\scripts\verify.ps1`
  - 39 个 Python 契约测试全部通过。
  - 适配器 `py_compile` 通过。
- 本机 `127.0.0.1:9924/rpc` 轻量健康检查通过，返回
  `rpc_method=anytxt.v1.getResult` 与结构化响应；该检查不等于语义或压力验证。
- 在临时的干净 Sirchmunk `3c7ee54` checkout 上：
  - `git apply --check --whitespace=error-all` 通过。
  - 应用后四个改动 Python 文件的 `py_compile` 通过。
  - `git apply --reverse --check --whitespace=error-all` 通过。
  - 实际反向回滚后 checkout 恢复干净。
- `requirements/core.txt` 与 `requirements/tests.txt` 的 SHA-256 仍与 `baseline.json` 一致。

## 尚未关闭的发布门槛

1. 运行时版本和目录/分页/字面量/正则能力状态机仍未实现；当前实现继续以保守拒绝或有界 rga 回退保证语义，不应宣称满足 FR-5A 的完整能力探测要求。
2. 至少 30 个固定查询的 recall@K、冷/热 P50/P95、回退率和请求规模基准仍未执行；README 中该项继续保持未完成。
3. 本阶段未对本机真实 AnyTXT 服务运行压力探针或完整 FAST/DEEP 端到端流程；发布前应在 1.3.3541+ 上复测，尤其关注跨请求并发、取消和超时后的进程稳定性。

## 验收标准

- 交付模板不会生成 v1/legacy 混配，且不再把并发提升到未验证值。
- 单客户端、跨客户端及超时残留线程场景均不存在跨方法 HTTP 重叠。
- 所有截断、预算耗尽和无效记录都通过 metadata 明确暴露，不产生伪完整结果。
- 补丁可在锁定基线上应用、编译并完整回滚。
- 完成上述三个未关闭门槛前，不将项目标记为最终发布完成。
