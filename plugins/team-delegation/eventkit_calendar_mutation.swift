import EventKit
import Foundation

struct MutationInput: Codable {
    let action: String
    let event_id: String?
    let expected_title: String?
    let expected_start: String?
    let calendar_id: String?
    let title: String?
    let start: String?
    let end: String?
    let all_day: Bool?
    let location: String?
    let notes: String?
    let url: String?
    let clear_location: Bool?
    let clear_notes: Bool?
    let clear_url: Bool?
    let span: String?
    let alarms: [Int]?
    let clear_alarms: Bool?
    let _restore_alarms: [AlarmSnapshot]?
}

struct AlarmSnapshot: Codable {
    let relativeOffset: Double
    let absoluteDate: String?
}

struct EventSnapshot: Codable {
    let calendarItemIdentifier: String
    let eventIdentifier: String?
    let title: String
    let start: String
    let end: String
    let isAllDay: Bool
    let calendarIdentifier: String
    let calendar: String
    let calendarSource: String
    let location: String?
    let notes: String?
    let url: String?
    let alarms: [Double]
    let alarmDetails: [AlarmSnapshot]
}

struct MutationOutput: Codable {
    let executorVersion: String
    let authority: String
    let action: String
    let before: EventSnapshot?
    let after: EventSnapshot?
    let mutationCalls: Int
}

func parseDate(_ value: String) -> Date? {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withColonSeparatorInTimeZone, .withFractionalSeconds]
    if let parsed = formatter.date(from: value) { return parsed }
    formatter.formatOptions = [.withInternetDateTime, .withColonSeparatorInTimeZone]
    return formatter.date(from: value)
}

func iso(_ value: Date) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withColonSeparatorInTimeZone]
    return formatter.string(from: value)
}

func snapshot(_ event: EKEvent) -> EventSnapshot {
    EventSnapshot(
        calendarItemIdentifier: event.calendarItemIdentifier,
        eventIdentifier: event.eventIdentifier,
        title: event.title ?? "",
        start: iso(event.startDate),
        end: iso(event.endDate),
        isAllDay: event.isAllDay,
        calendarIdentifier: event.calendar.calendarIdentifier,
        calendar: event.calendar.title,
        calendarSource: event.calendar.source.title,
        location: event.location,
        notes: event.notes,
        url: event.url?.absoluteString,
        alarms: (event.alarms ?? []).filter { $0.absoluteDate == nil }.map { -$0.relativeOffset / 60 },
        alarmDetails: (event.alarms ?? []).map { AlarmSnapshot(relativeOffset: $0.relativeOffset, absoluteDate: $0.absoluteDate.map(iso)) }
    )
}

func matchesIdentity(itemID: String, eventID: String?, title: String, start: Date,
                     requestedID: String, expectedTitle: String, expectedStart: Date) -> Bool {
    (itemID == requestedID || eventID == requestedID)
        && title == expectedTitle && abs(start.timeIntervalSince(expectedStart)) < 1
}

func applyAlarms(_ input: MutationInput, to event: EKEvent) throws {
    guard input.alarms != nil || input.clear_alarms == true || input._restore_alarms != nil else { return }
    // Do not replace alarm types that this snapshot cannot faithfully restore.
    guard !(event.alarms ?? []).contains(where: { $0.structuredLocation != nil || $0.type != .display }) else {
        throw NSError(domain: "TE_CALENDAR_ALARM_TYPE_UNSUPPORTED", code: 65)
    }
    if let saved = input._restore_alarms {
        event.alarms = try saved.map { value in
            if let raw = value.absoluteDate {
                guard let date = parseDate(raw) else { throw NSError(domain: "TE_CALENDAR_ALARM_DATE_INVALID", code: 65) }
                return EKAlarm(absoluteDate: date)
            }
            return EKAlarm(relativeOffset: value.relativeOffset)
        }
    } else if let minutes = input.alarms {
        guard minutes.count <= 5, minutes.allSatisfy({ $0 >= 0 && $0 <= 5256000 }), input.clear_alarms != true else {
            throw NSError(domain: "TE_CALENDAR_ALARMS_INVALID", code: 65)
        }
        event.alarms = minutes.map { EKAlarm(relativeOffset: -Double($0) * 60) }
    } else {
        event.alarms = []
    }
}

func requireAccess(_ store: EKEventStore) async throws -> Bool {
    switch EKEventStore.authorizationStatus(for: .event) {
    case .fullAccess, .authorized:
        return true
    case .notDetermined:
        if #available(macOS 14.0, *) {
            return try await store.requestFullAccessToEvents()
        }
        return try await withCheckedThrowingContinuation { continuation in
            store.requestAccess(to: .event) { ok, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: ok) }
            }
        }
    default:
        return false
    }
}

