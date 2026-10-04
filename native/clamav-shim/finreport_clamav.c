#include "finreport_clamav.h"

/* The ClamAV header location is supplied by the native build environment. */
#include <clamav.h>
#include <stdlib.h>

struct fr_clamav_engine {
    struct cl_engine *engine;
};

int fr_clamav_engine_create(const char *database_dir, fr_clamav_engine **out_engine) {
    unsigned int signature_count = 0;
    struct cl_engine *engine;
    int rc;
    if (!database_dir || !out_engine) return CL_EARG;
    rc = cl_init(CL_INIT_DEFAULT);
    if (rc != CL_SUCCESS) return rc;
    engine = cl_engine_new();
    if (!engine) return CL_EMEM;
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
