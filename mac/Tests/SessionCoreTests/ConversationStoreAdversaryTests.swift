import Foundation
import XCTest
@testable import SessionCore

/// Adversarial probes against the conversation store. Each test asserts the behavior a user relies on;
/// a failure is a finding, not a flaky test.
final class ConversationStoreAdversaryTests: XCTestCase {
    private var dir: URL!
    private var dbURL: URL { dir.appendingPathComponent("conversations.db") }
    private let utc = TimeZone(identifier: "UTC")!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("conv-adv-\(UUID().uuidString)")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func makeStore() throws -> ConversationStore { try ConversationStore(url: dbURL) }

    private func raw() throws -> Connection { try Connection(path: dbURL.path) }

    private func tool(_ json: String, _ store: ConversationStore, current: String? = nil) -> SessionSearchResult {
        SessionSearchTool.run(json: Data(json.utf8), store: store, currentSessionID: current, timeZone: utc)
    }

    private func object(_ result: SessionSearchResult, file: StaticString = #filePath, line: UInt = #line) -> [String: Any] {
        guard let obj = try? JSONSerialization.jsonObject(with: Data(result.text.utf8)) as? [String: Any] else {
            XCTFail("tool text is not a JSON object: \(result.text.prefix(300))", file: file, line: line)
            return [:]
        }
        return obj
    }

    private func ftsIntegrity(_ conn: Connection, file: StaticString = #filePath, line: UInt = #line) {
        for table in ["messages_fts", "messages_fts_trigram"] {
            do {
                try conn.exec("INSERT INTO \(table)(\(table)) VALUES('integrity-check')")
            } catch {
                XCTFail("\(table) integrity-check failed: \(error)", file: file, line: line)
            }
        }
    }

    /// Ten messages tok1...tok10 in session "a".
    private func tenMessageSession() throws -> ConversationStore {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        for i in 1...10 { try store.append(sessionID: "a", role: i % 2 == 0 ? .assistant : .user, content: "tok\(i) body") }
        return store
    }

    // MARK: Data integrity

