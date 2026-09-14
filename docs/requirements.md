# AnySirchmunk 需求说明

## 1. 背景

Sirchmunk 可以围绕本地资料进行查询规划、证据分析、答案生成和知识积累，但在大型 Windows 文献目录中使用 `ripgrep-all` 临时解析文件时，检索速度、覆盖率和候选文件质量不够稳定。

AnyTXT Searcher 已经为本地文件建立全文索引，能够快速返回相关文件和文本片段，但它本身不提供 Sirchmunk 式的 AI 分析、证据组织、知识聚类和历史知识复用。

本项目需要组合两者，而不是重新实现完整的搜索引擎或知识系统。

## 2. 产品目标

用户在 Sirchmunk 中提出问题后，Sirchmunk 继续负责理解问题和生成检索词；关键词搜索改由 AnyTXT 完成。AnyTXT 返回的文件路径和片段进入 Sirchmunk 原有证据链，最终答案和知识记录仍由 Sirchmunk 生成与保存。

预期流程如下：

1. 用户选择一个或多个本地文献目录并提出问题。
2. Sirchmunk 生成关键词或在 ReAct 循环中提出新的关键词。
3. AnyTXT 在现有索引中查找候选文件并返回命中片段。
4. 集成层校验路径，应用目录、深度、扩展名和排除规则。
5. Sirchmunk 从候选文件读取所需内容，生成带来源的答案。
6. Sirchmunk 将值得复用的结果写入现有 `KnowledgeStorage`。
7. 后续问题可以复用已经保存的知识簇。

## 3. 功能需求

### FR-1：AnyTXT 连接

- 通过可配置 URL 连接 AnyTXT 本地 JSON-RPC 服务。
- 默认地址为 `http://127.0.0.1:9920`。
- 为每次请求设置超时。
- 对连接失败、超时、非法响应和接口错误给出可诊断日志。
- 不把本地 API 暴露到公网，也不要求 AnyTXT 账号或云服务。

### FR-2：内容检索

- 支持单个关键词和多个关键词。
- 支持一个或多个搜索根目录。
- 调用 `ATRpcServer.Searcher.V1.GetResult` 获取候选文件。
- 调用 `ATRpcServer.Searcher.V1.GetFragment` 获取匹配片段。
- 保留 AnyTXT 返回的稳定文件标识、绝对路径、修改时间和文件大小等可用元数据。
- 去除重复文件，并限制请求数、文件数和片段总字符数。

### FR-3：Sirchmunk 兼容

- 输出 Sirchmunk 当前检索链可以消费的 `begin`、`match`、`end` 事件。
- 每个 `match` 至少包含 `data.path.text` 和 `data.lines.text`。
- 将检索后端标记为 `anytxt`，供 telemetry 和问题排查使用。
- 现有 `KeywordSearchTool`、FAST 候选排序和 DEEP ReAct 工具无需理解 AnyTXT 的 RPC 格式。

### FR-4：范围与过滤

- 搜索结果必须位于用户指定的搜索根目录中。
- 路径比较应适配 Windows 大小写不敏感和路径分隔符。
- 支持 Sirchmunk 的 `include`、`exclude` 和 `max_depth` 参数；AnyTXT 无法原生表达的规则由适配层进行结果过滤。
- 不读取搜索范围外的文件。

### FR-5：回退策略

- 提供 `rga`、`anytxt` 和后续可选的 `auto` 后端模式。
- 第一版中，`anytxt` 请求失败时可以按配置回退到 `rga`。
- 空搜索结果和后端故障必须区分；正常的零结果不应被误报为故障。
- 日志和检索 metadata 应记录实际后端及回退原因。

### FR-6：文件名搜索

- 第一阶段的 `FILENAME_ONLY` 模式继续使用 Sirchmunk 原有文件枚举方式。
- 在确认当前 AnyTXT 版本存在稳定的文件名检索接口后，再评估替换该路径。
- 内容检索接入不应降低文件名搜索能力。

### FR-7：知识持久化

