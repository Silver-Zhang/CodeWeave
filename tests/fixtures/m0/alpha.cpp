#include "shared.hpp"

int overloaded(int value) { return value + 1; }

static int local() { return overloaded(1); }

int entry() { return local() + overloaded(2); }
