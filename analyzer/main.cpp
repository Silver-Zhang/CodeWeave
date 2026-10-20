#include <clang/AST/ASTConsumer.h>
#include <clang/AST/ODRHash.h>
#include <clang/AST/Mangle.h>
#include <clang/Basic/Version.h>
#include <clang/AST/RecursiveASTVisitor.h>
#include <clang/Frontend/CompilerInstance.h>
#include <clang/Frontend/FrontendActions.h>
#include <clang/Index/USRGeneration.h>
#include <clang/Lex/Preprocessor.h>
#include <clang/Lex/Lexer.h>
#include <llvm/Support/FileSystem.h>
#include <llvm/Support/SHA256.h>
#include <llvm/ADT/StringExtras.h>
#include <clang/Tooling/CompilationDatabase.h>
#include <clang/Tooling/Tooling.h>
#include <llvm/Support/CommandLine.h>
#include <llvm/Support/raw_ostream.h>

#include <cstdio>
#include <fstream>
#include <optional>
#include <memory>
#include <string>
#include <vector>
#include <set>

namespace {

llvm::cl::OptionCategory Category("CodeWeave analyzer options");
llvm::cl::opt<std::string> CompilationDatabasePath(
    "compilation-database", llvm::cl::desc("Directory containing compile_commands.json"),
    llvm::cl::Required, llvm::cl::cat(Category));
llvm::cl::opt<std::string> OutputPath("output", llvm::cl::desc("JSONL output path"),
                                      llvm::cl::Required, llvm::cl::cat(Category));
llvm::cl::opt<std::string> ProjectRoot("project-root", llvm::cl::desc("Allowed physical source root"),
                                       llvm::cl::Required, llvm::cl::cat(Category));
llvm::cl::opt<unsigned> FactProtocol("fact-protocol", llvm::cl::init(2), llvm::cl::cat(Category));

std::string escapeJson(llvm::StringRef value) {
  std::string out;
  for (char c : value) {
    switch (c) {
    case '\\': out += "\\\\"; break;
    case '"': out += "\\\""; break;
    case '\n': out += "\\n"; break;
    case '\r': out += "\\r"; break;
    case '\t': out += "\\t"; break;
    default:
      if (static_cast<unsigned char>(c) < 0x20) {
        char buffer[7];
        snprintf(buffer, sizeof(buffer), "\\u%04x", static_cast<unsigned char>(c));
        out += buffer;
      } else {
        out += c;
      }
    }
  }
  return out;
}

std::string quoted(llvm::StringRef value) { return "\"" + escapeJson(value) + "\""; }

std::optional<std::string> usrFor(const clang::Decl *decl) {
  if (!decl) return std::nullopt;
  llvm::SmallString<256> usr;
  if (clang::index::generateUSRForDecl(decl->getCanonicalDecl(), usr)) return std::nullopt;
  return std::string(usr.str());
}

std::string canonicalPath(llvm::StringRef path);

std::string physicalPath(clang::SourceLocation location, clang::SourceManager &sourceManager) {
  const auto spelling = sourceManager.getSpellingLoc(location);
  const auto file = sourceManager.getFileLoc(spelling);
  const auto filename = sourceManager.getFilename(file);
  return filename.empty() ? std::string() : canonicalPath(filename);
}

bool allowedPath(llvm::StringRef path) {
  const std::string root = ProjectRoot;
  return path == root || (path.starts_with(root) && path.size() > root.size() && path[root.size()] == '/');
}

std::string digest(llvm::StringRef content) {
  const auto hash = llvm::SHA256::hash(llvm::arrayRefFromStringRef(content));
  return llvm::toHex(llvm::ArrayRef<uint8_t>(hash), true);
}
std::string currentTU;
std::unique_ptr<clang::MangleContext> currentMangler;
std::string canonicalPath(llvm::StringRef path) {
  llvm::SmallString<256> real;
  if (llvm::sys::fs::real_path(path, real)) return std::string(path);
  return std::string(real.str());
}

std::string identityFor(const clang::FunctionDecl *decl, clang::SourceManager &sourceManager) {
  auto usr = usrFor(decl).value_or(std::string());
  // Clang USRs encode a closure's physical source offset. Its compiler ABI
  // identity is scoped to the enclosing declaration and survives line moves.
  // Keep legacy protocol2 unchanged; do not correlate anonymous sites by name.
  if (FactProtocol == 3 && currentMangler) {
    if (const auto *method = llvm::dyn_cast<clang::CXXMethodDecl>(decl)) {
      if (method->getParent()->isLambda() || method->getParent()->isLocalClass()) {
        std::string mangled;
        llvm::raw_string_ostream stream(mangled);
        if (const auto *constructor = llvm::dyn_cast<clang::CXXConstructorDecl>(decl))
          currentMangler->mangleName(clang::GlobalDecl(constructor, clang::Ctor_Complete), stream);
        else if (const auto *destructor = llvm::dyn_cast<clang::CXXDestructorDecl>(decl))
          currentMangler->mangleName(clang::GlobalDecl(destructor, clang::Dtor_Complete), stream);
        else currentMangler->mangleName(clang::GlobalDecl(decl), stream);
        stream.flush();
        usr = "c:local-abi:" + mangled;
      }
    }
  }
  bool localToTU = !decl->hasExternalFormalLinkage();
  // A closure/local class inside an external inline function has the enclosing
  // function's ODR identity, even though its method has no formal linkage.
  for (const auto *context = decl->getDeclContext(); context; context = context->getParent()) {
    if (const auto *enclosing = llvm::dyn_cast<clang::FunctionDecl>(context)) {
      localToTU = !enclosing->hasExternalFormalLinkage();
    }
  }
  if (localToTU) {
    return usr + "@file=" + physicalPath(decl->getLocation(), sourceManager) + "@tu=" + currentTU;
  }
  return usr;
}

std::optional<unsigned> calleeOffset(const clang::CallExpr *call, clang::SourceManager &sm) {
  auto *expr = call->getCallee()->IgnoreParenImpCasts();
  clang::SourceLocation loc;
  if (auto *ref = llvm::dyn_cast<clang::DeclRefExpr>(expr)) loc = ref->getLocation();
  if (auto *member = llvm::dyn_cast<clang::MemberExpr>(expr)) loc = member->getMemberLoc();
  if (loc.isInvalid() || loc.isMacroID()) return std::nullopt;
  return sm.getFileOffset(loc);
}

class CalleeOffsets : public clang::RecursiveASTVisitor<CalleeOffsets> {
public:
  clang::SourceManager &sm;
  std::set<unsigned> offsets;
  explicit CalleeOffsets(clang::SourceManager &manager) : sm(manager) {}
  bool VisitCallExpr(clang::CallExpr *call) {
    if (auto offset = calleeOffset(call, sm)) offsets.insert(*offset);
    return true;
  }
};

std::string rangeJson(clang::SourceRange range, clang::ASTContext &context,
                      const std::set<unsigned> &mask = {}) {
  auto &sm = context.getSourceManager();
  auto begin = range.getBegin(), end = range.getEnd();
  if (begin.isInvalid() || end.isInvalid() || begin.isMacroID() || end.isMacroID()) return "null";
  if (!allowedPath(physicalPath(begin, sm))) return "null";
  end = clang::Lexer::getLocForEndOfToken(end, 0, sm, context.getLangOpts());
  if (end.isInvalid() || sm.getFileID(begin) != sm.getFileID(end)) return "null";
  unsigned start = sm.getFileOffset(begin), stop = sm.getFileOffset(end);
  auto buffer = sm.getBufferData(sm.getFileID(begin));
  if (stop < start || stop > buffer.size()) return "null";
  std::string tokens;
  auto loc = begin;
  while (sm.getFileOffset(loc) < stop) {
    clang::Token token;
    if (clang::Lexer::getRawToken(loc, token, sm, context.getLangOpts(), true)) break;
    auto offset = sm.getFileOffset(token.getLocation());
    if (offset >= stop || !token.getLength()) break;
    if (!token.is(clang::tok::comment)) {
      auto spelling = mask.count(offset) ? "@callee" : clang::Lexer::getSpelling(token, sm, context.getLangOpts());
      tokens += std::to_string(spelling.size()) + ":" + spelling + ";";
    }
    loc = token.getLocation().getLocWithOffset(token.getLength());
  }
  return "{\"file\":" + quoted(physicalPath(begin, sm)) + ",\"begin\":" + std::to_string(start) +
      ",\"end\":" + std::to_string(stop) + ",\"sha256\":" + quoted(digest(buffer.slice(start, stop))) +
      ",\"buffer_sha256\":" + quoted(digest(buffer)) + ",\"token_sha256\":" + quoted(digest(tokens)) + "}";
}

class Emitter {
public:
  explicit Emitter(std::ostream &out) : out_(out) {}

