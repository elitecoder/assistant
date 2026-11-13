import Foundation
import XCTest
@testable import SessionCore

func line(_ obj: [String: Any]) -> String {
    String(data: try! JSONSerialization.data(withJSONObject: obj), encoding: .utf8)!
}

func user(_ text: Any, at ts: String, extra: [String: Any] = [:]) -> String {
    line(["type": "user", "timestamp": ts, "message": ["content": text]].merging(extra) { $1 })
}

func assistant(_ text: String, at ts: String) -> String {
    line(["type": "assistant", "timestamp": ts, "message": ["content": [["type": "text", "text": text]]]])
}

func transcript(_ lines: [String]) -> Data { Data(lines.joined(separator: "\n").utf8) }

final class TranscriptTests: XCTestCase {
    func testSummaryKeepsOperatorMessagesAndDropsMachineOnes() {
        let s = Transcript.summarize(transcript([
            user("Fix the trim bug", at: "2026-11-01T10:00:00.000Z"),
            user([["type": "tool_result", "content": "ok"]], at: "2026-11-01T10:00:05.000Z"),
            user("<task-notification>done</task-notification>", at: "2026-11-01T10:00:06.000Z"),
            user("hidden", at: "2026-11-01T10:00:07.000Z", extra: ["isMeta": true]),
            user("Also add a test", at: "2026-11-01T10:01:00.000Z"),
            assistant("Workspace renamed → x", at: "2026-11-01T10:02:00.000Z"),
            assistant("Done. Should I open the PR?", at: "2026-11-01T10:03:00.000Z"),
            line(["type": "pr-link", "timestamp": "2026-11-01T10:04:00.000Z", "prUrl": "https://g/pr/7", "prNumber": 7]),
        ]))
        XCTAssertEqual(s.goal, "Fix the trim bug")
        XCTAssertEqual(s.lastAsk, "Also add a test")
        XCTAssertEqual(s.lastSaid, "Done. Should I open the PR?")
        XCTAssertEqual(s.pullRequests, ["https://g/pr/7": 7])
        XCTAssertEqual(s.lastActivity, Timestamps.parse("2026-11-01T10:04:00.000Z"))
    }

    func testMessageMatchRequiresSameSecondAndSameText() {
        let data = transcript([user("status? where are we", at: "2026-11-01T10:00:00.123Z")])
        XCTAssertTrue(Transcript.holdsMessage("status? where are we", submittedAt: "2026-11-01T10:00:00.900Z", in: data))
        XCTAssertFalse(Transcript.holdsMessage("something else", submittedAt: "2026-11-01T10:00:00.900Z", in: data))
        XCTAssertFalse(Transcript.holdsMessage("status? where are we", submittedAt: "2026-11-01T10:00:01.000Z", in: data))
        XCTAssertTrue(Transcript.holdsMessage("/title", submittedAt: "2026-11-01T10:00:00.000Z", in: data))
    }
}

final class ClassifierTests: XCTestCase {
    func summary(said: String, prs: [String: Int] = [:]) -> SessionSummary {
        var s = SessionSummary()
        s.goal = "goal"
        s.lastSaid = said
        s.pullRequests = prs
        return s
    }

    func testAskLinePicksLastQuestionEvenInsideAList() {
        let said = "Three things:\n5. **Rule:** can I remove the SSH rule?\nEverything else is restored."
        XCTAssertEqual(Classifier.askLine(said), "5. **Rule:** can I remove the SSH rule?")
    }

    func testStatusNeedsYouForQuestionOrWaitingPhrase() {
        XCTAssertEqual(Classifier.status(summary(said: "Should I start the two fixes?"), prStates: [:],
                                         idleMinutes: 120, threshold: 60), .needsYou)
        XCTAssertEqual(Classifier.status(summary(said: "gpt-oss is running. Tell me which way to go."), prStates: [:],
                                         idleMinutes: 120, threshold: 60), .needsYou)
        XCTAssertEqual(Classifier.status(summary(said: "Run passed in 11 minutes."), prStates: [:],
                                         idleMinutes: 120, threshold: 60), .idle)
    }

