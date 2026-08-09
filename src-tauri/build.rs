fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "select_file",
                "select_folder",
                "save_export",
            ]),
        ),
    )
    .expect("failed to run tauri-build");
}
