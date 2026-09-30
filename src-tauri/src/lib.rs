use std::collections::VecDeque;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, ChildStderr, Command, ExitStatus, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tauri::{Manager, WebviewUrl};

const STDERR_TAIL_LIMIT: usize = 16 * 1024;

struct WebProcess {
    child: Child,
    source: String,
    stderr_tail: Arc<Mutex<VecDeque<u8>>>,
    stderr_reader: Option<std::thread::JoinHandle<()>>,
}

#[derive(Clone, Copy)]
enum NativeCommand {
    Apply,
    Retry,
    Reset,
}

static WEB_PROCESS: Mutex<Option<WebProcess>> = Mutex::new(None);
static LIFECYCLE_OPERATION: Mutex<()> = Mutex::new(());
static STARTUP_ERROR_STATE: Mutex<Option<bool>> = Mutex::new(None);

fn web_port() -> String {
    std::env::var("ANOTHER_LLM_WEB_PORT").unwrap_or_else(|_| "8765".into())
}

fn managed_python_path(runtime_root: &std::path::Path) -> Result<PathBuf, String> {
    let python = runtime_root.join("bin").join("python3");
    if !python.is_file() {
        return Err(format!(
            "缺少内置 managed Python runtime：{}",
            python.display()
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if std::fs::metadata(&python)
            .map(|metadata| metadata.permissions().mode() & 0o111 == 0)
            .unwrap_or(true)
        {
            return Err(format!(
                "内置 managed Python 不可执行：{}",
                python.display()
            ));
        }
    }
    Ok(python)
}

fn managed_runtime_root(
    resource_dir: &std::path::Path,
    debug_runtime_root: Option<&std::path::Path>,
    debug_build: bool,
) -> Result<PathBuf, String> {
    if debug_build {
        return debug_runtime_root
            .filter(|path| !path.as_os_str().is_empty())
            .map(std::path::Path::to_path_buf)
            .ok_or_else(|| "开发模式缺少 ANOTHER_LLM_MANAGED_RUNTIME_DIR".to_string());
    }
    Ok(resource_dir.join("managed-runtime"))
}

fn python_command(python: &std::path::Path, port: &str) -> Command {
    let mut command = Command::new(python);
    command.args(["-I", "-B", "-m", "app.web", "--port", port]);
    command.env_remove("PYTHONPATH");
    command.env_remove("PYTHONHOME");
    command
}

fn data_root_command(python: &std::path::Path, args: &[&str]) -> Command {
    let mut command = Command::new(python);
    command.args(["-I", "-B", "-m", "app.data_root"]);
    command.args(args);
    command.env_remove("PYTHONPATH");
    command.env_remove("PYTHONHOME");
    command
}

fn managed_python_for_app(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("无法定位应用资源目录：{error}"))?;
    #[cfg(debug_assertions)]
    let debug_runtime_root = std::env::var_os("ANOTHER_LLM_MANAGED_RUNTIME_DIR").map(PathBuf::from);
    #[cfg(not(debug_assertions))]
    let debug_runtime_root: Option<PathBuf> = None;
    let runtime_root = managed_runtime_root(
        &resource_dir,
        debug_runtime_root.as_deref(),
        cfg!(debug_assertions),
    )?;
    managed_python_path(&runtime_root)
}

fn start_web_process(app: &tauri::AppHandle) -> Result<WebProcess, String> {
    let port = web_port();
    let python = managed_python_for_app(app)?;
    let source = format!("{} -I -B -m app.web --port {port}", python.display());
    spawn_web_process(python_command(&python, &port), source)
}

fn run_data_root_helper(app: &tauri::AppHandle, args: &[&str]) -> Result<String, String> {
    let python = managed_python_for_app(app)?;
    let command_name = format!(
        "{} -I -B -m app.data_root {}",
        python.display(),
        args.join(" ")
    );
    let output = data_root_command(&python, args)
        .output()
        .map_err(|error| format!("无法启动数据目录工具 {command_name}：{error}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let details = [stderr, stdout]
            .into_iter()
            .filter(|value| !value.is_empty())
            .collect::<Vec<_>>()
            .join("\n");
        return Err(format!(
            "数据目录工具执行失败（{}）{details}",
            exit_status_description(output.status)
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

enum RelocationError {
    ServiceAvailable(String),
    RecoveryRequired(String),
}

impl RelocationError {
    fn message(&self) -> &str {
        match self {
            Self::ServiceAvailable(message) | Self::RecoveryRequired(message) => message,
        }
    }
}

fn run_relocation_sequence<T: std::fmt::Display>(
    stop: impl FnOnce() -> Result<(), String>,
    relocate: impl FnOnce() -> Result<T, String>,
    restart: impl FnOnce() -> Result<(), String>,
) -> Result<T, RelocationError> {
    stop().map_err(|error| {
        RelocationError::ServiceAvailable(format!("迁移前无法安全停止 Web 服务：{error}"))
    })?;
    let relocation_result = relocate();
    let restart_result = restart();
    match (relocation_result, restart_result) {
        (Ok(value), Ok(())) => Ok(value),
        (Err(error), Ok(())) => Err(RelocationError::ServiceAvailable(error)),
        (Ok(value), Err(error)) => Err(RelocationError::RecoveryRequired(format!(
            "数据目录工具已完成：{value}\nWeb 服务启动失败：{error}"
        ))),
        (Err(relocation), Err(restart)) => Err(RelocationError::RecoveryRequired(format!(
            "数据目录迁移失败：{relocation}\n恢复 Web 服务也失败：{restart}"
        ))),
    }
}

fn validate_data_root_reset(confirm: bool, web_process_running: bool) -> Result<(), String> {
    if !confirm {
        return Err("切换默认数据目录需要显式确认".to_string());
    }
    if web_process_running {
        return Err("Web 服务运行时不能重置数据目录".to_string());
    }
    Ok(())
}

fn with_lifecycle_lock<T>(lock: &Mutex<()>, operation: impl FnOnce() -> T) -> T {
    let _guard = lock.lock().unwrap();
    operation()
}

fn startup_error_allows_reset(error: &str) -> bool {
    error.contains("invalid user data locator") || error.contains("custom user root is unavailable")
}

fn startup_error_state() -> Option<bool> {
    *STARTUP_ERROR_STATE.lock().unwrap()
}

fn is_web_service_url(url: &tauri::Url, port: &str) -> bool {
    let Ok(port) = port.parse::<u16>() else {
        return false;
    };
    url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port_or_known_default() == Some(port)
        && url.username().is_empty()
        && url.password().is_none()
}

fn is_startup_error_url(url: &tauri::Url) -> bool {
    let app_local_origin = if cfg!(windows) {
        url.scheme() == "http" && url.host_str() == Some("tauri.localhost")
    } else {
        url.scheme() == "tauri" && url.host_str() == Some("localhost")
    };
    app_local_origin
        && url.port().is_none()
        && url.path() == "/startup-error.html"
        && url.username().is_empty()
        && url.password().is_none()
}

fn allowed_webview_navigation(
    url: &tauri::Url,
    port: &str,
    web_process_running: bool,
    startup_error_state: Option<bool>,
) -> bool {
    (web_process_running && is_web_service_url(url, port))
        || (startup_error_state.is_some() && is_startup_error_url(url))
}

fn validate_native_command(
    command: NativeCommand,
    url: &tauri::Url,
    web_process_running: bool,
    startup_error_state: Option<bool>,
    port: &str,
) -> Result<(), String> {
    let authorized = match command {
        NativeCommand::Apply => web_process_running && is_web_service_url(url, port),
        NativeCommand::Retry => startup_error_state.is_some() && is_startup_error_url(url),
        NativeCommand::Reset => {
            !web_process_running && startup_error_state == Some(true) && is_startup_error_url(url)
        }
    };
    if authorized {
        Ok(())
    } else {
        Err("不允许从当前页面执行此操作".to_string())
    }
}

fn authorize_window_command(
    window: &tauri::WebviewWindow,
    command: NativeCommand,
) -> Result<(), String> {
    if window.label() != "main" {
        return Err("不允许从当前窗口执行此操作".to_string());
    }
    let url = window
        .url()
        .map_err(|error| format!("无法确认命令调用页面：{error}"))?;
    validate_native_command(
        command,
        &url,
        WEB_PROCESS.lock().unwrap().is_some(),
        startup_error_state(),
        &web_port(),
    )
}

fn start_and_store_web_process(app: &tauri::AppHandle) -> Result<(), String> {
    let port = web_port();
    let mut process = start_web_process(app)?;
    if let Err(error) = server_ready(&mut process, &port, Duration::from_secs(30)) {
        process.stop();
        return Err(error);
    }
    *WEB_PROCESS.lock().unwrap() = Some(process);
    *STARTUP_ERROR_STATE.lock().unwrap() = None;
    Ok(())
}

fn navigate_to_web_service(app: &tauri::AppHandle) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{}", web_port())
        .parse()
        .map_err(|error| format!("服务地址无效：{error}"))?;
    app.get_webview_window("main")
        .ok_or_else(|| "找不到主窗口".to_string())?
        .navigate(url)
        .map_err(|error| format!("无法打开 Web 服务：{error}"))
}

fn spawn_web_process(mut command: Command, source: String) -> Result<WebProcess, String> {
    command.stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .map_err(|error| format!("无法启动进程 {source}：{error}"))?;
    let stderr = match child.stderr.take() {
        Some(stderr) => stderr,
        None => {
            terminate_child(&mut child);
            return Err(format!("无法捕获 Web 服务标准错误输出：{source}"));
        }
    };
    let stderr_tail = Arc::new(Mutex::new(VecDeque::with_capacity(STDERR_TAIL_LIMIT)));
    let reader_tail = Arc::clone(&stderr_tail);
    let stderr_reader = std::thread::Builder::new()
        .name("web-service-stderr".to_string())
        .spawn(move || drain_stderr(stderr, reader_tail))
        .map_err(|error| {
            terminate_child(&mut child);
            format!("无法读取 Web 服务标准错误输出 {source}：{error}")
        })?;
    Ok(WebProcess {
        child,
        source,
        stderr_tail,
        stderr_reader: Some(stderr_reader),
    })
}

fn terminate_child(child: &mut Child) {
    let needs_kill = match child.try_wait() {
        Ok(Some(_)) => false,
        Ok(None) | Err(_) => true,
    };
    if needs_kill {
        let _ = child.kill();
    }
    let _ = child.wait();
}

fn drain_stderr(mut stderr: ChildStderr, tail: Arc<Mutex<VecDeque<u8>>>) {
    let mut buffer = [0u8; 4096];
    loop {
        match stderr.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(size) => append_stderr_tail(&tail, &buffer[..size]),
        }
    }
}

fn append_stderr_tail(tail: &Arc<Mutex<VecDeque<u8>>>, bytes: &[u8]) {
    let Ok(mut tail) = tail.lock() else {
        return;
    };
    for byte in bytes {
        if tail.len() == STDERR_TAIL_LIMIT {
            tail.pop_front();
        }
        tail.push_back(*byte);
    }
}

fn stderr_snapshot(tail: &Arc<Mutex<VecDeque<u8>>>) -> String {
    let Ok(tail) = tail.lock() else {
        return "无法读取 Web 服务标准错误输出".to_string();
    };
    String::from_utf8_lossy(&tail.iter().copied().collect::<Vec<_>>())
        .trim()
        .to_string()
}

impl WebProcess {
    fn stop(&mut self) {
        terminate_child(&mut self.child);
        self.finish_stderr();
    }

    fn finish_stderr(&mut self) {
        if let Some(reader) = self.stderr_reader.take() {
            let _ = reader.join();
        }
    }

    fn failure(&self, stage: &str, reason: String) -> String {
        let stderr = stderr_snapshot(&self.stderr_tail);
        let reason = if stderr.is_empty() {
            reason
        } else {
            format!("{reason}\n标准错误：{stderr}")
        };
        format!(
            "Web 服务启动失败\n阶段：{stage}\n命令：{}\n原因：{reason}",
            self.source
        )
    }
}

fn exit_status_description(status: ExitStatus) -> String {
    status
        .code()
        .map(|code| format!("退出码：{code}"))
        .unwrap_or_else(|| "进程被信号终止".to_string())
}

fn server_ready(process: &mut WebProcess, port: &str, timeout: Duration) -> Result<(), String> {
    let poll_interval = Duration::from_millis(100);
    let address = format!("127.0.0.1:{port}");
    let socket_address: SocketAddr = match address.parse() {
        Ok(address) => address,
        Err(error) => {
            return Err(process.failure("等待服务就绪", format!("服务地址无效：{error}")));
        }
    };
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        match process.child.try_wait() {
            Ok(Some(status)) => {
                process.finish_stderr();
                return Err(process.failure(
                    "等待服务就绪",
                    format!("进程提前退出（{}）", exit_status_description(status)),
                ));
            }
            Ok(None) => {}
            Err(error) => {
                return Err(process.failure("等待服务就绪", format!("检查进程状态失败：{error}")));
            }
        }

        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        if let Ok(mut stream) = TcpStream::connect_timeout(&socket_address, remaining) {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if !remaining.is_zero()
                && stream
                    .set_write_timeout(Some(remaining.min(poll_interval)))
                    .is_ok()
                && stream
                    .write_all(b"GET /api/v1/server/status HTTP/1.0\r\nHost: localhost\r\n\r\n")
                    .is_ok()
            {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if !remaining.is_zero()
                    && stream
                        .set_read_timeout(Some(remaining.min(poll_interval)))
                        .is_ok()
                {
                    let mut buffer = [0u8; 256];
                    if stream.read(&mut buffer).is_ok() {
                        let text = String::from_utf8_lossy(&buffer);
                        if text.contains(" 200 ") {
                            return Ok(());
                        }
                    }
                }
            }
        }

        let remaining = deadline.saturating_duration_since(Instant::now());
        if !remaining.is_zero() {
            std::thread::sleep(remaining.min(poll_interval));
        }
    }
    Err(process.failure(
        "等待服务就绪",
        format!("超时：无法连接 http://127.0.0.1:{port}"),
    ))
}

fn percent_encode_query(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(byte as char);
        } else {
            encoded.push('%');
            encoded.push(char::from(b"0123456789ABCDEF"[(byte >> 4) as usize]));
            encoded.push(char::from(b"0123456789ABCDEF"[(byte & 0x0F) as usize]));
        }
    }
    encoded
}

fn startup_error_url(error: &str) -> WebviewUrl {
    WebviewUrl::App(PathBuf::from(format!(
        "startup-error.html?error={}&allowReset={}",
        percent_encode_query(error),
        startup_error_allows_reset(error)
    )))
}

fn startup_error_navigation_url(error: &str, allow_reset: bool) -> Result<tauri::Url, String> {
    let origin = if cfg!(windows) {
        "http://tauri.localhost"
    } else {
        "tauri://localhost"
    };
    tauri::Url::parse(&format!(
        "{origin}/startup-error.html?error={}&allowReset={allow_reset}",
        percent_encode_query(error),
    ))
    .map_err(|error| format!("启动错误页地址无效：{error}"))
}

fn request_graceful_shutdown(port: &str, timeout: Duration) -> Result<(), String> {
    let address: SocketAddr = format!("127.0.0.1:{port}")
        .parse()
        .map_err(|error| format!("服务地址无效：{error}"))?;
    let mut stream = TcpStream::connect_timeout(&address, timeout)
        .map_err(|error| format!("无法请求服务安全停服：{error}"))?;
    stream
        .set_write_timeout(Some(timeout))
        .map_err(|error| format!("无法设置停服请求超时：{error}"))?;
    stream
        .set_read_timeout(Some(timeout))
        .map_err(|error| format!("无法设置停服响应超时：{error}"))?;
    stream
        .write_all(b"POST /api/v1/server/desktop-shutdown HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        .map_err(|error| format!("无法发送安全停服请求：{error}"))?;
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .map_err(|error| format!("读取安全停服响应失败：{error}"))?;
    let response = String::from_utf8_lossy(&response);
    let status = response.lines().next().unwrap_or("无状态行");
    if status.starts_with("HTTP/1.1 200 ") || status.starts_with("HTTP/1.0 200 ") {
        Ok(())
    } else {
        let body = response
            .split_once("\r\n\r\n")
            .map(|(_, body)| body)
            .unwrap_or("")
            .trim();
        Err(if body.is_empty() {
            format!("服务拒绝安全停服请求：{status}")
        } else {
            format!("服务拒绝安全停服请求：{status}：{body}")
        })
    }
}

fn graceful_stop_web_process(
    process: &mut WebProcess,
    port: &str,
    timeout: Duration,
) -> Result<(), String> {
    request_graceful_shutdown(port, timeout.min(Duration::from_secs(2)))?;
    let deadline = Instant::now() + timeout;
    loop {
        match process.child.try_wait() {
            Ok(Some(status)) if status.success() => {
                process.finish_stderr();
                return Ok(());
            }
            Ok(Some(status)) => {
                process.finish_stderr();
                return Err(process.failure(
                    "等待安全停服",
                    format!("服务退出失败（{}）", exit_status_description(status)),
                ));
            }
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(100).min(deadline - Instant::now()));
            }
            Ok(None) => return Err("服务已接受安全停服请求，但等待退出超时".to_string()),
            Err(error) => return Err(format!("检查服务停服状态失败：{error}")),
        }
    }
}

