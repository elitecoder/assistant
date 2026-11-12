// Ported from Hermes Agent (MIT) agent/context_compressor.py (role, handoff and tool-group predicates).
import Foundation

extension ConversationMessage {
    /// An assistant row that runs a tool; its result is the `.tool` row with the same `toolCallID`.
    var isToolCall: Bool { role == .assistant && toolCallID != nil }

    /// A compaction handoff. The persisted `isSummary` flag is the source of truth: a row without it is never a
    /// handoff, whatever its text quotes. A flagged row counts only while its content still has a handoff layout
    /// (a user may edit the row the store flagged).
    var isSummaryMessage: Bool {
        isSummary && Self.handoffKind(content) != nil
    }

    enum HandoffKind { case standalone, folded }

    /// `folded` is a carried row that starts with the prior-context header and holds the summary after the
    /// delimiter; `standalone` starts with the handoff prefix (possibly followed by live text after the end marker).
    static func handoffKind(_ content: String) -> HandoffKind? {
        if content.hasPrefix(CompressionText.mergedPriorContextHeader + "\n"),
           content.contains(CompressionText.foldDelimiterBlock + CompressionText.summaryPrefix) {
            return .folded
        }
        return startsWithSummaryPrefix(content) ? .standalone : nil
    }

    static func startsWithSummaryPrefix(_ text: String) -> Bool {
        text.hasPrefix(CompressionText.summaryPrefix) || text.hasPrefix(CompressionText.legacySummaryPrefix)
    }

    /// The row without its summary, the exact inverse of folding: nil for a standalone handoff, the carrier's own
    /// text for a folded row, the live text after the end marker for a prepended one, and the row itself (flag
    /// cleared) for anything else.
    var unwrappedHandoff: ConversationMessage? {
        guard isSummaryMessage else {
            guard isSummary else { return self }
            var copy = self
            copy.isSummary = false
            return copy
        }
        var prior: String
        if Self.handoffKind(content) == .folded {
            let body = content.dropFirst(CompressionText.mergedPriorContextHeader.count + 1)
            guard let delimiter = body.range(of: CompressionText.foldDelimiterBlock, options: .backwards) else { return nil }
            prior = String(body[..<delimiter.lowerBound])
        } else if let marker = content.range(of: CompressionText.summaryEndMarker) {
            prior = String(content[marker.upperBound...])
            if prior.hasPrefix("\n\n") { prior.removeFirst(2) }
        } else {
            prior = ""
        }
        guard !prior.trimmed.isEmpty else { return nil }
        var copy = self
        copy.content = prior
        copy.isSummary = false
        return copy
    }

    /// Role as strict chat-template alternation counts it: tool rows and tool-call rows are exempt.
    var templateVisibleRole: MessageRole? {
        if role == .tool || isToolCall { return nil }
        return role
    }

    var isBlankUserTurn: Bool {
        role == .user && !isSummaryMessage && content.trimmed.isEmpty
    }

    /// Internal user-role scaffolding recognized by content, since stores drop metadata.
    var isSyntheticCompressionUserTurn: Bool {
        guard role == .user else { return false }
        if isSummaryMessage { return true }
        let text = content.trimmed
        return text == CompressionText.compressionContinuationUserContent
            || text == CompressionText.legacyCompressionContinuationUserContent
            || text == CompressionText.maxIterationsSummaryRequest
            || text.hasPrefix(CompressionText.backgroundProcessNotificationPrefix)
    }

    /// An actionable, human-authored user turn. The Mac chat's workspace context rows are app scaffolding, as Hermes
    /// treats its own injected turn context (agent/conversation_compression_scaffolding.py).
    var isRealUserTurn: Bool {
        role == .user && !isSummaryMessage && !isBlankUserTurn && !isSyntheticCompressionUserTurn && !TurnContext.isContext(self)
    }

    /// The user's request text, after the last in-flight replay header if an earlier compaction restated it.
    var restatedRequestText: String {
        var text = content.trimmed
        if let range = text.range(of: CompressionText.inflightTaskReplayHeader, options: .backwards) {
            text = String(text[range.upperBound...]).trimmed
        }
        return text
    }

