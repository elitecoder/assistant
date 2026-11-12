import Foundation
import XCTest
@testable import SessionCore

private final class ReviewChat {
    let sessionID = "review-1"
    private(set) var messages: [ConversationMessage] = []
    private var nextCall = 1

    @discardableResult func user(_ text: String) -> ReviewChat { append(.user, text) }
    @discardableResult func assistant(_ text: String) -> ReviewChat { append(.assistant, text) }
    @discardableResult func call(_ command: String, id: String? = nil) -> ReviewChat {
        let callID = id ?? "rc_\(nextCall)"
        nextCall += 1
        return append(.assistant, command, toolName: "shell", toolCallID: callID)
    }
    @discardableResult func result(_ output: String, id: String) -> ReviewChat { append(.tool, output, toolName: "shell", toolCallID: id) }
    @discardableResult func run(_ command: String, output: String) -> ReviewChat {
        let id = "rc_\(nextCall)"
        return call(command, id: id).result(output, id: id)
    }
    @discardableResult func append(_ role: MessageRole, _ content: String, toolName: String? = nil, toolCallID: String? = nil,
                                   isSummary: Bool = false) -> ReviewChat {
        messages.append(ConversationMessage(sessionID: sessionID, seq: messages.count + 1, role: role, content: content,
                                            toolName: toolName, toolCallID: toolCallID,
                                            timestamp: Date(timeIntervalSince1970: 1_800_000_000 + Double(messages.count)),
                                            isSummary: isSummary))
        return self
    }
}

private final class ReviewSummarizer {
    var requests: [SummaryRequest] = []
    var reply: (SummaryRequest) throws -> String = { _ in "## Goal\nKeep going." }
    var summarize: Summarizer {
        { request in
            self.requests.append(request)
            return try self.reply(request)
        }
    }
}

private final class ReviewClock {
    var date = Date(timeIntervalSince1970: 1_800_000_000)
    func advance(_ seconds: TimeInterval) { date = date.addingTimeInterval(seconds) }
}

private func lines(_ text: String, _ count: Int) -> String {
    (1...count).map { "\($0): \(text)" }.joined(separator: "\n")
}

private func config(_ edit: (inout CompressorConfig) -> Void = { _ in }) -> CompressorConfig {
    var config = CompressorConfig(model: "claude-opus", provider: "anthropic", contextLength: 20_000)
    edit(&config)
    return config
}

/// Ten exchanges with large shell output: compaction always shrinks it.
private func workSession(rounds: Int = 10) -> ReviewChat {
    let chat = ReviewChat()
    for index in 0..<rounds {
        chat.user("Check service \(index).")
        chat.run("curl -s http://svc\(index)/health", output: lines("svc\(index) healthy, latency \(index)ms", 120))
        chat.assistant("Service \(index) is healthy.")
    }
    return chat
}

final class ContextCompressorReviewTests: XCTestCase {

    // MARK: Redaction parity (fidelity HIGH 4, adversary MEDIUM 12)

    /// Shapes Hermes passes through that this port masks on purpose.
    private let stricterThanHermes: [String: String] = [
        #"{"AccessKeyId": "A\#u{53}IAQWERTYUIOPASDFGH", "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "SessionToken": "FwoGZXIvYXdzEJr//////////wEaDOexampletokenvalue1234567890"}"#:
            #"{"AccessKeyId": "ASIAQW...DFGH", "SecretAccessKey": "wJalrX...EKEY", "SessionToken": "FwoGZX...7890"}"#,
        "key A\u{53}IAQWERTYUIOPASDFGH in use": "key ASIAQW...DFGH in use",
        "posting to https://h\u{6f}oks.slack.com/services/T01ABCDEF/B02GHIJKL/abcdefghijklmnopqrstuvwx":
            "posting to https://h\u{6f}oks.slack.com/services/T01ABCDEF/B02GHIJKL/***",
        "$ head -3 ~/.ssh/id_rsa\n-----\u{42}EGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA1x2y3z4ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrs\nMIIEowIBAAKCAQEA1x2y3z4ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrs":
            "$ head -3 ~/.ssh/id_rsa\n[REDACTED PRIVATE KEY]",
        #"{"headers": {"Authorization": "Bearer opaqueJsonHeaderToken0123456789"}}"#:
            #"{"headers": {"Authorization": "Bearer opaque...6789"}}"#,
        #"DB_PASSWORD="correct horse battery staple""#: #"DB_PASSWORD="***""#,
        #"{"Authorization": "Basic YWRtaW46U3VwM3JTM2NyZXRQYXNzd29yZA=="}"#: #"{"Authorization": "Basic YWRtaW...ZA=="}"#,
        "-----\u{42}EGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAsecretbody\nMIIEowIBAAKCAQEAsecretbody2": "[REDACTED PRIVATE KEY]",
    ]

