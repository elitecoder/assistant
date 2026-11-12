import XCTest
@testable import SessionCore

final class ModelFilesTests: XCTestCase {
    var folder: URL!

    override func setUpWithError() throws {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("model-files-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: folder) }

    func file(_ name: String, _ contents: String) throws -> (KokoroFile, URL) {
        let url = folder.appendingPathComponent("download-\(UUID().uuidString)")
        try Data(contents.utf8).write(to: url)
        let sha = try Kokoro.sha256(of: url)
        return (KokoroFile(name: name, url: URL(string: "https://example.com/\(name)")!, sha256: sha), url)
    }

    func testADownloadWithTheRightChecksumIsKept() throws {
        let (model, download) = try file("model.onnx", "weights")
        XCTAssertTrue(ModelFiles.accept(download, as: model, status: 200, in: folder))
        XCTAssertTrue(FileManager.default.fileExists(atPath: folder.appendingPathComponent("model.onnx").path))
        XCTAssertEqual(ModelFiles.needed([model], in: folder), [])
    }

    func testAWrongChecksumOrStatusIsRefusedAndThrownAway() throws {
        let (model, _) = try file("model.onnx", "weights")
        let tampered = folder.appendingPathComponent("tampered")
        try Data("not the weights".utf8).write(to: tampered)
        XCTAssertFalse(ModelFiles.accept(tampered, as: model, status: 200, in: folder))
        XCTAssertFalse(FileManager.default.fileExists(atPath: tampered.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: folder.appendingPathComponent("model.onnx").path))
        let (_, good) = try file("model.onnx", "weights")
        XCTAssertFalse(ModelFiles.accept(good, as: model, status: 404, in: folder))
    }

    /// A damaged file already on disk is deleted and listed for download again, as is a missing one.
    func testADamagedFileOnDiskIsDownloadedAgain() throws {
        let (model, _) = try file("model.onnx", "weights")
        let (vad, _) = try file("vad.onnx", "vad")
        try Data("damaged".utf8).write(to: folder.appendingPathComponent("model.onnx"))
        XCTAssertEqual(ModelFiles.needed([model, vad], in: folder), [model, vad])
        XCTAssertFalse(FileManager.default.fileExists(atPath: folder.appendingPathComponent("model.onnx").path))
    }

    /// A file is hashed once per install: its stamp (size, modification time, checksum) skips the hash on later
    /// checks, and a changed or damaged file is hashed again.
    func testAStampedFileIsHashedOncePerInstall() throws {
        let (model, download) = try file("model.onnx", "weights")
        XCTAssertTrue(ModelFiles.accept(download, as: model, status: 200, in: folder))
        var hashed = 0
        let counting: (URL) throws -> String = { hashed += 1; return try Kokoro.sha256(of: $0) }
        XCTAssertEqual(ModelFiles.needed([model], in: folder, hash: counting), [])
        XCTAssertEqual(ModelFiles.needed([model], in: folder, hash: counting), [])
        XCTAssertEqual(hashed, 0, "the download was stamped when it arrived")
        let path = folder.appendingPathComponent("model.onnx")
        let touched = Date(timeIntervalSinceNow: 60)
        try FileManager.default.setAttributes([.modificationDate: touched], ofItemAtPath: path.path)
        XCTAssertEqual(ModelFiles.needed([model], in: folder, hash: counting), [])
        XCTAssertEqual(hashed, 1, "touched: hashed again, then stamped")
        XCTAssertEqual(ModelFiles.needed([model], in: folder, hash: counting), [])
        XCTAssertEqual(hashed, 1)
        try Data("damaged".utf8).write(to: path)
        XCTAssertEqual(ModelFiles.needed([model], in: folder, hash: counting), [model])
        XCTAssertEqual(hashed, 2)
        XCTAssertFalse(FileManager.default.fileExists(atPath: path.path))
        try Data("damaged".utf8).write(to: path)
        try FileManager.default.setAttributes([.modificationDate: touched], ofItemAtPath: path.path)
        XCTAssertEqual(ModelFiles.needed([model], in: folder, hash: counting), [model],
                       "a damaged file's stamp is dropped, so a same-size file with its old time is still checked")
    }

    /// A file put in place without a stamp (an older install) is hashed once and stamped.
    func testAnUnstampedFileIsHashedOnceThenStamped() throws {
        let (model, download) = try file("model.onnx", "weights")
        try FileManager.default.moveItem(at: download, to: folder.appendingPathComponent("model.onnx"))
        var hashed = 0
        let counting: (URL) throws -> String = { hashed += 1; return try Kokoro.sha256(of: $0) }
        _ = ModelFiles.needed([model], in: folder, hash: counting)
        _ = ModelFiles.needed([model], in: folder, hash: counting)
        XCTAssertEqual(hashed, 1)
    }

    /// Every model downloads from a pinned commit or revision, never a moving branch.
    func testModelURLsArePinned() {
        for file in Kokoro.files + SmartTurn.files {
            XCTAssertFalse(file.url.path.contains("/main/") || file.url.path.contains("/master/"), file.url.absoluteString)
            XCTAssertEqual(file.sha256.count, 64)
        }
        XCTAssertTrue(Kokoro.files.allSatisfy { $0.url.path.contains("9dcd3b06468a3c1ecee6d09a33ca687c8e708566") })
    }
}
