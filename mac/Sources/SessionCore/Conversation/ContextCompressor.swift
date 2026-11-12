// Ported from Hermes Agent (MIT) agent/context_compressor.py, agent/context_compressor_window.py, agent/context_compressor_prune.py, agent/context_compressor_prellm.py, agent/context_compressor_summary.py, agent/conversation_compression_manual.py and hermes_cli/partial_compress.py.
import Foundation

public enum CompressionOutcome: Equatable, Sendable {
    case compacted
    /// Nothing eligible to compress, or compaction would not shrink the transcript; it is returned unchanged.
    case noOp(String)
    /// A summary failure that must not drop context; the transcript is returned unchanged.
    case aborted(SummaryFailureClass)
}

public struct CompressionStats: Equatable, Sendable {
    public var tokensBefore: Int
    public var tokensAfter: Int
    /// Tool results pruned or demoted to stubs before summarizing.
    public var prunedToolResults: Int
    public var summarizedMessages: Int
    public var usedFallbackSummary: Bool
}

/// What the store persists with `applyCompaction(sessionID:newActive: messages)`:
/// - `messages` is the new active transcript in array order (the store assigns fresh seqs in that order).
/// - A row with `seq > 0` is carried: it is an input row, possibly with rewritten content (pruned tool output, the
///   system-prompt note, or a folded-in summary), and the store supersedes the old copy.
/// - A row with `seq <= 0` is new: the summary row (unless the summary was folded into a carried row) or a restated
///   in-flight request.
/// - Every input row with `seq > 0` that is not carried is archived (`archivedSeqs`): inactive but searchable.
public struct CompressionResult: Sendable {
    public var outcome: CompressionOutcome
    public var messages: [ConversationMessage]
    /// The row carrying the summary: a new row, or a carried row the summary was folded into.
    public var summary: ConversationMessage?
    /// Rows with `seq <= 0`.
    public var newMessages: [ConversationMessage]
    /// Input seqs that are no longer active, ascending.
    public var archivedSeqs: [Int]
    /// Carried rows whose content or role changed.
    public var rewrittenSeqs: [Int]
    public var stats: CompressionStats
    /// Text worth showing the user (abort reason or fallback notice).
    public var warning: String?

    /// `archivedSeqs` as contiguous ranges, for display.
    public var archivedSeqRanges: [ClosedRange<Int>] { ContextCompressor.ranges(archivedSeqs) }
}

public struct PruneResult: Sendable {
    public var messages: [ConversationMessage]
    public var prunedCount: Int
    public var rewrittenSeqs: [Int]
}

/// Compacts a chat transcript: prunes old tool output, protects the head and a token-budget tail, summarizes the
/// middle with an injected summarizer, and falls back to a deterministic summary when the summarizer fails.
public final class ContextCompressor {
    public private(set) var config: CompressorConfig
    private let summarizer: Summarizer
    private let now: () -> Date

    public private(set) var compressionCount = 0
    /// The last summary body; the next compaction updates it instead of starting over.
    public private(set) var previousSummary: String?
    /// Redacted text of the last summary failure.
    public private(set) var lastSummaryError: String?
    private var summaryHasUserTurn: Bool?

    private var cooldownUntil = Date.distantPast
    private var consecutiveTimeoutFailures = 0
    private var consecutiveTruncationFailures = 0
    private var consecutiveOverloadAborts = 0
    private var consecutiveEmptyAborts = 0
    private var terminalFailures: Set<SummaryFailureClass> = []
    private var overloadDegraded = false
    private var fallbackCompressionStreak = 0
    private var fallbackProbeAt: Date?
    public private(set) var ineffectiveCompressionCount = 0
    private var antiThrashDeadline: Date?
    private var structuralBackoffUntil = Date.distantPast
    private var structuralNoOpLength = 0
    private var verifyCompactionClearedThreshold = false
    private var proactivePruneRearmTokens = 0

    /// Seq of a row the compressor creates; the store assigns the real one.
    public static let newRowSeq = 0
    static let antiThrashRecoverySeconds: TimeInterval = 300
    static let structuralNoOpBackoffSeconds: TimeInterval = 300
    static let feasibilitySkipMiddleFraction = 0.10
    static let restartHandoffProbeExtraMessages = 4
    static let minManualMessages = 4
    public static let defaultKeepLast = 2
    static let maxKeepLast = 100
    /// The smallest summary target; below this a handoff cannot hold the template.
    static let minSummaryTargetTokens = 500
    private static let terminalPrecedence: [SummaryFailureClass] = [.accessOrQuota, .network, .truncated, .emptyContent, .overloaded]

    public init(config: CompressorConfig, summarizer: @escaping Summarizer, now: @escaping () -> Date = Date.init) {
        self.config = config.sanitized
        self.summarizer = summarizer
        self.now = now
    }

    public var thresholdTokens: Int { config.thresholdTokens }

    // MARK: Trigger

    public func shouldCompress(promptTokens: Int) -> Bool {
        shouldCompressInfo(promptTokens: promptTokens).shouldCompress
    }

    /// `reason` is set only when compression is needed but blocked: `cooldown:<s>`, `structural_backoff:<s>` or `ineffective`.
    public func shouldCompressInfo(promptTokens: Int) -> (shouldCompress: Bool, reason: String?) {
        if promptTokens < thresholdTokens { return (false, nil) }
        if automaticCompressionBlocked() { return (false, blockReason() ?? "blocked") }
        return (true, nil)
    }

    /// The trigger over a transcript: usage-anchored when the anchor still fits, else a rough estimate. Between a
    /// compaction and the next provider reading, an anchor without a prefix fingerprint (one restored from the session
    /// row) cannot prove it priced the compacted transcript, so it is ignored.
    public func shouldCompress(messages: [ConversationMessage], anchor: UsageAnchor?) -> Bool {
        liftStructuralBackoffIfGrown(messageCount: messages.count)
        let usable = anchor.flatMap { verifyCompactionClearedThreshold && $0.prefixFingerprint == nil ? nil : $0 }
        return shouldCompress(promptTokens: UsageAnchor.contextTokens(messages, anchor: usable))
    }

    /// Feed the provider's reported prompt size after each response; it judges whether the last compaction worked.
    public func recordUsage(promptTokens: Int) {
        if promptTokens > 0 {
            if promptTokens < thresholdTokens { ineffectiveCompressionCount = 0 }
            if verifyCompactionClearedThreshold {
                ineffectiveCompressionCount = promptTokens >= thresholdTokens ? ineffectiveCompressionCount + 1 : 0
            }
        }
        verifyCompactionClearedThreshold = false
    }

    public func liftStructuralBackoffIfGrown(messageCount: Int) {
        if structuralBackoffUntil > now() && messageCount > structuralNoOpLength {
            structuralBackoffUntil = .distantPast
        }
    }

