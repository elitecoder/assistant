import XCTest
@testable import SessionCore

final class TranscriptAccumulatorTests: XCTestCase {
    func testRefinementsReplaceTheCurrentText() {
        var t = TranscriptAccumulator()
        XCTAssertEqual(t.update("tell"), "tell")
        XCTAssertEqual(t.update("tell work"), "tell work")
        XCTAssertEqual(t.update("tell workspace 25 to"), "tell workspace 25 to")
        XCTAssertEqual(t.update("Tell workspace 25 to"), "Tell workspace 25 to")
    }

    func testARestartAfterAPauseKeepsWhatWasHeard() {
        var t = TranscriptAccumulator()
        _ = t.update("tell workspace 25")
        _ = t.update("tell workspace 25 to use my token")
        XCTAssertEqual(t.update("then"), "tell workspace 25 to use my token then")
        XCTAssertEqual(t.update("then report back"), "tell workspace 25 to use my token then report back")
        XCTAssertEqual(t.update("and close it"), "tell workspace 25 to use my token then report back and close it")
        XCTAssertEqual(t.text, "tell workspace 25 to use my token then report back and close it")
    }

    func testAShrinkingCorrectionWithTheSameFirstWordIsNotARestart() {
        var t = TranscriptAccumulator()
        _ = t.update("what about the the version")
        XCTAssertEqual(t.update("what about the version"), "what about the version")
    }

    func testAnEarlyFirstWordRevisionIsNotARestart() {
        var t = TranscriptAccumulator()
        _ = t.update("hey")
        XCTAssertEqual(t.update("they said yes"), "they said yes")
    }
}
