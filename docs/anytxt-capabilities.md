# AnyTXT 能力核查记录

本文记录 AnySirchmunk 设计阶段对 AnyTXT 官方说明和本机安装的只读核查结果。它用于确定适配器的兼容基线，不代表所有 AnyTXT 版本都具有完全相同的行为。

## 1. 核查环境

核查日期：2026-09-14。

本机可执行文件版本信息：

| 组件 | 版本 |
| --- | --- |
| `ATGUI.exe` | 1.3.2477.0 |
| `ATService.exe` | 1.3.2477.0 |
| 主要文件内容辅助程序 | 1.3.2477.0 |

本地 JSON-RPC 服务监听 `127.0.0.1:9920`。核查只读取安装信息、配置数据库和搜索响应，没有修改 AnyTXT 设置或索引。

## 2. 当前实际索引格式

安装目录的 `config.db` 中，`IndexStat` 记录了 26 种已加入索引规则的格式，总文件数为 11,287。

| 格式 | 已索引文件数 |
| --- | ---: |
| `.pdf` | 6,610 |
| `.txt` | 2,186 |
| `.docx` | 2,095 |
| `.doc` | 129 |
| `.pptx` | 97 |
| `.xlsx` | 59 |
| `.epub` | 42 |
| `.mobi` | 25 |
| `.mmap` | 11 |
| `.ppt` | 9 |
| `.chm` | 8 |
| `.djvu` | 4 |
| `.xls` | 4 |
| `.azw3` | 2 |
| `.et` | 2 |
| `.one` | 2 |
| `.wps` | 1 |
| `.dps` | 1 |
| `.azw`、`.xmind`、`.ofd`、`.docm`、`.pptm`、`.xlsm`、`.xlsb`、`.pps` | 0 |

`ExtSupportStat` 中还存在大量由扫描过程发现的扩展名，但它们不能等同于“已经建立全文索引且可可靠解析”。集成层应以 `IndexStat` 和真实查询为判断依据。

本机当前规则没有图片或压缩包，因此不能假定 OCR 图片、ZIP、7Z、RAR 或 ISO 已经可搜索。用户可以在 AnyTXT 索引管理中单独添加支持的格式；OCR 还取决于是否安装和启用 OCR 组件。

## 3. 官方格式说明

