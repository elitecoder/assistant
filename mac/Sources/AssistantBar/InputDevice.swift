import CoreAudio
import IOKit
import SessionCore

enum InputDevice {
    static let preferenceKey = "microphone"

    static func address(_ selector: AudioObjectPropertySelector,
                        _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }

    static func defaultID() -> AudioDeviceID? {
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var a = address(kAudioHardwarePropertyDefaultInputDevice)
        let ok = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &device)
        return ok == noErr ? device : nil
    }

    static func name(_ device: AudioDeviceID) -> String? {
        var name: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        var a = address(kAudioObjectPropertyName)
        guard AudioObjectGetPropertyData(device, &a, 0, nil, &size, &name) == noErr, let name else { return nil }
        return name.takeRetainedValue() as String
    }

    static func transport(_ device: AudioDeviceID) -> UInt32 {
        var value = UInt32(0)
        var size = UInt32(MemoryLayout<UInt32>.size)
        var a = address(kAudioDevicePropertyTransportType)
        AudioObjectGetPropertyData(device, &a, 0, nil, &size, &value)
        return value
    }

    static func hasInput(_ device: AudioDeviceID) -> Bool {
        var size = UInt32(0)
        var a = address(kAudioDevicePropertyStreams, kAudioDevicePropertyScopeInput)
        return AudioObjectGetPropertyDataSize(device, &a, 0, nil, &size) == noErr && size > 0
    }

    static func all() -> [Microphone] {
        var a = address(kAudioHardwarePropertyDevices)
        var size = UInt32(0)
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size) == noErr
        else { return [] }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &ids) == noErr
        else { return [] }
        return ids.filter(hasInput).compactMap { id in
            guard let name = name(id) else { return nil }
            let t = transport(id)
            return Microphone(id: id, name: name, builtIn: t == kAudioDeviceTransportTypeBuiltIn,
                              virtual: t == kAudioDeviceTransportTypeVirtual || t == kAudioDeviceTransportTypeAggregate)
        }
    }

    static func lidClosed() -> Bool {
        let root = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPMrootDomain"))
        defer { IOObjectRelease(root) }
        let value = IORegistryEntryCreateCFProperty(root, "AppleClamshellState" as CFString, kCFAllocatorDefault, 0)
        return (value?.takeRetainedValue() as? Bool) ?? false
    }

    static var preferred: String? {
        get { UserDefaults.standard.string(forKey: preferenceKey) }
        set { UserDefaults.standard.set(newValue, forKey: preferenceKey) }
    }

    static func chosen() -> Microphone? {
        Microphones.choose(all(), preferred: preferred, defaultID: defaultID(), lidClosed: lidClosed())
    }
}
