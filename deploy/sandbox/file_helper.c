/* tiny-hermes-file-helper — the only door file tools may use.
 *
 * Design §5.2: `file.*` never trusts a checked path string. This helper opens
 * the data root once and resolves every descendant with
 * openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS), so
 * the check and the use are one syscall — a directory that flickers into a
 * symlink between them has no window to win. Absolute paths, "..", and empty
 * segments are refused before any syscall is made.
 *
 * Subcommands, all relative to --root:
 *   probe                      exit 0 when openat2 with these flags works
 *   list <rel> <offset> <n>    JSON entries in bytewise order
 *   read <rel> <max>           raw bytes to stdout; exit 3 when truncated
 *   write <rel> <max>          stdin to same-directory .tmp-<pid>, fsync,
 *                              rename over the target
 *
 * Exit codes: 0 ok, 1 refused input, 2 filesystem error, 3 truncated read,
 * 4 write over its limit, 5 openat2 unsupported.
 */

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/openat2.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define EXIT_REFUSED 1
#define EXIT_FS 2
#define EXIT_TRUNCATED 3
#define EXIT_OVER_LIMIT 4
#define EXIT_UNSUPPORTED 5

#define MAX_PATH_BYTES 4096
#define MAX_NAME_BYTES 255
#define IO_CHUNK 65536

static const uint64_t RESOLVE_FLAGS =
    RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS;

static int open_beneath(int dirfd, const char *path, uint64_t flags, uint64_t mode) {
    struct open_how how;
    memset(&how, 0, sizeof(how));
    how.flags = flags | O_CLOEXEC | O_NOFOLLOW;
    how.mode = mode;
    how.resolve = RESOLVE_FLAGS;
    long fd = syscall(SYS_openat2, dirfd, path, &how, sizeof(how));
    return (int)fd;
}

/* Relative, and every segment a real name: no "", ".", "..", no leading '/'.
 * argv cannot carry NUL, so NUL needs no check here. */
static int path_is_safe(const char *rel) {
    size_t length = strlen(rel);
    if (length == 0 || length >= MAX_PATH_BYTES || rel[0] == '/') {
        return 0;
    }
    const char *segment = rel;
    while (1) {
        const char *slash = strchr(segment, '/');
        size_t span = slash ? (size_t)(slash - segment) : strlen(segment);
        if (span == 0 || span > MAX_NAME_BYTES) {
            return 0;
        }
        if ((span == 1 && segment[0] == '.') ||
            (span == 2 && segment[0] == '.' && segment[1] == '.')) {
            return 0;
        }
        if (!slash) {
            return 1;
        }
        segment = slash + 1;
    }
}

static void json_escape(const char *raw) {
    for (const unsigned char *c = (const unsigned char *)raw; *c; c++) {
        if (*c == '"' || *c == '\\') {
            printf("\\%c", *c);
        } else if (*c < 0x20) {
            printf("\\u%04x", *c);
        } else {
            putchar(*c);
        }
    }
}

/* ---- probe ------------------------------------------------------------- */

static int run_probe(int root) {
    int fd = open_beneath(root, ".", O_RDONLY | O_DIRECTORY, 0);
    if (fd < 0) {
        return EXIT_UNSUPPORTED;
    }
    close(fd);
    return 0;
}

/* ---- list -------------------------------------------------------------- */

struct entry {
    char name[MAX_NAME_BYTES + 1];
    const char *type;
    long long size;
    int mode;
};

static int compare_names(const void *left, const void *right) {
    return strcmp(((const struct entry *)left)->name,
                  ((const struct entry *)right)->name);
}

static const char *type_name(mode_t mode) {
    if (S_ISREG(mode)) {
        return "file";
    }
    if (S_ISDIR(mode)) {
        return "directory";
    }
    if (S_ISLNK(mode)) {
        return "symlink";
    }
    return "other";
}

