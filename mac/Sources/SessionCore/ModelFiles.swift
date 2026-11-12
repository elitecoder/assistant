import Foundation

/// Downloaded model files, checked by SHA-256 when they arrive and once more before they're first loaded. A checked
/// file's size, modification time, and checksum are recorded in a stamp file next to it, so later launches don't hash
/// hundreds of megabytes again: a file is hashed again only when its size or modification time changed.
public enum ModelFiles {
    static let stampName = ".verified.json"

    struct Stamp: Codable, Equatable {
        let size: UInt64
        let modified: TimeInterval
        let sha256: String
    }

    static func stamps(in folder: URL) -> [String: Stamp] {
        guard let data = FileManager.default.contents(atPath: folder.appendingPathComponent(stampName).path) else { return [:] }
        return (try? JSONDecoder().decode([String: Stamp].self, from: data)) ?? [:]
    }

    static func save(_ stamps: [String: Stamp], in folder: URL) {
        let encoder = JSONEncoder()
        encoder.outputFormatting = .sortedKeys
        try? encoder.encode(stamps).write(to: folder.appendingPathComponent(stampName), options: .atomic)
    }

    /// The size and modification time of `url` now, with `sha256` as its checksum.
    static func stamp(_ url: URL, sha256: String) -> Stamp? {
        guard let a = try? FileManager.default.attributesOfItem(atPath: url.path), let size = a[.size] as? UInt64,
              let modified = a[.modificationDate] as? Date else { return nil }
        return Stamp(size: size, modified: modified.timeIntervalSince1970, sha256: sha256)
    }

    /// The files in `folder` that still need downloading: missing, or present with the wrong checksum. A file with the
    /// wrong checksum is deleted, so it downloads again. A file whose stamp still matches isn't hashed.
    public static func needed(_ files: [KokoroFile], in folder: URL, hash: (URL) throws -> String = Kokoro.sha256) -> [KokoroFile] {
        var stamps = stamps(in: folder)
        let before = stamps
        let needed = files.filter { file in
            let path = folder.appendingPathComponent(file.name)
            guard FileManager.default.fileExists(atPath: path.path) else { return true }
            if let recorded = stamps[file.name], recorded == stamp(path, sha256: file.sha256) { return false }
            if (try? hash(path)) == file.sha256 {
                stamps[file.name] = stamp(path, sha256: file.sha256)
                return false
            }
            stamps[file.name] = nil
            try? FileManager.default.removeItem(at: path)
            return true
        }
        if stamps != before { save(stamps, in: folder) }
        return needed
    }

    /// Moves a finished download into `folder` only if the server answered 200 and its checksum matches; otherwise
    /// the download is thrown away. A kept file is stamped, so it isn't hashed again before it loads.
    public static func accept(_ download: URL, as file: KokoroFile, status: Int, in folder: URL) -> Bool {
        defer { try? FileManager.default.removeItem(at: download) }
        guard status == 200, (try? Kokoro.sha256(of: download)) == file.sha256 else { return false }
        let target = folder.appendingPathComponent(file.name)
        try? FileManager.default.removeItem(at: target)
        guard (try? FileManager.default.moveItem(at: download, to: target)) != nil else { return false }
        var stamps = stamps(in: folder)
        stamps[file.name] = stamp(target, sha256: file.sha256)
        save(stamps, in: folder)
        return true
    }
}
