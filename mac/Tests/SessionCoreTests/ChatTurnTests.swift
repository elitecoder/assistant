import Foundation
import XCTest
@testable import SessionCore

/// stream-json lines as `claude -p --output-format stream-json --verbose` prints them.
enum Stream {
    static func json(_ object: [String: Any]) -> String {
        String(decoding: try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]), as: UTF8.self)
    }

    /// One model call's usage, split the way the provider reports it.
    static func usage(_ prompt: Int) -> [String: Any] {
        ["input_tokens": 2, "cache_read_input_tokens": prompt - 3, "cache_creation_input_tokens": 1]
    }

    static func assistant(_ blocks: [[String: Any]], prompt: Int? = nil, parent: String? = nil) -> String {
        var message: [String: Any] = ["content": blocks]
        if let prompt { message["usage"] = usage(prompt) }
        var event: [String: Any] = ["type": "assistant", "message": message]
        if let parent { event["parent_tool_use_id"] = parent }
        return json(event)
    }

    static func text(_ text: String, prompt: Int? = nil) -> String { assistant([["type": "text", "text": text]], prompt: prompt) }

    static func call(_ id: String, _ command: String, prompt: Int? = nil) -> String {
        assistant([["type": "tool_use", "id": id, "name": "Bash", "input": ["command": command]]], prompt: prompt)
    }

    static func result(_ id: String, _ output: Any) -> String {
        json(["type": "user", "message": ["content": [["type": "tool_result", "tool_use_id": id, "content": output]]]])
    }

    /// The result event: `usage` sums every call of the turn and `iterations` lists the last one.
    static func done(_ reply: String, lastCall: Int = 90_000, failed: Bool = false) -> String {
        json(["type": "result", "result": reply, "is_error": failed, "total_cost_usd": 0.01,
              "usage": ["input_tokens": 9, "cache_read_input_tokens": 190_000, "cache_creation_input_tokens": 9_000,
                        "iterations": [usage(lastCall)]]])
    }
}

func workspace(_ n: Int, _ title: String, said: String, idle: Double = 60) -> Item {
    var s = SessionSummary()
    s.goal = "Goal of \(title)"
    s.lastSaid = said
    let ws = Workspace(id: "W\(n)", ref: "workspace:\(n)", title: title, directory: "/r", submittedAt: nil, submittedMessage: nil)
    return Item(workspace: ws, transcript: "/t/\(n).jsonl", summary: s, attention: .needsYou, idleMinutes: idle, pullRequests: [])
}

func fleet(_ items: [Item]) -> ScanResult {
    var r = ScanResult()
    r.items = items
    return r
}

final class TurnAssemblyTests: XCTestCase {
    func row(_ seq: Int, _ role: MessageRole, _ content: String, call: String? = nil, summary: Bool = false) -> ConversationMessage {
        ConversationMessage(sessionID: "s", seq: seq, role: role, content: content, toolName: call == nil ? nil : "Bash",
                            toolCallID: call, isSummary: summary)
    }

    func testBlocksOfAnEarlierTurnAreAByteIdenticalPrefixOfTheNext() {
        let turn1 = [row(1, .system, "MEMORY"), row(2, .user, TurnContext.marker + "\n\nFLEET"), row(3, .user, "What needs me?")]
        let stored1 = turn1 + [row(4, .assistant, "ls", call: "c1"), row(5, .tool, "out", call: "c1"), row(6, .assistant, "Two do.")]
        let turn2 = stored1 + [row(7, .user, "And the CI fix?")]
        let first = TurnPrompt.blocks(turn1)
        let second = TurnPrompt.blocks(turn2)
        XCTAssertEqual(first, [TurnContext.marker + "\n\n" + Untrusted.wrap("FLEET"), "[Operator]\nWhat needs me?"])
        XCTAssertEqual(Array(second.prefix(first.count)), first)
        XCTAssertEqual(second.last, "[You ran Bash, call c1]\nls\n\n[Result of c1]\n" + Untrusted.wrap("out")
                       + "\n\n[You]\nTwo do.\n\n[Operator]\nAnd the CI fix?")
        let input1 = String(decoding: TurnPrompt.input(first), as: UTF8.self)
        let input2 = String(decoding: TurnPrompt.input(second), as: UTF8.self)
        let lastText = try! JSONSerialization.data(withJSONObject: ["text": first.last!], options: [.sortedKeys, .withoutEscapingSlashes])
        let cut = input1.range(of: String(decoding: lastText, as: UTF8.self).dropFirst().dropLast())!.upperBound
        XCTAssertTrue(input2.hasPrefix(String(input1[..<cut])))
    }

    func testASummaryQuotesTheOperatorButNotTheAppsContextRows() {
        let context = row(2, .user, TurnContext.row(fleet: "# Workspaces (checked x)\n## Alpha (workspace:1)", focus: nil)!)
        let section = SummaryPrompt.verbatimUserSection([context, row(3, .user, "Which need me?"), row(4, .assistant, "Alpha.")])
        XCTAssertTrue(section.contains("> Which need me?"))
        XCTAssertFalse(section.contains("Alpha (workspace:1)"))
    }

    func testSummaryRowRendersVerbatimBetweenTheHeadAndTheTail() {
        let summary = CompressionText.summaryPrefix + " Earlier: checked CI.\n\n" + CompressionText.summaryEndMarker
        let rows = [row(1, .system, ""), row(2, .user, "First ask"), row(3, .assistant, "First reply"),
                    row(4, .user, summary, summary: true), row(5, .assistant, "Later reply"), row(6, .user, "Now?")]
        let blocks = TurnPrompt.blocks(rows)
        XCTAssertEqual(blocks, ["[Operator]\nFirst ask", "[You]\nFirst reply\n\n" + Untrusted.wrap(summary),
                                "[You]\nLater reply\n\n[Operator]\nNow?"])
    }

