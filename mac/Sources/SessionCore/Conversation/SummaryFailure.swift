// Ported from Hermes Agent (MIT) agent/context_compressor.py (refusal detection, failure classification, cooldown ladder, terminal failures).
import Foundation

/// What the summarizer gets: the full prompt plus the inputs it was built from.
public struct SummaryRequest: Sendable {
    public var prompt: String
    /// Output target in tokens (already in the prompt); informational, never a hard cap.
    public var targetTokens: Int
    public var focusTopic: String?
    public var isIterativeUpdate: Bool
    public var turnCount: Int
}

public typealias Summarizer = (SummaryRequest) throws -> String

/// Errors a summarizer may throw so the compressor can classify them like Hermes does. Any other error is
/// classified by its description text.
public enum SummaryError: Error, Equatable, CustomStringConvertible {
    case noProvider
    case http(status: Int, message: String)
    case timeout(String)
    /// The summary stopped on the output cap (`finish_reason=length`) and is partial.
    case truncated
    case overloaded(String)
    case accessDenied(String)
    case network(String)
    case invalidResponse(String)
    case emptyContent
    case refusal
    case userAttribution

    public var description: String {
        switch self {
        case .noProvider: return "no llm provider configured"
        case .http(let status, let message): return "HTTP \(status): \(message)"
        case .timeout(let message): return "timed out: \(message)"
        case .truncated: return "Context compression summary was truncated (finish_reason=length): generation hit the output token cap and the summary is incomplete"
        case .overloaded(let message): return "overloaded: \(message)"
        case .accessDenied(let message): return "access denied: \(message)"
        case .network(let message): return "network error: \(message)"
        case .invalidResponse(let message): return "expecting value: \(message)"
        case .emptyContent: return "Context compression LLM returned empty content"
        case .refusal: return "Context compression LLM returned refusal content"
        case .userAttribution: return "Context compression summary invented user attribution for a session with no user-authored turns"
        }
    }
}

/// The failure class a compaction attempt ended with.
public enum SummaryFailureClass: String, Sendable {
    case accessOrQuota = "summary_auth_failure"
    case network = "summary_network_failure"
    case truncated = "summary_truncated_failure"
    case emptyContent = "summary_empty_content_failure"
    case overloaded = "summary_overload_failure"
    case aborted = "summary_generation_aborted"
}

struct SummaryFailureKind: Equatable {
    var modelNotFound = false
    var timeout = false
    var jsonDecode = false
    var streamingClosed = false
    var emptyContent = false
    var truncated = false
    var overloaded = false
    var accessOrQuota = false
    var noProvider = false
}

enum SummaryValidation {
    static let cooldownLadder = [60, 300, 900]
    static let noProviderCooldownSeconds = 600
    static let consecutiveOverloadAbortEscalation = 3
    /// Empty or refused replies abort like Hermes, but a summarizer that refuses every time must not freeze compaction.
    static let consecutiveEmptyAbortEscalation = 3