    func testContentWithNULBytesRoundTripsIntact() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        let content = "binary header\u{0}PAYLOAD after the nul"
        try store.append(sessionID: "s", role: .tool, content: content, toolName: "bash")
        let stored = try store.messages(sessionID: "s")[0].content
        XCTAssertEqual(stored.utf8.count, content.utf8.count, "stored \(stored.debugDescription)")
        XCTAssertEqual(stored, content)
    }

    func testSessionIDsWithNULDoNotAliasAnotherSession() throws {
        let store = try makeStore()
        try store.createSession(title: "victim", id: "victim")
        let other = try store.createSession(title: "attacker", id: "victim\u{0}x")
        XCTAssertEqual(other.title, "attacker", "a NUL-suffixed id resolved to the existing session \(other.id.debugDescription)")
    }

    func testSQLInjectionThroughEveryStringParameterIsInert() throws {
        let store = try makeStore()
        let evil = "x'); DROP TABLE messages; --"
        try store.createSession(title: evil, id: evil)
        try store.append(sessionID: evil, [MessageDraft(role: .assistant, content: evil, toolName: evil, toolCallID: evil),
                                           MessageDraft(role: .tool, content: evil, toolName: evil, toolCallID: evil)])
        try store.rename(sessionID: evil, to: "\"; DELETE FROM sessions; --")
        _ = try store.search(evil)
        _ = try store.search(evil, options: SearchOptions(roles: [.tool]))
        let payload = "{\"session_id\": \"\(evil.replacingOccurrences(of: "\"", with: "\\\""))\", \"around_seq\": \"1 OR 1=1\"}"
        _ = tool(payload, store)
        _ = tool("{\"query\": \"' OR 1=1 --\", \"role_filter\": \"user') OR ('1'='1\"}", store)
        XCTAssertEqual(try store.messages(sessionID: evil).first?.toolCallID, evil)
        XCTAssertEqual(try store.sessions().count, 1)
    }

    func testMultiMegabyteMessagesRoundTripAndTheirTailIsSearchable() throws {
        let store = try makeStore()
        try store.createSession(title: "big", id: "big")
        let filler = String(repeating: "lorem ipsum dolor ", count: 300_000)
        let userText = filler + " needleuserend"
        let toolText = filler + " needletoolend"
        try store.append(sessionID: "big", role: .user, content: userText)
        try store.append(sessionID: "big", role: .tool, content: toolText, toolName: "cat")
        let stored = try store.messages(sessionID: "big")
        XCTAssertEqual(stored.map(\.content.count), [userText.count, toolText.count])
        XCTAssertEqual(try store.search("needleuserend").map(\.seq), [1])
        XCTAssertEqual(try store.search("needletoolend", options: SearchOptions(roles: [.tool])).map(\.seq), [2])
        ftsIntegrity(try raw())
    }

    func testScrollCapsMultiMegabyteContentLikeReadAndDiscoveryDo() throws {
        let store = try makeStore()
        try store.createSession(title: "big", id: "big")
        try store.append(sessionID: "big", role: .tool, content: String(repeating: "x", count: 3_000_000), toolName: "cat")
        let result = tool("{\"session_id\": \"big\", \"around_seq\": 1}", store)
        XCTAssertLessThan(result.text.utf8.count, 100_000, "scroll returned \(result.text.utf8.count) bytes to the model")
    }

    func testEmptyContentAndFarTimestampsRoundTrip() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s", createdAt: Date(timeIntervalSince1970: -62_135_596_800))
        let past = Date(timeIntervalSince1970: -62_135_596_800)
        let future = Date(timeIntervalSince1970: 253_402_300_799)
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "", timestamp: past),
                                          MessageDraft(role: .assistant, content: "future reply", timestamp: future)])
        let stored = try store.messages(sessionID: "s")
        XCTAssertEqual(stored.map(\.content), ["", "future reply"])
        XCTAssertEqual(stored.map(\.timestamp), [past, future])
        XCTAssertEqual(try store.search("future", options: SearchOptions(sort: .oldest)).map(\.seq), [2])
        let read = object(tool("{\"session_id\": \"s\"}", store))
        XCTAssertEqual(read["success"] as? Bool, true)
    }

    func testNonFiniteTimestampNeverBlanksTheToolResult() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "infinite clock", timestamp: Date(timeIntervalSince1970: .infinity))
        let result = tool("{\"session_id\": \"s\"}", store)
        XCTAssertNotNil(object(result)["success"], "tool text was \(result.text)")
    }

    func testUnicodeNormalizationFormsFindEachOther() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "cafe\u{301} decomposed")
        try store.append(sessionID: "s", role: .user, content: "re\u{301}sume\u{301} composed".precomposedStringWithCanonicalMapping)
        XCTAssertEqual(try store.search("caf\u{E9}").map(\.seq), [1], "NFC query vs NFD content")
        XCTAssertEqual(try store.search("re\u{301}sume\u{301}").map(\.seq), [2], "NFD query vs NFC content")
    }

    func testRTLEmojiAndCJKContentIsSearchable() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "launch \u{1F680} complete")
        try store.append(sessionID: "s", role: .user, content: "\u{645}\u{631}\u{62D}\u{628}\u{627} \u{628}\u{627}\u{644}\u{639}\u{627}\u{644}\u{645}")
        try store.append(sessionID: "s", role: .user, content: "\u{5E9}\u{5DC}\u{5D5}\u{5DD} \u{202E}reversed\u{202C} text")
        try store.append(sessionID: "s", role: .user, content: "部署失败了")
        XCTAssertEqual(try store.search("\u{1F680}").map(\.seq), [1])
        XCTAssertEqual(try store.search("\u{645}\u{631}\u{62D}\u{628}\u{627}").map(\.seq), [2])
        XCTAssertEqual(try store.search("\u{5E9}\u{5DC}\u{5D5}\u{5DD}").map(\.seq), [3])
        XCTAssertEqual(try store.search("部署").map(\.seq), [4])
        XCTAssertEqual(try store.search("署失败").map(\.seq), [4])
    }

    // MARK: FTS query syntax

    func testPunctuationNextToOrBetweenWordsNeverHidesAMatch() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "the staging deploy broke again")
        var misses: [String] = []
        let punctuation = Array("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
        for c in punctuation {
            for query in ["staging deploy\(c)", "staging \(c) deploy", "staging\(c) deploy", "\(c)staging deploy"] {
                do {
                    if try store.search(query).isEmpty { misses.append(query) }
                } catch {
                    misses.append("\(query) THREW \(error)")
                }
            }
        }
        XCTAssertEqual(misses, [], "queries that found nothing")
    }

    func testNaturalSentencesTheModelSendsStillMatch() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "We fixed the login bug in the auth service yesterday")
        for query in ["login bug.", "What happened with the login bug?", "the auth service...", "e.g. login bug", "login bug - auth",
                      "login/auth bug", "`login` bug", "login bug — auth", "login bug; auth", "auth-service"] {
            XCTAssertFalse(try store.search(query).isEmpty, query)
        }
    }

    func testOperatorAndGrammarSoupNeverThrowsAndStillFindsTheWord() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "alpha bravo charlie", toolName: nil)
        let queries = ["AND AND alpha", "alpha OR OR bravo", "NOT NOT alpha", "alpha NOT", "alpha AND", "(alpha", "alpha)",
                       "NEAR(alpha charlie)", "NEAR(alpha charlie, 2)", "content:alpha", "tool_name:alpha", "{content}: alpha",
                       "-alpha", "alpha*", "*alpha", "al*pha", "^alpha", "alpha^", "\"alpha", "alpha\"", "\"\"\"alpha",
                       "\"alpha\" \"bravo", "\"alpha\"bravo", "alpha + bravo", "alpha | bravo", "alpha & bravo", "alpha ~ bravo",
                       "alpha OR", "OR alpha", "NEAR alpha", "alpha AND NOT", "alpha\u{0}bravo", "\u{0}Q0\u{0} alpha"]
        var misses: [String] = []
        for query in queries {
            do {
                if try store.search(query).isEmpty { misses.append(query) }
            } catch {
                misses.append("\(query.debugDescription) THREW \(error)")
            }
        }
        XCTAssertEqual(misses, [], "queries that found nothing")
    }

    func testHugeQueriesNeverThrowOnAnyRoute() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .tool, content: "w1 tool output", toolName: "bash")
        try store.append(sessionID: "s", role: .user, content: "w1 user text 部署")
        let words = (1...1_200).map { "w\($0)" }
        let cases: [(String, SearchOptions)] = [
            (String(repeating: "a", count: 100_000), SearchOptions()),
            (words.joined(separator: " "), SearchOptions()),
            (words.joined(separator: " OR "), SearchOptions()),
            (words.joined(separator: " "), SearchOptions(roles: [.tool])),
            (words.joined(separator: " OR "), SearchOptions(roles: [.user, .tool])),
            (Array(repeating: "部", count: 1_000).joined(separator: " "), SearchOptions()),
            (Array(repeating: "\"x\"", count: 700).joined(separator: " "), SearchOptions()),
        ]
        for (query, options) in cases {
            XCTAssertNoThrow(try store.search(query, options: options), "\(query.prefix(40))… roles=\(String(describing: options.roles))")
        }
    }

    func testMixedCJKAndCompoundTermFindsTheLatinMessage() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "the deploy-prod rollout stalled")
        XCTAssertFalse(try store.search("deploy-prod 部署").isEmpty)
        XCTAssertFalse(try store.search("部署 deploy-prod").isEmpty)
    }

    // MARK: Compaction

    func testRepeatedOverlappingCompactionKeepsEveryMessageFindableExactlyOnce() throws {
        let store = try tenMessageSession()
        try store.recordUsage(sessionID: "a", promptTokens: 999, atSeq: 10)
        try store.compact(sessionID: "a", seqRange: 2...4, summary: "summaryone digest")
        let afterFirst = try store.messages(sessionID: "a").map(\.seq)
        try store.compact(sessionID: "a", seqRange: 3...(afterFirst[2]), summary: "summarytwo digest")
        try store.append(sessionID: "a", role: .user, content: "tok11 body")
        try store.compact(sessionID: "a", seqRange: Int.min...Int.max, summary: "summarythree digest")
        try store.append(sessionID: "a", role: .assistant, content: "tok12 body")

        for i in 1...12 {
            XCTAssertEqual(try store.search("tok\(i)").count, 1, "tok\(i)")
        }
        for s in ["summaryone", "summarytwo", "summarythree"] {
            XCTAssertEqual(try store.search(s).count, 1, s)
        }
        let display = try store.messages(sessionID: "a", scope: .display)
        XCTAssertEqual(display.map(\.seq), display.map(\.seq).sorted())
        XCTAssertEqual(Set(display.map(\.content)).count, display.count, "display scope repeats a message")
        let order = display.map(\.content).filter { $0.hasPrefix("tok") }.map { Int($0.dropFirst(3).prefix { $0.isNumber })! }
        XCTAssertEqual(order, Array(1...12), "display reading order")
        XCTAssertEqual(try store.messages(sessionID: "a").map(\.content), ["summarythree digest", "tok12 body"])
        XCTAssertNil(try store.session(id: "a")?.lastPromptTokens)
        XCTAssertEqual(try store.sessions().first?.messageCount, 2)
        let all = try store.messages(sessionID: "a", scope: .all).map(\.seq)
        XCTAssertEqual(Set(all).count, all.count)
        ftsIntegrity(try raw())
    }

    func testCompactionKeepsToolCallLinksAndRolesOnTheResequencedTail() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", [
            MessageDraft(role: .user, content: "start"),
            MessageDraft(role: .user, content: "old"),
            MessageDraft(role: .assistant, content: "calling", toolName: "bash", toolCallID: "call_1"),
            MessageDraft(role: .tool, content: "result", toolName: "bash", toolCallID: "call_1"),
        ])
        try store.compact(sessionID: "s", seqRange: 1...2, summary: "sum")
        let live = try store.messages(sessionID: "s")
        XCTAssertEqual(live.map(\.role), [.user, .assistant, .tool])
        XCTAssertEqual(live.map(\.toolCallID), [nil, "call_1", "call_1"])
        XCTAssertEqual(live.map(\.toolName), [nil, "bash", "bash"])
    }

    func testAFailureMidwayThroughTheTailCopyRollsEverythingBack() throws {
        let store = try tenMessageSession()
        try store.recordUsage(sessionID: "a", promptTokens: 500, atSeq: 10)
        try raw().exec("""
            CREATE TRIGGER tail_boom BEFORE INSERT ON messages
            WHEN new.content = 'tok8 body' AND (SELECT COUNT(*) FROM messages WHERE content = 'tok8 body') >= 1
            BEGIN SELECT RAISE(ABORT, 'tail boom'); END;
            """)
        let before = try store.messages(sessionID: "a", scope: .all)
        XCTAssertThrowsError(try store.compact(sessionID: "a", seqRange: 1...3, summary: "never lands"))
        XCTAssertEqual(try store.messages(sessionID: "a", scope: .all), before)
        XCTAssertEqual(try store.session(id: "a")?.lastPromptTokens, 500)
        XCTAssertEqual(try store.sessions().first?.messageCount, 10)
        XCTAssertEqual(try store.search("never").count, 0)
        XCTAssertEqual(try store.search("tok5").count, 1)
        try store.append(sessionID: "a", role: .user, content: "after the failure")
        XCTAssertEqual(try store.messages(sessionID: "a").last?.seq, 11)
        ftsIntegrity(try raw())
    }

    func testScrollAfterCompactionShowsEachMessageOnce() throws {
        let store = try tenMessageSession()
        try store.compact(sessionID: "a", seqRange: 2...4, summary: "summary digest")
        let anchor = try XCTUnwrap(try store.search("tok6").first?.seq)
        guard case .scroll(let p) = tool("{\"session_id\": \"a\", \"around_seq\": \(anchor), \"window\": 20}", store).payload else {
            return XCTFail("not a scroll")
        }
        let contents = p.messages.map(\.content)
        XCTAssertEqual(Set(contents).count, contents.count, "scroll repeats superseded copies: \(contents)")
    }

    func testDiscoveryHydrationAfterCompactionShowsEachMessageOnce() throws {
        let store = try tenMessageSession()
        try store.compact(sessionID: "a", seqRange: 2...4, summary: "summary digest")
        let p = tool("{\"query\": \"tok7\", \"detail\": \"full\"}", store)
        guard case .discover(let d) = p.payload, let entry = d.results.first else { return XCTFail(p.text) }
        let contents = entry.messages.map(\.content)
        XCTAssertEqual(Set(contents).count, contents.count, "hydrated window repeats superseded copies: \(contents)")
    }

    func testSearchContextNeighborsSkipSupersededCopies() throws {
        let store = try tenMessageSession()
        try store.compact(sessionID: "a", seqRange: 2...4, summary: "summary digest")
        let hit = try XCTUnwrap(try store.search("summary").first)
        let contents = hit.context.map(\.content)
        XCTAssertFalse(contents.contains("tok10 body"), "summary's neighbors come from superseded rows: \(contents)")
    }

    // MARK: Concurrency

    func testManyThreadsAppendingToOneSessionLoseNothing() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        let errors = Locked<[String]>([])
        DispatchQueue.concurrentPerform(iterations: 600) { i in
            do {
                if i % 3 == 0 {
                    try store.append(sessionID: "s", [MessageDraft(role: .user, content: "batch \(i) a"),
                                                      MessageDraft(role: .assistant, content: "batch \(i) b")])
                } else {
                    try store.append(sessionID: "s", role: .user, content: "single \(i)")
                }
            } catch {
                errors.mutate { $0.append("\(error)") }
            }
        }
        XCTAssertEqual(errors.value, [])
        let seqs = try store.messages(sessionID: "s").map(\.seq)
        XCTAssertEqual(seqs, Array(1...800))
        XCTAssertEqual(try store.sessions().first?.messageCount, 800)
    }

    func testTwoStoresOnOneFileAppendWithoutDuplicateOrMissingSeqs() throws {
        let first = try makeStore()
        let second = try makeStore()
        try first.createSession(title: "t", id: "s")
        let errors = Locked<[String]>([])
        DispatchQueue.concurrentPerform(iterations: 400) { i in
            do {
                try (i % 2 == 0 ? first : second).append(sessionID: "s", role: .user, content: "from \(i % 2) number \(i)")
                if i % 10 == 0 { _ = try (i % 2 == 0 ? second : first).search("number") }
            } catch {
                errors.mutate { $0.append("\(error)") }
            }
        }
        XCTAssertEqual(errors.value, [])
        XCTAssertEqual(try second.messages(sessionID: "s").map(\.seq), Array(1...400))
        XCTAssertEqual(try first.sessions().first?.messageCount, 400)
        ftsIntegrity(try raw())
    }

    func testConcurrentFirstOpensOfAFreshFileAllSucceed() throws {
        for trial in 0..<15 {
            let url = dir.appendingPathComponent("fresh-\(trial).db")
            let errors = Locked<[String]>([])
            let stores = Locked<[ConversationStore]>([])
            DispatchQueue.concurrentPerform(iterations: 6) { _ in
                do {
                    let s = try ConversationStore(url: url)
                    stores.mutate { $0.append(s) }
                } catch {
                    errors.mutate { $0.append("\(error)") }
                }
            }
            XCTAssertEqual(errors.value, [], "trial \(trial)")
        }
    }

    func testReadersOnASecondStoreNeverSeeAHalfCompactedSession() throws {
        let writer = try makeStore()
        try writer.createSession(title: "t", id: "s")
        try writer.append(sessionID: "s", (1...120).map { MessageDraft(role: .user, content: "item\($0) text") })
        let reader = try makeStore()
        let problems = Locked<[String]>([])
        let done = Locked(false)
        let group = DispatchGroup()
        DispatchQueue.global().async(group: group) {
            defer { done.mutate { $0 = true } }
            do {
                for round in 0..<25 {
                    let live = try writer.messages(sessionID: "s")
                    guard live.count > 3 else { break }
                    try writer.compact(sessionID: "s", seqRange: live[0].seq...live[2].seq, summary: "round\(round) summary")
                    try writer.append(sessionID: "s", role: .assistant, content: "post\(round) reply")
                }
            } catch {
                problems.mutate { $0.append("writer: \(error)") }
            }
        }
        DispatchQueue.global().async(group: group) {
            while !done.value {
                do {
                    let display = try reader.messages(sessionID: "s", scope: .display)
                    let contents = display.map(\.content)
                    if Set(contents).count != contents.count { problems.mutate { $0.append("display duplicates") } }
                    let item60 = try reader.search("item60").count
                    if item60 != 1 { problems.mutate { $0.append("item60 found \(item60)x") } }
                } catch {
                    problems.mutate { $0.append("reader: \(error)") }
                }
            }
        }
        group.wait()
        XCTAssertEqual(Array(Set(problems.value)), [])
    }

    // MARK: Sessions and lineage

    func testDeletingAParentWithChildrenLeavesChildrenSearchableAndRooted() throws {
        let store = try makeStore()
        try store.createSession(title: "root", id: "root")
        try store.createSession(title: "mid", parentID: "root", id: "mid")
        try store.createSession(title: "leaf", parentID: "mid", id: "leaf")
        try store.append(sessionID: "leaf", role: .user, content: "leafword")
        try store.append(sessionID: "mid", role: .user, content: "midword")
        XCTAssertTrue(try store.deleteSession(id: "mid"))
        XCTAssertFalse(try store.deleteSession(id: "mid"))
        XCTAssertEqual(try store.lineage(of: "leaf").map(\.id), ["leaf"])
        XCTAssertEqual(try store.search("leafword").map(\.sessionID), ["leaf"])
        XCTAssertEqual(try store.search("midword").count, 0)
        XCTAssertThrowsError(try store.append(sessionID: "mid", role: .user, content: "ghost"))
        ftsIntegrity(try raw())
    }

    func testLineageCyclesWrittenBehindTheStoreTerminate() throws {
        let store = try makeStore()
        for id in ["a", "b", "c"] { try store.createSession(title: id, id: id) }
        let conn = try raw()
        try conn.exec("UPDATE sessions SET parent_session_id = 'b' WHERE id = 'a'; UPDATE sessions SET parent_session_id = 'c' WHERE id = 'b';")
        try conn.exec("UPDATE sessions SET parent_session_id = 'a' WHERE id = 'c'")
        XCTAssertEqual(try store.lineage(of: "a").count, 3)
        try conn.exec("UPDATE sessions SET parent_session_id = id")
        XCTAssertEqual(try store.lineage(of: "a").count, 1)
    }

    // MARK: session_search tool input

    func testMalformedAndMistypedArgumentsAlwaysReturnJSONWithSuccess() throws {
        let store = try makeStore()
        try store.createSession(title: "Deploy", id: "d")
        try store.append(sessionID: "d", role: .user, content: "deploy it")
        let inputs = ["", "{", "[]", "null", "\"str\"", "42", "{\"query\": 5}", "{\"query\": null}", "{\"query\": [\"deploy\"]}",
                      "{\"query\": {\"$ne\": 1}}", "{\"limit\": -5, \"query\": \"deploy\"}", "{\"limit\": 1e300, \"query\": \"deploy\"}",
                      "{\"limit\": \"abc\", \"query\": \"deploy\"}", "{\"limit\": 99999999999999999999999}",
                      "{\"session_id\": \"d\", \"around_seq\": \"abc\"}", "{\"session_id\": \"d\", \"around_seq\": 1e20}",
                      "{\"session_id\": \"d\", \"around_seq\": -1}", "{\"session_id\": \"d\", \"around_seq\": \"9223372036854775807\"}",
                      "{\"session_id\": \"d\", \"around_seq\": 1, \"window\": -100}", "{\"session_id\": \"d\", \"around_seq\": 1, \"window\": 1e9}",
                      "{\"query\": \"deploy\", \"after\": \"garbage\"}", "{\"query\": \"deploy\", \"after\": \"2026-02-30\"}",
                      "{\"query\": \"deploy\", \"after\": \"99999999999999999999999d\"}", "{\"query\": \"deploy\", \"before\": \"-5d\"}",
                      "{\"query\": \"deploy\", \"sort\": \"sideways\", \"detail\": \"mega\"}", "{\"query\": \"deploy\", \"role_filter\": \",,,bogus\"}",
                      "{\"query\": \"deploy\", \"exclude_session_ids\": 5}", "{\"query\": \"deploy\", \"exclude_session_ids\": [1, null, {}]}",
                      "{\"session_id\": \"nope\"}", "{\"session_id\": \"   \"}", "{\"query\": \"\\u0000\"}", "{\"query\": \"\\ud83d\"}"]
        for input in inputs {
            let result = tool(input, store)
            XCTAssertNotNil(object(result)["success"], "input \(input) -> \(result.text.prefix(200))")
        }
    }

    func testANonStringQueryIsNotSilentlyTreatedAsBrowse() throws {
        let store = try makeStore()
        try store.createSession(title: "Deploy", id: "d")
        let result = tool("{\"query\": [\"deploy\"]}", store)
        if case .browse = result.payload { XCTFail("an array query fell through to browse: \(result.text)") }
    }

    func testExcludedIDsGivenAsNonStringsStillExclude() throws {
        let store = try makeStore()
        try store.createSession(title: "x", id: "123")
        try store.append(sessionID: "123", role: .user, content: "excludeme")
        guard case .discover(let p) = tool("{\"query\": \"excludeme\", \"exclude_session_ids\": [123]}", store).payload else {
            return XCTFail("not discovery")
        }
        XCTAssertEqual(p.results.map(\.sessionID), [])
    }

    func testScrollPastBothEndsReportsTheEdges() throws {
        let store = try tenMessageSession()
        guard case .scroll(let end) = tool("{\"session_id\": \"a\", \"around_seq\": 10, \"window\": 20}", store).payload else {
            return XCTFail("no scroll")
        }
        XCTAssertEqual(end.messagesAfter, 0)
        XCTAssertEqual(end.messagesBefore, 9)
        guard case .scroll(let start) = tool("{\"session_id\": \"a\", \"around_seq\": 1, \"window\": 3}", store).payload else {
            return XCTFail("no scroll")
        }
        XCTAssertEqual(start.messagesBefore, 0)
        XCTAssertEqual(start.messages.map(\.seq), [1, 2, 3, 4])
        XCTAssertEqual(object(tool("{\"session_id\": \"a\", \"around_seq\": 11}", store))["success"] as? Bool, false)
    }

    func testCurrentSessionIsHiddenEverywhereButItsArchivedHistory() throws {
        let store = try tenMessageSession()
        try store.createSession(title: "other", id: "o")
        try store.append(sessionID: "o", role: .user, content: "tok9 elsewhere")
        guard case .discover(let p) = tool("{\"query\": \"tok9\", \"limit\": 10}", store, current: "a").payload else {
            return XCTFail("no discovery")
        }
        XCTAssertEqual(p.results.map(\.sessionID), ["o"])
        let unknown = object(tool("{\"session_id\": \"a\", \"around_seq\": 999}", store, current: "a"))
        XCTAssertEqual(unknown["success"] as? Bool, false)
        XCTAssertFalse((unknown["error"] as? String ?? "").contains("anchor lives in the current session"),
                       "a seq that does not exist is reported as live context: \(unknown)")
        guard case .browse(let b) = tool("{}", store, current: "a").payload else { return XCTFail("no browse") }
        XCTAssertFalse(b.results.map(\.sessionID).contains("a"))
    }

    func testTitleMatchOnTheCurrentSessionDoesNotHideAnOlderSameTitledSession() throws {
        let store = try makeStore()
        try store.createSession(title: "Weekly sync", id: "old", createdAt: Date(timeIntervalSince1970: 1_000))
        try store.append(sessionID: "old", role: .user, content: "agenda items")
        try store.createSession(title: "Weekly sync", id: "current", createdAt: Date(timeIntervalSince1970: 2_000))
        guard case .discover(let p) = tool("{\"query\": \"Weekly sync\"}", store, current: "current").payload else {
            return XCTFail("no discovery")
        }
        XCTAssertEqual(p.results.first?.sessionID, "old")
    }

    func testResultTextEscapesEveryHostileCharacter() throws {
        let store = try makeStore()
        try store.createSession(title: "q\"uote\\back", id: "s")
        let content = "quote\" backslash\\ ctrl\u{1}\u{7} line\u{2028}sep \u{1F600} </script> \\u0000 tab\t nl\n"
        try store.append(sessionID: "s", role: .user, content: content)
        let read = object(tool("{\"session_id\": \"s\"}", store))
        let messages = try XCTUnwrap(read["messages"] as? [[String: Any]])
        XCTAssertEqual(messages.first?["content"] as? String, content)
        let disc = object(tool("{\"query\": \"backslash\"}", store))
        XCTAssertEqual((disc["results"] as? [[String: Any]])?.count, 1)
    }

}

final class Locked<T>: @unchecked Sendable {
    private var stored: T
    private let lock = NSLock()
    init(_ value: T) { stored = value }
    var value: T { lock.lock(); defer { lock.unlock() }; return stored }
    func mutate(_ body: (inout T) -> Void) { lock.lock(); defer { lock.unlock() }; body(&stored) }
}
