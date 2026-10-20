# CodeWeave

**A local code map for humans and agents.**

CodeWeave 的目标是从大型代码仓库自动生成可交互、可查询、可更新的结构地图。
人类浏览模块、类、函数及调用关系；Agent 查询同一结构索引，精准定位必要源码。
地图不是聊天记忆库，也不依赖人工维护 Markdown 来建立代码关系。

## 当前状态

M2-A 本地可用版：Clang LibTooling → 封存输入 → SQLite 不可变索引 → JSON CLI / HTTP → 交互地图。可在编辑器保存修改后手动全量刷新、查询真实进度、比较两次成功索引或 Git HEAD 与工作区，并查看函数、调用关系和源码 Diff。保留 M0/M1 查询能力；尚未实现增量 TU 复用或自动监听。0.0.0 表示未发布。

## 开始使用 M2-A

Linux，Python **3.11+**、Node **22.12+**/npm、Git、CMake、C++ 编译器、匹配的 **LLVM/Clang 18 开发包**和 Bubblewrap（user namespaces 可用）。不自动安装系统软件，不执行被分析项目的构建脚本。

```bash
# 在 CodeWeave 源码根目录；首次准备 Python 环境
python3 -m venv .venv
.venv/bin/python -m pip install -e './backend[dev]'
# LLVM 前缀内应有 lib/cmake/clang/ClangConfig.cmake
export CODEWEAVE_LLVM_PREFIX=/path/to/llvm-18
./scripts/run_local.sh --demo --state-dir /tmp/my-codeweave-demo
# 浏览器打开 http://127.0.0.1:8000；Ctrl-C 停止服务
```

启动器只构建 CodeWeave 自身的分析器/前端，首次为自有演示工程真实建库。它打印实际 Python/LLVM/Node、状态目录和地址；缺依赖明确退出。`CODEWEAVE_PYTHON` 可指定已安装依赖的解释器；`--llvm-prefix` / `--resource-dir` 可覆盖探测，`--port` 可换端口。不需要工作流仓库、LLM 或远程服务。

演示工程复制到 **状态目录的 `demo-project/`**，后续启动不会覆盖你的修改。搜索 `choose` / `literal`，点击结果、调用边和源码；编辑该目录 `main.cpp` 中 `return 7;` → `return 9;`、`choose()` 中 `alpha()` → `beta()` 并保存。在网页点“刷新工作区”，等真实 request 状态 `published` 后点“比较上次成功与本次结果”；筛选“调用变化”，点击 `target_changed` 看两侧只读源码 Diff，或“在左/右版地图查看”。原历史地图不会被刷新自动替换。只在文件开头加空行时应仅改变位置证据；改 `config.hpp` 的宏会产生输入上下文变化；临时写入编译错误会显示失败且保留旧索引。

重启复用同一配置和所有已保存索引：

```bash
./scripts/run_local.sh --state-dir /tmp/my-codeweave-demo
```

自己的受支持项目必须事先有真实、匹配的编译数据库（CodeWeave 不替你运行项目 CMake/build）：

```bash
./scripts/run_local.sh --state-dir /tmp/my-project-map \
  --project-root /absolute/project \
  --compilation-database /absolute/build/compile_commands.json
# 需要时重复 --dependency-root /absolute/authorized/headers
# HEAD 基线配置不在 Git tree 时：--head-compilation-database /absolute/baseline-ccdb.json
```

状态目录必须在分析输入根之外。已配置状态拒绝偷偷更换项目/工具链，改配置用新状态目录。默认普通 `serve` 继续只读；`open` / 启动器显式启用受约束的工作区刷新。页面不能传入项目路径、数据库位置或编译命令。

“建立 HEAD 基线”读取真实 Git 对象和 tree 内编译数据库或显式基线配置。历史生成输入缺失会阻断 HEAD 侧并显示原因，正常 Workspace 刷新仍可用；不会拿旧索引代替 HEAD。普通 `.git` 目录受支持，linked worktree/alternates/submodule/LFS 历史输入不自动展开。工作区使用实际保存的磁盘字节（包括 staged、unstaged、untracked 和 ignored 文件），不是暂存区或未保存编辑器缓冲。

### M2-A 合同与限制

