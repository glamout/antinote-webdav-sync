/*
 * AntinoteWebDAVSync — Native C macOS Background Container Watcher
 *
 * Runs as the main executable of AntinoteWebDAVSync.app (LSUIElement=true).
 * - Keeps a tiny ~1.5 MB resident footprint and 0.0% idle CPU usage.
 * - Directly reads Antinote's sandboxed SQLite + WAL + SHM files under the
 *   AntinoteWebDAVSync.app bundle identity (so /usr/bin/python3 needs ZERO TCC permissions).
 * - Copies changed database files into ~/Library/Application Support/AntinoteWebDAVSync/mirror_db/
 *   and invokes src/antinote_webdav_sync.py on the mirrored database.
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <time.h>
#include <signal.h>
#include <pwd.h>
#include <limits.h>
#include <sys/stat.h>
#include <sys/file.h>
#include <sys/wait.h>

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

static volatile int keep_running = 1;

static void handle_sig(int sig) {
    (void)sig;
    keep_running = 0;
}

static const char *resolve_home_dir(void) {
    const char *home = getenv("HOME");
    if (home && home[0] != '\0') {
        return home;
    }
    struct passwd *pw = getpwuid(getuid());
    if (pw && pw->pw_dir && pw->pw_dir[0] != '\0') {
        return pw->pw_dir;
    }
    return "/tmp";
}

static int join_path(char *dst, size_t dst_size, const char *base, const char *sub) {
    int n = snprintf(dst, dst_size, "%s/%s", base, sub);
    return (n > 0 && (size_t)n < dst_size) ? 0 : -1;
}

static uint64_t fnv1a_file(const char *path, uint64_t h, int *perm_err) {
    struct stat st;
    if (stat(path, &st) != 0) {
        if (perm_err && (errno == EPERM || errno == EACCES)) {
            *perm_err = errno;
        }
        return h ^ 0xFFULL;
    }
    h ^= (uint64_t)st.st_mtime;
    h *= 1099511628211ULL;
    h ^= (uint64_t)st.st_size;
    h *= 1099511628211ULL;

    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        if (perm_err && (errno == EPERM || errno == EACCES)) {
            *perm_err = errno;
        }
        return h;
    }
    unsigned char buf[65536];
    ssize_t n;
    while ((n = read(fd, buf, sizeof(buf))) > 0) {
        for (ssize_t i = 0; i < n; i++) {
            h ^= (uint64_t)buf[i];
            h *= 1099511628211ULL;
        }
    }
    close(fd);
    return h;
}

static int copy_file_c(const char *src, const char *dst) {
    int in_fd = open(src, O_RDONLY);
    if (in_fd < 0) {
        unlink(dst);
        return -1;
    }
    int out_fd = open(dst, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (out_fd < 0) {
        close(in_fd);
        return -1;
    }
    unsigned char buf[65536];
    ssize_t n;
    while ((n = read(in_fd, buf, sizeof(buf))) > 0) {
        ssize_t written = 0;
        while (written < n) {
            ssize_t w = write(out_fd, buf + written, (size_t)(n - written));
            if (w <= 0) break;
            written += w;
        }
    }
    close(in_fd);
    close(out_fd);
    return 0;
}

static int read_json_int(const char *cfg_path, const char *key, int default_val, int min_val, int max_val) {
    int fd = open(cfg_path, O_RDONLY);
    if (fd < 0) return default_val;
    char buf[8192];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return default_val;
    buf[n] = '\0';

    char pattern[128];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    char *p = strstr(buf, pattern);
    if (!p) return default_val;
    p = strchr(p + strlen(pattern), ':');
    if (!p) return default_val;
    int val = atoi(p + 1);
    return (val >= min_val && val <= max_val) ? val : default_val;
}

static void resolve_source_db_path(const char *home, const char *cfg_path, char *out_path, size_t out_size) {
    /* 1. Check if user specified antinote_db_path in config.json */
    int fd = open(cfg_path, O_RDONLY);
    if (fd >= 0) {
        char buf[8192];
        ssize_t n = read(fd, buf, sizeof(buf) - 1);
        close(fd);
        if (n > 0) {
            buf[n] = '\0';
            char *p = strstr(buf, "\"antinote_db_path\"");
            if (p) {
                p = strchr(p, ':');
                if (p) {
                    char *q1 = strchr(p + 1, '"');
                    if (q1) {
                        char *q2 = strchr(q1 + 1, '"');
                        if (q2 && q2 > q1 + 1) {
                            size_t len = (size_t)(q2 - (q1 + 1));
                            if (len < out_size - 1) {
                                char tmp[PATH_MAX];
                                memcpy(tmp, q1 + 1, len);
                                tmp[len] = '\0';
                                if (tmp[0] == '~' && tmp[1] == '/') {
                                    (void)join_path(out_path, out_size, home, tmp + 2);
                                } else {
                                    snprintf(out_path, out_size, "%s", tmp);
                                }
                                return;
                            }
                        }
                    }
                }
            }
        }
    }

    /* 2. Default Antinote sandbox container path on macOS */
    snprintf(
        out_path,
        out_size,
        "%s/Library/Containers/com.chabomakers.Antinote/Data/Library/Application Support/cd-v1-notes.sqlite",
        home
    );
}

