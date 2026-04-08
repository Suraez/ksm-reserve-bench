#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/prctl.h>
#include <unistd.h>

#ifndef PR_SET_MEMORY_MERGE
#define PR_SET_MEMORY_MERGE 67
#endif

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <program> [args...]\n", argv[0]);
        return 1;
    }

    if (prctl(PR_SET_MEMORY_MERGE, 1, 0, 0, 0) != 0) {
        fprintf(stderr, "warning: prctl(PR_SET_MEMORY_MERGE) failed: %s\n", strerror(errno));
    }

    execvp(argv[1], &argv[1]);
    fprintf(stderr, "execvp failed: %s\n", strerror(errno));
    return 1;
}