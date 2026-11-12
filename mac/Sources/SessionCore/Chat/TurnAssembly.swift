// Turn assembly for the Mac chat, after Hermes Agent (MIT, Nous Research): agent/conversation_loop.py,
// agent/turn_context.py, agent/system_prompt.py and agent/prompt_caching.py.
import Foundation

/// The app-written part of a turn: workspace updates and a session's recent conversation. It is stored as its own
/// user row just before the operator's message, so later turns see it as history. The marker starts with Hermes's
/// `[System:` scaffolding prefix, so a compaction summary never quotes it as the operator's words, and the
/// compressor never takes it for the operator's request. The spoken style is not stored: it rides in the current
/// turn's tail only, so it never shapes later typed answers.
public enum TurnContext {
    public static let marker = "[System: context from Assistant, not typed by the operator]"

    public static func row(fleet: String?, focus: (title: String, exchange: String)?) -> String? {
        var parts: [String] = []
        if let fleet { parts.append(fleet) }
        if let focus { parts.append("# Recent conversation in \(focus.title)\n\n\(focus.exchange)") }
        return parts.isEmpty ? nil : ([marker] + parts).joined(separator: "\n\n")
    }

    public static func isContext(_ message: ConversationMessage) -> Bool {
        message.role == .user && message.content.hasPrefix(marker)
    }
}

/// What the model has already been shown of each workspace, kept per chat session.
public struct FleetMark: Codable, Equatable {
    public var key: String
    public var title: String
    public var signature: String
}

/// The marks of the last fleet update the chat stored, and of its last full digest.
public struct FleetState: Codable, Equatable {
    public var sent: [FleetMark]
    public var full: [FleetMark]

    /// After storing an update with `marks`. A full digest (no state yet) is also the new base.
    public static func after(_ state: FleetState?, sent marks: [FleetMark]) -> FleetState {
        FleetState(sent: marks, full: state?.full ?? marks)
    }

    /// After a compaction the updates since the full digest may be summarized away. When the full digest itself is
    /// still live (Hermes keeps the head on a first compaction), the next update is measured against it; otherwise
    /// the state is dropped and the next turn sends the full digest again.
    public static func afterCompaction(_ state: FleetState?, active: [ConversationMessage]) -> FleetState? {
        guard let state, active.contains(where: FleetDelta.carriesFullDigest) else { return nil }
        return FleetState(sent: state.full, full: state.full)
    }
}

public enum FleetDelta {
    static func carriesFullDigest(_ message: ConversationMessage) -> Bool {
        message.role == .user && message.content.contains(TurnContext.marker + "\n\n# Workspaces (checked ")
    }

    /// The fleet text to send this turn and the marks to remember once it is stored. With no marks (a session's
    /// first turn, or a compaction archived the full digest) it is the full digest; after that only changed, new,
    /// and closed workspaces, or nil when nothing changed.
    public static func update(_ entries: [FleetEntry], since marks: [FleetMark]?, now: Date) -> (text: String?, marks: [FleetMark]) {
        let next = entries.map { FleetMark(key: $0.key, title: $0.title, signature: $0.signature) }
        guard let marks else {
            return (([Digest.header(now)] + entries.map(\.text)).joined(separator: "\n"), next)
        }
        let seen = Dictionary(marks.map { ($0.key, $0.signature) }, uniquingKeysWith: { first, _ in first })
        let changed = entries.filter { seen[$0.key] != $0.signature }
        let current = Set(entries.map(\.key))
        let closed = marks.filter { !current.contains($0.key) }
        if changed.isEmpty && closed.isEmpty { return (nil, next) }
        var lines = ["# Workspace changes (checked \(ISO8601DateFormatter().string(from: now)))",
                     "Only the workspaces that changed. The others are as the earlier workspace blocks show."]
        lines += changed.map(\.text)
        if !closed.isEmpty {
            lines.append("\nClosed: " + closed.map { "\($0.title) (\($0.key))" }.joined(separator: ", "))
        }
        return (lines.joined(separator: "\n"), next)
    }
}

/// How the active transcript is sent: one stream-json user message whose text blocks hold the history in order,
/// ending with this turn's context and question. A block closes after every user row, so the block list of an
/// earlier turn is a byte-identical prefix of every later one, and the provider's cache entry written at the end of
/// one turn's message is found at the same block boundary in the next.
public enum TurnPrompt {
    public static func render(_ message: ConversationMessage) -> String {
        if message.isSummaryMessage { return labelingKeptRow(message) }
        if TurnContext.isContext(message) { return message.content }
        switch message.role {
        case .user: return "[Operator]\n" + message.content
        case .system: return message.content
        case .tool: return "[Result of \(message.toolCallID ?? "a command")]\n" + message.content
        case .assistant:
            guard let id = message.toolCallID else { return "[You]\n" + message.content }
            return "[You ran \(message.toolName ?? "a tool"), call \(id)]\n" + message.content
        }
    }