  void file(llvm::StringRef path, llvm::StringRef content) {
    out_ << "{\"schema\":" << FactProtocol << ",\"type\":\"file\",\"tu\":" << quoted(currentTU)
         << ",\"path\":" << quoted(path) << ",\"sha256\":" << quoted(digest(content))
         << ",\"content_hex\":" << (allowedPath(path) ? quoted(llvm::toHex(content)) : "null") << "}\n";
  }

  void entity(const clang::FunctionDecl *decl, clang::ASTContext &context) {
    auto &sourceManager = context.getSourceManager();
    const auto usr = usrFor(decl);
    if (!usr) return;
    const auto identity = identityFor(decl, sourceManager);
    const auto canonical = decl->getCanonicalDecl();
    const auto location = sourceManager.getSpellingLoc(canonical->getLocation());
    const auto file = physicalPath(location, sourceManager);
    if (file.empty() || !allowedPath(file)) return;
    out_ << "{\"schema\":" << FactProtocol << ",\"type\":\"entity\",\"tu\":" << quoted(currentTU) << ",\"usr\":" << quoted(identity)
         << ",\"semantic_usr\":" << quoted(*usr)
         << ",\"name\":" << quoted(canonical->getQualifiedNameAsString())
         << ",\"kind\":" << quoted(canonical->isCXXClassMember() ? "method" : "function")
         << ",\"declaration\":" << locationJson(location, sourceManager) << ",\"definition\":";
    if (const auto *definition = canonical->getDefinition()) {
      const auto definitionLocation = sourceManager.getSpellingLoc(definition->getLocation());
      if (!physicalPath(definitionLocation, sourceManager).empty()) {
        out_ << locationJson(definitionLocation, sourceManager);
      } else {
        out_ << "null";
      }
    } else {
      out_ << "null";
    }
    out_ << ",\"definition_odr\":";
    if (const auto *definition = canonical->getDefinition()) {
      clang::ODRHash hash;
      hash.AddFunctionDecl(definition);
      out_ << hash.CalculateHash();
    } else out_ << "null";
    if (FactProtocol == 3) {
      const auto *definition = canonical->getDefinition();
      CalleeOffsets masks(sourceManager);
      if (definition && definition->hasBody()) masks.TraverseStmt(definition->getBody());
      out_ << ",\"linkage\":" << quoted(decl->hasExternalFormalLinkage() ? "external" : "internal")
           << ",\"owner_tu\":" << quoted(currentTU)
           << ",\"definition_range\":" << (definition ? rangeJson(definition->getSourceRange(), context) : "null")
           << ",\"normalized_definition\":" << (definition ? rangeJson(definition->getSourceRange(), context, masks.offsets) : "null");
    }
    out_ << "}\n";
  }

