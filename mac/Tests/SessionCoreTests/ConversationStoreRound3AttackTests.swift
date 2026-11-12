import Foundation
import XCTest
@testable import SessionCore

/// Round-3 adversarial probes: the compaction watermark, tool pairing, archived originals and the reworked sanitizer.
final class ConversationStoreRound3AttackTests: XCTestCase {
    private var dir: URL!
    private var dbURL: URL { dir.appendingPathComponent("conversations.db") }

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("conv-r3-\(UUID().uuidString)")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func makeStore() throws -> ConversationStore { try ConversationStore(url: dbURL) }

    private func session(_ store: ConversationStore, count: Int = 10) throws {
        try store.createSession(title: "s", id: "s")
        for i in 1...count { try store.append(sessionID: "s", role: i % 2 == 0 ? .assistant : .user, content: "msg\(i) body") }
    }

    private func summary(_ text: String) -> ConversationMessage {
        ConversationMessage(sessionID: "s", seq: 0, role: .user, content: text, isSummary: true)
    }

    private func call(_ id: String) -> MessageDraft { MessageDraft(role: .assistant, content: "calling \(id)", toolName: "bash", toolCallID: id) }
    private func result(_ id: String) -> MessageDraft { MessageDraft(role: .tool, content: "result \(id)", toolName: "bash", toolCallID: id) }

    /// Live transcript is valid for a provider: every tool result follows a live call with its id.
    private func orphanResults(_ store: ConversationStore) throws -> [String] {
        var calls = Set<String>()
        var orphans: [String] = []
        for m in try store.resumeMessages(sessionID: "s") {
            guard let id = m.toolCallID else { continue }
            if m.role == .assistant { calls.insert(id) } else if m.role == .tool && !calls.contains(id) { orphans.append(id) }
        }
        return orphans
    }

    // MARK: Watermark

    func testAWatermarkAboveEverySeqBehavesLikeTheLastSeq() throws {
        let store = try makeStore()
        try session(store)
        let live = try store.messages(sessionID: "s")
        let applied = try store.applyCompaction(sessionID: "s", newActive: [summary("sum")] + live.suffix(2), watermark: Int.max)
        XCTAssertEqual(applied.archivedSeqs, Array(1...8))
        XCTAssertEqual(try store.messages(sessionID: "s").map(\.content), ["sum", "msg9 body", "msg10 body"])
    }

    func testAWatermarkBelowTheCarriedRowsIsRejectedRatherThanScramblingOrder() throws {
        let store = try makeStore()
        try session(store)
        let live = try store.messages(sessionID: "s")
        for watermark in [0, -5, Int.min, 4] {
            let before = try store.messages(sessionID: "s", scope: .all)
            do {
                try store.applyCompaction(sessionID: "s", newActive: [summary("sum w\(watermark)")] + live.suffix(2), watermark: watermark)
            } catch {
                XCTAssertEqual(try store.messages(sessionID: "s", scope: .all), before)
                continue
            }
            let order = try store.messages(sessionID: "s").map(\.content)
            XCTFail("watermark \(watermark) below the carried seqs was accepted; live order is now \(order)")
            return
        }
    }

    func testLateRowsFromASecondStoreDuringApplyCompactionStayLiveAndInOrder() throws {
        let compactor = try makeStore()
        try session(compactor, count: 30)
        let appender = try makeStore()
        let group = DispatchGroup()
        let errors = Locked<[String]>([])
        DispatchQueue.global().async(group: group) {
            for round in 0..<30 {
                do {
                    let live = try compactor.messages(sessionID: "s")
                    guard live.count > 4 else { continue }
                    try compactor.applyCompaction(sessionID: "s", newActive: [self.summary("r\(round) sum")] + live.suffix(live.count - 3),
                                                  watermark: live.last!.seq)
                } catch {
                    errors.mutate { $0.append("\(error)") }
                }
            }
        }
        DispatchQueue.global().async(group: group) {
            for i in 0..<300 {
                do { try appender.append(sessionID: "s", role: .user, content: "late\(i) text") } catch { errors.mutate { $0.append("\(error)") } }
            }
        }
        group.wait()
        XCTAssertEqual(errors.value, [])
        let lateOrder = try appender.messages(sessionID: "s").map(\.content).filter { $0.hasPrefix("late") }
            .map { Int($0.dropFirst(4).prefix { $0.isNumber })! }
        XCTAssertEqual(lateOrder, lateOrder.sorted(), "live late rows out of order")
        let display = try appender.messages(sessionID: "s", scope: .display).map(\.content)
        XCTAssertEqual(Set(display).count, display.count, "display repeats a message")
        for i in [0, 150, 299] { XCTAssertEqual(try appender.search("late\(i)").count, 1, "late\(i)") }
    }

    // MARK: Tool pairing

