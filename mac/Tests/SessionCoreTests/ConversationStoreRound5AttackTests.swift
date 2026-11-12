import Foundation
import XCTest
@testable import SessionCore

/// Round-5 adversarial probes: range compaction rerouted through applyCompaction, and the LIKE term cap.
final class ConversationStoreRound5AttackTests: XCTestCase {
    private var dir: URL!
    private var dbURL: URL { dir.appendingPathComponent("conversations.db") }

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("conv-r5-\(UUID().uuidString)")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func store(count: Int = 10) throws -> ConversationStore {
        let store = try ConversationStore(url: dbURL)
        try store.createSession(title: "s", id: "s")
        for i in 1...count { try store.append(sessionID: "s", role: i % 2 == 0 ? .assistant : .user, content: "row\(i) body") }
        return store
    }

    private func liveContents(_ s: ConversationStore) throws -> [String] { try s.messages(sessionID: "s").map(\.content) }

    private func assertEachFoundOnce(_ s: ConversationStore, _ n: Int, file: StaticString = #filePath, line: UInt = #line) {
        for i in 1...n {
            XCTAssertEqual(try s.search("row\(i)").count, 1, "row\(i)", file: file, line: line)
        }
    }

    func testOverlappingAndAdjacentRangesArchiveEachRowOnce() throws {
        let s = try store()
        let result = try s.compact(sessionID: "s", seqRanges: [2...5, 4...7, 8...8], summary: "sum A")
        XCTAssertEqual(try liveContents(s), ["row1 body", "sum A", "row9 body", "row10 body"])
        XCTAssertEqual(try s.messages(sessionID: "s").first?.seq, 1)
        for (old, new) in result.resequenced {
            XCTAssertEqual(try s.messages(sessionID: "s", scope: .all).first { $0.seq == new }?.content, "row\(old) body")
        }
        assertEachFoundOnce(s, 10)
        XCTAssertEqual(try s.messages(sessionID: "s", scope: .display).count, 11)
    }

    func testEmptyAndMissRangesThrowAndChangeNothing() throws {
        let s = try store()
        let before = try s.messages(sessionID: "s", scope: .all)
        XCTAssertThrowsError(try s.compact(sessionID: "s", seqRanges: [], summary: "x"))
        XCTAssertThrowsError(try s.compact(sessionID: "s", seqRanges: [50...60, -5...0], summary: "x"))
        XCTAssertEqual(try s.messages(sessionID: "s", scope: .all), before)
    }

    func testDisjointRangesKeepReadingOrder() throws {
        let s = try store()
        try s.compact(sessionID: "s", seqRanges: [7...8, 2...3], summary: "sum B")
        XCTAssertEqual(try liveContents(s), ["row1 body", "sum B", "row4 body", "row5 body", "row6 body", "row9 body", "row10 body"])
        let display = try s.messages(sessionID: "s", scope: .display).map(\.content)
        XCTAssertEqual(Set(display).count, display.count)
        assertEachFoundOnce(s, 10)
    }

    func testRangesCoveringAPendingCallOrSplittingAPairThrowAndChangeNothing() throws {
        let s = try store(count: 2)
        try s.append(sessionID: "s", [MessageDraft(role: .assistant, content: "call c", toolName: "bash", toolCallID: "c"),
                                      MessageDraft(role: .assistant, content: "call p", toolName: "bash", toolCallID: "p")])
        try s.append(sessionID: "s", [MessageDraft(role: .tool, content: "result c", toolName: "bash", toolCallID: "c")])
        let before = try s.messages(sessionID: "s", scope: .all)
        XCTAssertThrowsError(try s.compact(sessionID: "s", seqRanges: [1...4], summary: "covers pending p"))
        XCTAssertThrowsError(try s.compact(sessionID: "s", seqRanges: [3...3], summary: "splits c from its result"))
        XCTAssertThrowsError(try s.compact(sessionID: "s", seqRanges: [5...5], summary: "drops c's result"))
        XCTAssertEqual(try s.messages(sessionID: "s", scope: .all), before)
        XCTAssertNoThrow(try s.compact(sessionID: "s", seqRanges: [1...2], summary: "plain rows only"))
        XCTAssertNoThrow(try s.append(sessionID: "s", [MessageDraft(role: .tool, content: "result p", toolName: "bash", toolCallID: "p")]))
    }

    func testRangesCoveringEarlierSummariesAndLateAppends() throws {
        let s = try store()
        let first = try s.compact(sessionID: "s", seqRanges: [1...4], summary: "digestone text")
        try s.append(sessionID: "s", role: .user, content: "row11 body")
        let live = try s.messages(sessionID: "s")
        try s.compact(sessionID: "s", seqRanges: [first.summary.seq...live[2].seq], summary: "digesttwo text")
        try s.append(sessionID: "s", role: .user, content: "row12 body")
        let last = try s.messages(sessionID: "s").last!.seq
        try s.compact(sessionID: "s", seqRange: Int.min...last, summary: "digestthree text")
        XCTAssertEqual(try liveContents(s), ["digestthree text"])
        assertEachFoundOnce(s, 12)
        for d in ["digestone", "digesttwo", "digestthree"] { XCTAssertEqual(try s.search(d).count, 1, d) }
        let display = try s.messages(sessionID: "s", scope: .display).map(\.content)
        XCTAssertEqual(Set(display).count, display.count)
        XCTAssertEqual(display.count, 15)
    }

    func testLikeTermsNearTheByteLimitNeverThrowAndNeverMatchEverything() throws {
        let s = try store(count: 2)
        try s.append(sessionID: "s", [MessageDraft(role: .tool, content: "unrelated tool output", toolName: "bash")])
        let family = "\u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467}\u{200D}\u{1F466}"
        let terms = [String(repeating: "\u{1F680}", count: 13_000), String(repeating: "部", count: 17_000), String(repeating: "é", count: 26_000),
                     String(repeating: family, count: 3_000), String(repeating: family, count: 2_200), String(repeating: "%", count: 30_000), String(repeating: "\\", count: 30_000),
                     "zzz" + String(repeating: "\u{301}", count: 30_000), String(repeating: "_", count: 30_000)]
        var problems: [String] = []
        for term in terms {
            for roles in [nil, [MessageRole.tool]] {
                do {
                    let hits = try s.search(term, options: SearchOptions(roles: roles))
                    if !hits.isEmpty { problems.append("\(term.prefix(3)) x\(term.count) roles=\(String(describing: roles)) matched \(hits.count) unrelated rows") }
                } catch {
                    problems.append("\(term.prefix(3)) x\(term.count) threw \(error)")
                }
            }
        }
        XCTAssertEqual(problems, [])
    }
}
