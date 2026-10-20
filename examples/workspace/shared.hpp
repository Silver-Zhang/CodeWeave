#pragma once
#include <vector>
#include <cstddef>
#include "config.hpp"

namespace demo {
int alpha();
int beta();
int literal();
int choose();
int overloaded(int value);
double overloaded(double value);
inline int scaled(int value) { return value * DEMO_SCALE; }
class Reader {
public:
  int read() const;
  virtual int dispatch() const;
};
int run();
}