- 继续使用 Sirchmunk 的 `KnowledgeStorage`、`KnowledgeCluster` 和 `EvidenceUnit`。
- 不迁移或改写现有 Parquet schema。
- 知识记录中的来源路径必须保持为原始文件的绝对路径。
- 更换检索后端后，知识复用、合并、演化和证据追踪仍应工作。

### FR-8：配置

计划增加以下环境变量：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `SIRCHMUNK_SEARCH_BACKEND` | `rga` | 选择检索后端 |
| `ANYTXT_API_URL` | `http://127.0.0.1:9920` | AnyTXT 本地服务地址 |
| `ANYTXT_SEARCH_LIMIT` | `300` | 单次 RPC 最大候选数 |
| `ANYTXT_REQUEST_TIMEOUT` | 与现有检索超时协调 | RPC 请求超时 |
| `ANYTXT_FALLBACK_TO_RGA` | `true` | AnyTXT 故障时是否回退 |

配置值不得硬编码到检索逻辑中，也不得保存密钥或用户私有路径到仓库。

## 4. 非功能需求

### 兼容性

- 保持 Sirchmunk 的公开搜索调用、API 请求和 MCP payload 兼容。
- 未配置 AnyTXT 时保持原有默认行为。
- 兼容当前已验证的 AnyTXT 1.3.2477 接口，不依赖更高版本才提供的全文读取接口。

### 性能

- 不对整个文献目录重新遍历或建立第二套索引。
- 多关键词请求应有并发上限，避免阻塞 AnyTXT 服务。
- 同一文件的片段请求应去重。
- 在进入 LLM 之前限制候选文件和片段文本规模。

### 可靠性

- AnyTXT 崩溃或尚未启动时，Sirchmunk 进程不应崩溃。
- RPC 返回缺失字段、无效 JSON 或不存在的文件时，应跳过问题记录并保留其他有效结果。
- 取消 Sirchmunk 查询时，应尽快取消尚未开始的 AnyTXT 请求。

### 可观测性

- 记录请求耗时、候选数、有效文件数、片段请求数、实际后端和回退状态。
- 日志不得包含 LLM 密钥或整份私人文档内容。

## 5. 数据与多设备使用

文献路径在多台设备上都为同一绝对路径，例如 `D:\OneDrive\...`，可以提高知识记录跨设备复用的成功率。Sirchmunk 工作目录也可以放入 `D:\OneDrive\SirchmunkData`。

第一阶段的约束：

- OneDrive 只负责文件同步，不提供数据库并发控制。
- 两台设备不得同时修改同一个 `knowledge_clusters.parquet`。
- 启动 Sirchmunk 前应等待同步完成；结束搜索后也应等待知识文件上传完成。
- AnyTXT 的索引由每台设备分别建立，不直接通过 OneDrive 共用。
- 若以后需要并发写入，应把知识存储升级为带事务和同步协议的服务端存储。

## 6. 验收标准

1. 使用一个能在 AnyTXT 中命中的关键词执行 FAST 搜索，日志显示 `anytxt`，答案包含真实文件来源。
2. DEEP 模式在初始检索和 ReAct 后续检索中都能发现 AnyTXT 返回的文件。
3. 查询完成后，知识簇仍写入原有 Parquet 文件，并可在下一次查询中复用。
4. 停止 AnyTXT 服务后再次查询，系统按配置回退到 `rga`，且日志说明原因。
5. 将搜索路径限定到一个子目录时，不返回其他目录中的同名或同内容文件。
6. 中文、英文、带空格及包含常见正则符号的关键词不会造成 RPC 或匹配错误。
7. 未启用 AnyTXT 时，现有 Sirchmunk 行为和配置保持不变。

## 7. 暂不包含

- 替代 AnyTXT 的索引创建、更新或文件监控功能。
- 修改 Sirchmunk 的知识数据模型或迁移现有知识文件。
- 多设备同时写入同一知识库。
- 通过公网访问 AnyTXT。
- 第一阶段重新设计 Sirchmunk Web 界面。
- 把 embedding 改为云端服务；这属于独立的后续功能。