    /// Seconds left on the summary-failure cooldown, or nil when none is armed.
    public var activeCooldown: TimeInterval? {
        let remaining = cooldownUntil.timeIntervalSince(now())
        return remaining > 0 ? remaining : nil
    }

    /// Switch models: new trigger math, and a new runtime gets an immediate summary attempt.
    public func updateModel(_ newConfig: CompressorConfig) {
        let runtimeChanged = (newConfig.model, newConfig.provider) != (config.model, config.provider)
        config = newConfig.sanitized
        ineffectiveCompressionCount = 0
        verifyCompactionClearedThreshold = false
        proactivePruneRearmTokens = 0
        if runtimeChanged {
            fallbackCompressionStreak = 0
            clearCooldown()
            consecutiveOverloadAborts = 0
            consecutiveEmptyAborts = 0
        }
    }

    /// Forget all per-session state (new chat or session switch).
    public func resetSession() {
        compressionCount = 0
        previousSummary = nil
        summaryHasUserTurn = nil
        lastSummaryError = nil
        clearCooldown()
        consecutiveOverloadAborts = 0
        consecutiveEmptyAborts = 0
        terminalFailures = []
        overloadDegraded = false
        fallbackCompressionStreak = 0
        fallbackProbeAt = nil
        ineffectiveCompressionCount = 0
        antiThrashDeadline = nil
        structuralBackoffUntil = .distantPast
        structuralNoOpLength = 0
        verifyCompactionClearedThreshold = false
        proactivePruneRearmTokens = 0
    }

    private func blockReason() -> String? {
        for (label, until) in [("cooldown", cooldownUntil), ("structural_backoff", structuralBackoffUntil)] {
            let remaining = until.timeIntervalSince(now())
            if remaining > 0 { return "\(label):\(Int(remaining.rounded()))" }
        }
        return ineffectiveCompressionCount >= 2 ? "ineffective" : nil
    }

    private func automaticCompressionBlocked() -> Bool {
        let current = now()
        if cooldownUntil > current || structuralBackoffUntil > current { return true }
        guard ineffectiveCompressionCount >= 2 else {
            antiThrashDeadline = nil
            return false
        }
        if let deadline = antiThrashDeadline, deadline.timeIntervalSince(current) <= Self.antiThrashRecoverySeconds {
            if current >= deadline {
                antiThrashDeadline = nil
                ineffectiveCompressionCount = 1
                return false
            }
            return true
        }
        antiThrashDeadline = current.addingTimeInterval(Self.antiThrashRecoverySeconds)
        return true
    }

    // MARK: Compression entry points

    public func hasContentToCompress(_ messages: [ConversationMessage]) -> Bool {
        let start = TranscriptShape.alignForward(messages, protectHeadSize(messages))
        return start < findTailCut(messages, headEnd: start)
    }

    /// Summarize the middle of `messages` (active rows in order). `force` is a manual compress: it clears the failure
    /// cooldown, skips the pre-summary benches, and falls back to the deterministic summary on an empty or refused reply.
    public func compress(_ messages: [ConversationMessage], focusTopic: String? = nil, force: Bool = false,
                         memoryContext: String = "", bypassCooldown: Bool = false) -> CompressionResult {
        compress(messages, focusTopic: focusTopic, force: force, memoryContext: memoryContext,
                 bypassCooldown: bypassCooldown, restateInflight: true)
    }

    /// Manual compress that keeps the last `keepLast` exchanges (counted by user messages) verbatim and
    /// summarizes everything before them.
    public func compressHere(_ messages: [ConversationMessage], keepLast: Int = defaultKeepLast,
                             focusTopic: String? = nil) -> CompressionResult {
        if messages.count < Self.minManualMessages {
            return unchanged(messages, outcome: .noOp("too few messages to compress"))
        }
        let keep = max(1, min(keepLast, Self.maxKeepLast))
        let (head, tail) = Self.splitForPartialCompress(messages, keepLast: keep)
        guard !tail.isEmpty else { return compress(messages, focusTopic: focusTopic, force: true) }
        // The kept exchanges follow the summary, so the head's last request is never restated ahead of them.
        let headResult = compress(head, focusTopic: focusTopic, force: true, memoryContext: "", bypassCooldown: false,
                                  restateInflight: false)
        guard headResult.outcome == .compacted else {
            return unchanged(messages, outcome: headResult.outcome, warning: headResult.warning)
        }
        let joined = sanitizeToolPairs(Self.rejoin(headResult.messages, tail))
        return makeResult(input: messages, output: joined, outcome: headResult.outcome,
                          prunedCount: headResult.stats.prunedToolResults,
                          summarized: headResult.stats.summarizedMessages,
                          usedFallback: headResult.stats.usedFallbackSummary, warning: headResult.warning)
    }

    /// Head is summarized; tail is the last `keepLast` exchanges, starting on a user message. A boundary that would
    /// separate a call from its result moves to an older user message; with none left the whole history compresses.
    static func splitForPartialCompress(_ history: [ConversationMessage], keepLast: Int) -> ([ConversationMessage], [ConversationMessage]) {
        let users = history.indices.reversed().filter { history[$0].role == .user }
        for boundary in users.dropFirst(max(keepLast, 1) - 1) where boundary > 0 {
            if TranscriptShape.straddlingCalls(history, cut: boundary).isEmpty {
                return (Array(history[..<boundary]), Array(history[boundary...]))
            }
        }
        return (history, [])
    }

    /// Join a compressed head and the verbatim tail, folding the seam when both sides share a user/assistant role.
    static func rejoin(_ head: [ConversationMessage], _ tail: [ConversationMessage]) -> [ConversationMessage] {
        guard !tail.isEmpty else { return head }
        guard var last = head.last else { return tail }
        var rest = tail
        if last.role == rest[0].role && (last.role == .user || last.role == .assistant) && !last.isToolCall && !rest[0].isToolCall {
            last.content = "\(last.content)\n\n\(rest[0].content)"
            rest.removeFirst()
            return Array(head.dropLast()) + [last] + rest
        }
        return head + rest
    }

    /// Deterministic, no-summary prune of old tool output, gated on `proactivePruneTokens`. Commits only when it
    /// reclaims at least `proactivePruneMinReclaimTokens`, then waits for a full regrowth before pruning again.
    public func pruneToolResultsOnly(_ messages: [ConversationMessage], currentTokens: Int? = nil) -> PruneResult {
        let unchanged = PruneResult(messages: messages, prunedCount: 0, rewrittenSeqs: [])
        guard config.proactivePruneTokens > 0, currentTokens.map({ $0 >= config.proactivePruneTokens }) ?? true else {
            return unchanged
        }
        if messages.count <= config.protectLastN + protectHeadSize(messages) + 1 { return unchanged }
        let before = messages.reduce(0) { $0 + TokenEstimate.messageBudget($1) }
        let billedOver = currentTokens.map { thresholdTokens > 0 && $0 >= thresholdTokens } ?? false
        if before < proactivePruneRearmTokens && !billedOver { return unchanged }
        let (pruned, count) = pruneOldToolResults(messages, protectTailCount: config.protectLastN, protectTailTokens: nil,
                                                  minPruneChars: config.proactivePruneMinResultChars)
        guard count > 0 else { return unchanged }
        let after = pruned.reduce(0) { $0 + TokenEstimate.messageBudget($1) }
        let reclaimed = max(0, before - after)
        guard reclaimed >= config.proactivePruneMinReclaimTokens else { return unchanged }
        proactivePruneRearmTokens = after + max(reclaimed, config.proactivePruneTokens, config.proactivePruneMinReclaimTokens)
        let rewritten = zip(messages, pruned).filter { $0.content != $1.content }.map(\.0.seq)
        return PruneResult(messages: pruned, prunedCount: count, rewrittenSeqs: rewritten)
    }

