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

需要匹配的 LLVM/Clang 开发包；M0 验收会构建分析器并实际执行语义测试，不允许跳过。`CODEWEAVE_LLVM_PREFIX` 指向含 `lib/cmake` 的 LLVM 前缀：

```bash
export CODEWEAVE_LLVM_PREFIX=/path/to/llvm
bash scripts/verify_m0.sh
```

也可手动构建并运行：

```bash
cmake -S analyzer -B .build/analyzer \
  -DCMAKE_PREFIX_PATH="$CODEWEAVE_LLVM_PREFIX" \
  -DCMAKE_BUILD_RPATH="$CODEWEAVE_LLVM_PREFIX/lib"
cmake --build .build/analyzer -j2
cmake -S tests/fixtures/m0 -B /tmp/codeweave-m0-build \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
codeweave index \
  --project-root "$PWD/tests/fixtures/m0" \
  --resource-dir "$CODEWEAVE_LLVM_PREFIX/lib/clang/18" \
  --compilation-database /tmp/codeweave-m0-build \
  --database /tmp/codeweave-m0.sqlite3 \
  --analyzer "$PWD/.build/analyzer/codeweave-analyzer"
codeweave query status --database /tmp/codeweave-m0.sqlite3
```

必须通过 `--project-root` 明确授权实际项目根，支持 `src/` 与 `include/` 平级。编译数据库的 `directory` 只作为参数解析上下文，不授予挂载权限。可重复传入 `--dependency-root` 或 `--toolchain-root` 授权额外的具体目录；非系统安装的 Clang 可通过 `--resource-dir /path/to/llvm/lib/clang/18` 明确授权内置头文件目录（版本应匹配分析器）；默认系统工具链读取范围为 `/usr`、`/lib`、`/lib64`，不会因输入路径自动扩大。授权根中的符号链接仍受物理路径及沙箱边界限制。应用会白名单化解析参数、拒绝 response files、插件/`-Xclang` 转发、诊断/依赖/模块缓存输出参数，并用 Bubblewrap 在PID/网络等 namespace 隔离、清空继承环境、源码只读和临时输出目录的环境中运行分析器；缺少 Bubblewrap 时拒绝索引。编译数据库不是构建脚本，不会被执行。

索引保存 Clang 语义身份、调用点、物理文件锚点、实际解析缓冲区快照、源码 SHA-256、编译命令与数据库身份。`query` 输出 JSON：成功只写 stdout；失败只在 stderr 写 `{ "error": { "code", "message" } }` 并返回非零。`source` 仅可读取同一索引中登记的快照，单次上限为 200 行和 32768 原始源码字节（JSON 转义后的响应字节可更大）；超限会明确拒绝。

可用 `search <名称>`、`show <USR>`、`callers <USR>`、`callees <USR>`、`source <绝对路径> --start-line N --end-line M` 和 `status`。无匹配返回空列表，空搜索按 limit 列举；`show`、调用关系和源码读取需要显式值。`--help` 提供完整参数。

数据库 schema 3 与旧库不兼容，必须在新路径建库；查询使用真正的只读连接，只验证 schema，不迁移、不建表，也不创建缺失数据库。失败为退出码 2、stderr 单个 JSON；`--help` 是人类帮助文本。`--index-id` 可选择历史索引，省略时选择最后一次成功发布；失败候选不改变默认索引。`status` 列出 TU 命令/配置哈希、真实诊断、依赖哈希和范围限制。相同 TU 的互斥配置明确拒绝；同命令去重，不同 TU 使用不同命令合法。

源码查询只返回索引保存的项目缓冲区，授权外部依赖只保留身份，不开放全文。源文件在分析后修改不会让旧索引返回新文本；跨 TU 观察到同一路径内容不同则拒绝。调用行列以展开物理位置为主，`anchors` 保留 spelling、expansion 链和内容哈希；`calls` 为去重后的实例数，`call_observations` 为 AST 观察数，实体/调用的 `units` 给出 TU 来源。静态调用关系不证明某次运行必然执行。详细语义与内部组件边界见 [analyzer](analyzer/README.md)。

专用验收入口每次创建全新构建目录，输出源码/二进制哈希并运行 A（合成契约）、B（真实 Clang）、C（真实隔离公共 CLI）测试；缺分析器或任何 Skip 均不能通过。需 Bubblewrap/user namespaces 可用，测试还需 C 编译器构建只控制时序的自有探针。构建目录保留以便核验。

`serve` 仅启动 `127.0.0.1:8000` 的健康检查接口，不是可用地图。

初始化尚未生成完整依赖锁文件，不能据此宣称跨机器构建可复现。

## 仓库内容

`analyzer/` 为 Clang 提取器边界；`backend/` 为应用与 CLI；`frontend/` 为网页入口；
`tests/` 保存可公开的自有测试；`scripts/` 保存与本仓库构建、测试有关的工具。
稳定使用说明、测试与实现必须能在本仓库独立使用。
研发计划、方案、任务执行记录和内部审核不存放在本仓库，也不是运行依赖。

## License

MIT。第三方代码和素材保留各自许可证；被分析项目不因使用 CodeWeave 而改变许可。
