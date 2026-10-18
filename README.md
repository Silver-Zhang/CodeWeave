# CodeWeave

**A local code map for humans and agents.**

CodeWeave 的目标是从大型代码仓库自动生成可交互、可查询、可更新的结构地图。
人类浏览模块、类、函数及调用关系；Agent 查询同一结构索引，精准定位必要源码。
地图不是聊天记忆库，也不依赖人工维护 Markdown 来建立代码关系。

## 当前状态

这是仓库初始化后的 M0 版本，已实现 C++ LibTooling → SQLite → JSON CLI 的本地纵向闭环。当前覆盖函数/方法声明与定义、语义 USR 归并、直接调用点、虚调用标记、无法静态解析的函数指针调用以及索引快照读取。尚无交互图、工作区增量更新或版本分析；0.0.0 表示未发布；产品目标版本为 v0.1。

## 产品边界

Linux 本地、C++、Git 与匹配的编译数据库优先。结构由源码分析产生；保留具体调用点、
精确源码身份、编译配置与解析覆盖信息。已保存未提交的工作区修改属于首版范围。
网页和外部 Agent 共用查询内核。基础功能不依赖 LLM，不自动上传被分析源码。
GitLab、内嵌聊天、运行时序与状态生命周期不属于首版交付。

## 本地检查

需要 Python 3.11+、Git；M0 分析器另需匹配版本的 LLVM/Clang 开发包、CMake 和 C++ 编译器。以下命令不执行被分析仓库的构建脚本，CMake 只用于生成由 CodeWeave 自有 fixture 使用的编译数据库。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e './backend[dev]'
python -m pytest backend/tests -q
python -m unittest discover -s tests -p 'test_*.py' -v
python scripts/check_repository.py
```

## M0：本地 C++ 结构索引

先用匹配 LLVM/Clang 开发包构建 LibTooling 分析器，并将其路径传给 CLI：

```bash
cmake -S analyzer -B .build/analyzer \
  -DCMAKE_PREFIX_PATH=/path/to/llvm/lib/cmake \
  -DCMAKE_BUILD_RPATH=/path/to/llvm/lib
cmake --build .build/analyzer -j2
cmake -S tests/fixtures/m0 -B /tmp/codeweave-m0-build \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
codeweave index \
  --compilation-database /tmp/codeweave-m0-build \
  --database /tmp/codeweave-m0.sqlite3 \
  --analyzer "$PWD/.build/analyzer/codeweave-analyzer"
codeweave query status --database /tmp/codeweave-m0.sqlite3
codeweave query search overloaded --database /tmp/codeweave-m0.sqlite3
```

`index` 保存 Clang 语义身份、调用点、编译数据库身份、源文件快照与 SHA-256；`query` 输出 JSON，正常结果写 stdout，错误写 stderr 并返回非零退出码。可用 `show <USR>` 查看符号、`callers <USR>`/`callees <USR>` 查询关系、`source <绝对路径> --start-line N --end-line M` 读取索引快照。查询使用索引快照，不会把修改后的文件按旧行号当作原始结果。`--help` 提供完整参数。

`serve` 仅启动 `127.0.0.1:8000` 的健康检查接口，不是可用地图。

初始化尚未生成完整依赖锁文件，不能据此宣称跨机器构建可复现。

## 仓库内容

`analyzer/` 为 Clang 提取器边界；`backend/` 为应用与 CLI；`frontend/` 为网页入口；
`tests/` 保存可公开的自有测试；`scripts/` 保存与本仓库构建、测试有关的工具。
稳定使用说明、测试与实现必须能在本仓库独立使用。
研发计划、方案、任务执行记录和内部审核不存放在本仓库，也不是运行依赖。

## License

MIT。第三方代码和素材保留各自许可证；被分析项目不因使用 CodeWeave 而改变许可。
