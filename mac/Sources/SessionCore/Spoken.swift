import Foundation

public enum Spoken {
    public static let style = """
    # Reply style

    This reply is read aloud. Use two to four short sentences. No lists, markdown, file paths, or IDs.
    Say a workspace by its name and number, for example "Version Bump Conflicts, workspace 25".
    """

    /// Text a speech synthesizer can read: markdown marks, links, and code blocks (fenced with ```` ``` ```` or
    /// `~~~`, closed or not) removed.
    public static func plain(_ markdown: String) -> String {
        var s = markdown
        s = s.replacingOccurrences(of: #"(```|~~~)[\s\S]*?(\1|\z)"#, with: "", options: .regularExpression)
        s = s.replacingOccurrences(of: #"\[([^\]]+)\]\([^)]+\)"#, with: "$1", options: .regularExpression)
        s = s.replacingOccurrences(of: #"(?m)^\s*(#+|[-*]|\d+\.)\s+"#, with: "", options: .regularExpression)
        s = s.replacingOccurrences(of: #"[*_`]"#, with: "", options: .regularExpression)
        s = s.replacingOccurrences(of: #"\s*\[(\d+)\]"#, with: ", workspace $1", options: .regularExpression)
        return s.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }.joined(separator: " ")
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
