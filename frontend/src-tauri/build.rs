fn main() {
    let dist = std::path::Path::new("../dist");
    if !dist.exists() {
        let _ = std::fs::create_dir_all(dist);
        let _ = std::fs::write(dist.join("index.html"), "<!DOCTYPE html><html><body></body></html>");
    }
    tauri_build::build()
}
