/*
 * Запускатель «GOBL(in) Voice.app».
 *
 * Остаётся главным процессом приложения и запускает Contents/Resources/orca-voice.sh
 * (тот находит Python в lib и стартует app.py) как дочерний процесс.
 * Так macOS считает «ответственным» само приложение: разрешения «Микрофон»
 * и «Универсальный доступ» показываются как «GOBL(in) Voice», а не python3.13.
 *
 * Сборка: tools/build-launcher.sh
 */
#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>

extern char **environ;
static pid_t child = 0;

/* сигналы (выход, остановка из stop.sh) передаём Python */
static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(void) {
    /* путь к себе: .../GOBL(in) Voice.app/Contents/MacOS/orca-voice */
    char exe[PATH_MAX], real[PATH_MAX], script[PATH_MAX + 4];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) {
        fprintf(stderr, "launcher: cannot find itself\n");
        return 1;
    }
    /* .../Contents/MacOS/orca-voice -> .../Contents/Resources/orca-voice.sh */
    char *slash = strrchr(real, '/');
    if (slash) *slash = '\0';
    snprintf(script, sizeof(script), "%s/../Resources/orca-voice.sh", real);

    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);

    char *argv[] = {"/bin/sh", script, NULL};
    int err = posix_spawn(&child, "/bin/sh", NULL, NULL, argv, environ);
    if (err != 0) {
        fprintf(stderr, "launcher: cannot start %s (%d)\n", script, err);
        return 1;
    }

    /* ждём, пока программа закроется, и выходим с её кодом */
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) return 1;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