    func testParallelCallsWithResultsOutOfOrderAreAccepted() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "old turn"), MessageDraft(role: .user, content: "do two things"),
                                          call("c1"), call("c2"), result("c2"), result("c1")])
        let live = try store.messages(sessionID: "s")
        XCTAssertNoThrow(try store.applyCompaction(sessionID: "s", newActive: [summary("sum")] + live.suffix(5), watermark: live.last!.seq))
        XCTAssertEqual(try orphanResults(store), [])
    }

    func testArchivingACallWhoseResultIsStillPendingCannotLeaveAnOrphanResult() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "run it"), call("pending")])
        let live = try store.messages(sessionID: "s")
        do {
            try store.applyCompaction(sessionID: "s", newActive: [summary("sum")], watermark: live.last!.seq)
        } catch {
            return
        }
        try store.append(sessionID: "s", [result("pending")])
        XCTAssertEqual(try orphanResults(store), [], "the live transcript now holds a tool result whose call was archived")
        let next = try store.messages(sessionID: "s")
        XCTAssertNoThrow(try store.applyCompaction(sessionID: "s", newActive: next, watermark: next.last!.seq),
                         "carrying the live transcript unchanged is refused: the session can never be compacted while keeping it")
    }

    func testALateResultWhoseCallWasArchivedIsRefusedNotOrphaned() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "go"), call("c9")])
        let live = try store.messages(sessionID: "s")
        try store.append(sessionID: "s", [result("c9")])
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: [summary("sum")], watermark: live.last!.seq))
        XCTAssertEqual(try orphanResults(store), [])
    }

    // MARK: Archived originals

    func testARewrittenCarryIsFoundOnceInLiveSearchAndTheOriginalOnceAsHistory() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "first"),
                                          MessageDraft(role: .user, content: "keyword appears with a long tail of detail")])
        var live = try store.messages(sessionID: "s")
        live[1].content = "keyword appears (trimmed)"
        try store.applyCompaction(sessionID: "s", newActive: live, watermark: live.last!.seq)
        let hits = try store.search("keyword")
        XCTAssertEqual(hits.count, 2, "original and rewrite: \(hits.map(\.snippet))")
        XCTAssertEqual(hits.filter(\.active).count, 1)
        XCTAssertEqual(try store.search("tail").count, 1)
        let display = try store.messages(sessionID: "s", scope: .display).map(\.content)
        XCTAssertEqual(display.count, 3, "display shows first, the original and the rewrite: \(display)")
        live = try store.messages(sessionID: "s")
        live[1].content = "keyword (trimmed again)"
        try store.applyCompaction(sessionID: "s", newActive: live, watermark: live.last!.seq)
        XCTAssertEqual(try store.search("keyword").count, 3)
        XCTAssertEqual(try store.search("keyword").filter(\.active).count, 1)
    }

    // MARK: Sanitizer

    func testHyphensInsideWordsMatchTheWord() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        let words = ["x-ray scan", "e-mail sent", "pre-commit hook", "re-run job", "co-op mode", "a-b-c-d chain", "end- of line", "COVID-19 data"]
        for w in words { try store.append(sessionID: "t", role: .user, content: w) }
        var misses: [String] = []
        for (i, w) in words.enumerated() {
            for q in [w, String(w.split(separator: " ")[0]), "about \(w)"] where try !store.search(q).contains(where: { $0.seq == i + 1 }) {
                misses.append(q)
            }
        }
        XCTAssertEqual(misses, [])
    }

    func testCommandLineFlagsAreNotTurnedIntoExclusions() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        try store.append(sessionID: "t", role: .user, content: "I ran rm -rf build then git log -n 5 and ls -la in the repo")
        try store.append(sessionID: "t", role: .user, content: "the temperature dropped to -5 overnight")
        var misses: [String] = []
        for q in ["rm -rf build", "git log -n", "ls -la", "temperature -5", "dropped to -5"] where try store.search(q).isEmpty {
            misses.append(q)
        }
        XCTAssertEqual(misses, [], "a flag or negative number excluded the very message that contains it")
    }

    /// Hermes has no exclusion syntax, so a leading hyphen is literal: the dash goes and the phrase or word is required.
    func testAHyphenBeforeAPhraseOrTheFirstWordIsLiteral() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        try store.append(sessionID: "t", role: .user, content: "deploy to prod")
        try store.append(sessionID: "t", role: .user, content: "deploy to staging env")
        XCTAssertEqual(try store.search("deploy -\"staging env\"").map(\.seq), [2])
        XCTAssertEqual(try store.search("-staging deploy").map(\.seq), [2])
    }

    func testCapEdgeQueriesNeverThrowOnAnyRoute() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        try store.append(sessionID: "t", role: .tool, content: "w1 tool", toolName: "bash")
        try store.append(sessionID: "t", role: .user, content: "w1 user 部署")
        let words = (1...400).map { "w\($0)" }
        let queries = [words.joined(separator: " NOT "), words.joined(separator: " -"), words.joined(separator: " OR "),
                       words.map { "\"\($0) x\"" }.joined(separator: " NOT "), words.map { "\($0)*" }.joined(separator: " "),
                       (1...400).map { _ in "部署" }.joined(separator: " NOT "), String(repeating: "a*b*c* ", count: 300),
                       words.joined(separator: " AND NOT "), String(repeating: "- ", count: 1_000), String(repeating: "-a ", count: 700)]
        for q in queries {
            for roles in [nil, [MessageRole.tool], [MessageRole.user, .tool]] {
                XCTAssertNoThrow(try store.search(q, options: SearchOptions(roles: roles)), "\(q.prefix(40)) roles=\(String(describing: roles))")
            }
        }
        XCTAssertFalse(try store.search(words.joined(separator: " OR ")).isEmpty, "w1 is the first OR term")
    }
}