- 全量重新分析，所有 TU 读取同一套封存副本，在原虚拟路径保持实体身份；精确保存 AST 读取的项目缓冲区。封存是明确字节向量，不是全文件系统原子时刻。封存后磁盘变化不会改旧/新候选 AST，并记录 `workspace_changed_since_capture`。
- schema **4** 与 protocol **3** 增加快照、配置、attempt、定义范围和唯一 token 上下文证据。新索引、成功指针和 attempt 在单 SQLite 事务发布。失败、取消、进程中断不破坏旧图；重启把已停止的任务标为 interrupted，不自动重跑。
- 调用关系按 caller/target/dispatch 与实例数量比较，与行号和 instance ID 无关；唯一、同上下文调用才标记 target_changed。宏或重复调用保守显示 ambiguous/unmatched，关系数量仍准确；不猜调用对应，不证明运行时必定执行。
- 定义字节未变时，输入上下文比较实际 TU 正读集的头/依赖哈希，返回变化路径、前后 SHA-256 和编译 profile 身份；ODRHash 仅作为额外提示。变化详情最多预览8项/8192字节，给出完整数量和集合哈希，原始输入及 TU 关联保留在 SQLite，状态明细可分页查询。它是 TU 级保守提示，不是每个函数的精确依赖或运行时因果。为保留纯移行/其他函数体编辑的独立性，排除定义及 TU 主源码缓冲；同定义文件的外部常量变化可能只出现在文件/源码 Diff，不据“无函数事件”断言行为等价。
- 源码窗口严格按 UTF-8 显示，返回整文件/窗口 SHA-256 和原始字节范围（`begin` 包含、`end` 不包含）。非 UTF-8 窗口明确报 `source_not_utf8`，原始字节仍保存在索引；已存字节与哈希不符报 `source_integrity_error`，不显示替代字符。双侧源码 Diff 保留相同身份字段。
- 范围缩小时报告 out_of_scope；声明保留但唯一实现 TU 被移出时报告 definition_removed，带范围原因。源文件未被 ccdb 选择会计入 uncovered；coverage 只保证 selected units，不宣称整个仓库完整。
- 项目/依赖 symlink、FIFO/device 明确拒绝。标准头/资源及实际 ELF 运行库全部复制和校验；不映射整个 live `/usr`、不执行仓库脚本/hooks/filters，不支持响应文件或插件。授权依赖正文不经 source API 公开。M0 的 legacy `index` 路径仍是 observed-input 身份；使用工作区路径才有封存输入合同。
- 每输入根最多 **50000** 条目 / **1 GiB**；分析180秒墙钟、120秒CPU、4GiB地址空间、256MiB单输出上限；不保证大型项目性能。Diff beta 每侧50000符号/100000调用实例/32MiB调用锚点；超限明确失败。当前标准头按安装的系统 C++ 搜索顺序封存，不是完整可移植操作系统镜像；特殊目标需匹配显式工具链。
- 不做自动监听、TU 增量复用、任意历史构建复现、复杂 PCH/modules、完整隐式生命周期或全程序虚分派。`__DATE__` 等按真实分析时刻求值，provenance 记录 epoch，当前所有 TU 均无缓存复用，不承诺确定性构建。

旧 schema3 库只读兼容；显式迁移写入全新路径，旧库字节不变。迁移后的旧索引标为 `legacy_observed_inputs`，可查历史源码但不能伪装为可比较的 protocol3 快照。

```bash
codeweave migrate --from old.sqlite3 --to new.sqlite3
codeweave workspace status --state-dir /tmp/my-project-map
codeweave workspace refresh --state-dir /tmp/my-project-map
codeweave workspace head --state-dir /tmp/my-project-map
codeweave query diff --database /tmp/my-project-map/index.sqlite3 --left UUID1 --right UUID2
codeweave query diff-calls --database /tmp/my-project-map/index.sqlite3 --left UUID1 --right UUID2 --limit 20 --offset 0
```

整体验收（安装开发依赖与 Chromium 后，包含全新分析器、真实沙箱/CLI、全部后端、M1 浏览器回归和 M2 实际编辑闭环；必需项不允许 Skip）：

```bash
cd frontend && npm ci && npx playwright install chromium && cd ..
PYTHON="$PWD/.venv/bin/python" bash scripts/verify_m2.sh
```

