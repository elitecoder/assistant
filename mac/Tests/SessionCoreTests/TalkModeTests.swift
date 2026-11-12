import XCTest
@testable import SessionCore

final class BargeInGateTests: XCTestCase {
    /// Feeds 100 ms buffers, the size the microphone tap delivers.
    func run(_ levels: [Float], gate: BargeInGate = BargeInGate()) -> Int? {
        var gate = gate
        return levels.firstIndex { gate.take(level: $0, duration: 0.1) }
    }

    let quiet: [Float] = Array(repeating: 0.1, count: 5)

    func testTalkingOverTheAnswerInterruptsOnce() {
        let speech: [Float] = [0.6, 0.55, 0.62, 0.58, 0.6, 0.61, 0.57]
        XCTAssertEqual(run(quiet + speech), 9, "the fifth voiced buffer is 0.5 s of voice")
        var gate = BargeInGate()
        let fired = (quiet + speech + speech).filter { gate.take(level: $0, duration: 0.1) }
        XCTAssertEqual(fired.count, 1)
    }

    func testABriefDipInsideSpeechDoesNotResetIt() {
        XCTAssertEqual(run(quiet + [0.6, 0.6, 0.15, 0.6, 0.6, 0.6]), 10)
    }

    func testACoughOrAnMmHmmDoesNotInterrupt() {
        XCTAssertNil(run(quiet + [0.8, 0.7] + quiet), "a 0.2 s cough")
        XCTAssertNil(run(quiet + [0.5, 0.5, 0.1, 0.1, 0.5, 0.5, 0.5] + quiet), "uh-huh: two short sounds with a gap")
    }

    func testEchoLeftAfterCancellationIsTooQuiet() {
        let silentRoom: [Float] = Array(repeating: 0.02, count: 5)
        XCTAssertNil(run(silentRoom + Array(repeating: 0.29, count: 30)), "below the absolute floor, even in a silent room")
    }

    func testNothingInterruptsWhileEchoCancellationSettles() {
        XCTAssertEqual(run([0.1] + Array(repeating: 0.6, count: 9)), 7, "voice counts only from 0.3 s on")
    }

    func testANoisyRoomRaisesTheBar() {
        let room: [Float] = Array(repeating: 0.4, count: 10)
        XCTAssertNil(run(room + Array(repeating: 0.5, count: 10)), "0.5 is only 0.1 above a 0.4 room")
        XCTAssertNotNil(run(room + Array(repeating: 0.6, count: 10)))
    }

    func testBadInputIsIgnored() {
        var gate = BargeInGate()
        XCTAssertFalse(gate.take(level: .nan, duration: 0.1))
        XCTAssertFalse(gate.take(level: 0.9, duration: 0))
    }
}

final class TalkLoopTests: XCTestCase {
    func testTheLoopListensAnswersSpeaksAndListensAgain() {
        var loop = TalkLoop()
        XCTAssertEqual(loop.toggle(), [.listen])
        XCTAssertEqual(loop.heard(), [.watch])
        XCTAssertEqual(loop.phase, .answering)
        XCTAssertEqual(loop.startedSpeaking(), [])
        XCTAssertEqual(loop.finishedSpeaking(), [.stopWatching, .listen])
        XCTAssertEqual(loop.phase, .listening)
    }

    func testTalkingOverTheAnswerStopsItAndListens() {
        var loop = TalkLoop()
        _ = loop.toggle()
        _ = loop.heard()
        XCTAssertEqual(loop.bargedIn(), [], "nothing to interrupt before the answer plays")
        _ = loop.startedSpeaking()
        XCTAssertEqual(loop.bargedIn(), [.stopWatching, .interrupt])
        XCTAssertEqual(loop.phase, .listening)
        XCTAssertEqual(loop.finishedSpeaking(), [], "the interrupted answer ending doesn't start another listen")
    }

    func testAnAnswerWithNothingToSayStillListensAgain() {
        var loop = TalkLoop()
        _ = loop.toggle()
        _ = loop.heard()
        XCTAssertEqual(loop.finishedSpeaking(), [.stopWatching, .listen])
    }

    func testSilenceOrStopEndsTalkMode() {
        var loop = TalkLoop()
        _ = loop.toggle()
        XCTAssertEqual(loop.nothingHeard(), [.exit(note: TalkLoop.nothingHeardNote)])
        XCTAssertFalse(loop.on)
        _ = loop.toggle()
        _ = loop.heard()
        XCTAssertEqual(loop.stop(), [.stopWatching, .exit(note: nil)])
        XCTAssertEqual(loop.stop(), [])
        XCTAssertEqual(loop.heard(), [], "a question heard outside Talk Mode doesn't start it")
    }

    func testHoldingTheTalkKeyTurnsOnTalkMode() {
        XCTAssertTrue(TalkLoop.isLongPress(held: 0.6))
        XCTAssertFalse(TalkLoop.isLongPress(held: 0.3))
    }
}

final class BargeInWatchTests: XCTestCase {
    let voice: [Float] = Array(repeating: 0.6, count: 8)
    let quiet: [Float] = Array(repeating: 0.1, count: 5)

    func run(_ watch: inout BargeInWatch, _ levels: [Float], playing: Bool) -> Bool {
        levels.map { watch.take(level: $0, duration: 0.1, playing: playing) }.contains(true)
    }

    /// Voice while Claude is still writing (nothing playing) doesn't use the trigger up.
    func testVoiceBeforePlaybackDoesNotUseUpTheTrigger() {
        var watch = BargeInWatch()
        XCTAssertFalse(run(&watch, quiet + voice, playing: false))
        XCTAssertTrue(run(&watch, quiet + voice, playing: true))
    }

    /// Each sentence gap re-arms it, so a second interruption in the same answer works too.
    func testItReArmsAfterEveryGapInPlayback() {
        var watch = BargeInWatch()
        XCTAssertTrue(run(&watch, quiet + voice, playing: true))
        XCTAssertFalse(run(&watch, voice, playing: true), "fires once per stretch of playback")
        XCTAssertFalse(run(&watch, [0.1], playing: false))
        XCTAssertTrue(run(&watch, quiet + voice, playing: true))
    }

    /// The settle time counts from playback start, not from when the microphone opened.
    func testTheSettleTimeStartsWithPlayback() {
        var watch = BargeInWatch()
        _ = run(&watch, Array(repeating: 0.1, count: 30), playing: false)
        var fired: Int?
        for (i, level) in ([0.1] + Array(repeating: Float(0.6), count: 9)).enumerated()
        where watch.take(level: level, duration: 0.1, playing: true) {
            fired = i
        }
        XCTAssertEqual(fired, 7, "voice counts only from 0.3 s after playback starts")
    }
}

final class TalkLoopMicLostTests: XCTestCase {
    func testALostMicrophoneEndsTalkModeWithANote() {
        var loop = TalkLoop()
        _ = loop.toggle()
        XCTAssertEqual(loop.micLost(), [.stopWatching, .exit(note: TalkLoop.micLostNote)])
        XCTAssertFalse(loop.on)
        XCTAssertEqual(loop.micLost(), [])
    }
}
