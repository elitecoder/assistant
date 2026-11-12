import CryptoKit
import Foundation

/// Text that comes from other sessions (the workspace digest, a session's replies, transcripts, earlier chats) goes
/// to the chat's Claude inside a marked block, so nothing in it can pass for the operator. The block's id is a hash of
/// its own text: the same text always renders the same bytes (the prompt cache depends on that).
///
/// Inside, every kind of line break becomes `\n`, so a label after `\r\n`, `\r`, U+2028, U+2029, U+0085, a vertical
/// tab, or a form feed still starts a line that gets checked. A line whose first visible text is a speaker label or a
/// block tag (after spaces, no-break spaces, or invisible format characters) is quoted, and block tags anywhere in a
/// line are escaped, so the text can't open or close a block or start a fake turn.
public enum Untrusted {
    public static let tag = "untrusted-session-text"
    static let labels = ["[operator]", "[you]", "[you ran", "[result of", "[system:", "[context compaction"]
    static let breaks: Set<Unicode.Scalar> = ["\r", "\u{0B}", "\u{0C}", "\u{85}", "\u{2028}", "\u{2029}"]
    static let quotedMark = "(quoted) "

    public static func wrap(_ text: String) -> String {
        let body = normalize(text)
        let id = SHA256.hash(data: Data(body.utf8)).prefix(8).map { String(format: "%02x", $0) }.joined()
        let lines = body.split(separator: "\n", omittingEmptySubsequences: false).map { escape(String($0)) }
        return "<\(tag) id=\"\(id)\">\n" + lines.joined(separator: "\n") + "\n</\(tag) id=\"\(id)\">"
    }

    /// The text of a block `wrap` made, for showing a reply in the panel; other text is returned as it is.
    public static func unwrap(_ text: String) -> String {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        guard lines.count >= 2, lines.first?.hasPrefix("<\(tag) id=\"") == true, lines.last?.hasPrefix("</\(tag) id=\"") == true
        else { return text }
        return lines.dropFirst().dropLast().map { line in
            line.hasPrefix(quotedMark) ? String(line.dropFirst(quotedMark.count)) : String(line)
        }.joined(separator: "\n").replacingOccurrences(of: "&lt;", with: "<")
    }

    /// Every line break as `\n`, with `\r\n` as one break.
    static func normalize(_ text: String) -> String {
        var out = String.UnicodeScalarView()
        var previousCR = false
        for scalar in text.unicodeScalars {
            if scalar == "\n" && previousCR {
                previousCR = false
                continue
            }
            previousCR = scalar == "\r"
            out.append(breaks.contains(scalar) ? "\n" : scalar)
        }
        return String(out)
    }

    static func escape(_ line: String) -> String {
        let escaped = line.replacingOccurrences(of: #"<(\s*/?\s*)(?i:untrusted-session-text)"#, with: "&lt;$1untrusted-session-text",
                                                options: .regularExpression)
        let visible = String(String.UnicodeScalarView(escaped.unicodeScalars.drop { scalar in
            scalar.properties.isWhitespace || [.control, .format].contains(scalar.properties.generalCategory)
        })).lowercased()
        return labels.contains { visible.hasPrefix($0) } ? quotedMark + escaped : escaped
    }
}
