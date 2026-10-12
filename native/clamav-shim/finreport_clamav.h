#pragma once

#include <stdint.h>

#ifdef _WIN32
#define FR_CLAMAV_API __declspec(dllexport)
#else
#define FR_CLAMAV_API
#endif

typedef struct fr_clamav_engine fr_clamav_engine;

typedef struct fr_clamav_scan_result {
    int status; /* 0 clean, 1 infected, negative error */
    const char *threat_name; /* owned by the engine until the next scan */
} fr_clamav_scan_result;

FR_CLAMAV_API int fr_clamav_engine_create(const char *database_dir, fr_clamav_engine **out_engine);
FR_CLAMAV_API int fr_clamav_database_age(const char *database_dir, uint64_t *age_seconds);
FR_CLAMAV_API int fr_clamav_engine_scan_file(fr_clamav_engine *engine, const char *path, fr_clamav_scan_result *out_result);
FR_CLAMAV_API void fr_clamav_engine_free(fr_clamav_engine *engine);
FR_CLAMAV_API const char *fr_clamav_error_string(int code);
FR_CLAMAV_API const char *fr_clamav_version(void);
