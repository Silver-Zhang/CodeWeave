#include "shared.hpp"
static int local() { return 7; }
double over(double n) { return n + .5; }
int right() { return local() + left(); }
int pong(int n) { return n ? ping(n - 1) : 0; }
int dispatch(Base &b, int (*fn)(int)) { return b.run() + fn(4); }
