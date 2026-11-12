import Foundation
import XCTest
@testable import SessionCore

/// Round-2 adversarial probes: `applyCompaction` and the rewritten FTS sanitizer. Calls pass the `watermark` the API now requires.
final class ConversationStoreRound2AttackTests: XCTestCase {
    private var dir: URL!
    private var dbURL: URL { dir.appendingPathComponent("conversations.db") }

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("conv-r2-\(UUID().uuidString)")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func makeStore() throws -> ConversationStore { try ConversationStore(url: dbURL) }

    private func raw() throws -> Connection { try Connection(path: dbURL.path) }

    private func session(_ store: ConversationStore, _ id: String = "s", count: Int = 10) throws {
        try store.createSession(title: id, id: id)
        for i in 1...count { try store.append(sessionID: id, role: i % 2 == 0 ? .assistant : .user, content: "\(id)word\(i) body") }
    }

    private func summary(_ text: String) -> ConversationMessage {
        ConversationMessage(sessionID: "s", seq: 0, role: .user, content: text, isSummary: true)
    }

    private func integrity(file: StaticString = #filePath, line: UInt = #line) {
        for table in ["messages_fts", "messages_fts_trigram"] {
            XCTAssertNoThrow(try raw().exec("INSERT INTO \(table)(\(table)) VALUES('integrity-check')"), table, file: file, line: line)
        }
    }

    private func tool(_ json: String, _ store: ConversationStore, current: String? = nil) -> SessionSearchResult {
        SessionSearchTool.run(json: Data(json.utf8), store: store, currentSessionID: current)
    }

    // MARK: applyCompaction

    func testAMessageAppendedAfterTheCompactorsSnapshotStaysLive() throws {
        let store = try makeStore()
        try session(store)
        let snapshot = try store.messages(sessionID: "s")
        try store.append(sessionID: "s", role: .user, content: "late question arrives")
        do {
            try store.applyCompaction(sessionID: "s", newActive: [summary("sum")] + snapshot.suffix(2), watermark: snapshot.last!.seq)
        } catch {
            return
        }
        XCTAssertTrue(try store.messages(sessionID: "s").map(\.content).contains("late question arrives"),
                      "a message appended during summarization was silently dropped from the live context")
    }

    func testConcurrentCompactorOnOneStoreAndAppenderOnAnotherNeverDropsALiveMessage() throws {
        let compactorStore = try makeStore()
        try session(compactorStore, count: 20)
        let appenderStore = try makeStore()
        let group = DispatchGroup()
        let errors = Locked<[String]>([])
        let compacted = Locked<Set<String>>([])
        DispatchQueue.global().async(group: group) {
            for round in 0..<40 {
                do {
                    let live = try compactorStore.messages(sessionID: "s")
                    guard live.count > 3 else { continue }
                    compacted.mutate { $0.formUnion(live.prefix(2).map(\.content)) }
                    try compactorStore.applyCompaction(sessionID: "s", newActive: [self.summary("round\(round) sum")] + live.dropFirst(2),
                                                         watermark: live.last!.seq)
                } catch {
                    errors.mutate { $0.append("compactor: \(error)") }
                }
            }
        }
        DispatchQueue.global().async(group: group) {
            for i in 0..<200 {
                do { try appenderStore.append(sessionID: "s", role: .user, content: "appended\(i) text") } catch {
                    errors.mutate { $0.append("appender: \(error)") }
                }
            }
        }
        group.wait()
        let live = Set(try appenderStore.messages(sessionID: "s").map(\.content))
        let all = try appenderStore.messages(sessionID: "s", scope: .all).map(\.content)
        let lostFromLive = (0..<200).filter { i in !live.contains("appended\(i) text") && i >= 2 }
        // The compactor archives the first two live messages it saw each round, so only a message it never saw is a loss.
        let archivedOnly = lostFromLive.filter { i in all.contains("appended\(i) text") && !compacted.value.contains("appended\(i) text") }
        XCTAssertEqual(errors.value.filter { !$0.contains("notActive") }, [])
        XCTAssertEqual(archivedOnly.count, 0, "appended messages archived out of the live context by a stale snapshot: \(archivedOnly.prefix(10))")
        for i in [0, 50, 199] {
            XCTAssertEqual(try appenderStore.search("appended\(i)").count, 1, "appended\(i)")
        }
    }

