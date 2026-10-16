#include <iostream>

namespace demo {
int normalize(int x) { return x + 1; }
double normalize(double x) { return x + 0.5; }

struct Stepper {
    virtual ~Stepper() = default;
    virtual int step(int x) const { return normalize(x); }
};

int twice(int value) { return value * 2; }

#define APPLY(X) twice(X)

int execute(const Stepper& s, int x) {
    int (*transform)(int) = &twice;
    const int a = s.step(x);
    const int b = transform(a);
    return APPLY(b) + static_cast<int>(normalize(2.0));
}
} // namespace demo

int main() {
    demo::Stepper stepper;
    std::cout << demo::execute(stepper, 1) << '\n';
}