    /// A summary the compressor folded into a kept row: the summary text as is, and the kept row with its speaker label.
    static func labelingKeptRow(_ message: ConversationMessage) -> String {
        let content = message.content
        guard let kept = message.unwrappedHandoff else { return content }
        let searchRange: Range<String.Index>
        if let delimiter = content.range(of: CompressionText.mergedSummaryDelimiter) {
            searchRange = content.startIndex..<delimiter.lowerBound
        } else if let marker = content.range(of: CompressionText.summaryEndMarker) {
            searchRange = marker.upperBound..<content.endIndex
        } else {
            return content
        }
        guard let range = content.range(of: kept.content, range: searchRange) else { return content }
        return content.replacingCharacters(in: range, with: render(kept))
    }

    /// The system row after a compaction: memory as it is now, then the note that earlier turns were compacted.
    public static func frozenAfterCompaction(memory: String) -> String {
        [memory, CompressionText.compressionNote].filter { !$0.isEmpty }.joined(separator: "\n\n")
    }

    /// System rows are left out: their text rides in the system prompt.
    public static func blocks(_ messages: [ConversationMessage]) -> [String] {
        var blocks: [String] = []
        var open: [String] = []
        for message in messages where message.role != .system {
            open.append(render(message))
            if message.role == .user {
                blocks.append(open.joined(separator: "\n\n"))
                open = []
            }
        }
        if !open.isEmpty { blocks.append(open.joined(separator: "\n\n")) }
        return blocks
    }

    /// The stdin line for `claude -p --input-format stream-json`.
    public static func input(_ blocks: [String]) -> Data {
        let message: [String: Any] = ["type": "user", "message": ["role": "user", "content": blocks.map { ["type": "text", "text": $0] }]]
        var data = (try? JSONSerialization.data(withJSONObject: message, options: [.sortedKeys, .withoutEscapingSlashes])) ?? Data()
        data.append(UInt8(ascii: "\n"))
        return data
    }

    /// Instructions and tool guide (fixed for a build), then the session's frozen system row: the memory snapshot taken
    /// when the chat started, plus the note compaction adds.
    public static func systemPrompt(instructions: String, frozen: String) -> String {
        frozen.isEmpty ? instructions : instructions + "\n\n" + frozen
    }
}

/// One `claude -p` stream-json run, mapped to the rows the chat stores.
public struct TurnRecord: Equatable {
    /// Claude's text, each tool call (assistant row with its call id, the command as content) and each result, in order.
    public var rows: [MessageDraft]
    /// The final reply, or what to show when there is none.
    public var reply: String
    public var failed: Bool
    public var cost: Double?
    /// Prompt size (input + cache read + cache write) of the turn's first model call: the assembled transcript, as the
    /// provider priced it. Later calls in the turn also carry this turn's tool output in full, which the stored
    /// transcript caps, so they would overstate what the next turn resends.
    public var promptTokens: Int?
    /// The context window the provider reported for the model that answered.
    public var contextWindow: Int? = nil

    static let noReplyPrefix = "[No reply: "

    static func noReplyNote(_ reason: String, timestamp: Date) -> MessageDraft {
        MessageDraft(role: .assistant, content: noReplyPrefix + reason + "]", timestamp: timestamp)
    }

    /// What to store after the question, which is already stored: everything the run did, and a no-reply note when it
    /// failed, so the transcript never ends on an unanswered question or an open tool call.
    public func rowsToStore(timestamp: Date) -> [MessageDraft] {
        failed ? rows + [Self.noReplyNote(reply, timestamp: timestamp)] : rows
    }

    /// The provider refused the first call because the prompt doesn't fit, before anything ran.
    public var promptTooLong: Bool {
        guard failed, rows.isEmpty else { return false }
        let text = reply.lowercased()
        return text.contains("prompt is too long") || text.contains("exceed context limit") || text.contains("context window")
    }
}

public enum StreamTurn {
    public static let maxToolOutputChars = 20_000
    static let interruptedOutput = "(No output: the run stopped before this command finished.)"

