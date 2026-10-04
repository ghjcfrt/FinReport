use libloading::{Library, Symbol};
use serde::{Deserialize, Serialize};
use std::env;
use std::ffi::{c_char, c_void, CStr, CString};
use std::path::PathBuf;
use uuid::Uuid;

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields, tag = "operation", content = "payload")]
enum ControlRequest {
    RegisterAndEnqueue(RegisterPayload),
    GetScanStatus { quarantine_id: Uuid },
    GetHealth,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RegisterPayload {
    quarantine_id: Uuid,
    sha256: String,
    size_bytes: u64,
    original_filename: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields, tag = "operation", content = "payload")]
enum WorkerRequest {
    LoadDatabase {
        database_id: String,
    },
    Scan {
        job_id: Uuid,
        quarantine_id: Uuid,
        expected_sha256: String,
    },
    Ping,
    Shutdown,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Envelope<T> {
    protocol_major: u16,
    protocol_minor: u16,
    request_id: Uuid,
    payload: T,
}

#[repr(C)]
struct ScanResult {
    status: i32,
    threat_name: *const c_char,
}

type EngineCreate = unsafe extern "C" fn(*const c_char, *mut *mut c_void) -> i32;
type EngineScan = unsafe extern "C" fn(*mut c_void, *const c_char, *mut ScanResult) -> i32;
type EngineFree = unsafe extern "C" fn(*mut c_void);
type ErrorString = unsafe extern "C" fn(i32) -> *const c_char;

#[derive(Debug, Serialize)]
struct ProbeOutput {
    scan_rc: i32,
    status: i32,
    threat_name: Option<String>,
    message: String,
}

fn required_arg(args: &mut impl Iterator<Item = String>, name: &str) -> Result<String, String> {
    args.next()
        .ok_or_else(|| format!("missing value for {name}"))
}

fn run_native_probe(
    shim: PathBuf,
    database: PathBuf,
    file: PathBuf,
    certs: Option<PathBuf>,
    json: bool,
) -> Result<(), String> {
    if let Some(certs) = certs.filter(|p| p.is_dir()) {
        env::set_var("CVD_CERTS_DIR", certs);
    }
    let db =
        CString::new(database.to_string_lossy().as_bytes()).map_err(|_| "invalid database path")?;
    let path = CString::new(file.to_string_lossy().as_bytes()).map_err(|_| "invalid file path")?;
    let library = unsafe { Library::new(&shim) }.map_err(|e| format!("load shim: {e}"))?;
    unsafe {
        let create: Symbol<EngineCreate> = library
            .get(b"fr_clamav_engine_create")
            .map_err(|e| e.to_string())?;
        let scan: Symbol<EngineScan> = library
            .get(b"fr_clamav_engine_scan_file")
            .map_err(|e| e.to_string())?;
        let free: Symbol<EngineFree> = library
            .get(b"fr_clamav_engine_free")
            .map_err(|e| e.to_string())?;
        let error_string: Symbol<ErrorString> = library
            .get(b"fr_clamav_error_string")
            .map_err(|e| e.to_string())?;
        let mut engine = std::ptr::null_mut();
        let rc = create(db.as_ptr(), &mut engine);
        if rc != 0 || engine.is_null() {
            let message = CStr::from_ptr(error_string(rc)).to_string_lossy();
            return Err(format!("engine create failed rc={rc}: {message}"));
        }
        let mut result = ScanResult {
            status: 0,
            threat_name: std::ptr::null(),
        };
        let scan_rc = scan(engine, path.as_ptr(), &mut result);
        let threat = if result.threat_name.is_null() {
            None
        } else {
            Some(
                CStr::from_ptr(result.threat_name)
                    .to_string_lossy()
                    .into_owned(),
            )
        };
        free(engine);
        let message = CStr::from_ptr(error_string(scan_rc)).to_string_lossy();
        if json {
            let output = ProbeOutput {
                scan_rc,
                status: result.status,
                threat_name: threat,
                message: message.into_owned(),
            };
            println!("{}", serde_json::to_string(&output).map_err(|e| e.to_string())?);
        } else {
            println!(
                "scan_rc={scan_rc} status={} threat={} message={message}",
                result.status,
                threat.as_deref().unwrap_or("")
            );
        }
    }
    Ok(())
}

fn main() {
    let mut args = env::args().skip(1);
    match args.next().as_deref() {
        Some("native-probe") => {
            let mut shim = None;
            let mut database = None;
            let mut file = None;
            let mut certs = None;
            let mut json = false;
            while let Some(arg) = args.next() {
                match arg.as_str() {
                    "--shim" => shim = Some(required_arg(&mut args, "--shim").unwrap()),
                    "--database" => database = Some(required_arg(&mut args, "--database").unwrap()),
                    "--file" => file = Some(required_arg(&mut args, "--file").unwrap()),
                    "--certs" => certs = Some(required_arg(&mut args, "--certs").unwrap()),
                    "--json" => json = true,
                    other => {
                        eprintln!("unknown argument: {other}");
                        std::process::exit(2);
                    }
                }
            }
            let result = match (shim, database, file) {
                (Some(shim), Some(database), Some(file)) => run_native_probe(
                    PathBuf::from(shim),
                    PathBuf::from(database),
                    PathBuf::from(file),
                    certs.map(PathBuf::from),
                    json,
                ),
                _ => Err("native-probe requires --shim, --database and --file".to_string()),
            };
            if let Err(error) = result {
                eprintln!("{error}");
                std::process::exit(1);
            }
        }
        _ => {
            // Native Windows pipe, manifest recovery, and child-process supervision are
            // implemented in the next layer. Keep protocol types compiled and reviewed first.
            let _ = std::mem::size_of::<Envelope<ControlRequest>>();
            let _ = std::mem::size_of::<Envelope<WorkerRequest>>();
            eprintln!("usage: finreport-scanner-supervisor native-probe --shim DLL --database DIR --file PATH [--certs DIR] [--json]");
        }
    }
}
