use libloading::{Library, Symbol};
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use uuid::Uuid;

const MAJOR: u16 = 1;
const MINOR: u16 = 1;
const MAX_FRAME: usize = 64 * 1024;
const DEFAULT_PIPE: &str = r"\\.\pipe\FinReport.Scanner.v1";
const DEFAULT_MAX_DATABASE_AGE_SECONDS: u64 = 7 * 24 * 60 * 60;

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields, tag = "operation", content = "payload")]
enum Request {
    #[serde(rename = "register_and_enqueue")]
    RegisterAndEnqueue(Register),
    #[serde(rename = "get_scan_status")]
    GetScanStatus { quarantine_id: Uuid },
    #[serde(rename = "get_health")]
    GetHealth,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Register {
    quarantine_id: Uuid,
    sha256: String,
    size_bytes: u64,
    original_filename: Option<String>,
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Envelope<T> {
    protocol_major: u16,
    protocol_minor: u16,
    request_id: Uuid,
    payload: T,
}
#[derive(Debug, Serialize)]
#[serde(tag = "status", content = "payload")]
enum Response {
    #[serde(rename = "accepted")]
    Accepted {
        quarantine_id: Uuid,
        disposition: &'static str,
    },
    #[serde(rename = "scan_result")]
    ScanResult(Record),
    #[serde(rename = "health")]
    Health {
        ready: bool,
        queue_depth: usize,
        active_scans: usize,
        update_error: Option<String>,
    },
    #[serde(rename = "error")]
    Error { code: &'static str, message: String },
}
#[derive(Debug, Clone, Serialize)]
struct Record {
    quarantine_id: Uuid,
    disposition: String,
    sha256: String,
    file_size: u64,
    original_filename: Option<String>,
    object_id: Option<String>,
    threat_name: Option<String>,
    error_code: Option<String>,
    scanner_state: String,
    database_version: String,
    engine_version: String,
    scan_time_ms: u64,
}
#[derive(Debug, Serialize, Deserialize)]
struct WorkerRequest {
    file: String,
}
#[derive(Debug, Serialize, Deserialize)]
struct WorkerResult {
    scan_rc: i32,
    status: i32,
    threat_name: Option<String>,
    message: String,
    database_id: Option<String>,
    engine_version: Option<String>,
}
#[derive(Clone)]
struct Config {
    pipe: String,
    storage: PathBuf,
    manifest: PathBuf,
    shim: Option<PathBuf>,
    database: Option<PathBuf>,
    database_root: Option<PathBuf>,
    certs: Option<PathBuf>,
    timeout: Duration,
}
impl Config {
    fn from_env() -> Self {
        let storage = env::var_os("FINREPORT_STORAGE_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("data/storage"));
        let manifest = env::var_os("FINREPORT_SCANNER_MANIFEST")
            .map(PathBuf::from)
            .unwrap_or_else(|| storage.join("quarantine/manifest.db"));
        let database_root = env::var_os("FINREPORT_AV_NATIVE_DATABASE").map(PathBuf::from);
        let database = database_root.clone().and_then(resolve_database_path);
        Self {
            pipe: env::var("FINREPORT_SCANNER_PIPE").unwrap_or_else(|_| DEFAULT_PIPE.into()),
            storage,
            manifest,
            shim: env::var_os("FINREPORT_AV_NATIVE_SHIM").map(PathBuf::from),
            database,
            database_root,
            certs: env::var_os("FINREPORT_AV_NATIVE_CERTS").map(PathBuf::from),
            timeout: Duration::from_secs(
                env::var("FINREPORT_AV_SCAN_TIMEOUT_SECONDS")
                    .ok()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(60),
            ),
        }
    }
    fn incoming(&self, id: Uuid) -> PathBuf {
        self.storage
            .join("quarantine/incoming")
            .join(format!("{id}.ready"))
    }
    fn managed(&self, id: Uuid) -> PathBuf {
        self.storage.join("quarantine/managed").join(id.to_string())
    }
    fn object(&self, h: &str) -> PathBuf {
        self.storage
            .join("objects")
            .join(&h[..2])
            .join(format!("{h}.pdf"))
    }
}

#[derive(Deserialize)]
struct CurrentDatabase {
    schema: String,
    database_id: String,
    directory: String,
    manifest: String,
}

fn resolve_database_path(root: PathBuf) -> Option<PathBuf> {
    let current = root.join("current.json");
    if !current.is_file() {
        return Some(root);
    }
    let canonical_root = fs::canonicalize(&root).ok()?;
    let contents = fs::read_to_string(&current).ok()?;
    let manifest: CurrentDatabase = serde_json::from_str(&contents).ok()?;
    if manifest.schema != "finreport-clamav-current-v1"
        || manifest.database_id.is_empty()
        || !safe_relative_path(&manifest.directory)
        || !safe_relative_path(&manifest.manifest)
    {
        return None;
    }
    let directory = manifest.directory.clone();
    let resolved = root.join(&directory);
    let manifest_path = root.join(&manifest.manifest);
    if !resolved.is_dir() || !manifest_path.is_file() {
        return None;
    }
    let canonical_resolved = fs::canonicalize(&resolved).ok()?;
    let canonical_manifest = fs::canonicalize(&manifest_path).ok()?;
    if !is_contained(&canonical_root, &canonical_resolved)
        || !is_contained(&canonical_root, &canonical_manifest)
    {
        return None;
    }
    let manifest_contents = fs::read_to_string(manifest_path).ok()?;
    let database_manifest: serde_json::Value = serde_json::from_str(&manifest_contents).ok()?;
    if database_manifest.get("schema")?.as_str()? != "finreport-clamav-database-v1"
        || database_manifest.get("database_id")?.as_str()? != manifest.database_id
        || database_manifest.get("directory")?.as_str()? != directory
    {
        return None;
    }
    let files = database_manifest.get("files")?.as_array()?;
    for file in files {
        let relative = file.get("path")?.as_str()?;
        let path = Path::new(relative);
        if !safe_relative_path(relative) || path.components().count() != 1 {
            return None;
        }
        let actual_path = resolved.join(path);
        let metadata = fs::metadata(&actual_path).ok()?;
        if !metadata.is_file() || metadata.len() != file.get("bytes")?.as_u64()? {
            return None;
        }
        if !is_contained(&canonical_resolved, &fs::canonicalize(&actual_path).ok()?) {
            return None;
        }
        if sha256_file(&actual_path).ok()?.as_str() != file.get("sha256")?.as_str()? {
            return None;
        }
    }
    resolved.is_dir().then_some(resolved)
}

fn safe_relative_path(value: &str) -> bool {
    let path = Path::new(value);
    !value.is_empty()
        && !path.is_absolute()
        && path
            .components()
            .all(|component| matches!(component, std::path::Component::Normal(_)))
}

fn is_contained(root: &Path, candidate: &Path) -> bool {
    candidate == root || candidate.starts_with(root)
}
struct Worker {
    child: Child,
    stdin: std::process::ChildStdin,
    stdout: Arc<Mutex<BufReader<std::process::ChildStdout>>>,
    database_id: String,
    #[cfg(windows)]
    _job: Job,
}

#[cfg(windows)]
struct Job(windows_sys::Win32::Foundation::HANDLE);
#[cfg(windows)]
unsafe impl Send for Job {}
#[cfg(windows)]
impl Drop for Job {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}
#[cfg(windows)]
fn own_worker(child: &Child) -> Result<Job, String> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    unsafe {
        let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if handle.is_null() {
            return Err("JOB_CREATE_FAILED".into());
        }
        let job = Job(handle);
        let mut information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            &information as *const _ as *const _,
            std::mem::size_of_val(&information) as u32,
        ) == 0
        {
            return Err("JOB_CONFIGURE_FAILED".into());
        }
        if AssignProcessToJobObject(handle, child.as_raw_handle() as _) == 0 {
            return Err("JOB_ASSIGN_FAILED".into());
        }
        Ok(job)
    }
}
struct Supervisor {
    config: Config,
    db: Mutex<Connection>,
    worker: Mutex<Option<Worker>>,
    active: std::sync::atomic::AtomicUsize,
    update_error: Mutex<Option<String>>,
}
impl Supervisor {
    fn open(config: Config) -> Result<Arc<Self>, String> {
        for directory in [
            config
                .manifest
                .parent()
                .unwrap_or(Path::new("."))
                .to_path_buf(),
            config.storage.join("quarantine/incoming"),
            config.storage.join("quarantine/managed"),
        ] {
            fs::create_dir_all(directory).map_err(|e| e.to_string())?;
        }
        let db = Connection::open(&config.manifest).map_err(|e| e.to_string())?;
        db.pragma_update(None, "journal_mode", "WAL")
            .map_err(|e| e.to_string())?;
        db.pragma_update(None, "synchronous", "FULL")
            .map_err(|e| e.to_string())?;
        db.execute_batch("CREATE TABLE IF NOT EXISTS quarantine_files(id TEXT PRIMARY KEY,sha256 TEXT,size_bytes INTEGER,original_filename TEXT,status TEXT,object_id TEXT,threat_name TEXT,error_code TEXT,scanner_state TEXT,database_version TEXT,engine_version TEXT,scan_time_ms INTEGER,updated_at TEXT DEFAULT CURRENT_TIMESTAMP); CREATE TABLE IF NOT EXISTS request_ledger(caller_identity TEXT,request_id TEXT,payload_hash TEXT,response_json TEXT,PRIMARY KEY(caller_identity,request_id)); CREATE TABLE IF NOT EXISTS scan_history(id INTEGER PRIMARY KEY,quarantine_id TEXT NOT NULL,result_json TEXT NOT NULL,scanned_at TEXT DEFAULT CURRENT_TIMESTAMP); CREATE TABLE IF NOT EXISTS reconciliation(path TEXT PRIMARY KEY,reason TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);").map_err(|e|e.to_string())?;
        let supervisor = Arc::new(Self {
            config,
            db: Mutex::new(db),
            worker: Mutex::new(None),
            active: std::sync::atomic::AtomicUsize::new(0),
            update_error: Mutex::new(None),
        });
        supervisor.recover()?;
        Ok(supervisor)
    }
    fn response_for(
        &self,
        caller: &str,
        id: Uuid,
        raw: &[u8],
        f: impl FnOnce() -> Response,
    ) -> Vec<u8> {
        let digest = hex_digest(raw);
        if let Ok(v)=self.db.lock().unwrap().query_row("SELECT payload_hash,response_json FROM request_ledger WHERE caller_identity=?1 AND request_id=?2",params![caller,id.to_string()],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))){if v.0==digest{return v.1.into_bytes()}return resp(id,Response::Error{code:"REQUEST_ID_REUSE",message:"request_id payload mismatch".into()})}
        let bytes = resp(id, f());
        let ledger_result = self.db.lock().unwrap().execute(
            "INSERT INTO request_ledger VALUES(?1,?2,?3,?4)",
            params![
                caller,
                id.to_string(),
                digest,
                String::from_utf8_lossy(&bytes).to_string()
            ],
        );
        if ledger_result.is_err() {
            resp(
                id,
                Response::Error {
                    code: "REQUEST_LEDGER_FAILED",
                    message: "request response was not durably recorded".into(),
                },
            )
        } else {
            bytes
        }
    }
    fn handle(self: &Arc<Self>, r: Request) -> Response {
        match r{Request::GetHealth=>{let ready=match self.ensure_worker(){Ok(())=>true,Err(error)=>{*self.update_error.lock().unwrap()=Some(error);false}};Response::Health{ready,queue_depth:self.db.lock().unwrap().query_row("SELECT COUNT(*) FROM quarantine_files WHERE status='queued'",[],|r|r.get::<_,i64>(0)).unwrap_or(0)as usize,active_scans:self.active.load(std::sync::atomic::Ordering::SeqCst),update_error:self.update_error.lock().unwrap().clone()}},Request::GetScanStatus{quarantine_id:id}=>self.db.lock().unwrap().query_row("SELECT id,status,sha256,size_bytes,original_filename,object_id,threat_name,error_code,scanner_state,database_version,engine_version,scan_time_ms FROM quarantine_files WHERE id=?1",params![id.to_string()],row).map(Response::ScanResult).unwrap_or(Response::Error{code:"SCAN_NOT_FOUND",message:"unknown quarantine id".into()}),Request::RegisterAndEnqueue(p)=>self.register(p)}
    }
    fn ensure_worker(&self) -> Result<(), String> {
        let mut desired = self.config.clone();
        if let Some(root) = &desired.database_root {
            desired.database = resolve_database_path(root.clone());
        }
        let mut worker = self.worker.lock().unwrap();
        let desired_id = desired
            .database_root
            .as_ref()
            .and_then(|path| read_current_database_id(path))
            .or_else(|| {
                desired
                    .database
                    .as_ref()
                    .and_then(|path| database_fingerprint(path).ok())
            });
        let desired_id = match desired_id {
            Some(id) if !id.is_empty() => id,
            _ if worker.is_some() => {
                *self.update_error.lock().unwrap() = Some("NATIVE_SCANNER_UNAVAILABLE".into());
                return Ok(());
            }
            _ => return Err("NATIVE_SCANNER_UNAVAILABLE".into()),
        };
        if worker.as_ref().map(|item| item.database_id.as_str()) == Some(desired_id.as_str()) {
            *self.update_error.lock().unwrap() = None;
            return Ok(());
        }
        if worker.as_ref().map(|item| item.database_id.as_str()) != Some(desired_id.as_str()) {
            match start_worker(&desired) {
                Ok(candidate) => {
                    let _old = worker.replace(candidate);
                    *self.update_error.lock().unwrap() = None;
                }
                Err(error) if worker.is_some() => {
                    *self.update_error.lock().unwrap() = Some(error);
                }
                Err(error) => return Err(error),
            }
        }
        Ok(())
    }
    fn register(self: &Arc<Self>, mut p: Register) -> Response {
        if !valid_hash(&p.sha256)
            || p.quarantine_id.get_version_num() != 4
            || p.size_bytes == 0
            || p.size_bytes > 50 * 1024 * 1024
        {
            return Response::Error {
                code: "INVALID_REGISTRATION",
                message: "invalid id, hash or file size".into(),
            };
        }
        p.original_filename = p.original_filename.as_deref().and_then(sanitize_filename);
        let ready = self.config.incoming(p.quarantine_id);
        let managed = self.config.managed(p.quarantine_id);
        if let Ok(existing) = self.db.lock().unwrap().query_row(
            "SELECT sha256,size_bytes,status FROM quarantine_files WHERE id=?1",
            params![p.quarantine_id.to_string()],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                ))
            },
        ) {
            if existing.0 == p.sha256 && existing.1 == p.size_bytes as i64 {
                if matches!(existing.2.as_str(), "accepted" | "queued" | "scanning") {
                    if !managed.is_file() && ready.is_file() {
                        if let Err(error) = fs::rename(&ready, &managed) {
                            return Response::Error {
                                code: "READY_CLAIM_FAILED",
                                message: error.to_string(),
                            };
                        }
                    }
                    if managed.is_file() {
                        let _ = self.db.lock().unwrap().execute(
                            "UPDATE quarantine_files SET status='queued',scanner_state='queued' WHERE id=?1 AND status='accepted'",
                            params![p.quarantine_id.to_string()],
                        );
                    } else if !managed.is_file() {
                        return Response::Error {
                            code: "RECONCILIATION_REQUIRED",
                            message: "registered upload is missing from quarantine storage".into(),
                        };
                    }
                }
                return Response::Accepted {
                    quarantine_id: p.quarantine_id,
                    disposition: "queued",
                };
            }
            return Response::Error {
                code: "QUARANTINE_ID_REUSE",
                message: "quarantine metadata mismatch".into(),
            };
        }
        if !ready.is_file() {
            return Response::Error {
                code: "READY_FILE_NOT_FOUND",
                message: "ready upload unavailable".into(),
            };
        }
        if managed.exists() {
            return Response::Error {
                code: "RECONCILIATION_REQUIRED",
                message: "managed file already exists without registration".into(),
            };
        }
        if let Err(error) = self.db.lock().unwrap().execute("INSERT INTO quarantine_files VALUES(?1,?2,?3,?4,'accepted',NULL,NULL,NULL,'accepted','unknown','unknown',0,CURRENT_TIMESTAMP)",params![p.quarantine_id.to_string(),p.sha256,p.size_bytes,p.original_filename]) {
            return Response::Error { code: "MANIFEST_WRITE_FAILED", message: error.to_string() };
        }
        if let Err(e) = fs::rename(&ready, &managed) {
            return Response::Error {
                code: "READY_CLAIM_FAILED",
                message: e.to_string(),
            };
        }
        if let Err(error) = self.db.lock().unwrap().execute(
            "UPDATE quarantine_files SET status='queued',scanner_state='queued' WHERE id=?1",
            params![p.quarantine_id.to_string()],
        ) {
            return Response::Error {
                code: "MANIFEST_WRITE_FAILED",
                message: error.to_string(),
            };
        }
        Response::Accepted {
            quarantine_id: p.quarantine_id,
            disposition: "queued",
        }
    }
    fn scan(&self, p: Register, managed: PathBuf) {
        let start = Instant::now();
        self.active.store(1, std::sync::atomic::Ordering::SeqCst);
        let mut r = Record {
            quarantine_id: p.quarantine_id,
            disposition: "error".into(),
            sha256: p.sha256.clone(),
            file_size: p.size_bytes,
            original_filename: p.original_filename.clone(),
            object_id: None,
            threat_name: None,
            error_code: None,
            scanner_state: "degraded".into(),
            database_version: "unknown".into(),
            engine_version: "libclamav".into(),
            scan_time_ms: 0,
        };
        r.file_size = fs::metadata(&managed).map(|m| m.len()).unwrap_or(0);
        if r.file_size != p.size_bytes {
            r.error_code = Some("FILE_SIZE_MISMATCH".into());
            return self.finish(r, start, &managed);
        }
        match sha256_file(&managed) {
            Ok(v) if v == p.sha256 => (),
            Ok(_) => {
                r.error_code = Some("FILE_HASH_MISMATCH".into());
                return self.finish(r, start, &managed);
            }
            Err(e) => {
                r.error_code = Some(e.to_string());
                return self.finish(r, start, &managed);
            }
        }
        let mut magic = [0; 5];
        if File::open(&managed)
            .and_then(|mut f| f.read_exact(&mut magic))
            .is_err()
            || magic != *b"%PDF-"
        {
            r.error_code = Some("PDF_REQUIRED".into());
            return self.finish(r, start, &managed);
        }
        let w = match self.worker(&managed) {
            Ok(v) => v,
            Err(e) => {
                r.error_code = Some(e);
                return self.finish(r, start, &managed);
            }
        };
        r.database_version = w.database_id.clone().unwrap_or_else(|| "unknown".into());
        if let Some(version) = w.engine_version {
            r.engine_version = version;
        }
        r.scanner_state = "healthy".into();
        if w.status == 1 {
            r.disposition = "infected".into();
            r.threat_name = w.threat_name
        } else if w.status == 0 && w.scan_rc == 0 {
            if self.db.lock().unwrap().execute("UPDATE quarantine_files SET status='promoting',object_id=?2,database_version=?3,engine_version=?4 WHERE id=?1",params![p.quarantine_id.to_string(),p.sha256,r.database_version,r.engine_version]).is_err(){r.error_code=Some("MANIFEST_WRITE_FAILED".into());return self.finish(r,start,&managed);}
            match publish(&managed, &self.config.object(&p.sha256), &p.sha256) {
                Ok(()) => {
                    r.disposition = "clean".into();
                    r.object_id = Some(p.sha256)
                }
                Err(e) => r.error_code = Some(e.to_string()),
            }
        } else {
            r.error_code = Some(if w.message.starts_with("DATABASE_STALE:") {
                w.message
            } else if w.message.starts_with("DATABASE_AGE_UNAVAILABLE:") {
                w.message
            } else {
                "NATIVE_SCAN_FAILED".into()
            })
        }
        self.finish(r, start, &managed)
    }
    fn worker(&self, path: &Path) -> Result<WorkerResult, String> {
        self.ensure_worker()?;
        let mut g = self.worker.lock().unwrap();
        let w = g.as_mut().unwrap();
        let actual_database_id = w.database_id.clone();
        w.stdin
            .write_all(
                serde_json::to_string(&WorkerRequest {
                    file: path.display().to_string(),
                })
                .unwrap()
                .as_bytes(),
            )
            .and_then(|_| w.stdin.write_all(b"\n"))
            .map_err(|e| e.to_string())?;
        w.stdin.flush().map_err(|e| e.to_string())?;
        let (tx, rx) = std::sync::mpsc::channel();
        let out = Arc::clone(&w.stdout);
        thread::spawn(move || {
            let mut line = String::new();
            let result = out.lock().unwrap().read_line(&mut line).map(|n| (n, line));
            let _ = tx.send(result);
        });
        match rx.recv_timeout(self.config.timeout) {
            Ok(Ok((n, line))) if n > 0 => {
                let mut output: WorkerResult =
                    serde_json::from_str(line.trim()).map_err(|e| e.to_string())?;
                output.database_id = Some(actual_database_id);
                Ok(output)
            }
            Ok(Ok(_)) => {
                *g = None;
                Err("WORKER_EOF".into())
            }
            Ok(Err(e)) => {
                *g = None;
                Err(e.to_string())
            }
            Err(_) => {
                let _ = w.child.kill();
                let _ = w.child.wait();
                *g = None;
                Err("SCAN_TIMEOUT".into())
            }
        }
    }
    fn finish(&self, mut r: Record, start: Instant, managed: &Path) {
        r.scan_time_ms = start.elapsed().as_millis() as u64;
        let mut db = self.db.lock().unwrap();
        let committed=(||->rusqlite::Result<()>{let tx=db.transaction()?;tx.execute("UPDATE quarantine_files SET status=?2,object_id=?3,threat_name=?4,error_code=?5,scanner_state=?6,database_version=?7,engine_version=?8,scan_time_ms=?9 WHERE id=?1",params![r.quarantine_id.to_string(),r.disposition,r.object_id,r.threat_name,r.error_code,r.scanner_state,r.database_version,r.engine_version,r.scan_time_ms as i64])?;tx.execute("INSERT INTO scan_history(quarantine_id,result_json) VALUES(?1,?2)",params![r.quarantine_id.to_string(),serde_json::to_string(&r).unwrap()])?;tx.commit()})().is_ok();
        drop(db);
        if committed && r.disposition == "clean" {
            let _ = fs::remove_file(managed);
        }
        self.active.store(0, std::sync::atomic::Ordering::SeqCst);
    }
    fn reconcile(&self, path: &Path, reason: &str) -> Result<(), String> {
        self.db
            .lock()
            .unwrap()
            .execute(
                "INSERT OR IGNORE INTO reconciliation(path,reason) VALUES(?1,?2)",
                params![path.display().to_string(), reason],
            )
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    fn recover(&self) -> Result<(), String> {
        let db = self.db.lock().unwrap();
        let mut statement=db.prepare("SELECT id,status,sha256,size_bytes,original_filename,object_id,threat_name,error_code,scanner_state,database_version,engine_version,scan_time_ms FROM quarantine_files").map_err(|e|e.to_string())?;
        let records = statement
            .query_map([], row)
            .map_err(|e| e.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| e.to_string())?;
        drop(statement);
        drop(db);
        for record in &records {
            let managed = self.config.managed(record.quarantine_id);
            match record.disposition.as_str() {
                "accepted" => {
                    let ready = self.config.incoming(record.quarantine_id);
                    if !managed.is_file() && ready.is_file() {
                        fs::rename(&ready, &managed).map_err(|error| error.to_string())?;
                    }
                    if managed.is_file() {
                        self.db.lock().unwrap().execute("UPDATE quarantine_files SET status='queued',scanner_state='recovering' WHERE id=?1",params![record.quarantine_id.to_string()]).map_err(|error|error.to_string())?;
                    } else {
                        self.db.lock().unwrap().execute("UPDATE quarantine_files SET status='reconciliation_required',error_code='ACCEPTED_FILE_MISSING' WHERE id=?1",params![record.quarantine_id.to_string()]).map_err(|error|error.to_string())?;
                    }
                }
                "queued" | "scanning" => {
                    if managed.is_file() {
                        self.db.lock().unwrap().execute("UPDATE quarantine_files SET status='queued',scanner_state='recovering' WHERE id=?1",params![record.quarantine_id.to_string()]).map_err(|e|e.to_string())?;
                    } else {
                        self.db.lock().unwrap().execute("UPDATE quarantine_files SET status='reconciliation_required',error_code='MANAGED_FILE_MISSING' WHERE id=?1",params![record.quarantine_id.to_string()]).map_err(|e|e.to_string())?;
                    }
                }
                "promoting" => {
                    let target = self.config.object(&record.sha256);
                    if target.is_file()
                        && sha256_file(&target).ok().as_deref() == Some(&record.sha256)
                    {
                        let mut clean = record.clone();
                        clean.disposition = "clean".into();
                        clean.object_id = Some(record.sha256.clone());
                        self.finish(clean, Instant::now(), &managed);
                    } else {
                        self.reconcile(&target, "promoting object unavailable or corrupt")?;
                    }
                }
                "clean" => {
                    if self.config.object(&record.sha256).is_file() {
                        let _ = fs::remove_file(managed);
                    }
                }
                _ => {}
            }
        }
        for entry in fs::read_dir(self.config.storage.join("quarantine/managed"))
            .map_err(|e| e.to_string())?
            .filter_map(Result::ok)
        {
            if !records
                .iter()
                .any(|r| r.quarantine_id.to_string() == entry.file_name().to_string_lossy())
            {
                self.reconcile(&entry.path(), "orphan managed file")?;
            }
        }
        for entry in fs::read_dir(self.config.storage.join("quarantine/incoming"))
            .map_err(|e| e.to_string())?
            .filter_map(Result::ok)
        {
            if entry
                .path()
                .extension()
                .is_some_and(|extension| extension == "ready")
            {
                self.reconcile(&entry.path(), "orphan ready file")?;
            }
        }
        if let Ok(prefixes) = fs::read_dir(self.config.storage.join("objects")) {
            for prefix in prefixes.filter_map(Result::ok) {
                if let Ok(objects) = fs::read_dir(prefix.path()) {
                    for object in objects.filter_map(Result::ok) {
                        if !records.iter().any(|r| {
                            matches!(r.disposition.as_str(), "clean" | "promoting")
                                && self.config.object(&r.sha256) == object.path()
                        }) {
                            self.reconcile(&object.path(), "orphan trusted object")?;
                        }
                    }
                }
            }
        }
        Ok(())
    }
    fn dispatch(self: &Arc<Self>) {
        let s = Arc::clone(self);
        thread::spawn(move || loop {
            let jobs = {
                let db = s.db.lock().unwrap();
                let mut stmt=match db.prepare("SELECT id,sha256,size_bytes,original_filename FROM quarantine_files WHERE status='queued' ORDER BY rowid LIMIT 100"){Ok(v)=>v,Err(_)=>{thread::sleep(Duration::from_millis(100));continue}};
                stmt.query_map([], |r| {
                    Ok(Register {
                        quarantine_id: Uuid::parse_str(&r.get::<_, String>(0)?).unwrap(),
                        sha256: r.get(1)?,
                        size_bytes: r.get::<_, i64>(2)? as u64,
                        original_filename: r.get(3)?,
                    })
                })
                .ok()
                .into_iter()
                .flatten()
                .filter_map(Result::ok)
                .collect::<Vec<_>>()
            };
            for job in jobs {
                let id = job.quarantine_id;
                let changed=s.db.lock().unwrap().execute("UPDATE quarantine_files SET status='scanning' WHERE id=?1 AND status='queued'",params![id.to_string()]).unwrap_or(0);
                if changed == 1 {
                    s.scan(job, s.config.managed(id));
                }
            }
            thread::sleep(Duration::from_millis(50));
        });
    }
}
fn row(r: &rusqlite::Row<'_>) -> rusqlite::Result<Record> {
    Ok(Record {
        quarantine_id: Uuid::parse_str(&r.get::<_, String>(0)?).unwrap_or(Uuid::nil()),
        disposition: r.get(1)?,
        sha256: r.get(2)?,
        file_size: r.get::<_, i64>(3)? as u64,
        original_filename: r.get(4)?,
        object_id: r.get(5)?,
        threat_name: r.get(6)?,
        error_code: r.get(7)?,
        scanner_state: r.get(8)?,
        database_version: r.get(9)?,
        engine_version: r.get(10)?,
        scan_time_ms: r.get::<_, i64>(11)? as u64,
    })
}
fn start_worker(config: &Config) -> Result<Worker, String> {
    let database = config
        .database
        .as_ref()
        .ok_or("NATIVE_SCANNER_UNAVAILABLE")?;
    let files = database_files(database)?;
    let database_id = config
        .database_root
        .as_ref()
        .and_then(|path| read_current_database_id(path))
        .unwrap_or(database_fingerprint(database)?);
    let snapshot = config
        .storage
        .join("quarantine/database_snapshots")
        .join(&database_id);
    let source_fingerprint = database_fingerprint(database)?;
    if snapshot.exists()
        && database_fingerprint(&snapshot).ok().as_deref() != Some(&source_fingerprint)
    {
        let damaged = snapshot.with_extension(format!("corrupt-{}", Uuid::new_v4()));
        fs::rename(&snapshot, &damaged).map_err(|e| e.to_string())?;
        let _ = remove_path(&damaged);
    }
    if !snapshot.is_dir() {
        if let Some(parent) = snapshot.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let temporary = snapshot.with_extension(format!("staging-{}", Uuid::new_v4()));
        fs::create_dir_all(&temporary).map_err(|e| e.to_string())?;
        for file in &files {
            if let Err(error) = fs::copy(file, temporary.join(file.file_name().unwrap())) {
                let _ = remove_path(&temporary);
                return Err(error.to_string());
            }
        }
        if database_fingerprint(&temporary)? != source_fingerprint {
            let _ = fs::remove_dir_all(&temporary);
            return Err("DATABASE_SNAPSHOT_HASH_MISMATCH".into());
        }
        if let Err(error) = fs::rename(&temporary, &snapshot) {
            let _ = fs::remove_dir_all(&temporary);
            return Err(error.to_string());
        }
    }
    let mut child = Command::new(env::current_exe().map_err(|e| e.to_string())?)
        .arg("worker")
        .arg("--shim")
        .arg(config.shim.as_ref().ok_or("NATIVE_SCANNER_UNAVAILABLE")?)
        .arg("--database")
        .arg(&snapshot)
        .arg("--certs")
        .arg(config.certs.as_ref().ok_or("NATIVE_SCANNER_UNAVAILABLE")?)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| e.to_string())?;
    #[cfg(windows)]
    let job = match own_worker(&child) {
        Ok(job) => job,
        Err(error) => {
            let mut child = child;
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
    };
    let stdin = child.stdin.take().ok_or("stdin")?;
    let stdout = Arc::new(Mutex::new(BufReader::new(
        child.stdout.take().ok_or("stdout")?,
    )));
    let (sender, receiver) = std::sync::mpsc::channel();
    let output = Arc::clone(&stdout);
    thread::spawn(move || {
        let mut line = String::new();
        let value = output
            .lock()
            .unwrap()
            .read_line(&mut line)
            .map(|n| (n, line));
        let _ = sender.send(value);
    });
    match receiver.recv_timeout(config.timeout) {
        Ok(Ok((n, line))) if n > 0 && line.trim() == "READY" => Ok(Worker {
            child,
            stdin,
            stdout,
            database_id,
            #[cfg(windows)]
            _job: job,
        }),
        Ok(Ok((n, line))) if n > 0 && line.trim().starts_with("ERROR:") => {
            let _ = child.kill();
            let _ = child.wait();
            Err(line.trim()["ERROR:".len()..].to_string())
        }
        _ => {
            let _ = child.kill();
            let _ = child.wait();
            Err("WORKER_STARTUP_FAILED".into())
        }
    }
}