static int run_list(int root, const char *rel, long offset, long limit) {
    int fd = (strcmp(rel, ".") == 0)
                 ? open_beneath(root, ".", O_RDONLY | O_DIRECTORY, 0)
                 : open_beneath(root, rel, O_RDONLY | O_DIRECTORY, 0);
    if (fd < 0) {
        fprintf(stderr, "{\"error\":\"%s\"}\n", strerror(errno));
        return EXIT_FS;
    }
    DIR *dir = fdopendir(fd);
    if (!dir) {
        close(fd);
        return EXIT_FS;
    }

    struct entry *entries = NULL;
    size_t count = 0, capacity = 0;
    struct dirent *item;
    errno = 0;
    while ((item = readdir(dir)) != NULL) {
        if (strcmp(item->d_name, ".") == 0 || strcmp(item->d_name, "..") == 0) {
            continue;
        }
        if (count == capacity) {
            capacity = capacity ? capacity * 2 : 64;
            struct entry *grown = realloc(entries, capacity * sizeof(*entries));
            if (!grown) {
                free(entries);
                closedir(dir);
                return EXIT_FS;
            }
            entries = grown;
        }
        struct stat status;
        if (fstatat(dirfd(dir), item->d_name, &status, AT_SYMLINK_NOFOLLOW) != 0) {
            continue; /* deleted between readdir and stat: not an error */
        }
        struct entry *slot = &entries[count++];
        snprintf(slot->name, sizeof(slot->name), "%s", item->d_name);
        slot->type = type_name(status.st_mode);
        slot->size = S_ISREG(status.st_mode) ? (long long)status.st_size : 0;
        slot->mode = (int)(status.st_mode & 07777);
    }
    closedir(dir);

    qsort(entries, count, sizeof(*entries), compare_names);
    printf("{\"entries\":[");
    int printed = 0;
    for (size_t i = 0; i < count; i++) {
        if ((long)i < offset || printed >= limit) {
            continue;
        }
        printf("%s{\"path\":\"", printed ? "," : "");
        json_escape(entries[i].name);
        printf("\",\"type\":\"%s\",\"size\":%lld,\"mode\":%d}",
               entries[i].type, entries[i].size, entries[i].mode);
        printed++;
    }
    printf("],\"total\":%zu}\n", count);
    free(entries);
    return 0;
}

/* ---- read -------------------------------------------------------------- */

static int run_read(int root, const char *rel, long long max_bytes) {
    int fd = open_beneath(root, rel, O_RDONLY, 0);
    if (fd < 0) {
        fprintf(stderr, "{\"error\":\"%s\"}\n", strerror(errno));
        return EXIT_FS;
    }
    struct stat status;
    if (fstat(fd, &status) != 0 || !S_ISREG(status.st_mode)) {
        close(fd);
        fprintf(stderr, "{\"error\":\"not a regular file\"}\n");
        return EXIT_FS;
    }

    char buffer[IO_CHUNK];
    long long remaining = max_bytes;
    while (remaining > 0) {
        size_t ask = remaining < IO_CHUNK ? (size_t)remaining : IO_CHUNK;
        ssize_t got = read(fd, buffer, ask);
        if (got < 0) {
            close(fd);
            return EXIT_FS;
        }
        if (got == 0) {
            break;
        }
        if (fwrite(buffer, 1, (size_t)got, stdout) != (size_t)got) {
            close(fd);
            return EXIT_FS;
        }
        remaining -= got;
    }
    fflush(stdout);
    close(fd);
    if (status.st_size > max_bytes) {
        fprintf(stderr, "{\"truncated\":true,\"size\":%lld}\n",
                (long long)status.st_size);
        return EXIT_TRUNCATED;
    }
    return 0;
}

/* ---- write ------------------------------------------------------------- */

/* Open (creating as needed) every directory above the final segment, one
 * openat2 per segment so a symlink smuggled into the middle is refused at
 * exactly the segment it occupies. Returns the parent fd; stores the final
 * name in `leaf`. */
static int open_parent(int root, const char *rel, const char **leaf) {
    int current = dup(root);
    if (current < 0) {
        return -1;
    }
    const char *segment = rel;
    while (1) {
        const char *slash = strchr(segment, '/');
        if (!slash) {
            *leaf = segment;
            return current;
        }
        char name[MAX_NAME_BYTES + 1];
        size_t span = (size_t)(slash - segment);
        memcpy(name, segment, span);
        name[span] = '\0';

        int next = open_beneath(current, name, O_RDONLY | O_DIRECTORY, 0);
        if (next < 0 && errno == ENOENT) {
            if (mkdirat(current, name, 0755) != 0 && errno != EEXIST) {
                close(current);
                return -1;
            }
            next = open_beneath(current, name, O_RDONLY | O_DIRECTORY, 0);
        }
        close(current);
        if (next < 0) {
            return -1;
        }
        current = next;
        segment = slash + 1;
    }
}