    // MARK: The compaction pass

    private func compress(_ messages: [ConversationMessage], focusTopic: String?, force: Bool, memoryContext: String,
                          bypassCooldown: Bool, restateInflight: Bool) -> CompressionResult {
        if force {
            clearCooldown()
            structuralBackoffUntil = .distantPast
        }
        let minForCompress = protectHeadSize(messages) + 3 + 1
        if messages.count <= minForCompress {
            return structuralNoOp(messages, reason: "only \(messages.count) messages (need > \(minForCompress))")
        }
        let canonical = dropBlankEchoes(messages)
        let plan = planCompactionWindow(messages)
        var working = plan.working
        var prunedCount = plan.pruned
        let start = plan.start
        let end = plan.end
        if start >= end {
            return structuralNoOp(messages, reason: "compress_start (\(start)) >= compress_end (\(end)) - transcript fits within tail budget")
        }
        if config.tailMode == .lean {
            let (demoted, count) = ToolOutput.demoteStaleTailTools(working, tailStart: end, sessionID: sessionID(of: messages))
            working = demoted
            prunedCount += count
        }
        let scan = scanWindowHandoffs(working, start: start, end: end)
        if scan.turns.isEmpty {
            return structuralNoOp(messages, reason: "window \(start)-\(end) holds only already-summarized handoffs")
        }

        let size = sizeBudget(scan.turns)
        let preSkip = !force && (fallbackStreakSkip() || feasibilitySkip(scan.turns))
        var summary: String?
        if !preSkip {
            summary = generateSummary(turns: scan.turns, focusTopic: focusTopic ?? SummaryPrompt.autoFocusTopic(working),
                                      memoryContext: memoryContext, bypassCooldown: bypassCooldown,
                                      sessionID: sessionID(of: messages), size: size)
            if summary == nil, let failure = abortingFailure(force: force) {
                previousSummary = scan.previousSummaryBefore
                summaryHasUserTurn = scan.hasUserTurnBefore
                return unchanged(messages, outcome: .aborted(failure),
                                 warning: SummaryValidation.abortMessage(failure, preserved: end - start))
            }
        }
        let usedFallback = summary == nil
        var warning: String?
        if summary == nil {
            summary = fallbackSummary(turns: scan.turns, preSkip: preSkip, sessionID: sessionID(of: messages), size: size)
            warning = preSkip ? nil : "Summary generation failed — inserted a deterministic fallback summary for \(end - start) message(s)."
        }

        var compressed = assemble(working, start: start, end: end, scan: scan, summary: summary!)
        compressed = sanitizeToolPairs(compressed)
        if restateInflight {
            compressed = reappendInflightTask(compressed, inflight: findInflightUserTask(canonical))
        }
        let tokensBefore = TokenEstimate.messages(messages)
        if Self.grows(TokenEstimate.messages(compressed), from: tokensBefore) {
            compressed = Self.salvageGrown(compressed, budget: tokensBefore) ?? compressed
        }
        if Self.grows(TokenEstimate.messages(compressed), from: tokensBefore) {
            previousSummary = scan.previousSummaryBefore
            summaryHasUserTurn = scan.hasUserTurnBefore
            ineffectiveCompressionCount += 1
            return unchanged(messages, outcome: .noOp("compaction would grow the transcript"))
        }
        compressionCount += 1
        recordCompletedCompaction(usedFallback: usedFallback, preSkip: preSkip)
        return makeResult(input: messages, output: compressed, outcome: .compacted, prunedCount: prunedCount,
                          summarized: scan.turns.count, usedFallback: usedFallback, warning: warning)
    }

    /// How big the summary may be. Hermes's target (summary budget plus the session-log budget) and section budgets
    /// apply unchanged unless that target is more than half the trigger (triggers under about 28K tokens), where one
    /// compaction could not get back under it: then the target is half the trigger and the sections shrink to fit.
    /// The model's reply is always cut at twice the target, a guard Hermes does not have.
    struct SizeBudget {
        var target: Int
        var bodyChars: Int
        var constrained: Bool

        var lean: SummaryPrompt.LeanBudget {
            constrained ? SummaryPrompt.LeanBudget(summaryCapChars: bodyChars) : SummaryPrompt.LeanBudget()
        }
    }

    private func sizeBudget(_ turns: [ConversationMessage]) -> SizeBudget {
        let budget = SummaryPrompt.summaryBudget(turns: turns, maxSummaryTokens: config.maxSummaryTokens)
        let hermes = budget + (config.tailMode == .lean ? CompressionText.leanSessionLogBudgetTokens : 0)
        let ceiling = max(Self.minSummaryTargetTokens, thresholdTokens / 2)
        let target = min(hermes, ceiling)
        return SizeBudget(target: target, bodyChars: target * TokenEstimate.charsPerToken * 2, constrained: hermes > ceiling)
    }

    /// Hermes's one mechanical pass on a candidate that grew (context_compressor.py:566): clear all but the newest two
    /// tool results and cap an oversized summary row. The unanswered tool round is output the model has not read yet,
    /// so it is never cleared, as the earlier pruning pass spares it. Returns the candidate only when it is now
    /// strictly smaller.
    static func salvageGrown(_ candidate: [ConversationMessage], budget: Int) -> [ConversationMessage]? {
        var out = candidate
        let keep = Set(out.indices.filter { out[$0].role == .tool }.suffix(2)).union(TranscriptShape.pendingToolRound(out))
        for index in out.indices {
            if out[index].role == .tool && !keep.contains(index) && out[index].content.scalarCount > ToolOutput.pruneMinChars {
                out[index].content = CompressionText.prunedToolPlaceholder
            }
            if out[index].isSummaryMessage && ConversationMessage.handoffKind(out[index].content) == .standalone
                && out[index].content.scalarCount > salvageSummaryMaxChars {
                out[index].content = CompressionText.elide(out[index].content, limit: salvageSummaryMaxChars)
                    + "\n\n" + CompressionText.summaryEndMarker
            }
        }
        guard out.contains(where: { $0.role == .user }), TokenEstimate.messages(out) < budget else { return nil }
        return out
    }

