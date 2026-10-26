// witness-slackprobe: print the Slack desktop app's accessibility tree as JSON
// lines, one node per line: {"d":depth,"role":…,"title":…,"desc":…,"value":…}.
//
// Deliberately dumb. Finding the huddle in the tree happens in Python
// (witnessd/_slack_huddle.py), because this binary is the one holding the
// Accessibility grant and an ad-hoc signed rebuild invalidates that grant.
//
// Runs as its own app bundle (WitnessSlackProbe.app), launched through
// `open`, so TCC sees it, not the daemon's Python, as the responsible
// process. Exit codes: 0 ok, 2 Slack not running, 3 not trusted.

import AppKit
import ApplicationServices
import Foundation

let slackBundle = "com.tinyspeck.slackmacgap"

var maxNodes = 20000
var maxDepth = 80
var args = CommandLine.arguments.dropFirst().makeIterator()
while let a = args.next() {
    switch a {
    case "--max-nodes": maxNodes = Int(args.next() ?? "") ?? maxNodes
    case "--max-depth": maxDepth = Int(args.next() ?? "") ?? maxDepth
    default: break
    }
}

func emit(_ obj: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: obj),
       let s = String(data: data, encoding: .utf8) {
        print(s)
    }
}

guard AXIsProcessTrusted() else {
    emit(["error": "not trusted for accessibility"])
    exit(3)
}

guard let slack = NSWorkspace.shared.runningApplications.first(where: {
    $0.bundleIdentifier?.lowercased() == slackBundle
}) else {
    emit(["error": "slack not running"])
    exit(2)
}

let app = AXUIElementCreateApplication(slack.processIdentifier)
AXUIElementSetMessagingTimeout(app, 1.0)
// Electron builds its web-content tree only for assistive clients that ask.
AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)

func attr(_ el: AXUIElement, _ name: String) -> CFTypeRef? {
    var v: CFTypeRef?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

func str(_ el: AXUIElement, _ name: String) -> String? {
    guard let v = attr(el, name) else { return nil }
    if let s = v as? String { return s.isEmpty ? nil : String(s.prefix(200)) }
    if let n = v as? NSNumber { return n.stringValue }
    return nil
}

func children(_ el: AXUIElement) -> [AXUIElement] {
    (attr(el, kAXChildrenAttribute) as? [AXUIElement]) ?? []
}

func walk(_ roots: [AXUIElement]) -> [[String: Any]] {
    var out: [[String: Any]] = []
    var stack: [(AXUIElement, Int)] = roots.reversed().map { ($0, 0) }
    while let (el, depth) = stack.popLast(), out.count < maxNodes {
        var node: [String: Any] = ["d": depth]
        for (key, name) in [("role", kAXRoleAttribute), ("subrole", kAXSubroleAttribute),
                            ("title", kAXTitleAttribute), ("desc", kAXDescriptionAttribute),
                            ("value", kAXValueAttribute), ("id", "AXDOMIdentifier"),
                            ("help", kAXHelpAttribute)] {
            if let s = str(el, name) { node[key] = s }
        }
        if let classes = attr(el, "AXDOMClassList") as? [String], !classes.isEmpty {
            node["cls"] = classes.joined(separator: " ")
        }
        out.append(node)
        if depth < maxDepth {
            stack.append(contentsOf: children(el).reversed().map { ($0, depth + 1) })
        }
    }
    return out
}

func windows() -> [AXUIElement] {
    (attr(app, kAXWindowsAttribute) as? [AXUIElement]) ?? []
}

// The tree fills in asynchronously after AXManualAccessibility flips on, so a
// first walk can come back as bare window chrome.
var nodes = walk(windows())
for _ in 0..<6 where nodes.count < 200 {
    Thread.sleep(forTimeInterval: 0.25)
    nodes = walk(windows())
}
nodes.forEach(emit)
