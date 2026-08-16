use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::WebviewUrl;

static SIDECAR: Mutex<Option<Child>> = Mutex::new(None);

fn web_port() -> String {
    std::env::var("ANOTHER_LLM_WEB_PORT").unwrap_or_else(|_| "8765".into())
}

fn bundled_sidecar() -> Option<PathBuf> {
    let exe_dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
    let resources = exe_dir
        .parent()?
        .join("Resources")
        .join("_up_")
        .join("sidecar-dist")
        .join("translator-sidecar")
        .join("translator-sidecar");
    resources.is_file().then_some(resources)
}

fn start_sidecar() -> Option<Child> {
    let port = web_port();
    if let Some(executable) = bundled_sidecar() {
        return Command::new(executable)
            .args(["--port", &port])
            .spawn()
            .ok();
    }
    let python =
        std::env::var("ANOTHER_LLM_PYTHON").unwrap_or_else(|_| "python3".into());
    let mut command = Command::new(python);
    command.args(["-m", "app.web", "--port", &port]);
    if let Ok(root) = std::env::var("ANOTHER_LLM_REPO_ROOT") {
        command.current_dir(root);
    }
    command.spawn().ok()
}

fn server_ready(port: &str, timeout: Duration) -> bool {
    let address = format!("127.0.0.1:{port}");
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if let Ok(mut stream) = TcpStream::connect(&address) {
            let _ = stream.write_all(
                b"GET /api/v1/server/status HTTP/1.0\r\nHost: localhost\r\n\r\n",
            );
            let mut buffer = [0u8; 256];
            if stream.read(&mut buffer).is_ok() {
                let text = String::from_utf8_lossy(&buffer);
                if text.contains(" 200 ") {
                    return true;
                }
            }
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

fn http_get(port: &str, path: &str) -> Result<Vec<u8>, String> {
    let mut stream = TcpStream::connect(format!("127.0.0.1:{port}"))
        .map_err(|error| format!("无法连接服务：{error}"))?;
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
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
        let status = headers.lines().next().unwrap_or("").to_string();
        return Err(format!("下载失败：{status}"));
    }
    Ok(response[separator + 4..].to_vec())
}

#[tauri::command]
fn save_export(path: String, filename: String) -> Result<String, String> {
    let bytes = http_get(&web_port(), &path)?;
    let destination = rfd::FileDialog::new()
        .set_file_name(&filename)
        .save_file();
    let Some(destination) = destination else {
        return Ok(String::new());
    };
    let mut file = std::fs::File::create(&destination)
        .map_err(|error| format!("无法创建文件：{error}"))?;
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
    let mut builder = tauri::Builder::default();
    #[cfg(debug_assertions)]
    {
        builder = builder.plugin(
            tauri_plugin_mcp_bridge::Builder::new()
                .bind_address("127.0.0.1")
                .build(),
        );
    }
    builder
        .setup(|app| {
            let port = web_port();
            *SIDECAR.lock().unwrap() = start_sidecar();
            if !server_ready(&port, Duration::from_secs(30)) {
                eprintln!("sidecar 启动超时：无法连接 http://127.0.0.1:{port}");
            }
            let url = format!("http://127.0.0.1:{port}")
                .parse()
                .expect("invalid sidecar URL");
            let _ = tauri::WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
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
                if let Some(mut child) = SIDECAR.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}
