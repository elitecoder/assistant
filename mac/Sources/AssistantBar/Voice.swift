import AVFoundation
import SessionCore
import os
import Speech

let voiceLog = Logger(subsystem: "com.assistant.sessions", category: "voice")

/// Hears one spoken question, then hands its text to `onHeard` after a pause in speech.
final class Listener: ObservableObject {
    @Published var listening = false
    @Published var heard = ""
    @Published var levels = [Float](repeating: 0, count: Listener.bars)
    @Published var microphone: String?
    static let bars = 36
    private var loudest: Float = 0
    var onHeard: ((String) -> Void)?
    var onProblem: ((String) -> Void)?

    private let engine = AVAudioEngine()
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var pause: Timer?

    func toggle() { listening ? stop() : start() }

    func start() {
        voiceLog.notice("start: mic status \(AVCaptureDevice.authorizationStatus(for: .audio).rawValue), speech status \(SFSpeechRecognizer.authorizationStatus().rawValue)")
        AVCaptureDevice.requestAccess(for: .audio) { mic in
            voiceLog.notice("mic access: \(mic)")
            SFSpeechRecognizer.requestAuthorization { speech in
                voiceLog.notice("speech access: \(speech.rawValue)")
                DispatchQueue.main.async {
                    if !mic {
                        self.onProblem?("Assistant can't use the microphone. Allow it in System Settings, Privacy & Security, Microphone.")
                    } else if speech != .authorized {
                        self.onProblem?("Assistant can't use speech recognition. Allow it in System Settings, Privacy & Security, Speech Recognition.")
                    } else {
                        self.begin()
                    }
                }
            }
        }
    }

    private func begin() {
        voiceLog.notice("begin: listening \(self.listening), recognizer \(self.recognizer != nil), available \(self.recognizer?.isAvailable ?? false), on-device \(self.recognizer?.supportsOnDeviceRecognition ?? false)")
        guard !listening, let recognizer, recognizer.isAvailable else {
            onProblem?("Speech recognition isn't available right now.")
            return
        }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        let input = engine.inputNode
        microphone = InputDevice.defaultName()
        levels = [Float](repeating: 0, count: Self.bars)
        loudest = 0
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { [weak self] buffer, _ in
            request.append(buffer)
            guard let channel = buffer.floatChannelData?[0] else { return }
            let level = Meter.level(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
            DispatchQueue.main.async {
                guard let self, self.listening else { return }
                self.loudest = max(self.loudest, level)
                self.levels = Meter.window(self.levels, adding: level, count: Self.bars)
            }
        }
        engine.prepare()
        do {
            try engine.start()
            voiceLog.notice("engine started on \(self.microphone ?? "unknown", privacy: .public), format \(input.outputFormat(forBus: 0).description, privacy: .public)")
        } catch {
            voiceLog.error("engine failed: \(error.localizedDescription)")
            input.removeTap(onBus: 0)
            onProblem?("The microphone didn't start: \(error.localizedDescription)")
            return
        }
        self.request = request
        heard = ""
        listening = true
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            DispatchQueue.main.async {
                guard let self, self.listening else { return }
                if let error { voiceLog.error("recognition error: \((error as NSError).domain, privacy: .public) \((error as NSError).code, privacy: .public) \(error.localizedDescription, privacy: .public)") }
                if let result {
                    self.heard = result.bestTranscription.formattedString
                    self.waitForPause()
                    if result.isFinal { self.stop() }
                } else if let error = error as NSError? {
                    self.stop()
                    self.onProblem?(Spoken.problem(domain: error.domain, code: error.code,
                                                   description: error.localizedDescription))
                }
            }
        }
        waitForPause()
    }

    private func waitForPause() {
        pause?.invalidate()
        pause = Timer.scheduledTimer(withTimeInterval: heard.isEmpty ? 6 : 1.8, repeats: false) { [weak self] _ in
            self?.stop()
        }
    }

    func stop() {
        guard listening else { return }
        listening = false
        pause?.invalidate()
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.cancel()
        request = nil
        task = nil
        let text = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        voiceLog.notice("stopped, heard \(text.count) characters, loudest level \(self.loudest)")
        if text.isEmpty {
            onProblem?(loudest < 0.15
                ? "I didn't hear anything from \(microphone ?? "the microphone"). Check the input in System Settings, Sound."
                : "I heard sound but no words. Try again a little closer to the microphone.")
        }
        if !text.isEmpty { onHeard?(text) }
    }
}

final class Speaker: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    @Published var speaking = false
    private let synth = AVSpeechSynthesizer()

    override init() {
        super.init()
        synth.delegate = self
    }

    func say(_ markdown: String) {
        synth.stopSpeaking(at: .immediate)
        let utterance = AVSpeechUtterance(string: Spoken.plain(markdown))
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
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