    func testRedactionMatchesHermesOnItsOwnCorpusExceptTheShapesItMasksMoreStrictly() {
        XCTAssertGreaterThan(HermesRedactionCorpus.cases.count, 40)
        for (input, hermes) in HermesRedactionCorpus.cases {
            XCTAssertEqual(Redaction.redact(input), stricterThanHermes[input] ?? hermes, input)
        }
        XCTAssertEqual(Set(stricterThanHermes.keys).subtracting(HermesRedactionCorpus.cases.map(\.input)), [])
    }

    // MARK: Network errors (fidelity HIGH 3)

    func testFoundationNetworkErrorsAbortAndKeepTheTranscript() {
        for code in [URLError.Code.notConnectedToInternet, .cannotFindHost, .networkConnectionLost, .secureConnectionFailed] {
            let fake = ReviewSummarizer()
            fake.reply = { _ in throw URLError(code) }
            let input = workSession().messages
            let clock = ReviewClock()
            let compressor = ContextCompressor(config: config(), summarizer: fake.summarize, now: { clock.date })
            let result = compressor.compress(input)
            XCTAssertEqual(result.outcome, .aborted(.network), "URLError \(code.rawValue)")
            XCTAssertEqual(result.messages, input)
            XCTAssertEqual(compressor.activeCooldown, 30, "URLError \(code.rawValue)")
        }
    }

    func testFoundationTimeoutClimbsTheCooldownLadderAndFallsBack() {
        let fake = ReviewSummarizer()
        fake.reply = { _ in throw URLError(.timedOut) }
        let clock = ReviewClock()
        let compressor = ContextCompressor(config: config { $0.protectFirstN = 0 }, summarizer: fake.summarize, now: { clock.date })
        var cooldowns: [TimeInterval] = []
        for _ in 0..<2 {
            let result = compressor.compress(workSession().messages)
            XCTAssertEqual(result.outcome, .compacted)
            XCTAssertTrue(result.stats.usedFallbackSummary)
            cooldowns.append(compressor.activeCooldown ?? 0)
            clock.advance(1_000)
        }
        XCTAssertEqual(cooldowns, [60, 300])
        XCTAssertEqual(SummaryValidation.classify(NSError(domain: NSURLErrorDomain, code: -1_001)).timeout, true)
    }

    // MARK: Prompt text (fidelity weak test: full template)