    func testFleetDeltaSendsTheFullDigestThenOnlyRealChanges() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let a = workspace(1, "Alpha", said: "Merge?")
        let b = workspace(2, "Beta", said: "Done.")
        let full = FleetDelta.update(Digest.entries(fleet([a, b])), since: nil, now: now)
        XCTAssertEqual(full.text, Digest.fleet(fleet([a, b]), now: now))

        let clockOnly = FleetDelta.update(Digest.entries(fleet([workspace(1, "Alpha", said: "Merge?", idle: 600), b])),
                                          since: full.marks, now: now)
        XCTAssertNil(clockOnly.text)

        let changed = FleetDelta.update(Digest.entries(fleet([workspace(1, "Alpha", said: "Merged it."), workspace(3, "Gamma", said: "Hi")])),
                                        since: full.marks, now: now)
        let text = try! XCTUnwrap(changed.text)
        XCTAssertTrue(text.hasPrefix("# Workspace changes (checked "))
        XCTAssertTrue(text.contains("## Alpha (workspace:1)") && text.contains("Merged it."))
        XCTAssertTrue(text.contains("## Gamma (workspace:3)"))
        XCTAssertFalse(text.contains("## Beta"))
        XCTAssertTrue(text.contains("Closed: Beta (workspace:2)"))
    }

    func testAfterACompactionTheFleetIsMeasuredFromTheFullDigestWhileItIsLive() {
        let base = [FleetMark(key: "workspace:1", title: "Alpha", signature: "old")]
        let state = FleetState.after(FleetState.after(nil, sent: base),
                                     sent: [FleetMark(key: "workspace:1", title: "Alpha", signature: "new")])
        XCTAssertEqual(state.full, base)
        let digest = row(2, .user, TurnContext.marker + "\n\n# Workspaces (checked 2026-11-11T10:00:00Z)\n...")
        XCTAssertEqual(FleetState.afterCompaction(state, active: [row(1, .system, ""), digest]), FleetState(sent: base, full: base))
        let delta = row(2, .user, TurnContext.marker + "\n\n# Workspace changes (checked 2026-11-11T10:00:00Z)")
        XCTAssertNil(FleetState.afterCompaction(state, active: [row(1, .system, ""), delta]))
    }

    func testStreamEventsPairEachCallWithItsResultAndKeepTheFinalReply() {
        let lines = [
            Stream.json(["type": "system", "subtype": "init"]),
            Stream.assistant([["type": "thinking", "thinking": "hmm"], ["type": "text", "text": "Checking."]], prompt: 4_321),
            Stream.assistant([["type": "tool_use", "id": "a", "name": "Bash", "input": ["command": "/A --sessions"]],
                              ["type": "tool_use", "id": "b", "name": "Read", "input": ["file_path": "/t/1.jsonl"]]], prompt: 4_321),
            Stream.assistant([["type": "text", "text": "subagent noise"]], parent: "a"),
            Stream.result("b", [["type": "text", "text": "line one"], ["type": "image"]]),
            Stream.result("a", "workspace:1 Alpha"),
            Stream.result("zzz", "orphan"),
            Stream.text("Alpha needs you.", prompt: 30_000),
            Stream.done("Alpha needs you.", lastCall: 30_000),
        ].joined(separator: "\n")
        let record = StreamTurn.record(Data(lines.utf8), stopped: false)
        XCTAssertEqual(record.rows.map(\.role), [.assistant, .assistant, .assistant, .tool, .tool, .assistant])
        XCTAssertEqual(record.rows.map(\.content), ["Checking.", "/A --sessions", #"{"file_path":"/t/1.jsonl"}"#,
                                                    "line one\n[image]", "workspace:1 Alpha", "Alpha needs you."])
        XCTAssertEqual(record.rows.map(\.toolCallID), [nil, "a", "b", "b", "a", nil])
        XCTAssertEqual(record.rows[3].toolName, "Read")
        XCTAssertEqual(record.reply, "Alpha needs you.")
        XCTAssertFalse(record.failed)
        XCTAssertEqual(record.promptTokens, 4_321, "the first call priced the assembled transcript; later calls add this turn's tool output")
        XCTAssertEqual(record.cost, 0.01)
    }

    func testFinalReplyIsAddedWhenNoTextEventCarriedIt() {
        let lines = [Stream.call("a", "/A --sessions"), Stream.result("a", "x"), Stream.done("Done.", lastCall: 77)].joined(separator: "\n")
        let record = StreamTurn.record(Data(lines.utf8), stopped: false)
        XCTAssertEqual(record.promptTokens, 77, "without per-call usage, the result event's call is the fallback")
        XCTAssertEqual(record.rows.last?.content, "Done.")
        XCTAssertEqual(record.rows.last?.role, .assistant)
        XCTAssertNil(record.rows.last?.toolCallID)
    }

    /// A run ended in the middle of a message keeps the text streamed so far, and `said` holds Claude's words.
    func testAStoppedRunKeepsTheTextItHadStreamed() {
        func delta(_ text: String) -> String {
            Stream.json(["type": "stream_event", "event": ["type": "content_block_delta", "index": 0,
                                                           "delta": ["type": "text_delta", "text": text]]])
        }
        let lines = [delta("First "), delta("part."),
                     Stream.json(["type": "assistant", "message": ["content": [["type": "text", "text": "First part."]]]]),
                     delta("The version bump "), delta("session needs your token.")].joined(separator: "\n")
        let stopped = StreamTurn.record(Data(lines.utf8), stopped: true)
        XCTAssertEqual(stopped.said, "First part.\n\nThe version bump session needs your token.")
        XCTAssertEqual(stopped.rows.last?.content, "The version bump session needs your token.")
        let finished = StreamTurn.record(Data((lines + "\n" + Stream.done("Done.", lastCall: 1)).utf8), stopped: false)
        XCTAssertFalse(finished.rows.contains { $0.content.hasPrefix("The version bump") }, "only a stopped run keeps a draft")
    }

    func testAStoppedRunClosesItsOpenCallAndKeepsTheTurn() {
        let lines = [Stream.call("a", "/A --wait workspace:1")].joined(separator: "\n")
        let record = StreamTurn.record(Data(lines.utf8), stopped: true)
        XCTAssertTrue(record.failed)
        XCTAssertEqual(record.rows.map(\.toolCallID), ["a", "a"])
        XCTAssertEqual(record.rows.last?.content, StreamTurn.interruptedOutput)
        let send = StreamTurn.record(Data(Stream.call("b", "/A --send workspace:1 \"go\"").utf8), stopped: true)
        XCTAssertEqual(send.rows.last?.content, StreamTurn.deliveryUnknown, "an open send may have gone in")
        let stored = record.rowsToStore(timestamp: Date())
        XCTAssertEqual(stored.map(\.role), [.assistant, .tool, .assistant])
        XCTAssertEqual(stored.last?.content, "[No reply: Stopped.]")
    }

    func testAFailedRunThatRanNothingStoresOnlyANoReplyNote() {
        let record = StreamTurn.record(Data("command not found".utf8), stopped: false)
        XCTAssertTrue(record.failed)
        XCTAssertEqual(record.reply, Asker.noAnswer)
        XCTAssertEqual(record.rowsToStore(timestamp: Date()).map(\.content), ["[No reply: \(Asker.noAnswer)]"])
        XCTAssertFalse(record.promptTooLong)
    }

    func testOnlyAFirstCallRefusedAsTooLongAsksForARetry() {
        let refused = StreamTurn.record(Data(Stream.done("Prompt is too long", failed: true).utf8), stopped: false)
        XCTAssertTrue(refused.promptTooLong)
        let ranTool = [Stream.call("a", "/A --sessions"), Stream.result("a", "x"), Stream.done("Prompt is too long", failed: true)]
        XCTAssertFalse(StreamTurn.record(Data(ranTool.joined(separator: "\n").utf8), stopped: false).promptTooLong)
        XCTAssertFalse(StreamTurn.record(Data(Stream.done("Overloaded", failed: true).utf8), stopped: false).promptTooLong)
    }

    func testASummaryFoldedIntoAKeptRowKeepsThatRowsSpeakerLabel() {
        let summary = CompressionText.summaryPrefix + " Earlier: CI.\n\n" + CompressionText.summaryEndMarker
        let ended = row(5, .user, summary + "\n\nQuestion 9", summary: true)
        XCTAssertEqual(TurnPrompt.render(ended), Untrusted.wrap(summary) + "\n\n[Operator]\nQuestion 9")
        let merged = row(6, .assistant, CompressionText.mergedPriorContextHeader + "\nReply 8\n\n" + CompressionText.mergedSummaryDelimiter
                         + "\n\n" + summary, summary: true)
        let rest = (CompressionText.mergedPriorContextHeader + "\n\n\n" + CompressionText.mergedSummaryDelimiter + "\n\n" + summary)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        XCTAssertEqual(TurnPrompt.render(merged), Untrusted.wrap(rest) + "\n\n[You]\nReply 8")
        XCTAssertEqual(TurnPrompt.render(row(7, .user, summary, summary: true)), Untrusted.wrap(summary))
    }

    func testAContextRowIsNeverTakenForTheOperatorsRequest() {
        let rows = [row(2, .user, "What needs me?"), row(3, .assistant, "Alpha."),
                    row(4, .user, TurnContext.marker + "\n\n# Workspace changes (checked x)\n## Alpha")]
        XCTAssertFalse(rows[2].isRealUserTurn)
        XCTAssertTrue(rows[0].isRealUserTurn)
        XCTAssertEqual(SummaryPrompt.latestUserTaskSnapshot(rows),
                       "User asked (deterministic, from compacted turns): 'What needs me?'\nHistorical only; newer protected-tail messages after this summary win.")
    }

    func testThePanelHidesFailedQuestionsAndShowsAKeptQuestionOnce() {
        let summary = CompressionText.summaryPrefix + " x\n\n" + CompressionText.summaryEndMarker
        let asked = Date(timeIntervalSince1970: 1_800_000_100)
        var original = row(2, .user, "Question 9")
        original.timestamp = asked
        var carrier = row(9, .user, summary + "\n\nQuestion 9", summary: true)
        carrier.timestamp = asked
        let rows = [row(1, .user, "Failed one"), row(1, .assistant, "[No reply: API Error]"), original, row(3, .assistant, "Nine."),
                    row(4, .user, "Crashed mid-run"), carrier, row(10, .assistant, "Later.")]
        XCTAssertEqual(ChatHistory.turns(rows, tool: "/A").map(\.text),
                       ["Question 9", "Nine.", "Crashed mid-run", ChatHistory.compactedNote, "Later."])
    }

    func testLongToolOutputIsCut() {
        let text = StreamTurn.resultText(String(repeating: "x", count: StreamTurn.maxToolOutputChars + 5))
        XCTAssertTrue(text.hasSuffix("[… 5 more characters cut]"))
    }

    func testHistoryShowsTheOperatorsChatWithoutAppRows() {
        let summary = CompressionText.summaryPrefix + " x\n\n" + CompressionText.summaryEndMarker
        let rows = [row(1, .system, "MEMORY"), row(2, .user, TurnContext.marker + "\n\nFLEET"), row(3, .user, "Tell 25 go"),
                    row(4, .assistant, "/A/AssistantBar --send workspace:25 \"go\"", call: "c"), row(5, .tool, "REPLY\nOn it.", call: "c"),
                    row(6, .assistant, "Sent."), row(7, .user, summary, summary: true)]
        let turns = ChatHistory.turns(rows, tool: "/A/AssistantBar")
        XCTAssertEqual(turns.map(\.text), ["Tell 25 go", "Sent to workspace:25: go", "Reply: On it.", "Sent.", ChatHistory.compactedNote])
        XCTAssertEqual(turns.map(\.role), [.you, .step, .step, .assistant, .step])
    }

    func testContextWindowComesFromTheOverrideThenTheTable() {
        let defaults = UserDefaults(suiteName: "chat-window-\(UUID().uuidString)")!
        XCTAssertEqual(ContextWindow.tokens(for: "haiku", environment: [:], settings: defaults), 1_000_000)
        XCTAssertEqual(ContextWindow.tokens(for: "sonnet", environment: [:], settings: defaults), 1_000_000)
        XCTAssertEqual(ContextWindow.tokens(for: "claude-opus-5-5", environment: [:], settings: defaults), 1_000_000)
        XCTAssertTrue(ContextWindow.remember(400_000, for: "haiku", settings: defaults))
        XCTAssertFalse(ContextWindow.remember(400_000, for: "haiku", settings: defaults))
        XCTAssertEqual(ContextWindow.tokens(for: "haiku", environment: [:], settings: defaults), 400_000,
                       "the provider's reported window wins over the table")
        defaults.set(30_000, forKey: "contextWindow")
        XCTAssertEqual(ContextWindow.tokens(for: "haiku", environment: [:], settings: defaults), 30_000)
        XCTAssertEqual(ContextWindow.tokens(for: "haiku", environment: ["ASSISTANT_CONTEXT_WINDOW": "12000"], settings: defaults), 12_000)
        XCTAssertEqual(ContextMeter(tokens: 23_456, window: 40_000).text, "Context 23,456 of 40,000 tokens (59%)")
    }

    func testSummarizerReplyReadsTheJSONResult() throws {
        XCTAssertEqual(try ClaudeSummarizer.reply(Data(#"{"result":" ## Goal\nx ","is_error":false}"#.utf8)), "## Goal\nx")
        XCTAssertThrowsError(try ClaudeSummarizer.reply(Data(#"{"result":"","is_error":false}"#.utf8))) {
            XCTAssertEqual($0 as? SummaryError, .emptyContent)
        }
        XCTAssertThrowsError(try ClaudeSummarizer.reply(Data(#"{"result":"Rate limit reached","is_error":true}"#.utf8))) {
            XCTAssertEqual(String(describing: $0), "Rate limit reached")
        }
    }
}

/// A `claude -p` stand-in: records each invocation and answers with scripted stream-json. It reports a prompt size
/// proportional to what it was sent, the way the provider does.
final class FakeClaude {
    var invocations: [ClaudeInvocation] = []
    var script: (Int, ClaudeInvocation) -> [String] = { _, _ in [Stream.text("OK", prompt: 1), Stream.done("OK")] }

    var runner: ClaudeRunner {
        { invocation, onLine in
            self.invocations.append(invocation)
            let lines = self.script(self.invocations.count, invocation)
            lines.forEach(onLine)
            return ClaudeRun(output: Data(lines.joined(separator: "\n").utf8))
        }
    }

    static func tokens(_ invocation: ClaudeInvocation) -> Int { invocation.input.count / 4 + 500 }

    static func blocks(_ invocation: ClaudeInvocation) -> [String] {
        let event = try! JSONSerialization.jsonObject(with: invocation.input) as! [String: Any]
        let content = (event["message"] as! [String: Any])["content"] as! [[String: Any]]
        return content.map { $0["text"] as! String }
    }
}

final class ChatEngineTests: XCTestCase {
    var folder: URL!
    var store: ConversationStore!

    override func setUpWithError() throws {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("chat-engine-\(UUID().uuidString)")
        store = try ConversationStore(url: folder.appendingPathComponent("conversations.db"))
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: folder)
    }

    func engine(_ claude: FakeClaude, store: ConversationStore? = nil, window: Int = 200_000, summarizer: @escaping Summarizer = { _ in "## Goal\nx" }) -> ChatEngine {
        ChatEngine(store: store ?? self.store, memoryFolder: folder.appendingPathComponent("memory"), tool: "/A/AssistantBar", model: "haiku",
                   effort: "high", windowFor: { _ in window }, runner: claude.runner, summarizer: summarizer)
    }

    var memoryEnv: [String: String] { ["ASSISTANT_MEMORY_DIR": folder.appendingPathComponent("memory").path] }

    func testTheMemorySnapshotIsFrozenForTheChatAndRefreshedForTheNext() {
        _ = ChatTools.memory(#"{"target":"user","operations":[{"action":"add","content":"Operator is Mukul"}]}"#, environment: memoryEnv)
        let claude = FakeClaude()
        let chat = engine(claude)
        _ = chat.ask("one", fleet: ScanResult()) { _ in }
        _ = ChatTools.memory(#"{"target":"user","operations":[{"action":"add","content":"Prefers short answers"}]}"#, environment: memoryEnv)
        _ = chat.ask("two", fleet: ScanResult()) { _ in }
        XCTAssertTrue(claude.invocations[0].systemPrompt.contains("Operator is Mukul"))
        XCTAssertEqual(claude.invocations[1].systemPrompt, claude.invocations[0].systemPrompt)
        chat.newChat()
        _ = chat.ask("three", fleet: ScanResult()) { _ in }
        XCTAssertTrue(claude.invocations[2].systemPrompt.contains("Prefers short answers"))
    }

    func testTurnsAreStoredAndAReopenedEngineContinuesTheSameChat() throws {
        let claude = FakeClaude()
        claude.script = { n, inv in
            [Stream.call("c\(n)", "/A/AssistantBar --sessions", prompt: FakeClaude.tokens(inv)), Stream.result("c\(n)", "workspace:1"),
             Stream.text("Answer \(n)"), Stream.done("Answer \(n)")]
        }
        let first = engine(claude)
        let outcome = first.ask("What needs me?", fleet: fleet([workspace(1, "Alpha", said: "Merge?")])) { _ in }
        XCTAssertEqual(outcome.reply, "Answer 1")
        let sid = try XCTUnwrap(first.sessionID)
        let rows = try store.messages(sessionID: sid)
        XCTAssertEqual(rows.map(\.role), [.system, .user, .user, .assistant, .tool, .assistant])
        XCTAssertEqual(rows[4].toolCallID, "c1")
        XCTAssertEqual(try store.session(id: sid)?.title, "What needs me?")
        let priced = FakeClaude.tokens(claude.invocations[0])
        XCTAssertEqual(try store.session(id: sid)?.lastPromptTokens, priced)
        XCTAssertEqual(try store.session(id: sid)?.lastPromptTokensAtSeq, rows[2].seq, "anchored at the question the first call priced")
        XCTAssertEqual(outcome.meter?.tokens, priced + TokenEstimate.messages(Array(rows[3...])))
        XCTAssertEqual(claude.invocations[0].environment["ASSISTANT_SESSION_ID"], sid)

        let reopened = engine(claude)
        try reopened.openLatest()
        XCTAssertEqual(reopened.sessionID, sid)
        XCTAssertEqual(reopened.meter, outcome.meter)
        XCTAssertEqual(reopened.displayTurns().map(\.text), ["What needs me?", "Answer 1"])
        _ = reopened.ask("And now?", fleet: fleet([workspace(1, "Alpha", said: "Merge?")])) { _ in }
        XCTAssertEqual(Array(FakeClaude.blocks(claude.invocations[1]).prefix(2)), FakeClaude.blocks(claude.invocations[0]))

        reopened.newChat()
        _ = reopened.ask("Fresh", fleet: ScanResult()) { _ in }
        XCTAssertEqual(try store.session(id: try XCTUnwrap(reopened.sessionID))?.parentID, sid)
    }

    /// Real store and compressor, fake summarizer and claude, a small window: the chat compacts, the prompt shrinks,
    /// archived text stays findable, tool pairs hold, a row written mid-compaction survives, and the fleet restarts.
    func testAMultiTurnChatCompactsAndKeepsEverythingFindable() throws {
        let claude = FakeClaude()
        claude.script = { n, inv in
            let output = "Transcript excerpt \(n): " + String(repeating: "lorem ipsum \(n) ", count: 500)
            return [Stream.call("c\(n)", "/A/AssistantBar --send workspace:1 \"status \(n)\"", prompt: FakeClaude.tokens(inv)), Stream.result("c\(n)", "REPLY\n" + output),
                    Stream.text("Turn \(n) answered."), Stream.done("Turn \(n) answered.")]
        }
        var summaryRequests = 0
        var lateWriter: (() -> Void)?
        let chat = engine(claude, window: 12_000, summarizer: { _ in
            summaryRequests += 1
            lateWriter?()
            return "## Goal\nKeep the fleet moving.\n\n## Completed Actions\n1. Checked workspaces [tool: Bash]"
        })
        lateWriter = { [store] in _ = try? store!.append(sessionID: chat.sessionID!, role: .assistant, content: "LATE-ROW written mid-summary") }
        let questions = ["What needs me?", "What about the PELICAN-7 rollout?", "And the CI fix?", "Anything on docs?",
                         "Who waits longest?", "Summarize Alpha.", "Check Beta.", "Final status?"]
        var notes: [String] = []
        var compactedAt: Int?
        for (i, question) in questions.enumerated() {
            let alpha = workspace(1, "Alpha", said: i >= 2 ? "Merged." : "Merge?")
            _ = chat.ask(question, fleet: fleet([alpha, workspace(2, "Beta", said: "Done.")])) { step in
                if case .note(let text) = step { notes.append(text) }
            }
            let active = try store.messages(sessionID: chat.sessionID!)
            for (index, row) in active.enumerated() where row.role == .tool {
                XCTAssertTrue(active[..<index].contains { $0.toolCallID == row.toolCallID && $0.role == .assistant }, "orphan \(row.seq)")
            }
            for call in active where call.role == .assistant && call.toolCallID != nil {
                XCTAssertTrue(active.contains { $0.role == .tool && $0.toolCallID == call.toolCallID }, "unanswered \(call.seq)")
            }
            if compactedAt == nil, summaryRequests > 0 { compactedAt = i }
        }
        let turn = try XCTUnwrap(compactedAt, "no compaction in \(questions.count) turns")
        XCTAssertTrue(notes.contains { $0.hasPrefix("Compressed ") && $0.hasSuffix(" messages into a summary.") }, "\(notes)")

        let before = claude.invocations[turn - 1].input.count
        let after = claude.invocations[turn].input.count
        XCTAssertLessThan(after, before, "the prompt after compaction should be smaller")

        let sent = FakeClaude.blocks(claude.invocations[turn]).joined(separator: "\n")
        XCTAssertTrue(sent.contains(String(Untrusted.literal(CompressionText.summaryPrefix).dropFirst().dropLast())), "the summary is sent, escaped")
        XCTAssertTrue(sent.contains("LATE-ROW written mid-summary"))
        XCTAssertFalse(sent.contains("Transcript excerpt 2:"), "turn 2's tool output is archived, not resent")
        XCTAssertEqual(sent.components(separatedBy: "# Workspaces (checked ").count, 2, "the first digest stays in the head, once")
        let newBlocks = FakeClaude.blocks(claude.invocations[turn]).suffix(2).joined()
        XCTAssertTrue(newBlocks.contains("# Workspace changes") && newBlocks.contains("Merged."),
                      "the summarized Alpha update is resent against the first digest")
        XCTAssertTrue(claude.invocations[turn].systemPrompt.contains(CompressionText.compressionNote))

        let found = SessionSearchTool.run(json: Data(#"{"query":"PELICAN"}"#.utf8), store: store, currentSessionID: chat.sessionID)
        guard case .discover(let payload) = found.payload else { return XCTFail(found.text) }
        XCTAssertEqual(payload.results.first?.sessionID, chat.sessionID)
        XCTAssertTrue(found.text.contains("PELICAN-7 rollout"))

        let display = chat.displayTurns().map(\.text)
        XCTAssertTrue(display.contains("What about the PELICAN-7 rollout?"))
        XCTAssertTrue(display.contains(ChatHistory.compactedNote))
    }

    func testTurnsBeforeACompactionExtendEachOtherAndOnlyChangedWorkspacesAreResent() {
        let claude = FakeClaude()
        let chat = engine(claude)
        let alpha = workspace(1, "Alpha", said: "Merge?")
        let beta = workspace(2, "Beta", said: "Done.")
        _ = chat.ask("one", fleet: fleet([alpha, beta])) { _ in }
        _ = chat.ask("two", fleet: fleet([workspace(1, "Alpha", said: "Merge?", idle: 900), beta])) { _ in }
        _ = chat.ask("three", fleet: fleet([workspace(1, "Alpha", said: "Merged."), beta])) { _ in }
        let blocks = claude.invocations.map(FakeClaude.blocks)
        for i in 1..<blocks.count {
            XCTAssertEqual(Array(blocks[i].prefix(blocks[i - 1].count)), blocks[i - 1], "turn \(i + 1) must extend turn \(i)")
            XCTAssertEqual(claude.invocations[i].systemPrompt, claude.invocations[0].systemPrompt)
        }
        XCTAssertTrue(blocks[0][0].contains("# Workspaces (checked ") && blocks[0][0].contains("## Beta"))
        XCTAssertFalse(blocks[1].last!.contains("# Workspace"))
        let third = blocks[2].suffix(2).joined()
        XCTAssertTrue(third.contains("# Workspace changes") && third.contains("Merged.") && !third.contains("## Beta"))
    }

    func testOnceACompactionArchivesTheFullDigestTheNextTurnSendsItAgain() throws {
        let claude = FakeClaude()
        let long = String(repeating: "word ", count: 400)
        claude.script = { n, _ in [Stream.text("Reply \(n) " + long, prompt: 10), Stream.done("Reply \(n) " + long)] }
        let chat = engine(claude, window: 12_000)
        let ask = { (q: String) in _ = chat.ask(q, fleet: fleet([workspace(1, "Alpha", said: "Merge?")])) { _ in } }
        for i in 1...8 { ask("question \(i)") }
        XCTAssertTrue(chat.compressNow(focus: nil).hasPrefix("Compressed "))
        let active = try store.messages(sessionID: chat.sessionID!)
        XCTAssertTrue(active.contains(where: FleetDelta.carriesFullDigest), "a first compaction keeps the head")
        for i in 9...14 { ask("question \(i)") }
        XCTAssertTrue(chat.compressNow(focus: nil).hasPrefix("Compressed "))
        XCTAssertFalse(try store.messages(sessionID: chat.sessionID!).contains(where: FleetDelta.carriesFullDigest))
        ask("question 15")
        let newBlocks = FakeClaude.blocks(claude.invocations.last!).suffix(2).joined()
        XCTAssertTrue(newBlocks.contains("# Workspaces (checked ") && newBlocks.contains("## Alpha"))
    }

    func testManualCompressReportsWhenThereIsNothingToDo() {
        let chat = engine(FakeClaude())
        XCTAssertEqual(chat.compressNow(focus: nil), "Nothing to compress yet.")
        _ = chat.ask("one", fleet: ScanResult()) { _ in }
        XCTAssertTrue(chat.compressNow(focus: "deploy").hasPrefix("Nothing to compress yet ("))
    }

    func testManualCompressWithFocusPassesTheTopicToTheSummarizer() throws {
        let claude = FakeClaude()
        claude.script = { n, inv in [Stream.text("Reply \(n) " + String(repeating: "word ", count: 400), prompt: 10),
                                    Stream.done("Reply \(n) " + String(repeating: "word ", count: 400))] }
        var focus: String?
        let chat = engine(claude, window: 12_000, summarizer: { request in
            focus = request.focusTopic
            return "## Goal\nx"
        })
        for i in 1...8 { _ = chat.ask("question \(i)", fleet: ScanResult()) { _ in } }
        let note = chat.compressNow(focus: "the deploy token")
        XCTAssertTrue(note.hasPrefix("Compressed "), note)
        XCTAssertEqual(focus, "the deploy token")
        XCTAssertNil(chat.meter)
        XCTAssertTrue(try store.messages(sessionID: chat.sessionID!).contains { $0.isSummary })
    }

    func testAFailedTurnKeepsItsContextAndQuestionWithANoReplyNote() throws {
        let claude = FakeClaude()
        let chat = engine(claude)
        _ = chat.ask("one", fleet: fleet([workspace(1, "Alpha", said: "Merge?")])) { _ in }
        let sid = try XCTUnwrap(chat.sessionID)
        claude.script = { _, _ in ["command not found"] }
        let failed = chat.ask("two", fleet: fleet([workspace(1, "Alpha", said: "Merged.")])) { _ in }
        XCTAssertTrue(failed.failed)
        XCTAssertEqual(Array(try store.messages(sessionID: sid).suffix(3).map(\.content).dropFirst()),
                       ["two", "[No reply: \(Asker.noAnswer)]"])
        claude.script = { _, _ in [Stream.text("OK", prompt: 1), Stream.done("OK")] }
        _ = chat.ask("three", fleet: fleet([workspace(1, "Alpha", said: "Merged.")])) { _ in }
        let blocks = FakeClaude.blocks(claude.invocations[2])
        XCTAssertEqual(Array(blocks.prefix(FakeClaude.blocks(claude.invocations[1]).count)), FakeClaude.blocks(claude.invocations[1]))
        XCTAssertFalse(blocks.last!.contains("Merged."), "the failed turn's update is already in the history")
        XCTAssertEqual(chat.displayTurns().map(\.text), ["one", "OK", "three", "OK"])
    }

    func testTheCompressionCheckCountsThisTurnsContextAndQuestion() throws {
        let claude = FakeClaude()
        var summarized = 0
        let long = String(repeating: "word ", count: 400)
        claude.script = { n, _ in [Stream.text("Reply \(n) " + long, prompt: 10), Stream.done("Reply \(n) " + long)] }
        let chat = engine(claude, window: 12_000, summarizer: { _ in summarized += 1; return "## Goal\nx" })
        for i in 1...8 { _ = chat.ask("q\(i)", fleet: ScanResult()) { _ in } }
        XCTAssertEqual(summarized, 0, "the stored rows alone stay under the trigger")
        let big = String(repeating: "Alpha said a lot. ", count: 3_000)
        _ = chat.ask("q9", fleet: ScanResult(), focus: (title: "Alpha", exchange: big)) { _ in }
        XCTAssertEqual(summarized, 1, "this turn's context row pushes the chat past the trigger")
    }

    func testACompactionReloadsMemoryIntoTheSystemPrompt() throws {
        let claude = FakeClaude()
        let long = String(repeating: "word ", count: 400)
        claude.script = { n, _ in [Stream.text("Reply \(n) " + long, prompt: 10), Stream.done("Reply \(n) " + long)] }
        let chat = engine(claude, window: 12_000)
        for i in 1...8 { _ = chat.ask("q\(i)", fleet: ScanResult()) { _ in } }
        _ = ChatTools.memory(#"{"target":"user","operations":[{"action":"add","content":"Saved mid-chat"}]}"#, environment: memoryEnv)
        _ = chat.ask("q9", fleet: ScanResult()) { _ in }
        XCTAssertFalse(claude.invocations.last!.systemPrompt.contains("Saved mid-chat"), "frozen until a compaction")
        XCTAssertTrue(chat.compressNow(focus: nil).hasPrefix("Compressed "))
        _ = chat.ask("q10", fleet: ScanResult()) { _ in }
        let system = claude.invocations.last!.systemPrompt
        XCTAssertTrue(system.contains("Saved mid-chat") && system.contains(CompressionText.compressionNote))
    }

    func testAPromptRefusedAsTooLongCompressesAndRunsOnceMore() throws {
        let claude = FakeClaude()
        let long = String(repeating: "word ", count: 400)
        var summarized = 0
        claude.script = { n, _ in
            if n == 9 || n == 11 || n == 12 { return [Stream.done("Prompt is too long", failed: true)] }
            return [Stream.text("Reply \(n) " + long, prompt: 10), Stream.done("Reply \(n) " + long)]
        }
        let chat = engine(claude, window: 12_000, summarizer: { _ in summarized += 1; return "## Goal\nx" })
        for i in 1...8 { _ = chat.ask("q\(i)", fleet: ScanResult()) { _ in } }
        let retried = chat.ask("q9", fleet: ScanResult()) { _ in }
        XCTAssertEqual(retried.reply, "Reply 10 " + long)
        XCTAssertEqual(summarized, 1)
        XCTAssertLessThan(claude.invocations[9].input.count, claude.invocations[8].input.count)
        let twice = chat.ask("q10", fleet: ScanResult()) { _ in }
        XCTAssertTrue(twice.failed, "only one retry per turn")
        XCTAssertEqual(claude.invocations.count, 12)
    }

    func testACompactionThatCantBeSavedKeepsTheChatAndPausesCompression() throws {
        let claude = FakeClaude()
        let long = String(repeating: "word ", count: 1_500)
        claude.script = { n, inv in [Stream.text("Reply \(n) " + long, prompt: FakeClaude.tokens(inv)), Stream.done("Reply \(n) " + long)] }
        var other: ChatEngine?
        var summarized = 0
        let chat = engine(claude, window: 12_000, summarizer: { _ in
            summarized += 1
            if summarized == 1, let other { _ = other.compressNow(focus: nil) }
            return "## Goal\nx"
        })
        for i in 1...3 { _ = chat.ask("q\(i)", fleet: ScanResult()) { _ in } }
        other = engine(FakeClaude(), store: try ConversationStore(url: store.url), window: 12_000)
        try other!.open(chat.sessionID!)
        var notes: [String] = []
        for i in 4...12 {
            let outcome = chat.ask("q\(i)", fleet: ScanResult()) { if case .note(let n) = $0 { notes.append(n) } }
            XCTAssertFalse(outcome.failed, outcome.reply)
        }
        XCTAssertTrue(notes.contains { $0.hasPrefix("Compression couldn't be saved") }, "\(notes)")
        XCTAssertTrue(notes.contains { $0.hasPrefix("Compression waits ") }, "automatic compression pauses: \(notes)")
        XCTAssertEqual(summarized, 1)
        XCTAssertEqual(chat.displayTurns().filter { $0.role == .you }.count, 12)
    }

    func testAPauseSetByOneProcessHoldsInTheNext() throws {
        let long = String(repeating: "word ", count: 1_500)
        let script: (Int, ClaudeInvocation) -> [String] = { n, inv in
            [Stream.text("Reply \(n) " + long, prompt: FakeClaude.tokens(inv)), Stream.done("Reply \(n) " + long)]
        }
        let first = FakeClaude()
        first.script = script
        var failures = 0
        let chat = engine(first, window: 12_000, summarizer: { _ in failures += 1; throw SummaryError.overloaded("529") })
        for i in 1...8 { _ = chat.ask("q\(i)", fleet: ScanResult()) { _ in } }
        XCTAssertEqual(failures, 1, "the first process tried once, then waited out its own cooldown")
        let second = FakeClaude()
        second.script = script
        var asked = 0
        var notes: [String] = []
        let next = engine(second, store: try ConversationStore(url: store.url), window: 12_000, summarizer: { _ in asked += 1; return "## Goal\nx" })
        try next.open(chat.sessionID!)
        for j in 1...12 {
            _ = next.ask("later \(j) " + long, fleet: ScanResult()) { if case .note(let n) = $0 { notes.append(n) } }
        }
        XCTAssertTrue(notes.contains { $0.hasPrefix("Compression waits ") }, "the next process crossed the trigger: \(notes)")
        XCTAssertEqual(asked, 0, "a new process ignored the failed summary's cooldown")
    }

    func testTheSpokenStyleIsSentForItsTurnButNeverStored() throws {
        let claude = FakeClaude()
        let chat = engine(claude)
        _ = chat.ask("spoken one", fleet: ScanResult(), spoken: true) { _ in }
        _ = chat.ask("typed two", fleet: ScanResult()) { _ in }
        XCTAssertEqual(FakeClaude.blocks(claude.invocations[0]).last, TurnContext.marker + "\n\n" + Spoken.style)
        XCTAssertFalse(FakeClaude.blocks(claude.invocations[1]).contains { $0.contains("read aloud") })
        XCTAssertFalse(try store.messages(sessionID: chat.sessionID!, scope: .all).contains { $0.content.contains("read aloud") })
    }
}

final class ChatToolsTests: XCTestCase {
    func testSearchFindsTheCurrentChatsArchivedTurnsThroughTheEnvironment() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("chat-tools-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: folder) }
        let url = folder.appendingPathComponent("c.db")
        let store = try ConversationStore(url: url)
        let other = try store.createSession(title: "Old chat")
        try store.append(sessionID: other.id, role: .user, content: "The walrus deploy failed")
        let text = ChatTools.search(#"{"query":"walrus"}"#, environment: ["ASSISTANT_DB": url.path, "ASSISTANT_SESSION_ID": "current"])
        XCTAssertTrue(text.contains("walrus deploy"), text)
        XCTAssertTrue(ChatTools.search("not json", environment: ["ASSISTANT_DB": url.path]).contains(#""success":false"#))
    }

    func testSearchNeverOffersAContextRowAsAMatch() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("chat-tools-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try ConversationStore(url: folder.appendingPathComponent("c.db"))
        let digest = try store.createSession(title: "Digest only")
        try store.append(sessionID: digest.id, role: .user, content: TurnContext.row(fleet: "## Narwhal rollout (workspace:7)", focus: nil)!)
        try store.append(sessionID: digest.id, role: .user, content: "What needs me?")
        let asked = try store.createSession(title: "Asked")
        try store.append(sessionID: asked.id, role: .user, content: "Did the narwhal rollout ship?")
        let found = SessionSearchTool.run(json: Data(#"{"query":"narwhal"}"#.utf8), store: store, currentSessionID: "current")
        guard case .discover(let payload) = found.payload else { return XCTFail(found.text) }
        XCTAssertEqual(payload.results.map(\.sessionID), [asked.id], found.text)
    }

    func testMemoryWritesToTheFolderFromTheEnvironment() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("chat-memory-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: folder) }
        let text = ChatTools.memory(#"{"target":"memory","operations":[{"action":"add","content":"cmux runs the fleet"}]}"#,
                                    environment: ["ASSISTANT_MEMORY_DIR": folder.path])
        XCTAssertTrue(text.contains(#""success": true"#) || text.contains(#""success":true"#), text)
        XCTAssertTrue(try String(contentsOf: folder.appendingPathComponent("MEMORY.md")).contains("cmux runs the fleet"))
    }
}

final class ReportedWindowTests: XCTestCase {
    func testTheResultEventsContextWindowIsRead() {
        let lines = [
            #"{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}],"usage":{"input_tokens":10}}}"#,
            #"{"type":"result","is_error":false,"result":"ok","modelUsage":{"claude-haiku-5-5":{"contextWindow":1000000}}}"#,
        ]
        let record = StreamTurn.record(Data(lines.joined(separator: "\n").utf8), stopped: false)
        XCTAssertEqual(record.contextWindow, 1_000_000)
    }
}
