import XCTest
@testable import AssistantBar
@testable import SessionCore

final class KokoroSpeedTests: XCTestCase {
    func samples(_ engine: KokoroEngine, speed: Float) -> Int {
        var count = 0
        let done = expectation(description: "spoken")
        engine.speak("Version Bump Conflicts is waiting on one choice from you.", voiceID: Kokoro.voices[0].id,
                     speed: speed, isCurrent: { true }, chunk: { count += $0.count }, done: { _ in done.fulfill() })
        wait(for: [done], timeout: 120)
        return count
    }

    func testDoubleSpeedSpeaksInRoughlyHalfTheTime() throws {
        try XCTSkipUnless(KokoroEngine.installed && KokoroEngine.supported, "Kokoro voice isn't downloaded on this Mac")
        let engine = KokoroEngine()
        let normal = samples(engine, speed: 1)
        let fast = samples(engine, speed: 2)
        XCTAssertGreaterThan(normal, 0)
        XCTAssertLessThan(Double(fast), Double(normal) * 0.65, "2× should be close to half as long")
        XCTAssertGreaterThan(Double(fast), Double(normal) * 0.35)
    }
}
