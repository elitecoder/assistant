import AVFoundation
import XCTest
@testable import AssistantBar
@testable import SessionCore

/// Renders speech with AVSpeechSynthesizer.write (nothing plays) as 16 kHz samples, with a pause inserted before
/// the word at `pauseBefore` (a character offset), keeping the sentence's own intonation.
final class SpeechRenderer: NSObject, AVSpeechSynthesizerDelegate {
    private var frames = 0
    private var words: [(location: Int, frame: Int)] = []
    /// Where the inserted pause starts, in samples.
    private(set) var pauseStart: Int?

    func speechSynthesizer(_ s: AVSpeechSynthesizer, willSpeakRangeOfSpeechString range: NSRange, utterance: AVSpeechUtterance) {
        words.append((range.location, frames))
    }

    func render(_ text: String, pauseBefore: Int? = nil, pause: TimeInterval = 0, trailing: TimeInterval = 3) -> [Float] {
        let synth = AVSpeechSynthesizer()
        synth.delegate = self
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        let target = AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1)!
        var samples: [Float] = []
        var done = false
        var converter: AVAudioConverter?
        synth.write(utterance) { buffer in
            guard let pcm = buffer as? AVAudioPCMBuffer else { return }
            if pcm.frameLength == 0 { return done = true }
            if converter == nil { converter = AVAudioConverter(from: pcm.format, to: target) }
            if let out = TurnDetector.convert(pcm, converter!, target) {
                samples += UnsafeBufferPointer(start: out.floatChannelData![0], count: Int(out.frameLength))
            }
            self.frames = samples.count
        }
        while !done { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
        if let pauseBefore, let cut = words.first(where: { $0.location >= pauseBefore })?.frame {
            samples.insert(contentsOf: [Float](repeating: 0, count: Int(pause * 16_000)), at: cut)
            pauseStart = cut
        }
        return samples + [Float](repeating: 0, count: Int(trailing * 16_000))
    }
}

final class TurnDetectionTests: XCTestCase {
    func models() throws -> OnnxTurn {
        try XCTSkipUnless(TurnModels.installed, "Smart Turn isn't downloaded on this Mac")
        return try XCTUnwrap(TurnModels.shared.loaded())
    }

    /// A finished question ends on the 2.5 s timer: Smart Turn never ends one sooner.
    func testAFinishedQuestionEndsOnTheTimer() throws {
        let models = try models()
        let audio = SpeechRenderer().render("What does the version bump session need from me right now?")
        let r = TurnEval.run(audio, models: models, transcribe: { _ in "" })
        let shipped = try XCTUnwrap(r.shipped), timer = try XCTUnwrap(r.timer)
        XCTAssertEqual(shipped, timer, accuracy: 0.05)
        XCTAssertFalse(r.held)
    }

    /// A 2.7 s pause mid-sentence ("Switch the voice to … Kokoro"): the timer alone cuts the question off; Smart Turn
    /// says it isn't finished and holds the turn open.
    func testAPauseMidSentenceIsHeldOpen() throws {
        let models = try models()
        let text = "Switch the voice to Kokoro and read the answer a little faster."
        let audio = SpeechRenderer().render(text, pauseBefore: "Switch the voice to ".count, pause: 2.7)
        let r = TurnEval.run(audio, models: models, transcribe: { _ in "" })
        let shipped = try XCTUnwrap(r.shipped), timer = try XCTUnwrap(r.timer)
        XCTAssertLessThan(timer, r.lastSpeech, "the 2.5 s timer cuts this question off")
        XCTAssertGreaterThanOrEqual(shipped, r.lastSpeech, "Smart Turn didn't hold the pause open")
    }

    /// Time keeps counting past the 16 seconds of audio the detector keeps, and the audio it keeps stays bounded.
    func testLongQuestionsKeepTimeAndBoundedAudio() {
        let core = TurnCore(models: nil)
        for _ in 0..<200 { _ = core.feed([Float](repeating: 0, count: 1600)) }
        XCTAssertEqual(core.seconds, 20, accuracy: 0.05)
        XCTAssertLessThanOrEqual(core.audio.count, 2 * SmartTurn.window + 1600)
    }

    func testWithoutTheModelOnlyTheTimerDecides() {
        let core = TurnCore(models: nil)
        XCTAssertNil(core.feed([Float](repeating: 0, count: 16_000)))
        XCTAssertFalse(core.end.modelAvailable)
    }
}

@MainActor
final class ListenerTurnTests: XCTestCase {
    var recognizers = FakeRecognizers()
    var fake: FakeMic!

