import Foundation

public struct SessionSummary: Equatable {
    public var goal: String?
    public var lastAsk: String?
    public var lastSaid: String?
    public var lastActivity: Date?
    public var pullRequests: [String: Int] = [:]

    public init() {}
}

enum Timestamps {
    static let withFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    static let plain = ISO8601DateFormatter()

    static func parse(_ s: String) -> Date? { withFraction.date(from: s) ?? plain.date(from: s) }
}

public enum Transcript {
    static let skipPrefixes = ["<", "Caveat:", "[Request interrupted", "Another Claude session",
                               "This session is being continued"]

    static func lines(_ data: Data) -> [Data] {
        data.split(separator: UInt8(ascii: "\n")).map { Data($0) }
    }

    static func record(_ line: Data) -> [String: Any]? {
        (try? JSONSerialization.jsonObject(with: line)) as? [String: Any]
    }

    /// The operator's text in a user record. `skipping` leaves out app-written lines such as command output (which
    /// start with "<"); delivery checks match the raw text instead, so a message that starts with "<" still counts.
    static func humanText(_ r: [String: Any], skipping: Bool = true) -> String? {
        guard r["type"] as? String == "user", r["isMeta"] as? Bool != true, r["isSidechain"] as? Bool != true,
              let message = r["message"] as? [String: Any] else { return nil }
        var text: String?
        if let s = message["content"] as? String {
            text = s
        } else if let blocks = message["content"] as? [[String: Any]] {
            text = blocks.filter { $0["type"] as? String == "text" }
                .compactMap { $0["text"] as? String }
                .joined(separator: " ")
        }
        guard let t = text?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty,
              !skipping || !skipPrefixes.contains(where: { t.hasPrefix($0) }) else { return nil }
        return t
    }

    static func assistantText(_ r: [String: Any]) -> String? {
        guard r["type"] as? String == "assistant", r["isSidechain"] as? Bool != true,
              let blocks = (r["message"] as? [String: Any])?["content"] as? [[String: Any]] else { return nil }
        return blocks.filter { $0["type"] as? String == "text" }
            .compactMap { $0["text"] as? String }
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .last { !$0.isEmpty && !$0.hasPrefix("Workspace renamed") }
    }

    public static func summarize(_ data: Data) -> SessionSummary {
        var s = SessionSummary()
        for line in lines(data) {
            guard let r = record(line) else { continue }
            if let t = r["timestamp"] as? String, let d = Timestamps.parse(t) { s.lastActivity = d }
            if r["type"] as? String == "pr-link", let url = r["prUrl"] as? String, let n = r["prNumber"] as? Int {
                s.pullRequests[url] = n
            }
            if let t = humanText(r) {
                if s.goal == nil { s.goal = t }
                s.lastAsk = t
            }
            if let t = assistantText(r) { s.lastSaid = t }
        }
        return s
    }

    static func letters(_ s: String) -> String {
        let word = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "_"))
        return String(String.UnicodeScalarView(s.unicodeScalars.filter { word.contains($0) }.prefix(30)))
    }

    /// True when `data` holds the operator message cmux reports for a workspace.
    public static func holdsMessage(_ message: String?, submittedAt: String, in data: Data) -> Bool {
        let needle = Data("\"timestamp\":\"\(submittedAt.prefix(19))".utf8)
        guard data.range(of: needle) != nil else { return false }
        guard let message, !message.hasPrefix("/"), !message.hasPrefix("<") else { return true }
        let want = letters(message)
        return lines(data).contains { line in
            guard line.range(of: needle) != nil, let r = record(line), let text = humanText(r) else { return false }
            return letters(text) == want
        }
    }
}
