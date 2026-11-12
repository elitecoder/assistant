import Foundation
import XCTest
@testable import SessionCore

/// Round-4 adversarial probes: the append-time orphan check, the pending-call refusal and the query bound.
final class ConversationStoreRound4AttackTests: XCTestCase {
    private var dir: URL!
    private var dbURL: URL { dir.appendingPathComponent("conversations.db") }

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("conv-r4-\(UUID().uuidString)")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func makeStore() throws -> ConversationStore { try ConversationStore(url: dbURL) }

    private func summary(_ text: String) -> ConversationMessage {
        ConversationMessage(sessionID: "s", seq: 0, role: .user, content: text, isSummary: true)
    }

    private func call(_ id: String) -> MessageDraft { MessageDraft(role: .assistant, content: "calling \(id)", toolName: "bash", toolCallID: id) }
    private func result(_ id: String?) -> MessageDraft { MessageDraft(role: .tool, content: "result \(id ?? "none")", toolName: "bash", toolCallID: id) }

    private func orphans(_ store: ConversationStore) throws -> [String] {
        var calls = Set<String>()
        var out: [String] = []
        for m in try store.resumeMessages(sessionID: "s") where m.toolCallID != nil {
            if m.role == .assistant { calls.insert(m.toolCallID!) } else if m.role == .tool && !calls.contains(m.toolCallID!) { out.append(m.toolCallID!) }
        }
        return out
    }

