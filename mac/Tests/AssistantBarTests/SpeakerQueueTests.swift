import AVFoundation
import XCTest
@testable import AssistantBar
@testable import SessionCore

/// Stands in for Kokoro, the player, and the system synthesizer, so the Speaker's queueing runs with no sound.
final class FakeOutput: SpeechOutput {
    struct Synthesis {
        let text: String
        let chunk: ([Float]) -> Void
        let done: (Bool) -> Void
    }

    var naturalVoiceReady = true
    var syntheses: [Synthesis] = []
    var scheduled: [() -> Void] = []
    var stops = 0
    var spokenApple: [String] = []
    var writtenApple: [String] = []
    let engine: AVAudioEngine? = nil

    func synthesize(_ text: String, voiceID: String, speed: Float, isCurrent: @escaping () -> Bool,
                    chunk: @escaping ([Float]) -> Void, done: @escaping (Bool) -> Void) {
        syntheses.append(Synthesis(text: text, chunk: chunk, done: done))
    }

    func schedule(_ buffer: AVAudioPCMBuffer, played: @escaping () -> Void) { scheduled.append(played) }
    func stopPlayback() { stops += 1 }
    func speakApple(_ utterance: AVSpeechUtterance) { spokenApple.append(utterance.speechString) }
    func writeApple(_ utterance: AVSpeechUtterance, _ buffer: @escaping (AVAudioBuffer) -> Void) {
        writtenApple.append(utterance.speechString)
    }
    func stopApple() {}
}

final class SpeakerQueueTests: XCTestCase {
    var saved: Any?

    override func setUp() {
        saved = UserDefaults.standard.object(forKey: Speaker.preferenceKey)
        UserDefaults.standard.set(Kokoro.voices[0].id, forKey: Speaker.preferenceKey)
    }

    override func tearDown() { UserDefaults.standard.set(saved, forKey: Speaker.preferenceKey) }

    func settle() { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }

    /// A sentence that arrives while another plays is queued after it; nothing playing is cut off.
    func testAppendQueuesWithoutCuttingOffTheSentencePlaying() {
        let output = FakeOutput()
        let speaker = Speaker(output: output)
        speaker.say("First sentence.")
        let stopsAfterSay = output.stops
        output.syntheses[0].chunk([0.1, 0.2])
        settle()
        speaker.append("Second sentence.")
        XCTAssertEqual(output.syntheses.map(\.text), ["First sentence.", "Second sentence."])
        XCTAssertEqual(output.stops, stopsAfterSay, "append stopped the sentence playing")
        XCTAssertTrue(speaker.playingAudio)
    }

    /// Speaking stays on until every queued sentence is synthesized and played, not when the first one ends.
    func testSpeakingLastsUntilEverySentenceHasPlayed() {
        let output = FakeOutput()
        let speaker = Speaker(output: output)
        speaker.say("First sentence.")
        output.syntheses[0].chunk([0.1])
        output.syntheses[0].done(true)
        speaker.append("Second sentence.")
        settle()
        output.scheduled[0]()
        settle()
        XCTAssertTrue(speaker.speaking, "said done while Kokoro was still working on the second sentence")
        XCTAssertFalse(speaker.playingAudio, "nothing is playing between the sentences")
        output.syntheses[1].chunk([0.1])
        output.syntheses[1].done(true)
        settle()
        XCTAssertTrue(speaker.playingAudio)
        output.scheduled[1]()
        settle()
        XCTAssertFalse(speaker.speaking)
        XCTAssertFalse(speaker.playingAudio)
    }

    func testStopEndsPlaybackAndDropsLateAudio() {
        let output = FakeOutput()
        let speaker = Speaker(output: output)
        speaker.say("First sentence.")
        output.syntheses[0].chunk([0.1])
        settle()
        speaker.stop()
        XCTAssertFalse(speaker.playingAudio)
        output.syntheses[0].chunk([0.1])
        settle()
        XCTAssertEqual(output.scheduled.count, 1, "audio from the stopped answer was played")
        XCTAssertFalse(speaker.speaking)
    }

    /// In Talk Mode, Apple's voice goes through the engine (rendered with write), so echo cancellation removes it.
    func testTalkModesAppleVoiceGoesThroughEchoCancellation() {
        let output = FakeOutput()
        output.naturalVoiceReady = false
        let speaker = Speaker(output: output)
        speaker.watching = true
        speaker.say("The trim fix merged.")
        XCTAssertEqual(output.writtenApple, ["The trim fix merged."])
        XCTAssertEqual(output.spokenApple, [])
        speaker.watching = false
        speaker.say("Outside Talk Mode.")
        XCTAssertEqual(output.spokenApple, ["Outside Talk Mode."])
    }
}