[AnyTXT 官方主页](https://anytxt.net/)列出的主要支持类型包括：

- 普通文本和代码文件；
- DOC、DOCX、XLS、XLSX、PPT、PPTX 等 Microsoft Office 文件；
- OneNote、WPS；
- PDF，包括由 OCR 处理的扫描 PDF；
- MOBI、EPUB、AZW、DJVU、LaTeX 等电子书；
- CHM、OFD、XMind 等思维导图、WizNote、Edraw；
- OCR 图片；
- EXE、DLL、SO 等二进制文件。

[官方版本记录](https://anytxt.net/download/)说明：

- 1.3.1260 增加 ZIP、7Z、RAR、ISO 的索引和搜索，需要在索引规则中手动添加；
- 1.3.1517 增加文件内容正则搜索和全文搜索 API Beta；
- 1.3.2463 增加按文件名过滤搜索结果；
- 1.3.3173 增加 HWP/HWPX、HTTP 鉴权和文件全文获取 API。

当前安装版本 1.3.2477 晚于 1.3.2463、早于 1.3.3173。项目不能依赖 1.3.3173 才明确公布的文件全文 API；Sirchmunk 将根据返回路径自行读取原文件。

## 4. 已验证的 RPC

### 4.1 搜索结果

方法：

```text
ATRpcServer.Searcher.V1.GetResult
```

本机响应的 `output` 包含：

```text
count, field, files
```

`field` 的值为：

```text
fid, lastModify, size, file
```

`limit` 会限制实际返回数量。官方论坛也报告单次结果最多为 300。[官方论坛 API 示例](https://anytxt.net/forums/topic/index-reindex-and-search-with-api/)

2026-09-15 对本机 1.3.2477 做了两页只读对照：`limit=2` 时，`offset=0` 与
`offset=2` 返回不同的 `fid`，确认 offset 可以推进分页。响应中的 `count` 在两页
都为 2，表现为当前页数量而非总命中数，因此实现不能以 `offset >= count` 作为
结束条件，而以空页/短页结束并检测重复页。该检查不证明索引变化时具有快照一致性；
拆分关键词仍不能绕过完整性预算。

### 4.2 命中片段

方法：

```text
ATRpcServer.Searcher.V1.GetFragment
```

传入 `fid` 和原查询 `pattern` 后，本机可从 `output.text` 取得命中片段。片段不提供可靠的原文件行号，适配器不应伪造行号。

安装程序中还能看到 `GetFragmentAll` 方法名，但缺少稳定的公开参数和返回契约。第一阶段不依赖它。

### 4.3 传输契约

2026-09-15 复核确认，本地服务的请求必须同时满足两个条件：

- **JSON-RPC 2.0 信封**：`{"jsonrpc": "2.0", "id": <n>, "method": "ATRpcServer.Searcher.V1.*", "params": {"input": {...}}}`。
  裸 `{"method": ..., "input": ...}` 形式不会得到响应，请求会一直挂起到调用方超时。
- **同时带 `Accept: application/json` 和 `Content-Type: application/json`**。
  缺少其中任意一个时服务端返回 `HTTP 400`（响应体为空）；两个都带才返回 `HTTP 200`。

以上两条已固化在适配器 `AnyTXTClient` 和仓库测试 `RpcContractTests` 中：改动信封结构或删减任一头部都会使测试失败，而不是等到运行期才发现请求被静默拒绝。

### 4.4 服务端稳定性

2026-09-15 逐级加压实测（同一进程连续发送 `GetResult`）：

| 场景 | 请求数 | 并发 | 结果 |
| --- | ---: | ---: | --- |
| 英文 pattern | 9 / 36 | 1 | 全部成功 |
| 中文及中英混合 pattern | 9 / 36 | 1 | 全部成功 |
| 英文 pattern | 9 / 54 | 2 / 4 | 全部成功 |
| 中英混合 pattern | 36 | 2 | 全部成功 |
| **中英混合 pattern** | **18** | **4** | **5 成功、13 失败，`ATGUI.exe` 段错误退出** |

崩溃会终止整个 RPC 服务：进程消失、9920 不再监听、日志中没有任何错误记录，此后每个请求都返回 `WinError 10061 连接被拒`，整轮检索全部失败。因此适配器默认 `ANYTXT_MAX_CONCURRENCY=2`；调高会让一次查询在服务崩溃后彻底失败，不属于已验证配置。

降低并发与片段请求量能显著延长稳定运行时间：`ANYTXT_MAX_CONCURRENCY=1` 加
`ANYTXT_MAX_FRAGMENT_REQUESTS=20` 时，一次 7.5 分钟的 DEEP 查询（6 轮 ReAct、数百个请求）
只遇到一次短暂不可用；配合进程级自动重启（崩溃即拉起 ATGUI.exe）即可跑完长查询。
DEEP 一类长查询建议显式使用这组低负载配置，并给 `ATGUI.exe` 配置自动重启。
崩溃窗口内的检索会以 WARNING 明确记录，不会静默降级成零结果。

## 5. 全局搜索参数差异

在本机 1.3.2477 中，对同一个已知可命中的查询进行只读对照：

| `filterDir` | `filterExt` | 结果 |
| --- | --- | --- |
| `""`（或省略） | `""` | 服务端回显 `filterDir="C:"`，返回结果全部位于 C 盘 |
| `""` | `"*.pdf"` | 同上，仅 C 盘 PDF |
| `"C:\\"` | `""` | 正常返回 C 盘结果 |
| `"D:\\"` | `""` | 正常返回 D 盘结果 |
| `"E:\\"` | `""` | 正常返回 E 盘结果 |
| `"C:\\*"` | `""` | 回显 `"C:*"`，等价于 C 盘 |
| 具体目录 | `"*.pdf"` | 正常返回目录内 PDF 结果 |
| `"C:,D:,E:"` | `""` | 0 条（不支持多值列表） |
| `"*"` | `"*"` 或 `"*.pdf"` | 0 条 |

2026-09-15 的复核（`pattern=the`、`limit=300`）证明空 `filterDir` **不是**全局搜索：
服务端把空值解析为它自己的当前目录（本次为 `C:`），返回的 300 条结果全部来自 C 盘。
本机索引实际覆盖 C、D、E 三个卷（索引数据目录中存在 `C.atc`、`D.atc`、`E.atc`，
以及早期残留的 `F.ati`），因此只查询空 `filterDir` 会静默漏掉 D、E 两个卷。
此前“空 `filterDir` 表示全局”的记录只验证了“有结果”，没有验证结果覆盖哪些卷。

官方论坛示例使用 `filterDir="*"`，示例用户注明版本为 1.3.1517；本机版本为 1.3.2477。官方发布说明没有记录这一参数语义变化，因此不能断定是升级或未升级造成的。

实现决策：

- 空 `filterDir` 不再被当作全局搜索。它只代表“服务端默认目录”，结果必须标记为
  不完整：`complete=false`、`reason=unverified_global_scope`、
  `effective_scope=anytxt_server_default`，并输出告警提示配置；
- 覆盖多个卷的全局检索由 `ANYTXT_GLOBAL_ROOTS` 显式声明（例如
  `["C:\\", "D:\\", "E:\\"]`），适配器对每个根分别请求，再按规范化后的绝对路径去重合并；
- 显式指定范围时传具体目录；
- 不硬编码论坛中的 `"*"`；
- RPC 没有可列出已索引卷的方法（`GetIndexList`、`GetDriveList`、`GetVolumeList`、
  `GetIndexStat`、`GetVersion`、`GetStatus` 均返回 `404`），卷列表只能由配置提供，
  适配器不应猜测或扫描磁盘；
- 对其他版本保持能力未知，只有已知内容且确认入索引的测试文件与正反例能够验证最终策略；不凭普通零结果切换参数。

## 6. 正则搜索

本机语言资源明确包含：

- `Regular Expression`；
- `Regular Match`；
- `Search based on regular expression`；
- 正则较慢，建议指定路径和文件类型的提示。

本机 RPC 对普通词和 `B.rtel`、`rhetor.*` 等正则形式均能返回结果。这与官方从 1.3.1517 起支持内容正则搜索的发布说明一致。

仍有一个接口边界：公开的 `GetResult` 示例只有 `pattern`、`filterDir`、`filterExt`、时间、分页和排序参数，没有独立的 `searchType`。现阶段无法仅凭公开资料证明 RPC 的正则模式是否与 GUI 当前搜索模式完全独立。

实现决策：

- 普通关键词也需确认字面量语义；当前正则命中记录不能证明 `a.b`、`C++` 等词的转义行为；
- 正则查询使用已入索引的已知语料进行正反例验证，无测试语料时保持未知；
- 不支持的正则语法或不可确认的模式按明确目录范围回退到 `rga`，无回退范围时报告不支持，不改成普通关键词；
- 不通过修改 AnyTXT 配置数据库来切换模式。

## 7. 对 AnySirchmunk 的影响

核查结果简化了第一阶段方案：

1. 默认使用全局索引查询，不必按多个 OneDrive 目录拆分；实际请求数取决于关键词、分页和片段预算。
2. 用户仍可显式限定目录和扩展名。
3. AnyTXT 已覆盖主要文献格式，尤其是 PDF、DOCX 和 TXT。
4. AnyTXT 负责候选文件和片段；Sirchmunk 继续读取原文件，避免依赖新版全文 API。
5. 适配器区分健康检查与语义验证，因为该 API 仍带有 Beta 标记且公开契约不完整。当前记录是设计依据，尚不构成完整的兼容性测试报告。

## 8. 后续复核

升级 AnyTXT 后应重新运行以下兼容检查：

- 全局 `filterDir` 的有效表示，以及空值被解析到哪个目录；
- JSON-RPC 信封结构与必需的请求头是否变化；
- `GetResult` 字段和单次结果上限；
- `GetFragment` 返回结构；
- 正则模式是否需要独立参数；
- HTTP 鉴权是否启用；
- 新的文件全文 API 方法名、输入和输出；
- 原索引规则与文件数量是否保留。
