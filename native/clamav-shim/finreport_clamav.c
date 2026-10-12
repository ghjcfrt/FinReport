#include "finreport_clamav.h"

/* The ClamAV header location is supplied by the native build environment. */
#include <clamav.h>
#include <stdint.h>
#include <stdlib.h>

#if defined(_WIN32)
#include <windows.h>
#endif

struct fr_clamav_engine {
    struct cl_engine *engine;
};

int fr_clamav_database_age(const char *database_dir, uint64_t *age_seconds) {
    time_t age = 0;
    cl_error_t rc;
    if (!database_dir || !age_seconds) return CL_EARG;
    rc = cl_cvdgetage(database_dir, &age);
    if (rc != CL_SUCCESS) return rc;
    if (age < 0) return CL_EFORMAT;
    *age_seconds = (uint64_t)age;
    return CL_SUCCESS;
}

/*
 * Rust sets CVD_CERTS_DIR immediately before loading this DLL.  On Windows,
 * that update is made through the process environment, while the MinGW CRT's
 * getenv() view can be stale.  Read the OS environment directly so the
 * explicit engine setting is reliable for both inherited and runtime values.
 */
static const char *fr_cvd_certs_dir(char *buffer, size_t buffer_size) {
#if defined(_WIN32)
    DWORD length = GetEnvironmentVariableA("CVD_CERTS_DIR", buffer, (DWORD)buffer_size);
    if (length == 0 || length >= buffer_size || buffer[0] == '\0') return NULL;
    return buffer;
#else
    const char *value = getenv("CVD_CERTS_DIR");
    if (!value || value[0] == '\0') return NULL;
    (void)buffer;
    (void)buffer_size;
    return value;
#endif
}

/* An empty CVD_CERTS_DIR is different from an unset value to ClamAV and
 * causes cl_engine_new() to retain an unusable empty certificate path. */
static void fr_clear_empty_cvd_certs_env(void) {
#if defined(_WIN32)
    char buffer[2];
    /* GetEnvironmentVariableA returns zero for both an empty and a missing
     * variable; clearing in either case is harmless and normalizes the empty
     * value before cl_engine_new() computes its platform default. */
    buffer[0] = '\0';
    if (GetEnvironmentVariableA("CVD_CERTS_DIR", buffer, sizeof(buffer)) == 0 ||
        buffer[0] == '\0') {
        /* Keep the MinGW CRT view in sync with the Windows process block;
         * ClamAV itself calls CRT getenv() during cl_engine_new(). */
        _putenv("CVD_CERTS_DIR=");
        SetEnvironmentVariableA("CVD_CERTS_DIR", NULL);
    }
#else
    const char *value = getenv("CVD_CERTS_DIR");
    if (value && value[0] == '\0') unsetenv("CVD_CERTS_DIR");
#endif
}

int fr_clamav_engine_create(const char *database_dir, fr_clamav_engine **out_engine) {
    unsigned int signature_count = 0;
    struct cl_engine *engine;
    int rc;
    if (!database_dir || !out_engine) return CL_EARG;
    rc = cl_init(CL_INIT_DEFAULT);
    if (rc != CL_SUCCESS) return rc;
    fr_clear_empty_cvd_certs_env();
    engine = cl_engine_new();
    if (!engine) return CL_EMEM;

    /*
     * cl_engine_new() reads CVD_CERTS_DIR for its initial default, but keep
     * the certificate location explicit on the engine before loading any
     * database.  An unset or empty variable leaves ClamAV's platform default
     * intact; passing NULL to cl_engine_set_str() is not supported.
     */
    {
        char certs_buffer[32768];
        const char *certs_dir = fr_cvd_certs_dir(certs_buffer, sizeof(certs_buffer));
        if (certs_dir && certs_dir[0] != '\0') {
            rc = cl_engine_set_str(engine, CL_ENGINE_CVDCERTSDIR, certs_dir);
            if (rc != CL_SUCCESS) {
                cl_engine_free(engine);
                return rc;
            }
        }
    }
    rc = cl_load(database_dir, engine, &signature_count, CL_DB_STDOPT);
    if (rc == CL_SUCCESS) rc = cl_engine_compile(engine);
    if (rc != CL_SUCCESS) {
        cl_engine_free(engine);
        return rc;
    }
    *out_engine = (fr_clamav_engine *)calloc(1, sizeof(**out_engine));
    if (!*out_engine) {
        cl_engine_free(engine);
        return CL_EMEM;
    }
    (*out_engine)->engine = engine;
    return CL_SUCCESS;
}

int fr_clamav_engine_scan_file(fr_clamav_engine *wrapper, const char *path, fr_clamav_scan_result *out_result) {
    cl_verdict_t verdict = CL_VERDICT_NOTHING_FOUND;
    const char *virus = NULL;
    struct cl_scan_options options = {0};
    int rc;
    if (!wrapper || !wrapper->engine || !path || !out_result) return CL_EARG;
    options.general = CL_SCAN_GENERAL_HEURISTICS;
    options.parse = CL_SCAN_PARSE_ARCHIVE | CL_SCAN_PARSE_ELF | CL_SCAN_PARSE_PDF |
                    CL_SCAN_PARSE_SWF | CL_SCAN_PARSE_HWP3 | CL_SCAN_PARSE_XMLDOCS |
                    CL_SCAN_PARSE_MAIL | CL_SCAN_PARSE_OLE2 | CL_SCAN_PARSE_HTML |
                    CL_SCAN_PARSE_PE | CL_SCAN_PARSE_ONENOTE | CL_SCAN_PARSE_IMAGE;
    options.heuristic = CL_SCAN_HEURISTIC_BROKEN | CL_SCAN_HEURISTIC_EXCEEDS_MAX |
                        CL_SCAN_HEURISTIC_PHISHING_SSL_MISMATCH |
                        CL_SCAN_HEURISTIC_PHISHING_CLOAK | CL_SCAN_HEURISTIC_MACROS |
                        CL_SCAN_HEURISTIC_ENCRYPTED_ARCHIVE |
                        CL_SCAN_HEURISTIC_ENCRYPTED_DOC | CL_SCAN_HEURISTIC_BROKEN_MEDIA;
    rc = cl_scanfile_ex(path, &verdict, &virus, NULL, wrapper->engine, &options,
                        NULL, NULL, NULL, NULL, NULL, NULL);
    /* CL_VIRUS is a positive scan result, not an engine error. */
    out_result->status = rc == CL_SUCCESS || rc == CL_VIRUS
                             ? (verdict == CL_VERDICT_NOTHING_FOUND || verdict == CL_VERDICT_TRUSTED ? 0 : 1)
                             : -rc;
    out_result->threat_name = virus;
    return rc;
}

void fr_clamav_engine_free(fr_clamav_engine *wrapper) {
    if (!wrapper) return;
    if (wrapper->engine) cl_engine_free(wrapper->engine);
    free(wrapper);
}

const char *fr_clamav_error_string(int code) { return cl_strerror(code); }
const char *fr_clamav_version(void) { return cl_retver(); }