fn stop_web_process_for_relocation(port: &str) -> Result<(), String> {
    let current_process = WEB_PROCESS.lock().unwrap().take();
    let Some(mut process) = current_process else {
        return Err("找不到正在运行的 Web 服务进程".to_string());
    };
    match graceful_stop_web_process(&mut process, port, Duration::from_secs(15)) {
        Ok(()) => Ok(()),
        Err(error) => {
            *WEB_PROCESS.lock().unwrap() = Some(process);
            Err(error)
        }
    }
}

fn http_request(
    port: &str,
    path: &str,
    method: &str,
    body: Option<&str>,
) -> Result<Vec<u8>, String> {
    let mut stream = TcpStream::connect(format!("127.0.0.1:{port}"))
        .map_err(|error| format!("无法连接服务：{error}"))?;
    let body = body.unwrap_or("");
    let content_headers = if body.is_empty() {
        String::new()
    } else {
        format!(
            "Content-Type: application/json\r\nContent-Length: {}\r\n",
            body.len()
        )
    };
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n{content_headers}\r\n{body}"
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|error| format!("请求失败：{error}"))?;
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .map_err(|error| format!("读取响应失败：{error}"))?;
    let separator = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| "服务响应无效".to_string())?;
    let headers = String::from_utf8_lossy(&response[..separator]);
    if !headers.starts_with("HTTP/1.1 200") {
        let body = &response[separator + 4..];
        if !body.is_empty() {
            return Err(String::from_utf8_lossy(body).into_owned());
        }
        let status = headers.lines().next().unwrap_or("").to_string();
        return Err(format!("下载失败：{status}"));
    }
    Ok(response[separator + 4..].to_vec())
}