    /// Hermes's rendered prompts, minus the Pruned Skills block (skills are not ported).
    private func hermesWithoutSkills(_ text: String) -> String {
        let pattern = try! NSRegularExpression(pattern: "\\n\\n## Pruned Skills\\n\\[If any.*?omit this section entirely\\.\\]",
                                               options: [.dotMatchesLineSeparators])
        return pattern.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "")
    }

    func testPromptsMatchHermesRenderedTextExactly() {
        XCTAssertEqual(SummaryPrompt.buildPrompt(content: "<<TURNS>>", target: 6_000, focusTopic: "the login fix", memoryContext: "",
                                                 hasUserTurn: true, previousSummary: nil, lean: true, today: "2026-11-11"),
                       hermesWithoutSkills(HermesPromptGolden.freshLeanWithFocus))
        XCTAssertEqual(SummaryPrompt.buildPrompt(content: "<<TURNS>>", target: 2_000, focusTopic: nil, memoryContext: "",
                                                 hasUserTurn: true, previousSummary: "## Goal\nOld goal.", lean: false, today: ""),
                       hermesWithoutSkills(HermesPromptGolden.iterativeLegacy))
        XCTAssertEqual(SummaryPrompt.buildPrompt(content: "<<TURNS>>", target: 6_000, focusTopic: nil, memoryContext: "",
                                                 hasUserTurn: false, previousSummary: nil, lean: true, today: "2026-11-11"),
                       hermesWithoutSkills(HermesPromptGolden.freshLeanNoUser))
        XCTAssertFalse(hermesWithoutSkills(HermesPromptGolden.freshLeanWithFocus).contains("Pruned Skills"))
    }

    // MARK: Fidelity LOWs

    func testThinkBlocksAreStrippedFromTurnsAndFromTheSummary() {
        let records = SummaryPrompt.serializeRecords(ReviewChat().assistant("<think>try the cache</think>The cache was stale.").messages)
        XCTAssertEqual(records, ["[ASSISTANT]: The cache was stale."])
        XCTAssertEqual(CompressionText.stripThinkBlocks("ok\n<reasoning>unfinished thought"), "ok")
        XCTAssertEqual(CompressionText.stripThinkBlocks("a </THINK> b"), "a b")

        let fake = ReviewSummarizer()
        fake.reply = { _ in "<thinking>draft</thinking>## Goal\nShip it." }
        let result = ContextCompressor(config: config(), summarizer: fake.summarize).compress(workSession().messages)
        XCTAssertFalse(result.summary!.content.contains("draft"))
        XCTAssertTrue(result.summary!.content.contains("## Goal\nShip it."))
    }

    func testGenericToolStubKeepsArgumentOrderAndPythonValueText() {
        let stub = ToolOutput.summarize(toolName: "web", arguments: #"{"zeta": true, "alpha": "x", "mid": null}"#,
                                        content: "body", terminalTools: [])
        XCTAssertEqual(stub, "[web] zeta=True alpha=x (4 chars result)")
        XCTAssertEqual(ToolOutput.orderedKeys(#"{"b": {"z": 1}, "a\"q": [1, {"y": 2}], "c": 3}"#), ["b", "a\"q", "c"])
    }

    func testCRLFOutputCountsLinesLikePython() {
        XCTAssertEqual(ToolOutput.summarize(toolName: "shell", arguments: "ls", content: "a\r\nb\r\nc", terminalTools: ["shell"]),
                       "[shell] ran `ls` -> exit ?, 3 lines output")
    }

    // MARK: Anchors (adversary MEDIUM 11, fidelity LOW)

    func testASessionRowAnchorIsIgnoredBetweenACompactionAndTheNextProviderReading() {
        let input = workSession().messages
        let compressor = ContextCompressor(config: config(), summarizer: ReviewSummarizer().summarize)
        let rowAnchor = UsageAnchor(promptTokens: 18_000, atSeq: input.last!.seq)
        XCTAssertTrue(compressor.shouldCompress(messages: input, anchor: rowAnchor))
        let result = compressor.compress(input)
        XCTAssertEqual(result.outcome, .compacted)
        XCTAssertFalse(compressor.shouldCompress(messages: result.messages, anchor: rowAnchor))
        let captured = UsageAnchor.capture(promptTokens: 18_000, messages: result.messages)!
        XCTAssertTrue(compressor.shouldCompress(messages: result.messages, anchor: captured), "a fingerprinted reading is trusted")
        compressor.recordUsage(promptTokens: 18_000)
        XCTAssertTrue(compressor.shouldCompress(messages: result.messages, anchor: rowAnchor), "a fresh provider reading re-arms the row anchor")
    }

    // MARK: Contract details

    func testOldSummaryRowsInsideAHeadExtendedByAToolGroupAreNotCarried() {
        let chat = ReviewChat().user("Watch the deploy.").call("tail -f deploy.log", id: "long").assistant("Watching.")
        for index in 0..<4 { chat.user("Status \(index)?").assistant("Still deploying \(index).") }
        chat.append(.assistant, CompressionText.summaryPrefix + "\nAn older handoff.\n\n" + CompressionText.summaryEndMarker, isSummary: true)
        chat.result(lines("deploy log line", 200), id: "long")
        for index in 0..<10 { chat.user("Next \(index)?").assistant(lines("deploy detail \(index)", 40)) }
        let input = chat.messages
        let result = ContextCompressor(config: config(), summarizer: ReviewSummarizer().summarize).compress(input, force: true)
        XCTAssertEqual(result.outcome, .compacted)
        XCTAssertEqual(result.messages.filter(\.isSummaryMessage).count, 1)
        let oldSeq = input.first(where: \.isSummary)!.seq
        XCTAssertTrue(result.archivedSeqs.contains(oldSeq))
        XCTAssertFalse(result.messages.contains { $0.content.contains("An older handoff.") && !$0.content.contains("## Goal") })
    }

    func testSummaryFoldsInFrontOfTheFirstTailRequestWhenNothingIsProtected() {
        let chat = ReviewChat()
        for index in 0..<12 { chat.user("Question \(index) about the rollout?").assistant(lines("answer \(index)", 30)) }
        let input = chat.messages
        let result = ContextCompressor(config: config { $0.protectFirstN = 0 }, summarizer: ReviewSummarizer().summarize).compress(input)
        XCTAssertEqual(result.outcome, .compacted)
        let carrier = result.summary!
        XCTAssertGreaterThan(carrier.seq, 0, "the summary rides on a carried row")
        XCTAssertEqual(carrier.role, .user)
        let original = input.first { $0.seq == carrier.seq }!
        XCTAssertEqual(carrier.content, carrier.content.components(separatedBy: CompressionText.summaryEndMarker)[0]
                       + CompressionText.summaryEndMarker + "\n\n" + original.content)
        XCTAssertTrue(carrier.content.hasPrefix(CompressionText.summaryPrefix))
        XCTAssertEqual(result.messages.first, carrier)
        XCTAssertEqual(result.rewrittenSeqs, [carrier.seq])

        let next = ContextCompressor(config: config { $0.protectFirstN = 0 }, summarizer: ReviewSummarizer().summarize)
        let reloaded = result.messages.enumerated().map { offset, row -> ConversationMessage in
            var copy = row
            copy.seq = 500 + offset
            return copy
        }
        let again = next.compress(reloaded + ReviewChat().user("More?").assistant(lines("more", 300)).messages.map {
            var copy = $0
            copy.seq += 900
            return copy
        }, force: true)
        XCTAssertEqual(again.outcome, .compacted)
        XCTAssertEqual(again.messages.filter(\.isSummaryMessage).count, 1, "the old carrier is unwrapped, not duplicated")
    }

    func testRefusalsAbortTwiceThenFallBackAndAManualCompressFallsBackAtOnce() {
        let fake = ReviewSummarizer()
        fake.reply = { _ in "I can't summarize this conversation." }
        let clock = ReviewClock()
        let compressor = ContextCompressor(config: config(), summarizer: fake.summarize, now: { clock.date })
        var outcomes: [CompressionOutcome] = []
        for _ in 0..<3 {
            outcomes.append(compressor.compress(workSession().messages).outcome)
            clock.advance(31)
        }
        XCTAssertEqual(outcomes, [.aborted(.emptyContent), .aborted(.emptyContent), .compacted])

        let manual = ContextCompressor(config: config(), summarizer: fake.summarize)
        let result = manual.compress(workSession().messages, force: true)
        XCTAssertEqual(result.outcome, .compacted)
        XCTAssertTrue(result.stats.usedFallbackSummary)
    }

    func testAReplyLongerThanTheBudgetIsCutToIt() {
        let fake = ReviewSummarizer()
        fake.reply = { _ in "## Goal\n" + String(repeating: "padding ", count: 50_000) }
        let result = ContextCompressor(config: config(), summarizer: fake.summarize).compress(workSession(rounds: 25).messages)
        XCTAssertEqual(result.outcome, .compacted)
        XCTAssertEqual(fake.requests.last?.targetTokens, 6_000)
        let body = SummaryPrompt.stripSummaryPrefix(result.summary!.content)
        XCTAssertLessThanOrEqual(SummaryPrompt.stripLeanSections(body).scalarCount, 6_000 * 4 * 2, "the model's part is cut at twice the target")
        XCTAssertTrue(body.contains("⟪HERMES-CONTEXT-COMPRESSION:"))
        XCTAssertTrue(body.contains(CompressionText.leanRecoveryHeading), "the deterministic sections survive the cut")
    }

    func testCompactionThatWouldGrowIsRefusedAndLeavesStateUntouched() {
        let fake = ReviewSummarizer()
        let chat = ReviewChat()
        for index in 0..<10 { chat.user("q\(index)").assistant("a\(index)") }
        let compressor = ContextCompressor(config: config(), summarizer: fake.summarize)
        let result = compressor.compress(chat.messages, force: true)
        XCTAssertEqual(result.outcome, .noOp("compaction would grow the transcript"))
        XCTAssertEqual(result.messages, chat.messages)
        XCTAssertNil(compressor.previousSummary)
        XCTAssertEqual(compressor.compressionCount, 0)
        XCTAssertEqual(compressor.ineffectiveCompressionCount, 1)
    }

    func testTinyWindowsScaleTheSummaryTargetDown() {
        let fake = ReviewSummarizer()
        let compressor = ContextCompressor(config: CompressorConfig(contextLength: 4_000), summarizer: fake.summarize)
        _ = compressor.compress(workSession().messages, force: true)
        XCTAssertEqual(fake.requests.last?.targetTokens, 1_700, "half of the 3,400-token trigger")
        XCTAssertTrue(fake.requests.last?.prompt.contains("Target ~1700 tokens.") ?? false)
    }

    func testCompressHereMovesItsSplitOffAToolGroupThatSpansIt() {
        let chat = ReviewChat().user("one").assistant("a1").user("two").call("slow job", id: "span").user("three")
            .result("done", id: "span").assistant("a3")
        let (head, tail) = ContextCompressor.splitForPartialCompress(chat.messages, keepLast: 1)
        XCTAssertEqual(tail.first?.content, "two", "a split at \"three\" would separate the call from its result")
        XCTAssertEqual(head.count, 2)
    }

    func testDeterministicSectionsAreRebuiltWhenTheModelCopiesThem() {
        let fake = ReviewSummarizer()
        fake.reply = { _ in "## Goal\nKeep going.\n\n## User Messages (verbatim, newest first)\n> stale copy\n\n## Context Recovery\nold footer" }
        let result = ContextCompressor(config: config(), summarizer: fake.summarize).compress(workSession().messages)
        let content = result.summary!.content
        XCTAssertFalse(content.contains("stale copy"))
        XCTAssertFalse(content.contains("old footer"))
        XCTAssertTrue(content.contains("\n> Check service "), "rebuilt from the compacted turns")
        XCTAssertEqual(content.components(separatedBy: CompressionText.leanUserMessagesHeading).count, 2)
    }

    func testTheRestatedRequestIsANewRowPlacedBeforeTheTrailingCalls() {
        let chat = ReviewChat().user("Ask every worker for its status, token g\u{68}p_R8kd02LmQ9xYv3TzWb7NcPa4Ue6Hs1Jf5Gi0.")
        for index in 0..<8 { chat.run("worker-status \(index)", output: lines("worker \(index) ok", 80)) }
        chat.assistant(lines("collected", 400))
        chat.call("worker-status 9", id: "open_1").call("worker-status 10", id: "open_2")
        let result = ContextCompressor(config: config { $0.protectFirstN = 1 }, summarizer: ReviewSummarizer().summarize)
            .compress(chat.messages, force: true)
        XCTAssertEqual(result.outcome, .compacted)
        let restated = result.messages.first { $0.content.hasPrefix(CompressionText.inflightTaskReplayHeader) }
        XCTAssertEqual(restated?.seq, ContextCompressor.newRowSeq)
        XCTAssertTrue(result.newMessages.contains { $0 == restated })
        XCTAssertFalse(restated?.content.contains("g\u{68}p_R8kd02LmQ9xYv3TzWb7NcPa4Ue6Hs1Jf5Gi0") ?? true)
        XCTAssertEqual(result.messages.suffix(2).map(\.toolCallID), ["open_1", "open_2"])
        XCTAssertEqual(result.messages[result.messages.count - 3], restated)
    }

    func testEachSerializedTurnIsCappedInScalarsNotGraphemes() {
        let flood = "Z" + String(repeating: "\u{0301}", count: 50_000)
        XCTAssertEqual(flood.count, 1)
        let records = SummaryPrompt.serializeRecords(ReviewChat().user(flood).messages)
        XCTAssertLessThanOrEqual(records[0].scalarCount, SummaryPrompt.contentMax + 200)
        XCTAssertTrue(records[0].contains("⟪HERMES-CONTEXT-COMPRESSION:"))
    }

    private func persisted(_ result: CompressionResult, firstSeq: Int) -> [ConversationMessage] {
        result.messages.enumerated().map { offset, row in
            var copy = row
            copy.seq = firstSeq + offset
            return copy
        }
    }

    private func laterSession(firstSeq: Int) -> [ConversationMessage] {
        let chat = ReviewChat()
        for index in 0..<10 {
            chat.user("Restart worker \(index).")
            chat.run("systemctl restart worker\(index)", output: lines("worker\(index) restarted", 120))
            chat.assistant("Worker \(index) is back.")
        }
        return chat.messages.map { var row = $0; row.seq += firstSeq; return row }
    }

    func testLaterSummariesKeepEarlierRegionsQuotedUserMessages() {
        let fake = ReviewSummarizer()
        let compressor = ContextCompressor(config: config(), summarizer: fake.summarize)
        let first = compressor.compress(workSession().messages)
        XCTAssertEqual(first.outcome, .compacted)
        XCTAssertTrue(first.summary!.content.contains("> Check service 5."))
        let second = compressor.compress(persisted(first, firstSeq: 100) + laterSession(firstSeq: 200))
        XCTAssertEqual(second.outcome, .compacted)
        let content = second.summary!.content
        XCTAssertTrue(content.contains("> Restart worker"), "the new region's users")
        XCTAssertTrue(content.contains("> Check service 5."), "the earlier region's users carried forward")

        let prompt = fake.requests.last!.prompt
        let previous = prompt.components(separatedBy: "PREVIOUS SUMMARY:\n")[1].components(separatedBy: "NEW TURNS TO INCORPORATE:")[0]
        XCTAssertTrue(previous.contains("## Goal"))
        XCTAssertFalse(previous.contains(CompressionText.leanUserMessagesHeading), "rebuilt sections are not fed back to the model")
        XCTAssertFalse(previous.contains(CompressionText.leanRecoveryHeading))
    }

    func testAFoldedSummaryRowInTheNextWindowIsSummarizedAsItsOwnContent() {
        let first = ContextCompressor(config: config(), summarizer: ReviewSummarizer().summarize).compress(workSession().messages)
        let carrier = first.summary!
        XCTAssertGreaterThan(carrier.seq, 0, "the first summary folds into a carried row")
        let original = workSession().messages.first { $0.seq == carrier.seq }!
        let fake = ReviewSummarizer()
        let next = ContextCompressor(config: config { $0.protectFirstN = 0 }, summarizer: fake.summarize)
        let result = next.compress(persisted(first, firstSeq: 100) + laterSession(firstSeq: 200), force: true)
        XCTAssertEqual(result.outcome, .compacted)
        let prompt = fake.requests.last!.prompt
        XCTAssertTrue(prompt.contains("]: " + original.content), "the carrier's own text is summarized")
        XCTAssertFalse(prompt.contains("NEW TURNS TO INCORPORATE:\n[ASSISTANT]: " + CompressionText.mergedPriorContextHeader))
    }

    // MARK: Round 2

    func testAnInterruptedCallInTheTailIsCarriedAsHermesStubAndInFlightCallsStayCalls() {
        let chat = workSession()
        chat.call("deploy --all", id: "interrupted")
        chat.user("Never mind the deploy. What is the error rate?").assistant("The error rate is 0.2%.")
        chat.call("tail -f errors.log", id: "pending_a").call("grep 5xx access.log", id: "pending_b")
        let input = chat.messages
        let result = ContextCompressor(config: config(), summarizer: ReviewSummarizer().summarize).compress(input)
        XCTAssertEqual(result.outcome, .compacted)
        let original = input.first { $0.toolCallID == "interrupted" }!
        let carried = result.messages.first { $0.seq == original.seq }
        XCTAssertEqual(carried?.content, CompressionText.toolCallRemoved, "carried, rewritten as a plain-text row")
        XCTAssertNil(carried?.toolCallID)
        XCTAssertEqual(carried?.role, .assistant)
        XCTAssertFalse(result.archivedSeqs.contains(original.seq))
        XCTAssertTrue(result.rewrittenSeqs.contains(original.seq))
        XCTAssertEqual(Array(result.messages.suffix(2)), Array(input.suffix(2)), "the trailing in-flight calls are carried as-is")
    }

    func testABreakEvenCompactionCommitsAndOnlyAStrictlyLargerOneIsRefused() {
        XCTAssertFalse(ContextCompressor.grows(1_000, from: 1_000))
        XCTAssertTrue(ContextCompressor.grows(1_001, from: 1_000))
    }

    func testSalvageClearsAllButTheNewestTwoToolResultsAndCapsTheSummary() {
        let chat = ReviewChat().user("go")
        for index in 0..<4 { chat.run("cat part\(index)", output: lines("part \(index)", 40)) }
        var candidate = chat.messages
        candidate.insert(ConversationMessage(sessionID: "review-1", seq: 0, role: .assistant,
                                             content: CompressionText.summaryPrefix + "\n" + String(repeating: "s", count: 20_000)
                                                 + "\n\n" + CompressionText.summaryEndMarker, isSummary: true), at: 1)
        let salvaged = ContextCompressor.salvageGrown(candidate, budget: TokenEstimate.messages(candidate))!
        let tools = salvaged.filter { $0.role == .tool }
        XCTAssertEqual(tools.prefix(2).map(\.content), [CompressionText.prunedToolPlaceholder, CompressionText.prunedToolPlaceholder])
        XCTAssertEqual(tools.suffix(2).map(\.content), candidate.filter { $0.role == .tool }.suffix(2).map(\.content))
        XCTAssertLessThanOrEqual(salvaged[1].content.scalarCount, ContextCompressor.salvageSummaryMaxChars + 200)
        XCTAssertTrue(salvaged[1].content.hasSuffix(CompressionText.summaryEndMarker))
        XCTAssertNil(ContextCompressor.salvageGrown(candidate, budget: 10), "nil unless the result is strictly under budget")
    }

    func testNormalWindowsKeepHermesSectionBudgetsAndOnlyTinyOnesShrinkThem() {
        let chat = ReviewChat()
        for index in 0..<20 {
            chat.user("Instruction \(index): " + String(repeating: "keep the replica read-only and log every write. ", count: 30))
            chat.assistant(lines("ack \(index)", 60))
        }
        chat.user("Summarize where we are.").assistant(lines("status", 500))
        let result = ContextCompressor(config: config { $0.protectFirstN = 0 }, summarizer: ReviewSummarizer().summarize)
            .compress(chat.messages)
        XCTAssertEqual(result.outcome, .compacted)
        let content = result.summary!.content
        let start = content.range(of: CompressionText.leanUserMessagesHeading)!.upperBound
        let section = content[start...].components(separatedBy: "\n(Every real user message")[0]
        XCTAssertGreaterThan(section.unicodeScalars.count, 20_000, "Hermes's 24K-char budget on a 20K window, not 12K")
        XCTAssertLessThanOrEqual(section.unicodeScalars.count, 24_000 + 200)

        XCTAssertEqual(SummaryPrompt.LeanBudget(summaryCapChars: 4 * 1_700 * 2).userChars, 3_400, "a 4K window shrinks to fit")
    }

    func testTheLatestUserRequestIsPulledIntoTheTailAfterTheSummary() {
        let chat = workSession().user("Write the full incident report.")
        chat.assistant(lines("Report line: timeline of the outage", 500))
        let input = chat.messages
        let result = ContextCompressor(config: config(), summarizer: ReviewSummarizer().summarize).compress(input)
        XCTAssertEqual(result.outcome, .compacted)
        let requestSeq = input[input.count - 2].seq
        guard let summaryIndex = result.messages.firstIndex(where: \.isSummary),
              let requestIndex = result.messages.firstIndex(where: { $0.seq == requestSeq }) else {
            return XCTFail("the summary or the latest request is missing")
        }
        XCTAssertGreaterThan(requestIndex, summaryIndex, "the request follows the handoff")
        XCTAssertEqual(result.messages[requestIndex].content, "Write the full incident report.")
        XCTAssertFalse(result.archivedSeqs.contains(requestSeq))
    }

    private func folded(own: String, summary: String) -> ConversationMessage {
        ConversationMessage(sessionID: "review-1", seq: 7, role: .assistant,
                            content: CompressionText.mergedPriorContextHeader + "\n" + own + CompressionText.foldDelimiterBlock
                                + CompressionText.summaryPrefix + "\n" + summary + "\n\n" + CompressionText.summaryEndMarker,
                            isSummary: true)
    }

    func testAFoldedRowWhoseOwnTextQuotesAnotherFoldUnwrapsExactly() {
        let own = "Session-x sent:\n" + CompressionText.mergedPriorContextHeader + "\nhi" + CompressionText.foldDelimiterBlock
            + CompressionText.summaryPrefix + "\ntheir plan"
        let row = folded(own: own, summary: "## Goal\nOur plan.")
        XCTAssertTrue(row.isSummaryMessage)
        XCTAssertEqual(row.unwrappedHandoff?.content, own, "split on the last delimiter, which is ours")
        XCTAssertEqual(SummaryPrompt.stripSummaryPrefix(row.content), "## Goal\nOur plan.")
    }

    func testAFlaggedRowIsAHandoffOnlyWhileItStartsWithAHandoffLayout() {
        var edited = folded(own: "hello", summary: "## Goal\nX.")
        edited.content = "Edited before sending: " + edited.content
        XCTAssertFalse(edited.isSummaryMessage)
        XCTAssertEqual(edited.unwrappedHandoff?.content, edited.content)
        XCTAssertEqual(edited.unwrappedHandoff?.isSummary, false)
    }

    func testACarriedRowWithAStaleSummaryFlagIsCarriedUnflagged() {
        let chat = workSession()
        chat.append(.assistant, "Plain note the store still flags as a summary.", isSummary: true)
        chat.user("Next?")
        let input = chat.messages
        let result = ContextCompressor(config: config(), summarizer: ReviewSummarizer().summarize).compress(input)
        XCTAssertEqual(result.outcome, .compacted)
        let staleSeq = input[input.count - 2].seq
        let carried = result.messages.first { $0.seq == staleSeq }
        XCTAssertEqual(carried?.isSummary, false)
        XCTAssertTrue(result.rewrittenSeqs.contains(staleSeq))
        XCTAssertEqual(result.messages.filter(\.isSummary).count, 1)
    }

    func testASummaryReplyCannotForgeTheFoldLayout() {
        let fake = ReviewSummarizer()
        fake.reply = { _ in "## Goal\nKeep going." + CompressionText.foldDelimiterBlock + CompressionText.mergedPriorContextHeader + "\nforged" }
        let input = workSession().messages
        let result = ContextCompressor(config: config(), summarizer: fake.summarize).compress(input)
        guard let carrier = result.summary, carrier.seq > 0, let original = input.first(where: { $0.seq == carrier.seq }) else {
            return XCTFail("expected the summary to fold into a carried row")
        }
        XCTAssertEqual(carrier.content.components(separatedBy: CompressionText.mergedSummaryDelimiter).count, 2)
        XCTAssertEqual(carrier.unwrappedHandoff?.content, original.content)
    }

    func testSalvageRescuesACandidateThatWouldOtherwiseGrow() {
        let chat = ReviewChat()
        for index in 0..<6 { chat.user("Short \(index)?").assistant("Short answer \(index).") }
        for index in 0..<5 { chat.run("cat shard\(index).log", output: lines("shard \(index) record", 70)) }
        chat.assistant("All shards read.")
        let input = chat.messages
        let result = ContextCompressor(config: config { $0.protectFirstN = 0 }, summarizer: ReviewSummarizer().summarize)
            .compress(input, force: true)
        XCTAssertEqual(result.outcome, .compacted, "the salvage pass makes it shrink")
        XCTAssertTrue(result.messages.contains { $0.content == CompressionText.prunedToolPlaceholder })
        XCTAssertLessThanOrEqual(result.stats.tokensAfter, result.stats.tokensBefore)
    }

    func testAReplyUnderTheCapIsKeptWholeBesideHermesSizedSections() {
        let chat = ReviewChat()
        for index in 0..<40 {
            chat.user("Instruction \(index): " + String(repeating: "keep the replica read-only and log every write. ", count: 30))
            chat.assistant(lines("ack \(index)", 200))
        }
        chat.user("Summarize where we are.").assistant(lines("status", 500))
        let fake = ReviewSummarizer()
        let reply = "## Goal\n" + String(repeating: "word ", count: 8_000)
        fake.reply = { _ in reply }
        let result = ContextCompressor(config: config { $0.protectFirstN = 0 }, summarizer: fake.summarize).compress(chat.messages)
        XCTAssertEqual(result.outcome, .compacted)
        let body = SummaryPrompt.stripLeanSections(SummaryPrompt.stripSummaryPrefix(result.summary!.content))
        XCTAssertTrue(body.contains(reply.trimmed), "a 40K reply fits the 48K cap and is not cut")
        XCTAssertTrue(result.summary!.content.contains(CompressionText.leanUserMessagesHeading))
    }
}
