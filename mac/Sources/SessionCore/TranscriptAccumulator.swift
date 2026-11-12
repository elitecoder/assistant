import Foundation

/// Joins Apple's live transcription across pauses. After a pause, on-device recognition can start a fresh
/// transcript instead of extending the last one; this keeps what was already heard and adds the new words after it.
public struct TranscriptAccumulator: Equatable {
    public private(set) var committed = ""
    private var last = ""

    public init() {}

    /// Takes the recognizer's latest partial result and returns the full text heard so far.
    public mutating func update(_ partial: String) -> String {
        let text = partial.trimmingCharacters(in: .whitespacesAndNewlines)
        if Self.startedOver(from: last, to: text) { committed = Self.join(committed, last) }
        last = text
        return Self.join(committed, text)
    }

    public var text: String { Self.join(committed, last) }

    /// A restart, not a refinement: the new result no longer begins with the old first word, and it didn't grow
    /// out of a short phrase (the recognizer still revises the first word of a one- or two-word phrase).
    static func startedOver(from old: String, to new: String) -> Bool {
        let oldWords = words(old)
        let newWords = words(new)
        guard let first = oldWords.first, let newFirst = newWords.first, newFirst != first else { return false }
        return newWords.count <= oldWords.count || oldWords.count >= 3
    }

    static func words(_ text: String) -> [String] {
        text.lowercased().split(whereSeparator: { $0.isWhitespace }).map { $0.trimmingCharacters(in: .punctuationCharacters) }
            .filter { !$0.isEmpty }
    }

    static func join(_ a: String, _ b: String) -> String {
        [a, b].filter { !$0.isEmpty }.joined(separator: " ")
    }
}