static void run_python_sync(const char *py_script, const char *mirror_db, int force) {
    pid_t pid = fork();
    if (pid == 0) {
        char db_arg[PATH_MAX + 16];
        snprintf(db_arg, sizeof(db_arg), "--db=%s", mirror_db);
        if (force) {
            char *args[] = { "/usr/bin/python3", (char *)py_script, db_arg, "--force", NULL };
            execv("/usr/bin/python3", args);
        } else {
            char *args[] = { "/usr/bin/python3", (char *)py_script, db_arg, NULL };
            execv("/usr/bin/python3", args);
        }
        _exit(1);
    } else if (pid > 0) {
        int status = 0;
        waitpid(pid, &status, 0);
    }
}

int main(void) {
    signal(SIGTERM, handle_sig);
    signal(SIGINT, handle_sig);

    const char *home = resolve_home_dir();

    char work_dir[PATH_MAX];
    char lock_path[PATH_MAX];
    char cfg_path[PATH_MAX];
    char py_script[PATH_MAX];
    char mirror_dir[PATH_MAX];
    char mirror_db[PATH_MAX];
    char mirror_wal[PATH_MAX];
    char mirror_shm[PATH_MAX];
    char src_db[PATH_MAX];
    char src_wal[PATH_MAX];
    char src_shm[PATH_MAX];

    if (join_path(work_dir, sizeof(work_dir), home, "Library/Application Support/AntinoteWebDAVSync") != 0 ||
        join_path(lock_path, sizeof(lock_path), work_dir, "daemon.lock") != 0 ||
        join_path(cfg_path, sizeof(cfg_path), work_dir, "config.json") != 0 ||
        join_path(py_script, sizeof(py_script), work_dir, "antinote_webdav_sync.py") != 0 ||
        join_path(mirror_dir, sizeof(mirror_dir), work_dir, "mirror_db") != 0 ||
        join_path(mirror_db, sizeof(mirror_db), mirror_dir, "cd-v1-notes.sqlite") != 0 ||
        join_path(mirror_wal, sizeof(mirror_wal), mirror_dir, "cd-v1-notes.sqlite-wal") != 0 ||
        join_path(mirror_shm, sizeof(mirror_shm), mirror_dir, "cd-v1-notes.sqlite-shm") != 0) {
        return 1;
    }

    mkdir(work_dir, 0700);
    mkdir(mirror_dir, 0700);

    int out_fd = open("/tmp/antinote-webdav-sync.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
    int err_fd = open("/tmp/antinote-webdav-sync.err", O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (out_fd >= 0) { dup2(out_fd, STDOUT_FILENO); close(out_fd); }
    if (err_fd >= 0) { dup2(err_fd, STDERR_FILENO); close(err_fd); }

    int lock_fd = open(lock_path, O_WRONLY | O_CREAT, 0600);
    if (lock_fd < 0 || flock(lock_fd, LOCK_EX | LOCK_NB) != 0) {
        return 0;
    }

    uint64_t last_hash = 0;
    time_t last_audit = 0;
    int first_run = 1;

    while (keep_running) {
        resolve_source_db_path(home, cfg_path, src_db, sizeof(src_db));
        snprintf(src_wal, sizeof(src_wal), "%.4080s-wal", src_db);
        snprintf(src_shm, sizeof(src_shm), "%.4080s-shm", src_db);

        int perm_err = 0;
        uint64_t h = 14695981039346656037ULL;
        h = fnv1a_file(src_db, h, &perm_err);
        h = fnv1a_file(src_wal, h, &perm_err);
        h = fnv1a_file(src_shm, h, &perm_err);

        if (perm_err != 0) {
            fprintf(
                stderr,
                "[AntinoteWebDAVSync.app] Permission denied reading %s (errno=%d). Grant access to AntinoteWebDAVSync.app in System Settings > Privacy & Security > Full Disk Access.\n",
                src_db,
                perm_err
            );
            fflush(stderr);
        } else {
            int audit_interval = read_json_int(cfg_path, "audit_interval_seconds", 300, 10, 86400);
            time_t now = time(NULL);
            if (first_run || h != last_hash || (now - last_audit >= audit_interval)) {
                if (copy_file_c(src_db, mirror_db) == 0) {
                    copy_file_c(src_wal, mirror_wal);
                    copy_file_c(src_shm, mirror_shm);
                    run_python_sync(py_script, mirror_db, first_run);
                    last_hash = h;
                    if (first_run || (now - last_audit >= audit_interval)) {
                        last_audit = now;
                    }
                    first_run = 0;
                }
            }
        }

        int sleep_sec = read_json_int(cfg_path, "poll_interval_seconds", 2, 1, 3600);
        for (int i = 0; i < sleep_sec && keep_running; i++) {
            sleep(1);
        }
    }
    return 0;
}
