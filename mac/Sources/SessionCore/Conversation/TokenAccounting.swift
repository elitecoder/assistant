// Ported from Hermes Agent (MIT) agent/model_metadata.py, agent/usage_anchor.py, agent/turn_usage.py and agent/context_compressor.py.
import Foundation

/// Rough token accounting, matching Hermes's `bytes / 4` estimator and its per-message tail-budget price.
public enum TokenEstimate {
    public static let charsPerToken = 4

    /// CJK/Hangul/Kana code points count about one token each; everything else is `ceil(UTF-8 bytes / 4)`.
    public static func rough(_ text: String) -> Int {
        if text.isEmpty { return 0 }
        var dense = 0
        var otherBytes = 0
        for scalar in text.unicodeScalars {
            if isTokenDense(scalar) {
                dense += 1
            } else {
                otherBytes += UTF8.width(scalar)
            }
        }
        return dense + (otherBytes + 3) / charsPerToken
    }

    /// Per-message price used by every tail-protection walk: content, plus 10 for role overhead, plus the tool-call envelope.
    public static func messageBudget(_ message: ConversationMessage) -> Int {
        if message.isToolCall {
            return 10 + rough(toolCallEnvelope(message))
        }
        return rough(message.content) + 10
    }

    /// Whole-message estimate over the wire shape (Hermes prices `str(message)`).
    public static func message(_ message: ConversationMessage) -> Int {
        rough(wireShadow(message))
    }

    public static func messages(_ messages: [ConversationMessage]) -> Int {
        messages.reduce(0) { $0 + message($1) }
    }

    static func toolCallEnvelope(_ message: ConversationMessage) -> String {
        "{'id': \((message.toolCallID ?? "").pythonRepr), 'type': 'function', 'function': {'name': \((message.toolName ?? "tool").pythonRepr), 'arguments': \(message.content.pythonRepr)}}"
    }

    static func wireShadow(_ message: ConversationMessage) -> String {
        if message.isToolCall {
            return "{'role': 'assistant', 'content': '', 'tool_calls': [\(toolCallEnvelope(message))]}"
        }
        var shadow = "{'role': \(message.role.rawValue.pythonRepr), 'content': \(message.content.pythonRepr)"
        if message.role == .tool, let id = message.toolCallID {
            shadow += ", 'tool_call_id': \(id.pythonRepr)"
        }
        return shadow + "}"
    }

    private static func isTokenDense(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.value {
        case 0x1100...0x11FF, 0x2E80...0x9FFF, 0xA960...0xA97F, 0xAC00...0xD7AF, 0xF900...0xFAFF, 0xFF00...0xFFEF:
            return true
        default:
            return false
        }
    }
}

/// The provider's last reported prompt size at a message seq. Messages after it are estimated, so the
/// context figure is exact up to the anchor and rough only for what was appended since.
public struct UsageAnchor: Equatable, Sendable {
    public var promptTokens: Int
    public var completionTokens: Int
    /// Seq of the newest message the provider priced.
    public var atSeq: Int
    /// Fingerprint of every message up to `atSeq`; nil skips the prefix check (anchors restored from a session row).
    public var prefixFingerprint: UInt64?

    public init(promptTokens: Int, completionTokens: Int = 0, atSeq: Int, prefixFingerprint: UInt64? = nil) {
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens
        self.atSeq = atSeq
        self.prefixFingerprint = prefixFingerprint
    }

    /// The anchor a session row carries, or nil when the provider never reported usage.
    public init?(session: ConversationSession) {
        guard let tokens = session.lastPromptTokens, tokens > 0, let seq = session.lastPromptTokensAtSeq else { return nil }
        self.init(promptTokens: tokens, atSeq: seq)
    }

    /// Anchor a provider reading against the transcript it priced; nil when usage is missing.
    public static func capture(promptTokens: Int, completionTokens: Int = 0,
                               messages: [ConversationMessage]) -> UsageAnchor? {
        guard promptTokens > 0, let last = messages.last else { return nil }
        return UsageAnchor(promptTokens: promptTokens, completionTokens: max(0, completionTokens), atSeq: last.seq,
                           prefixFingerprint: fingerprint(messages))
    }

    /// Anchored prompt + completion tokens plus a rough estimate of the messages after the anchor; nil when the
    /// anchor no longer matches the transcript (a compaction or rewind replaced the priced prefix).
    public func contextTokens(_ messages: [ConversationMessage]) -> Int? {
        guard promptTokens > 0, let baseIndex = messages.firstIndex(where: { $0.seq == atSeq }) else { return nil }
        if let prefixFingerprint, UsageAnchor.fingerprint(Array(messages[...baseIndex])) != prefixFingerprint {
            return nil
        }
        var delta = Array(messages[(baseIndex + 1)...])
        if completionTokens > 0, let first = delta.first, first.role == .assistant {
            delta.removeFirst()
        }
        return [max(0, completionTokens), TokenEstimate.messages(delta)].reduce(promptTokens) { total, next in
            let (sum, overflow) = total.addingReportingOverflow(next)
            return overflow ? Int.max : sum
        }
    }

    /// Context size for the trigger: anchored when the anchor still fits, else a full rough estimate.
    public static func contextTokens(_ messages: [ConversationMessage], anchor: UsageAnchor?) -> Int {
        anchor?.contextTokens(messages) ?? TokenEstimate.messages(messages)
    }

    static func fingerprint(_ messages: [ConversationMessage]) -> UInt64 {
        var hash = FNV1a()
        for message in messages {
            hash.add(message.role.rawValue)
            hash.add(message.content)
            hash.add(message.toolCallID ?? "")
            hash.add(message.toolName ?? "")
        }
        return hash.value
    }
}

struct FNV1a {
    private(set) var value: UInt64 = 0xcbf2_9ce4_8422_2325

    mutating func add(_ text: String) {
        for byte in text.utf8 {
            value ^= UInt64(byte)
            value = value &* 0x0000_0100_0000_01B3
        }
        value ^= 0xFF
        value = value &* 0x0000_0100_0000_01B3
    }

    static func hash(_ text: String) -> UInt64 {
        var hash = FNV1a()
        hash.add(text)
        return hash.value
    }
}
