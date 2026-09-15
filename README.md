# AnySirchmunk

AnySirchmunk 计划把 [AnyTXT Searcher](https://anytxt.net/) 的本地全文索引接入 [Sirchmunk](https://github.com/modelscope/sirchmunk) 的检索链路。

它保留两个软件各自擅长的部分：

- AnyTXT 负责在已经建立索引的本地文献中快速查找文件和命中片段。
- Sirchmunk 负责查询规划、证据读取、答案生成、知识聚类、历史知识复用和持久化。
- 当 AnyTXT 不可用且有明确搜索目录时，可以按配置回退到 Sirchmunk 原有的 `rga` 检索器。

项目已提供面向固定 Sirchmunk 基线的第一版补丁。详细需求见
[docs/requirements.md](docs/requirements.md)，技术方案见
[docs/architecture.md](docs/architecture.md)，锁定信息见 [baseline.json](baseline.json)。

## 目标工作流

```mermaid
flowchart LR
    Q[用户问题] --> P[Sirchmunk 查询规划]
    P --> A[AnyTXT 本地索引检索]
    A --> E[文件路径与命中片段]
    E --> R[Sirchmunk 读取原始文件并分析]
    R --> K[KnowledgeStorage]
    R --> O[带来源的答案]
    K --> P
```

## 第一阶段范围

第一阶段会实现一个 AnyTXT 检索适配器，并尽量保持 Sirchmunk 的下游流程不变：

1. 调用 AnyTXT 本机 JSON-RPC 服务检索关键词：未指定范围时按 `ANYTXT_GLOBAL_ROOTS` 逐卷查询并合并，用户明确限定范围时只查询这些目录。
2. 将文件路径和命中片段转换为 Sirchmunk 当前检索器使用的数据结构。
3. 让 Sirchmunk 的 FAST 和 DEEP 查询通过配置选择 AnyTXT。
4. 继续使用 Sirchmunk 的原始文件读取、证据追踪和知识保存能力。
5. AnyTXT 不可连接或请求失败时，在显式查询目录或配置的回退根目录内按配置回退到 `rga`；无目录时明确报告不可用。

实施顺序为：锁定 Sirchmunk commit 与检索契约，先完成单关键词 FAST 和原文引用闭环，再加入分页、过滤、有界回退，最后接通 DEEP/ReAct、知识复用并运行性能基准。事件格式一致本身不能证明所有下游路径兼容。

## 计划中的配置

```dotenv
SIRCHMUNK_SEARCH_BACKEND=anytxt
ANYTXT_API_URL=http://127.0.0.1:9920
ANYTXT_SEARCH_LIMIT=300
ANYTXT_GLOBAL_ROOTS=["C:\\", "D:\\", "E:\\"]
ANYTXT_FALLBACK_TO_RGA=true
ANYTXT_FALLBACK_ROOTS=[]
```

默认配置仍将保持 Sirchmunk 原有行为。只有显式选择 `anytxt` 后才使用 AnyTXT 索引。

`ANYTXT_SEARCH_LIMIT` 是页大小。`ANYTXT_GLOBAL_ROOTS` 定义“用户没有指定目录”时的检索范围：AnyTXT 的 RPC 没有枚举已索引卷的方法，而且把空 `filterDir` 解析成它自己的当前目录（实测只返回 C 盘），所以不配置它时只查询那个默认目录，结果会被标记为不完整（`unverified_global_scope`）并输出告警，而不会冒充全局结果。本机索引覆盖多个卷时，应按上面示例逐卷列出。

全局搜索失败后，仅在 `ANYTXT_FALLBACK_ROOTS` 配置了绝对目录 JSON 数组时才回退，并明确结果范围已缩小；不会自动扫描当前目录或整盘。文件名搜索也需要明确目录。完整预算配置见需求文档 FR-8。

结果达到预算上限时标记不完整，不对截断集合执行精确 AND/NOT 或计数。正则、字面量转义和大小写行为必须经过验证，未知能力按范围回退或报不支持。

## 数据位置

AnyTXT 的索引继续由 AnyTXT 自己管理。Sirchmunk 的知识库继续位于：

```text
{SIRCHMUNK_WORK_PATH}/.cache/knowledge/knowledge_clusters.parquet
```

例如把 `SIRCHMUNK_WORK_PATH` 设置为 `D:\OneDrive\SirchmunkData`，可以让知识文件进入 OneDrive。多个设备使用相同盘符和相同文献路径，有利于复用知识记录中的来源路径，但第一阶段不支持多台设备同时写同一份 Parquet 文件。使用前应等待 OneDrive 同步完成，并且同一时间只在一台设备运行写入任务。

## 前置条件

- Windows
- AnyTXT Searcher 已安装、正在运行并完成文献索引
- AnyTXT 本地 API 可通过 `127.0.0.1:9920` 访问
- Sirchmunk 的 Python 环境可以正常运行
- 已配置 Sirchmunk 使用的 LLM API

## 安装

补丁只适用于 Sirchmunk commit
`3c7ee54f93fa198db2020a3ab850356f2dacff72`。先准备一个干净 checkout：

```powershell
git clone https://github.com/modelscope/sirchmunk.git
git -C .\sirchmunk checkout 3c7ee54f93fa198db2020a3ab850356f2dacff72
.\scripts\apply.ps1 -SirchmunkPath .\sirchmunk
.\scripts\verify.ps1 -SirchmunkPath .\sirchmunk
```

随后按 Sirchmunk 自身说明安装依赖，在其 `.env` 中显式启用：

```dotenv
SIRCHMUNK_SEARCH_BACKEND=anytxt
ANYTXT_API_URL=http://127.0.0.1:9920
ANYTXT_GLOBAL_ROOTS=["C:\\", "D:\\", "E:\\"]
```

不设置 `SIRCHMUNK_SEARCH_BACKEND` 时仍使用上游 `rga`。没有配置 `ANYTXT_GLOBAL_ROOTS` 时，
不指定目录的查询只会命中 AnyTXT 服务端自己的默认目录，且结果会被标记为不完整。
全局检索发生故障时，只有配置了真实存在的绝对目录数组才允许缩小范围回退：

```dotenv
ANYTXT_FALLBACK_ROOTS=["D:\\Documents"]
```

回滚补丁：

```powershell
.\scripts\rollback.ps1 -SirchmunkPath .\sirchmunk
```

## 已实现范围

- [x] 验证 AnyTXT 本地搜索接口可以返回文件路径
- [x] 验证 AnyTXT 可以返回命中片段
- [x] 核对本机 1.3.2477 的索引格式、全局搜索和正则查询行为
- [x] 梳理 Sirchmunk 检索结果与知识存储链路
- [x] 锁定 Sirchmunk commit、依赖清单校验值与补丁交付步骤
- [x] 验证本机分页 offset、当前页 count 和全局/显式范围调用契约
- [x] 实现 AnyTXT JSON-RPC 客户端
- [x] 实现 Sirchmunk 检索器适配层
- [x] 接入 FAST、DEEP 初始关键词检索和 ReAct 关键词工具
- [x] 增加有界回退、完整性 metadata 和诊断日志
- [x] 完成模拟 RPC 契约测试、真实 RPC smoke test 及补丁应用/回滚验证
- [ ] 在完整 Sirchmunk 运行环境中完成 FAST/DEEP/知识复用端到端验收
- [ ] 完成固定语料上的召回与性能基准

当前对正则、大小写敏感、whole-word、精确 count，以及包含正则元字符的
literal 查询保持保守策略：显式范围内按配置回退到 `rga`；全局且没有
`ANYTXT_FALLBACK_ROOTS` 时明确报不支持，不静默改变语义。

本机能力核查结果和官方资料对照见 [docs/anytxt-capabilities.md](docs/anytxt-capabilities.md)。在已安装的 1.3.2477 中，全局 RPC 搜索使用空 `filterDir`；论坛示例中的 `"*"` 在本机返回零结果。第一版采用此兼容配置；未知版本或语义需已入索引的已知测试文件及正反例验证，不能仅凭零结果自动切换参数。

## 上游项目

AnySirchmunk 是一个独立集成项目。AnyTXT Searcher 和 Sirchmunk 的名称、代码及相关权利归各自所有者所有。后续实现会根据所采用的集成方式补充准确的许可证和分发说明。
