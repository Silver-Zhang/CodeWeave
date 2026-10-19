#include "shared.hpp"
static int local() { return over(1); }
int over(int n) { return n + 3; }
int left() { return local() + over(2) + over(3); }
int ping(int n) { return n ? pong(n - 1) : 0; }
int unicode_entry() { const char *s = u8"汉😀"; (void)s; return over(2); }
int han_entry() { const char *s = u8"汉"; (void)s; return over(2); }
int emoji_entry() { const char *s = u8"😀"; (void)s; return over(2); }
int ascii_entry() { const char *s = "ascii"; (void)s; return over(2); }
/*汉😀*/ int named_after_unicode() { return over(2); }
int combining_entry() { const char *s = u8"á"; (void)s; return over(2); }
/*汉😀*/ int eol_entry() { return over(2)
; }