#[cfg(test)]
mod tests {
    use super::{
        allowed_webview_navigation, append_stderr_tail, data_root_command,
        graceful_stop_web_process, http_request, is_startup_error_url, is_web_service_url,
        managed_python_path, managed_runtime_root, python_command, request_graceful_shutdown,
        run_relocation_sequence, server_ready, spawn_web_process, startup_error_allows_reset,
        startup_error_navigation_url, stderr_snapshot, validate_data_root_reset,
        validate_native_command, with_lifecycle_lock, NativeCommand, RelocationError,
        STDERR_TAIL_LIMIT,
    };
    use std::collections::VecDeque;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::process::Command;
    use std::sync::{mpsc, Arc, Mutex};
    use std::time::{Duration, Instant};

    fn startup_error_test_url(query: &str) -> tauri::Url {
        let origin = if cfg!(windows) {
            "http://tauri.localhost"
        } else {
            "tauri://localhost"
        };
        tauri::Url::parse(&format!("{origin}/startup-error.html?{query}")).unwrap()
    }

    #[test]
    fn release_runtime_root_uses_tauri_resources_even_with_debug_override() {
        let root = managed_runtime_root(
            std::path::Path::new("/App/Contents/Resources"),
            Some(std::path::Path::new("/staging/runtime")),
            false,
        )
        .unwrap();

        assert_eq!(
            root,
            std::path::Path::new("/App/Contents/Resources/managed-runtime")
        );
    }

