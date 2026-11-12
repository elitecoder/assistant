import Foundation

public enum Spoken {
    public static let style = """
    # Reply style

    This reply is read aloud. Use two to four short sentences. No lists, markdown, file paths, or IDs.
    Say a workspace by its name and number, for example "Version Bump Conflicts, workspace 25".
    """

    /// Text a speech synthesizer can read: markdown marks, links, and fenced code blocks removed. A fence follows
    /// CommonMark: it opens on a line of its own and closes only with the same mark, at least as long, so a stray
    /// ```` ``` ```` in a sentence removes nothing, and a block left open runs to the end of the reply.
    public static func plain(_ markdown: String) -> String {
        var kept: [Substring] = []
        var open: Fence?
        for line in markdown.split(separator: "\n", omittingEmptySubsequences: false) {
            if let fence = open {
                if fence.closes(line) { open = nil }
                continue
            }
            if let fence = Fence.opening(line) {
                open = fence
                continue
            }
            kept.append(line)
        }
        var s = kept.joined(separator: "\n")
        s = s.replacingOccurrences(of: #"\[([^\]]+)\]\([^)]+\)"#, with: "$1", options: .regularExpression)
        s = s.replacingOccurrences(of: #"(?m)^\s*(#+|[-*]|\d+\.)\s+"#, with: "", options: .regularExpression)
        s = s.replacingOccurrences(of: #"[*_`]"#, with: "", options: .regularExpression)
        s = s.replacingOccurrences(of: #"\s*\[(\d+)\]"#, with: ", workspace $1", options: .regularExpression)
        return s.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }.joined(separator: " ")
    }
}

/// A CommonMark code fence: a line that starts (after up to three spaces) with three or more backticks or tildes.
public struct Fence: Equatable {
    let mark: Character
    let length: Int

    /// The fence `line` opens, if it's a fence line. A backtick fence's info string can't hold a backtick.
    public static func opening<S: StringProtocol>(_ line: S) -> Fence? {
        let indent = line.prefix { $0 == " " }.count
        guard indent <= 3 else { return nil }
        let rest = line.dropFirst(indent)
        guard let mark = rest.first, mark == "`" || mark == "~" else { return nil }
        let length = rest.prefix { $0 == mark }.count
        guard length >= 3 else { return nil }
        if mark == "`" && rest.dropFirst(length).contains("`") { return nil }
        return Fence(mark: mark, length: length)
    }

    /// Whether `line` closes this fence: the same mark, at least as long, and nothing after it but spaces.
    public func closes<S: StringProtocol>(_ line: S) -> Bool {
        let indent = line.prefix { $0 == " " }.count
        guard indent <= 3 else { return false }
        let rest = line.dropFirst(indent)
        let run = rest.prefix { $0 == mark }.count
        return run >= length && rest.dropFirst(run).allSatisfy { $0 == " " || $0 == "\t" || $0 == "\r" }
    }
}

extension Spoken {
    /// What to tell the operator when speech recognition fails.
    public static func problem(domain: String, code: Int, description: String) -> String {
        if domain == "kLSRErrorDomain" && code == 201 {
            return "Voice needs Dictation. Turn it on in System Settings, Keyboard, Dictation, then try again."
        }
        return "Voice didn't work: \(description)"
    }
}