  void call(const clang::CallExpr *call, const clang::FunctionDecl *caller,
            clang::ASTContext &context) {
    auto &sourceManager = context.getSourceManager();
    const auto location = sourceManager.getExpansionLoc(call->getExprLoc());
    const auto physical = physicalPath(location, sourceManager);
    if (physical.empty() || !allowedPath(physical)) return;
    const clang::FunctionDecl *callee = call->getDirectCallee();
    std::string dispatch = call->isTypeDependent() || call->isValueDependent()
                               ? "unknown-dependent" : "unknown-function-pointer";
    if (callee) {
      dispatch = "direct";
      if (const auto *method = llvm::dyn_cast<clang::CXXMethodDecl>(callee)) {
        if (method->isVirtual()) {
          const auto *member = llvm::dyn_cast<clang::MemberExpr>(call->getCallee()->IgnoreParenImpCasts());
          if (!member || !member->hasQualifier()) dispatch = "virtual";
        }
      }
    }
    out_ << "{\"schema\":" << FactProtocol << ",\"type\":\"call\",\"tu\":" << quoted(currentTU) << ",\"caller_usr\":" << quoted(identityFor(caller, sourceManager));
    out_ << ",\"target_usr\":";
    if (callee) out_ << quoted(identityFor(callee, sourceManager)); else out_ << "null";
    out_ << ",\"dispatch\":" << quoted(dispatch)
         << ",\"location\":" << locationJson(location, sourceManager)
         << ",\"spelling\":" << locationJson(sourceManager.getSpellingLoc(call->getExprLoc()), sourceManager)
         << ",\"expansion_chain\":[";
    auto macro = call->getExprLoc();
    bool first = true;
    while (macro.isMacroID()) {
      if (!first) out_ << ",";
      first = false;
      const auto range = sourceManager.getImmediateExpansionRange(macro);
      out_ << "[" << locationJson(sourceManager.getSpellingLoc(range.getBegin()), sourceManager)
           << "," << locationJson(sourceManager.getSpellingLoc(range.getEnd()), sourceManager) << "]";
      macro = sourceManager.getImmediateMacroCallerLoc(macro);
    }
    out_ << "]";
    if (FactProtocol == 3) {
      std::set<unsigned> masks;
      if (auto offset = calleeOffset(call, sourceManager)) masks.insert(*offset);
      out_ << ",\"syntax\":" << (masks.empty() ? "null" : rangeJson(call->getSourceRange(), context, masks));
    }
    out_ << "}\n";
  }

private:
  std::string locationJson(clang::SourceLocation location,
                           clang::SourceManager &sourceManager) const {
    const auto spelling = sourceManager.getSpellingLoc(location);
    bool invalid = false;
    const auto line = sourceManager.getSpellingLineNumber(spelling, &invalid);
    const auto column = sourceManager.getSpellingColumnNumber(spelling, &invalid);
    const auto offset = sourceManager.getFileOffset(spelling);
    return "{\"file\":" + quoted(physicalPath(spelling, sourceManager)) + ",\"line\":" +
           std::to_string(invalid ? 0 : line) + ",\"column\":" +
           std::to_string(invalid ? 0 : column) + ",\"offset\":" + std::to_string(offset) + ",\"sha256\":" + quoted(digest(sourceManager.getBufferData(sourceManager.getFileID(spelling)))) + "}";
  }
  std::ostream &out_;
};

class Visitor : public clang::RecursiveASTVisitor<Visitor> {
public:
  Visitor(clang::ASTContext &context, Emitter &emitter) : context_(context), emitter_(emitter) {}
  // TraverseDecl is the common dispatch point for all FunctionDecl subclasses.
  bool TraverseDecl(clang::Decl *decl) {
    auto *function = llvm::dyn_cast_or_null<clang::FunctionDecl>(decl);
    if (function) functionStack_.push_back(function);
    const bool result = clang::RecursiveASTVisitor<Visitor>::TraverseDecl(decl);
    if (function) functionStack_.pop_back();
    return result;
  }
  bool shouldVisitTemplateInstantiations() const { return true; }
  bool TraverseLambdaExpr(clang::LambdaExpr *expr) {
    for (auto *init : expr->capture_inits()) if (init && !TraverseStmt(init)) return false;
    return TraverseDecl(expr->getCallOperator());
  }
  bool TraverseUnaryExprOrTypeTraitExpr(clang::UnaryExprOrTypeTraitExpr *) { return true; }
  bool TraverseCXXNoexceptExpr(clang::CXXNoexceptExpr *) { return true; }
  bool TraverseDecltypeTypeLoc(clang::DecltypeTypeLoc) { return true; }
  bool TraverseDecltypeType(clang::DecltypeType *) { return true; }
  bool VisitFunctionDecl(clang::FunctionDecl *decl) {
    const auto path = physicalPath(decl->getLocation(), context_.getSourceManager());
    if (!allowedPath(path)) return true;
    emitter_.entity(decl, context_);
    return true;
  }
  bool VisitCallExpr(clang::CallExpr *call) {
    const auto path = physicalPath(call->getExprLoc(), context_.getSourceManager());
    if (!allowedPath(path)) return true;
    if (!functionStack_.empty()) {
      if (const auto *target = call->getDirectCallee()) emitter_.entity(target, context_);
      emitter_.call(call, functionStack_.back(), context_);
    }
    return true;
  }
private:
  clang::ASTContext &context_;
  Emitter &emitter_;
  std::vector<const clang::FunctionDecl *> functionStack_;
};

class Consumer : public clang::ASTConsumer {
public:
  explicit Consumer(Emitter &emitter) : emitter_(emitter) {}
  void HandleTranslationUnit(clang::ASTContext &context) override {
    currentMangler.reset(context.createMangleContext());
    Visitor visitor(context, emitter_);
    visitor.TraverseDecl(context.getTranslationUnitDecl());
    currentMangler.reset();
  }
private:
  Emitter &emitter_;
};

class Action : public clang::ASTFrontendAction {
public:
  explicit Action(Emitter &emitter) : emitter_(emitter) {}
  std::unique_ptr<clang::ASTConsumer> CreateASTConsumer(clang::CompilerInstance &, llvm::StringRef) override {
    return std::make_unique<Consumer>(emitter_);
  }
  void EndSourceFileAction() override {
    auto &sourceManager = getCompilerInstance().getSourceManager();
    for (auto it = sourceManager.fileinfo_begin(); it != sourceManager.fileinfo_end(); ++it) {
      const auto file = sourceManager.translateFile(it->first);
      if (file.isInvalid()) continue;
      bool invalid = false;
      const auto content = sourceManager.getBufferData(file, &invalid);
      if (invalid) continue;
      const auto path = canonicalPath(it->first.getName());
      emitter_.file(path, content);
    }
  }
private:
  Emitter &emitter_;
};

class Factory : public clang::tooling::FrontendActionFactory {
public:
  explicit Factory(Emitter &emitter) : emitter_(emitter) {}
  std::unique_ptr<clang::FrontendAction> create() override { return std::make_unique<Action>(emitter_); }
private:
  Emitter &emitter_;
};

class Diagnostics : public clang::DiagnosticConsumer {
public:
  bool failed = false;
  std::vector<std::string> records;
  void HandleDiagnostic(clang::DiagnosticsEngine::Level level, const clang::Diagnostic &info) override {
    clang::DiagnosticConsumer::HandleDiagnostic(level, info);
    llvm::SmallString<256> message;
    info.FormatDiagnostic(message);
    const bool error = level >= clang::DiagnosticsEngine::Error;
    failed |= error;
    const std::string severity = error ? "error" : level == clang::DiagnosticsEngine::Warning ? "warning" : "note";
    records.push_back("{\"level\":" + quoted(severity) + ",\"message\":" + quoted(message) + "}");
    llvm::errs() << severity << ": " << message << "\n";
  }
};

} // namespace

