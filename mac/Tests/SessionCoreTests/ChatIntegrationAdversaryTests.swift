import Foundation
import XCTest
@testable import SessionCore

/// Adversarial checks of the chat integration: hostile stream-json, compaction under pressure, prompt-cache
/// stability, two engines on one database, and crash windows. Each test states the behavior the chat should have.
final class ChatIntegrationAdversaryTests: XCTestCase {
    var folder: URL!
    var dbURL: URL!
    var store: ConversationStore!

    override func setUpWithError() throws {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("chat-adv-\(UUID().uuidString)")
        dbURL = folder.appendingPathComponent("conversations.db")
        store = try ConversationStore(url: dbURL)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: folder)
    }

    func engine(_ runner: @escaping ClaudeRunner, store: ConversationStore? = nil, window: Int = 200_000,
                summarizer: @escaping Summarizer = { _ in "## Goal\nx" }) -> ChatEngine {
        ChatEngine(store: store ?? self.store, memoryFolder: folder.appendingPathComponent("memory"), tool: "/A/AssistantBar",
                   model: "haiku", effort: "high", windowFor: { _ in window }, runner: runner, summarizer: summarizer)
    }

    func scripted(_ lines: [String]) -> ClaudeRunner {
        { _, onLine in
            lines.forEach(onLine)
            return ClaudeRun(output: Data(lines.joined(separator: "\n").utf8))
        }
    }

    func assertPairing(_ sid: String, file: StaticString = #filePath, line: UInt = #line) throws {
        let active = try store.messages(sessionID: sid)
        for (i, row) in active.enumerated() where row.role == .tool {
            XCTAssertTrue(active[..<i].contains { $0.role == .assistant && $0.toolCallID == row.toolCallID }, "orphan result \(row.seq)",
                          file: file, line: line)
        }
    }

    // MARK: Stream-json from hell

    /// Node's JSON.stringify leaves U+2028, U+2029 and U+0085 raw, so a real `claude -p` line can carry them.
    func testAReplyWithALineSeparatorIsKept() {
        let reply = "Alpha needs you.\u{2028}Beta is done."
        let lines = [Stream.text(reply, prompt: 10), Stream.done(reply)].joined(separator: "\n")
        let record = StreamTurn.record(Data(lines.utf8), stopped: false)
        XCTAssertFalse(record.failed, "a reply with U+2028 is reported as no answer")
        XCTAssertEqual(record.reply, reply)
    }

    func testToolOutputWithANextLineCharacterKeepsItsResult() {
        let lines = [Stream.call("a", "/A --sessions"), Stream.result("a", "workspace:1\u{85}Alpha"), Stream.done("ok")].joined(separator: "\n")
        let record = StreamTurn.record(Data(lines.utf8), stopped: false)
        XCTAssertEqual(record.rows.first { $0.role == .tool }?.content, "workspace:1\u{85}Alpha",
                       "the result line split on U+0085 and the call was recorded as interrupted")
    }

    func testATruncatedResultAfterAToolKeepsTheTurnAndTheNextTurnWorks() throws {
        var n = 0
        let chat = engine({ _, _ in
            n += 1
            let lines = n == 1 ? [Stream.call("a", "/A/AssistantBar --sessions", prompt: 50), Stream.result("a", "ws"),
                                  String(Stream.done("Done").prefix(30))]
                : [Stream.text("Second", prompt: 60), Stream.done("Second")]
            return ClaudeRun(output: Data(lines.joined(separator: "\n").utf8))
        })
        let first = chat.ask("one", fleet: ScanResult()) { _ in }
        XCTAssertTrue(first.failed)
        let second = chat.ask("two", fleet: ScanResult()) { _ in }
        XCTAssertEqual(second.reply, "Second")
        try assertPairing(chat.sessionID!)
        XCTAssertEqual(chat.displayTurns().filter { $0.role == .you }.map(\.text), ["one", "two"])
    }

    func testResultBeforeItsCallAndDuplicateIDsStillStoreAndCompact() throws {
        let long = String(repeating: "word ", count: 600)
        var n = 0
        let chat = engine({ _, _ in
            n += 1
            let lines = [Stream.result("d\(n)", "early"), Stream.call("d\(n)", "/A/AssistantBar --sessions", prompt: 100),
                         Stream.call("d\(n)", "/A/AssistantBar --sessions"), Stream.result("d\(n)", "x"),
                         Stream.assistant([["type": "text", "text": "sub"]], parent: "d\(n)"),
                         Stream.text("Reply \(n) " + long), Stream.done("Reply \(n) " + long)]
            return ClaudeRun(output: Data(lines.joined(separator: "\n").utf8))
        }, window: 12_000)
        for i in 1...10 {
            let outcome = chat.ask("q\(i)", fleet: ScanResult()) { _ in }
            XCTAssertFalse(outcome.reply.hasPrefix("The chat couldn't be saved"), outcome.reply)
        }
        try assertPairing(chat.sessionID!)
        let note = chat.compressNow(focus: nil)
        XCTAssertFalse(note.hasPrefix("Compression failed"), note)
        try assertPairing(chat.sessionID!)
    }

    func testAnErrorResultAndAnEmptyReplyLeaveASaneChat() throws {
        var n = 0
        let chat = engine({ _, _ in
            n += 1
            let lines: [String]
            switch n {
            case 1: lines = [Stream.done("API Error: overloaded", failed: true)]
            case 2: lines = [Stream.done("")]
            default: lines = [Stream.text("Fine", prompt: 5), Stream.done("Fine")]
            }
            return ClaudeRun(output: Data(lines.joined(separator: "\n").utf8))
        })
        XCTAssertTrue(chat.ask("one", fleet: ScanResult()) { _ in }.failed)
        let empty = chat.ask("two", fleet: ScanResult()) { _ in }
        XCTAssertFalse(empty.failed)
        XCTAssertEqual(chat.ask("three", fleet: ScanResult()) { _ in }.reply, "Fine")
        XCTAssertEqual(chat.displayTurns().map(\.text), ["two", "three", "Fine"])
    }

    func testHugeToolOutputAndHugeCommandsAreBounded() throws {
        let big = String(repeating: "y", count: 10_000_000)
        let lines = [Stream.call("a", "/A/AssistantBar --search '" + big + "'", prompt: 10), Stream.result("a", big),
                     Stream.text("Done", prompt: 10), Stream.done("Done")]
        let started = Date()
        let chat = engine(scripted(lines))
        _ = chat.ask("big", fleet: ScanResult()) { _ in }
        XCTAssertLessThan(Date().timeIntervalSince(started), 20)
        let rows = try store.messages(sessionID: chat.sessionID!)
        XCTAssertLessThan(rows.first { $0.role == .tool }!.content.count, 25_000)
        XCTAssertLessThan(rows.map(\.content.count).max()!, 100_000, "the tool call text is stored and resent uncapped")
    }

    func testNonUTF8BytesDoNotLoseTheTurn() throws {
        var data = Data((Stream.call("a", "/A/AssistantBar --sessions", prompt: 10) + "\n").utf8)
        data.append(contentsOf: [0xFF, 0xFE, 0x0A])
        data.append(Data((Stream.result("a", "ok") + "\n" + Stream.done("Done")).utf8))
        let chat = engine({ _, _ in ClaudeRun(output: data) })
        XCTAssertEqual(chat.ask("bytes", fleet: ScanResult()) { _ in }.reply, "Done")
        try assertPairing(chat.sessionID!)
    }

    // MARK: Compaction under pressure

    func testFailingRefusingAndJunkSummariesNeverStallOrLoseTurns() throws {
        var calls = 0
        let summarizer: Summarizer = { _ in
            calls += 1
            switch calls % 5 {
            case 0: throw SummaryError.timeout("no summary after 300 seconds")
            case 1: throw SummaryError.overloaded("529")
            case 2: return ""
            case 3: return "I can't help with that."
            default: return String(repeating: "junk ", count: 200_000)
            }
        }
        let long = String(repeating: "word ", count: 500)
        var n = 0
        let chat = engine({ inv, _ in
            n += 1
            let lines = [Stream.text("Reply \(n) " + long, prompt: FakeClaude.tokens(inv)), Stream.done("Reply \(n) " + long)]
            return ClaudeRun(output: Data(lines.joined(separator: "\n").utf8))
        }, window: 12_000, summarizer: summarizer)
        var largest = 0
        let started = Date()
        for i in 1...40 {
            let outcome = chat.ask("q\(i)", fleet: ScanResult()) { _ in }
            XCTAssertFalse(outcome.failed, "turn \(i): \(outcome.reply)")
            let rows = try store.resumeMessages(sessionID: chat.sessionID!)
            largest = max(largest, TokenEstimate.messages(rows))
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 30)
        XCTAssertEqual(chat.displayTurns().filter { $0.role == .you }.count, 40)
        XCTAssertLessThan(largest, 12_000 * 3, "the live context outgrew the window: \(largest) tokens")
    }

    func testCompactingEveryTurnFor200TurnsStaysBounded() throws {
        let long = String(repeating: "word ", count: 900)
        var n = 0
        var summaryPrompt = 0
        let chat = engine({ inv, _ in
            n += 1
            let lines = [Stream.call("c\(n)", "/A/AssistantBar --sessions", prompt: FakeClaude.tokens(inv)), Stream.result("c\(n)", long),
                         Stream.text("Reply \(n)"), Stream.done("Reply \(n)")]
            return ClaudeRun(output: Data(lines.joined(separator: "\n").utf8))
        }, window: 8_000, summarizer: { request in
            summaryPrompt = max(summaryPrompt, request.prompt.count)
            return "## Goal\nKeep going.\n\n## Completed Actions\n1. Turn [tool: Bash]"
        })
        let started = Date()
        var maxActive = 0
        for i in 1...200 {
            _ = chat.ask("question \(i)", fleet: ScanResult()) { _ in }
            maxActive = max(maxActive, try store.resumeMessages(sessionID: chat.sessionID!).count)
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 60)
        XCTAssertLessThan(maxActive, 80, "active rows grew to \(maxActive)")
        XCTAssertLessThan(summaryPrompt, 200_000, "summary prompt grew to \(summaryPrompt) chars")
        let you = chat.displayTurns().filter { $0.role == .you }.map(\.text)
        let counts = Dictionary(you.map { ($0, 1) }, uniquingKeysWith: +).filter { $0.value > 1 }
        XCTAssertEqual(you.count, 200, "visible questions: \(you.count), repeated: \(counts.sorted { $0.key < $1.key }.prefix(5))")
        try assertPairing(chat.sessionID!)
    }

    func testAnotherEnginesTurnDuringTheSummaryIsKept() throws {
        let long = String(repeating: "word ", count: 500)
        let other = try ConversationStore(url: dbURL)
        var sid: String?
        var injected = false
        var b: ChatEngine?
        let chat = engine({ inv, _ in
            let lines = [Stream.text("A " + long, prompt: FakeClaude.tokens(inv)), Stream.done("A " + long)]
            return ClaudeRun(output: Data(lines.joined(separator: "\n").utf8))
        }, window: 12_000, summarizer: { _ in
            if !injected, let sid {
                injected = true
                try? b?.use(sid)
                _ = b?.ask("from the CLI mid-summary", fleet: ScanResult()) { _ in }
                let fresh = self.engine(self.scripted([Stream.text("new", prompt: 1), Stream.done("new")]), store: other)
                _ = fresh.ask("brand new chat mid-summary", fleet: ScanResult()) { _ in }
            }
            return "## Goal\nx"
        })
        b = engine(scripted([Stream.call("cli", "/A/AssistantBar --sessions", prompt: 10), Stream.result("cli", "ws"),
                             Stream.text("CLI reply"), Stream.done("CLI reply")]), store: other)
        for i in 1...30 {
            _ = chat.ask("q\(i)", fleet: ScanResult()) { _ in }
            sid = chat.sessionID
            if injected { break }
        }
        XCTAssertTrue(injected, "no compaction ran")
        let active = try store.resumeMessages(sessionID: sid!)
        XCTAssertTrue(active.contains { $0.content == "from the CLI mid-summary" })
        XCTAssertTrue(active.contains { $0.content == "CLI reply" })
        try assertPairing(sid!)
        XCTAssertEqual(try store.sessions(limit: 10).count, 2)
    }

    // MARK: Caching

    func testEveryTurnExtendsTheLastPromptAcrossToggles() throws {
        let claude = FakeClaude()
        var n = 0
        claude.script = { _, inv in
            n += 1
            switch n {
            case 4: return [Stream.call("t4", "/A/AssistantBar --send workspace:1 \"go\"", prompt: 9), Stream.result("t4", "REPLY\nok"),
                            Stream.done("API Error", failed: true)]
            case 5: return [Stream.done("")]
            case 6: return ["garbage"]
            default: return [Stream.text("R\(n)", prompt: FakeClaude.tokens(inv)), Stream.done("R\(n)")]
            }
        }
        let chat = engine(claude.runner)
        let a = workspace(1, "Alpha", said: "Merge?")
        _ = chat.ask("one", fleet: fleet([a])) { _ in }
        _ = chat.ask("two", fleet: fleet([workspace(1, "Alpha", said: "Merged.")]), focus: (title: "Alpha", exchange: "OPERATOR: hi")) { _ in }
        _ = chat.ask("three  \r\n", fleet: fleet([a, workspace(2, "Beta", said: "x")]), spoken: true) { _ in }
        _ = ChatTools.memory(#"{"target":"user","operations":[{"action":"add","content":"Mid-chat fact"}]}"#,
                             environment: ["ASSISTANT_MEMORY_DIR": folder.appendingPathComponent("memory").path])
        _ = chat.ask("four", fleet: fleet([a])) { _ in }
        chat.setModel("sonnet", effort: nil)
        _ = chat.ask("five", fleet: fleet([a]), spoken: true) { _ in }
        _ = chat.ask("six", fleet: fleet([a])) { _ in }
        let reopened = engine(claude.runner)
        try reopened.open(chat.sessionID!)
        _ = reopened.ask("seven \u{2028} with separator", fleet: fleet([a])) { _ in }
        _ = reopened.ask("eight", fleet: fleet([])) { _ in }
        let blocks = claude.invocations.map(FakeClaude.blocks)
        // Turns 3 and 5 were spoken: the reply style rides in that turn's tail block only and is never stored. Turn 6
        // failed before doing anything; its question and no-reply note are stored, so it is a full prefix too.
        let spoken: Set<Int> = [2, 4]
        for i in 1..<blocks.count {
            let previousStored = blocks[i - 1].dropLast(spoken.contains(i - 1) ? 1 : 0)
            XCTAssertEqual(Array(blocks[i].prefix(previousStored.count)), Array(previousStored), "turn \(i + 1) broke the cached prefix")
            XCTAssertEqual(claude.invocations[i].systemPrompt, claude.invocations[0].systemPrompt, "turn \(i + 1) changed the system prompt")
        }
        for i in blocks.indices {
            let styled = blocks[i].filter { $0.contains("read aloud") }
            XCTAssertEqual(styled.count, spoken.contains(i) ? 1 : 0, "turn \(i + 1) carries the spoken style \(styled.count) times")
            if spoken.contains(i) { XCTAssertEqual(blocks[i].last, styled.first) }
        }
    }

    // MARK: Concurrency

    func testAnInterleavedTurnFromTheCLIKeepsTheAppsCachedPrefix() throws {
        let other = try ConversationStore(url: dbURL)
        let claude = FakeClaude()
        var cli: ChatEngine?
        var sid: String?
        claude.script = { n, inv in
            if n == 2, let sid, let cli {
                try? cli.use(sid)
                _ = cli.ask("CLI question", fleet: ScanResult()) { _ in }
            }
            return [Stream.text("App \(n)", prompt: FakeClaude.tokens(inv)), Stream.done("App \(n)")]
        }
        let app = engine(claude.runner)
        cli = engine(scripted([Stream.text("CLI answer", prompt: 5), Stream.done("CLI answer")]), store: other)
        _ = app.ask("one", fleet: ScanResult()) { _ in }
        sid = app.sessionID
        _ = app.ask("two", fleet: ScanResult()) { _ in }
        _ = app.ask("three", fleet: ScanResult()) { _ in }
        try assertPairing(sid!)
        let blocks = claude.invocations.map(FakeClaude.blocks)
        XCTAssertEqual(Array(blocks[2].prefix(blocks[1].count)), blocks[1], "the CLI's rows landed inside the app's cached prefix")
        let texts = try store.messages(sessionID: sid!).map(\.content)
        XCTAssertLessThan(texts.firstIndex(of: "two")!, texts.firstIndex(of: "App 2")!)
    }

    func testTwoEnginesOnOneDatabaseWritingTheSameSessionAtOnce() throws {
        let id = "cli-shared"
        let stores = [store!, try ConversationStore(url: dbURL)]
        let engines = stores.enumerated().map { index, s -> ChatEngine in
            engine({ _, _ in
                Thread.sleep(forTimeInterval: 0.002)
                let call = "e\(index)-\(UUID().uuidString)"
                let lines = [Stream.call(call, "/A/AssistantBar --sessions", prompt: 20), Stream.result(call, "x"), Stream.text("E\(index)"), Stream.done("E\(index)")]
                return ClaudeRun(output: Data(lines.joined(separator: "\n").utf8))
            }, store: s)
        }
        for e in engines { try e.use(id) }
        var failures: [String] = []
        let lock = NSLock()
        DispatchQueue.concurrentPerform(iterations: 2) { index in
            for i in 1...30 {
                let outcome = engines[index].ask("E\(index) q\(i)", fleet: ScanResult()) { _ in }
                if outcome.failed { lock.lock(); failures.append(outcome.reply); lock.unlock() }
            }
        }
        XCTAssertEqual(failures, [])
        let rows = try store.messages(sessionID: id, scope: .all)
        XCTAssertEqual(rows.filter { $0.role == .system }.count, 1, "both engines wrote a memory snapshot")
        XCTAssertEqual(rows.filter { $0.role == .user && !TurnContext.isContext($0) }.count, 60)
        XCTAssertEqual(rows.filter { TurnContext.isContext($0) }.count, 1, "both engines sent the full digest")
        XCTAssertEqual(Set(rows.map(\.seq)).count, rows.count)
    }

    func testReopeningDuringARunningTurnShowsTheQuestionInFlight() throws {
        var seenMidRun: [String] = []
        let watcher = engine(scripted([]), store: try ConversationStore(url: dbURL))
        var sid: String?
        var n = 0
        let chat = engine({ inv, _ in
            n += 1
            if n == 2, let sid {
                try? watcher.open(sid)
                seenMidRun = watcher.displayTurns().map(\.text)
            }
            let lines = [Stream.call("s\(n)", "/A/AssistantBar --send workspace:1 \"merge it\"", prompt: 10), Stream.result("s\(n)", "REPLY\nmerged"),
                         Stream.text("Done \(n)"), Stream.done("Done \(n)")]
            return ClaudeRun(output: Data(lines.joined(separator: "\n").utf8))
        })
        _ = chat.ask("one", fleet: ScanResult()) { _ in }
        sid = chat.sessionID
        _ = chat.ask("Tell Alpha to merge", fleet: ScanResult()) { _ in }
        XCTAssertTrue(seenMidRun.contains("Tell Alpha to merge"),
                      "a crash or second window mid-turn loses the question and the --send it already made: \(seenMidRun)")
    }

    // MARK: Persistence

    func testAChatLeftHalfWrittenReopensSanelyAndContinues() throws {
        let s = try store.createSession(title: "Half", id: "half")
        try store.append(sessionID: s.id, role: .system, content: "")
        try store.append(sessionID: s.id, role: .user, content: "question with no reply")
        try store.append(sessionID: s.id, [MessageDraft(role: .assistant, content: "/A/AssistantBar --sessions", toolName: "Bash", toolCallID: "dangling")])
        let chat = engine({ inv, _ in
            let lines = [Stream.text("Back " + String(repeating: "w ", count: 3_000), prompt: FakeClaude.tokens(inv)), Stream.done("Back")]
            return ClaudeRun(output: Data(lines.joined(separator: "\n").utf8))
        }, window: 12_000)
        try FileManager.default.createDirectory(at: chat.fleetStateURL("half").deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("{not json".utf8).write(to: chat.fleetStateURL("half"))
        try chat.open("half")
        XCTAssertEqual(chat.displayTurns().map(\.text), ["question with no reply"])
        for i in 1...8 {
            let outcome = chat.ask("q\(i)", fleet: fleet([workspace(1, "Alpha", said: "x")])) { _ in }
            XCTAssertFalse(outcome.failed, outcome.reply)
        }
        XCTAssertFalse(chat.compressNow(focus: nil).hasPrefix("Compression failed"))
        try assertPairing("half")
    }
}