    public static func record(_ data: Data, stopped: Bool, model: String? = nil, now: Date = Date()) -> TurnRecord {
        var rows: [MessageDraft] = []
        var open: [String] = []
        var firstPrompt: Int?
        var result: [String: Any]?
        for event in events(data) {
            if event["parent_tool_use_id"] is String { continue }
            let message = event["message"] as? [String: Any]
            let blocks = message?["content"] as? [[String: Any]] ?? []
            switch event["type"] as? String {
            case "assistant":
                if firstPrompt == nil, let usage = message?["usage"] as? [String: Any], promptTokens(usage) > 0 {
                    firstPrompt = promptTokens(usage)
                }
                for block in blocks {
                    switch block["type"] as? String {
                    case "text":
                        if let text = block["text"] as? String, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                            rows.append(MessageDraft(role: .assistant, content: text, timestamp: now))
                        }
                    case "tool_use":
                        guard let id = block["id"] as? String else { continue }
                        let name = block["name"] as? String ?? "tool"
                        rows.append(MessageDraft(role: .assistant, content: callText(name, block["input"]), toolName: name,
                                                 toolCallID: id, timestamp: now))
                        open.append(id)
                    default: continue
                    }
                }
            case "user":
                for block in blocks where block["type"] as? String == "tool_result" {
                    guard let id = block["tool_use_id"] as? String, let index = open.firstIndex(of: id) else { continue }
                    open.remove(at: index)
                    let name = rows.last { $0.toolCallID == id }?.toolName
                    rows.append(MessageDraft(role: .tool, content: resultText(block["content"]), toolName: name,
                                             toolCallID: id, timestamp: now))
                }
            case "result":
                result = event
            default: continue
            }
        }
        for id in open {
            let name = rows.last { $0.toolCallID == id }?.toolName
            rows.append(MessageDraft(role: .tool, content: interruptedOutput, toolName: name, toolCallID: id, timestamp: now))
        }
        guard let result, !stopped else {
            return TurnRecord(rows: rows, reply: stopped ? "Stopped." : Asker.noAnswer, failed: true, cost: nil,
                              promptTokens: firstPrompt)
        }
        let failed = result["is_error"] as? Bool ?? false
        let reply = (result["result"] as? String) ?? "No answer."
        if !failed, !reply.isEmpty, rows.last.map({ $0.role != .assistant || $0.toolCallID != nil || $0.content != reply }) ?? true {
            rows.append(MessageDraft(role: .assistant, content: reply, timestamp: now))
        }
        let usage = result["usage"] as? [String: Any]
        let iteration = (usage?["iterations"] as? [[String: Any]])?.first
        return TurnRecord(rows: rows, reply: reply, failed: failed, cost: result["total_cost_usd"] as? Double,
                          promptTokens: firstPrompt ?? iteration.map(promptTokens) ?? usage.map(promptTokens),
                          contextWindow: reportedWindow(result["modelUsage"] as? [String: Any], model: model))
    }

    /// The window reported for the model that answered. A turn can also bill a helper model, whose window says nothing
    /// about this chat's; with no model named, a single entry is the only safe answer.
    static func reportedWindow(_ usage: [String: Any]?, model: String?) -> Int? {
        guard let usage else { return nil }
        let windows = usage.compactMapValues { ($0 as? [String: Any])?["contextWindow"] as? Int }
            .filter { ContextWindow.plausible($0.value) }
        guard let model = model?.lowercased() else { return windows.count == 1 ? windows.first?.value : nil }
        return windows.first { $0.key.lowercased().contains(model) || model.contains($0.key.lowercased()) }?.value
    }

    /// The JSON objects of a stream-json run, one per line. Lines end at the LF byte only: a JSON string may carry
    /// U+2028, U+2029 or U+0085 raw, and those are not line ends.
    public static func events(_ data: Data) -> [[String: Any]] {
        data.split(separator: UInt8(ascii: "\n")).compactMap {
            (try? JSONSerialization.jsonObject(with: Data($0))) as? [String: Any]
        }
    }

    /// One call's prompt is its input plus cache reads and cache writes. The result event's `usage` sums every call
    /// of the turn, so it is only a fallback.
    static func promptTokens(_ usage: [String: Any]) -> Int {
        ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"].reduce(0) { $0 + ((usage[$1] as? Int) ?? 0) }
    }

    /// The command, or the input as JSON, capped like tool output.
    static func callText(_ name: String, _ input: Any?) -> String {
        if let command = (input as? [String: Any])?["command"] as? String { return capped(command) }
        guard let input, let data = try? JSONSerialization.data(withJSONObject: input, options: [.sortedKeys, .withoutEscapingSlashes]) else {
            return name
        }
        return capped(String(decoding: data, as: UTF8.self))
    }

    static func resultText(_ content: Any?) -> String {
        if let s = content as? String { return capped(s) }
        if let parts = content as? [[String: Any]] {
            return capped(parts.map { ($0["text"] as? String) ?? "[\($0["type"] as? String ?? "content")]" }.joined(separator: "\n"))
        }
        return ""
    }

    static func capped(_ text: String) -> String {
        let utf16 = text.utf16.count
        guard utf16 > maxToolOutputChars else { return text }
        let total = text.count
        guard total > maxToolOutputChars else { return text }
        return String(text.prefix(maxToolOutputChars)) + "\n[… \(total - maxToolOutputChars) more characters cut]"
    }
}

