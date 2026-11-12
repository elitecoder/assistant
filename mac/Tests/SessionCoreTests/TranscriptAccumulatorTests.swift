import XCTest
@testable import SessionCore

/// Result sequences as SFSpeechRecognizer sends them: untimed partials, then the utterance again with segment times
/// when it ends. Recorded from `AssistantBar --transcribe <file> --legacy`.
final class TranscriptAccumulatorTests: XCTestCase {
    typealias Segment = TranscriptAccumulator.Segment

    static func untimed(_ text: String) -> [Segment] {
        text.split(separator: " ").map { _ in Segment(start: 0, duration: 0) }
    }

    static func timed(_ text: String, from start: TimeInterval) -> [Segment] {
        text.split(separator: " ").enumerated().map { Segment(start: start + Double($0.offset) * 0.3, duration: 0.3) }
    }

    func partial(_ t: inout TranscriptAccumulator, _ text: String) -> String {
        t.update(text, segments: Self.untimed(text))
    }

    func timed(_ t: inout TranscriptAccumulator, _ text: String, from start: TimeInterval) -> String {
        t.update(text, segments: Self.timed(text, from: start))
    }

    func testRefinementsReplaceTheCurrentText() {
        var t = TranscriptAccumulator()
        XCTAssertEqual(partial(&t, "tell"), "tell")
        XCTAssertEqual(partial(&t, "tell work"), "tell work")
        XCTAssertEqual(partial(&t, "tell workspace 25 to"), "tell workspace 25 to")
        XCTAssertEqual(partial(&t, "Tell workspace 25 to"), "Tell workspace 25 to")
    }

    /// Without timing, a shorter result with a new first word is still a revision of the same utterance.
    func testAnUntimedRevisionIsNeverARestart() {
        var t = TranscriptAccumulator()
        _ = partial(&t, "What's the status")
        XCTAssertEqual(partial(&t, "What is the status of the PR"), "What is the status of the PR")
        _ = partial(&t, "hey")
        XCTAssertEqual(partial(&t, "they said yes"), "they said yes")
    }

    func testARestartAfterATimedUtteranceKeepsWhatWasHeard() {
        var t = TranscriptAccumulator()
        _ = partial(&t, "tell workspace 25")
        _ = timed(&t, "tell workspace 25 to use my token", from: 0)
        XCTAssertEqual(partial(&t, "then"), "tell workspace 25 to use my token then")
        XCTAssertEqual(partial(&t, "then report back"), "tell workspace 25 to use my token then report back")
        XCTAssertEqual(timed(&t, "then report back", from: 4), "tell workspace 25 to use my token then report back")
        XCTAssertEqual(t.text, "tell workspace 25 to use my token then report back")
    }

    /// Recorded: "Tell workspace 25 to stop the build", a pause, then "Tell workspace 30 to start the deploy".
    func testARestartWithTheSameFirstWordKeepsTheFirstSentence() {
        var t = TranscriptAccumulator()
        _ = partial(&t, "Tell workspace 25 to stop the bill")
        _ = timed(&t, "Tell workspace 25 to stop the bill", from: 0)
        _ = partial(&t, "Tell")
        _ = partial(&t, "Tell workspace")
        _ = partial(&t, "Tell workspace 30 to start the deploy")
        XCTAssertEqual(timed(&t, "Tell workspace 30 to start the deploy", from: 6),
                       "Tell workspace 25 to stop the bill Tell workspace 30 to start the deploy")
    }

    func testTheSameUtteranceTimedAgainReplacesIt() {
        var t = TranscriptAccumulator()
        _ = timed(&t, "what's the status", from: 0)
        XCTAssertEqual(timed(&t, "what is the status", from: 0), "what is the status")
    }

    func testALaterTimedUtteranceWithNoPartialsBetweenIsKept() {
        var t = TranscriptAccumulator()
        _ = timed(&t, "first part", from: 0)
        XCTAssertEqual(timed(&t, "second part", from: 3), "first part second part")
    }
}