    func testStatusWorkingBeforeThreshold() {
        XCTAssertEqual(Classifier.status(summary(said: "Should I?"), prStates: [:], idleMinutes: 59, threshold: 60),
                       .working)
    }

    func testStatusDoneOnlyWhenEveryPRMerged() {
        let s = summary(said: "Want me to stop it?", prs: ["a": 1, "b": 2])
        XCTAssertEqual(Classifier.status(s, prStates: ["a": "MERGED", "b": "MERGED"], idleMinutes: 90, threshold: 60),
                       .done)
        XCTAssertEqual(Classifier.status(s, prStates: ["a": "MERGED", "b": "open, clean"], idleMinutes: 90,
                                         threshold: 60), .needsYou)
    }
}

final class LocatorTests: XCTestCase {
    func testFindsTheSessionWhoseMessageMatchesAmongSameSecondFiles() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let folder = root.appendingPathComponent("-work-repo")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let ts = "2026-11-01T10:00:00.500Z"
        try transcript([user("fix the version bump", at: ts)]).write(to: folder.appendingPathComponent("a.jsonl"))
        try transcript([user("what were we doing here?", at: ts),
                        assistant("Should I merge?", at: "2026-11-01T10:05:00.000Z")])
            .write(to: folder.appendingPathComponent("b.jsonl"))
        let list = Data("""
        {"workspaces":[{"id":"W1","ref":"workspace:1","title":"T","current_directory":"/work/repo",
        "latest_submitted_at":"\(ts)","latest_submitted_message":"what were we doing here?"}]}
        """.utf8)
        let ws = try XCTUnwrap(Workspace.parseList(list).first)
        let locator = TranscriptLocator(projects: root, home: "/nowhere")
        let path = try XCTUnwrap(locator.transcript(for: ws))
        XCTAssertEqual(URL(fileURLWithPath: path).lastPathComponent, "b.jsonl")
        XCTAssertEqual(locator.summary(at: path)?.lastSaid, "Should I merge?")
    }

    func testAWorkspaceWithNoTranscriptReadsOnlyTranscriptsThatChangeAfterItsFirstSearch() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let folder = root.appendingPathComponent("-work-repo")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let ts = "2026-11-01T10:00:00.500Z"
        try transcript([user("fix the version bump", at: ts)]).write(to: folder.appendingPathComponent("a.jsonl"))
        let list = Data("""
        {"workspaces":[{"id":"W1","ref":"workspace:1","title":"T","current_directory":"/work/repo",
        "latest_submitted_at":"\(ts)","latest_submitted_message":"what were we doing here?"}]}
        """.utf8)
        let ws = try XCTUnwrap(Workspace.parseList(list).first)
        // FSEvents can still report a write from just before the stream starts.
        Thread.sleep(forTimeInterval: 2)
        let reads = Counter()
        let locator = TranscriptLocator(projects: root, home: "/nowhere") { path in
            reads.add(URL(fileURLWithPath: path).lastPathComponent)
            return FileManager.default.contents(atPath: path)
        }
        XCTAssertNil(locator.transcript(for: ws))
        XCTAssertEqual(reads.names, ["a.jsonl"])
        Thread.sleep(forTimeInterval: 2)
        XCTAssertNil(locator.transcript(for: ws))
        XCTAssertEqual(reads.names, ["a.jsonl"], "a second lookup with nothing changed reads no transcript")

        try transcript([user("what were we doing here?", at: ts)]).write(to: folder.appendingPathComponent("b.jsonl"))
        var found: String?
        let deadline = Date().addingTimeInterval(10)
        while found == nil, Date() < deadline {
            Thread.sleep(forTimeInterval: 0.25)
            found = locator.transcript(for: ws)
        }
        XCTAssertEqual(found.map { URL(fileURLWithPath: $0).lastPathComponent }, "b.jsonl")
        XCTAssertEqual(reads.names, ["a.jsonl", "b.jsonl"], "only the new transcript is read")
    }
}

final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var list: [String] = []
    var names: [String] { lock.withLock { list } }
    func add(_ name: String) { lock.withLock { list.append(name) } }
}
