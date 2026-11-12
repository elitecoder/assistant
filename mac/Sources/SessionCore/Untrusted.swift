import CryptoKit
import Foundation

/// Text that comes from other sessions (the workspace digest, a session's replies, transcripts) goes to the chat's
/// Claude inside a marked block, so nothing in it can pass for the operator. The block's id is a hash of its own
/// text: the same text always renders the same bytes (the prompt cache depends on that), and the text can't contain
/// its own closing tag. Lines inside that look like a speaker label are quoted, so they can't start a fake turn.
public enum Untrusted {
    public static let tag = "untrusted-session-text"
    static let labels = ["[Operator]", "[You]", "[You ran", "[Result of", "[System:", "[CONTEXT COMPACTION"]

    public static func wrap(_ text: String) -> String {
        let id = SHA256.hash(data: Data(text.utf8)).prefix(8).map { String(format: "%02x", $0) }.joined()
        let quoted = text.split(separator: "\n", omittingEmptySubsequences: false).map { line -> String in
            let start = line.drop { $0 == " " || $0 == "\t" }
            return labels.contains { start.hasPrefix($0) } || start.hasPrefix("<\(tag)") || start.hasPrefix("</\(tag)")
                ? "(quoted) " + line : String(line)
        }.joined(separator: "\n")
        return "<\(tag) id=\"\(id)\">\n\(quoted)\n</\(tag) id=\"\(id)\">"
    }
}
