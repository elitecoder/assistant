import Foundation

/// Downloaded model files, checked by SHA-256 both when they arrive and before they're first loaded.
public enum ModelFiles {
    /// The files in `folder` that still need downloading: missing, or present with the wrong checksum. A file with the
    /// wrong checksum is deleted, so it downloads again.
    public static func needed(_ files: [KokoroFile], in folder: URL) -> [KokoroFile] {
        files.filter { file in
            let path = folder.appendingPathComponent(file.name)
            guard FileManager.default.fileExists(atPath: path.path) else { return true }
            if (try? Kokoro.sha256(of: path)) == file.sha256 { return false }
            try? FileManager.default.removeItem(at: path)
            return true
        }
    }

    /// Moves a finished download into `folder` only if the server answered 200 and its checksum matches; otherwise
    /// the download is thrown away.
    public static func accept(_ download: URL, as file: KokoroFile, status: Int, in folder: URL) -> Bool {
        defer { try? FileManager.default.removeItem(at: download) }
        guard status == 200, (try? Kokoro.sha256(of: download)) == file.sha256 else { return false }
        let target = folder.appendingPathComponent(file.name)
        try? FileManager.default.removeItem(at: target)
        return (try? FileManager.default.moveItem(at: download, to: target)) != nil
    }
}
