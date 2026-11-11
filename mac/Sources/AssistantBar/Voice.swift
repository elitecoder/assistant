import AudioToolbox
import AVFoundation
import SessionCore
import os
import Speech

let voiceLog = Logger(subsystem: "com.assistant.sessions", category: "voice")

/// Hears one spoken question, then hands its text to `onHeard` after a pause in speech.
/// A microphone that sends pure silence is skipped for the next one, the way call apps do.
final class Listener: ObservableObject {
    @Published var listening = false
    @Published var heard = ""
    @Published var levels = [Float](repeating: 0, count: Listener.bars)
    @Published var microphone: String?
    @Published var note: String?
    static let bars = 36
    static let silenceBeforeSwitch: TimeInterval = 0.7
    var onHeard: ((String) -> Void)?
    var onProblem: ((String) -> Void)?

    private var engine = AVAudioEngine()
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var pause: Timer?
    private var loudest: Float = 0
    private var micID: UInt32?
    private var tried = Set<UInt32>()
    private var startedAt = Date()
    private var gotSignal = false
    private var session = UUID()

    func toggle() { listening ? stop() : start() }

    var choices: [String] { InputDevice.all().filter { !$0.virtual }.map(\.name) }

    var current: String? { listening ? microphone : InputDevice.chosen()?.name }

    func use(_ name: String) {
        InputDevice.preferred = name
        note = nil
        objectWillChange.send()
        guard listening else { return }
        teardown()
        tried = []
        begin()
    }

    func start() {
        AVCaptureDevice.requestAccess(for: .audio) { mic in
            SFSpeechRecognizer.requestAuthorization { speech in
                DispatchQueue.main.async {
                    if !mic {
                        self.onProblem?("Assistant can't use the microphone. Allow it in System Settings, Privacy & Security, Microphone.")
                    } else if speech != .authorized {
                        self.onProblem?("Assistant can't use speech recognition. Allow it in System Settings, Privacy & Security, Speech Recognition.")
                    } else {
                        self.tried = []
                        self.note = nil
                        self.begin()
                    }
                }
            }
        }
    }

    private func begin() {
        guard !listening, let recognizer, recognizer.isAvailable else {
            onProblem?("Speech recognition isn't available right now.")
            return
        }
        guard let mic = InputDevice.chosen(excluding: tried) else {
            onProblem?("None of your microphones sent any sound. Check them in System Settings, Sound.")
            return
        }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        engine = AVAudioEngine()
        let input = engine.inputNode
        if let unit = input.audioUnit {
            var device = AudioDeviceID(mic.id)
            let status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                                              &device, UInt32(MemoryLayout<AudioDeviceID>.size))
            if status != noErr { voiceLog.error("couldn't select \(mic.name, privacy: .public): \(status)") }
        }
        micID = mic.id
        microphone = mic.name
        levels = [Float](repeating: 0, count: Self.bars)
        loudest = 0
        startedAt = Date()
        gotSignal = false
        let token = UUID()
        session = token
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { [weak self] buffer, _ in
            request.append(buffer)
            guard let channel = buffer.floatChannelData?[0] else { return }
            let samples = UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength))
            let level = Meter.level(samples)
            let silent = samples.allSatisfy { $0 == 0 }
            DispatchQueue.main.async {
                guard let self, self.session == token else { return }
                self.took(level: level, silent: silent)
            }
        }
        engine.prepare()
        do {
            try engine.start()
            voiceLog.notice("listening on \(mic.name, privacy: .public)")
        } catch {
            voiceLog.error("engine failed on \(mic.name, privacy: .public): \(error.localizedDescription, privacy: .public)")
            input.removeTap(onBus: 0)
            tried.insert(mic.id)
            begin()
            return
        }
        self.request = request
        heard = ""
        listening = true
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            DispatchQueue.main.async {
                guard let self, self.session == token, self.listening else { return }
                if let result {
                    self.heard = result.bestTranscription.formattedString
                    self.waitForPause()
                    if result.isFinal { self.stop() }
                } else if let error = error as NSError? {
                    voiceLog.error("recognition error: \(error.domain, privacy: .public) \(error.code)")
                    self.stop()
                    self.onProblem?(Spoken.problem(domain: error.domain, code: error.code,
                                                   description: error.localizedDescription))
                }
            }
        }
        waitForPause()
    }

    private func took(level: Float, silent: Bool) {
        guard listening else { return }
        loudest = max(loudest, level)
        levels = Meter.window(levels, adding: level, count: Self.bars)
        if !silent { gotSignal = true }
        if !gotSignal, Date().timeIntervalSince(startedAt) >= Self.silenceBeforeSwitch { skipSilentMicrophone() }
    }

    private func skipSilentMicrophone() {
        let silentName = microphone ?? "That microphone"
        voiceLog.notice("\(silentName, privacy: .public) sent pure silence, trying the next one")
        if let micID { tried.insert(micID) }
        teardown()
        begin()
        if listening, let microphone { note = "\(silentName) sent no sound, so I switched to \(microphone)." }
    }

    private func waitForPause() {
        pause?.invalidate()
        pause = Timer.scheduledTimer(withTimeInterval: heard.isEmpty ? 6 : 1.8, repeats: false) { [weak self] _ in
            self?.stop()
        }
    }

    private func teardown() {
        listening = false
        session = UUID()
        pause?.invalidate()
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.cancel()
        request = nil
        task = nil
    }

    func stop() {
        guard listening else { return }
        teardown()
        let text = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        voiceLog.notice("stopped, heard \(text.count) characters, loudest level \(self.loudest)")
        if !text.isEmpty {
            onHeard?(text)
        } else {
            onProblem?(loudest < 0.15
                ? "I didn't hear anything from \(microphone ?? "the microphone"). Pick another one from the microphone menu."
                : "I heard sound but no words. Try again a little closer to the microphone.")
        }
    }
}

final class Speaker: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    @Published var speaking = false
    private let synth = AVSpeechSynthesizer()
    static let preferenceKey = "voice"

    static var systemDefault: String? { AVSpeechSynthesisVoice(language: "en-US")?.identifier }

    var voices: [VoiceInfo] { Voices.usable(Self.installed(), systemDefault: Self.systemDefault) }

    var current: VoiceInfo? {
        Voices.choose(Self.installed(), preferred: UserDefaults.standard.string(forKey: Self.preferenceKey),
                      systemDefault: Self.systemDefault)
    }

    static func installed() -> [VoiceInfo] {
        AVSpeechSynthesisVoice.speechVoices().map {
            VoiceInfo(id: $0.identifier, name: $0.name, language: $0.language, quality: $0.quality.rawValue,
                      novelty: $0.voiceTraits.contains(.isNoveltyVoice))
        }
    }

    func use(_ voice: VoiceInfo) {
        UserDefaults.standard.set(voice.id, forKey: Self.preferenceKey)
        objectWillChange.send()
        say("Hi, this is how I'll sound.")
    }

    override init() {
        super.init()
        synth.delegate = self
    }

    func say(_ markdown: String) {
        synth.stopSpeaking(at: .immediate)
        let utterance = AVSpeechUtterance(string: Spoken.plain(markdown))
        utterance.voice = current.flatMap { AVSpeechSynthesisVoice(identifier: $0.id) }
            ?? AVSpeechSynthesisVoice(language: "en-US")
        speaking = true
        synth.speak(utterance)
    }

    func stop() {
        synth.stopSpeaking(at: .immediate)
        speaking = false
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        DispatchQueue.main.async { self.speaking = false }
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel u: AVSpeechUtterance) {
        DispatchQueue.main.async { self.speaking = false }
    }
}
