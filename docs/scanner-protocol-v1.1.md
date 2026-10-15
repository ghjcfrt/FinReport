# FinReport Scanner Protocol v1.1

This document freezes the scanner boundary. It is an implementation contract, not a
claim that a ClamAV runtime is present on every development machine.

The Python `clamav` mode is only a local fallback for hosts that explicitly configure
an absolute `clamscan` executable. Production mode is `supervisor` and fails closed when
the Rust Supervisor control pipe is unavailable. Tests may use `mock`; that mode must
never be enabled in a deployment configuration.

## Boundary

`WPF -> FastAPI -> Control Pipe -> Rust Supervisor -> Worker Private Pipe -> libclamav.dll`.
The WPF client never connects to a pipe or reads quarantine/trusted-object paths.

## File flow

```text
incoming/<id>.uploading -> incoming/<id>.ready -> managed/<id>
  -> scanning -> promoting -> trusted_objects/<sha256>.bin -> clean
```

Only the Scanner may claim `.ready` files. The Scanner recomputes size and SHA-256 after
claiming. `original_filename` is audit metadata only and never participates in a path.

## Control operations

The FastAPI service account may send `register_and_enqueue`, `get_scan_status`, and
`get_health`. Messages use a 4-byte big-endian length prefix followed by UTF-8 JSON,
with a 64 KiB maximum and strict unknown-field rejection.

`manifest.db` is the durable state truth. The bounded FIFO is only an execution mechanism;
a full queue never makes registration fail after manifest persistence.

The `mock` scanner mode is test-only and is fail-closed by default. It may promote a file
only when `FINREPORT_ALLOW_MOCK_SCANNER=true` is explicitly set; otherwise registration
returns `UNAVAILABLE` with error code `MOCK_SCANNER_DISABLED`.

## Recovery invariants

- An orphan managed file is `reconciliation_required`, never silently requeued or deleted.
- A trusted object without a manifest row in `promoting` is also `reconciliation_required`.
- `clean` is committed only after the object and audit record are durable; managed deletion
  happens afterwards and is idempotent.
- Request idempotency is persisted by `(caller_identity, request_id)` plus payload hash,
  protocol version, operation, and response JSON.
- A Worker loads one immutable `database_id` for its lifetime. Native timeout means killing
  the Worker process, not cancelling a Rust future around a C call.

The C shim is intentionally small. It owns the ClamAV pointers and exposes engine
creation, database-age checking, file scanning, cleanup, error text, and version to the
Rust Worker.

## Database delivery

A production database is an immutable directory selected by a `database_id`. The
database update operation is separate from file scanning and is never executed inside
the C shim. The update flow is:

```text
freshclam -> new staging/version directory -> candidate Worker load and compile
  -> candidate READY -> current.json pointer swap -> new work uses candidate
  -> old Worker drains or exits
```

Running Worker directories must never be overwritten. `current.json` activation alone
does not switch an already running Worker; the Supervisor must create and validate a
candidate Worker and update its dispatch target. An initialization or compilation
failure must preserve the current Worker and its current database version.

The locally available `tmp/clamav-experiment-db/clamav.hdb` contains one test signature.
It proves verdict mapping only and is not an acceptable production malware database.
On 2026-10-05 the official ClamAV 1.5.4 Windows x64 package was downloaded from the
Cisco-Talos release URL and its published SHA-256 was checked. Its `freshclam.exe`
downloaded `main.cvd` v63, `daily.cvd` v28144 and `bytecode.cvd` v339. `sigtool --verify`
validated all three detached `.sign` files as `ClamAV_datafiles_release`. A separate
GCC shim linked against the official `libclamav.dll` loaded this database and returned
clean for the clean fixture and `Eicar-Signature` for the EICAR fixture. The existing
MinGW build still uses `ENABLE_LIBCLAMAV_ONLY=ON` and `BYTECODE_RUNTIME=none`; it remains
an experimental test runtime and must not be mixed with the official MSVC runtime.

`tools/update-clamav-database.ps1` creates a unique version directory, rejects empty
database files, records file sizes and SHA-256 digests, verifies official CVD signatures
with `-RequireOfficialDatabase -SigtoolPath -SignatureCerts`, and validates the candidate
with the native probe before replacing `current.json`. The script rejects `-Activate`
unless both official signature verification and a complete native probe are supplied.
The Supervisor watches the immutable pointer, loads a READY candidate Worker before
switching dispatch, and retains the existing Worker when candidate initialization fails.