    static let salvageSummaryMaxChars = 8_000

    /// Hermes refuses only a strictly larger result (conversation_compression.py:3128); a break-even one commits.
    static func grows(_ after: Int, from before: Int) -> Bool { after > before }

    private func planCompactionWindow(_ messages: [ConversationMessage])
        -> (working: [ConversationMessage], pruned: Int, start: Int, end: Int) {
        let (pruned, count) = pruneOldToolResults(messages, protectTailCount: config.protectLastN,
                                                  protectTailTokens: config.tailTokenBudget, minPruneChars: ToolOutput.pruneMinChars)
        let working = dropBlankEchoes(pruned)
        let start = TranscriptShape.alignForward(working, protectHeadSize(working))
        var end = findTailCut(working, headEnd: start)
        let latestUser = TranscriptShape.lastRealUserIndex(working, from: 0)
        if end == latestUser {
            var bridge = latestUser - 1
            let bridgeRole = bridge >= 0 ? working[bridge].role : nil
            if bridgeRole == .tool {
                bridge = TranscriptShape.alignBackward(working, latestUser)
            } else if bridgeRole != .assistant {
                bridge = -1
            }
            if bridge >= progressFloor(working, headEnd: start, cut: bridge) { end = bridge }
        }
        return (working, count, start, end)
    }

    private func structuralNoOp(_ messages: [ConversationMessage], reason: String) -> CompressionResult {
        structuralBackoffUntil = now().addingTimeInterval(Self.structuralNoOpBackoffSeconds)
        structuralNoOpLength = messages.count
        return unchanged(messages, outcome: .noOp(reason))
    }

    // MARK: Head protection

    private func effectiveProtectFirstN(_ messages: [ConversationMessage]) -> Int {
        if compressionCount >= 1 || previousSummary != nil { return 0 }
        if config.protectFirstN > 0 && !messages.isEmpty {
            let probeStart = messages[0].role == .system ? 1 : 0
            let probeEnd = min(messages.count, probeStart + config.protectFirstN + Self.restartHandoffProbeExtraMessages)
            if probeStart < probeEnd && messages[probeStart..<probeEnd].contains(where: \.isSummaryMessage) { return 0 }
        }
        return config.protectFirstN
    }

    func protectHeadSize(_ messages: [ConversationMessage]) -> Int {
        (messages.first?.role == .system ? 1 : 0) + effectiveProtectFirstN(messages)
    }

    // MARK: Tail protection

    private func walkTailBudget(_ messages: [ConversationMessage], headEnd: Int, ceiling: Int, minTail: Int,
                                cutAtBreak: Bool, price: (Int) -> Int) -> (cut: Int, accumulated: Int) {
        let count = messages.count
        var accumulated = 0
        var cut = count
        var index = count - 1
        while index >= max(0, headEnd) {
            let tokens = price(index)
            if accumulated + tokens > ceiling && count - index >= minTail {
                return (cutAtBreak ? index : cut, accumulated)
            }
            accumulated += tokens
            cut = index
            index -= 1
        }
        return (cut, accumulated)
    }

    private func rowPrice(_ messages: [ConversationMessage]) -> (Int) -> Int {
        { TokenEstimate.messageBudget(messages[$0]) }
    }

    /// Rows at or before the newest in-window handoff cost nothing: the scan folds them into the summary anyway.
    private func tailCutPrice(_ messages: [ConversationMessage], headEnd: Int) -> (Int) -> Int {
        let price = rowPrice(messages)
        guard headEnd < messages.count,
              let newest = (max(0, headEnd)..<messages.count).reversed().first(where: { messages[$0].isSummaryMessage }) else {
            return price
        }
        return { $0 <= newest ? 0 : price($0) }
    }

    /// Lowest cut that leaves the summary window a row the handoff scan keeps.
    private func progressFloor(_ messages: [ConversationMessage], headEnd: Int, cut: Int) -> Int {
        let floor = headEnd + 1
        guard headEnd < messages.count,
              let newest = (max(0, headEnd)..<messages.count).reversed().first(where: { messages[$0].isSummaryMessage }) else {
            return floor
        }
        let upper = max(cut, newest + 1)
        if headEnd < upper && !messages[headEnd..<min(upper, messages.count)].allSatisfy(\.isSummaryMessage) { return floor }
        let raised = TranscriptShape.alignForward(messages, newest + 2)
        let lastUser = TranscriptShape.lastRealUserIndex(messages, from: newest + 1)
        return raised >= messages.count || (lastUser >= 0 && lastUser < raised) ? floor : raised
    }

    private func softCeiling(_ budget: Int) -> Int {
        var ceiling = Int(Double(budget) * 1.5)
        if config.contextLength > 0 {
            ceiling = min(ceiling, Int(Double(config.contextLength) * CompressorConfig.tailMaxContextFraction))
        }
        return max(ceiling, budget)
    }

    /// Walk back from the end accumulating tokens until the budget; return the first protected index. Keeps the
    /// latest real user turn and the latest assistant reply in the tail, and never splits a tool group.
    func findTailCut(_ messages: [ConversationMessage], headEnd: Int, budget: Int? = nil) -> Int {
        let budget = budget ?? config.tailTokenBudget
        let count = messages.count
        let availableTail = max(0, count - headEnd - 1)
        let minTailFloor = max(3, min(config.protectLastN, ToolOutput.maxTailMessageFloor))
        let compressibleTailCap = max(3, availableTail - 2)
        let minTail = availableTail > 1 ? min(minTailFloor, compressibleTailCap, availableTail) : 0
        let soft = softCeiling(budget)
        let walkFloor = soft >= minTail * 10 ? 0 : minTail
        let price = tailCutPrice(messages, headEnd: headEnd)
        var (cut, accumulated) = walkTailBudget(messages, headEnd: headEnd, ceiling: soft, minTail: walkFloor,
                                                cutAtBreak: false, price: price)
        if cut <= headEnd && accumulated > 0 && accumulated <= soft {
            cut = walkTailBudget(messages, headEnd: headEnd, ceiling: budget, minTail: minTail, cutAtBreak: true, price: price).cut
        }
        let fallbackCut = count - minTail
        cut = min(cut, count - max(walkFloor, 1))
        if cut <= headEnd { cut = max(fallbackCut, headEnd + 1) }
        cut = TranscriptShape.alignBackward(messages, cut)

        let lastUser = TranscriptShape.lastRealUserIndex(messages, from: headEnd)
        let userAnchored = ensureLastUserInTail(messages, cut: cut, headEnd: headEnd)
        var splitOversizedTurn = false
        if userAnchored < cut
            && TokenEstimate.messageBudget(messages[lastUser]) <= soft
            && (lastUser..<cut).contains(where: { messages[$0].isToolCall })
            && walkTailBudget(messages, headEnd: userAnchored, ceiling: soft, minTail: 0, cutAtBreak: false,
                              price: rowPrice(messages)).cut > userAnchored {
            splitOversizedTurn = true
        } else {
            cut = userAnchored
        }
        if !splitOversizedTurn {
            let assistantAnchored = ensureLastAssistantInTail(messages, cut: cut, headEnd: headEnd)
            let price = rowPrice(messages)
            let overCeiling = assistantAnchored < cut
                && (assistantAnchored..<cut).contains(where: { messages[$0].isToolCall })
                && (assistantAnchored..<cut).reduce(0, { $0 + price($1) }) > soft
            if !overCeiling { cut = assistantAnchored }
        }
        if config.minTailUserMessages > 1 {
            cut = ensureLastNUsersInTail(messages, cut: cut, headEnd: headEnd, n: config.minTailUserMessages)
        }
        return min(count, TranscriptShape.alignForward(messages, max(cut, progressFloor(messages, headEnd: headEnd, cut: cut))))
    }