    private static let refusalPrefix = Regex.compile(
        #"^\s*(?:(?:sorry|i(?:['’]m| am)\s+sorry|i\s+apologi[sz]e|as\s+an\s+ai)\s*[,;:]?\s*(?:but\s+)?)?(?:i|we)\s+(?:can(?:\s*not|['’]t)|could\s*not|couldn['’]t|won['’]t|will\s+not|must\s+decline|refuse\s+to|am\s+unable\s+to|am\s+not\s+able\s+to)\b|^\s*(?:i['’]?m|i\s+am)\s+(?:unable|not\s+able)\b"#,
        options: [.caseInsensitive])
    private static let sectionHeading = Regex.compile(#"^##\s"#, options: [.anchorsMatchLines])

    /// A whole response that refuses instead of summarizing: refusal opener, no `## ` sections, and it names the summary.
    static func isRefusal(_ content: String) -> Bool {
        let normalized = content.collapsedWhitespace
        guard Regex.matches(refusalPrefix, normalized) else { return false }
        if Regex.matches(sectionHeading, content) { return false }
        let opener = normalized.scalarPrefix(400).lowercased()
        return opener.contains("summar") || opener.contains("checkpoint")
    }

    static func ladderRung(_ count: Int) -> Int {
        cooldownLadder[min(count, cooldownLadder.count) - 1]
    }

    static func classify(_ error: Error) -> SummaryFailureKind {
        var kind = SummaryFailureKind()
        if let known = error as? SummaryError {
            switch known {
            case .noProvider: kind.noProvider = true
            case .timeout: kind.timeout = true
            case .truncated: kind.truncated = true
            case .overloaded: kind.overloaded = true
            case .accessDenied: kind.accessOrQuota = true
            case .network: kind.streamingClosed = true
            case .invalidResponse: kind.jsonDecode = true
            case .emptyContent, .refusal: kind.emptyContent = true
            case .userAttribution: break
            case .http(let status, let message):
                kind = classify(text: message.lowercased(), status: status)
            }
            return kind
        }
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain {
            if nsError.code == URLError.timedOut.rawValue {
                kind.timeout = true
            } else if networkURLErrorCodes.contains(nsError.code) {
                kind.streamingClosed = true
            }
            return kind
        }
        return classify(text: String(describing: error).lowercased(), status: nil)
    }

    /// Offline, DNS, dropped connection and TLS failures: transient, so the session is kept unchanged (Hermes aborts these).
    static let networkURLErrorCodes: Set<Int> = Set([
        URLError.Code.notConnectedToInternet, .cannotFindHost, .cannotConnectToHost, .networkConnectionLost, .dnsLookupFailed,
        .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate, .serverCertificateNotYetValid,
        .serverCertificateHasUnknownRoot, .clientCertificateRejected, .clientCertificateRequired, .dataNotAllowed,
        .internationalRoamingOff, .callIsActive,
    ].map(\.rawValue))

    private static func classify(text err: String, status: Int?) -> SummaryFailureKind {
        var kind = SummaryFailureKind()
        if err.contains("no llm provider configured") { kind.noProvider = true }
        kind.modelNotFound = [404, 503].contains(status ?? 0)
            || ["model_not_found", "does not exist", "no available channel"].contains(where: err.contains)
        kind.timeout = [408, 429, 502, 504].contains(status ?? 0) || err.contains("timeout") || err.contains("timed out")
        kind.jsonDecode = err.contains("expecting value")
        kind.streamingClosed = ["connection reset", "connection refused", "connection closed", "network",
                                "certificate", "remoteprotocolerror", "incomplete chunked read"].contains(where: err.contains)
        kind.emptyContent = ["empty content", "refusal content", "llm returned none response",
                             "llm returned invalid response"].contains(where: err.contains)
        kind.truncated = err.contains("finish_reason=length")
        kind.overloaded = status == 529 || ["overloaded", "at capacity", "over capacity"].contains(where: err.contains)
        let rateLimited = status == 429 || err.contains("rate limit")
        kind.accessOrQuota = !rateLimited && ([401, 402, 403].contains(status ?? 0)
            || ["no api key was found", "no api key found", "no credentials were found", "insufficient_quota",
                "quota exceeded", "quota_exceeded", "out of funds", "out of credits", "out of credit",
                "out of extra usage"].contains(where: err.contains))
        return kind
    }

    /// Redacted before it is cut, so a secret straddling the cut cannot survive half-masked.
    static func shortErrorText(_ error: Error, limit: Int = 220) -> String {
        var text = Redaction.redact(String(describing: error).trimmed)
        if text.isEmpty { text = String(describing: type(of: error)) }
        return text.scalarCount <= limit ? text : text.scalarPrefix(limit - 3).trimmingTrailingWhitespace() + "..."
    }

    /// User-facing abort text per terminal failure class, with `%d` for the preserved message count.
    static func abortMessage(_ failure: SummaryFailureClass, preserved: Int) -> String {
        let template: String
        switch failure {
        case .accessOrQuota:
            template = "Summary generation failed with a terminal access or quota error — aborting compression. %d "
                + "message(s) preserved unchanged; the session was NOT rotated. Check the provider credential, "
                + "permission, quota, or inference endpoint, then retry with /compress or start fresh with /new."
        case .network:
            template = "Summary generation failed with a network/connection error — aborting compression. %d message(s) "
                + "preserved unchanged; the session was NOT rotated. This is transient: retry with /compress once "
                + "connectivity recovers, or continue the conversation as-is."
        case .truncated:
            template = "Summary generation failed (output hit the token cap; summary is incomplete) — aborting compression. "
                + "%d message(s) preserved unchanged; the session was NOT rotated. A truncated summary would silently "
                + "lose context: retry with /compress, or raise the summarizer's output budget."
        case .emptyContent:
            template = "Summary generation failed (LLM returned empty content) — aborting compression. %d message(s) "
                + "preserved unchanged; the session was NOT rotated. This indicates upstream provider degradation: "
                + "retry with /compress once the provider recovers, or continue the conversation as-is."
        case .overloaded:
            template = "Summary generation failed because the provider is overloaded — aborting compression. %d message(s) "
                + "preserved unchanged; the session was NOT rotated. Retry with /compress once capacity recovers, "
                + "or continue the conversation as-is."
        case .aborted:
            template = "Summary generation failed — aborting compression (compression.abort_on_summary_failure=true). "
                + "%d message(s) preserved unchanged. Conversation is frozen until the next /compress or /new."
        }
        return template.replacingOccurrences(of: "%d", with: String(preserved))
    }
}
