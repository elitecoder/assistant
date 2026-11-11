import AppKit
import Carbon.HIToolbox
import ServiceManagement
import SessionCore
import SwiftUI
import UserNotifications

@main
enum Main {
    static let delegate = AppDelegate()

    static func main() {
        if CommandLine.arguments.contains("--print") {
            printScan()
            return
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.delegate = delegate
        app.run()
    }

    static func printScan() {
        let result = SessionScanner().scan()
        if let error = result.error { print(error) }
        for item in result.items where item.attention != .working {
            print("[\(item.attention)] \(item.workspace.title)  (\(item.idleText) idle, \(item.workspace.ref))")
            print("  goal:   \(item.goal)".prefix(120))
            print("  claude: \(item.question)".prefix(120))
            if !item.pullRequests.isEmpty {
                print("  PRs:    " + item.pullRequests.map { "#\($0.number) \($0.state)" }.joined(separator: ", "))
            }
        }
        print("working: \(result.items(.working).count), not tracked: \(result.untracked.count)")
    }
}

final class Model: ObservableObject {
    @Published var result = ScanResult()
    @Published var scanning = false
    @Published var lastScan: Date?
    @Published var opensAtLogin = SMAppService.mainApp.status == .enabled
    var onScan: ((ScanResult) -> Void)?
    var onFocus: (() -> Void)?

    private let scanner = SessionScanner()
    private let queue = DispatchQueue(label: "assistant.scan")

    func refresh() {
        guard !scanning else { return }
        scanning = true
        queue.async {
            let r = self.scanner.scan()
            DispatchQueue.main.async {
                self.result = r
                self.scanning = false
                self.lastScan = Date()
                self.onScan?(r)
            }
        }
    }

    func focus(_ ws: Workspace) {
        onFocus?()
        queue.async { SessionScanner.focus(ws) }
        NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: "/Applications/cmux.app"),
                                           configuration: NSWorkspace.OpenConfiguration())
    }

    func setOpensAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            NSLog("Assistant: login item change failed: \(error)")
        }
        opensAtLogin = SMAppService.mainApp.status == .enabled
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let model = Model()
    var statusItem: NSStatusItem!
    let popover = NSPopover()
    var notified = Set<String>()
    var timer: Timer?
    var hotKey: EventHotKeyRef?

    func applicationDidFinishLaunching(_ note: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.action = #selector(toggle)
        statusItem.button?.target = self
        updateIcon()

        popover.behavior = .transient
        popover.contentViewController = NSHostingController(rootView: PanelView(model: model))

        model.onScan = { [weak self] r in
            self?.updateIcon()
            self?.notifyNew(r)
        }
        model.onFocus = { [weak self] in self?.popover.performClose(nil) }
        model.refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 120, repeats: true) { [weak self] _ in self?.model.refresh() }

        if Bundle.main.bundleIdentifier != nil {
            UNUserNotificationCenter.current().delegate = self
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        }
        registerHotKey()
        if CommandLine.arguments.contains("--show") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.show() }
        }
    }

    @objc func toggle() {
        if popover.isShown {
            popover.performClose(nil)
        } else {
            show()
        }
    }

    func show() {
        guard let button = statusItem.button else { return }
        model.refresh()
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }

    func updateIcon() {
        let count = model.result.items(.needsYou).count
        let symbol = count > 0 ? "person.crop.circle.badge.exclamationmark" : "person.crop.circle.badge.checkmark"
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Assistant")
        image?.isTemplate = true
        statusItem.button?.image = image
        statusItem.button?.imagePosition = .imageLeading
        statusItem.button?.title = count > 0 ? " \(count)" : ""
        statusItem.button?.toolTip = count > 0 ? "\(count) sessions need you (⌃⌥Space)" : "Nothing needs you (⌃⌥Space)"
    }

    func notifyNew(_ r: ScanResult) {
        let needs = r.items(.needsYou)
        let fresh = needs.filter { !notified.contains($0.id) }
        notified = Set(needs.map(\.id))
        guard !fresh.isEmpty, Bundle.main.bundleIdentifier != nil else { return }
        let content = UNMutableNotificationContent()
        content.title = fresh.count == 1 ? "A session needs you" : "\(fresh.count) sessions need you"
        content.body = fresh.prefix(4).map { "\($0.workspace.title): \($0.question)" }.joined(separator: "\n")
        content.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString,
                                                                     content: content, trigger: nil))
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        DispatchQueue.main.async { self.show() }
        done()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }

    func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
            DispatchQueue.main.async { Main.delegate.toggle() }
            return noErr
        }, 1, &spec, nil, nil)
        let id = EventHotKeyID(signature: OSType(0x4153_5354), id: 1)
        RegisterEventHotKey(UInt32(kVK_Space), UInt32(controlKey | optionKey), id, GetApplicationEventTarget(), 0,
                            &hotKey)
    }
}
