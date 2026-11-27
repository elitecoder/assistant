import Foundation
import XCTest

/// Runs the real make-signing-cert.sh and sign-app.sh against a throwaway keychain.
final class SigningTests: XCTestCase {
    private let mac = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent()
    private var dir: URL!
    private var keychain: String { dir.appendingPathComponent("test.keychain-db").path }

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("signing-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try run("/usr/bin/security", ["create-keychain", "-p", "", keychain])
        try run("/usr/bin/security", ["unlock-keychain", "-p", "", keychain])
    }

    override func tearDownWithError() throws {
        _ = try? run("/usr/bin/security", ["delete-keychain", keychain])
        try? FileManager.default.removeItem(at: dir)
    }

    @discardableResult
    private func run(_ tool: String, _ args: [String], env: [String: String] = [:]) throws -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tool)
        p.arguments = args
        p.environment = ProcessInfo.processInfo.environment.merging(env) { $1 }
        let out = Pipe()
        p.standardOutput = out
        p.standardError = out
        try p.run()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let text = String(decoding: data, as: UTF8.self)
        guard p.terminationStatus == 0 else {
            throw NSError(domain: tool, code: Int(p.terminationStatus), userInfo: [NSLocalizedDescriptionKey: text])
        }
        return text
    }

    private func makeCertificate() throws {
        try run(mac.appendingPathComponent("make-signing-cert.sh").path, ["--keychain", keychain])
        try run("/usr/bin/security", ["set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", "", keychain])
    }

    /// Builds a minimal app whose executable differs per `version`, signs it with sign-app.sh,
    /// and returns the designated requirement macOS uses to recognize it.
    private func signedRequirement(version: Int) throws -> String {
        let app = dir.appendingPathComponent("v\(version)/Assistant.app")
        let macos = app.appendingPathComponent("Contents/MacOS")
        try FileManager.default.createDirectory(at: macos, withIntermediateDirectories: true)
        let source = dir.appendingPathComponent("main\(version).c")
        try "int build = \(version); int main(void) { return build - \(version); }\n"
            .write(to: source, atomically: true, encoding: .utf8)
        try run("/usr/bin/cc", ["-o", macos.appendingPathComponent("AssistantBar").path, source.path])
        try FileManager.default.copyItem(at: mac.appendingPathComponent("Info.plist"),
                                         to: app.appendingPathComponent("Contents/Info.plist"))
        try run(mac.appendingPathComponent("sign-app.sh").path, [app.path], env: ["ASSISTANT_SIGN_KEYCHAIN": keychain])
        try run("/usr/bin/codesign", ["--verify", "--strict", "--deep", app.path])
        let shown = try run("/usr/bin/codesign", ["-d", "-r-", app.path])
        return try XCTUnwrap(shown.split(separator: "\n").first { $0.contains("designated =>") }.map(String.init))
    }

    func testBuildsSignedWithTheCertificateKeepTheSameIdentity() throws {
        try makeCertificate()
        let first = try signedRequirement(version: 1)
        let second = try signedRequirement(version: 2)
        XCTAssertTrue(first.contains("identifier \"com.assistant.sessions\" and certificate leaf = H\""), first)
        XCTAssertEqual(first, second)
    }

    func testWithoutTheCertificateEachBuildGetsANewIdentity() throws {
        let first = try signedRequirement(version: 1)
        let second = try signedRequirement(version: 2)
        XCTAssertTrue(first.contains("cdhash"), first)
        XCTAssertNotEqual(first, second)
    }

    func testMakingTheCertificateTwiceKeepsTheFirstOne() throws {
        try makeCertificate()
        let first = try signedRequirement(version: 1)
        let again = try run(mac.appendingPathComponent("make-signing-cert.sh").path, ["--keychain", keychain])
        XCTAssertTrue(again.contains("already"), again)
        XCTAssertEqual(try signedRequirement(version: 2), first)
    }
}