static int run_write(int root, const char *rel, long long max_bytes) {
    const char *leaf = NULL;
    int parent = open_parent(root, rel, &leaf);
    if (parent < 0) {
        fprintf(stderr, "{\"error\":\"%s\"}\n", strerror(errno));
        return EXIT_FS;
    }

    char tmp_name[64];
    snprintf(tmp_name, sizeof(tmp_name), ".tmp-%d", (int)getpid());
    int tmp = open_beneath(parent, tmp_name, O_WRONLY | O_CREAT | O_EXCL, 0644);
    if (tmp < 0) {
        close(parent);
        fprintf(stderr, "{\"error\":\"%s\"}\n", strerror(errno));
        return EXIT_FS;
    }

    int failure = 0;
    long long received = 0;
    char buffer[IO_CHUNK];
    while (1) {
        ssize_t got = read(STDIN_FILENO, buffer, sizeof(buffer));
        if (got < 0) {
            failure = EXIT_FS;
            break;
        }
        if (got == 0) {
            break;
        }
        received += got;
        if (received > max_bytes) {
            failure = EXIT_OVER_LIMIT;
            fprintf(stderr, "{\"error\":\"write over limit\",\"limit\":%lld}\n",
                    max_bytes);
            break;
        }
        if (write(tmp, buffer, (size_t)got) != got) {
            failure = EXIT_FS;
            break;
        }
    }

    if (!failure && fsync(tmp) != 0) {
        failure = EXIT_FS;
    }
    close(tmp);
    if (!failure && renameat(parent, tmp_name, parent, leaf) != 0) {
        failure = EXIT_FS;
    }
    if (!failure && fsync(parent) != 0) {
        failure = EXIT_FS;
    }
    if (failure) {
        unlinkat(parent, tmp_name, 0); /* best effort; may already be renamed */
    }
    close(parent);
    return failure;
}

/* ---- entry point ------------------------------------------------------- */

static long long parse_positive(const char *raw) {
    char *end = NULL;
    long long value = strtoll(raw, &end, 10);
    if (end == raw || *end != '\0' || value < 0) {
        return -1;
    }
    return value;
}

int main(int argc, char **argv) {
    if (argc < 4 || strcmp(argv[1], "--root") != 0) {
        fprintf(stderr, "usage: %s --root <dir> <probe|list|read|write> ...\n",
                argv[0]);
        return EXIT_REFUSED;
    }

    int root = open(argv[2], O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (root < 0) {
        fprintf(stderr, "{\"error\":\"cannot open root: %s\"}\n", strerror(errno));
        return EXIT_FS;
    }
    const char *verb = argv[3];

    if (strcmp(verb, "probe") == 0) {
        return run_probe(root);
    }
    if (strcmp(verb, "list") == 0 && argc == 7) {
        const char *rel = argv[4];
        long long offset = parse_positive(argv[5]);
        long long limit = parse_positive(argv[6]);
        if ((strcmp(rel, ".") != 0 && !path_is_safe(rel)) || offset < 0 || limit <= 0) {
            return EXIT_REFUSED;
        }
        return run_list(root, rel, (long)offset, (long)limit);
    }
    if (strcmp(verb, "read") == 0 && argc == 6) {
        long long max_bytes = parse_positive(argv[5]);
        if (!path_is_safe(argv[4]) || max_bytes <= 0) {
            return EXIT_REFUSED;
        }
        return run_read(root, argv[4], max_bytes);
    }
    if (strcmp(verb, "write") == 0 && argc == 6) {
        long long max_bytes = parse_positive(argv[5]);
        if (!path_is_safe(argv[4]) || max_bytes <= 0) {
            return EXIT_REFUSED;
        }
        return run_write(root, argv[4], max_bytes);
    }
    fprintf(stderr, "unknown or malformed subcommand\n");
    return EXIT_REFUSED;
}
