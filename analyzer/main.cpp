#include <clang/AST/ASTConsumer.h>
#include <clang/AST/RecursiveASTVisitor.h>
#include <clang/Frontend/CompilerInstance.h>
#include <clang/Frontend/FrontendActions.h>
#include <clang/Index/USRGeneration.h>
#include <clang/Tooling/CompilationDatabase.h>
#include <clang/Tooling/Tooling.h>
#include <llvm/Support/CommandLine.h>
#include <llvm/Support/raw_ostream.h>

#include <cstdio>
#include <fstream>
#include <optional>
#include <set>
#include <string>
#include <vector>

namespace {

llvm::cl::OptionCategory Category("CodeWeave analyzer options");
llvm::cl::opt<std::string> CompilationDatabasePath(
    "compilation-database", llvm::cl::desc("Directory containing compile_commands.json"),
    llvm::cl::Required, llvm::cl::cat(Category));
llvm::cl::opt<std::string> OutputPath("output", llvm::cl::desc("JSONL output path"),
                                      llvm::cl::Required, llvm::cl::cat(Category));

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

class Emitter {
public:
  explicit Emitter(std::ostream &out) : out_(out) {}

  void entity(const clang::FunctionDecl *decl, clang::SourceManager &sourceManager) {
    auto usr = usrFor(decl);
    if (!usr || !seen_.insert(*usr).second) return;
    const auto canonical = decl->getCanonicalDecl();
    const auto location = sourceManager.getExpansionLoc(canonical->getLocation());
    const auto presumed = sourceManager.getPresumedLoc(location);
    if (!presumed.isValid() || presumed.isInvalid()) return;
    out_ << "{\"type\":\"entity\",\"usr\":" << quoted(*usr)
         << ",\"name\":" << quoted(canonical->getQualifiedNameAsString())
         << ",\"kind\":" << quoted(canonical->isCXXClassMember() ? "method" : "function")
         << ",\"declaration\":" << locationJson(presumed) << ",\"definition\":";
    if (const auto *definition = canonical->getDefinition()) {
      const auto definitionLocation = sourceManager.getExpansionLoc(definition->getLocation());
      const auto definitionPresumed = sourceManager.getPresumedLoc(definitionLocation);
      if (definitionPresumed.isValid() && !definitionPresumed.isInvalid()) {
        out_ << locationJson(definitionPresumed);
      } else {
        out_ << "null";
      }
    } else {
      out_ << "null";
    }
    out_ << "}\n";
  }

  void call(const clang::CallExpr *call, const clang::FunctionDecl *caller,
            clang::SourceManager &sourceManager) {
    const auto location = sourceManager.getExpansionLoc(call->getExprLoc());
    const auto presumed = sourceManager.getPresumedLoc(location);
    if (!presumed.isValid() || presumed.isInvalid()) return;
    const clang::FunctionDecl *callee = call->getDirectCallee();
    std::string dispatch = "unknown-function-pointer";
    if (callee) {
      dispatch = "direct";
      if (const auto *method = llvm::dyn_cast<clang::CXXMethodDecl>(callee)) {
        if (method->isVirtual()) dispatch = "virtual";
      }
    }
    out_ << "{\"type\":\"call\",\"caller_usr\":";
    if (const auto source = usrFor(caller)) out_ << quoted(*source); else out_ << "null";
    out_ << ",\"target_usr\":";
    if (const auto target = usrFor(callee)) out_ << quoted(*target); else out_ << "null";
    out_ << ",\"dispatch\":" << quoted(dispatch)
         << ",\"location\":" << locationJson(presumed) << "}\n";
  }

private:
  std::string locationJson(const clang::PresumedLoc &location) const {
    return "{\"file\":" + quoted(location.getFilename()) + ",\"line\":" +
           std::to_string(location.getLine()) + ",\"column\":" +
           std::to_string(location.getColumn()) + "}";
  }
  std::ostream &out_;
  std::set<std::string> seen_;
};

class Visitor : public clang::RecursiveASTVisitor<Visitor> {
public:
  Visitor(clang::ASTContext &context, Emitter &emitter) : context_(context), emitter_(emitter) {}
  bool TraverseFunctionDecl(clang::FunctionDecl *decl) {
    functionStack_.push_back(decl);
    const bool result = clang::RecursiveASTVisitor<Visitor>::TraverseFunctionDecl(decl);
    functionStack_.pop_back();
    return result;
  }
  bool VisitFunctionDecl(clang::FunctionDecl *decl) {
    if (!context_.getSourceManager().isInMainFile(decl->getLocation())) return true;
    emitter_.entity(decl, context_.getSourceManager());
    return true;
  }
  bool VisitCallExpr(clang::CallExpr *call) {
    if (!context_.getSourceManager().isInMainFile(call->getExprLoc())) return true;
    if (!functionStack_.empty()) {
      emitter_.call(call, functionStack_.back(), context_.getSourceManager());
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
    Visitor visitor(context, emitter_);
    visitor.TraverseDecl(context.getTranslationUnitDecl());
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

} // namespace

int main(int argc, const char **argv) {
  llvm::cl::HideUnrelatedOptions(Category);
  llvm::cl::ParseCommandLineOptions(argc, argv, "CodeWeave Clang source analyzer\n");
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
  Emitter emitter(output);
  clang::tooling::ClangTool tool(*database, database->getAllFiles());
  Factory factory(emitter);
  const int result = tool.run(&factory);
  output.flush();
  if (result != 0) {
    llvm::errs() << "codeweave-analyzer: analysis failed; output must not be imported\n";
  }
  return result;
}
