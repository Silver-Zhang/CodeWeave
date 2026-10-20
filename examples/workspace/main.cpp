#include "shared.hpp"

namespace demo {
int literal() { return 7; }
int choose() { return alpha(); }
int run() {
  Reader reader;
  std::vector<int> values{1, 2};
  return choose() + reader.read() + overloaded(3) + scaled(values.size());
}
}
