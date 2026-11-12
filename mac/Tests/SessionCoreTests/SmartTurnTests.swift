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
            if case .done = d { break }
            if d == .ask, !answers.isEmpty { _ = t.answer(answers.removeFirst()) }
        }
        return out
    }

    /// The timer decides: Smart Turn is asked once, 0.2 s into the pause, and a confident "finished" doesn't end it
    /// sooner.
    func testTheTimerEndsAFinishedQuestionAtTwoAndAHalfSeconds() {
        var t = TurnEnd(modelAvailable: true)
        XCTAssertEqual(silence(&t, 1), [], "silence before any speech doesn't count")
        speech(&t, 1)
        XCTAssertEqual(silence(&t, 0.16), [])
        XCTAssertEqual(silence(&t, 0.07, answers: [0.99]), [.ask])
        XCTAssertEqual(silence(&t, 2.2), [], "never earlier, however sure the model is")
        XCTAssertEqual(silence(&t, 0.1), [.done(held: false)])
        XCTAssertEqual(t.silence, 2.5, accuracy: 0.05)
    }

    /// "Not finished" holds the turn open up to 4 seconds of silence.
    func testUnfinishedHoldsUpToFourSeconds() {
        var t = TurnEnd(modelAvailable: true)
        speech(&t, 1)
        XCTAssertEqual(silence(&t, 0.25, answers: [0.2]), [.ask])
        XCTAssertEqual(silence(&t, 3.6), [], "past the 2.5 s timer")
        XCTAssertEqual(silence(&t, 0.2), [.done(held: true)])
    }

    func testSpeakingAgainStartsOverAndDropsAStaleAnswer() {
        var t = TurnEnd(modelAvailable: true)
        speech(&t, 1)
        XCTAssertEqual(silence(&t, 0.25), [.ask])
        speech(&t, 0.5)
        XCTAssertEqual(t.answer(0.1), .wait)
        XCTAssertEqual(silence(&t, 0.25), [.ask], "asked again in the new pause")
        _ = t.answer(0.9)
        XCTAssertEqual(silence(&t, 2.4), [.done(held: false)], "the stale 0.1 didn't hold this pause")
    }

    func testAnAnswerWithNothingAskedIsIgnored() {
        var t = TurnEnd(modelAvailable: true)
        speech(&t, 1)
        _ = t.answer(0.1)
        XCTAssertEqual(silence(&t, 2.6).last, .done(held: false))
    }

    func testWithoutTheModelOnlyTheTimerDecides() {
        var t = TurnEnd(modelAvailable: false)
        speech(&t, 1)
        XCTAssertEqual(silence(&t, 2.4), [])
        XCTAssertEqual(silence(&t, 0.2).first, .done(held: false))
    }

    /// The Listener's timer after words waits longer only while Smart Turn holds the turn, until past the longest hold.
    func testTheTimerAfterWordsWaitsLongerOnlyWhileHolding() {
        XCTAssertEqual(TurnEnd.extraAfterWords(timer: 2.5, holding: true), 2.5)
        XCTAssertEqual(TurnEnd.extraAfterWords(timer: 2.5, holding: false), 0)
        XCTAssertEqual(TurnEnd.extraAfterWords(timer: 30, holding: true), 0)
    }

    /// Holding starts with a "not finished" answer and ends when the operator speaks again.
    func testHoldingLastsUntilSpeech() {
        var t = TurnEnd(modelAvailable: true)
        speech(&t, 1)
        XCTAssertFalse(t.holding)
        _ = silence(&t, 0.25, answers: [0.2])
        XCTAssertTrue(t.holding)
        speech(&t, 0.1)
        XCTAssertFalse(t.holding)
        _ = silence(&t, 0.25, answers: [0.9])
        XCTAssertFalse(t.holding)
    }
}

final class SentenceEndTests: XCTestCase {
    func testAFinishedSentenceEndsInAMarkAndNotADanglingWord() {
        XCTAssertTrue(SentenceEnd.complete("What does the version bump session need from me?"))
        XCTAssertTrue(SentenceEnd.complete("Tell the trim session to push."))
        XCTAssertFalse(SentenceEnd.complete("Switch the voice to."), "a period after a preposition")
        XCTAssertFalse(SentenceEnd.complete("Remind the release session that the..."))
        XCTAssertFalse(SentenceEnd.complete("Tell the cleanup session to delete"), "no mark")
        XCTAssertFalse(SentenceEnd.complete("Ask about their."))
    }
}
