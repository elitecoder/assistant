import Foundation

public enum Attention: Int, Comparable {
    case needsYou, done, idle, working

    public static func < (a: Attention, b: Attention) -> Bool { a.rawValue < b.rawValue }
}

public enum Classifier {
    static let waitingPhrases = ["tell me which", "tell me if", "waiting on you", "your call", "need from you",
                                 "needs from you", "let me know", "your approval", "your go-ahead", "say go",
                                 "reply with", "one reply"]
    static let trim = CharacterSet(charactersIn: " -*#>`_").union(.whitespaces)

    /// The line of Claude's last reply that most likely asks the operator something.
    public static func askLine(_ said: String?) -> String {
        let lines = (said ?? "").split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: trim) }
            .filter { !$0.isEmpty }
        return lines.last { $0.hasSuffix("?") } ?? lines.last ?? ""
    }

    public static func asksOperator(_ said: String?) -> Bool {
        let lines = (said ?? "").split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: trim).lowercased() }
            .filter { !$0.isEmpty }
        return askLine(said).hasSuffix("?") || lines.suffix(4).contains { line in waitingPhrases.contains { line.contains($0) } }
    }

    public static func status(_ s: SessionSummary, prStates: [String: String], idleMinutes: Double,
                              threshold: Double) -> Attention {
        if idleMinutes < threshold { return .working }
        if !s.pullRequests.isEmpty, s.pullRequests.keys.allSatisfy({ prStates[$0] == "MERGED" }) { return .done }
        return asksOperator(s.lastSaid) ? .needsYou : .idle
    }
}