## Acceptance Evidence

The implementation status must be reported separately from this frozen contract.
The following are mandatory for claiming that the built-in Scanner satisfies v1.1:

- A real API upload of a clean PDF reaches `clean` through the production control pipe;
  an infected PDF never creates a trusted object or a parseable filing.
- Scanner absence, initialization failure, invalid response, hash/size mismatch, native
  timeout, Worker crash, and unavailable or stale database all fail closed and retain
  sufficient durable metadata for diagnosis or reconciliation.
- Native timeout terminates the Worker process; a fresh Worker becomes READY before
  accepting another scan.
- Queue pressure does not lose durable registrations. Request replay returns the
  persisted response; reuse of an ID with a different payload is rejected.
- Restart tests cover orphan managed files, orphan trusted objects, an interrupted
  promoting transaction, and managed cleanup after durable clean commit.
- Update tests prove immutable Worker database IDs, candidate load/compile validation,
  successful activation, and preservation of the old Worker when candidate validation
  fails.
- Deployment tests run with the bundled runtime and official database without relying
  on `tmp` or an unrelated machine-wide ClamAV installation. They preserve matching
  ABI/architecture, safe DLL loading, certificates, ACLs, and license notices.

The broader product acceptance items in `word/可实施技术设计V2.md` section 11 remain
separate gates: at least five issuers per enabled module with annual, half-year and
complex/negative samples; double-reviewed gold JSON; complete durable audit chains;
exact strict-fact and independent metric recomputation; positive/negative validation
tests; sector constraints; API-bypass tests; frozen comparable peer pools with at least
ten audited issuers and 80 percent coverage; registered-model invocation and complete
traces; rejection of invalid claims; reproducible blind evaluation by independent
reviewers; and the WPF upload-to-export end-to-end demo. A Scanner probe passing does
not satisfy these broader requirements.

## Implementation status (2026-10-06 audit)

| Area | Status | Evidence or gap |
|---|---|---|
| ClamAV 1.5.4 MinGW `libclamav` and C shim | Verified experimentally | Rust native-probe clean/infected verdict mapping works with the bundled test HDB; this build has no Bytecode runtime. |
| Official ClamAV Windows runtime and GCC shim | Verified in production path | Official package SHA-256 checked; GCC shim loads the signed main/daily/bytecode set; clean and EICAR probes pass; the same runtime is used by the Supervisor Worker. |
| Python Supervisor adapter | Verified in production path | The adapter writes `.ready`, uses the v1.1 Named Pipe, polls durable scan status, verifies hash/object identity, and fails closed on pipe, timeout, protocol, or verdict errors. |
| Official signed database delivery | Verified in production path | `freshclam` downloaded main v63, daily v28144 and bytecode v339; all detached signatures verify; the bundle launcher rejects missing, experimental, or corrupt database manifests. |
| Candidate staging helper | Verified with Supervisor activation | `tools/update-clamav-database.ps1` stages immutable versions, hashes files, verifies official signatures when requested, probes, and swaps `current.json`; the bundle harness also verifies candidate Worker activation and preservation of the old Worker after candidate validation failure. |
| Production Control Pipe / Worker lifecycle | Verified by Windows integration harness | Official bundle run covered health/READY, framing and unknown-field rejection, request replay, missing assets, startup and in-process stale-database rejection, timeout/crash rebuild, clean promotion, infected blocking, hash/size mismatch, candidate Worker activation, failed candidate preservation of the old Worker, restart recovery, orphan reconciliation, queue pressure, trusted-object tamper blocking, API upload/parse gate, and Supervisor-stop fail-closed: 21/21 checks passed. A synthetic-HDB run additionally covered the core lifecycle with deterministic threat mapping and queue stress above 100 items. |
| Word section 11 product gates | Separate and incomplete | Dataset, gold JSON, full audit, metric, sector, model, blind-evaluation, and WPF end-to-end gates remain independent of ClamAV probe success. |

Accordingly, the repository may claim an integrated ClamAV admission path for the
bundled Windows deployment: the official runtime and signed database are staged,
the Supervisor owns the Worker and Named Pipe, the API cannot create or retry a parse
job without a clean trusted object, and the launcher refuses to expose the API until
the official database manifest and Supervisor READY check pass. Candidate database
activation and failed-candidate preservation are covered by the 21-check bundle
harness; freshclam scheduling and deployment operations remain separate, and the
broader Word section 11 product gates remain independent acceptance work.