    #[test]
    fn debug_runtime_root_requires_and_uses_explicit_staging() {
        let resources = std::path::Path::new("/App/Contents/Resources");
        assert!(managed_runtime_root(resources, None, true).is_err());

        let staging = std::path::Path::new("/build/managed-runtime-dist");
        let root = managed_runtime_root(resources, Some(staging), true).unwrap();
        assert_eq!(root, staging);
        assert!(managed_python_path(&root).is_err());
    }

    #[test]
    fn managed_python_path_requires_staged_runtime() {
        let root = std::env::temp_dir().join(format!(
            "managed-runtime-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(root.join("bin")).unwrap();

        assert!(managed_python_path(&root).is_err());

        let python = root.join("bin/python3");
        std::fs::write(&python, "").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&python, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        assert_eq!(managed_python_path(&root).unwrap(), python);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn managed_python_command_runs_the_web_module_with_the_requested_port() {
        let command = python_command(std::path::Path::new("/runtime/bin/python3"), "9123");
        assert_eq!(command.get_program(), "/runtime/bin/python3");
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            ["-I", "-B", "-m", "app.web", "--port", "9123"]
        );
        for name in ["PYTHONPATH", "PYTHONHOME"] {
            assert_eq!(
                command
                    .get_envs()
                    .find(|(env_name, _)| *env_name == std::ffi::OsStr::new(name))
                    .map(|(_, value)| value),
                Some(None),
                "{name} must not leak into the bundled runtime"
            );
        }
    }

    #[test]
    fn data_root_helper_command_uses_managed_python_and_clears_python_overrides() {
        let command = data_root_command(
            std::path::Path::new("/runtime/bin/python3"),
            &["apply-pending"],
        );

        assert_eq!(command.get_program(), "/runtime/bin/python3");
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            ["-I", "-B", "-m", "app.data_root", "apply-pending"]
        );
        for name in ["PYTHONPATH", "PYTHONHOME"] {
            assert_eq!(
                command
                    .get_envs()
                    .find(|(env_name, _)| *env_name == std::ffi::OsStr::new(name))
                    .map(|(_, value)| value),
                Some(None),
                "{name} must not leak into the bundled runtime"
            );
        }
    }

    #[test]
    fn relocation_restarts_after_helper_failure_and_reports_both_errors() {
        let calls = std::cell::RefCell::new(Vec::new());
        let error = run_relocation_sequence(
            || {
                calls.borrow_mut().push("stop");
                Ok::<(), String>(())
            },
            || {
                calls.borrow_mut().push("helper");
                Err::<String, _>("relocation failed".to_string())
            },
            || {
                calls.borrow_mut().push("restart");
                Err("restart failed".to_string())
            },
        )
        .unwrap_err();

        assert_eq!(*calls.borrow(), ["stop", "helper", "restart"]);
        assert!(matches!(error, RelocationError::RecoveryRequired(_)));
        assert!(error.message().contains("relocation failed"));
        assert!(error.message().contains("restart failed"));
    }

    #[test]
    fn relocation_does_not_run_helper_when_graceful_stop_fails() {
        let calls = std::cell::RefCell::new(Vec::new());
        let error = run_relocation_sequence(
            || {
                calls.borrow_mut().push("stop");
                Err("graceful stop timed out".to_string())
            },
            || {
                calls.borrow_mut().push("helper");
                Ok::<String, String>("completed".to_string())
            },
            || {
                calls.borrow_mut().push("restart");
                Ok(())
            },
        )
        .unwrap_err();

        assert_eq!(*calls.borrow(), ["stop"]);
        assert!(matches!(error, RelocationError::ServiceAvailable(_)));
        assert!(error.message().contains("graceful stop timed out"));
    }

    #[test]
    fn relocation_preserves_successful_helper_output_when_restart_fails() {
        let error = run_relocation_sequence(
            || Ok(()),
            || Ok("{\"warning\":\"old directory retained\"}".to_string()),
            || Err("restart failed".to_string()),
        )
        .unwrap_err();

        assert!(matches!(error, RelocationError::RecoveryRequired(_)));
        assert!(error.message().contains("old directory retained"));
        assert!(error.message().contains("restart failed"));
    }

    #[test]
    fn data_root_reset_requires_confirmation_and_no_running_web_process() {
        assert!(validate_data_root_reset(false, false).is_err());
        assert!(validate_data_root_reset(true, true).is_err());
        assert!(validate_data_root_reset(true, false).is_ok());
    }

    #[test]
    fn data_root_reset_is_not_authorized_without_startup_error_state() {
        let error_page = startup_error_test_url("allowReset=true");
        assert!(
            validate_native_command(NativeCommand::Reset, &error_page, false, None, "8765",)
                .is_err()
        );
    }

    #[test]
    fn webview_navigation_allows_only_current_service_and_active_error_page() {
        let service = tauri::Url::parse("http://127.0.0.1:9123/").unwrap();
        let other_port = tauri::Url::parse("http://127.0.0.1:9124/").unwrap();
        let startup_error = startup_error_test_url("allowReset=false");

        assert!(allowed_webview_navigation(&service, "9123", true, None));
        assert!(!allowed_webview_navigation(&other_port, "9123", true, None));
        assert!(allowed_webview_navigation(
            &startup_error,
            "9123",
            false,
            Some(false)
        ));
        assert!(!allowed_webview_navigation(
            &startup_error,
            "9123",
            false,
            None
        ));
    }

    #[test]
    fn startup_error_url_requires_the_platform_app_origin_and_exact_path() {
        let (expected, other_platform_origin) = if cfg!(windows) {
            (
                "http://tauri.localhost/startup-error.html?error=broken",
                "tauri://localhost/startup-error.html?error=broken",
            )
        } else {
            (
                "tauri://localhost/startup-error.html?error=broken",
                "https://tauri.localhost/startup-error.html?error=broken",
            )
        };
        assert!(is_startup_error_url(&tauri::Url::parse(expected).unwrap()));
        assert!(!is_startup_error_url(
            &tauri::Url::parse(other_platform_origin).unwrap()
        ));
        assert!(!is_startup_error_url(
            &tauri::Url::parse("tauri://localhost/other.html").unwrap()
        ));
        assert!(!is_startup_error_url(
            &tauri::Url::parse("tauri://user@localhost/startup-error.html").unwrap()
        ));
    }

    #[test]
    fn relocation_recovery_url_is_trusted_and_preserves_reset_authorization() {
        let error = "invalid user data locator /custom";
        let url = startup_error_navigation_url(error, true).unwrap();

        assert!(is_startup_error_url(&url));
        assert!(url.query().unwrap().contains("allowReset=true"));
        assert!(
            validate_native_command(NativeCommand::Reset, &url, false, Some(true), "8765",).is_ok()
        );
    }

    #[test]
    fn web_service_origin_uses_the_configured_port() {
        let service = tauri::Url::parse("http://127.0.0.1:9123/").unwrap();
        let wrong_port = tauri::Url::parse("http://127.0.0.1:8765/").unwrap();
        let wrong_host = tauri::Url::parse("http://localhost:9123/").unwrap();

        assert!(is_web_service_url(&service, "9123"));
        assert!(!is_web_service_url(&wrong_port, "9123"));
        assert!(!is_web_service_url(&wrong_host, "9123"));
    }

    #[test]
    fn native_command_authorization_uses_page_and_service_lifecycle() {
        let service = tauri::Url::parse("http://127.0.0.1:9123/").unwrap();
        let error_page = startup_error_test_url("allowReset=true");

        assert!(
            validate_native_command(NativeCommand::Apply, &service, true, None, "9123",).is_ok()
        );
        assert!(validate_native_command(
            NativeCommand::Apply,
            &service,
            false,
            Some(false),
            "9123",
        )
        .is_err());
        assert!(validate_native_command(
            NativeCommand::Retry,
            &error_page,
            false,
            Some(false),
            "9123",
        )
        .is_ok());
        assert!(validate_native_command(
            NativeCommand::Retry,
            &error_page,
            true,
            Some(false),
            "9123",
        )
        .is_ok());
        assert!(validate_native_command(
            NativeCommand::Reset,
            &error_page,
            false,
            Some(false),
            "9123",
        )
        .is_err());
        assert!(validate_native_command(
            NativeCommand::Reset,
            &error_page,
            false,
            Some(true),
            "9123",
        )
        .is_ok());
        assert!(
            validate_native_command(NativeCommand::Retry, &service, true, None, "9123",).is_err()
        );
    }

    #[test]
    fn lifecycle_lock_serializes_operations() {
        let lifecycle = std::sync::Arc::new(std::sync::Mutex::new(()));
        let guard = lifecycle.lock().unwrap();
        let (started_tx, started_rx) = std::sync::mpsc::channel();
        let (finished_tx, finished_rx) = std::sync::mpsc::channel();
        let worker_lock = std::sync::Arc::clone(&lifecycle);
        let worker = std::thread::spawn(move || {
            started_tx.send(()).unwrap();
            with_lifecycle_lock(&worker_lock, || finished_tx.send(()).unwrap());
        });

        started_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(finished_rx.recv_timeout(Duration::from_millis(50)).is_err());
        drop(guard);
        finished_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        worker.join().unwrap();
    }

    #[test]
    fn locator_errors_allow_default_reset_but_plugin_errors_do_not() {
        assert!(startup_error_allows_reset("invalid user data locator /x"));
        assert!(startup_error_allows_reset(
            "custom user root is unavailable: /x"
        ));
        assert!(!startup_error_allows_reset(
            "plugin protocol version is unsupported"
        ));
    }

    #[test]
    fn http_request_preserves_error_response_body() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port().to_string();
        let payload = r#"{"error":"missing tag","code":"export_error","params":{"reason":"missing_target_language_tag"}}"#;
        let response = format!(
            "HTTP/1.1 400 Bad Request\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            payload.len(),
            payload,
        );
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 1024];
            let _ = stream.read(&mut request).unwrap();
            stream.write_all(response.as_bytes()).unwrap();
        });

        let error = http_request(&port, "/download", "GET", None).unwrap_err();

        server.join().unwrap();
        assert_eq!(error, payload);
    }

    #[test]
    fn stderr_tail_keeps_only_the_bounded_suffix() {
        let tail = Arc::new(Mutex::new(VecDeque::new()));
        let input = vec![b'x'; STDERR_TAIL_LIMIT + 3];

        append_stderr_tail(&tail, &input);

        assert_eq!(stderr_snapshot(&tail).len(), STDERR_TAIL_LIMIT);
        assert_eq!(stderr_snapshot(&tail), "x".repeat(STDERR_TAIL_LIMIT));
    }

    #[test]
    fn server_ready_reports_early_exit_and_captured_stderr() {
        let mut command = Command::new("/bin/sh");
        command.args([
            "-c",
            "printf 'plugin=/tmp/plugins/demo/plugin.toml: invalid protocol\\n' >&2; exit 7",
        ]);
        let mut process =
            spawn_web_process(command, "/bin/sh -c <web-service>".to_string()).unwrap();

        let error = server_ready(&mut process, "1", Duration::from_secs(2)).unwrap_err();

        assert!(error.contains("Web 服务启动失败"));
        assert!(error.contains("等待服务就绪"));
        assert!(error.contains("提前退出"));
        assert!(error.contains("plugin=/tmp/plugins/demo/plugin.toml"));
        assert!(error.contains("退出码：7"));
    }

    #[test]
    fn stopping_after_timeout_reaps_the_web_process() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "exec sleep 60"]);
        let mut process = spawn_web_process(command, "/bin/sh -c exec sleep".to_string()).unwrap();

        let error = server_ready(&mut process, "1", Duration::from_millis(1)).unwrap_err();
        assert!(error.contains("超时"));

        process.stop();

        assert!(process.child.try_wait().unwrap().is_some());
    }

    #[test]
    fn graceful_stop_requests_shutdown_and_waits_for_process_exit() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port().to_string();
        let (request_tx, request_rx) = mpsc::channel();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 512];
            let size = stream.read(&mut request).unwrap();
            request_tx
                .send(String::from_utf8_lossy(&request[..size]).into_owned())
                .unwrap();
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                .unwrap();
        });
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "sleep 0.1; exit 0"]);
        let mut process = spawn_web_process(command, "/bin/sh graceful exit".to_string()).unwrap();

        graceful_stop_web_process(&mut process, &port, Duration::from_secs(2)).unwrap();

        server.join().unwrap();
        let request = request_rx.recv().unwrap();
        assert!(request.starts_with("POST /api/v1/server/desktop-shutdown HTTP/1.1"));
        assert_eq!(process.child.try_wait().unwrap().unwrap().code(), Some(0));
    }

    #[test]
    fn graceful_stop_timeout_does_not_kill_the_web_process() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port().to_string();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 512];
            let _ = stream.read(&mut request).unwrap();
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                .unwrap();
        });
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "exec sleep 60"]);
        let mut process =
            spawn_web_process(command, "/bin/sh long-running service".to_string()).unwrap();

        let error =
            graceful_stop_web_process(&mut process, &port, Duration::from_millis(100)).unwrap_err();

        server.join().unwrap();
        assert!(error.contains("超时"));
        assert!(process.child.try_wait().unwrap().is_none());
        process.stop();
        assert!(process.child.try_wait().unwrap().is_some());
    }

    #[test]
    fn graceful_stop_rejects_server_conflict_response() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port().to_string();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 512];
            let _ = stream.read(&mut request).unwrap();
            stream
                .write_all(b"HTTP/1.1 409 Conflict\r\nContent-Length: 20\r\nConnection: close\r\n\r\nactive backend tasks")
                .unwrap();
        });

        let error = request_graceful_shutdown(&port, Duration::from_secs(1)).unwrap_err();

        server.join().unwrap();
        assert!(error.contains("409 Conflict"));
        assert!(error.contains("active backend tasks"));
    }

    #[test]
    fn server_ready_times_out_when_http_listener_does_not_respond() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port().to_string();
        let (accepted_tx, accepted_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let listener_thread = std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            accepted_tx.send(()).unwrap();
            release_rx.recv().unwrap();
            drop(stream);
        });

        let mut command = Command::new("/bin/sh");
        command.args(["-c", "exec sleep 60"]);
        let mut process = spawn_web_process(command, "/bin/sh -c exec sleep".to_string()).unwrap();
        let started = Instant::now();
        let error = server_ready(&mut process, &port, Duration::from_millis(100)).unwrap_err();

        assert!(error.contains("超时"));
        assert!(accepted_rx.recv_timeout(Duration::from_secs(1)).is_ok());
        assert!(started.elapsed() < Duration::from_secs(1));

        release_tx.send(()).unwrap();
        listener_thread.join().unwrap();
        process.stop();
    }

    #[test]
    fn server_ready_reports_child_exit_while_http_listener_stalls() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port().to_string();
        let (accepted_tx, accepted_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let listener_thread = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 1024];
            let _ = stream.read(&mut request).unwrap();
            accepted_tx.send(()).unwrap();
            release_rx.recv().unwrap();
        });

        let mut command = Command::new("/bin/sh");
        command.args([
            "-c",
            "sleep 0.1; printf 'PermissionError: app.log\\n' >&2; exit 9",
        ]);
        let mut process =
            spawn_web_process(command, "/bin/sh -c <web-service>".to_string()).unwrap();
        let started = Instant::now();
        let error = server_ready(&mut process, &port, Duration::from_secs(2)).unwrap_err();
        let elapsed = started.elapsed();

        assert!(accepted_rx.recv_timeout(Duration::from_secs(1)).is_ok());
        release_tx.send(()).unwrap();
        listener_thread.join().unwrap();

        assert!(elapsed < Duration::from_secs(1));
        assert!(error.contains("提前退出"), "{error}");
        assert!(error.contains("PermissionError: app.log"), "{error}");
        assert!(error.contains("退出码：9"), "{error}");
    }
}

