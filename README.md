# CodeWeave

**A local code map for humans and agents.**

CodeWeave 的目标是从大型代码仓库自动生成可交互、可查询、可更新的结构地图。
人类浏览模块、类、函数及调用关系；Agent 查询同一结构索引，精准定位必要源码。
地图不是聊天记忆库，也不依赖人工维护 Markdown 来建立代码关系。

## 当前状态

这是仓库初始化版本，不是已实现的 v0.1 产品。目前只有 Python API/CLI 启动骨架、
能力状态检查和自有两版本 C++ 测试素材。尚无 Clang 索引、结构查询、交互图、
工作区更新或版本分析。0.0.0 表示未发布；产品目标版本为 v0.1。

## 产品边界

Linux 本地、C++、Git 与匹配的编译数据库优先。结构由源码分析产生；保留具体调用点、
精确源码身份、编译配置与解析覆盖信息。已保存未提交的工作区修改属于首版范围。
网页和外部 Agent 共用查询内核。基础功能不依赖 LLM，不自动上传被分析源码。
GitLab、内嵌聊天、运行时序与状态生命周期不属于首版交付。

## 本地检查

需要 Python 3.11+、Git；编译测试素材另需 CMake 和 C++ 编译器。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e './backend[dev]'
python -m pytest backend/tests -q
python -m unittest discover -s tests -p 'test_*.py' -v
python scripts/check_repository.py
codeweave version
codeweave serve
```

`serve` 仅启动 `127.0.0.1:8000` 的健康检查接口，不是可用地图。
初始化尚未生成完整依赖锁文件，不能据此宣称跨机器构建可复现。

## 仓库内容

`analyzer/` 为 Clang 提取器边界；`backend/` 为应用与 CLI；`frontend/` 为网页入口；
`tests/` 保存可公开的自有测试；`scripts/` 保存与本仓库构建、测试有关的工具。
稳定使用说明、测试与实现必须能在本仓库独立使用。
研发计划、方案、任务执行记录和内部审核不存放在本仓库，也不是运行依赖。

## License

MIT。第三方代码和素材保留各自许可证；被分析项目不因使用 CodeWeave 而改变许可。
