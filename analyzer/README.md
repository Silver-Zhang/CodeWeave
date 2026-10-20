# analyzer

Clang LibTooling 提取器是内部受信组件。公开入口为 `codeweave index`，由 Python 校验输入、授权根和参数，并始终在 Bubblewrap 中执行分析器。**直接运行分析器没有安全隔离，不接受不可信编译数据库**；`--output` 也仅供调用方指定自有临时文件。命令数据库不作为 shell 脚本执行，但 Clang 参数本身可能产生副作用，不能绕过公开入口的保护。

构建需匹配版本的 LLVM/Clang 开发包（含 `ClangConfig.cmake`、`Tooling.h` 和库），当前验证 LLVM/Clang 18、Linux、C++17：

```bash
cmake -S analyzer -B .build/analyzer -DCMAKE_PREFIX_PATH=/path/to/llvm
cmake --build .build/analyzer -j2
```

协议版本 2：JSONL `entity/call/file/unit` 均带物理 TU 路径。`file` 的 SHA-256 来自 SourceManager 实际缓冲区；项目文件使用 `content_hex` 无损保存字节，授权依赖/系统文件仅保存哈希。锚点含物理文件、行、字节列、偏移和缓冲区哈希。调用还保存 spelling 与逐层 expansion 链，导入器用 caller/target/dispatch/完整锚点的稳定内容哈希建立实例身份；重复 TU 观察另存来源。USR 用 Clang 生成，内部链接另加物理声明路径及 TU，外部实体按 USR 归并。定义冲突检查位置、缓冲区身份和 Clang ODRHash；它不是全程序 ODR 证明。

函数上下文覆盖 FunctionDecl 的派生类及 lambda operator；遍历简单模板实例。只报告函数体中可求值的显式 `CallExpr`，不报告 sizeof/noexcept/decltype 中的调用。取地址不是调用；虚调用保留静态候选，指针目标未知标为 `unknown-function-pointer`，模板依赖表达式标为 `unknown-dependent`，均不伪造目标。隐式构造/析构、全局初始化边、复杂模板与 PCH/modules 不在 M0 保证范围。每个 unit 含真实 driver/frontend 诊断、状态及 Clang 版本；任何 error 或 tool failure 导致非零，不导入候选。

所有已读取文件按 TU 记录；跨 TU 同路径不同内容拒绝发布。系统头仅哈希且动态运行库未整体归档，因此该索引用于解释已观测输入，不宣称完整构建可复现。编译数据库原始字节身份与实际归一化命令身份分别保存。
