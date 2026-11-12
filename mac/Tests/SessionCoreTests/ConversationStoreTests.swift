import Foundation
import XCTest
@testable import SessionCore

private struct Unexpected: Error { let text: String }

final class ConversationStoreTests: XCTestCase {
    private var dir: URL!
    private var dbURL: URL { dir.appendingPathComponent("conversations.db") }
    private let utc = TimeZone(identifier: "UTC")!
    private let jan15 = Date(timeIntervalSince1970: 1_800_000_000)

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("conv-store-\(UUID().uuidString)")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func makeStore() throws -> ConversationStore { try ConversationStore(url: dbURL) }

    private func raw() throws -> Connection { try Connection(path: dbURL.path) }

    /// Empties the word index's data blocks with the system sqlite3 tool; the library this process links refuses shadow-table writes.
    private func corruptWordIndex() throws {
        let tool = URL(fileURLWithPath: "/usr/bin/sqlite3")
        guard FileManager.default.isExecutableFile(atPath: tool.path) else { throw XCTSkip("needs /usr/bin/sqlite3") }
        let process = Process()
        process.executableURL = tool
        process.arguments = [dbURL.path, "DELETE FROM messages_fts_data;"]
        try process.run()
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0)
    }

    private func json(_ result: SessionSearchResult) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(result.text.utf8)) as? [String: Any])
    }

    private func discovery(_ result: SessionSearchResult) throws -> DiscoveryPayload {
        guard case .discover(let p) = result.payload else { throw Unexpected(text: result.text) }
        return p
    }

    // MARK: Schema

    func testFreshStoreUsesWALStampsVersionAndIsPrivate() throws {
        _ = try makeStore()
        let conn = try raw()
        XCTAssertEqual(try conn.one("PRAGMA journal_mode")?.string("journal_mode"), "wal")
        XCTAssertEqual(try conn.one("SELECT version FROM schema_version")?.int("version"), ConversationSchema.version)
        XCTAssertNotNil(try conn.one("SELECT value FROM state_meta WHERE key = 'store_instance_id'"))
        for path in [dbURL.path, dbURL.path + "-wal"] {
            XCTAssertEqual(try FileManager.default.attributesOfItem(atPath: path)[.posixPermissions] as? Int, 0o600, path)
        }
    }

    func testReopeningTightensAnExistingStoreToOwnerOnly() throws {
        _ = try makeStore()
        for path in [dbURL.path, dbURL.path + "-wal"] {
            try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: path)
        }
        _ = try makeStore()
        for path in [dbURL.path, dbURL.path + "-wal"] {
            XCTAssertEqual(try FileManager.default.attributesOfItem(atPath: path)[.posixPermissions] as? Int, 0o600, path)
        }
    }

    func testDefaultLocationIsApplicationSupportAssistant() {
        XCTAssertTrue(ConversationStore.defaultURL.path.hasSuffix("Library/Application Support/Assistant/conversations.db"))
    }

    func testStoreFromOlderBuildGainsMissingColumnsAndKeepsItsRows() throws {
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let old = try raw()
        try old.exec("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', parent_session_id TEXT, started_at REAL NOT NULL);
            INSERT INTO sessions (id, title, started_at) VALUES ('legacy', 'Old chat', 1700000000);
            """)
        old.close()
        let store = try makeStore()
        XCTAssertEqual(try store.session(id: "legacy")?.title, "Old chat")
        try store.recordUsage(sessionID: "legacy", promptTokens: 1200, atSeq: 4)
        XCTAssertEqual(try store.session(id: "legacy")?.lastPromptTokens, 1200)
        XCTAssertEqual(try store.session(id: "legacy")?.archived, false)
    }

    func testOlderSchemaVersionAdvancesAndNewerIsLeftAlone() throws {
        _ = try makeStore()
        try raw().run("UPDATE schema_version SET version = 0")
        _ = try makeStore()
        XCTAssertEqual(try raw().one("SELECT version FROM schema_version")?.int("version"), ConversationSchema.version)
        try raw().run("UPDATE schema_version SET version = 99")
        _ = try makeStore()
        XCTAssertEqual(try raw().one("SELECT version FROM schema_version")?.int("version"), 99)
    }

    func testIndexesAndFTSTriggersExist() throws {
        _ = try makeStore()
        let names = Set(try raw().query("SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger')").compactMap { $0.string("name") })
        for name in ["idx_sessions_parent", "idx_sessions_effective_activity", "idx_messages_session_active"]
            + ConversationSchema.baseTriggers + ConversationSchema.trigramTriggers {
            XCTAssertTrue(names.contains(name), name)
        }
    }

    func testIndexMissingItsTriggersIsRebuiltOnOpen() throws {
        var store: ConversationStore? = try makeStore()
        try store!.createSession(title: "t", id: "s")
        store = nil
        let conn = try raw()
        try ConversationSchema.dropTriggers(conn, ConversationSchema.baseTriggers + ConversationSchema.trigramTriggers)
        try conn.run("INSERT INTO messages (session_id, seq, role, content, timestamp) VALUES ('s', 1, 'user', 'written without triggers qz', 1)")
        conn.close()
        XCTAssertEqual(try makeStore().search("qz").map(\.seq), [1], "a two-letter term only the word index can serve")
    }

    // MARK: Sessions

    func testCreateSessionStoresFieldsAndNeverOverwritesAnExistingRow() throws {
        let store = try makeStore()
        let parent = try store.createSession(title: "Parent", id: "p", createdAt: jan15)
        let child = try store.createSession(title: "Child", parentID: "p", id: "c", createdAt: jan15)
        XCTAssertEqual(parent.createdAt, jan15)
        XCTAssertEqual(child.parentID, "p")
        XCTAssertEqual(try store.createSession(title: "Renamed?", id: "c").title, "Child")
    }

    func testCreateSessionUnderUnknownParentThrows() throws {
        XCTAssertThrowsError(try makeStore().createSession(title: "x", parentID: "missing")) {
            XCTAssertEqual($0 as? ConversationStoreError, .sessionNotFound("missing"))
        }
    }

    func testListIsMostRecentlyActiveFirstAndArchiveHidesFromDefault() throws {
        let store = try makeStore()
        try store.createSession(title: "Older", id: "a", createdAt: Date(timeIntervalSince1970: 100))
        try store.createSession(title: "Newer", id: "b", createdAt: Date(timeIntervalSince1970: 200))
        try store.append(sessionID: "a", role: .user, content: "first line\nsecond line", timestamp: Date(timeIntervalSince1970: 300))
        XCTAssertEqual(try store.sessions().map(\.session.id), ["a", "b"])
        let a = try XCTUnwrap(store.sessions().first)
        XCTAssertEqual(a.preview, "first line second line")
        XCTAssertEqual(a.messageCount, 1)
        XCTAssertEqual(a.lastActive, Date(timeIntervalSince1970: 300))
        try store.setArchived(sessionID: "a", true)
        XCTAssertEqual(try store.sessions().map(\.session.id), ["b"])
        XCTAssertEqual(try store.sessions(archive: .archived).map(\.session.id), ["a"])
        XCTAssertEqual(try store.session(id: "a")?.archived, true)
        XCTAssertEqual(try store.search("second").map(\.sessionID), ["a"])
    }

    func testPreviewIsShortenedToSixtyCharacters() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: String(repeating: "word ", count: 30))
        XCTAssertEqual(try store.sessions().first?.preview, String(String(repeating: "word ", count: 12).prefix(60)) + "...")
    }

    func testRenameSanitizesAndRejectsOverlongTitles() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.rename(sessionID: "s", to: "  Plan\u{200B} the\n\tlaunch\u{0007} ")
        XCTAssertEqual(try store.session(id: "s")?.title, "Plan the launch")
        XCTAssertThrowsError(try store.rename(sessionID: "s", to: String(repeating: "x", count: 101)))
        XCTAssertThrowsError(try store.rename(sessionID: "nope", to: "x")) {
            XCTAssertEqual($0 as? ConversationStoreError, .sessionNotFound("nope"))
        }
    }

    func testLineageWalksParentsRootFirstAndStopsOnACycle() throws {
        let store = try makeStore()
        try store.createSession(title: "root", id: "r")
        try store.createSession(title: "mid", parentID: "r", id: "m")
        try store.createSession(title: "tip", parentID: "m", id: "t")
        XCTAssertEqual(try store.lineage(of: "t").map(\.id), ["r", "m", "t"])
        try raw().run("UPDATE sessions SET parent_session_id = 't' WHERE id = 'r'")
        XCTAssertEqual(try store.lineage(of: "t").map(\.id), ["r", "m", "t"])
    }

    func testDeleteRemovesMessagesFromSearchAndKeepsChildrenAsRoots() throws {
        let store = try makeStore()
        try store.createSession(title: "p", id: "p")
        try store.createSession(title: "c", parentID: "p", id: "c")
        try store.append(sessionID: "p", role: .user, content: "ephemeral secret")
        XCTAssertTrue(try store.deleteSession(id: "p"))
        XCTAssertNil(try store.session(id: "p"))
        XCTAssertNil(try store.session(id: "c")?.parentID)
        XCTAssertEqual(try store.search("ephemeral"), [])
        XCTAssertFalse(try store.deleteSession(id: "p"))
    }

    // MARK: Messages

    func testAppendAssignsMonotonicSeqPerSession() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        try store.createSession(title: "b", id: "b")
        let a1 = try store.append(sessionID: "a", role: .user, content: "one")
        try store.append(sessionID: "b", role: .user, content: "other")
        let rest = try store.append(sessionID: "a", [MessageDraft(role: .assistant, content: "two", toolName: "bash", toolCallID: "call-1"),
                                                     MessageDraft(role: .tool, content: "three", toolCallID: "call-1")])
        XCTAssertEqual([a1.seq] + rest.map(\.seq), [1, 2, 3])
        XCTAssertEqual(try store.messages(sessionID: "b").map(\.seq), [1])
        let stored = try store.messages(sessionID: "a")
        XCTAssertEqual(stored.map(\.content), ["one", "two", "three"])
        XCTAssertEqual(stored[1].toolName, "bash")
        XCTAssertEqual(stored[2].toolCallID, "call-1")
        XCTAssertEqual(stored, [a1] + rest)
    }

    func testBatchAppendIsAllOrNothing() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        try raw().exec("CREATE TRIGGER boom BEFORE INSERT ON messages WHEN new.content = 'boom' BEGIN SELECT RAISE(ABORT, 'boom'); END;")
        XCTAssertThrowsError(try store.append(sessionID: "a", [MessageDraft(role: .user, content: "kept?"),
                                                              MessageDraft(role: .assistant, content: "boom")]))
        XCTAssertEqual(try store.messages(sessionID: "a", scope: .all), [])
        XCTAssertEqual(try store.sessions().first?.messageCount, 0)
    }

    func testAppendToUnknownSessionThrows() throws {
        XCTAssertThrowsError(try makeStore().append(sessionID: "ghost", role: .user, content: "hi")) {
            XCTAssertEqual($0 as? ConversationStoreError, .sessionNotFound("ghost"))
        }
    }

    func testWindowAroundSeqCountsBothSidesAndIgnoresForeignSeq() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        for i in 1...10 { try store.append(sessionID: "a", role: i % 2 == 0 ? .assistant : .user, content: "m\(i)") }
        let w = try store.window(sessionID: "a", aroundSeq: 5, radius: 2)
        XCTAssertEqual(w.messages.map(\.seq), [3, 4, 5, 6, 7])
        XCTAssertEqual([w.messagesBefore, w.messagesAfter], [2, 2])
        let edge = try store.window(sessionID: "a", aroundSeq: 9, radius: 3)
        XCTAssertEqual(edge.messages.map(\.seq), [6, 7, 8, 9, 10])
        XCTAssertEqual([edge.messagesBefore, edge.messagesAfter], [3, 1])
        XCTAssertEqual(try store.window(sessionID: "a", aroundSeq: 99).messages, [])
    }

    func testResumeRefusesAnOverlongContextAndZeroDisablesTheGuard() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        for i in 1...5 { try store.append(sessionID: "a", role: .user, content: "m\(i)") }
        XCTAssertEqual(try store.resumeMessages(sessionID: "a", maxMessages: 5).count, 5)
        XCTAssertThrowsError(try store.resumeMessages(sessionID: "a", maxMessages: 4)) {
            XCTAssertEqual($0 as? ConversationStoreError, .resumeTooLarge(count: 5, limit: 4))
        }
        XCTAssertEqual(try store.resumeMessages(sessionID: "a", maxMessages: 0).count, 5)
        XCTAssertThrowsError(try store.resumeMessages(sessionID: "a", maxMessages: -1))
        try store.compact(sessionID: "a", seqRange: 1...4, summary: "summary")
        XCTAssertEqual(try store.resumeMessages(sessionID: "a", maxMessages: 2).map(\.content), ["summary", "m5"])
        XCTAssertEqual(ConversationStore.defaultMaxResumeMessages, 20_000)
    }

    // MARK: Compaction

    private func compactedFixture() throws -> ConversationStore {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        let words = ["alpha kickoff", "bravo plan", "charlie details", "delta tail", "echo latest"]
        for (i, w) in words.enumerated() { try store.append(sessionID: "a", role: i % 2 == 0 ? .user : .assistant, content: w) }
        try store.compact(sessionID: "a", seqRange: 2...3, summary: "Summary of the middle turns")
        return store
    }

    func testCompactionArchivesTheRangeAndSummaryTakesItsPlace() throws {
        let store = try compactedFixture()
        let active = try store.messages(sessionID: "a")
        XCTAssertEqual(active.map(\.content), ["alpha kickoff", "Summary of the middle turns", "delta tail", "echo latest"])
        XCTAssertEqual(active.map(\.seq), [1, 6, 7, 8])
        XCTAssertEqual(active.map(\.isSummary), [false, true, false, false])
        let display = try store.messages(sessionID: "a", scope: .display).map(\.content)
        XCTAssertEqual(display, ["alpha kickoff", "bravo plan", "charlie details", "Summary of the middle turns", "delta tail", "echo latest"])
        XCTAssertEqual(try store.messages(sessionID: "a", scope: .all).count, 8)
        XCTAssertEqual(try store.sessions().first?.messageCount, 4)
    }

    func testCompactionRollsBackEntirelyWhenTheSummaryInsertFails() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        for i in 1...3 { try store.append(sessionID: "a", role: .user, content: "m\(i)") }
        try raw().exec("CREATE TRIGGER boom BEFORE INSERT ON messages WHEN new.content = 'boom' BEGIN SELECT RAISE(ABORT, 'boom'); END;")
        try store.recordUsage(sessionID: "a", promptTokens: 77, atSeq: 3)
        XCTAssertThrowsError(try store.compact(sessionID: "a", seqRange: 1...2, summary: "boom"))
        XCTAssertEqual(try store.messages(sessionID: "a").map(\.content), ["m1", "m2", "m3"])
        XCTAssertEqual(try store.sessions().first?.messageCount, 3)
        XCTAssertEqual(try store.session(id: "a")?.lastPromptTokens, 77)
    }

    func testArchivedRowsStaySearchableAndEachMessageIsFoundOnce() throws {
        let store = try compactedFixture()
        let archived = try store.search("charlie")
        XCTAssertEqual(archived.map(\.seq), [3])
        XCTAssertEqual(archived.map(\.compacted), [true])
        XCTAssertEqual(try store.search("delta").map(\.seq), [7])
        XCTAssertEqual(try store.search("delta", options: SearchOptions(includeSuperseded: true)).map(\.seq).sorted(), [4, 7])
    }

    func testCompactingARangeWithNoActiveMessagesThrows() throws {
        let store = try compactedFixture()
        XCTAssertThrowsError(try store.compact(sessionID: "a", seqRange: 2...3, summary: "again")) {
            XCTAssertEqual($0 as? ConversationStoreError, .nothingToCompact(sessionID: "a", seqRanges: [2...3]))
        }
        XCTAssertThrowsError(try store.compact(sessionID: "zzz", seqRange: 1...1, summary: "x"))
    }

    func testCompactionClearsTheUsageAnchor() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        try store.append(sessionID: "a", [MessageDraft(role: .user, content: "q"), MessageDraft(role: .assistant, content: "a")])
        try store.recordUsage(sessionID: "a", promptTokens: 90_000, atSeq: 2)
        try store.compact(sessionID: "a", seqRange: 1...1, summary: "s")
        let session = try XCTUnwrap(store.session(id: "a"))
        XCTAssertNil(session.lastPromptTokens)
        XCTAssertNil(session.lastPromptTokensAtSeq)
    }

    // MARK: Usage

    func testRecordUsageStoresPromptTokensAndTheirSeq() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        try store.recordUsage(sessionID: "a", promptTokens: 4321, atSeq: 7)
        let session = try XCTUnwrap(store.session(id: "a"))
        XCTAssertEqual(session.lastPromptTokens, 4321)
        XCTAssertEqual(session.lastPromptTokensAtSeq, 7)
        XCTAssertThrowsError(try store.recordUsage(sessionID: "nope", promptTokens: 1, atSeq: 1))
    }

    // MARK: Search

    func testSearchRanksByBM25AndMarksSnippets() throws {
        let store = try makeStore()
        try store.createSession(title: "Dense", id: "dense")
        try store.createSession(title: "Sparse", id: "sparse")
        try store.append(sessionID: "sparse", role: .user,
                         content: "we mentioned kubernetes once in a long rambling sentence about many unrelated other things today")
        try store.append(sessionID: "dense", role: .user, content: "kubernetes kubernetes cluster kubernetes")
        let hits = try store.search("kubernetes")
        XCTAssertEqual(hits.map(\.sessionID), ["dense", "sparse"])
        XCTAssertTrue(hits[0].snippet.contains(">>>kubernetes<<<"), hits[0].snippet)
        XCTAssertEqual(hits[0].sessionTitle, "Dense")
    }

    func testUserTextNeverRaisesAnFTSSyntaxError() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: """
            meet me at user host about gateway run py it s 50 a b why e mc2 x y tag dollar bracket C path file TODO fix
            unbalanced near start
            """)
        let queries = ["it's", "gateway/run.py", "user@host", "a,b", "why?", "e=mc2", "a;b", "a!b", "a&b", "a|b", "x~y",
                       "#tag", "$dollar", "[bracket]", "<tag>", "C:\\path\\file", "TODO: fix", "\"unbalanced", "OR gateway NOT",
                       "NEAR(", "^start", "{x}", "50%"]
        for q in queries {
            let sanitized = FTSQuery.sanitize(q)
            XCTAssertFalse(sanitized.isEmpty, q)
            XCTAssertNoThrow(try store.conn.query("SELECT count(*) AS n FROM messages_fts WHERE messages_fts MATCH ?", [sanitized]), q)
            XCTAssertEqual(try store.search(q).count, 1, q)
        }
        XCTAssertEqual(FTSQuery.sanitize("AND"), "\"AND\"", "a query of only an operator word searches for the word")
        XCTAssertEqual(FTSQuery.sanitize("((( *** )))"), "")
    }

    func testSanitizerKeepsPhrasesPrefixesAndCompoundTerms() {
        XCTAssertEqual(FTSQuery.sanitize("hello world").split(separator: " "), ["hello", "world"])
        XCTAssertTrue(FTSQuery.sanitize("\"exact phrase\" more").contains("\"exact phrase\""))
        XCTAssertTrue(FTSQuery.sanitize("my-app.config.ts").contains("\"my-app.config.ts\""))
        XCTAssertEqual(FTSQuery.sanitize("deploy**"), "deploy*")
        XCTAssertEqual(FTSQuery.sanitize("*lead"), "lead")
        XCTAssertEqual(FTSQuery.sanitize("AND cats OR"), "cats")
        XCTAssertFalse(FTSQuery.sanitize("50%").contains("%"))
        XCTAssertTrue(FTSQuery.sanitize("完成50%").contains("%"))
        XCTAssertEqual(FTSQuery.sanitize(String(repeating: "a", count: 70_000)).count, FTSQuery.maxQueryChars)
    }

    func testPrefixAndPhraseQueriesMatch() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "the gateway restarted after the deploy")
        XCTAssertEqual(try store.search("gate*").count, 1)
        XCTAssertEqual(try store.search("\"gateway restarted\"").count, 1)
    }

    func testSortNewestAndOldestOrderByTime() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "release notes", timestamp: Date(timeIntervalSince1970: 10))
        try store.append(sessionID: "s", role: .user, content: "release release release", timestamp: Date(timeIntervalSince1970: 20))
        try store.append(sessionID: "s", role: .user, content: "release candidate", timestamp: Date(timeIntervalSince1970: 30))
        XCTAssertEqual(try store.search("release", options: SearchOptions(sort: .newest)).map(\.seq), [3, 2, 1])
        XCTAssertEqual(try store.search("release", options: SearchOptions(sort: .oldest)).map(\.seq), [1, 2, 3])
    }

    func testToolOutputIsIndexedByPrefixAndSearchedInFullOnlyWhenAskedFor() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .tool, content: "startmarker " + String(repeating: "x ", count: 5_000) + "needleword",
                         toolName: "bash")
        try store.append(sessionID: "s", role: .user, content: "what did the build print")
        XCTAssertEqual(try store.search("startmarker").map(\.role), [.tool])
        XCTAssertEqual(try store.search("needleword"), [])
        XCTAssertEqual(try store.search("needleword", options: SearchOptions(roles: [.tool])).map(\.seq), [1])
        XCTAssertEqual(try store.search("build", options: SearchOptions(roles: [.tool])), [])
        XCTAssertEqual(try store.search("build", options: SearchOptions(roles: [.user])).map(\.seq), [2])
    }

    func testStartBoundsFilterBySessionStart() throws {
        let store = try makeStore()
        try store.createSession(title: "old", id: "old", createdAt: Date(timeIntervalSince1970: 1_000))
        try store.createSession(title: "new", id: "new", createdAt: Date(timeIntervalSince1970: 2_000))
        try store.append(sessionID: "old", role: .user, content: "budget review")
        try store.append(sessionID: "new", role: .user, content: "budget review")
        let after = SearchOptions(startedAfter: Date(timeIntervalSince1970: 2_000))
        XCTAssertEqual(try store.search("budget", options: after).map(\.sessionID), ["new"])
        let before = SearchOptions(startedBefore: Date(timeIntervalSince1970: 2_000))
        XCTAssertEqual(try store.search("budget", options: before).map(\.sessionID), ["old"])
    }

    func testAMultiWordMissIsRetriedMatchingAnyTerm() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "Sarah prefers the standup meeting scheduled on Thursday mornings")
        XCTAssertEqual(try store.search("when does Sarah like her standup scheduled").count, 1)
        XCTAssertEqual(try store.search("Sarah NOT standup").count, 0)
    }

    func testCJKUsesTheTrigramIndexAndShortTokensFallBackToSubstring() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "我们今天讨论了数据库迁移")
        let trigram = try store.search("数据库")
        XCTAssertEqual(trigram.count, 1)
        XCTAssertTrue(trigram[0].snippet.contains(">>>数据库<<<"), trigram[0].snippet)
        let short = try store.search("数据")
        XCTAssertEqual(short.count, 1)
        XCTAssertFalse(short[0].snippet.contains(">>>"))
        XCTAssertEqual(try store.search("广西 OR 迁移").count, 1)
    }

    func testLatinWordGluedToCJKIsFoundThroughTheTrigramRetry() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "修改youer服务端")
        XCTAssertEqual(try store.search("youer").count, 1)
    }

    func testHitsCarryTheirNeighborsAsContext() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "before"), MessageDraft(role: .assistant, content: "the target"),
                                          MessageDraft(role: .user, content: "after")])
        let hit = try XCTUnwrap(store.search("target").first)
        XCTAssertEqual(hit.context.map(\.content), ["before", "the target", "after"])
        XCTAssertEqual(try store.search("target", options: SearchOptions(includeContext: false)).first?.context, [])
    }

    func testIndexFollowsContentEditsAndDeletes() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "original wording")
        try raw().run("UPDATE messages SET content = 'revised wording' WHERE session_id = 's'")
        XCTAssertEqual(try store.search("original"), [])
        XCTAssertEqual(try store.search("revised").count, 1)
        try raw().run("DELETE FROM messages WHERE session_id = 's'")
        XCTAssertEqual(try store.search("revised"), [])
    }

    func testCorruptIndexFailsOpenToSubstringSearchAndIsRebuiltOnNextOpen() throws {
        var store: ConversationStore? = try makeStore()
        try store!.createSession(title: "t", id: "s")
        try store!.append(sessionID: "s", role: .user, content: "alpha first qx")
        try corruptWordIndex()
        try store!.append(sessionID: "s", role: .user, content: "gamma second qz")
        let fallback = try store!.search("qz")
        XCTAssertEqual(fallback.map(\.seq), [2])
        XCTAssertFalse(fallback[0].snippet.contains(">>>"))
        XCTAssertNotNil(try raw().one("SELECT 1 AS hit FROM state_meta WHERE key = 'fts_stale'"))
        store = nil
        let reopened = try makeStore()
        XCTAssertNil(try raw().one("SELECT 1 AS hit FROM state_meta WHERE key = 'fts_stale'"))
        XCTAssertTrue(try XCTUnwrap(reopened.search("qz").first).snippet.contains(">>>qz<<<"))
        XCTAssertEqual(try reopened.search("qx").count, 1, "two-letter terms only the rebuilt word index can serve")
    }

    func testConcurrentAppendsAndReadsKeepSeqsUnique() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        try store.createSession(title: "b", id: "b")
        DispatchQueue.concurrentPerform(iterations: 200) { i in
            let session = i % 2 == 0 ? "a" : "b"
            do {
                try store.append(sessionID: session, role: .user, content: "parallel message \(i)")
                _ = try store.search("parallel")
                _ = try store.messages(sessionID: session)
            } catch {
                XCTFail("\(error)")
            }
        }
        XCTAssertEqual(try store.messages(sessionID: "a").map(\.seq), Array(1...100))
        XCTAssertEqual(try store.messages(sessionID: "b").map(\.seq), Array(1...100))
    }

    // MARK: session_search tool

    private func toolFixture() throws -> ConversationStore {
        let store = try makeStore()
        try store.createSession(title: "Deploy pipeline", id: "deploy", createdAt: jan15)
        try store.createSession(title: "Garden notes", id: "garden", createdAt: jan15.addingTimeInterval(60))
        try store.createSession(title: "Current chat", id: "current", createdAt: jan15.addingTimeInterval(120))
        try store.append(sessionID: "deploy", [
            MessageDraft(role: .user, content: "Goal: fix the staging deploy"),
            MessageDraft(role: .assistant, content: "Looking at the rollout"),
            MessageDraft(role: .tool, content: "kubectl output", toolName: "bash"),
            MessageDraft(role: .assistant, content: "The deploy failed on the migration step"),
            MessageDraft(role: .user, content: "ok retry it"),
            MessageDraft(role: .assistant, content: "Resolved: deploy is green"),
        ])
        try store.append(sessionID: "garden", role: .user, content: "When should I deploy the tomato seedlings outside?")
        try store.append(sessionID: "current", role: .user, content: "remind me what broke in the deploy")
        return store
    }

    func testToolDefinitionIsWellFormed() throws {
        XCTAssertEqual(SessionSearchTool.name, "session_search")
        XCTAssertTrue(SessionSearchTool.description.contains("Searches conversation history ONLY"))
        let schema = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(SessionSearchTool.inputSchema.utf8)) as? [String: Any])
        let properties = try XCTUnwrap(schema["properties"] as? [String: Any])
        XCTAssertEqual(Set(properties.keys), ["query", "limit", "sort", "detail", "after", "before", "exclude_session_ids",
                                              "session_id", "around_seq", "window", "role_filter"])
    }

    func testDiscoveryGroupsBySessionAndHydratesOnlyTheTopResult() throws {
        let store = try toolFixture()
        let result = SessionSearchTool.run(SessionSearchRequest(query: "deploy"), store: store, currentSessionID: "current", timeZone: utc)
        let p = try discovery(result)
        XCTAssertEqual(p.results.map(\.sessionID), ["deploy", "garden"])
        XCTAssertEqual(p.results.map(\.detail), ["full", "compact"])
        XCTAssertEqual(p.results[0].title, "Deploy pipeline")
        XCTAssertEqual(p.results[0].when, "January 15, 2027 at 08:00 AM")
        XCTAssertEqual(p.results[0].messagesBefore + p.results[0].messagesAfter, 5)
        XCTAssertFalse(p.results[0].messages.contains { $0.role == .tool })
        XCTAssertEqual(p.results[1].messages.count, 1)
        XCTAssertEqual(p.results[1].messages.first?.anchor, true)
        XCTAssertEqual(p.sessionsSearched, 2)
        let full = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "deploy", detail: "full"), store: store,
                                                       currentSessionID: "current"))
        XCTAssertEqual(full.results.map(\.detail), ["full", "full"])
        let obj = try json(result)
        XCTAssertEqual(obj["success"] as? Bool, true)
        XCTAssertEqual(obj["mode"] as? String, "discover")
        XCTAssertNotNil((obj["results"] as? [[String: Any]])?.first?["match_seq"])
    }

    func testDiscoveryHidesTheLiveCurrentSessionButNotItsCompactedHistory() throws {
        let store = try toolFixture()
        let none = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "broke"), store: store, currentSessionID: "current"))
        XCTAssertEqual(none.results, [])
        XCTAssertEqual(none.sessionsSearched, 0)
        let nothing = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "xylophone"), store: store, currentSessionID: nil))
        XCTAssertEqual(nothing.message, SessionSearchTool.noMatchMessage)
        try store.append(sessionID: "current", role: .assistant, content: "the migration step broke")
        try store.compact(sessionID: "current", seqRange: 1...2, summary: "earlier: deploy breakage")
        let found = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "broke"), store: store, currentSessionID: "current"))
        XCTAssertEqual(found.results.map(\.sessionID), ["current"])
        let unscoped = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "migration step"), store: store, currentSessionID: nil))
        XCTAssertEqual(Set(unscoped.results.map(\.sessionID)), ["deploy", "current"])
    }

    func testDiscoveryOmitsExcludedSessionsAndHonorsLimit() throws {
        let store = try toolFixture()
        let excluded = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "deploy", excludeSessionIDs: ["deploy"]),
                                                           store: store, currentSessionID: "current"))
        XCTAssertEqual(excluded.results.map(\.sessionID), ["garden"])
        let one = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "deploy", limit: 1), store: store, currentSessionID: "current"))
        XCTAssertEqual(one.results.count, 1)
        XCTAssertEqual(SessionSearchTool.clamp(nil, default: 3, 1, 10), 3)
        XCTAssertEqual(SessionSearchTool.clamp(50, default: 3, 1, 10), 10)
        XCTAssertEqual(SessionSearchTool.clamp(0, default: 3, 1, 10), 1)
    }

    func testDiscoveryTimeBoundsAcceptISOAndRelativeValues() throws {
        let store = try toolFixture()
        let now = jan15.addingTimeInterval(3630)
        let recent = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "deploy", after: "2027-01-15T08:00:30"),
                                                         store: store, currentSessionID: "current"))
        XCTAssertEqual(recent.results.map(\.sessionID), ["garden"])
        let old = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "deploy", before: "1h"), store: store,
                                                      currentSessionID: "current", now: now))
        XCTAssertEqual(old.results.map(\.sessionID), ["deploy"])
        XCTAssertEqual(try SessionSearchTool.parseBound("2w", now: now), now.addingTimeInterval(-14 * 86_400))
        XCTAssertEqual(try SessionSearchTool.parseBound("2027-01-15", now: now), Date(timeIntervalSince1970: 1_799_971_200))
        let bad = SessionSearchTool.run(SessionSearchRequest(query: "deploy", after: "last tuesday"), store: store, currentSessionID: nil)
        XCTAssertEqual(try json(bad)["error"] as? String,
                       "invalid time bound: 'last tuesday' (expected ISO date/datetime like 2026-07-01, or a relative duration like 7d, 24h, 2w)")
        XCTAssertEqual(try json(bad)["success"] as? Bool, false)
    }

    func testATitleMatchLeadsTheResults() throws {
        let store = try toolFixture()
        let p = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "'Garden notes'"), store: store, currentSessionID: "current"))
        XCTAssertEqual(p.results.first?.matchedRole, "session_title")
        XCTAssertEqual(p.results.first?.sessionID, "garden")
        XCTAssertEqual(p.results.first?.snippet, "Session title matched: Garden notes")
    }

    func testBookendsLeaveOutCompactionSummaries() throws {
        let store = try makeStore()
        try store.createSession(title: "long", id: "long")
        for i in 1...20 { try store.append(sessionID: "long", role: i % 2 == 0 ? .assistant : .user, content: i == 3 ? "the zebra question" : "turn \(i)") }
        try store.compact(sessionID: "long", seqRange: 1...19, summary: "turn summary")
        let p = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "zebra"), store: store, currentSessionID: nil))
        let entry = try XCTUnwrap(p.results.first)
        XCTAssertEqual(entry.matchSeq, 3)
        XCTAssertEqual(entry.bookendEnd.map(\.seq), [19, 22], "the superseded original of 22 (seq 20) is not shown")
        XCTAssertFalse((entry.bookendStart + entry.bookendEnd).contains { $0.content == "turn summary" })
    }

    func testScrollReturnsAWindowAndRejectsTheLiveContext() throws {
        let store = try toolFixture()
        let result = SessionSearchTool.run(SessionSearchRequest(sessionID: "deploy", aroundSeq: 4, window: 1), store: store,
                                           currentSessionID: "current")
        guard case .scroll(let p) = result.payload else { return XCTFail(result.text) }
        XCTAssertEqual(p.messages.map(\.seq), [3, 4, 5])
        XCTAssertEqual(p.messages.filter { $0.anchor == true }.map(\.seq), [4])
        XCTAssertEqual(p.sessionMeta.title, "Deploy pipeline")
        guard case .scroll(let wide) = SessionSearchTool.run(SessionSearchRequest(sessionID: "deploy", aroundSeq: 1, window: 99),
                                                            store: store, currentSessionID: nil).payload else { return XCTFail() }
        XCTAssertEqual(wide.window, 20)
        let live = SessionSearchTool.run(SessionSearchRequest(sessionID: "current", aroundSeq: 1), store: store, currentSessionID: "current")
        XCTAssertEqual(live.payload, .error("scroll rejected: anchor lives in the current session (already in your active context)"))
        let missing = SessionSearchTool.run(SessionSearchRequest(sessionID: "deploy", aroundSeq: 99), store: store, currentSessionID: nil)
        XCTAssertEqual(missing.payload, .error("around_seq 99 not in session_id deploy"))
    }

    func testReadShowsHeadAndTailCapsContentAndStripsANSI() throws {
        let store = try makeStore()
        try store.createSession(title: "big", id: "big")
        try store.append(sessionID: "big", role: .tool, content: "\u{1B}[31mred\u{1B}[0m " + String(repeating: "y", count: 3_000))
        for i in 2...40 { try store.append(sessionID: "big", role: .user, content: "m\(i)") }
        guard case .read(let p) = SessionSearchTool.run(SessionSearchRequest(sessionID: "big"), store: store, currentSessionID: nil).payload
        else { return XCTFail() }
        XCTAssertTrue(p.truncated)
        XCTAssertEqual(p.messageCount, 40)
        XCTAssertEqual(p.messages.map(\.seq), Array(1...20) + Array(31...40))
        XCTAssertTrue(p.messages[0].content.hasPrefix("red y"))
        XCTAssertEqual(p.messages[0].contentTruncated, true)
        XCTAssertEqual(p.messages[0].content.count, 2_001)
        XCTAssertEqual(SessionSearchTool.run(SessionSearchRequest(sessionID: "nope"), store: store, currentSessionID: nil).payload,
                       .error("session_id not found: nope"))
    }

    func testBrowseListsRecentSessionsWithoutTheCurrentOne() throws {
        let store = try toolFixture()
        guard case .browse(let p) = SessionSearchTool.run(SessionSearchRequest(), store: store, currentSessionID: "current").payload
        else { return XCTFail() }
        XCTAssertEqual(Set(p.results.map(\.sessionID)), ["deploy", "garden"])
        XCTAssertEqual(p.message, "Showing 2 most recent sessions. Pass a query= to search, or session_id+around_seq to scroll.")
        XCTAssertEqual(p.results.first { $0.sessionID == "garden" }?.preview, "When should I deploy the tomato seedlings outside?")
    }

    func testToolInputDecodesLeniently() throws {
        let store = try toolFixture()
        let limited = Data(#"{"query":"deploy","limit":"1"}"#.utf8)
        XCTAssertEqual(try discovery(SessionSearchTool.run(json: limited, store: store, currentSessionID: "current")).results.count, 1)
        let excluded = Data(#"{"query":"deploy","exclude_session_ids":"deploy","role_filter":"user, assistant"}"#.utf8)
        XCTAssertEqual(try discovery(SessionSearchTool.run(json: excluded, store: store, currentSessionID: "current")).results.map(\.sessionID),
                       ["garden"])
        let scroll = SessionSearchTool.run(json: Data(#"{"session_id":"deploy","around_seq":"x"}"#.utf8), store: store, currentSessionID: nil)
        XCTAssertEqual(scroll.payload, .error("scroll requires integer around_seq"))
        XCTAssertEqual(SessionSearchTool.run(json: Data("[1]".utf8), store: store, currentSessionID: nil).payload,
                       .error("session_search arguments must be a JSON object"))
    }

    // MARK: Review fixes

    func testFallbackFormsAnswerAQueryFTS5CannotParse() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "alpha bravo")
        XCTAssertThrowsError(try store.conn.query("SELECT 1 FROM messages_fts WHERE messages_fts MATCH ?", ["alpha: AND"]))
        let hits = try store.wordIndexRows("alpha: AND", SearchOptions())
        XCTAssertEqual(hits.map(\.seq), [1])
        XCTAssertTrue(hits[0].snippet.contains(">>>alpha<<<"), "answered by the word index, not the substring scan")
    }

    func testReviewerPunctuationQueriesAllMatch() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "hello world from the deploy script using node js eg config and flag")
        for q in ["hello.", "`deploy`", "-flag", "e.g. config", "hello world.", "deploy -", "flag AND AND node", "hello OR OR world",
                  "node.js.", "what about hello?", "al*pha* hello", "\"\" hello", "\"!!\" hello"] {
            XCTAssertEqual(try store.search(q).count, 1, q)
        }
    }

    func testSanitizerOutputParsesWithoutTheQuotedRetry() throws {
        let store = try makeStore()
        let cases = ["flag AND AND node": "flag AND node", "NOT NOT alpha AND NOT": "alpha", "hello OR OR world": "hello OR world",
                     "al*pha": "al* pha", "al*pha*": "al* pha*", "x*y": "x* y", "deploy -staging": "deploy staging", "rm -rf build": "rm rf build",
                     "NOT": "\"NOT\"", "AND OR": "\"AND\" \"OR\"", "\"\u{1F680}\"": "\"\u{1F680}\"", "can not": "can not", "\"\" hello": "hello", "\"!!\" hello": "hello", "user@host": "user host",
                     "node.js.": "\"node.js\"", "e.g. config": "\"e.g\" config", "`deploy`": "deploy", "-flag": "flag"]
        for (query, expected) in cases {
            XCTAssertEqual(FTSQuery.sanitize(query), expected, query)
            XCTAssertNoThrow(try store.conn.query("SELECT 1 FROM messages_fts WHERE messages_fts MATCH ?", [expected]), query)
        }
    }

    func testANonFiniteTimestampStillReadsSuccessfully() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "infinite clock", timestamp: Date(timeIntervalSince1970: .infinity))
        let obj = try json(SessionSearchTool.run(SessionSearchRequest(sessionID: "s"), store: store, currentSessionID: nil))
        XCTAssertEqual(obj["success"] as? Bool, true)
        XCTAssertEqual((obj["messages"] as? [[String: Any]])?.first?["content"] as? String, "infinite clock")
    }

    func testLimitPlusOneNeverOverflows() {
        XCTAssertEqual(ConversationStore.limitPlusOne(4), 5)
        XCTAssertEqual(ConversationStore.limitPlusOne(Int.max), -1)
    }

    func testIntMaxLimitsNeitherCrashNorTruncate() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        for i in 1...10 { try store.append(sessionID: "a", role: .user, content: "m\(i)") }
        XCTAssertEqual(try store.resumeMessages(sessionID: "a", maxMessages: Int.max).count, 10)
        XCTAssertEqual(try store.window(sessionID: "a", aroundSeq: 5, radius: Int.max).messages.count, 10)
    }

    func testCompactReportsWhereEachLiveMessageMovedAndResolvesOldSeqs() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        for i in 1...6 { try store.append(sessionID: "a", role: .user, content: "m\(i)") }
        let result = try store.compact(sessionID: "a", seqRanges: [2...2, 4...4], summary: "sum")
        XCTAssertEqual(result.summary.seq, 7)
        XCTAssertEqual(result.resequenced, [3: 8, 5: 9, 6: 10])
        XCTAssertEqual(try store.messages(sessionID: "a").map(\.content), ["m1", "sum", "m3", "m5", "m6"])
        XCTAssertEqual(try store.resolveActiveSeq(sessionID: "a", seq: 5), 9)
        XCTAssertEqual(try store.resolveActiveSeq(sessionID: "a", seq: 1), 1)
        XCTAssertNil(try store.resolveActiveSeq(sessionID: "a", seq: 2), "archived into the summary, no live copy")
        XCTAssertNil(try store.resolveActiveSeq(sessionID: "a", seq: 99))
    }

    func testStaleMarkerWrittenByAnotherProcessSwitchesSearchToTheCanonicalRows() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        XCTAssertEqual(try store.search("anything"), [])
        let other = try raw()
        try other.run("INSERT INTO state_meta (key, value) VALUES ('fts_stale', '1')")
        try ConversationSchema.dropTriggers(other, ConversationSchema.baseTriggers + ConversationSchema.trigramTriggers)
        try other.run("INSERT INTO messages (session_id, seq, role, content, timestamp) VALUES ('s', 1, 'user', 'written while detached', 1)")
        XCTAssertEqual(try store.search("detached").map(\.seq), [1])
    }

    func testLastActivityNeverMovesBackward() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a", createdAt: Date(timeIntervalSince1970: 100))
        try store.append(sessionID: "a", role: .user, content: "new", timestamp: Date(timeIntervalSince1970: 5_000))
        try store.append(sessionID: "a", role: .user, content: "imported old", timestamp: Date(timeIntervalSince1970: 200))
        XCTAssertEqual(try store.sessions().first?.lastActive, Date(timeIntervalSince1970: 5_000))
    }

    func testTimeBoundsReadWhatPythonFromisoformatReads() throws {
        let base = Date(timeIntervalSince1970: 1_799_971_200)
        let accepted: [(String, Double)] = [
            ("2027-01-15", 0), ("20270115", 0), ("2027-01-15T08", 8 * 3600), ("2027-01-15 08:00", 8 * 3600),
            ("2027-01-15x08:00", 8 * 3600), ("2027-01-15T0800", 8 * 3600), ("2027-01-15T08:00:30.5", 8 * 3600 + 30.5),
            ("2027-01-15T08:00:30,5", 8 * 3600 + 30.5), ("2027-01-15T08:00:30+02:00", 6 * 3600 + 30),
            ("2027-01-15T08:00+0530", 8 * 3600 - 5.5 * 3600), ("2027-01-15T08Z", 8 * 3600), ("2027-01-15T24:00", 86_400),
        ]
        for (text, offset) in accepted {
            XCTAssertEqual(try SessionSearchTool.parseBound(text, now: base), base.addingTimeInterval(offset), text)
        }
        for text in ["2027-1-5", "2027-01-15Z", "2026-02-30", "2027-01-15T08:00:60", "2027-0115", "7 days", "1.5d"] {
            XCTAssertThrowsError(try SessionSearchTool.parseBound(text, now: base), text)
        }
    }

    func testSubstringSnippetsFindTheTermRegardlessOfCase() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .tool, content: String(repeating: "filler ", count: 40) + "Kubernetes rollout", toolName: "bash")
        let hit = try XCTUnwrap(store.search("kubernetes", options: SearchOptions(roles: [.tool])).first)
        XCTAssertTrue(hit.snippet.contains("Kubernetes rollout"), hit.snippet)
    }

    func testLikeWildcardsInTheQueryMatchLiterally() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .tool, content: "file50ax", toolName: "ls")
        try store.append(sessionID: "s", role: .tool, content: "file50_x", toolName: "ls")
        XCTAssertEqual(try store.search("50_x", options: SearchOptions(roles: [.tool])).map(\.seq), [2])
    }

    func testTitleMatchIgnoresCaseAndStandsDownWhenTheTitleIsShared() throws {
        let store = try makeStore()
        try store.createSession(title: "Release Plan", id: "one", createdAt: Date(timeIntervalSince1970: 100))
        try store.append(sessionID: "one", role: .user, content: "kickoff")
        try store.createSession(title: "release plan #2", id: "numbered", createdAt: Date(timeIntervalSince1970: 200))
        let single = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "release plan"), store: store, currentSessionID: nil))
        XCTAssertEqual(single.results.first?.sessionID, "one")
        XCTAssertEqual(single.results.first?.matchedRole, "session_title")
        try store.createSession(title: "RELEASE PLAN", id: "two", createdAt: Date(timeIntervalSince1970: 300))
        let shared = try discovery(SessionSearchTool.run(SessionSearchRequest(query: "release plan"), store: store, currentSessionID: nil))
        XCTAssertFalse(shared.results.contains { $0.matchedRole == "session_title" })
    }

    func testScrollReachesTheCurrentSessionsArchivedHistory() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        for i in 1...6 { try store.append(sessionID: "a", role: .user, content: "m\(i)") }
        try store.compact(sessionID: "a", seqRange: 1...3, summary: "sum")
        let result = SessionSearchTool.run(SessionSearchRequest(sessionID: "a", aroundSeq: 2, window: 1), store: store, currentSessionID: "a")
        guard case .scroll(let p) = result.payload else { return XCTFail(result.text) }
        XCTAssertEqual(p.messages.map(\.content), ["m1", "m2", "m3"])
    }

    func testDeletingCJKMessagesKeepsTheTrigramIndexConsistent() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "数据库迁移完成")
        try store.deleteSession(id: "s")
        XCTAssertNoThrow(try raw().exec("INSERT INTO messages_fts_trigram(messages_fts_trigram, rank) VALUES('integrity-check', 1)"),
                         "the index still holds the deleted row")
    }

    // MARK: applyCompaction

    private func applyFixture() throws -> (ConversationStore, [ConversationMessage]) {
        let store = try makeStore()
        try store.createSession(title: "Migration", id: "s")
        let rows = try store.append(sessionID: "s", [
            MessageDraft(role: .user, content: "goal migrate the database"),
            MessageDraft(role: .assistant, content: "plan alpha"),
            MessageDraft(role: .assistant, content: "calling psql", toolName: "psql", toolCallID: "c1"),
            MessageDraft(role: .tool, content: "huge psql output", toolName: "psql", toolCallID: "c1"),
            MessageDraft(role: .user, content: "detail bravo"),
            MessageDraft(role: .assistant, content: "answer charlie"),
            MessageDraft(role: .user, content: "latest question delta"),
            MessageDraft(role: .assistant, content: "latest answer echo"),
        ])
        try store.recordUsage(sessionID: "s", promptTokens: 50_000, atSeq: 8)
        var pruned = rows[3]
        pruned.content = "[pruned tool output]"
        let summary = ConversationMessage(sessionID: "s", seq: 0, role: .user, content: "summary of the migration", isSummary: true)
        return (store, [summary, rows[0], rows[2], pruned, rows[6], rows[7]])
    }

    func testApplyCompactionReplacesTheLiveTranscriptAndArchivesWhatItDrops() throws {
        let (store, newActive) = try applyFixture()
        let applied = try store.applyCompaction(sessionID: "s", newActive: newActive, watermark: 8, timestamp: jan15)
        XCTAssertEqual(applied.archivedSeqs, [2, 5, 6])
        XCTAssertEqual(applied.summarySeqs, [9])
        XCTAssertEqual(applied.oldToNew, [1: 10, 3: 11, 4: 12, 7: 13, 8: 14])
        let live = try store.messages(sessionID: "s")
        XCTAssertEqual(live.map(\.content), newActive.map(\.content))
        XCTAssertEqual(live.map(\.seq), Array(9...14))
        XCTAssertEqual(live.map(\.toolCallID), [nil, nil, "c1", "c1", nil, nil])
        XCTAssertEqual(live.map(\.toolName), [nil, nil, "psql", "psql", nil, nil])
        XCTAssertEqual(live.map(\.role), [.user, .user, .assistant, .tool, .user, .assistant])
        XCTAssertEqual(live.map(\.isSummary), [true, false, false, false, false, false])
        XCTAssertEqual(live[0].timestamp, jan15)
        XCTAssertEqual(live[1].timestamp, newActive[1].timestamp)
        XCTAssertEqual(try store.sessions().first?.messageCount, 6)
        XCTAssertNil(try store.session(id: "s")?.lastPromptTokens)
        XCTAssertNil(try store.session(id: "s")?.lastPromptTokensAtSeq)
    }

    func testApplyCompactionRewrittenRowIsLiveAndItsOriginalStaysSearchableHistory() throws {
        let (store, newActive) = try applyFixture()
        try store.applyCompaction(sessionID: "s", newActive: newActive, watermark: 8)
        let original = try store.search("huge", options: SearchOptions(roles: [.tool]))
        XCTAssertEqual(original.map(\.seq), [4])
        XCTAssertEqual(original.map(\.compacted), [true])
        XCTAssertTrue(try store.messages(sessionID: "s", scope: .display).contains { $0.content == "huge psql output" })
        XCTAssertEqual(try store.search("pruned", options: SearchOptions(roles: [.tool])).map(\.seq), [12])
        XCTAssertEqual(try store.search("goal").map(\.seq), [10], "a carried message is found once, as its live copy")
    }

    func testApplyCompactionDropsAreSearchableHistoryEvenWithKeptRowsBetween() throws {
        let (store, newActive) = try applyFixture()
        try store.applyCompaction(sessionID: "s", newActive: newActive, watermark: 8)
        for (word, seq) in [("alpha", 2), ("bravo", 5), ("charlie", 6)] {
            let hits = try store.search(word)
            XCTAssertEqual(hits.map(\.seq), [seq], word)
            XCTAssertEqual(hits.map(\.compacted), [true], word)
            XCTAssertTrue(hits[0].snippet.contains(">>>\(word)<<<"), hits[0].snippet)
        }
    }

    func testApplyCompactionTwiceArchivesTheFirstSummary() throws {
        let (store, newActive) = try applyFixture()
        try store.applyCompaction(sessionID: "s", newActive: newActive, watermark: 8)
        let live = try store.messages(sessionID: "s")
        let second = ConversationMessage(sessionID: "s", seq: 0, role: .user, content: "second summary", isSummary: true)
        let applied = try store.applyCompaction(sessionID: "s", newActive: [second] + live.suffix(2), watermark: live.last!.seq)
        XCTAssertEqual(applied.archivedSeqs, [9, 10, 11, 12])
        XCTAssertEqual(try store.messages(sessionID: "s").map(\.content), ["second summary", "latest question delta", "latest answer echo"])
        XCTAssertEqual(try store.search("migration").map(\.compacted), [true])
        XCTAssertEqual(try store.search("delta").map(\.seq), [16])
    }

    func testScrollAcrossAnAppliedCompactionShowsEachMessageOnceInOrder() throws {
        let (store, newActive) = try applyFixture()
        try store.applyCompaction(sessionID: "s", newActive: newActive, watermark: 8)
        let result = SessionSearchTool.run(SessionSearchRequest(sessionID: "s", aroundSeq: 5, window: 20), store: store, currentSessionID: nil)
        guard case .scroll(let p) = result.payload else { return XCTFail(result.text) }
        XCTAssertEqual(p.messages.map(\.seq), [2, 4, 5, 6] + Array(9...14), "the rewritten tool row's original (4) is history too")
        XCTAssertEqual(Set(p.messages.map(\.content)).count, p.messages.count)
    }

    func testApplyCompactionRollsBackOnAFailedInsert() throws {
        let (store, newActive) = try applyFixture()
        try raw().exec("CREATE TRIGGER boom BEFORE INSERT ON messages WHEN new.content = 'latest question delta' BEGIN SELECT RAISE(ABORT, 'boom'); END;")
        let before = try store.messages(sessionID: "s", scope: .all)
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: newActive, watermark: 8))
        XCTAssertEqual(try store.messages(sessionID: "s", scope: .all), before)
        XCTAssertEqual(try store.sessions().first?.messageCount, 8)
        XCTAssertEqual(try store.session(id: "s")?.lastPromptTokens, 50_000)
        XCTAssertEqual(try store.search("summary"), [])
        XCTAssertEqual(try store.search("huge", options: SearchOptions(roles: [.tool])).map(\.seq), [4])
        XCTAssertNoThrow(try raw().exec("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')"))
    }

    func testApplyCompactionRejectsASeqThatIsNotLive() throws {
        let (store, newActive) = try applyFixture()
        var ghost = newActive[1]
        ghost.seq = 99
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: [ghost], watermark: 8)) {
            XCTAssertEqual($0 as? ConversationStoreError, .notActive(sessionID: "s", seq: 99))
        }
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: [newActive[1], newActive[1]], watermark: 8))
        XCTAssertEqual(try store.messages(sessionID: "s").count, 8)
    }

    // MARK: Round 2

    func testApplyCompactionKeepsMessagesThatArrivedAfterTheWatermarkLive() throws {
        let (store, newActive) = try applyFixture()
        try store.append(sessionID: "s", role: .user, content: "late arrival foxtrot")
        let applied = try store.applyCompaction(sessionID: "s", newActive: newActive, watermark: 8)
        XCTAssertEqual(applied.lateSeqs, [9: 16])
        XCTAssertEqual(applied.archivedSeqs, [2, 5, 6])
        XCTAssertEqual(try store.messages(sessionID: "s").map(\.content), newActive.map(\.content) + ["late arrival foxtrot"])
        XCTAssertEqual(try store.search("foxtrot").map(\.seq), [16])
        XCTAssertEqual(try store.sessions().first?.messageCount, 7)
    }

    func testApplyCompactionRefusesToSplitAToolCallFromItsResult() throws {
        let (store, newActive) = try applyFixture()
        let before = try store.messages(sessionID: "s", scope: .all)
        let resultWithoutCall = newActive.filter { $0.seq != 3 }
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: resultWithoutCall, watermark: 8)) {
            XCTAssertEqual($0 as? ConversationStoreError, .brokenToolPairing(toolCallID: "c1"))
        }
        let callWithoutResult = newActive.filter { $0.seq != 4 }
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: callWithoutResult, watermark: 8)) {
            XCTAssertEqual($0 as? ConversationStoreError, .brokenToolPairing(toolCallID: "c1"))
        }
        XCTAssertEqual(try store.messages(sessionID: "s", scope: .all), before)
        XCTAssertNoThrow(try store.applyCompaction(sessionID: "s", newActive: newActive.filter { $0.seq != 3 && $0.seq != 4 }, watermark: 8))
    }

    func testApplyCompactionWithNothingNeedsExplicitPermission() throws {
        let (store, _) = try applyFixture()
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: [], watermark: 8)) {
            XCTAssertEqual($0 as? ConversationStoreError, .emptyCompaction(sessionID: "s"))
        }
        XCTAssertEqual(try store.messages(sessionID: "s").count, 8)
        XCTAssertEqual(try store.applyCompaction(sessionID: "s", newActive: [], watermark: 8, allowEmpty: true).archivedSeqs, Array(1...8))
    }

    func testApplyCompactionRejectsAMessageOfAnotherSession() throws {
        let (store, newActive) = try applyFixture()
        var foreign = newActive[1]
        foreign.sessionID = "elsewhere"
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: [newActive[0], foreign], watermark: 8)) {
            XCTAssertEqual($0 as? ConversationStoreError, .foreignMessage(sessionID: "s", messageSessionID: "elsewhere"))
        }
    }

    func testTruncatingAnOverlongQueryKeepsItsExclusions() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        try store.append(sessionID: "t", role: .user, content: "q secret")
        let long = Array(repeating: "q", count: 250).joined(separator: " ") + " NOT secret"
        let sanitized = FTSQuery.sanitize(long)
        XCTAssertLessThanOrEqual(FTSQuery.tokens(sanitized).count, FTSQuery.maxUnits)
        XCTAssertTrue(sanitized.hasSuffix("NOT secret"), String(sanitized.suffix(40)))
        XCTAssertEqual(try store.search(long), [])
    }

    // MARK: Fidelity round 2 probes, asserting

    func testFidelityR2SanitizerQueriesFindTheMessageUnlessAPhraseIsAbsent() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "s")
        try store.append(sessionID: "s", role: .user, content: "hello world from the deploy script using node js eg config and flag x y")
        for q in ["hello.", "`deploy`", "-flag", "e.g. config", "hello world.", "deploy -", "flag AND AND node", "hello OR OR world",
                  "node.js.", "\"hello world\"", "depl*", "x*y", "hello NEAR world", "NEAR(hello world)", "hello AND", "NOT hello"] {
            XCTAssertEqual(try store.search(q).count, 1, q)
        }
        XCTAssertEqual(try store.search("\"world hello\"").count, 0, "a phrase in the wrong order is absent")
        XCTAssertEqual(try store.search("hello -world").count, 1, "a leading hyphen is literal, as in Hermes")
    }

    func testFidelityR2CompactionShowsEachMessageOnce() throws {
        let store = try makeStore()
        try store.createSession(title: "a", id: "a")
        for i in 1...10 { try store.append(sessionID: "a", role: i % 2 == 0 ? .assistant : .user, content: "turn\(i) word\(i)") }
        try store.compact(sessionID: "a", seqRange: 1...6, summary: "summary one")
        let window = try store.window(sessionID: "a", aroundSeq: 6, radius: 5).messages.map(\.content)
        XCTAssertEqual(window, ["turn1 word1", "turn2 word2", "turn3 word3", "turn4 word4", "turn5 word5", "turn6 word6",
                                "summary one", "turn7 word7", "turn8 word8", "turn9 word9", "turn10 word10"])
        XCTAssertEqual(try store.search("summary").first?.context.map(\.content), ["turn6 word6", "summary one", "turn7 word7"])
        XCTAssertEqual(try store.search("word8").map(\.seq), [13])
    }

    func testFidelityR2TitleLookupIgnoresNumberedSiblings() throws {
        let store = try makeStore()
        try store.createSession(title: "Deploy", id: "old", createdAt: Date(timeIntervalSince1970: 100))
        try store.createSession(title: "deploy #2 retro", id: "num", createdAt: Date(timeIntervalSince1970: 50))
        XCTAssertEqual(try store.sessionsTitled("deploy").map(\.id), ["old"])
    }

    // MARK: Round 3

    func testApplyCompactionRefusesToArchiveACallStillWaitingForItsResult() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "run it"),
                                          MessageDraft(role: .assistant, content: "calling", toolName: "bash", toolCallID: "pending")])
        let live = try store.messages(sessionID: "s")
        let summary = ConversationMessage(sessionID: "s", seq: 0, role: .user, content: "sum", isSummary: true)
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: [summary], watermark: live.last!.seq)) {
            XCTAssertEqual($0 as? ConversationStoreError, .brokenToolPairing(toolCallID: "pending"))
        }
        XCTAssertNoThrow(try store.applyCompaction(sessionID: "s", newActive: [summary, live[1]], watermark: live.last!.seq))
        XCTAssertNoThrow(try store.append(sessionID: "s", role: .tool, content: "done", toolName: "bash", toolCallID: "pending"))
    }

    func testAppendingAResultWhoseCallIsNotLiveThrows() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        XCTAssertThrowsError(try store.append(sessionID: "s", role: .tool, content: "who called?", toolCallID: "ghost")) {
            XCTAssertEqual($0 as? ConversationStoreError, .orphanToolResult(sessionID: "s", toolCallID: "ghost"))
        }
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "go"),
                                          MessageDraft(role: .assistant, content: "calling", toolCallID: "c1"),
                                          MessageDraft(role: .tool, content: "first result", toolCallID: "c1")])
        try store.compact(sessionID: "s", seqRange: 1...3, summary: "sum")
        XCTAssertThrowsError(try store.append(sessionID: "s", role: .tool, content: "late result", toolCallID: "c1")) {
            XCTAssertEqual($0 as? ConversationStoreError, .orphanToolResult(sessionID: "s", toolCallID: "c1"))
        }
        XCTAssertNoThrow(try store.append(sessionID: "s", role: .tool, content: "uncorrelated output"))
        XCTAssertEqual(try store.messages(sessionID: "s").map(\.content), ["sum", "uncorrelated output"])
    }

    func testApplyCompactionRejectsAWatermarkBelowTheCarriedRowsOrNegative() throws {
        let (store, newActive) = try applyFixture()
        let before = try store.messages(sessionID: "s", scope: .all)
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: newActive, watermark: 7)) {
            XCTAssertEqual($0 as? ConversationStoreError, .invalidWatermark(watermark: 7, highestCarried: 8))
        }
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: [newActive[0]], watermark: -1)) {
            XCTAssertEqual($0 as? ConversationStoreError, .invalidWatermark(watermark: -1, highestCarried: nil))
        }
        XCTAssertEqual(try store.messages(sessionID: "s", scope: .all), before)
    }

    func testAnExclusionPastTwoThousandCharactersIsKept() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        try store.append(sessionID: "t", role: .user, content: "q secret")
        let long = String(repeating: "q ", count: 1_500) + "NOT secret"
        XCTAssertGreaterThan(long.count, 2_048)
        XCTAssertTrue(FTSQuery.sanitize(long).hasSuffix("NOT secret"))
        XCTAssertEqual(try store.search(long), [])
    }

    func testTheHardBoundNeverCutsInsideAPhrase() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        let query = String(repeating: "a", count: FTSQuery.maxQueryChars - 6) + " \"hello world\""
        let bounded = FTSQuery.bounded(query)
        XCTAssertEqual(bounded.filter { $0 == "\"" }.count, 0, "no stray quote is left")
        XCTAssertTrue(bounded.hasSuffix(" hell"), "the cut phrase's words stay, unquoted")
        let opening = FTSQuery.bounded("\"" + String(repeating: "needle ", count: 20_000))
        XCTAssertTrue(opening.hasPrefix(" needle"), "an opening quote at the start keeps the words after it")
        XCTAssertEqual(FTSQuery.bounded("short \"phrase kept\""), "short \"phrase kept\"")
        XCTAssertNoThrow(try store.search(query))
    }

    // MARK: Round 4

    func testRangeCompactionRefusesToArchiveAPendingCallOrSplitACallFromItsResult() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "go"),
                                          MessageDraft(role: .assistant, content: "calling", toolCallID: "pending")])
        let before = try store.messages(sessionID: "s", scope: .all)
        XCTAssertThrowsError(try store.compact(sessionID: "s", seqRange: 1...2, summary: "sum")) {
            XCTAssertEqual($0 as? ConversationStoreError, .brokenToolPairing(toolCallID: "pending"))
        }
        try store.append(sessionID: "s", role: .tool, content: "answer", toolCallID: "pending")
        XCTAssertThrowsError(try store.compact(sessionID: "s", seqRange: 1...2, summary: "sum")) {
            XCTAssertEqual($0 as? ConversationStoreError, .brokenToolPairing(toolCallID: "pending"), "call archived, result live")
        }
        XCTAssertEqual(try store.messages(sessionID: "s", scope: .all), before + [try store.messages(sessionID: "s").last!])
        XCTAssertNoThrow(try store.compact(sessionID: "s", seqRange: 1...3, summary: "sum"))
        XCTAssertEqual(try store.messages(sessionID: "s").map(\.content), ["sum"])
    }

    func testRangeCompactionKeepsTheMessagesBeforeTheRangeInPlace() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        for i in 1...5 { try store.append(sessionID: "s", role: .user, content: "m\(i)") }
        let result = try store.compact(sessionID: "s", seqRange: 3...4, summary: "sum")
        XCTAssertEqual(result.resequenced, [5: 7])
        XCTAssertEqual(try store.messages(sessionID: "s").map(\.seq), [1, 2, 6, 7])
        XCTAssertEqual(try store.messages(sessionID: "s", scope: .display).map(\.content), ["m1", "m2", "m3", "m4", "sum", "m5"])
        XCTAssertEqual(try store.search("m1").map(\.seq), [1])
    }

    func testAnOverlongSubstringTermIsCutToFitSQLitesPatternLimit() throws {
        let store = try makeStore()
        try store.createSession(title: "t", id: "t")
        let long = String(repeating: "x", count: 30_000)
        try store.append(sessionID: "t", role: .tool, content: long, toolName: "cat")
        XCTAssertEqual(try store.search(long, options: SearchOptions(roles: [.tool])).map(\.seq), [1])
        XCTAssertLessThanOrEqual((FTSQuery.likeParams(String(repeating: "%", count: 40_000))[0] as? String)?.utf8.count ?? .max,
                                 FTSQuery.maxLikePatternBytes)
        XCTAssertNoThrow(try store.search(String(repeating: "é", count: 60_000), options: SearchOptions(roles: [.tool])))
    }

    // MARK: Round 5: interrupted tool calls

    private func interruptedFixture() throws -> ConversationStore {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "check the logs"),
                                          MessageDraft(role: .assistant, content: "calling", toolName: "bash", toolCallID: "x"),
                                          MessageDraft(role: .user, content: "never mind, do something else"),
                                          MessageDraft(role: .assistant, content: "sure")])
        return store
    }

    private func summaryRow(_ text: String) -> ConversationMessage {
        ConversationMessage(sessionID: "s", seq: 0, role: .user, content: text, isSummary: true)
    }

    func testAnInterruptedCallMayBeArchived() throws {
        let store = try interruptedFixture()
        let live = try store.messages(sessionID: "s")
        XCTAssertNoThrow(try store.applyCompaction(sessionID: "s", newActive: [summaryRow("sum")] + live.suffix(2), watermark: live.last!.seq))
        XCTAssertEqual(try store.messages(sessionID: "s").map(\.content), ["sum", "never mind, do something else", "sure"])
        XCTAssertNoThrow(try store.compact(sessionID: "s", seqRange: 5...5, summary: "sum again"))
    }

    func testALateResultForAnInterruptedArchivedCallIsRefused() throws {
        let store = try interruptedFixture()
        let live = try store.messages(sessionID: "s")
        try store.applyCompaction(sessionID: "s", newActive: [summaryRow("sum")] + live.suffix(2), watermark: live.last!.seq)
        XCTAssertThrowsError(try store.append(sessionID: "s", role: .tool, content: "late logs", toolName: "bash", toolCallID: "x")) {
            XCTAssertEqual($0 as? ConversationStoreError, .orphanToolResult(sessionID: "s", toolCallID: "x"))
        }
    }

    func testACallFollowedOnlyByOtherToolTrafficIsStillInFlight() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        try store.append(sessionID: "s", [MessageDraft(role: .user, content: "two jobs"),
                                          MessageDraft(role: .assistant, content: "call a", toolCallID: "a"),
                                          MessageDraft(role: .assistant, content: "call b", toolCallID: "b"),
                                          MessageDraft(role: .tool, content: "result b", toolCallID: "b")])
        let live = try store.messages(sessionID: "s")
        XCTAssertThrowsError(try store.applyCompaction(sessionID: "s", newActive: [summaryRow("sum")], watermark: live.last!.seq)) {
            XCTAssertEqual($0 as? ConversationStoreError, .brokenToolPairing(toolCallID: "a"))
        }
        XCTAssertThrowsError(try store.compact(sessionID: "s", seqRange: 1...4, summary: "sum"))
    }

    func testRepeatedCompactionsNeverGetStuckOnInterruptedCalls() throws {
        let store = try makeStore()
        try store.createSession(title: "s", id: "s")
        for round in 0..<6 {
            try store.append(sessionID: "s", [MessageDraft(role: .user, content: "ask \(round)"),
                                              MessageDraft(role: .assistant, content: "calling \(round)", toolCallID: "c\(round)"),
                                              MessageDraft(role: .user, content: "moved on \(round)")])
            let live = try store.messages(sessionID: "s")
            XCTAssertNoThrow(try store.applyCompaction(sessionID: "s", newActive: [summaryRow("digest \(round)"), live.last!],
                                                       watermark: live.last!.seq), "round \(round)")
        }
        XCTAssertEqual(try store.messages(sessionID: "s").map(\.content), ["digest 5", "moved on 5"])
    }
}
