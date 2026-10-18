#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <time.h>
#include <stdio.h>
static int seen_a=0, seen_b=0, paused=0;
static void pause_second(const char *path) {
  const char *root=getenv("AUDIT_FIXTURE");
  const char *out=getenv("AUDIT_PAUSE_DIR");
  if (!root || !out || strncmp(path,root,strlen(root))!=0) return;
  int *seen=NULL;
  if (strcmp(path+strlen(root),"/a.cpp")==0) seen=&seen_a;
  if (strcmp(path+strlen(root),"/b.cpp")==0) seen=&seen_b;
  if (!seen || *seen) return;
  *seen=1;
  if (!seen_a || !seen_b || paused) return;
  paused=1;
  char marker[4096],release[4096];
  snprintf(marker,sizeof(marker),"%s/second-source-ready",out);
  snprintf(release,sizeof(release),"%s/second-source-release",out);
  long fd=syscall(SYS_openat,AT_FDCWD,marker,O_WRONLY|O_CREAT|O_TRUNC,0600);
  if (fd>=0) { syscall(SYS_write,fd,path,strlen(path));syscall(SYS_close,fd); }
  struct timespec delay={0,10000000};
  for (int i=0;i<3000;i++) {
    fd=syscall(SYS_openat,AT_FDCWD,release,O_RDONLY,0);
    if (fd>=0) {syscall(SYS_close,fd);return;}
    nanosleep(&delay,NULL);
  }
}
int open(const char *path,int flags,...) {
  mode_t mode=0;
  if (flags&O_CREAT) {va_list ap;va_start(ap,flags);mode=va_arg(ap,int);va_end(ap);}
  pause_second(path);
  int (*real_open)(const char*,int,...)=dlsym(RTLD_NEXT,"open");
  return real_open(path,flags,mode);
}