    func testRewrittenCarriedContentKeepsTheOriginalSearchableAndVisible() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        try store.append(sessionID: "s", role: .user, content: "start")
        try store.append(sessionID: "s", role: .tool, content: "uniqueoriginalword plus a huge tool dump", toolName: "bash")
        var live = try store.messages(sessionID: "s")
        live[1].content = "[tool output trimmed]"
        try store.applyCompaction(sessionID: "s", newActive: live, watermark: live.last!.seq)
        XCTAssertEqual(try store.search("uniqueoriginalword", options: SearchOptions(roles: [.tool])).count, 1,
                       "the pre-rewrite text vanished from search")
        XCTAssertTrue(try store.messages(sessionID: "s", scope: .display).map(\.content).contains { $0.contains("uniqueoriginalword") },
                      "the pre-rewrite text vanished from the display transcript")
    }

    func testCarriedMessagesFromAnotherSessionAreRejected() throws {
        let store = try makeStore()
        try session(store, "s")
        try session(store, "b")
        let foreign = try store.messages(sessionID: "b")
        let before = try store.messages(sessionID: "s", scope: .all)
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: foreign, watermark: 10),
                             "messages of session b were accepted as carried rows of s")
        XCTAssertEqual(try store.messages(sessionID: "s", scope: .all), before)
    }

    func testEmptyNewActiveArchivesEverythingAndTheSessionKeepsWorking() throws {
        let store = try makeStore()
        try session(store)
        let applied = try store.applyCompaction(sessionID: "s", newActive: [], watermark: 10, allowEmpty: true)
        XCTAssertEqual(applied.archivedSeqs, Array(1...10))
        XCTAssertEqual(try store.messages(sessionID: "s"), [])
        XCTAssertEqual(try store.sessions().first?.messageCount, 0)
        for i in 1...10 { XCTAssertEqual(try store.search("sword\(i)").count, 1, "sword\(i)") }
        let next = try store.append(sessionID: "s", role: .user, content: "fresh start")
        XCTAssertEqual(next.seq, 11)
        XCTAssertNotNil(try? JSONSerialization.jsonObject(with: Data(tool("{\"session_id\": \"s\"}", store).text.utf8)))
        integrity()
    }

    func testCarryingEverythingUnchangedKeepsEachMessageOnceEverywhere() throws {
        let store = try makeStore()
        try session(store)
        let live = try store.messages(sessionID: "s")
        let applied = try store.applyCompaction(sessionID: "s", newActive: live, watermark: live.last!.seq)
        XCTAssertEqual(applied.archivedSeqs, [])
        XCTAssertEqual(try store.messages(sessionID: "s").map(\.content), live.map(\.content))
        let display = try store.messages(sessionID: "s", scope: .display).map(\.content)
        XCTAssertEqual(display, live.map(\.content))
        for i in 1...10 { XCTAssertEqual(try store.search("sword\(i)").count, 1) }
        guard case .scroll(let p) = tool("{\"session_id\": \"s\", \"around_seq\": 15, \"window\": 20}", store).payload else {
            return XCTFail("scroll")
        }
        XCTAssertEqual(p.messages.map(\.content), live.map(\.content))
    }

    func testAllNewMessagesArchiveTheWholeHistory() throws {
        let store = try makeStore()
        try session(store)
        let applied = try store.applyCompaction(sessionID: "s", newActive: [summary("all new one"), summary("all new two")], watermark: 10)
        XCTAssertEqual(applied.summarySeqs, [11, 12])
        XCTAssertEqual(try store.messages(sessionID: "s").map(\.content), ["all new one", "all new two"])
        XCTAssertEqual(try store.messages(sessionID: "s", scope: .display).count, 12)
    }

    func testDuplicateInactiveAndUnknownSeqsThrowAndChangeNothing() throws {
        let store = try makeStore()
        try session(store)
        try store.recordUsage(sessionID: "s", promptTokens: 77, atSeq: 10)
        let live = try store.messages(sessionID: "s")
        let before = try store.messages(sessionID: "s", scope: .all)
        var unknown = live[0]
        unknown.seq = 999
        for bad in [[live[0], live[0]], [unknown], [live[3]] + live] {
            XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: bad, watermark: 10))
        }
        try store.applyCompaction(sessionID: "s", newActive: Array(live.suffix(3)), watermark: 10)
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: [live[0]], watermark: 13), "an archived seq was accepted as carried")
        try store.recordUsage(sessionID: "s", promptTokens: 77, atSeq: 13)
        let mid = try store.messages(sessionID: "s", scope: .all)
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: [live[9], live[9]], watermark: 13))
        XCTAssertEqual(try store.messages(sessionID: "s", scope: .all), mid)
        XCTAssertEqual(try store.session(id: "s")?.lastPromptTokens, 77)
        XCTAssertNotEqual(before, mid)
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "nope", newActive: [], watermark: 0, allowEmpty: true))
    }

    func testHugeNewActiveLandsInOrder() throws {
        let store = try makeStore()
        try session(store, count: 5)
        let drafts = (1...20_000).map { ConversationMessage(sessionID: "s", seq: -$0, role: .assistant, content: "bulk\($0) line") }
        let started = Date()
        try store.applyCompaction(sessionID: "s", newActive: drafts, watermark: 5)
        let elapsed = Date().timeIntervalSince(started)
        let live = try store.messages(sessionID: "s")
        XCTAssertEqual(live.count, 20_000)
        XCTAssertEqual(live.first?.content, "bulk1 line")
        XCTAssertEqual(live.last?.content, "bulk20000 line")
        XCTAssertLessThan(elapsed, 30)
        XCTAssertEqual(try store.search("bulk19999").count, 1)
    }

    func testManySuccessiveCompactionsKeepEachMessageOnceAndBoundRowGrowth() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        var distinct = 0
        for round in 0..<60 {
            try store.append(sessionID: "s", [MessageDraft(role: .user, content: "q\(round) asked"),
                                              MessageDraft(role: .assistant, content: "a\(round) answered")])
            distinct += 2
            let live = try store.messages(sessionID: "s")
            if live.count > 12 {
                try store.applyCompaction(sessionID: "s", newActive: [summary("digest\(round) text")] + live.suffix(8), watermark: live.last!.seq)
                distinct += 1
            }
        }
        for round in stride(from: 0, to: 60, by: 7) {
            XCTAssertEqual(try store.search("q\(round)").count, 1, "q\(round)")
            XCTAssertEqual(try store.search("a\(round)").count, 1, "a\(round)")
        }
        let digests = try store.search("digest*", options: SearchOptions(limit: 500))
        XCTAssertEqual(Set(digests.map(\.seq)).count, digests.count)
        let rows = try store.messages(sessionID: "s", scope: .all).count
        let display = try store.messages(sessionID: "s", scope: .display)
        XCTAssertEqual(Set(display.map(\.content)).count, display.count, "display repeats a message")
        XCTAssertEqual(display.count, distinct, "display scope should be exactly the messages ever written")
        XCTAssertLessThan(rows, distinct * 4, "rows=\(rows) for \(distinct) distinct messages")
        integrity()
    }

    func testToolShapesAfterApplyCompactionShowEachMessageOnce() throws {
        let store = try makeStore()
        try session(store)
        let live = try store.messages(sessionID: "s")
        try store.applyCompaction(sessionID: "s", newActive: [summary("digest of early turns")] + live.suffix(4), watermark: live.last!.seq)
        guard case .discover(let d) = tool("{\"query\": \"sword8\", \"detail\": \"full\"}", store).payload,
              let entry = d.results.first else { return XCTFail("discovery") }
        let shown = entry.bookendStart.map(\.content) + entry.messages.map(\.content) + entry.bookendEnd.map(\.content)
        XCTAssertEqual(Set(shown).count, shown.count, "\(shown)")
        guard case .scroll(let p) = tool("{\"session_id\": \"s\", \"around_seq\": 3, \"window\": 20}", store).payload else {
            return XCTFail("scroll")
        }
        let scrolled = p.messages.map(\.content)
        XCTAssertEqual(Set(scrolled).count, scrolled.count, "\(scrolled)")
        guard case .read(let r) = tool("{\"session_id\": \"s\"}", store).payload else { return XCTFail("read") }
        XCTAssertEqual(r.messages.map(\.content), ["digest of early turns"] + live.suffix(4).map(\.content))
        guard case .browse(let b) = tool("{}", store).payload else { return XCTFail("browse") }
        XCTAssertEqual(b.results.first?.messageCount, 5)
    }

    // MARK: Sanitizer

    func testLowercaseNotAndOrInToolSearchAreWordsNotOperators() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        try store.append(sessionID: "t", role: .tool, content: "error: can not connect to database", toolName: "psql")
        try store.append(sessionID: "t", role: .tool, content: "pick this or that option", toolName: "cli")
        XCTAssertEqual(try store.search("can not connect", options: SearchOptions(roles: [.tool])).count, 1)
        XCTAssertEqual(try store.search("this or that", options: SearchOptions(roles: [.tool])).map(\.seq), [2])
        XCTAssertEqual(try store.search("can not connect", options: SearchOptions(roles: [.user, .assistant, .tool])).count, 1)
    }

    func testOperatorWordsAndSymbolPhrasesAsTheWholeQueryStillMatch() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        try store.append(sessionID: "t", role: .user, content: "wire the NOT gate after the AND gate")
        try store.append(sessionID: "t", role: .user, content: "launch \u{1F680} done")
        var misses: [String] = []
        for q in ["NOT", "AND", "\"NOT\"", "\"\u{1F680}\"", "\u{1F680}", "\"NOT gate\""] {
            if try store.search(q).isEmpty { misses.append(q) }
        }
        XCTAssertEqual(misses, [])
    }

    func testRealWorldTokensWithPunctuationFindTheirMessage() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        let literals = ["C++ templates", "C# records", ".NET runtime", "node.js server", "$HOME/bin", "50% done", "issue #1234",
                        "ping @alice", "mail user@example.com", "~/dev/project", "x=1 assignment", "https://example.com/a-b?q=1",
                        "it's fine", "naïve café", "C:\\Users\\bob", "SELECT * FROM t;", "v2.0.1-beta", "foo::bar()", "[link](url)",
                        "a->b pointer", "err!: boom", "100°F"]
        for (i, text) in literals.enumerated() { try store.append(sessionID: "t", role: .user, content: "item\(i) \(text)") }
        var misses: [String] = []
        for (i, text) in literals.enumerated() {
            let hits = try store.search(text, options: SearchOptions(limit: 50))
            if !hits.contains(where: { $0.seq == i + 1 }) { misses.append(text) }
        }
        XCTAssertEqual(misses, [], "literal text present in a message but not found")
    }

    func testTermsBeyondTheCapDoNotChangeTheAnswer() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        try store.append(sessionID: "t", role: .user, content: "alpha secret plans")
        try store.append(sessionID: "t", role: .user, content: "zeta40 only")
        let fillers = (1...39).map { "nomatch\($0)" }
        XCTAssertEqual(try store.search((fillers + ["zeta40"]).joined(separator: " OR ")).map(\.seq), [2],
                       "the 40th OR term was dropped")
        let alphas = Array(repeating: "alpha", count: 16).joined(separator: " OR ")
        XCTAssertTrue(FTSQuery.sanitize(alphas + " NOT secret").hasSuffix("NOT secret"), "a NOT past the cap was dropped")
        // FTS5 binds NOT tighter than OR: this is "alpha OR ... OR (alpha NOT secret)", which matches message 1.
        XCTAssertEqual(try store.search(alphas + " NOT secret").map(\.seq), [1])
        let andAlphas = Array(repeating: "alpha", count: 16).joined(separator: " ")
        XCTAssertEqual(try store.search(andAlphas + " NOT secret").map(\.seq), [], "an AND chain with NOT excludes the message")
    }

    func testALeadingHyphenIsLiteralNotAnExclusion() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        try store.append(sessionID: "t", role: .user, content: "deploy to prod")
        try store.append(sessionID: "t", role: .user, content: "deploy to staging")
        // Hermes has no exclusion syntax (round 3 decision): "-staging" is the word "staging", as in "ls -la" or "rm -rf".
        XCTAssertEqual(try store.search("deploy -staging").map(\.seq), [2])
    }

    func testPathologicalQueriesNeverThrow() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        try store.append(sessionID: "t", role: .user, content: "anything at all")
        let queries = [String(repeating: "\"", count: 4_000), String(repeating: "*", count: 4_000), "\"*\"", "\"\" \"\" \"\"",
                       "\"a\" * \"b\"", "NOT \"x\"", "OR OR OR", "\"anything\" NOT NOT \"all\"", String(repeating: "a\"b ", count: 600),
                       String(repeating: "\u{1F680} ", count: 1_000), String(repeating: "部 ", count: 1_000), "\u{0}\u{0}\u{0}",
                       String(repeating: "x-", count: 1_000), String(repeating: "\"a b\" ", count: 500), "a* OR b* NOT c*"]
        for q in queries {
            for roles in [nil, [MessageRole.tool]] {
                XCTAssertNoThrow(try store.search(q, options: SearchOptions(roles: roles)), "\(q.prefix(30)) roles=\(String(describing: roles))")
            }
        }
    }
}