    func listening(_ models: OnnxTurn) -> Listener {
        let mic = Microphone(id: 1, name: "Logitech BRIO", builtIn: false, virtual: false)
        fake = FakeMic(mic)
        let fake = fake!
        let listener = Listener(Listener.Dependencies(access: { (.granted, .granted) }, requestAccess: { _, done in done(true, true) },
                                                      choose: { $0.contains(1) ? nil : mic }, open: { _ in .success(fake) },
                                                      recognizers: recognizers, turnModels: { models }))
        listener.pauseBeforeWords = 30
        listener.pauseAfterWords = 30
        listener.start()
        let end = Date().addingTimeInterval(3)
        while fake.sink == nil && Date() < end { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        RunLoop.main.run(until: Date().addingTimeInterval(0.2))
        return listener
    }

    func feed(_ audio: [Float]) {
        for start in stride(from: 0, to: audio.count, by: 1600) {
            fake.sink?(buffer(channels: [Array(audio[start..<min(audio.count, start + 1600)])]))
        }
    }

    func wait(_ seconds: TimeInterval, until condition: () -> Bool) {
        let deadline = Date().addingTimeInterval(seconds)
        while !condition() && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
    }

    /// With Smart Turn loaded, the Listener stops on its decision, long before the (here 30 s) pause timers.
    func testSmartTurnEndsTheQuestion() throws {
        try XCTSkipUnless(TurnModels.installed, "Smart Turn isn't downloaded on this Mac")
        let models = try XCTUnwrap(TurnModels.shared.loaded())
        let audio = SpeechRenderer().render("Tell the trim fix session to merge main into its branch.", trailing: 3)
        let listener = listening(models)
        recognizers.sessions[0].onText("Tell the trim fix session to merge main")
        feed(audio)
        wait(10) { !listener.listening }
        XCTAssertFalse(listener.listening, "Smart Turn didn't end the question")
        XCTAssertTrue(recognizers.sessions[0].finished)
    }

    /// Room noise the voice detector takes for speech doesn't end a question that has no words yet; once words
    /// arrive after Smart Turn said it was over, the question ends right away.
    func testATurnWithNoWordsWaitsForThem() throws {
        try XCTSkipUnless(TurnModels.installed, "Smart Turn isn't downloaded on this Mac")
        let models = try XCTUnwrap(TurnModels.shared.loaded())
        let audio = SpeechRenderer().render("What needs me right now?", trailing: 3)
        let listener = listening(models)
        feed(audio)
        wait(2) { !listener.listening }
        XCTAssertTrue(listener.listening, "ended with no words")
        recognizers.sessions[0].onText("What needs me right now?")
        XCTAssertFalse(listener.listening)
        XCTAssertTrue(recognizers.sessions[0].finished)
    }

    /// Noise the voice detector hears as speech keeps Smart Turn from deciding (here, no audio reaches it at all);
    /// the timer after words still ends the question on time, as it does without Smart Turn.
    func testWithSmartTurnUndecidedTheTimerAfterWordsEndsTheQuestion() throws {
        try XCTSkipUnless(TurnModels.installed, "Smart Turn isn't downloaded on this Mac")
        let models = try XCTUnwrap(TurnModels.shared.loaded())
        let listener = listening(models)
        listener.pauseAfterWords = 0.5
        recognizers.sessions[0].onText("Tell the trim fix session to merge main")
        wait(3) { !listener.listening }
        XCTAssertFalse(listener.listening, "the timer after words waited for Smart Turn")
    }

    /// Mid-sentence ("Switch the voice to …"), Smart Turn holds the question open, so the 2.5 s timer after the last
    /// words doesn't cut it off; the question still ends past the longest hold.
    func testAHeldQuestionOutlastsTheTimerAfterWords() throws {
        try XCTSkipUnless(TurnModels.installed, "Smart Turn isn't downloaded on this Mac")
        let models = try XCTUnwrap(TurnModels.shared.loaded())
        let renderer = SpeechRenderer()
        let text = "Switch the voice to Kokoro and read the answer a little faster."
        let audio = renderer.render(text, pauseBefore: "Switch the voice to ".count, pause: 3.5)
        let pause = try XCTUnwrap(renderer.pauseStart)
        let listener = listening(models)
        listener.pauseAfterWords = TurnEnd.timer
        recognizers.sessions[0].onText("Switch the voice to")
        let started = Date()
        feed(Array(audio.prefix(pause + Int(3.5 * 16_000))))
        wait(3.5) { !listener.listening }
        XCTAssertTrue(listener.listening, "the timer after words cut off a held question")
        wait(4) { !listener.listening }
        XCTAssertFalse(listener.listening, "a held question never ended")
        XCTAssertLessThan(Date().timeIntervalSince(started), TurnEnd.longest + 1.5)
    }
}

final class DeviceListTests: XCTestCase {
    /// Redraws read the cached devices; only a device change (or a press) queries Core Audio again.
    func testDevicesAreQueriedOnlyAfterTheyChange() {
        var queries = 0
        let list = DeviceList {
            queries += 1
            return DeviceList.Snapshot(mics: [], defaultID: nil, lidClosed: false)
        }
        for _ in 0..<100 { _ = list.snapshot }
        XCTAssertEqual(queries, 1)
        list.changed()
        _ = list.snapshot
        XCTAssertEqual(queries, 2)
    }
}

final class TurnModelsLoadingTests: XCTestCase {
    /// Loading takes about half a second; asking for the models meanwhile mustn't wait for it.
    func testAskingWhileTheModelsLoadDoesNotWait() {
        let models = TurnModels(installed: { true }, damaged: { [] }, open: {
            Thread.sleep(forTimeInterval: 0.5)
            return nil
        })
        XCTAssertNil(models.ready(), "the first ask starts loading in the background")
        Thread.sleep(forTimeInterval: 0.05)
        let started = Date()
        XCTAssertNil(models.ready())
        XCTAssertLessThan(Date().timeIntervalSince(started), 0.05, "waited for the load")
    }

    /// A file that fails its checksum before the first load isn't loaded; it's downloaded again.
    func testADamagedModelIsNotLoaded() {
        var opened = false
        var fetched: [String] = []
        let models = TurnModels(installed: { true }, damaged: { [SmartTurn.vad] }, open: {
            opened = true
            return nil
        }, redownload: { fetched += $0.map(\.name) })
        XCTAssertNil(models.loaded())
        XCTAssertFalse(opened)
        XCTAssertEqual(fetched, ["silero_vad.onnx"])
    }
}
