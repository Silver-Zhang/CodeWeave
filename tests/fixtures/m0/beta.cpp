#include "shared.hpp"

int overloaded(double value) { return static_cast<int>(value); }

static int local() { return 7; }

int use_callback(int (*callback)(int)) { return callback(3); }

struct Base {
  virtual int run() { return 1; }
  virtual ~Base() = default;
};

struct Derived : Base {
  int run() override { return 2; }
};

int virtual_user(Base &base) { return base.run(); }

int main() {
  auto address_only = static_cast<int (*)(int)>(&overloaded);
  (void)address_only;
  Derived derived;
  return entry() + use_callback(overloaded) + virtual_user(derived) + local();
}
