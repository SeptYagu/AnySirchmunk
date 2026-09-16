# 检索基准

`jsbach-30.json` 是 30 个固定查询；`results/2026-09-16-jsbach-30-final.json` 是在 49 份公开学术 PDF 上得到的最终结果。结果文件隐藏了本机语料根目录，不包含文献正文或 API 密钥。

运行器不调用 LLM。每个查询对 AnyTXT 和 rga 各执行 1 次冷运行、5 次热运行，记录 recall@10、precision@10、P50/P95、请求数、片段字符数、完整率和回退率：

```powershell
python scripts/retrieval_benchmark.py `
  --manifest benchmarks/jsbach-30.json `
  --root <已被AnyTXT索引的语料目录> `
  --sirchmunk-path <锁定基线并已应用补丁的Sirchmunk目录> `
  --work-path <Sirchmunk工作目录> `
  --output benchmarks/results/result.json
```

2026-09-16 结果：AnyTXT 和 rga 的平均 recall@10 均为 1.0；AnyTXT 热 P95 为 1366.56 ms，rga 为 3944.27 ms，比例 34.6%；AnyTXT 的 180 次运行完整率 100%、回退率 0%。

ground truth 由 rga 对照结果构建，适合检验回归，但不是人工相关性金标准。AnyTXT 索引提取与 rga 临时提取存在连字符和文本规范化差异，因此 precision 与负例差异应结合逐查询记录解释。