API、错误及明细分块协议见 [HTTP 契约](docs/http-api.md)。以下保留 legacy M0/M1 入口说明。

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

必须通过 `--project-root` 明确授权实际项目根，支持 `src/` 与 `include/` 平级。编译数据库的 `directory` 只作为参数解析上下文，不授予挂载权限。可重复传入 `--dependency-root` 或 `--toolchain-root` 授权额外的具体目录；非系统安装的 Clang 可通过 `--resource-dir /path/to/llvm/lib/clang/18` 明确授权内置头文件目录（版本应匹配分析器）；默认系统工具链读取范围为 `/usr`、`/lib`、`/lib64`，不会因输入路径自动扩大。授权根中的符号链接仍受物理路径及沙箱边界限制。应用会在选项分组前检查完整原始 argv，统一拒绝任意位置以 `@` 开头的响应文件令牌（含 `-D @file`、引号/转义后的 `command` 令牌及输出选项值）；`-DMAIL="a@b"` 等内部 `@` 不作响应文件。随后白名单化解析参数，拒绝 response files、插件/`-Xclang` 转发、诊断/依赖/模块缓存输出参数，并用 Bubblewrap 在PID/网络等 namespace 隔离、清空继承环境、源码只读和临时输出目录的环境中运行分析器；缺少 Bubblewrap 时拒绝索引。编译数据库不是构建脚本，不会被执行。

索引保存 Clang 语义身份、调用点、物理文件锚点、实际解析缓冲区快照、源码 SHA-256、编译命令与数据库身份。`query` 输出 JSON：成功只写 stdout；失败只在 stderr 写 `{ "error": { "code", "message" } }` 并返回非零。`source` 仅可读取同一索引中登记的快照，单次上限为 200 行和 32768 原始源码字节（JSON 转义后的响应字节可更大）；超限会明确拒绝。

可用 `search <名称>`、`show <USR>`、`callers <USR>`、`callees <USR>`、`source <绝对路径> --start-line N --end-line M` 和 `status`。无匹配返回空列表，空搜索按 limit 列举；`show`、调用关系和源码读取需要显式值。`--help` 提供完整参数。

数据库 schema 3 与旧库不兼容，必须在新路径建库；查询使用真正的只读连接，只验证 schema，不迁移、不建表，也不创建缺失数据库。失败为退出码 2、stderr 单个 JSON；`--help` 是人类帮助文本。`--index-id` 可选择历史索引，省略时选择最后一次成功发布；失败候选不改变默认索引。`status` 默认只返回 index/schema/coverage、项目根、编译数据库身份、unit_count/input_count、诊断计数及范围限制。不会默认输出全部编译命令或依赖列表。相同 TU 的互斥配置明确拒绝；同命令去重，不同 TU 使用不同命令合法。

明细必须显式请求，并可按固定索引分页：

```bash
codeweave query status --database index.sqlite --details units --limit 20 --offset 0
codeweave query status --database index.sqlite --details inputs --limit 20 --offset 0
```

`units` 按 source_file、`inputs` 按 path 稳定排序；`limit` 为 1–200，`offset` 为非负行偏移。返回 `items/returned_count/total_count/remaining_count/has_more/next_offset`。每次 status 响应最多 **32768 序列化 UTF-8 JSON 字节（含换行）**；预算可能先于 limit 截断页面，`budget_limited` 会明确提示，继续使用 next_offset。建议跨页固定 `--index-id`。

超过 2048 原始 UTF-8 字节的单字段返回 `{truncated,preview,utf8_bytes,sha256}`，不修改库内原始值。完整命令、诊断或路径可对单行分块读取：

```bash
codeweave query status --database index.sqlite --details units --limit 1 --offset 0 --field command --field-offset 0
codeweave query status --database index.sqlite --field toolchain --field-offset 0
```

每块至多 4096 原始字节，`content` 为 base64，带 `field_bytes/field_sha256/returned_bytes/has_more/next_field_offset`；逐块 base64 解码，再按字节偏移拼接并校验哈希，即可无损恢复原存储文本。units 可读 command/diagnostics/source_file，inputs 可读 path，toolchain 不带 details。这样超长配置与诊断仍可追溯，少量长字段也不能绕过响应预算。偏移越界或不适用的参数组合沿用 JSON error/exit 2。