@main
struct CalendarMutation {
    static func main() async {
        guard CommandLine.arguments.count == 2,
              let data = CommandLine.arguments[1].data(using: .utf8),
              let input = try? JSONDecoder().decode(MutationInput.self, from: data)
        else {
            fputs("TE_CALENDAR_MUTATION_ARGUMENT_ERROR\n", stderr)
            exit(64)
        }

        let store = EKEventStore()
        do {
            guard try await requireAccess(store) else {
                fputs("TE_CALENDAR_PERMISSION_DENIED\n", stderr)
                exit(77)
            }
            let span: EKSpan = input.span == "future" ? .futureEvents : .thisEvent
            var before: EventSnapshot?
            var after: EventSnapshot?

            if input.action == "create" {
                guard let title = input.title,
                      let startRaw = input.start, let start = parseDate(startRaw),
                      let endRaw = input.end, let end = parseDate(endRaw), end > start
                else {
                    fputs("TE_CALENDAR_CREATE_FIELDS_INVALID\n", stderr)
                    exit(65)
                }
                let event = EKEvent(eventStore: store)
                event.title = title
                event.startDate = start
                event.endDate = end
                event.isAllDay = input.all_day ?? false
                if let calendarID = input.calendar_id {
                    guard let calendar = store.calendar(withIdentifier: calendarID) else {
                        fputs("TE_CALENDAR_NOT_FOUND\n", stderr)
                        exit(66)
                    }
                    event.calendar = calendar
                } else {
                    event.calendar = store.defaultCalendarForNewEvents
                }
                event.location = input.location
                event.notes = input.notes
                if let rawURL = input.url { event.url = URL(string: rawURL) }
                try applyAlarms(input, to: event)
                try store.save(event, span: .thisEvent, commit: true)
                after = snapshot(event)
            } else {
                guard let eventID = input.event_id else {
                    throw NSError(domain: "TE_CALENDAR_EVENT_NOT_FOUND", code: 66)
                }
                // Lookup exposes both identifiers; external (iCalUID) is never accepted.
                let direct = (store.calendarItem(withIdentifier: eventID) as? EKEvent)
                    ?? store.event(withIdentifier: eventID)
                guard var event = direct else {
                    throw NSError(domain: "TE_CALENDAR_EVENT_NOT_FOUND", code: 66)
                }
                if input.action == "get" {
                    // Read-only snapshot for the linked-work watcher; recurring events are flagged.
                    let current = snapshot(event)
                    let output = MutationOutput(
                        executorVersion: "1", authority: "EventKit/macOS Calendar",
                        action: event.hasRecurrenceRules ? "get_recurring" : "get",
                        before: current, after: current, mutationCalls: 0
                    )
                    let encoder = JSONEncoder()
                    encoder.outputFormatting = [.sortedKeys]
                    FileHandle.standardOutput.write(try encoder.encode(output))
                    FileHandle.standardOutput.write(Data("\n".utf8))
                    return
                }
                guard let expectedTitle = input.expected_title,
                      let rawStart = input.expected_start, let expectedStart = parseDate(rawStart) else {
                    throw NSError(domain: "TE_CALENDAR_EXPECTED_IDENTITY_REQUIRED", code: 65)
                }
                // A recurring identifier can resolve to the series' first occurrence.
                // Search the requested occurrence, but never by title alone.
                let predicate = store.predicateForEvents(withStart: expectedStart.addingTimeInterval(-1),
                    end: expectedStart.addingTimeInterval(1), calendars: [event.calendar])
                let matches = store.events(matching: predicate).filter {
                    matchesIdentity(itemID: $0.calendarItemIdentifier, eventID: $0.eventIdentifier,
                        title: $0.title ?? "", start: $0.startDate, requestedID: eventID,
                        expectedTitle: expectedTitle, expectedStart: expectedStart)
                }
                guard matches.count == 1 else {
                    throw NSError(domain: "TE_CALENDAR_IDENTITY_MISMATCH_OR_AMBIGUOUS", code: 65)
                }
                event = matches[0]
                before = snapshot(event)
                if input.action == "delete" {
                    try store.remove(event, span: span, commit: true)
                } else if input.action == "update" {
                    if let title = input.title { event.title = title }
                    if let raw = input.start {
                        guard let value = parseDate(raw) else { throw NSError(domain: "TE_CALENDAR_START_INVALID", code: 65) }
                        event.startDate = value
                    }
                    if let raw = input.end {
                        guard let value = parseDate(raw) else { throw NSError(domain: "TE_CALENDAR_END_INVALID", code: 65) }
                        event.endDate = value
                    }
                    guard event.endDate > event.startDate else { throw NSError(domain: "TE_CALENDAR_RANGE_INVALID", code: 65) }
                    if let allDay = input.all_day { event.isAllDay = allDay }
                    if input.clear_location == true { event.location = nil }
                    else if let location = input.location { event.location = location }
                    if input.clear_notes == true { event.notes = nil }
                    else if let notes = input.notes { event.notes = notes }
                    if input.clear_url == true { event.url = nil }
                    else if let rawURL = input.url { event.url = URL(string: rawURL) }
                    if let calendarID = input.calendar_id {
                        guard let calendar = store.calendar(withIdentifier: calendarID) else {
                            fputs("TE_CALENDAR_NOT_FOUND\n", stderr)
                            exit(66)
                        }
                        event.calendar = calendar
                    }
                    try applyAlarms(input, to: event)
                    try store.save(event, span: span, commit: true)
                    after = snapshot(event)
                } else {
                    fputs("TE_CALENDAR_MUTATION_ACTION_INVALID\n", stderr)
                    exit(64)
                }
            }

            let output = MutationOutput(
                executorVersion: "1", authority: "EventKit/macOS Calendar",
                action: input.action, before: before, after: after, mutationCalls: 1
            )
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            FileHandle.standardOutput.write(try encoder.encode(output))
            FileHandle.standardOutput.write(Data("\n".utf8))
        } catch {
            fputs("TE_CALENDAR_MUTATION_ERROR: \(error)\n", stderr)
            exit(70)
        }
    }
}
