import XCTest
@testable import SessionCore

final class SpeechSpeedTests: XCTestCase {
    func testSpeedStaysBetweenOneAndTwo() {
        XCTAssertEqual(SpeechSpeed.clamped(0.5), 1.0)
        XCTAssertEqual(SpeechSpeed.clamped(3), 2.0)
        XCTAssertEqual(SpeechSpeed.clamped(.nan), 1.0)
        XCTAssertEqual(SpeechSpeed.clamped(1.5), 1.5)
    }

    /// Rates from the measured map: × 1.5 is about 0.586, not 0.75 (which plays at 2.5×), and × 2 about 0.669.
    func testAppleRateFollowsTheMeasuredSpeedupsAndCapsAtTheMaximum() {
        XCTAssertEqual(SpeechSpeed.appleRate(1.0, defaultRate: 0.5, maximumRate: 1.0), 0.5)
        XCTAssertEqual(SpeechSpeed.appleRate(1.5, defaultRate: 0.5, maximumRate: 1.0), 0.5855, accuracy: 0.001)
        XCTAssertEqual(SpeechSpeed.appleRate(2.0, defaultRate: 0.5, maximumRate: 1.0), 0.6693, accuracy: 0.001)
        XCTAssertEqual(SpeechSpeed.appleRate(1.25, defaultRate: 0.5, maximumRate: 1.0), 0.5450, accuracy: 0.001)
        XCTAssertEqual(SpeechSpeed.appleRate(2.0, defaultRate: 0.5, maximumRate: 0.6), 0.6)
        let rates = SpeechSpeed.choices.map { SpeechSpeed.appleRate($0, defaultRate: 0.5, maximumRate: 1) }
        XCTAssertEqual(rates, rates.sorted(), "faster choices never get a slower rate")
    }

    func testLabelsReadNaturally() {
        XCTAssertEqual(SpeechSpeed.choices.map(SpeechSpeed.label), ["1×", "1.25×", "1.5×", "1.75×", "2×"])
    }
}