源码查询只返回索引保存的项目缓冲区，授权外部依赖只保留身份，不开放全文。源文件在分析后修改不会让旧索引返回新文本；跨 TU 观察到同一路径内容不同则拒绝。调用行列以展开物理位置为主，`anchors` 保留 spelling、expansion 链和内容哈希；`calls` 为去重后的实例数，`call_observations` 为 AST 观察数，实体/调用的 `units` 给出 TU 来源。静态调用关系不证明某次运行必然执行。详细语义与内部组件边界见 [analyzer](analyzer/README.md)。

专用验收入口每次创建全新构建目录，输出源码/二进制哈希并运行 A（合成契约）、B（真实 Clang）、C（真实隔离公共 CLI）测试；缺分析器或任何 Skip 均不能通过。需 Bubblewrap/user namespaces 可用，测试还需 C 编译器构建只控制时序的自有探针。构建目录保留以便核验。

`serve` 默认只读；提供 `--frontend frontend/dist` 才同时提供已构建的地图页面。

初始化尚未生成完整依赖锁文件，不能据此宣称跨机器构建可复现。

## 仓库内容

`analyzer/` 为 Clang 提取器边界；`backend/` 为应用与 CLI；`frontend/` 为网页入口；
`tests/` 保存可公开的自有测试；`scripts/` 保存与本仓库构建、测试有关的工具。
稳定使用说明、测试与实现必须能在本仓库独立使用。
研发计划、方案、任务执行记录和内部审核不存放在本仓库，也不是运行依赖。

## License

MIT。第三方代码和素材保留各自许可证；被分析项目不因使用 CodeWeave 而改变许可。


## M1：本地交互地图

依赖 Node **22.12+**、npm、Python 3.11+。查询现有 M0 schema 3 索引无需启动 Clang；建库仍须真实分析器和 Bubblewrap。前后端不依赖工作流仓库。

```bash
python -m pip install -e './backend[dev]'
cd frontend
npm ci
npm run build
cd ..
codeweave serve --database /path/to/index.sqlite3 --frontend frontend/dist
# 浏览器打开 http://127.0.0.1:8000
```

服务固定监听本机，可用 `--port` 更改端口；数据库/前端目录只接受受信启动参数。页面选择索引、搜索函数/方法，搜索结果保留 USR 区分重载与同名实体。选中后加载一层调用点；点击邻居后展开/收起、居中/缩放/重置。点击节点或调用点显示 Monaco 中的**保存快照**、物理路径、行列、index_id 和来源。虚调用为静态声明目标，未知目标保留为未知；图不是运行轨迹。

页面每层最多各 12 个 caller/callee 调用点，累计 100 节点、150 调用点、24 次邻接请求；到达预算应收起或重置。每个真实调用实例一条边，递归和环按身份去重。页面提示剩余调用点，可用分页 HTTP/CLI 继续查询。布局为确定性网格，复杂图的高级布局和大型工程性能不属于本阶段。源码编辑器按需加载，首次源码显示需下载较大的本地 Monaco 模块；不使用 CDN、远程源码或 LLM。

开发时分别运行 `codeweave serve --database ...` 与 `cd frontend && npm run dev`；Vite 同源代理 `/api` 到本机 8000。默认无跨域开放，Host 限本机名称；不适合直接暴露到网络。服务不检测当前磁盘修改：历史 index_id 永远读历史快照，刷新索引列表可选择新发布索引，选择后清空旧图和源码。

API 及预算见 [HTTP 契约](docs/http-api.md)。`query search/callers/callees/indexes` 支持稳定 `--offset` 分页，`query neighbors <USR>` 与 HTTP 使用同一局部图内核。超大事实响应明确拒绝，原始数据库不截断。

完整 M1 验收（全新构建分析器、全部后端、前端类型/构建/测试、真实 Chromium）：

```bash
export CODEWEAVE_LLVM_PREFIX=/path/to/llvm
# 可选 PYTHON=/path/to/venv/bin/python
cd frontend && npx playwright install chromium && cd ..
bash scripts/verify_m1.sh
```

验收需要真实命名空间权限；Bubblewrap 无法运行或分析器缺失时失败，不能以 Skip 替代。Playwright 自行创建两份自有多文件 fixture 索引、启动本机服务并验证页面；不分析外部项目。测试 trace/build/index 留在临时或忽略目录，不入库。