/// Stored rows as the chat panel shows them: the operator's messages, replies, and session messaging steps.
public enum ChatHistory {
    public static let compactedNote = "Earlier messages were compressed into a summary."

    /// A question whose run failed before doing anything (only a no-reply note follows it) is left out: the panel
    /// showed the error when it happened. A row a summary was folded into shows once, not again after its original.
    public static func turns(_ messages: [ConversationMessage], tool: String) -> [ChatTurn] {
        let rows = messages.filter { $0.role != .system && !TurnContext.isContext($0) }
        var hidden = Set<Int>()
        for (i, row) in rows.enumerated() where row.role == .user && !row.isSummaryMessage && i + 1 < rows.count {
            let next = rows[i + 1]
            let ends = i + 2 == rows.count || rows[i + 2].role == .user
            if next.role == .assistant && next.toolCallID == nil && next.content.hasPrefix(TurnRecord.noReplyPrefix) && ends {
                hidden.formUnion([i, i + 1])
            }
        }
        func key(_ m: ConversationMessage) -> String { "\(m.role.rawValue)|\(m.timestamp.timeIntervalSince1970)|\(m.content)" }
        var shown = Set<String>()
        var out: [ChatTurn] = []
        for (i, message) in rows.enumerated() where !hidden.contains(i) {
            var row = message
            if message.isSummaryMessage {
                out.append(ChatTurn(role: .step, text: compactedNote))
                guard let kept = message.unwrappedHandoff, !TurnContext.isContext(kept), !shown.contains(key(kept)) else { continue }
                row = kept
            }
            shown.insert(key(row))
            switch row.role {
            case .user: out.append(ChatTurn(role: .you, text: row.content))
            case .assistant where row.toolCallID == nil: out.append(ChatTurn(role: .assistant, text: row.content))
            case .assistant:
                if let step = Messaging.step(command: row.content, sendCommand: tool) { out.append(ChatTurn(role: .step, text: step.text)) }
            case .tool:
                if row.content.hasPrefix("REPLY") { out.append(ChatTurn(role: .step, text: ChatStep.replied(Messaging.replyBody(row.content)).text)) }
            case .system: continue
            }
        }
        return out
    }
}

/// The model's context window the compressor plans against: what the provider last reported for the model, else the
/// table (Claude Code reports 1,000,000 tokens for Haiku, Sonnet, and Opus).
public enum ContextWindow {
    public static let table: [(String, Int)] = [("haiku", 1_000_000), ("sonnet", 1_000_000), ("opus", 1_000_000)]
    public static let fallback = 200_000

    static func reportedKey(_ model: String) -> String { "reportedContextWindow." + model.lowercased() }

    /// A reported window outside this range is a malformed report: 1 would compress every turn, and Int.max never.
    public static let plausibleRange = 8_000...10_000_000

    static func plausible(_ window: Int) -> Bool { plausibleRange.contains(window) }

    /// `ASSISTANT_CONTEXT_WINDOW`, then the `contextWindow` setting, then the last reported window, then the table.
    public static func tokens(for model: String, environment: [String: String] = ProcessInfo.processInfo.environment,
                              settings: UserDefaults? = Asker.settings) -> Int {
        if let value = environment["ASSISTANT_CONTEXT_WINDOW"].flatMap(Int.init), value > 0 { return value }
        if let value = settings?.integer(forKey: "contextWindow"), value > 0 { return value }
        if let value = settings?.integer(forKey: reportedKey(model)), plausible(value) { return value }
        let name = model.lowercased()
        return table.first { name.contains($0.0) }?.1 ?? fallback
    }

    /// Remembers the window the provider reported; returns true when it differs from what was known.
    @discardableResult
    public static func remember(_ window: Int, for model: String, settings: UserDefaults? = Asker.settings) -> Bool {
        guard plausible(window), let settings else { return false }
        let changed = settings.integer(forKey: reportedKey(model)) != window
        settings.set(window, forKey: reportedKey(model))
        return changed
    }
}

public struct ContextMeter: Equatable {
    public var tokens: Int
    public var window: Int

    public init(tokens: Int, window: Int) {
        self.tokens = tokens
        self.window = window
    }

    public var percent: Int { window > 0 ? Int((Double(tokens) / Double(window) * 100).rounded()) : 0 }

    public var text: String {
        let format = IntegerFormatStyle<Int>.number.locale(Locale(identifier: "en_US"))
        return "Context \(tokens.formatted(format)) of \(window.formatted(format)) tokens (\(percent)%)"
    }
}