#[tauri::command]
fn save_export(path: String, filename: String, body: Option<String>) -> Result<String, String> {
    let method = if body.is_some() { "POST" } else { "GET" };
    let bytes = http_request(&web_port(), &path, method, body.as_deref())?;
    let destination = rfd::FileDialog::new().set_file_name(&filename).save_file();
    let Some(destination) = destination else {
        return Ok(String::new());
    };
    let mut file =
        std::fs::File::create(&destination).map_err(|error| format!("无法创建文件：{error}"))?;
    file.write_all(&bytes)
        .map_err(|error| format!("写入文件失败：{error}"))?;
    Ok(destination.to_string_lossy().into_owned())
}

#[tauri::command]
fn select_file() -> Option<String> {
    rfd::FileDialog::new()
        .pick_file()
        .map(|path| path.to_string_lossy().into_owned())
}

#[tauri::command]
fn select_folder() -> Option<String> {
    rfd::FileDialog::new()
        .pick_folder()
        .map(|path| path.to_string_lossy().into_owned())
}

#[tauri::command]
fn apply_data_root_relocation(
    app: tauri::AppHandle,
    window: tauri::WebviewWindow,
) -> Result<String, String> {
    with_lifecycle_lock(&LIFECYCLE_OPERATION, || {
        authorize_window_command(&window, NativeCommand::Apply)?;
        match run_relocation_sequence(
            || stop_web_process_for_relocation(&web_port()),
            || run_data_root_helper(&app, &["apply-pending"]),
            || start_and_store_web_process(&app),
        ) {
            Ok(value) => Ok(value),
            Err(RelocationError::ServiceAvailable(error)) => Err(error),
            Err(RelocationError::RecoveryRequired(error)) => {
                let allow_reset = startup_error_allows_reset(&error);
                *STARTUP_ERROR_STATE.lock().unwrap() = Some(allow_reset);
                let url = startup_error_navigation_url(&error, allow_reset)?;
                window.navigate(url).map_err(|navigation_error| {
                    format!("{error}\n无法打开恢复页面：{navigation_error}")
                })?;
                Err(error)
            }
        }
    })
}