    private func ensureLastUserInTail(_ messages: [ConversationMessage], cut: Int, headEnd: Int) -> Int {
        let lastUser = TranscriptShape.lastRealUserIndex(messages, from: headEnd)
        if lastUser < 0 || lastUser >= cut { return cut }
        let adjusted = max(lastUser, headEnd + 1)
        if adjusted > lastUser {
            var pairEnd = lastUser + 1
            if pairEnd < messages.count && messages[pairEnd].role == .assistant {
                pairEnd = TranscriptShape.alignForward(messages, pairEnd + 1)
            }
            return max(pairEnd, headEnd + 1)
        }
        return adjusted
    }

    private func ensureLastAssistantInTail(_ messages: [ConversationMessage], cut: Int, headEnd: Int) -> Int {
        let lastAssistant = TranscriptShape.lastAssistantIndex(messages, from: headEnd)
        if lastAssistant < 0 || lastAssistant >= cut { return cut }
        return max(TranscriptShape.alignBackward(messages, lastAssistant), headEnd + 1)
    }

    private func ensureLastNUsersInTail(_ messages: [ConversationMessage], cut: Int, headEnd: Int, n: Int) -> Int {
        let users = TranscriptShape.realUserIndicesDescending(messages, from: headEnd)
        guard !users.isEmpty else { return cut }
        let nth = users[min(n, users.count) - 1]
        return nth >= cut ? cut : max(nth, headEnd + 1)
    }

    // MARK: Tool-output pruning

    func pruneOldToolResults(_ messages: [ConversationMessage], protectTailCount: Int, protectTailTokens: Int?,
                             minPruneChars: Int) -> ([ConversationMessage], Int) {
        guard !messages.isEmpty else { return (messages, 0) }
        var result = messages
        let calls = ToolOutput.callsByID(result)
        var boundary = pruneBoundary(result, protectTailCount: protectTailCount, protectTailTokens: protectTailTokens)
        let spared = sparedPendingToolRound(result)
        if !spared.isEmpty { boundary = min(boundary, spared.lowerBound) }
        var pruned = ToolOutput.dedupe(&result)
        for index in 0..<max(0, min(boundary, result.count)) where ToolOutput.demote(
            &result, at: index, calls: calls, minPruneChars: minPruneChars, terminalTools: config.terminalToolNames) {
            pruned += 1
        }
        if let tokens = protectTailTokens, tokens > 0 {
            pruned += pressureDemoteTail(&result, boundary: boundary, budget: tokens, calls: calls,
                                         minPruneChars: minPruneChars, spared: spared)
        }
        return (result, pruned)
    }

    private func pruneBoundary(_ messages: [ConversationMessage], protectTailCount: Int, protectTailTokens: Int?) -> Int {
        guard let tokens = protectTailTokens, tokens > 0 else { return messages.count - protectTailCount }
        let minProtect = min(protectTailCount, messages.count, ToolOutput.maxTailMessageFloor)
        let boundary = walkTailBudget(messages, headEnd: 0, ceiling: tokens, minTail: minProtect, cutAtBreak: true,
                                      price: rowPrice(messages)).cut
        return min(boundary, messages.count - minProtect)
    }

    /// The unanswered tool round (with its calls), spared from pruning unless it alone exceeds 20% of the input window.
    private func sparedPendingToolRound(_ messages: [ConversationMessage]) -> Range<Int> {
        var pending = TranscriptShape.pendingToolRound(messages)
        guard !pending.isEmpty else { return pending }
        var start = pending.lowerBound
        while start > 0 && messages[start - 1].isToolCall { start -= 1 }
        pending = start..<pending.upperBound
        let hardShare = Int(Double(config.effectiveInputWindow) * CompressorConfig.tailMaxContextFraction)
        let size = pending.reduce(0) { $0 + TokenEstimate.messageBudget(messages[$1]) }
        return size <= hardShare ? pending : 0..<0
    }

    /// Demote tool bodies inside the protected tail when the tail alone exceeds its soft ceiling.
    private func pressureDemoteTail(_ result: inout [ConversationMessage], boundary: Int, budget: Int,
                                    calls: [String: ToolOutput.CallInfo], minPruneChars: Int, spared: Range<Int>) -> Int {
        let soft = softCeiling(budget)
        let demoteEnd = result.count - min(ToolOutput.pressureKeepRecentMessages, result.count)
        let start = max(0, boundary)
        func regionTokens(_ rows: [ConversationMessage]) -> Int {
            rows[min(start, rows.count)...].reduce(0) { $0 + TokenEstimate.messageBudget($1) }
        }
        if demoteEnd <= boundary || regionTokens(result) <= soft { return 0 }
        var demoted = 0
        let tools = config.terminalToolNames
        func shrink(_ index: Int) {
            if spared.contains(index) { return }
            if ToolOutput.demote(&result, at: index, calls: calls, minPruneChars: minPruneChars, terminalTools: tools) {
                demoted += 1
            }
        }
        for index in start..<demoteEnd {
            shrink(index)
            if regionTokens(result) <= soft { break }
        }
        if regionTokens(result) > soft {
            let lastTool = result.indices.reversed().first(where: { result[$0].role == .tool })
            for index in start..<result.count where index != lastTool { shrink(index) }
            if let lastTool, !spared.contains(lastTool), lastTool >= boundary, regionTokens(result) > soft,
               ToolOutput.demote(&result, at: lastTool, calls: calls, minPruneChars: minPruneChars, terminalTools: tools) {
                demoted += 1
            }
        }
        return demoted
    }

    private func dropBlankEchoes(_ messages: [ConversationMessage]) -> [ConversationMessage] {
        let user = TranscriptShape.lastRealUserIndex(messages, from: 0)
        guard user >= 0 else { return messages }
        var index = user + 1
        while index < messages.count && messages[index].isBlankUserTurn { index += 1 }
        if index == user + 1 || index >= messages.count || messages[index].role != .assistant { return messages }
        return Array(messages[...user]) + Array(messages[index...])
    }

