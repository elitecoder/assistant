import CryptoKit
import Foundation

/// Text that comes from other sessions (the workspace digest, a session's replies, transcripts, earlier chats) goes
/// to the chat's Claude inside a marked block, so nothing in it can pass for the operator. The block's body is the
/// text as a single JSON string literal on one line: line breaks become `\n` and `\r`, and every character that is or
/// looks like a tag or label bracket (any scalar whose compatibility form holds `<`, `>`, `[`, or `]`, so full-width
/// and small forms too), every control and invisible format character, and every line or paragraph separator becomes a
/// `\uXXXX` escape. So nothing inside can start a line, open or close a tag, or form a speaker label, whatever Unicode
/// it uses. The block's id is a hash of the text: the same text always renders the same bytes (the prompt cache
/// depends on that).
public enum Untrusted {
    public static let tag = "untrusted-session-text"

    public static func wrap(_ text: String) -> String {
        let id = SHA256.hash(data: Data(text.utf8)).prefix(8).map { String(format: "%02x", $0) }.joined()
        return "<\(tag) id=\"\(id)\">\n" + literal(text) + "\n</\(tag) id=\"\(id)\">"
    }

    /// The text of a block `wrap` made, for showing a reply in the panel; other text is returned as it is.
    public static func unwrap(_ text: String) -> String {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        guard lines.count == 3, lines[0].hasPrefix("<\(tag) id=\""), lines[2].hasPrefix("</\(tag) id=\""),
              let decoded = try? JSONSerialization.jsonObject(with: Data(lines[1].utf8), options: .fragmentsAllowed) as? String
        else { return text }
        return decoded
    }

    /// `text` as one JSON string literal.
    static func literal(_ text: String) -> String {
        var out = "\""
        for scalar in text.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            default:
                if escaped(scalar) {
                    for unit in String(scalar).utf16 { out += String(format: "\\u%04x", unit) }
                } else {
                    out.unicodeScalars.append(scalar)
                }
            }
        }
        return out + "\""
    }

    static let brackets = Set("<>[]".unicodeScalars)

    static func escaped(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.properties.generalCategory {
        case .control, .format, .lineSeparator, .paragraphSeparator, .privateUse, .unassigned: return true
        default: return String(scalar).precomposedStringWithCompatibilityMapping.unicodeScalars.contains(where: brackets.contains)
        }
    }
}
