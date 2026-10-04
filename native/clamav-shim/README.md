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
It does not provide `clamscan.exe` or `freshclam.exe`; the existing Python
`FINREPORT_AV_MODE=clamav` path therefore cannot use it. The Rust Supervisor
must load this shim from its trusted absolute directory before it becomes part
of the end-to-end scanner flow.

The exported operations are:

```text
fr_clamav_engine_create
fr_clamav_engine_scan_file
fr_clamav_engine_free
fr_clamav_error_string
```

The Rust Supervisor currently has an experimental `native-probe` command which
loads these exports directly. Example:

```powershell
finreport-scanner-supervisor native-probe `
  --shim D:\code\FinReport\tmp\clamav-experiment\finreport_clamav.dll `
  --database D:\code\FinReport\tmp\clamav-experiment-db `
  --file D:\code\FinReport\tmp\clamav-experiment-test.bin `
  --certs D:\code\FinReport\tmp\clamav-experiment\certs
```

The probe is deliberately not the production Supervisor protocol yet; it only
proves the Rust-to-shim ABI and scan result mapping.

`freshclam.exe` updates a candidate database directory outside the shim. A
candidate Worker loads and compiles that directory before Supervisor activation.
