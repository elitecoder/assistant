import XCTest
@testable import SessionCore

final class SmartTurnFeatureTests: XCTestCase {
    /// Values from Hugging Face's WhisperFeatureExtractor(chunk_length=8) on the same signal (transformers 5.22.2,
    /// `do_normalize=True`), so Smart Turn gets the features it was trained on.
    func testFeaturesMatchHuggingFacesWhisperExtractor() {
        let tone = (0..<(3 * 16_000)).map { n -> Float in
            let t = Double(n) / 16_000
            return Float(0.3 * sin(2 * .pi * 440 * t) + 0.1 * sin(2 * .pi * 3000 * t) * Double(n) / Double(3 * 16_000))
        }
        let f = SmartTurn.features(SmartTurn.lastWindow(tone))
        XCTAssertEqual(f.count, 80 * 800)
        let mean = f.reduce(0, +) / Float(f.count)
        let std = (f.reduce(0) { $0 + ($1 - mean) * ($1 - mean) } / Float(f.count)).squareRoot()
        XCTAssertEqual(mean, -0.182659, accuracy: 0.002)
        XCTAssertEqual(std, 0.290551, accuracy: 0.002)
        let value = { (m: Int, t: Int) in f[m * 800 + t] }
        XCTAssertEqual(value(0, 0), -0.233481, accuracy: 0.002)
        XCTAssertEqual(value(10, 700), 1.677053, accuracy: 0.005)
        XCTAssertEqual(value(2, 799), 0.821418, accuracy: 0.005)
        XCTAssertEqual(value(24, 799), 0.525556, accuracy: 0.005)
        XCTAssertEqual(value(46, 799), -0.146094, accuracy: 0.005)
        XCTAssertEqual(value(68, 799), -0.003945, accuracy: 0.005)
        XCTAssertEqual(value(79, 799), -0.105776, accuracy: 0.005)
    }

    /// Eight seconds of sound with no silence in front: the first frames depend on the reflected padding.
    func testTheFirstFramesMatchWhenTheWindowIsFull() {
        let tone = (0..<(8 * 16_000)).map { n -> Float in
            let t = Double(n) / 16_000
            return Float(0.3 * sin(2 * .pi * 440 * t) + 0.1 * sin(2 * .pi * 3000 * t))
        }
        let f = SmartTurn.features(tone)
        let value = { (m: Int, t: Int) in f[m * 800 + t] }
        XCTAssertEqual(value(2, 0), 1.219985, accuracy: 0.005)
        XCTAssertEqual(value(2, 1), 0.704608, accuracy: 0.005)
        XCTAssertEqual(value(37, 0), 0.441667, accuracy: 0.005)
        XCTAssertEqual(value(46, 0), 0.551080, accuracy: 0.005)
        XCTAssertEqual(value(10, 0), 1.552244, accuracy: 0.005)
    }

    func testTheWindowKeepsTheLastEightSecondsWithSilenceInFront() {
        XCTAssertEqual(SmartTurn.lastWindow([1, 2]).count, 128_000)
        XCTAssertEqual(SmartTurn.lastWindow([1, 2]).suffix(3), [0, 1, 2])
        let long = (0..<130_000).map(Float.init)
        XCTAssertEqual(SmartTurn.lastWindow(long).first, 2_000)
    }
}

final class TurnEndTests: XCTestCase {
    func speech(_ t: inout TurnEnd, _ seconds: TimeInterval) {
        for _ in 0..<Int(seconds / 0.032) { _ = t.take(speech: true, duration: 0.032) }
    }

    /// Silence in 32 ms windows, answering each ask with the next probability; returns the decisions that weren't `wait`.
    func silence(_ t: inout TurnEnd, _ seconds: TimeInterval, answers: [Float] = []) -> [TurnEnd.Decision] {
        var answers = answers
        var out: [TurnEnd.Decision] = []
        for _ in 0..<Int((seconds / 0.032).rounded()) {
            let d = t.take(speech: false, duration: 0.032)
            if d != .wait { out.append(d) }
            if d == .ask, !answers.isEmpty {
                let a = t.answer(answers.removeFirst())
                if a != .wait { out.append(a) }
            }
        }
        return out
    }

    func testThreeConfidentCallsEndTheTurn() {
        var t = TurnEnd(modelAvailable: true)
        XCTAssertEqual(silence(&t, 1), [], "silence before any speech doesn't count")
        speech(&t, 1)
        XCTAssertEqual(silence(&t, 0.16), [], "a gap between words isn't a pause yet")
        XCTAssertEqual(silence(&t, 0.07, answers: [0.95]), [.ask])
        XCTAssertEqual(silence(&t, 0.3, answers: [0.95]), [.ask], "asked again at 0.5 s")
        XCTAssertEqual(silence(&t, 0.42, answers: [0.97]), [.ask, .done(byModel: true)], "and at 0.9 s")
    }

    /// A call at 0.5 that's sure but not sure enough (0.8) means mid-sentence: keep listening to the limit.
    func testOneUnsureCallKeepsListeningUntilTheLimit() {
        var t = TurnEnd(modelAvailable: true)
        speech(&t, 1)
        XCTAssertEqual(silence(&t, 0.25, answers: [0.99]), [.ask])
        XCTAssertEqual(silence(&t, 0.3, answers: [0.8]), [.ask])
        XCTAssertEqual(silence(&t, 2.3), [], "no more asks once it doubted")
        XCTAssertEqual(silence(&t, 0.2).first, .done(byModel: false), "the hard limit")
    }

    func testSpeakingAgainStartsOverAndDropsAStaleAnswer() {
        var t = TurnEnd(modelAvailable: true)
        speech(&t, 1)
        XCTAssertEqual(silence(&t, 0.25), [.ask])
        speech(&t, 0.5)
        XCTAssertEqual(t.answer(0.99), .wait, "the operator spoke again before the answer came")
        XCTAssertEqual(silence(&t, 0.25, answers: [0.99]), [.ask])
        XCTAssertEqual(silence(&t, 0.3, answers: [0.99]), [.ask])
        XCTAssertEqual(silence(&t, 0.42, answers: [0.99]), [.ask, .done(byModel: true)])
    }

    func testAnAnswerWithNothingAskedIsIgnored() {
        var t = TurnEnd(modelAvailable: true)
        speech(&t, 1)
        XCTAssertEqual(t.answer(0.99), .wait)
    }

    func testWithoutTheModelTheOldTimerDecides() {
        var t = TurnEnd(modelAvailable: false)
        speech(&t, 1)
        XCTAssertEqual(silence(&t, 2.4), [])
        XCTAssertEqual(silence(&t, 0.2).first, .done(byModel: false))
    }

    func testTheListenersTimerIsOnlyABackstopWithSmartTurn() {
        XCTAssertEqual(TurnEnd.backstop(timer: 2.5, smartTurn: true), 4)
        XCTAssertEqual(TurnEnd.backstop(timer: 2.5, smartTurn: false), 2.5)
        XCTAssertEqual(TurnEnd.backstop(timer: 30, smartTurn: true), 30)
    }
}
