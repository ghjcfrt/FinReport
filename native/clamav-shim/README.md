# FinReport ClamAV Shim

This directory contains the deliberately small native boundary used by the Rust
Worker. It is not built by the Python workspace.

The build must provide a matching Windows architecture for `clamav.h`, an import
library, `libclamav.dll`, and all dependent DLLs. The resulting shim DLL
must be loaded from an absolute, trusted installation directory. The Worker owns
the engine lifetime; the shim never performs database updates.

## Experimental MinGW Build

For the locally built ClamAV 1.5.4 runtime, run:

```powershell
.\native\clamav-shim\build-mingw.ps1
```

It consumes `tmp\clamav-install-154` and `tmp\vcpkg`, then emits a complete
experimental DLL directory at `tmp\clamav-experiment`. This build uses the
ClamAV 1.5 scan API and is ABI-compatible only with its bundled MinGW runtime.
It does not provide `clamscan.exe` or `freshclam.exe`; the Python
`FINREPORT_AV_MODE=clamav` fallback therefore cannot use it. This MinGW build is
experimental and is not the official runtime packaged by the production launcher.

The exported operations are:

```text
fr_clamav_engine_create
fr_clamav_database_age
fr_clamav_engine_scan_file
fr_clamav_engine_free
fr_clamav_error_string
fr_clamav_version
```

The Rust Supervisor has a `native-probe` diagnostic command which loads these
exports directly. The production path uses the Supervisor control pipe and Worker
with the separately packaged official Windows runtime. Example:

```powershell
finreport-scanner-supervisor native-probe `
  --shim D:\code\FinReport\tmp\clamav-experiment\finreport_clamav.dll `
  --database D:\code\FinReport\tmp\clamav-experiment-db `
  --file D:\code\FinReport\tmp\clamav-experiment-test.bin `
  --certs D:\code\FinReport\tmp\clamav-experiment\certs
```

The probe exercises the Rust-to-shim ABI and scan result mapping; it does not
exercise the production control-pipe, queue, or Worker lifecycle.

`fr_clamav_database_age` wraps ClamAV's `cl_cvdgetage` and reports the age of the
youngest database file in seconds. The Supervisor requires this export, rejects a
database older than `FINREPORT_AV_MAX_DATABASE_AGE_SECONDS` (default 604800, or
7 days), and checks the age again before every scan. A missing age export,
unreadable age, invalid threshold, or stale result keeps the Worker out of READY
or fails the scan closed.

`freshclam.exe` updates a candidate database directory outside the shim. A
candidate Worker loads and compiles that directory before Supervisor activation.

## Database staging and activation

The repository includes `tools/update-clamav-database.ps1` for preparing a
versioned database root. It never modifies an existing version directory. A
candidate is copied or downloaded into `versions/<database_id>`, every database
file is checked for a non-zero length and recorded with a SHA-256 digest, and
the native probe validates the candidate before activation. With
`-Activate`, the script atomically replaces only `current.json`; the directory
used by a running Worker is left untouched.

Production activation is guarded: `-Activate` is rejected unless
`-RequireOfficialDatabase`, `-SigtoolPath`, `-SignatureCerts`, and all four
`-Probe*` arguments are present. This keeps a local HDB or an uncompiled candidate
from becoming the active database by omission.

Copy a locally prepared database:

```powershell
.\tools\update-clamav-database.ps1 `
  -DatabaseRoot D:\ProgramData\FinReport\clamav-db `
  -SourceDatabase D:\staging\clamav-database `
  -SigtoolPath D:\ProgramData\FinReport\clamav-updater\sigtool.exe `
  -SignatureCerts D:\ProgramData\FinReport\clamav-updater\certs `
  -RequireOfficialDatabase `
  -DatabaseId db-20261005-001 `
  -ProbeExecutable D:\ProgramData\FinReport\scanner\finreport-scanner-supervisor.exe `
  -ProbeShim D:\ProgramData\FinReport\clamav\finreport_clamav.dll `
  -ProbeFile D:\staging\clean-fixture.pdf `
  -ProbeCerts D:\ProgramData\FinReport\clamav\certs `
  -Activate
