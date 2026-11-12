import AVFoundation
import ObjCExceptions
import XCTest
@testable import SessionCore

final class AudioExceptionTests: XCTestCase {
    /// AVAudioEngine raises an Objective-C exception for a bad tap; the app must catch it instead of aborting.
    func testARaisedAudioEngineExceptionIsCaught() {
        let engine = AVAudioEngine()
        let mixer = AVAudioMixerNode()
        engine.attach(mixer)
        engine.connect(mixer, to: engine.mainMixerNode, format: nil)
        let first = AssistantCatchException { mixer.installTap(onBus: 0, bufferSize: 1024, format: nil) { _, _ in } }
        XCTAssertNil(first)
        let second = AssistantCatchException { mixer.installTap(onBus: 0, bufferSize: 1024, format: nil) { _, _ in } }
        XCTAssertNotNil(second, "installing a second tap on one bus raises")
        mixer.removeTap(onBus: 0)
    }

    func testZeroRateOrNoChannelFormatsAreUnusable() {
        XCTAssertFalse(Microphones.usable(sampleRate: 0, channels: 1))
        XCTAssertFalse(Microphones.usable(sampleRate: 48_000, channels: 0))
        XCTAssertFalse(Microphones.usable(sampleRate: .nan, channels: 2))
        XCTAssertTrue(Microphones.usable(sampleRate: 48_000, channels: 1))
    }
}
