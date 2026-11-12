import XCTest
@testable import SessionCore

final class SpeechSpeedTests: XCTestCase {
    func testSpeedStaysBetweenOneAndTwo() {
        XCTAssertEqual(SpeechSpeed.clamped(0.5), 1.0)
        XCTAssertEqual(SpeechSpeed.clamped(3), 2.0)
        XCTAssertEqual(SpeechSpeed.clamped(.nan), 1.0)
        XCTAssertEqual(SpeechSpeed.clamped(1.5), 1.5)
    }

    func testAppleRateScalesTheDefaultAndCapsAtTheMaximum() {
        XCTAssertEqual(SpeechSpeed.appleRate(1.0, defaultRate: 0.5, maximumRate: 1.0), 0.5)
        XCTAssertEqual(SpeechSpeed.appleRate(1.5, defaultRate: 0.5, maximumRate: 1.0), 0.75)
        XCTAssertEqual(SpeechSpeed.appleRate(2.0, defaultRate: 0.6, maximumRate: 1.0), 1.0)
    }

    func testLabelsReadNaturally() {
        XCTAssertEqual(SpeechSpeed.choices.map(SpeechSpeed.label), ["1×", "1.25×", "1.5×", "1.75×", "2×"])
    }
}