```

When a matching `freshclam.exe` is available, use it to update a new candidate
directory. The script passes `--datadir=<candidate>` and never points
`freshclam` at the active directory:

```powershell
.\tools\update-clamav-database.ps1 `
  -DatabaseRoot D:\ProgramData\FinReport\clamav-db `
  -FreshclamPath D:\ProgramData\FinReport\clamav\freshclam.exe `
  -FreshclamConfig D:\ProgramData\FinReport\clamav\freshclam.conf `
  -SigtoolPath D:\ProgramData\FinReport\clamav-updater\sigtool.exe `
  -SignatureCerts D:\ProgramData\FinReport\clamav-updater\certs `
  -RequireOfficialDatabase `
  -DatabaseId db-20261005-002 `
  -ProbeExecutable D:\ProgramData\FinReport\scanner\finreport-scanner-supervisor.exe `
  -ProbeShim D:\ProgramData\FinReport\clamav\finreport_clamav.dll `
  -ProbeFile D:\staging\clean-fixture.pdf `
  -ProbeCerts D:\ProgramData\FinReport\clamav\certs `
  -Activate
```

The local MinGW build does not contain `freshclam.exe` and was deliberately
configured with `ENABLE_LIBCLAMAV_ONLY=ON` and `BYTECODE_RUNTIME=none`. The
existing `tmp\clamav-experiment-db\clamav.hdb` is a test signature database,
not a production update source. The staging script therefore fails closed when
no source directory or freshclam executable is supplied, and rejects zero-byte
database placeholders.

For a production-compatible database and Bytecode-enabled engine, use the official
ClamAV 1.5.4 Windows x64 release. The package includes `freshclam.exe`, `sigtool.exe`,
the MSVC `libclamav.dll`, its matching dependencies and `certs\clamav.crt`. Keep that
runtime in its own directory. A GCC-built shim can link to the official DLL through a
generated import library; do not copy the official MSVC DLLs over the MinGW build or
mix their import libraries. `freshclam` and `sigtool` are update/verification tools,
not substitutes for the Supervisor Worker.

The 2026-10-05 verification used the Cisco-Talos release URL and checked ZIP SHA-256
`0d9e0228b2674137ea1a2853566c98a0278ad52ab2582c3d6dbd75373848c395`. `freshclam`
downloaded main v63, daily v28144 and bytecode v339. `sigtool --verify` accepted all
three detached signatures as `ClamAV_datafiles_release`, and the official-runtime GCC
shim returned a clean verdict for the clean fixture and `Eicar-Signature` for the
standard EICAR fixture. The generated database manifest records each file's size and
SHA-256 and the verification output.

A local MinGW freshclam build was attempted on 2026-10-05 with the installed CMake
4.4.3, MinGW-w64 14.2.0 and the existing x64-mingw-dynamic vcpkg tree. The
configure stage stopped at `Could NOT find CURL (missing: CURL_LIBRARY
CURL_INCLUDE_DIR)`; no new compiler or dependency was installed. The official
Windows package supplies the updater used by the verified staging run.

The runtime in `tmp\clamav-experiment` is a MinGW-w64 x64 build. It must be
deployed as a complete set with its matching `libclamav.dll`,
`libclammspack.dll`, OpenSSL, zlib, PCRE2, JSON-C, libxml2, iconv, bzip2 and
MinGW runtime DLLs. Do not mix these binaries with an MSVC ClamAV package or
its import libraries. The bundled `certs\clamav.crt` must travel with the
runtime. The ClamAV and dependency license notices are retained under the
build's `COPYING` directory; any redistributable package must preserve those
notices and comply with the applicable GPL/LGPL terms.
