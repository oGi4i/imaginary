"""Offline contract tests for the loader probe using an authored libvips stub."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tooling/runtime-image/loader_probe.c"

HEADER = r'''
#ifndef STUB_VIPS_H
#define STUB_VIPS_H
#include <stddef.h>
typedef unsigned long GType;
typedef unsigned int GLogLevelFlags;
typedef void *gpointer;
typedef struct { const char *name; } VipsObjectClass;
typedef void (*GLogFunc)(const char *, GLogLevelFlags, const char *, gpointer);
typedef void *(*VipsClassMapFn)(VipsObjectClass *, void *);
#define VIPS_TYPE_FOREIGN_LOAD 1
#define VIPS_TYPE_FOREIGN_SAVE 2
#define G_TYPE_FROM_CLASS(c) ((GType)(c))
#define G_LOG_LEVEL_ERROR (1u << 2)
#define G_LOG_LEVEL_CRITICAL (1u << 3)
#define G_LOG_LEVEL_WARNING (1u << 4)
const char *g_type_name(GType type);
GLogFunc g_log_set_default_handler(GLogFunc handler, gpointer data);
unsigned int g_log_set_handler(const char *domain, GLogLevelFlags levels, GLogFunc handler, gpointer data);
int vips_init(const char *name);
int vips_version(int flag);
void *vips_class_map_all(GType type, VipsClassMapFn fn, void *data);
const char *vips_error_buffer(void);
void vips_shutdown(void);
#endif
'''

STUB = r'''
#include <vips/vips.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static GLogFunc handler;
static const char *scenario(void) { const char *s = getenv("SCENARIO"); return s ? s : ""; }
static int is(const char *s) { return strcmp(scenario(), s) == 0; }
static void warning(void) { if (handler) handler("VIPS", G_LOG_LEVEL_WARNING, "private message", NULL); }
GLogFunc g_log_set_default_handler(GLogFunc h, gpointer d) { (void)d; handler = h; return NULL; }
unsigned int g_log_set_handler(const char *domain, GLogLevelFlags levels, GLogFunc h, gpointer d) {
    (void)domain; (void)levels; (void)d; handler = h; return 1;
}
const char *g_type_name(GType t) { return ((VipsObjectClass *)t)->name; }
int vips_init(const char *name) {
    if (strcmp(name, "/usr/local/bin/imaginary")) return -1;
    if (is("init-warning")) warning();
    if (is("stderr-warning")) { fputs("private warning", stderr); fflush(stderr); }
    if (is("stderr-flood")) { for (int i = 0; i < 100000; i++) fputc('x', stderr); fflush(stderr); }
    if (is("stdout-noise")) { fputs("private output", stdout); fflush(stdout); }
    if (is("init-fail")) { fputs("private error", stderr); return -1; }
    return 0;
}
int vips_version(int i) {
    static const int v[] = {8, 10, 0};
    if (is("version-negative")) return -1;
    if (is("version-overflow")) return 10000;
    if (is("version")) return 9;
    return v[i];
}
void *vips_class_map_all(GType type, VipsClassMapFn fn, void *data) {
    VipsObjectClass c;
    char name[160];
    const char *prefix = type == VIPS_TYPE_FOREIGN_LOAD ? "VipsForeignLoad" : "VipsForeignSave";
    if (is("map-warning")) warning();
    if (is("empty")) return NULL;
    if (is("overflow") || is("limit")) {
        for (int i = 0; i < (is("limit") ? 1024 : 1025); i++) { snprintf(name, sizeof name, "%s%04d", prefix, i); c.name = name; if (fn(&c, data)) break; }
        return NULL;
    }
    if (is("long") || is("name-limit")) { int n = is("long") ? 129 : 128; memset(name, 'X', n); name[n] = 0; memcpy(name, prefix, strlen(prefix)); c.name = name; fn(&c, data); return NULL; }
    if (is("invalid")) { c.name = "VipsForeignLoadBad\nline"; fn(&c, data); return NULL; }
    if (is("punctuation")) { c.name = "VipsForeignLoadBad!"; fn(&c, data); return NULL; }
    if (is("wrong-prefix")) { c.name = "OtherClass"; fn(&c, data); return NULL; }
    snprintf(name, sizeof name, "%sZ", prefix); c.name = name; fn(&c, data);
    snprintf(name, sizeof name, "%sA", prefix); c.name = name; fn(&c, data);
    if (is("duplicate")) fn(&c, data);
    return NULL;
}
const char *vips_error_buffer(void) { return is("error-buffer") ? "private error" : ""; }
void vips_shutdown(void) { if (is("shutdown-warning")) warning(); }
'''


class LoaderProbeSourceTests(unittest.TestCase):
    def test_source_uses_runtime_version_and_class_map(self):
        source = SOURCE.read_text()
        for text in ("vips_version(0)", "vips_version(1)", "vips_version(2)",
                     "VIPS_TYPE_FOREIGN_LOAD", "VIPS_TYPE_FOREIGN_SAVE"):
            self.assertIn(text, source)


@unittest.skipUnless(os.environ.get("GITHUB_ACTIONS") == "true" and
                     os.environ.get("LOADER_PROBE_STUB_TESTS") == "1",
                     "stub compilation requires explicit hosted CI opt-in")
class LoaderProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        directory = Path(cls.temp.name)
        (directory / "vips").mkdir()
        (directory / "vips/vips.h").write_text(HEADER)
        (directory / "stub.c").write_text(STUB)
        cls.probe = directory / "probe"
        subprocess.run(["cc", "-std=c99", "-Wall", "-Wextra", "-Werror", "-I", str(directory),
                        str(SOURCE), str(directory / "stub.c"), "-o", str(cls.probe)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_probe(self, scenario="", *args):
        return subprocess.run([str(self.probe), *args], env={**os.environ, "SCENARIO": scenario},
                              capture_output=True, timeout=5)

    def test_sorted_unique_protocol(self):
        result = self.run_probe()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"VIPS_PROBE 1\nVERSION 8.10.0\nLOAD VipsForeignLoadA\n"
                         b"LOAD VipsForeignLoadZ\nSAVE VipsForeignSaveA\nSAVE VipsForeignSaveZ\nEND\n")
        self.assertEqual(result.stderr, b"")

    def test_runtime_version_is_reported(self):
        result = self.run_probe("version")
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"VERSION 9.9.9\n", result.stdout)

    def test_library_stdout_is_not_exposed(self):
        result = self.run_probe("stdout-noise")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith(b"VIPS_PROBE 1\n"))
        self.assertNotIn(b"private", result.stdout)

    def test_class_and_name_limits_are_inclusive(self):
        result = self.run_probe("limit")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout.splitlines()), 2051)
        result = self.run_probe("name-limit")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout.splitlines()[2][5:]), 128)

    def test_empty_registry(self):
        result = self.run_probe("empty")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"VIPS_PROBE 1\nVERSION 8.10.0\nEND\n")

    def test_failures_emit_no_private_output(self):
        for scenario in ("init-warning", "stderr-warning", "stderr-flood", "init-fail", "version-negative", "version-overflow", "map-warning", "shutdown-warning",
                         "error-buffer", "overflow", "long", "invalid", "punctuation", "wrong-prefix", "duplicate"):
            with self.subTest(scenario=scenario):
                result = self.run_probe(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"")

    def test_arguments_are_rejected(self):
        result = self.run_probe("", "unexpected")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((result.stdout, result.stderr), (b"", b""))


if __name__ == "__main__":
    unittest.main()
