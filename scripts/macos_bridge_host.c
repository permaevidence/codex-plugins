#include <errno.h>
#include <limits.h>
#include <pwd.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static pid_t child_pid = -1;

static void forward_signal(int signal_number) {
    if (child_pid > 0) {
        (void)kill(-child_pid, signal_number);
    }
}

static const char *home_directory(void) {
    const char *value = getenv("HOME");
    if (value != NULL && value[0] != '\0') {
        return value;
    }
    struct passwd *entry = getpwuid(getuid());
    return entry == NULL ? NULL : entry->pw_dir;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        puts("PermaEvidence Codex Bridge Host 1");
        return 0;
    }
    if (argc != 1) {
        fprintf(stderr, "usage: %s [--version]\n", argv[0]);
        return 64;
    }

    const char *override = getenv("PERMAEVIDENCE_CODEX_START_SCRIPT");
    const char *home = home_directory();
    char script[PATH_MAX];
    if (override != NULL && override[0] != '\0') {
        if (snprintf(script, sizeof(script), "%s", override) >= (int)sizeof(script)) {
            fputs("bridge start-script override is too long\n", stderr);
            return 78;
        }
    } else {
        if (home == NULL) {
            fputs("could not determine the user home directory\n", stderr);
            return 78;
        }
        int written = snprintf(
            script,
            sizeof(script),
            "%s/Library/Application Support/PermaEvidenceCodex/current/"
            "plugins/codex-telegram-bridge/scripts/start_bridge.sh",
            home
        );
        if (written < 0 || written >= (int)sizeof(script)) {
            fputs("bridge start-script path is too long\n", stderr);
            return 78;
        }
    }

    if (access(script, R_OK) != 0) {
        fprintf(stderr, "bridge start script is unavailable: %s: %s\n", script, strerror(errno));
        return 78;
    }

    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = forward_signal;
    sigemptyset(&action.sa_mask);
    (void)sigaction(SIGTERM, &action, NULL);
    (void)sigaction(SIGINT, &action, NULL);
    (void)sigaction(SIGHUP, &action, NULL);

    child_pid = fork();
    if (child_pid < 0) {
        fprintf(stderr, "could not start bridge supervisor: %s\n", strerror(errno));
        return 71;
    }
    if (child_pid == 0) {
        (void)setpgid(0, 0);
        execl("/bin/bash", "bash", script, (char *)NULL);
        fprintf(stderr, "could not execute /bin/bash: %s\n", strerror(errno));
        _exit(71);
    }

    (void)setpgid(child_pid, child_pid);
    int status = 0;
    while (waitpid(child_pid, &status, 0) < 0) {
        if (errno == EINTR) {
            continue;
        }
        fprintf(stderr, "could not wait for bridge supervisor: %s\n", strerror(errno));
        return 71;
    }
    child_pid = -1;
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 1;
}
