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

static WEB_PROCESS: Mutex<Option<WebProcess>> = Mutex::new(None);

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
    command.args(["-m", "app.web", "--port", port]);
    command
}

fn start_web_process(app: &tauri::AppHandle) -> Result<WebProcess, String> {
    let port = web_port();
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
    let python = managed_python_path(&runtime_root)?;
    let source = format!("{} -m app.web --port {port}", python.display());
    spawn_web_process(python_command(&python, &port), source)
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
                && stream.set_write_timeout(Some(remaining)).is_ok()
                && stream
                    .write_all(b"GET /api/v1/server/status HTTP/1.0\r\nHost: localhost\r\n\r\n")
                    .is_ok()
            {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if !remaining.is_zero() && stream.set_read_timeout(Some(remaining)).is_ok() {
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
            std::thread::sleep(remaining.min(Duration::from_millis(300)));
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
        "startup-error.html?error={}",
        percent_encode_query(error)
    )))
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
        append_stderr_tail, http_request, managed_python_path, managed_runtime_root,
        python_command, server_ready, spawn_web_process, stderr_snapshot, STDERR_TAIL_LIMIT,
    };
    use std::collections::VecDeque;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::process::Command;
    use std::sync::{mpsc, Arc, Mutex};
    use std::time::{Duration, Instant};

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
            ["-m", "app.web", "--port", "9123"]
        );
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
            let port = web_port();
            let url = match start_web_process(&app.handle()) {
                Ok(mut process) => {
                    match server_ready(&mut process, &port, Duration::from_secs(30)) {
                        Ok(()) => {
                            *WEB_PROCESS.lock().unwrap() = Some(process);
                            WebviewUrl::External(
                                format!("http://127.0.0.1:{port}")
                                    .parse()
                                    .expect("invalid Web service URL"),
                            )
                        }
                        Err(error) => {
                            process.stop();
                            eprintln!("{error}");
                            startup_error_url(&error)
                        }
                    }
                }
                Err(reason) => {
                    let error = format!("Web 服务启动失败\n阶段：启动 Web 服务\n原因：{reason}");
                    eprintln!("{error}");
                    startup_error_url(&error)
                }
            };
            let _ = tauri::WebviewWindowBuilder::new(app, "main", url)
                .title("译工坊")
                .inner_size(1280.0, 860.0)
                .build();
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            select_file,
            select_folder,
            save_export
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