#[tauri::command]
fn retry_web_service(app: tauri::AppHandle, window: tauri::WebviewWindow) -> Result<(), String> {
    with_lifecycle_lock(&LIFECYCLE_OPERATION, || {
        authorize_window_command(&window, NativeCommand::Retry)?;
        let old_process = WEB_PROCESS.lock().unwrap().take();
        if let Some(mut process) = old_process {
            match process.child.try_wait() {
                Ok(Some(_)) => process.finish_stderr(),
                Ok(None) => {
                    *WEB_PROCESS.lock().unwrap() = Some(process);
                    return Err("Web 服务仍在安全退出，请稍后重试".to_string());
                }
                Err(error) => {
                    *WEB_PROCESS.lock().unwrap() = Some(process);
                    return Err(format!("检查旧 Web 服务状态失败：{error}"));
                }
            }
        }
        start_and_store_web_process(&app)?;
        navigate_to_web_service(&app)
    })
}

#[tauri::command]
fn reset_data_root(
    app: tauri::AppHandle,
    window: tauri::WebviewWindow,
    confirm: bool,
) -> Result<String, String> {
    with_lifecycle_lock(&LIFECYCLE_OPERATION, || {
        authorize_window_command(&window, NativeCommand::Reset)?;
        validate_data_root_reset(confirm, WEB_PROCESS.lock().unwrap().is_some())?;
        let result = run_relocation_sequence(
            || Ok(()),
            || run_data_root_helper(&app, &["reset", "--confirm"]),
            || start_and_store_web_process(&app),
        )
        .map_err(|error| error.message().to_string())?;
        navigate_to_web_service(&app)?;
        Ok(result)
    })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default().plugin(tauri_plugin_opener::init());
    #[cfg(debug_assertions)]
    let builder = builder.plugin(
        tauri_plugin_mcp_bridge::Builder::new()
            .bind_address("127.0.0.1")
            .build(),
    );
    builder
        .setup(|app| {
            let url = match start_and_store_web_process(&app.handle()) {
                Ok(()) => WebviewUrl::External(
                    format!("http://127.0.0.1:{}", web_port())
                        .parse()
                        .expect("invalid Web service URL"),
                ),
                Err(error) => {
                    let error = if error.contains("Web 服务启动失败") {
                        error
                    } else {
                        format!("Web 服务启动失败\n阶段：启动 Web 服务\n原因：{error}")
                    };
                    eprintln!("{error}");
                    *STARTUP_ERROR_STATE.lock().unwrap() = Some(startup_error_allows_reset(&error));
                    startup_error_url(&error)
                }
            };
            let port = web_port();
            let _ = tauri::WebviewWindowBuilder::new(app, "main", url)
                .title("译工坊")
                .inner_size(1280.0, 860.0)
                .on_navigation(move |url| {
                    allowed_webview_navigation(
                        url,
                        &port,
                        WEB_PROCESS.lock().unwrap().is_some(),
                        startup_error_state(),
                    )
                })
                .build();
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            select_file,
            select_folder,
            save_export,
            apply_data_root_relocation,
            retry_web_service,
            reset_data_root
        ])
        .build(tauri::generate_context!())
        .expect("failed to build tauri app")
        .run(|_app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(mut process) = WEB_PROCESS.lock().unwrap().take() {
                    process.stop();
                }
            }
        });
}