    private func fresh() throws -> ConversationStore {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "old one"), MessageDraft(role: .user, content: "old two")])
        return store
    }

    // MARK: Append check

    func testAResultBeforeItsCallInOneBatchIsRefusedAndNothingLands() throws {
        let store = try fresh()
        XCTAssertThrowsError(try store.append(sessionID: "s", [result("c1"), call("c1")]))
        XCTAssertEqual(try store.messages(sessionID: "s").count, 2)
        XCTAssertNoThrow(try store.append(sessionID: "s", [call("c1"), result("c1")]))
    }

    func testParallelCallsAnsweredOutOfOrderAcrossAppendsAreAccepted() throws {
        let store = try fresh()
        try store.append(sessionID: "s", [call("p1"), call("p2"), call("p3")])
        XCTAssertNoThrow(try store.append(sessionID: "s", [result("p3")]))
        XCTAssertNoThrow(try store.append(sessionID: "s", [result("p1"), result("p2")]))
        XCTAssertEqual(try orphans(store), [])
    }

    /// Decision (round 4): a result with an empty or missing call id is plain tool output, as Hermes allows.
    func testAResultWithNoCallIDIsAcceptedAsPlainToolOutput() throws {
        let store = try fresh()
        XCTAssertNoThrow(try store.append(sessionID: "s", [result(nil)]))
        XCTAssertNoThrow(try store.append(sessionID: "s", [result("")]))
        XCTAssertEqual(try store.messages(sessionID: "s").suffix(2).map(\.content), ["result none", "result "])
    }

    func testARangeCompactionCannotOrphanAResultOrLoseALateToolOutput() throws {
        let store = try fresh()
        try store.append(sessionID: "s", [call("r1")])
        do {
            try store.compact(sessionID: "s", seqRanges: [1...3], summary: "range summary")
        } catch {
            return
        }
        do {
            try store.append(sessionID: "s", [result("r1")])
        } catch {
            return XCTFail("compact(seqRanges:) archived a pending call, so its tool output can no longer be stored: \(error)")
        }
        XCTAssertEqual(try orphans(store), [])
    }

    func testARangeCompactionCannotSplitACallFromItsLiveResult() throws {
        let store = try fresh()
        try store.append(sessionID: "s", [call("k1"), result("k1"), MessageDraft(role: .assistant, content: "done")])
        do {
            try store.compact(sessionID: "s", seqRanges: [1...3], summary: "range summary")
        } catch {
            return
        }
        XCTAssertEqual(try orphans(store), [], "compact(seqRanges:) left a live tool result whose call it archived")
    }

    func testAResultRacingACompactionFromASecondStoreNeverOrphansOrIsLost() throws {
        let compactor = try fresh()
        let appender = try makeStore()
        let errors = Locked<[String]>([])
        let group = DispatchGroup()
        DispatchQueue.global().async(group: group) {
            for i in 0..<120 {
                do {
                    try appender.append(sessionID: "s", [self.call("x\(i)")])
                    try appender.append(sessionID: "s", [self.result("x\(i)")])
                } catch {
                    errors.mutate { $0.append("append \(i): \(error)") }
                }
            }
        }
        DispatchQueue.global().async(group: group) {
            for round in 0..<40 {
                do {
                    let live = try compactor.messages(sessionID: "s")
                    guard live.count > 6 else { continue }
                    try compactor.applyCompaction(sessionID: "s", newActive: [self.summary("r\(round)")] + live.suffix(4), watermark: live.last!.seq)
                } catch let error as ConversationStoreError {
                    if case .brokenToolPairing = error { continue }
                    errors.mutate { $0.append("compact: \(error)") }
                } catch {
                    errors.mutate { $0.append("compact: \(error)") }
                }
            }
        }
        group.wait()
        XCTAssertEqual(errors.value, [], "a tool result was refused or a compaction failed for another reason")
        XCTAssertEqual(try orphans(appender), [])
        let all = try appender.messages(sessionID: "s", scope: .display).map(\.content)
        XCTAssertEqual((0..<120).filter { !all.contains("result x\($0)") }, [], "tool outputs missing from history")
    }

    // MARK: Pending-call refusal

    func testAPendingCallThatNeverGetsAResultHasAnEscape() throws {
        let store = try fresh()
        try store.append(sessionID: "s", [call("hung")])
        let live = try store.messages(sessionID: "s")
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: [summary("sum")], watermark: live.last!.seq))
        XCTAssertNoThrow(try store.applyCompaction(sessionID: "s", newActive: [summary("sum carrying the call"), live.last!], watermark: live.last!.seq),
                         "carrying the pending call should be allowed")
        try store.append(sessionID: "s", [MessageDraft(role: .tool, content: "error: tool interrupted", toolName: "bash", toolCallID: "hung")])
        let after = try store.messages(sessionID: "s")
        XCTAssertNoThrow(try store.applyCompaction(sessionID: "s", newActive: [summary("final")], watermark: after.last!.seq),
                         "once a synthetic result answers the call, the pair can be archived")
        XCTAssertEqual(try store.messages(sessionID: "s").map(\.content), ["final"])
    }

    func testARewrittenPendingCallStillCountsAsLive() throws {
        let store = try fresh()
        try store.append(sessionID: "s", [call("rw")])
        var live = try store.messages(sessionID: "s")
        live[2].content = "calling rw (trimmed)"
        XCTAssertNoThrow(try store.applyCompaction(sessionID: "s", newActive: [summary("sum"), live[2]], watermark: live.last!.seq))
        XCTAssertNoThrow(try store.append(sessionID: "s", [result("rw")]))
        XCTAssertEqual(try orphans(store), [])
    }

    // MARK: Query bound

    func testHugeAndUnbalancedQueriesNeverThrowAndStayFast() throws {
        let store = try fresh()
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "needle in the haystack")])
        let queries = [String(repeating: "needle ", count: 20_000), "\"" + String(repeating: "needle ", count: 20_000),
                       String(repeating: "\"", count: 70_001), String(repeating: "a", count: 2_000_000), String(repeating: "\"needle\" ", count: 9_000),
                       String(repeating: "\u{1F680}", count: 70_000), String(repeating: "部", count: 70_000), String(repeating: "é", count: 70_000) + " needle"]
        for q in queries {
            let started = Date()
            for roles in [nil, [MessageRole.tool]] {
                XCTAssertNoThrow(try store.search(q, options: SearchOptions(roles: roles)), "\(q.prefix(20)) roles=\(String(describing: roles))")
            }
            XCTAssertLessThan(Date().timeIntervalSince(started), 5, "\(q.prefix(20)) took too long")
        }
        XCTAssertFalse(try store.search(String(repeating: "needle ", count: 20_000)).isEmpty)
        XCTAssertFalse(try store.search("\"" + String(repeating: "needle ", count: 20_000)).isEmpty,
                       "a stray opening quote before a long query emptied it")
    }
}
