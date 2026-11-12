import XCTest
@testable import SessionCore

enum PartialStream {
    static func delta(_ text: String, parent: String? = nil) -> String {
        var event: [String: Any] = ["type": "stream_event",
                                    "event": ["type": "content_block_delta", "index": 0,
                                              "delta": ["type": "text_delta", "text": text]]]
        if let parent { event["parent_tool_use_id"] = parent }
        return Stream.json(event)
    }

    static let blockStop = Stream.json(["type": "stream_event", "event": ["type": "content_block_stop", "index": 0]])
    static let start = Stream.json(["type": "stream_event", "event": ["type": "content_block_start", "index": 0,
                                                                     "content_block": ["type": "text", "text": ""]]])
}

final class SpokenStreamTests: XCTestCase {
    /// Deltas as Claude Code streams them, recorded from `claude -p --include-partial-messages`.
    func testTheFirstSentenceComesOutBeforeTheReplyIsDone() {
        var s = SpokenStream()
        XCTAssertEqual(s.take(line: PartialStream.start), [])
        XCTAssertEqual(s.take(line: PartialStream.delta("Cats are small")), [])
        XCTAssertEqual(s.take(line: PartialStream.delta(", carnivorous mammals for thousands of years")), [])
        XCTAssertEqual(s.take(line: PartialStream.delta(". They are")), ["Cats are small, carnivorous mammals for thousands of years."])
        XCTAssertEqual(s.take(line: PartialStream.delta(" known for agility.")), [], "a mark at the very end waits")
        XCTAssertEqual(s.take(line: PartialStream.blockStop), ["They are known for agility."])
        XCTAssertEqual(s.finish(reply: "ignored once text streamed"), [])
        XCTAssertEqual(s.sentences, 2)
    }

    func testDecimalsAbbreviationsAndShortSentencesDontSplit() {
        var s = SpokenStream()
        _ = s.take(line: PartialStream.delta("Speed is 1.5 now, e.g. for Kokoro. Yes. It merged into main today. "))
        XCTAssertEqual(s.finish(reply: ""), [])
        var t = SpokenStream()
        XCTAssertEqual(t.take(line: PartialStream.delta("Speed is 1.5 now, e.g. for Kokoro. Yes. It merged into main today. More")),
                       ["Speed is 1.5 now, e.g. for Kokoro.", "Yes. It merged into main today."])
    }

    func testLineBreaksEndSentencesSoListItemsAreReadOneByOne() {
        var s = SpokenStream()
        XCTAssertEqual(s.take(line: PartialStream.delta("Two sessions need you:\n- Version bump waits on a token\n- Trim")),
                       ["Two sessions need you:", "- Version bump waits on a token"])
        XCTAssertEqual(s.finish(reply: ""), ["- Trim"])
    }

    func testASubagentsTextIsNeverRead() {
        var s = SpokenStream()
        XCTAssertEqual(s.take(line: PartialStream.delta("Searching the transcripts now. Then more. ", parent: "toolu_1")), [])
        XCTAssertEqual(s.finish(reply: "The trim session is done."), ["The trim session is done."])
    }

    func testWithoutStreamedTextTheWholeReplyIsReadInSentences() {
        var s = SpokenStream()
        XCTAssertEqual(s.take(line: Stream.text("ignored, not a stream event")), [])
        XCTAssertEqual(s.finish(reply: "The version bump needs your token. The trim fix merged."),
                       ["The version bump needs your token.", "The trim fix merged."])
    }
}

final class StreamedReplyEngineTests: XCTestCase {
    var folder: URL!
    var store: ConversationStore!