    // MARK: Handoff scan

    private struct HandoffScan {
        var turns: [ConversationMessage]
        var tailStart: Int
        var previousSummaryBefore: String?
        var hasUserTurnBefore: Bool?
    }

    /// Rehydrate the previous summary from in-transcript summary rows and drop them from the summarizer window; a
    /// merged carrier stays in the window as its prior content.
    private func scanWindowHandoffs(_ messages: [ConversationMessage], start: Int, end: Int) -> HandoffScan {
        var scan = HandoffScan(turns: Array(messages[start..<end]), tailStart: end,
                               previousSummaryBefore: previousSummary, hasUserTurnBefore: summaryHasUserTurn)
        let searchStart = messages.first?.role == .system ? 1 : 0
        let hits = (searchStart..<messages.count).filter { messages[$0].isSummaryMessage }
            .map { ($0, SummaryPrompt.stripSummaryPrefix(messages[$0].content)) }
        let realUserPresent = messages.contains { $0.role == .user && !$0.isSyntheticCompressionUserTurn }
        guard let (summaryIndex, body) = hits.last else {
            previousSummary = nil
            summaryHasUserTurn = realUserPresent
            return scan
        }
        if previousSummary?.isEmpty ?? true {
            let joined = hits.map(\.1).filter { !$0.isEmpty }.joined(separator: "\n\n")
            previousSummary = joined.isEmpty ? previousSummary : joined
        }
        if realUserPresent {
            summaryHasUserTurn = true
        } else if summaryHasUserTurn == nil {
            summaryHasUserTurn = !body.contains(CompressionText.noUserTaskSentinel)
        }
        let window = summaryIndex >= start ? messages[start...summaryIndex].compactMap(\.unwrappedHandoff) : []
        let after = max(summaryIndex + 1, start)
        scan.turns = window + (after < end ? Array(messages[after..<end]) : [])
        if summaryIndex >= end { scan.tailStart = summaryIndex + 1 }
        return scan
    }

    // MARK: Summary generation

    private func generateSummary(turns: [ConversationMessage], focusTopic: String?, memoryContext: String,
                                 bypassCooldown: Bool, sessionID: String, size: SizeBudget) -> String? {
        if now() < cooldownUntil && !bypassCooldown { return nil }
        let focus = focusTopic.map(Redaction.redact)
        if let previous = previousSummary { previousSummary = Redaction.redact(previous) }
        let lean = config.tailMode == .lean
        let records = SummaryPrompt.serializeRecords(turns)
        let content = lean ? SummaryPrompt.sampleRecords(records) : SummaryPrompt.boundSummaryInput(records.joined(separator: "\n\n"))
        let hasUserTurn = summaryHasUserTurn ?? turns.contains { $0.role == .user && !$0.isSyntheticCompressionUserTurn }
        let previousForPrompt = previousSummary.map(SummaryPrompt.stripLeanSections)
        let prompt = SummaryPrompt.buildPrompt(content: content, target: size.target, focusTopic: focus, memoryContext: memoryContext,
                                               hasUserTurn: hasUserTurn, previousSummary: previousForPrompt, lean: lean,
                                               today: Self.dateString(now()))
        let request = SummaryRequest(prompt: prompt, targetTokens: size.target, focusTopic: focus,
                                     isIterativeUpdate: !(previousForPrompt?.isEmpty ?? true), turnCount: turns.count)
        do {
            let raw = CompressionText.stripThinkBlocks(try summarizer(request)).trimmed
            if raw.isEmpty { throw SummaryError.emptyContent }
            if SummaryValidation.isRefusal(raw) { throw SummaryError.refusal }
            var summary = CompressionText.neutralizingMarkers(SummaryPrompt.stripSummaryPrefix(Redaction.redact(raw))).trimmed
            if summary.isEmpty { throw SummaryError.emptyContent }
            summary = SummaryPrompt.groundHistoricalTask(summary, turns: turns)
            summary = finish(summary, turns: turns, sessionID: sessionID, size: size)
            try SummaryPrompt.validateUserProvenance(summary, hasUserTurn: hasUserTurn)
            recordSummarySuccess(summary)
            return SummaryPrompt.withSummaryPrefix(summary)
        } catch {
            recordSummaryFailure(error)
            return nil
        }
    }

    /// Cap the summary to its budget and rebuild the deterministic sections (lean mode), so neither a runaway reply nor
    /// a copied old section can replace them.
    private func finish(_ body: String, turns: [ConversationMessage], sessionID: String, size: SizeBudget) -> String {
        let body = SummaryPrompt.stripLeanSections(body)
        guard config.tailMode == .lean else { return CompressionText.elide(body, limit: size.bodyChars) }
        let extras = leanExtras(turns: turns, sessionID: sessionID, size: size)
        return CompressionText.elide(body, limit: bodyLimit(size, extras: extras)) + extras
    }

    /// Unconstrained, the body gets its full cap beside Hermes-sized sections; constrained, they share the cap.
    private func bodyLimit(_ size: SizeBudget, extras: String) -> Int {
        size.constrained ? max(size.bodyChars / 4, size.bodyChars - extras.scalarCount) : size.bodyChars
    }

    private func leanExtras(turns: [ConversationMessage], sessionID: String, size: SizeBudget) -> String {
        SummaryPrompt.leanSections(turns: turns, sessionID: sessionID, previous: previousSummary, budget: size.lean)
    }

    private func recordSummarySuccess(_ summary: String) {
        previousSummary = summary
        clearCooldown()
        terminalFailures = []
        consecutiveOverloadAborts = 0
        consecutiveEmptyAborts = 0
    }

    /// Classify a failure and arm its cooldown: timeouts and truncations climb 60 → 300 → 900 s on separate counters.
    private func recordSummaryFailure(_ error: Error) {
        let kind = SummaryValidation.classify(error)
        if kind.noProvider {
            armCooldown(seconds: SummaryValidation.noProviderCooldownSeconds, error: "no auxiliary LLM provider configured")
            return
        }
        if kind.accessOrQuota { terminalFailures.insert(.accessOrQuota) }
        let seconds: Int
        if kind.timeout {
            consecutiveTimeoutFailures += 1
            seconds = SummaryValidation.ladderRung(consecutiveTimeoutFailures)
        } else if kind.truncated {
            consecutiveTruncationFailures += 1
            seconds = SummaryValidation.ladderRung(consecutiveTruncationFailures)
        } else {
            seconds = kind.jsonDecode || kind.streamingClosed || kind.emptyContent ? 30 : 60
        }
        armCooldown(seconds: seconds, error: SummaryValidation.shortErrorText(error))
        if kind.streamingClosed {
            terminalFailures.insert(.network)
        } else if kind.truncated {
            terminalFailures.insert(.truncated)
        } else if kind.emptyContent {
            consecutiveEmptyAborts += 1
            if consecutiveEmptyAborts < SummaryValidation.consecutiveEmptyAbortEscalation {
                terminalFailures.insert(.emptyContent)
            } else {
                terminalFailures = []
            }
        } else if kind.overloaded && !kind.accessOrQuota {
            consecutiveOverloadAborts += 1
            if consecutiveOverloadAborts < SummaryValidation.consecutiveOverloadAbortEscalation {
                terminalFailures.insert(.overloaded)
            } else {
                overloadDegraded = true
                terminalFailures = []
            }
        }
    }

