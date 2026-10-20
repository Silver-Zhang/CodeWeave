#include "shared.hpp"

namespace demo {
int alpha() { return 11; }
int beta() { return 22; }
int overloaded(int value) { return value + literal(); }
double overloaded(double value) { return value + 0.5; }
int Reader::read() const { return alpha(); }
int Reader::dispatch() const { return beta(); }
}