fn remove_path(path: &Path) -> io::Result<()> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() => fs::remove_dir_all(path),
        Ok(_) => fs::remove_file(path),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}
fn database_files(database: &Path) -> Result<Vec<PathBuf>, String> {
    let mut files = fs::read_dir(database)
        .map_err(|e| e.to_string())?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_file())
        .collect::<Vec<_>>();
    files.sort();
    Ok(files)
}
fn database_fingerprint(database: &Path) -> Result<String, String> {
    let mut hash = Sha256::new();
    for file in database_files(database)? {
        hash.update(file.file_name().unwrap().to_string_lossy().as_bytes());
        hash.update(sha256_file(&file).map_err(|e| e.to_string())?.as_bytes());
    }
    Ok(hash.finalize().iter().map(|b| format!("{b:02x}")).collect())
}
fn read_current_database_id(root: &Path) -> Option<String> {
    let current: CurrentDatabase =
        serde_json::from_str(&fs::read_to_string(root.join("current.json")).ok()?).ok()?;
    (current.schema == "finreport-clamav-current-v1" && !current.database_id.is_empty())
        .then_some(current.database_id)
}
fn worker_loop(shim: PathBuf, db: PathBuf, certs: PathBuf) -> Result<(), String> {
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let stdout = io::stdout();
    let mut output = stdout.lock();
    let mut e = match Native::load(&shim, &db, &certs) {
        Ok(engine) => engine,
        Err(error) => {
            let _ = writeln!(output, "ERROR:{error}");
            let _ = output.flush();
            return Err(error);
        }
    };
    writeln!(output, "READY").map_err(|e| e.to_string())?;
    output.flush().map_err(|e| e.to_string())?;
    for line in input.by_ref().lines() {
        let req: WorkerRequest =
            serde_json::from_str(&line.map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
        writeln!(
            output,
            "{}",
            serde_json::to_string(&e.scan(Path::new(&req.file))).unwrap()
        )
        .map_err(|e| e.to_string())?;
        output.flush().map_err(|e| e.to_string())?
    }
    Ok(())
}
struct Native {
    _lib: Library,
    ptr: *mut std::ffi::c_void,
    database_age: Symbol<'static, unsafe extern "C" fn(*const i8, *mut u64) -> i32>,
    database_path: std::ffi::CString,
    max_database_age_seconds: u64,
    scan: Symbol<
        'static,
        unsafe extern "C" fn(*mut std::ffi::c_void, *const i8, *mut NResult) -> i32,
    >,
    free: Symbol<'static, unsafe extern "C" fn(*mut std::ffi::c_void)>,
    error: Symbol<'static, unsafe extern "C" fn(i32) -> *const i8>,
    version: Option<Symbol<'static, unsafe extern "C" fn() -> *const i8>>,
}
unsafe impl Send for Native {}
#[repr(C)]
struct NResult {
    status: i32,
    threat_name: *const i8,
}
impl Native {
    fn load(shim: &Path, db: &Path, certs: &Path) -> Result<Self, String> {
        let max_age = max_database_age_seconds()?;
        env::set_var("CVD_CERTS_DIR", certs);
        let lib = unsafe { Library::new(shim) }.map_err(|e| e.to_string())?;
        unsafe {
            type DatabaseAge = unsafe extern "C" fn(*const i8, *mut u64) -> i32;
            let database_age: Symbol<DatabaseAge> = lib
                .get(b"fr_clamav_database_age")
                .map_err(|_| "DATABASE_AGE_API_UNAVAILABLE".to_string())?;
            let database_path = std::ffi::CString::new(db.to_string_lossy().as_bytes())
                .map_err(|_| "invalid db")?;
            let mut age_seconds = 0_u64;
            let age_rc = database_age(database_path.as_ptr(), &mut age_seconds);
            if age_rc != 0 {
                return Err(format!("DATABASE_AGE_UNAVAILABLE:{age_rc}"));
            }
            validate_database_age(age_seconds, max_age)?;
            type C = unsafe extern "C" fn(*const i8, *mut *mut std::ffi::c_void) -> i32;
            let c: Symbol<C> = lib
                .get(b"fr_clamav_engine_create")
                .map_err(|e| e.to_string())?;
            let s: Symbol<
                unsafe extern "C" fn(*mut std::ffi::c_void, *const i8, *mut NResult) -> i32,
            > = lib
                .get(b"fr_clamav_engine_scan_file")
                .map_err(|e| e.to_string())?;
            let f: Symbol<unsafe extern "C" fn(*mut std::ffi::c_void)> = lib
                .get(b"fr_clamav_engine_free")
                .map_err(|e| e.to_string())?;
            let e: Symbol<unsafe extern "C" fn(i32) -> *const i8> = lib
                .get(b"fr_clamav_error_string")
                .map_err(|e| e.to_string())?;
            let version = lib.get(b"fr_clamav_version").ok().map(
                |symbol: Symbol<unsafe extern "C" fn() -> *const i8>| std::mem::transmute(symbol),
            );
            let mut ptr = std::ptr::null_mut();
            let rc = c(database_path.as_ptr(), &mut ptr);
            if rc != 0 || ptr.is_null() {
                return Err("engine create failed".into());
            }
            let scan = std::mem::transmute(s);
            let free = std::mem::transmute(f);
            let error = std::mem::transmute(e);
            let database_age = std::mem::transmute(database_age);
            drop(c);
            Ok(Self {
                _lib: lib,
                ptr,
                database_age,
                database_path,
                max_database_age_seconds: max_age,
                scan,
                free,
                error,
                version,
            })
        }
    }
    fn scan(&mut self, p: &Path) -> WorkerResult {
        unsafe {
            let mut age_seconds = 0_u64;
            let age_rc = (self.database_age)(self.database_path.as_ptr(), &mut age_seconds);
            if age_rc != 0 {
                return WorkerResult {
                    scan_rc: -1,
                    status: -1,
                    threat_name: None,
                    message: format!("DATABASE_AGE_UNAVAILABLE:{age_rc}"),
                    database_id: None,
                    engine_version: None,
                };
            }
            if let Err(error) = validate_database_age(age_seconds, self.max_database_age_seconds) {
                return WorkerResult {
                    scan_rc: -1,
                    status: -1,
                    threat_name: None,
                    message: error,
                    database_id: None,
                    engine_version: None,
                };
            }
            let p = match std::ffi::CString::new(p.to_string_lossy().as_bytes()) {
                Ok(v) => v,
                Err(_) => {
                    return WorkerResult {
                        scan_rc: -1,
                        status: -1,
                        threat_name: None,
                        message: "invalid path".into(),
                        database_id: None,
                        engine_version: None,
                    }
                }
            };
            let mut r = NResult {
                status: 0,
                threat_name: std::ptr::null(),
            };
            let rc = (self.scan)(self.ptr, p.as_ptr(), &mut r);
            WorkerResult {
                scan_rc: rc,
                status: r.status,
                threat_name: (!r.threat_name.is_null()).then(|| {
                    std::ffi::CStr::from_ptr(r.threat_name)
                        .to_string_lossy()
                        .into()
                }),
                message: std::ffi::CStr::from_ptr((self.error)(rc))
                    .to_string_lossy()
                    .into(),
                database_id: None,
                engine_version: self
                    .version
                    .as_ref()
                    .map(|version| std::ffi::CStr::from_ptr(version()).to_string_lossy().into()),
            }
        }
    }
}

fn max_database_age_seconds() -> Result<u64, String> {
    match env::var("FINREPORT_AV_MAX_DATABASE_AGE_SECONDS") {
        Ok(value) => max_database_age_seconds_from(Some(&value)),
        Err(env::VarError::NotPresent) => Ok(DEFAULT_MAX_DATABASE_AGE_SECONDS),
        Err(env::VarError::NotUnicode(_)) => Err("INVALID_DATABASE_MAX_AGE_SECONDS".into()),
    }
}

fn max_database_age_seconds_from(value: Option<&str>) -> Result<u64, String> {
    match value {
        Some(value) => value
            .parse::<u64>()
            .ok()
            .filter(|age| *age > 0)
            .ok_or_else(|| "INVALID_DATABASE_MAX_AGE_SECONDS".into()),
        None => Ok(DEFAULT_MAX_DATABASE_AGE_SECONDS),
    }
}

fn validate_database_age(age_seconds: u64, max_age_seconds: u64) -> Result<(), String> {
    if max_age_seconds == 0 {
        return Err("INVALID_DATABASE_MAX_AGE_SECONDS".into());
    }
    if age_seconds > max_age_seconds {
        return Err(format!(
            "DATABASE_STALE:age_seconds={age_seconds}:max_age_seconds={max_age_seconds}"
        ));
    }
    Ok(())
}
impl Drop for Native {
    fn drop(&mut self) {
        unsafe { (self.free)(self.ptr) }
    }
}
fn valid_hash(v: &str) -> bool {
    v.len() == 64
        && v.bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}
fn sanitize_filename(value: &str) -> Option<String> {
    let name = Path::new(value)
        .file_name()?
        .to_str()?
        .chars()
        .filter(|c| c.is_ascii_graphic() || *c == ' ')
        .take(255)
        .collect::<String>();
    (!name.is_empty()).then_some(name)
}
fn hex_digest(v: &[u8]) -> String {
    Sha256::digest(v)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
fn sha256_file(p: &Path) -> io::Result<String> {
    let mut f = File::open(p)?;
    let mut h = Sha256::new();
    let mut b = [0; 1024 * 1024];
    loop {
        let n = f.read(&mut b)?;
        if n == 0 {
            break;
        }
        h.update(&b[..n])
    }
    Ok(h.finalize().iter().map(|b| format!("{b:02x}")).collect())
}
fn publish(source: &Path, target: &Path, expected: &str) -> io::Result<()> {
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }
    if target.exists() {
        if sha256_file(target)? != expected {
            return Err(io::Error::other("OBJECT_HASH_CONFLICT"));
        }
        return Ok(());
    }
    let parent = target
        .parent()
        .ok_or_else(|| io::Error::other("OBJECT_PARENT_MISSING"))?;
    let temporary = parent.join(format!(".{expected}.{}.tmp", Uuid::new_v4()));
    let mut input = File::open(source)?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    io::copy(&mut input, &mut output)?;
    output.sync_all()?;
    drop(output);
    if sha256_file(&temporary)? != expected {
        let _ = fs::remove_file(&temporary);
        return Err(io::Error::other("OBJECT_HASH_CONFLICT"));
    }
    fs::rename(temporary, target)?;
    Ok(())
}
fn resp<T: Serialize>(id: Uuid, p: T) -> Vec<u8> {
    serde_json::to_vec(&Envelope {
        protocol_major: MAJOR,
        protocol_minor: MINOR,
        request_id: id,
        payload: p,
    })
    .unwrap()
}
fn read_frame(r: &mut impl Read) -> io::Result<Vec<u8>> {
    let mut h = [0; 4];
    r.read_exact(&mut h)?;
    let n = u32::from_be_bytes(h) as usize;
    if n == 0 || n > MAX_FRAME {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "frame exceeds 64 KiB",
        ));
    }
    let mut b = vec![0; n];
    r.read_exact(&mut b)?;
    Ok(b)
}
#[cfg(windows)]
fn read_frame_timeout(r: &mut File, timeout: Duration) -> io::Result<Vec<u8>> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::Pipes::PeekNamedPipe;
    let handle = r.as_raw_handle() as windows_sys::Win32::Foundation::HANDLE;
    let deadline = Instant::now() + timeout;
    let wait_for = |needed: usize| -> io::Result<()> {
        loop {
            let mut available = 0_u32;
            let ok = unsafe {
                PeekNamedPipe(
                    handle,
                    std::ptr::null_mut(),
                    0,
                    std::ptr::null_mut(),
                    &mut available,
                    std::ptr::null_mut(),
                )
            };
            if ok == 0 {
                return Err(io::Error::last_os_error());
            }
            if available as usize >= needed {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "pipe read timeout"));
            }
            thread::sleep(Duration::from_millis(10));
        }
    };
    wait_for(4)?;
    let mut header = [0_u8; 4];
    r.read_exact(&mut header)?;
    let n = u32::from_be_bytes(header) as usize;
    if n == 0 || n > MAX_FRAME {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "frame exceeds 64 KiB",
        ));
    }
    wait_for(n)?;
    let mut body = vec![0_u8; n];
    r.read_exact(&mut body)?;
    Ok(body)
}
fn write_frame(w: &mut impl Write, b: &[u8]) -> io::Result<()> {
    if b.is_empty() || b.len() > MAX_FRAME {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "frame exceeds 64 KiB",
        ));
    }
    w.write_all(&(b.len() as u32).to_be_bytes())?;
    w.write_all(b)?;
    w.flush()
}
fn process(s: &Arc<Supervisor>, b: &[u8], caller: &str) -> Vec<u8> {
    match serde_json::from_slice::<Envelope<Request>>(b){
        Ok(e) if e.protocol_major==MAJOR&&e.protocol_minor<=MINOR => {
            let request_id=e.request_id;
            match e.payload {
                Request::GetHealth => resp(request_id,s.handle(Request::GetHealth)),
                Request::GetScanStatus{quarantine_id} => resp(request_id,s.handle(Request::GetScanStatus{quarantine_id})),
                request => s.response_for(caller,request_id,b,||s.handle(request)),
            }
        }
        Ok(_) => serde_json::to_vec(&serde_json::json!({"status":"error","payload":{"code":"PROTOCOL_VERSION_UNSUPPORTED"}})).unwrap(),
        Err(e)=>serde_json::to_vec(&serde_json::json!({"status":"error","payload":{"code":"INVALID_REQUEST","message":e.to_string()}})).unwrap()
    }
}
fn stdio(s: Arc<Supervisor>) -> Result<(), String> {
    let stdin = io::stdin();
    let mut i = stdin.lock();
    let stdout = io::stdout();
    let mut o = stdout.lock();
    loop {
        let b = match read_frame(&mut i) {
            Ok(v) => v,
            Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(()),
            Err(e) => return Err(e.to_string()),
        };
        write_frame(&mut o, &process(&s, &b, "stdio")).map_err(|e| e.to_string())?
    }
}
#[cfg(windows)]
fn token_sid(token: windows_sys::Win32::Foundation::HANDLE) -> Result<String, String> {
    use windows_sys::Win32::Security::Authorization::ConvertSidToStringSidW;
    use windows_sys::Win32::Security::{GetTokenInformation, TokenUser, TOKEN_USER};
    unsafe {
        let mut length = 0_u32;
        GetTokenInformation(token, TokenUser, std::ptr::null_mut(), 0, &mut length);
        if length == 0 {
            return Err("TOKEN_USER_QUERY_FAILED".into());
        }
        let mut buffer = vec![
            0_usize;
            (length as usize + std::mem::size_of::<usize>() - 1)
                / std::mem::size_of::<usize>()
        ];
        if GetTokenInformation(
            token,
            TokenUser,
            buffer.as_mut_ptr() as _,
            length,
            &mut length,
        ) == 0
        {
            return Err("TOKEN_USER_QUERY_FAILED".into());
        }
        let user = &*(buffer.as_ptr() as *const TOKEN_USER);
        let mut text = std::ptr::null_mut();
        if ConvertSidToStringSidW(user.User.Sid, &mut text) == 0 {
            return Err("TOKEN_SID_FAILED".into());
        }
        let mut count = 0;
        while *text.add(count) != 0 {
            count += 1;
        }
        let result = String::from_utf16_lossy(std::slice::from_raw_parts(text, count));
        windows_sys::Win32::Foundation::LocalFree(text as _);
        Ok(result)
    }
}
#[cfg(windows)]
fn process_sid() -> Result<String, String> {
    use windows_sys::Win32::Security::TOKEN_QUERY;
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
    unsafe {
        let mut token = std::ptr::null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
            return Err("PROCESS_TOKEN_FAILED".into());
        }
        let result = token_sid(token);
        windows_sys::Win32::Foundation::CloseHandle(token);
        result
    }
}
#[cfg(windows)]
fn pipe_client_sid(handle: windows_sys::Win32::Foundation::HANDLE) -> Result<String, String> {
    use windows_sys::Win32::Security::{RevertToSelf, TOKEN_QUERY};
    use windows_sys::Win32::System::Threading::{GetCurrentThread, OpenThreadToken};
    unsafe {
        if windows_sys::Win32::System::Pipes::ImpersonateNamedPipeClient(handle) == 0 {
            return Err("PIPE_IMPERSONATION_FAILED".into());
        }
        let mut token = std::ptr::null_mut();
        let result = if OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, 1, &mut token) == 0 {
            Err("PIPE_CLIENT_TOKEN_FAILED".into())
        } else {
            let result = token_sid(token);
            windows_sys::Win32::Foundation::CloseHandle(token);
            result
        };
        RevertToSelf();
        result
    }
}
#[cfg(windows)]
fn pipe(s: Arc<Supervisor>) -> Result<(), String> {
    use std::os::windows::io::FromRawHandle;
    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, ERROR_PIPE_CONNECTED, INVALID_HANDLE_VALUE,
    };
    use windows_sys::Win32::Security::Authorization::ConvertStringSecurityDescriptorToSecurityDescriptorW;
    use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;
    use windows_sys::Win32::Storage::FileSystem::{FlushFileBuffers, PIPE_ACCESS_DUPLEX};
    use windows_sys::Win32::System::Pipes::{
        ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, PIPE_READMODE_BYTE,
        PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_BYTE, PIPE_WAIT,
    };
    let n: Vec<u16> = s.config.pipe.encode_utf16().chain(Some(0)).collect();
    let allowed = env::var("FINREPORT_SCANNER_ALLOWED_SID").unwrap_or(process_sid()?);
    if allowed.len() <= 2
        || !allowed.starts_with("S-")
        || allowed[2..]
            .chars()
            .any(|c| !(c.is_ascii_digit() || c == '-'))
        || allowed[2..].split('-').any(|part| part.is_empty())
    {
        return Err("INVALID_SCANNER_ALLOWED_SID".into());
    }
    let sddl = format!("D:P(A;;GA;;;SY)(A;;GA;;;{allowed})");
    let sddl_w: Vec<u16> = sddl.encode_utf16().chain(Some(0)).collect();
    loop {
        let mut security_descriptor = std::ptr::null_mut();
        let mut descriptor_size = 0_u32;
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl_w.as_ptr(),
                1,
                &mut security_descriptor,
                &mut descriptor_size,
            )
        } == 0
        {
            return Err("PIPE_SECURITY_DESCRIPTOR_FAILED".into());
        }
        let security = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: security_descriptor,
            bInheritHandle: 0,
        };
        let h = unsafe {
            CreateNamedPipeW(
                n.as_ptr(),
                PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                MAX_FRAME as u32,
                MAX_FRAME as u32,
                0,
                &security,
            )
        };
        unsafe {
            windows_sys::Win32::Foundation::LocalFree(security_descriptor as _);
        }
        if h == INVALID_HANDLE_VALUE {
            return Err(format!("CreateNamedPipeW {}", unsafe { GetLastError() }));
        }
        let ok = unsafe { ConnectNamedPipe(h, std::ptr::null_mut()) } != 0
            || unsafe { GetLastError() } == ERROR_PIPE_CONNECTED;
        if ok {
            let mut f = unsafe { File::from_raw_handle(h as *mut _) };
            if let Ok(b) = read_frame_timeout(&mut f, s.config.timeout) {
                if let Ok(caller) = pipe_client_sid(h) {
                    if caller == allowed || caller == "S-1-5-18" {
                        let _ = write_frame(&mut f, &process(&s, &b, &caller));
                    }
                }
            }
            let _ = unsafe { FlushFileBuffers(h) };
            let _ = unsafe { DisconnectNamedPipe(h) };
            drop(f);
        } else {
            unsafe { CloseHandle(h) };
        }
    }
}
#[cfg(not(windows))]
fn pipe(_s: Arc<Supervisor>) -> Result<(), String> {
    Err("named pipe only supported on Windows; use stdio".into())
}
fn main() {
    let mut args = env::args().skip(1);
    let command = args.next().unwrap_or_default();
    let result=match command.as_str(){
        "serve"=>Supervisor::open(Config::from_env()).and_then(|s|{s.dispatch();pipe(s)}),
        "stdio"=>Supervisor::open(Config::from_env()).and_then(|s|{s.dispatch();stdio(s)}),
        "worker"|"native-probe"=>{
            let mut shim=None;let mut database=None;let mut certs=None;let mut file=None;let mut json=false;
            while let Some(arg)=args.next(){match arg.as_str(){"--shim"=>shim=args.next(),"--database"=>database=args.next(),"--certs"=>certs=args.next(),"--file"=>file=args.next(),"--json"=>json=true,_=>{eprintln!("unknown argument {arg}");std::process::exit(2);}}}
            match (shim,database,certs){
                (Some(shim),Some(database),Some(certs))=>{
                    if command=="worker"{worker_loop(PathBuf::from(shim),PathBuf::from(database),PathBuf::from(certs))}
                    else if let Some(file)=file{resolve_database_path(PathBuf::from(&database)).ok_or_else(||"DATABASE_POINTER_INVALID".to_string()).and_then(|database|Native::load(Path::new(&shim),&database,Path::new(&certs))).map(|mut engine|{let output=engine.scan(Path::new(&file));if json{println!("{}",serde_json::to_string(&output).unwrap());}else{println!("scan_rc={} status={} threat={} message={}",output.scan_rc,output.status,output.threat_name.as_deref().unwrap_or(""),output.message);}})}
                    else{Err("native-probe requires --file".into())}
                }
                _=>Err("worker/native-probe requires --shim, --database and --certs".into()),
            }
        }
        _=>Err("usage: serve | stdio | worker | native-probe --shim DLL --database DIR --certs DIR [--file PATH --json]".into()),
    };
    if let Err(error) = result {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn protocol_uses_snake_case_operation() {
        let value = serde_json::to_value(Request::GetHealth).unwrap();
        assert_eq!(value["operation"], "get_health");
    }
    #[test]
    fn frame_prefix_is_big_endian_and_bounded() {
        let mut bytes = Vec::new();
        write_frame(&mut bytes, b"hello").unwrap();
        assert_eq!(&bytes[..4], &[0, 0, 0, 5]);
        assert_eq!(read_frame(&mut bytes.as_slice()).unwrap(), b"hello");
        assert!(write_frame(&mut Vec::new(), &vec![0_u8; MAX_FRAME + 1]).is_err());
    }
    #[test]
    fn hash_validation_rejects_uppercase() {
        assert!(valid_hash(&"a".repeat(64)));
        assert!(!valid_hash(&"A".repeat(64)));
        assert!(!valid_hash("short"));
    }
    #[test]
    fn database_pointer_paths_reject_traversal_and_absolute_paths() {
        assert!(safe_relative_path("versions/db-1"));
        assert!(safe_relative_path("database-manifest.json"));
        assert!(!safe_relative_path("../outside"));
        assert!(!safe_relative_path("versions/../outside"));
        assert!(!safe_relative_path(r"C:\outside"));
        assert!(!safe_relative_path(""));
    }
    #[test]
    fn stale_database_age_fails_closed() {
        assert!(validate_database_age(7 * 24 * 60 * 60, 7 * 24 * 60 * 60).is_ok());
        assert!(
            validate_database_age(7 * 24 * 60 * 60 + 1, 7 * 24 * 60 * 60)
                .unwrap_err()
                .starts_with("DATABASE_STALE:")
        );
        assert_eq!(
            max_database_age_seconds_from(Some("0")),
            Err("INVALID_DATABASE_MAX_AGE_SECONDS".into())
        );
        assert_eq!(
            max_database_age_seconds_from(Some("6h")),
            Err("INVALID_DATABASE_MAX_AGE_SECONDS".into())
        );
        assert_eq!(
            max_database_age_seconds_from(None),
            Ok(DEFAULT_MAX_DATABASE_AGE_SECONDS)
        );
    }
}
