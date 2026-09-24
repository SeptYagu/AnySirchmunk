# AnySirchmunk 开发环境

本文件是开发者和 AI Agent 的 Python 环境入口。

## Python 基准
- Windows 开发基准：Python 3.12.13。
- 版本来源：根目录 `.python-version`。
- 本仓库的合同测试和补丁工具使用独立外置环境。
- 被打补丁的 Sirchmunk checkout 仍使用 Sirchmunk 自己的运行环境，不与本仓库混用依赖。

## 外置环境
默认：
```text
D:\AIenvs\AnySirchmunk
```
可用 `AI_ENVS_ROOT` 覆盖根目录。

## 初始化与验证
```powershell
.\scripts\setup_dev.ps1
.\scripts\verify.ps1
```

本仓库当前测试仅使用 Python 标准库，因此环境不额外安装第三方包。

## AI Agent 规则
- 不创建或复用仓库内 `.venv`。
- 不修改系统 Python，不与其他仓库共享第三方依赖环境。
- Sirchmunk 目标 checkout 的依赖按其上游说明管理；不要装进 AnySirchmunk 环境。
- 两台电脑分别运行初始化脚本，只同步环境描述、补丁和源码。