    var hasMergedInflightReplay: Bool {
        guard isSummaryMessage, let marker = content.range(of: CompressionText.summaryEndMarker, options: .backwards) else {
            return false
        }
        let rest = content[marker.upperBound...].trimmingLeadingWhitespace()
        return rest.hasPrefix(CompressionText.inflightTaskReplayHeader)
            && !rest.dropFirst(CompressionText.inflightTaskReplayHeader.count).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    func withContent(_ content: String) -> ConversationMessage {
        var copy = self
        copy.content = content
        return copy
    }
}

/// Index helpers over an ordered transcript. Tool groups never split: a cut never separates a call from its result.
enum TranscriptShape {
    static func resultIndex(byCallID messages: [ConversationMessage]) -> [String: Int] {
        var map: [String: Int] = [:]
        for (index, message) in messages.enumerated() where message.role == .tool {
            if let id = message.toolCallID, map[id] == nil { map[id] = index }
        }
        return map
    }

    /// Call indices before `cut` whose result sits at or after it.
    static func straddlingCalls(_ messages: [ConversationMessage], cut: Int) -> [(call: Int, result: Int)] {
        guard cut > 0, cut < messages.count else { return [] }
        let results = resultIndex(byCallID: messages)
        var pairs: [(Int, Int)] = []
        for index in 0..<cut where messages[index].isToolCall {
            if let id = messages[index].toolCallID, let result = results[id], result >= cut {
                pairs.append((index, result))
            }
        }
        return pairs
    }

    /// Push a compress-start boundary forward past orphan tool results (and past any result whose call is before it).
    static func alignForward(_ messages: [ConversationMessage], _ start: Int) -> Int {
        var index = start
        while true {
            while index < messages.count && messages[index].role == .tool { index += 1 }
            let pairs = straddlingCalls(messages, cut: index)
            guard let last = pairs.map(\.result).max() else { return index }
            index = last + 1
        }
    }

    /// Pull a compress-end boundary back so the tool group just before it moves whole into the tail.
    static func alignBackward(_ messages: [ConversationMessage], _ end: Int) -> Int {
        if end <= 0 || end >= messages.count { return end }
        var cut = end
        if let check = (0..<cut).reversed().first(where: { messages[$0].role != .tool }), messages[check].isToolCall {
            var start = check
            while start > 0 && messages[start - 1].isToolCall { start -= 1 }
            cut = start
        }
        while let earliest = straddlingCalls(messages, cut: cut).map(\.call).min() {
            cut = earliest
        }
        return cut
    }

    /// The tool results the transcript ends with: a round the model has not answered yet.
    static func pendingToolRound(_ messages: [ConversationMessage]) -> Range<Int> {
        let end = messages.count
        var start = end
        while start > 0 && messages[start - 1].role == .tool { start -= 1 }
        return start..<end
    }

    static func lastRealUserIndex(_ messages: [ConversationMessage], from headEnd: Int) -> Int {
        guard headEnd < messages.count else { return -1 }
        return (max(0, headEnd)..<messages.count).reversed().first(where: { messages[$0].isRealUserTurn }) ?? -1
    }

    static func realUserIndicesDescending(_ messages: [ConversationMessage], from headEnd: Int) -> [Int] {
        guard headEnd < messages.count else { return [] }
        return (max(0, headEnd)..<messages.count).reversed().filter { messages[$0].isRealUserTurn }
    }

    /// Last text-bearing non-summary assistant reply at or after `headEnd`, else the last non-summary assistant row.
    static func lastAssistantIndex(_ messages: [ConversationMessage], from headEnd: Int) -> Int {
        guard headEnd < messages.count else { return -1 }
        var lastAny = -1
        for index in (max(0, headEnd)..<messages.count).reversed() {
            let message = messages[index]
            guard message.role == .assistant, !message.isSummaryMessage else { continue }
            if lastAny < 0 { lastAny = index }
            if !message.isToolCall && !message.content.trimmed.isEmpty { return index }
        }
        return lastAny
    }

    static func lastTemplateVisibleRole(_ messages: [ConversationMessage]) -> MessageRole? {
        messages.reversed().lazy.compactMap(\.templateVisibleRole).first
    }
}
