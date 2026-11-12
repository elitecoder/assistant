import Foundation

/// Picks whole sentences out of Claude's reply while it's still being written (`claude -p --output-format
/// stream-json --include-partial-messages`), so the first one can be spoken before the rest arrives.
public struct SpokenStream: Equatable {
    private var buffer = ""
    private var sawText = false
    /// The fence of the code block being written, which isn't read.
    private var fence: Fence?
    /// The buffer starts at the beginning of a line, where a fence can open or close.
    private var lineStart = true
    public private(set) var sentences = 0

    /// A sentence shorter than this waits to be joined to the next one, so "Yes." isn't read on its own.
    static let shortest = 12
    static let abbreviations: Set<String> = ["e.g", "i.e", "etc", "vs", "mr", "mrs", "ms", "dr", "st", "no", "approx"]

    public init() {}

    /// The sentences this stream-json line completes. Only the operator's own conversation counts: a subagent's text
    /// (an event with a `parent_tool_use_id`) is never read aloud.
    public mutating func take(line: String) -> [String] {
        guard line.contains("\"stream_event\""),
              let event = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any],
              event["type"] as? String == "stream_event", !(event["parent_tool_use_id"] is String),
              let inner = event["event"] as? [String: Any] else { return [] }
        switch inner["type"] as? String {
        case "content_block_delta":
            guard let delta = inner["delta"] as? [String: Any], delta["type"] as? String == "text_delta",
                  let text = delta["text"] as? String else { return [] }
            sawText = true
            buffer += text
            return drain(all: false)
        case "content_block_stop", "message_stop":
            return drain(all: true)
        default:
            return []
        }
    }

    /// What's left to read once the reply is complete. When nothing streamed, the whole reply.
    public mutating func finish(reply: String) -> [String] {
        if !sawText {
            sawText = true
            buffer = reply
        }
        return drain(all: true)
    }

    private mutating func drain(all: Bool) -> [String] {
        var out: [String] = []
        var from = buffer.startIndex
        var searchFrom = from
        while let end = Self.sentenceEnd(in: buffer, from: searchFrom) {
            let startsLine = searchFrom == buffer.startIndex ? lineStart : buffer[buffer.index(before: searchFrom)] == "\n"
            // A fenced code block is never read: a fence line opens it, a matching fence line closes it (CommonMark),
            // and its lines are dropped.
            // The line as written, indent kept: four or more spaces in front make it indented text, not a fence.
            let line = Self.droppingTrailingSpace(buffer[searchFrom..<end])
            let opens = startsLine ? Fence.opening(line) : nil
            if fence != nil || opens != nil {
                let pending = buffer[from..<searchFrom].trimmingCharacters(in: .whitespacesAndNewlines)
                if !pending.isEmpty { out.append(pending) }
                if let open = fence {
                    if startsLine && open.closes(line) { fence = nil }
                } else {
                    fence = opens
                }
                searchFrom = end
                from = end
                continue
            }
            let sentence = buffer[from..<end].trimmingCharacters(in: .whitespacesAndNewlines)
            searchFrom = end
            guard sentence.count >= Self.shortest else { continue }
            out.append(sentence)
            from = end
        }
        lineStart = from == buffer.startIndex ? lineStart : buffer[buffer.index(before: from)] == "\n"
        buffer = String(buffer[from...])
        if all {
            let rest = buffer.trimmingCharacters(in: .whitespacesAndNewlines)
            if !rest.isEmpty && fence == nil && !(lineStart && Fence.opening(Self.droppingTrailingSpace(buffer[...])) != nil) {
                out.append(rest)
            }
            buffer = ""
            // A block left open ends with the text it's in, so it can't silence what Claude writes after it.
            fence = nil
            lineStart = true
        }
        sentences += out.count
        return out
    }

    static func droppingTrailingSpace(_ text: Substring) -> Substring {
        var end = text.endIndex
        while end > text.startIndex, text[text.index(before: end)].isWhitespace { end = text.index(before: end) }
        return text[..<end]
    }

    /// Just past the first sentence end at or after `start`: ".", "!", or "?" (and any closing quotes or brackets)
    /// followed by white space, or a line break. A decimal like "1.5" or an abbreviation like "e.g." isn't an end, and
    /// a mark at the very end of the text waits, since more may follow.
    static func sentenceEnd(in text: String, from start: String.Index) -> String.Index? {
        var i = start
        while i < text.endIndex {
            let c = text[i]
            if c == "\n" { return text.index(after: i) }
            if c == "." || c == "!" || c == "?" {
                var j = text.index(after: i)
                while j < text.endIndex, "\"')]*”’".contains(text[j]) { j = text.index(after: j) }
                if j < text.endIndex, text[j].isWhitespace, !(c == "." && endsInAbbreviation(text[start..<i])) {
                    return j
                }
            }
            i = text.index(after: i)
        }
        return nil
    }

    static func endsInAbbreviation(_ text: Substring) -> Bool {
        guard let word = text.split(whereSeparator: { $0.isWhitespace || $0 == "(" }).last else { return false }
        return abbreviations.contains(word.lowercased())
    }
}