    private func armCooldown(seconds: Int, error: String) {
        cooldownUntil = max(cooldownUntil, now().addingTimeInterval(TimeInterval(seconds)))
        lastSummaryError = error
    }

    private func clearCooldown() {
        cooldownUntil = .distantPast
        lastSummaryError = nil
        consecutiveTimeoutFailures = 0
        consecutiveTruncationFailures = 0
    }

    /// Terminal failures always abort (context is kept); otherwise `abortOnSummaryFailure` decides. A manual compress
    /// gets past an empty or refused reply with the deterministic fallback.
    private func abortingFailure(force: Bool) -> SummaryFailureClass? {
        if let terminal = Self.terminalPrecedence.first(where: terminalFailures.contains) {
            return force && terminal == .emptyContent ? nil : terminal
        }
        return config.abortOnSummaryFailure ? .aborted : nil
    }

    private func fallbackSummary(turns: [ConversationMessage], preSkip: Bool, sessionID: String, size: SizeBudget) -> String {
        let extras = config.tailMode == .lean ? leanExtras(turns: turns, sessionID: sessionID, size: size) : ""
        let limit = min(SummaryPrompt.fallbackSummaryMaxChars, bodyLimit(size, extras: extras))
        let summary = CompressionText.neutralizingMarkers(SummaryPrompt.staticFallback(turns: turns, reason: preSkip ? nil : lastSummaryError,
                                                   previousSummary: previousSummary, terminalTools: config.terminalToolNames,
                                                   maxChars: limit)) + extras
        previousSummary = SummaryPrompt.stripSummaryPrefix(summary)
        return summary
    }

    /// Two fallback summaries in a row bench the summarizer; it is probed again once per recovery window.
    private func fallbackStreakSkip() -> Bool {
        guard fallbackCompressionStreak >= 2 else {
            fallbackProbeAt = nil
            return false
        }
        let current = now()
        if let probe = fallbackProbeAt, current >= probe {
            fallbackProbeAt = nil
            return false
        }
        if fallbackProbeAt == nil || fallbackProbeAt!.timeIntervalSince(current) > Self.antiThrashRecoverySeconds {
            fallbackProbeAt = current.addingTimeInterval(Self.antiThrashRecoverySeconds)
        }
        return true
    }

    /// After an ineffective compaction, a middle under 10% of the trigger is dropped without a summary call.
    private func feasibilitySkip(_ turns: [ConversationMessage]) -> Bool {
        guard ineffectiveCompressionCount >= 1 else { return false }
        return TokenEstimate.messages(turns) < Int(Double(thresholdTokens) * Self.feasibilitySkipMiddleFraction)
    }

    private func recordCompletedCompaction(usedFallback: Bool, preSkip: Bool) {
        structuralBackoffUntil = .distantPast
        verifyCompactionClearedThreshold = true
        proactivePruneRearmTokens = 0
        if preSkip { return }
        fallbackCompressionStreak = usedFallback ? fallbackCompressionStreak + 1 : 0
        consecutiveOverloadAborts = 0
        consecutiveEmptyAborts = 0
        overloadDegraded = false
    }

    // MARK: Assembly

    /// Head, summary, tail. Old handoffs are dropped (standalone) or unwrapped (merged). When the summary row would
    /// repeat a role on both sides, it is folded into the first text row of the tail instead, as Hermes does
    /// (context_compressor.py:5290-5345): that row is carried with the summary added.
    private func assemble(_ messages: [ConversationMessage], start: Int, end: Int, scan: HandoffScan,
                          summary: String) -> [ConversationMessage] {
        var head: [ConversationMessage] = []
        for index in 0..<start {
            var message = messages[index]
            if index == 0 && message.role == .system && !message.content.contains(CompressionText.compressionNote) {
                message.content += (message.content.isEmpty ? "" : "\n\n") + CompressionText.compressionNote
            }
            if let kept = message.unwrappedHandoff { head.append(kept) }
        }
        let tailStart = max(end, scan.tailStart)
        var tail = tailStart < messages.count ? messages[tailStart...].compactMap(\.unwrappedHandoff) : []

        let lastHeadRole: MessageRole? = head.isEmpty ? .user : TranscriptShape.lastTemplateVisibleRole(head)
        let firstTailRole = tail.lazy.compactMap(\.templateVisibleRole).first
        let forceUserLeading = start == 0 || lastHeadRole == .system
            || !(head + tail).contains { $0.role == .user && !$0.content.trimmed.isEmpty }
        var role: MessageRole = lastHeadRole == nil || lastHeadRole == .assistant || forceUserLeading ? .user : .assistant
        var merge = false
        if let firstTailRole, role == firstTailRole {
            let flipped: MessageRole = role == .user ? .assistant : .user
            if flipped != lastHeadRole && lastHeadRole != nil && !forceUserLeading {
                role = flipped
            } else {
                merge = true
            }
        }
        if merge, let carrierIndex = tail.firstIndex(where: { $0.templateVisibleRole != nil }) {
            var carrier = tail[carrierIndex]
            if forceUserLeading && role == .user {
                carrier.content = summary + "\n\n" + CompressionText.summaryEndMarker + "\n\n" + carrier.content
            } else {
                carrier.content = CompressionText.mergedPriorContextHeader + "\n" + carrier.content + "\n\n"
                    + CompressionText.mergedSummaryDelimiter + "\n\n" + summary + "\n\n" + CompressionText.summaryEndMarker
            }
            carrier.isSummary = true
            tail[carrierIndex] = carrier
            return head + tail
        }
        let first = scan.turns.first ?? messages[start]
        let summaryMessage = ConversationMessage(sessionID: first.sessionID, seq: Self.newRowSeq, role: role,
                                                 content: summary + "\n\n" + CompressionText.summaryEndMarker,
                                                 timestamp: now(), isSummary: true)
        return head + [summaryMessage] + tail
    }

