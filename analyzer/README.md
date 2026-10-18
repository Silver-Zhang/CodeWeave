# analyzer

Clang LibTooling 独立提取器，产生由 Python 内核导入的 JSONL 语义事实；不使用正则或手写图替代 C++ 语义分析。它从受控的 `compile_commands.json` 读取翻译单元，不执行其中的构建命令。

## 构建

需匹配版本的 LLVM/Clang **开发**包（含 `ClangConfig.cmake`、`Tooling.h` 和库）。示例：

```bash
cmake -S analyzer -B .build/analyzer -DCMAKE_PREFIX_PATH=/path/to/llvm/lib/cmake
cmake --build .build/analyzer -j2
```

构建产物为 `.build/analyzer/codeweave-analyzer`。分析器的非零退出码表示本次分析不可导入；应用内核会保留旧 SQLite 索引。
