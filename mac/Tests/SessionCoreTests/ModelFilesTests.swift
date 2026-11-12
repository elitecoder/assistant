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

    /// Every model downloads from a pinned commit or revision, never a moving branch.
    func testModelURLsArePinned() {
        for file in Kokoro.files + SmartTurn.files {
            XCTAssertFalse(file.url.path.contains("/main/") || file.url.path.contains("/master/"), file.url.absoluteString)
            XCTAssertEqual(file.sha256.count, 64)
        }
        XCTAssertTrue(Kokoro.files.allSatisfy { $0.url.path.contains("9dcd3b06468a3c1ecee6d09a33ca687c8e708566") })
    }
}
