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

    /// The device's own input rate and channel count, which AVAudioEngine can misreport right after a device switch.
    static func inputShape(_ device: AudioDeviceID) -> AudioShape? {
        var rate = Float64(0)
        var size = UInt32(MemoryLayout<Float64>.size)
        var a = address(kAudioDevicePropertyNominalSampleRate)
        guard AudioObjectGetPropertyData(device, &a, 0, nil, &size, &rate) == noErr else { return nil }
        var c = address(kAudioDevicePropertyStreamConfiguration, kAudioDevicePropertyScopeInput)
        guard AudioObjectGetPropertyDataSize(device, &c, 0, nil, &size) == noErr, size > 0 else { return nil }
        let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(device, &c, 0, nil, &size, raw) == noErr else { return nil }
        let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
        return AudioShape(sampleRate: rate, channels: list.reduce(0) { $0 + $1.mNumberChannels })
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
                              virtual: t == kAudioDeviceTransportTypeVirtual || t == kAudioDeviceTransportTypeAggregate,
                              bluetooth: t == kAudioDeviceTransportTypeBluetooth || t == kAudioDeviceTransportTypeBluetoothLE)
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

    static func chosen(excluding: Set<UInt32> = []) -> Microphone? {
        let devices = DeviceList.shared.snapshot
        return Microphones.choose(devices.mics, preferred: preferred, defaultID: devices.defaultID,
                                  lidClosed: devices.lidClosed, excluding: excluding)
    }
}

/// The input devices, the default one, and the lid, read from the system only when Core Audio says the devices
/// changed (or a press asks for a fresh look), not on every redraw of the panel.
final class DeviceList {
    struct Snapshot {
        let mics: [Microphone]
        let defaultID: UInt32?
        let lidClosed: Bool
    }

    static let shared: DeviceList = {
        let list = DeviceList { Snapshot(mics: InputDevice.all(), defaultID: InputDevice.defaultID(), lidClosed: InputDevice.lidClosed()) }
        list.listen()
        return list
    }()

    private let query: () -> Snapshot
    private let lock = NSLock()
    private var cached: Snapshot?
    /// Runs on the main thread after the devices change.
    var onChange: (() -> Void)?

    init(query: @escaping () -> Snapshot) { self.query = query }

    var snapshot: Snapshot {
        lock.withLock {
            if let cached { return cached }
            let fresh = query()
            cached = fresh
            return fresh
        }
    }

    /// Forgets the cached devices, so the next look reads them again.
    func changed() {
        lock.withLock { cached = nil }
        DispatchQueue.main.async { self.onChange?() }
    }

    private func listen() {
        for selector in [kAudioHardwarePropertyDevices, kAudioHardwarePropertyDefaultInputDevice] {
            var a = InputDevice.address(selector)
            AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &a, DispatchQueue.main) { [weak self] _, _ in
                self?.changed()
            }
        }
        listenForLid()
    }

    /// IOKit's `kIOPMMessageClamshellStateChange`: `iokit_family_msg(sub_iokit_powermanagement, 0x100)`.
    static let clamshellMessage: UInt32 = 0xE003_4100
    private var lidPort: IONotificationPortRef?
    private var lidNotifier: io_object_t = 0
    /// Power management tells this list when the lid opens or closes.
    var listeningForLid: Bool { lidPort != nil }

    /// Closing the lid makes the built-in microphone unusable without any Core Audio device change, so the lid gets
    /// its own listener: power management's interest notifications on the root domain.
    private func listenForLid() {
        let root = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPMrootDomain"))
        guard root != 0, let port = IONotificationPortCreate(kIOMainPortDefault) else { return }
        defer { IOObjectRelease(root) }
        IONotificationPortSetDispatchQueue(port, DispatchQueue.main)
        let result = IOServiceAddInterestNotification(port, root, kIOGeneralInterest, { context, _, message, _ in
            guard let context else { return }
            Unmanaged<DeviceList>.fromOpaque(context).takeUnretainedValue().powerMessage(message)
        }, Unmanaged.passUnretained(self).toOpaque(), &lidNotifier)
        if result == KERN_SUCCESS {
            lidPort = port
        } else {
            IONotificationPortDestroy(port)
        }
    }

    /// A power management message: the lid opening or closing means the devices to choose from changed.
    func powerMessage(_ message: UInt32) {
        if message == Self.clamshellMessage { changed() }
    }
}
