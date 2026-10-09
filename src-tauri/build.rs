fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "select_file",
            "select_folder",
            "save_export",
            "apply_data_root_relocation",
            "retry_web_service",
            "reset_data_root",
        ]),
    ))
    .expect("failed to run tauri-build");
}
