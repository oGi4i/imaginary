#include <vips/vips.h>

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define MAX_CLASSES 1024
#define MAX_NAME 128
#define OUTPUT_SIZE (64 + 2 * MAX_CLASSES * (MAX_NAME + 6))

enum {
    EXIT_SETUP = 41,
    EXIT_INIT = 42,
    EXIT_VERSION = 43,
    EXIT_CLASSES = 44,
    EXIT_LIBRARY = 45,
    EXIT_STDERR = 46,
    EXIT_PROTOCOL = 47,
    EXIT_OUTPUT = 48
};

typedef struct {
    char names[MAX_CLASSES][MAX_NAME + 1];
    size_t count;
    const char *prefix;
    int failed;
} Classes;

static Classes loads = { .prefix = "VipsForeignLoad" };
static Classes saves = { .prefix = "VipsForeignSave" };
static char output[OUTPUT_SIZE];
static int log_failed;

static void
capture_log(const char *domain, GLogLevelFlags level, const char *message,
    gpointer data)
{
    (void) domain;
    (void) message;
    (void) data;
    if (level & (G_LOG_LEVEL_WARNING | G_LOG_LEVEL_CRITICAL | G_LOG_LEVEL_ERROR))
        log_failed = 1;
}

static void *
collect_class(VipsObjectClass *cls, void *data)
{
    Classes *classes = data;
    const char *name = g_type_name(G_TYPE_FROM_CLASS(cls));
    size_t length = 0;
    size_t prefix_length = strlen(classes->prefix);

    if (!name) {
        classes->failed = 1;
        return classes;
    }
    while (length <= MAX_NAME && name[length]) {
        unsigned char c = (unsigned char) name[length];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            (length > 0 && ((c >= '0' && c <= '9') || c == '_')))) {
            classes->failed = 1;
            return classes;
        }
        length++;
    }
    if (length > MAX_NAME || length <= prefix_length ||
        strncmp(name, classes->prefix, prefix_length) != 0 ||
        classes->count == MAX_CLASSES) {
        classes->failed = 1;
        return classes;
    }
    memcpy(classes->names[classes->count++], name, length + 1);
    return NULL;
}

static int
compare_names(const void *a, const void *b)
{
    return strcmp(a, b);
}

static int
append(const char *text, size_t *used)
{
    size_t length = strlen(text);
    if (length > sizeof output - *used)
        return -1;
    memcpy(output + *used, text, length);
    *used += length;
    return 0;
}

static int
append_classes(Classes *classes, const char *tag, size_t *used)
{
    size_t i;
    qsort(classes->names, classes->count, sizeof classes->names[0], compare_names);
    for (i = 0; i < classes->count; i++) {
        if (i && strcmp(classes->names[i - 1], classes->names[i]) == 0)
            return -1;
        if (append(tag, used) || append(classes->names[i], used) || append("\n", used))
            return -1;
    }
    return 0;
}

int
main(int argc, char **argv)
{
    int failed = 0;
    int stderr_failed = 0;
    int result_fd;
    int null_fd;
    int error_pipe[2];
    char error_byte;
    char version[40];
    int major;
    int minor;
    int patch;
    size_t used = 0;
    (void) argv;

    if (argc != 1)
        return EXIT_SETUP;
    result_fd = dup(STDOUT_FILENO);
    null_fd = open("/dev/null", O_WRONLY);
    if (result_fd < 0 || null_fd < 0 || pipe(error_pipe) < 0 ||
        fcntl(error_pipe[1], F_SETFL, O_NONBLOCK) < 0 ||
        dup2(null_fd, STDOUT_FILENO) < 0 || dup2(error_pipe[1], STDERR_FILENO) < 0)
        return EXIT_SETUP;
    close(error_pipe[1]);
    close(null_fd);
    g_log_set_default_handler(capture_log, NULL);
    g_log_set_handler("VIPS", G_LOG_LEVEL_WARNING | G_LOG_LEVEL_CRITICAL |
        G_LOG_LEVEL_ERROR, capture_log, NULL);
    g_log_set_handler("GLib-GObject", G_LOG_LEVEL_WARNING | G_LOG_LEVEL_CRITICAL |
        G_LOG_LEVEL_ERROR, capture_log, NULL);
    if (vips_init("/usr/local/bin/imaginary") != 0)
        return EXIT_INIT;

    major = vips_version(0);
    minor = vips_version(1);
    patch = vips_version(2);
    if (major < 0 || major > 9999 || minor < 0 || minor > 9999 ||
        patch < 0 || patch > 9999 ||
        snprintf(version, sizeof version, "VERSION %d.%d.%d\n", major, minor, patch) < 0)
        failed = EXIT_VERSION;
    if (!failed) {
        vips_class_map_all(VIPS_TYPE_FOREIGN_LOAD, collect_class, &loads);
        vips_class_map_all(VIPS_TYPE_FOREIGN_SAVE, collect_class, &saves);
    }
    if (!failed && (loads.failed || saves.failed))
        failed = EXIT_CLASSES;
    if (loads.failed || saves.failed || log_failed || *vips_error_buffer()) {
        if (!failed)
            failed = EXIT_LIBRARY;
    }
    vips_shutdown();
    if (fflush(stderr) != 0)
        stderr_failed = 1;
    close(STDERR_FILENO);
    if (read(error_pipe[0], &error_byte, 1) != 0)
        stderr_failed = 1;
    close(error_pipe[0]);
    if (failed)
        return failed;
    if (log_failed)
        return EXIT_LIBRARY;
    if (stderr_failed)
        return EXIT_STDERR;

    if (append("VIPS_PROBE 1\n", &used) || append(version, &used) ||
        append_classes(&loads, "LOAD ", &used) ||
        append_classes(&saves, "SAVE ", &used) ||
        append("END\n", &used))
        return EXIT_PROTOCOL;
    if (fflush(stdout) != 0 || dup2(result_fd, STDOUT_FILENO) < 0)
        return EXIT_OUTPUT;
    close(result_fd);
    if (fwrite(output, 1, used, stdout) != used || fflush(stdout) != 0)
        return EXIT_OUTPUT;
    return 0;
}