    override func setUpWithError() throws {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("streamed-\(UUID().uuidString)")
        store = try ConversationStore(url: folder.appendingPathComponent("conversations.db"))
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: folder) }

    func engine(_ claude: FakeClaude, store: ConversationStore) -> ChatEngine {
        ChatEngine(store: store, memoryFolder: folder.appendingPathComponent("memory"), tool: "/A/AssistantBar", model: "haiku",
                   effort: "high", windowFor: { _ in 200_000 }, runner: claude.runner, summarizer: { _ in "## Goal\nx" })
    }

    static let reply = "The version bump needs your token. The trim fix merged this morning."

    /// The streamed run stores the same rows and returns the same reply as a run without partial messages, and its
    /// sentences reach `speak` as they're written.
    func testStreamingSpeechLeavesTheStoredTurnUnchanged() throws {
        let plain = FakeClaude()
        plain.script = { _, _ in [Stream.text(Self.reply, prompt: 10), Stream.done(Self.reply)] }
        let streamed = FakeClaude()
        streamed.script = { _, invocation in
            XCTAssertTrue(invocation.partialMessages)
            return [PartialStream.start, PartialStream.delta("The version bump needs your token."),
                    PartialStream.delta(" The trim fix merged"), Stream.text(Self.reply, prompt: 10),
                    PartialStream.delta(" this morning."), PartialStream.blockStop, Stream.done(Self.reply)]
        }
        let otherStore = try ConversationStore(url: folder.appendingPathComponent("other.db"))
        let a = engine(plain, store: store).ask("What needs me?", fleet: ScanResult(), spoken: true) { _ in }
        var spoken: [String] = []
        var runFinished = false
        var spokenBeforeTheRunEnded = 0
        let streamedEngine = ChatEngine(store: otherStore, memoryFolder: folder.appendingPathComponent("memory"),
                                        tool: "/A/AssistantBar", model: "haiku", effort: "high", windowFor: { _ in 200_000 },
                                        runner: { invocation, onLine in
                                            defer { runFinished = true }
                                            return streamed.runner(invocation, onLine)
                                        }, summarizer: { _ in "## Goal\nx" })
        let b = streamedEngine.ask("What needs me?", fleet: ScanResult(), spoken: true, speak: {
            spoken.append($0)
            if !runFinished { spokenBeforeTheRunEnded += 1 }
        }) { _ in }
        XCTAssertEqual(spokenBeforeTheRunEnded, 2, "sentences are spoken while Claude is still writing")
        XCTAssertEqual(a.reply, b.reply)
        XCTAssertEqual(b.spokenSentences, 2)
        XCTAssertEqual(spoken, ["The version bump needs your token.", "The trim fix merged this morning."])
        let rows = { (s: ConversationStore) in
            try s.messages(sessionID: try XCTUnwrap(s.sessions().first?.session.id)).map { "\($0.role)|\($0.content)" }
        }
        XCTAssertEqual(try rows(store), try rows(otherStore))
        XCTAssertFalse(plain.invocations[0].partialMessages)
        XCTAssertTrue(Asker.arguments(streamed.invocations[0], tool: "/A/AssistantBar").contains("--include-partial-messages"))
        XCTAssertFalse(Asker.arguments(plain.invocations[0], tool: "/A/AssistantBar").contains("--include-partial-messages"))
    }

    func testAFailedRunSpeaksNothingSoTheErrorIsReadInstead() {
        let claude = FakeClaude()
        claude.script = { _, _ in [Stream.done("Overloaded", failed: true)] }
        var spoken: [String] = []
        let outcome = engine(claude, store: store).ask("q", fleet: ScanResult(), spoken: true, speak: { spoken.append($0) }) { _ in }
        XCTAssertTrue(outcome.failed)
        XCTAssertEqual(spoken, [])
        XCTAssertEqual(outcome.spokenSentences, 0)
    }
}

final class StreamedReplyTurnsTests: XCTestCase {
    func testAStreamedReplyStartsThenQueuesAndWaitsForListeningInOrder() {
        var turns = VoiceTurns()
        turns.newReply()
        XCTAssertEqual(turns.part("One.", listening: false), .start("One."))
        XCTAssertEqual(turns.part("Two.", listening: false), .add("Two."))
        XCTAssertEqual(turns.part("Three.", listening: true), .deferred(note: true))
        XCTAssertEqual(turns.part("Four.", listening: false), .deferred(note: false), "the rest stays in order")
        XCTAssertEqual(turns.listeningEnded(), "Three. Four.")
        turns.newReply()
        XCTAssertEqual(turns.part("Next.", listening: false), .start("Next."))
    }

    /// Muting drops the sentences that were waiting for listening to end, not only the ones still to come.
    func testMutingDropsTheSentencesWaitingForListeningToEnd() {
        var turns = VoiceTurns()
        turns.newReply()
        XCTAssertEqual(turns.part("One.", listening: true), .deferred(note: true))
        turns.mute()
        XCTAssertEqual(turns.part("Two.", listening: false), .dropped)
        XCTAssertNil(turns.listeningEnded())
    }
}

final class SpokenStreamCodeTests: XCTestCase {
    func testAFencedCodeBlockIsNeverRead() {
        var s = SpokenStream()
        var out = s.take(line: PartialStream.delta("Run this in the trim session:\n```bash\ngit push origin main\n"))
        out += s.take(line: PartialStream.delta("echo done. Really done.\n```\nThen tell me when it pushed.\n"))
        out += s.finish(reply: "")
        XCTAssertEqual(out, ["Run this in the trim session:", "Then tell me when it pushed."])
    }

    func testAnUnclosedBlockAtTheEndIsDropped() {
        var s = SpokenStream()
        _ = s.take(line: PartialStream.delta("Here's the command, it's long enough.\n```\nrm -rf build"))
        XCTAssertEqual(s.finish(reply: ""), [])
    }
}