int main(int argc, const char **argv) {
  llvm::cl::HideUnrelatedOptions(Category);
  llvm::cl::ParseCommandLineOptions(argc, argv, "CodeWeave Clang source analyzer\n");
  if (FactProtocol != 2 && FactProtocol != 3) return 2;
  std::string error;
  auto database = clang::tooling::CompilationDatabase::loadFromDirectory(CompilationDatabasePath, error);
  if (!database) {
    llvm::errs() << "codeweave-analyzer: " << error << "\n";
    return 2;
  }
  std::ofstream output(OutputPath, std::ios::trunc);
  if (!output) {
    llvm::errs() << "codeweave-analyzer: cannot write output " << OutputPath << "\n";
    return 2;
  }
  // Internal component: only the public Python entry point authorizes inputs and
  // provides OS isolation. This executable must never be used on untrusted input.
  Emitter emitter(output);
  int result = 0;
  for (const auto &file : database->getAllFiles()) {
    currentTU = canonicalPath(file);
    clang::tooling::ClangTool tool(*database, {file});
    Diagnostics diagnostics;
    tool.setDiagnosticConsumer(&diagnostics);
    Factory factory(emitter);
    const int unitResult = tool.run(&factory);
    const bool failed = unitResult != 0 || diagnostics.failed;
    result |= failed;
    output << "{\"schema\":" << FactProtocol << ",\"type\":\"unit\",\"tu\":" << quoted(currentTU)
           << ",\"clang_version\":" << quoted(CLANG_VERSION_STRING)
           << ",\"status\":" << quoted(failed ? "failed" : "complete") << ",\"diagnostics\":[";
    for (size_t i = 0; i < diagnostics.records.size(); ++i) {
      if (i) output << ",";
      output << diagnostics.records[i];
    }
    output << "]}\n";
  }
  output.flush();
  return output ? result : 2;
}
