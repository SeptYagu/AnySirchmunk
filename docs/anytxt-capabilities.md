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

`limit` 会限制实际返回数量。官方论坛也报告单次结果最多为 300，因此适配器应支持分页或查询拆分，并记录达到上限的情况。[官方论坛 API 示例](https://anytxt.net/forums/topic/index-reindex-and-search-with-api/)

### 4.2 命中片段

方法：

```text
ATRpcServer.Searcher.V1.GetFragment
```

传入 `fid` 和原查询 `pattern` 后，本机可从 `output.text` 取得命中片段。片段不提供可靠的原文件行号，适配器不应伪造行号。

安装程序中还能看到 `GetFragmentAll` 方法名，但缺少稳定的公开参数和返回契约。第一阶段不依赖它。

## 5. 全局搜索参数差异

在本机 1.3.2477 中，对同一个已知可命中的查询进行只读对照：

| `filterDir` | `filterExt` | 结果 |
| --- | --- | --- |
| `""` | `""` | 正常返回全局结果 |
| `""` | `"*.pdf"` | 正常返回全局 PDF 结果 |
| `"D:\\"` | `"*.pdf"` | 正常返回 D 盘 PDF 结果 |
| 具体目录 | `"*.pdf"` | 正常返回目录内 PDF 结果 |
| `"*"` | `"*"` | 0 条 |
| `"*"` | `"*.pdf"` | 0 条 |

官方论坛示例使用 `filterDir="*"`，示例用户注明版本为 1.3.1517；本机版本为 1.3.2477。官方发布说明没有记录这一参数语义变化，因此不能断定是升级或未升级造成的。

实现决策：

- 当前兼容基线使用空 `filterDir` 表示全局搜索；
- 显式指定范围时传具体目录；
- 不硬编码论坛中的 `"*"`；
- 对其他版本执行能力探测并记录最终策略。

## 6. 正则搜索

本机语言资源明确包含：

- `Regular Expression`；
- `Regular Match`；
- `Search based on regular expression`；
- 正则较慢，建议指定路径和文件类型的提示。

本机 RPC 对普通词和 `B.rtel`、`rhetor.*` 等正则形式均能返回结果。这与官方从 1.3.1517 起支持内容正则搜索的发布说明一致。

仍有一个接口边界：公开的 `GetResult` 示例只有 `pattern`、`filterDir`、`filterExt`、时间、分页和排序参数，没有独立的 `searchType`。现阶段无法仅凭公开资料证明 RPC 的正则模式是否与 GUI 当前搜索模式完全独立。

实现决策：

- 普通关键词直接使用 AnyTXT；
- 正则查询在首次需要时进行能力验证；
- 不支持的正则语法或不可确认的模式回退到 `rga`；
- 不通过修改 AnyTXT 配置数据库来切换模式。

## 7. 对 AnySirchmunk 的影响

核查结果简化了第一阶段方案：

1. 默认只需一次全局索引查询，不必把多个 OneDrive 目录拆成大量请求。
2. 用户仍可显式限定目录和扩展名。
3. AnyTXT 已覆盖主要文献格式，尤其是 PDF、DOCX 和 TXT。
4. AnyTXT 负责候选文件和片段；Sirchmunk 继续读取原文件，避免依赖新版全文 API。
5. 适配器必须围绕能力探测设计，因为该 API 仍带有 Beta 标记且公开契约不完整。

## 8. 后续复核

升级 AnyTXT 后应重新运行以下兼容检查：

- 全局 `filterDir` 的有效表示；
- `GetResult` 字段和单次结果上限；
- `GetFragment` 返回结构；
- 正则模式是否需要独立参数；
- HTTP 鉴权是否启用；
- 新的文件全文 API 方法名、输入和输出；
- 原索引规则与文件数量是否保留。
