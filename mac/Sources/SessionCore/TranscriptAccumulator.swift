import Foundation

/// Joins SFSpeechRecognizer's live transcription across pauses. After a pause, on-device recognition starts a fresh
/// transcript instead of extending the last one. Partial results carry no timing (every segment is at 0 with no
/// duration); the recognizer marks the end of an utterance by sending it once more with real segment times. So a
/// timed result closes the utterance, and whatever comes after it, untimed or timed from later in the audio, is the
/// next one. Words alone can't tell a restart from a revision: "Tell workspace 25…" can restart as "Tell…", and
/// "What's the status" can be revised to "What is the status of the PR".
public struct TranscriptAccumulator: Equatable {
    /// One word's place in the audio, in seconds from the start of the request.
    public struct Segment: Equatable {
        public let start: TimeInterval
        public let duration: TimeInterval

        public init(start: TimeInterval, duration: TimeInterval) {
            self.start = start
            self.duration = duration
        }
    }

    public private(set) var committed = ""
    private var current = ""
    /// Where the current utterance ends, once the recognizer has timed it.
    private var closedEnd: TimeInterval?

    /// A timed result that starts this much before the closed utterance ends, or more, is that utterance again.
    static let overlap: TimeInterval = 0.05

    public init() {}

    /// Takes the recognizer's latest result and returns the full text heard so far.
    public mutating func update(_ text: String, segments: [Segment]) -> String {
        let timed = segments.contains { $0.duration > 0 }
        if let closedEnd {
            if !timed || (segments.first?.start ?? 0) >= closedEnd - Self.overlap { committed = Self.join(committed, current) }
            self.closedEnd = nil
        }
        if timed, let last = segments.last { closedEnd = last.start + last.duration }
        current = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return self.text
    }

    public var text: String { Self.join(committed, current) }

    static func join(_ a: String, _ b: String) -> String {
        [a, b].filter { !$0.isEmpty }.joined(separator: " ")
    }
}