    /// Drop tool results whose call is gone. A call whose result is gone was interrupted (the user or the assistant
    /// moved on): it is carried as Hermes's plain-text stub (context_compressor.py:4528-4542), no longer a call. Calls
    /// still in flight stay exactly as they are.
    private func sanitizeToolPairs(_ messages: [ConversationMessage]) -> [ConversationMessage] {
        var seenCalls: Set<String> = []
        var result: [ConversationMessage] = []
        for message in messages {
            if message.isToolCall, let id = message.toolCallID { seenCalls.insert(id) }
            if message.role == .tool && !seenCalls.contains(message.toolCallID ?? "") { continue }
            result.append(message)
        }
        let resultIDs = Set(result.filter { $0.role == .tool }.compactMap(\.toolCallID))
        let inflight = Self.inflightCalls(result)
        var stubs: Set<Int> = []
        for index in result.indices where result[index].isToolCall && !inflight.contains(index)
            && !resultIDs.contains(result[index].toolCallID ?? "") {
            result[index].content = CompressionText.toolCallRemoved
            result[index].toolCallID = nil
            result[index].toolName = nil
            stubs.insert(index)
        }
        // A stub inside a partly answered parallel group would sit between a call and its result; that call is
        // archived instead so the group stays whole.
        let splitting = stubs.filter { !TranscriptShape.straddlingCalls(result, cut: $0).isEmpty }
        return result.enumerated().filter { !splitting.contains($0.offset) }.map(\.element)
    }

    /// Calls with no user or assistant text after them: still waiting for output, so never rewritten or archived.
    private static func inflightCalls(_ messages: [ConversationMessage]) -> Range<Int> {
        let lastText = messages.lastIndex { $0.role != .tool && !$0.isToolCall } ?? -1
        guard let first = messages.indices.first(where: { $0 > lastText && messages[$0].isToolCall }) else { return 0..<0 }
        return first..<messages.count
    }

    /// The user turn still awaiting an answer: the transcript does not end with a text reply after it.
    private func findInflightUserTask(_ messages: [ConversationMessage]) -> ConversationMessage? {
        guard let userIndex = messages.indices.reversed().first(where: {
            messages[$0].isRealUserTurn || messages[$0].hasMergedInflightReplay
        }) else { return nil }
        for message in messages[(userIndex + 1)...].reversed() {
            if message.role != .assistant || message.isToolCall { break }
            if !message.content.trimmed.isEmpty { return nil }
        }
        return messages[userIndex]
    }

    /// When the only unfinished request sits before the summary, restate it (redacted) after the handoff so the
    /// summary's "respond only to messages after this" rule still points at it. The head copy is then not carried.
    private func reappendInflightTask(_ compressed: [ConversationMessage], inflight: ConversationMessage?) -> [ConversationMessage] {
        guard let inflight, !compressed.isEmpty,
              var carrierIndex = compressed.indices.reversed().first(where: { compressed[$0].isSummaryMessage }) else {
            return compressed
        }
        if compressed[(carrierIndex + 1)...].contains(where: \.isRealUserTurn) { return compressed }
        guard let marker = compressed[carrierIndex].content.range(of: CompressionText.summaryEndMarker),
              compressed[carrierIndex].content[marker.upperBound...].trimmed.isEmpty else { return compressed }
        let taskText = Redaction.redact(inflight.restatedRequestText)
        guard !taskText.isEmpty else { return compressed }

        var result = compressed
        let carrierIsNew = result[carrierIndex].seq <= Self.newRowSeq
        if carrierIsNew && !inflight.isSummaryMessage && inflight.seq > Self.newRowSeq {
            let before = result.count
            result.removeAll { $0.role == .user && !$0.isSummaryMessage && $0.seq == inflight.seq }
            carrierIndex = result.firstIndex(where: \.isSummary) ?? carrierIndex
            if result.count != before {
                if let first = result.firstIndex(where: { $0.role != .system }), result[first].role != .user, first != carrierIndex {
                    let carrier = result.remove(at: carrierIndex)
                    result.insert(carrier, at: first)
                    carrierIndex = first
                }
                if result[carrierIndex].role == .assistant
                    && TranscriptShape.lastTemplateVisibleRole(Array(result[..<carrierIndex])) != .user {
                    result[carrierIndex].role = .user
                }
            }
        }
        if TranscriptShape.lastTemplateVisibleRole(result) == .user {
            result[carrierIndex].content += "\n\n" + CompressionText.inflightTaskReplayHeader + "\n" + taskText
            return result
        }
        let replay = ConversationMessage(sessionID: inflight.sessionID, seq: Self.newRowSeq, role: .user,
                                         content: CompressionText.inflightTaskReplayHeader + "\n" + taskText, timestamp: now())
        let pendingCalls = Self.inflightCalls(result)
        result.insert(replay, at: pendingCalls.isEmpty ? result.count : pendingCalls.lowerBound)
        return result
    }

    // MARK: Results

    private func sessionID(of messages: [ConversationMessage]) -> String {
        messages.first(where: { !$0.sessionID.isEmpty })?.sessionID ?? ""
    }

    private func unchanged(_ messages: [ConversationMessage], outcome: CompressionOutcome, warning: String? = nil) -> CompressionResult {
        let tokens = TokenEstimate.messages(messages)
        return CompressionResult(outcome: outcome, messages: messages, summary: nil, newMessages: [], archivedSeqs: [],
                                 rewrittenSeqs: [], stats: CompressionStats(tokensBefore: tokens, tokensAfter: tokens,
                                                                           prunedToolResults: 0, summarizedMessages: 0,
                                                                           usedFallbackSummary: false),
                                 warning: warning)
    }

    private func makeResult(input: [ConversationMessage], output: [ConversationMessage], outcome: CompressionOutcome,
                            prunedCount: Int, summarized: Int, usedFallback: Bool, warning: String?) -> CompressionResult {
        let inputBySeq = Dictionary(input.filter { $0.seq > Self.newRowSeq }.map { ($0.seq, $0) }, uniquingKeysWith: { first, _ in first })
        let carried = output.filter { $0.seq > Self.newRowSeq }
        let carriedSeqs = Set(carried.map(\.seq))
        let archived = Set(inputBySeq.keys).subtracting(carriedSeqs).sorted()
        let rewritten = carried.filter { row in
            inputBySeq[row.seq].map { $0.content != row.content || $0.role != row.role || $0.isSummary != row.isSummary } ?? false
        }.map(\.seq)
        return CompressionResult(
            outcome: outcome, messages: output, summary: output.first(where: \.isSummary),
            newMessages: output.filter { $0.seq <= Self.newRowSeq }, archivedSeqs: archived, rewrittenSeqs: rewritten,
            stats: CompressionStats(tokensBefore: TokenEstimate.messages(input), tokensAfter: TokenEstimate.messages(output),
                                    prunedToolResults: prunedCount, summarizedMessages: summarized,
                                    usedFallbackSummary: usedFallback),
            warning: warning)
    }

    static func ranges(_ sorted: [Int]) -> [ClosedRange<Int>] {
        var out: [ClosedRange<Int>] = []
        for value in sorted {
            if let last = out.last, value == last.upperBound + 1 {
                out[out.count - 1] = last.lowerBound...value
            } else if out.last?.contains(value) != true {
                out.append(value...value)
            }
        }
        return out
    }

    static func dateString(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: date)
    }
}
