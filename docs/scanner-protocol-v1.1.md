# FinReport Scanner Protocol v1.1

This document freezes the scanner boundary. It is an implementation contract, not a
claim that a ClamAV runtime is present on every development machine.

The Python `clamav` mode is only a local fallback for hosts that explicitly configure
an absolute `clamscan` executable. Production mode is `supervisor` and fails closed until
the Rust Supervisor control pipe is available. Tests may use `mock`; that mode must never
be enabled in a deployment configuration.

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

## Recovery invariants

- An orphan managed file is `reconciliation_required`, never silently requeued or deleted.
- A trusted object without a manifest row in `promoting` is also `reconciliation_required`.
- `clean` is committed only after the object and audit record are durable; managed deletion
  happens afterwards and is idempotent.
- Request idempotency is persisted by `(caller_identity, request_id)` plus payload hash,
  protocol version, operation, and response JSON.
- A Worker loads one immutable `database_id` for its lifetime. Native timeout means killing
  the Worker process, not cancelling a Rust future around a C call.

The C shim is intentionally small. It owns the ClamAV pointers and exposes only engine
create, file scan, free, and error-string functions to the Rust Worker.
