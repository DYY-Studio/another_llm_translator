use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::WebviewUrl;

static SIDECAR: Mutex<Option<Child>> = Mutex::new(None);

fn web_port() -> String {
    std::env::var("MINIMAL_LLM_WEB_PORT").unwrap_or_else(|_| "8765".into())
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
            .args(["--host", "0.0.0.0", "--port", &port])
            .spawn()
            .ok();
    }
    let python =
        std::env::var("MINIMAL_LLM_PYTHON").unwrap_or_else(|_| "python3".into());
    let mut command = Command::new(python);
    command.args(["-m", "app.web", "--host", "0.0.0.0", "--port", &port]);
    if let Ok(root) = std::env::var("MINIMAL_LLM_REPO_ROOT") {
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
    tauri::Builder::default()
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
        .invoke_handler(tauri::generate_handler![select_file, select_folder])
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
