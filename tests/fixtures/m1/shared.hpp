#pragma once
int over(int);
double over(double);
int left();
int right();
int ping(int);
int pong(int);
struct Base { virtual int run() { return over(2); } };
