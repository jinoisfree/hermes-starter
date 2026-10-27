import Foundation
import EventKit

struct AlarmRow: Codable {
    let relativeOffset: Double
    let absoluteDate: String?
}

struct EventRow: Codable {
    let eventIdentifier: String?
    let calendarItemIdentifier: String
    let externalIdentifier: String?
    let title: String
    let start: String
    let end: String
    let isAllDay: Bool
    let calendar: String
    let calendarIdentifier: String
    let location: String?
    let alarms: [Double]
    let alarmDetails: [AlarmRow]
    let conflict: Bool
    // Zoom link in title/location/notes/URL; notes and URL themselves stay out of the output.
    let zoomMeeting: Bool
    let recurring: Bool
    let writable: Bool
}

func isZoomMeeting(_ event: EKEvent) -> Bool {
    let text = [event.title, event.location, event.notes, event.url?.absoluteString]
        .compactMap { $0?.lowercased() }.joined(separator: "\n")
    return text.contains("zoom.us") || text.contains("zoomgov.com") || (event.title ?? "").lowercased().contains("zoom")
}

struct CalendarRow: Codable {
    let title: String
    let identifier: String
    let source: String
    let eventCount: Int
}

struct Output: Codable {
    let executorVersion: String
    let authority: String
    let timezone: String
    let rangeStartInclusive: String
    let rangeEndExclusive: String
    let selectionRule: String
    let queriedAt: String
    let sourceQueryCalls: Int
    let calendars: [CalendarRow]
    let events: [EventRow]
    let conflictCount: Int
    let mutationCalls: Int
}

func iso(_ value: Date, timezone: TimeZone) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withColonSeparatorInTimeZone]
    formatter.timeZone = timezone
    return formatter.string(from: value)
}

@main
struct CalendarQuery {
    static func main() async {
        guard CommandLine.arguments.count == 3 else {
            fputs("usage: eventkit_calendar_query YYYY-MM-DD TimeZone\n", stderr)
            exit(64)
        }
        let requestedDate = CommandLine.arguments[1]
        let timezoneName = CommandLine.arguments[2]
        guard let timezone = TimeZone(identifier: timezoneName) else {
            fputs("TE_CALENDAR_TIMEZONE_INVALID\n", stderr)
            exit(65)
        }
        let parts = requestedDate.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else {
            fputs("TE_CALENDAR_DATE_INVALID\n", stderr)
            exit(65)
        }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timezone
        guard let start = calendar.date(from: DateComponents(
            year: parts[0], month: parts[1], day: parts[2], hour: 0, minute: 0, second: 0
        )), let end = calendar.date(byAdding: .day, value: 1, to: start) else {
            fputs("TE_CALENDAR_DATE_INVALID\n", stderr)
            exit(65)
        }

        let store = EKEventStore()
        do {
            let status = EKEventStore.authorizationStatus(for: .event)
            let granted: Bool
            switch status {
            case .fullAccess, .authorized:
                granted = true
            case .notDetermined:
                if #available(macOS 14.0, *) {
                    granted = try await store.requestFullAccessToEvents()
                } else {
                    granted = try await withCheckedThrowingContinuation { continuation in
                        store.requestAccess(to: .event) { ok, error in
                            if let error { continuation.resume(throwing: error) }
                            else { continuation.resume(returning: ok) }
                        }
                    }
                }
            default:
                granted = false
            }
            guard granted else {
                fputs("TE_CALENDAR_PERMISSION_DENIED\n", stderr)
                exit(77)
            }

            let calendars = store.calendars(for: .event)
            let predicate = store.predicateForEvents(withStart: start, end: end, calendars: calendars)
            let selected = store.events(matching: predicate)
                .filter { $0.endDate > start && $0.startDate < end }
                .sorted {
                    if $0.startDate != $1.startDate { return $0.startDate < $1.startDate }
                    if $0.endDate != $1.endDate { return $0.endDate < $1.endDate }
                    return ($0.title ?? "").localizedCaseInsensitiveCompare($1.title ?? "") == .orderedAscending
                }

            var conflictIndexes = Set<Int>()
            if selected.count > 1 {
                for left in 0..<(selected.count - 1) {
                    for right in (left + 1)..<selected.count {
                        if selected[left].startDate < selected[right].endDate && selected[left].endDate > selected[right].startDate {
                            conflictIndexes.insert(left)
                            conflictIndexes.insert(right)
                        }
                    }
                }
            }
            let rows = selected.enumerated().map { index, event in
                EventRow(
                    eventIdentifier: event.eventIdentifier,
                    calendarItemIdentifier: event.calendarItemIdentifier,
                    externalIdentifier: event.calendarItemExternalIdentifier,
                    title: event.title ?? "",
                    start: iso(event.startDate, timezone: timezone),
                    end: iso(event.endDate, timezone: timezone),
                    isAllDay: event.isAllDay,
                    calendar: event.calendar.title,
                    calendarIdentifier: event.calendar.calendarIdentifier,
                    location: event.location,
                    alarms: (event.alarms ?? []).filter { $0.absoluteDate == nil }.map { -$0.relativeOffset / 60 },
                    alarmDetails: (event.alarms ?? []).map { AlarmRow(relativeOffset: $0.relativeOffset, absoluteDate: $0.absoluteDate.map { iso($0, timezone: timezone) }) },
                    conflict: conflictIndexes.contains(index),
                    zoomMeeting: isZoomMeeting(event),
                    recurring: event.hasRecurrenceRules,
                    writable: event.calendar.allowsContentModifications
                )
            }
            let calendarRows = calendars
                .map { item in
                    CalendarRow(
                        title: item.title,
                        identifier: item.calendarIdentifier,
                        source: item.source.title,
                        eventCount: selected.filter { $0.calendar.calendarIdentifier == item.calendarIdentifier }.count
                    )
                }
                .sorted { $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending }
            let output = Output(
                executorVersion: "1",
                authority: "EventKit/macOS Calendar",
                timezone: timezoneName,
                rangeStartInclusive: iso(start, timezone: timezone),
                rangeEndExclusive: iso(end, timezone: timezone),
                selectionRule: "event.end > start && event.start < end",
                queriedAt: iso(Date(), timezone: timezone),
                sourceQueryCalls: 1,
                calendars: calendarRows,
                events: rows,
                conflictCount: conflictIndexes.count,
                mutationCalls: 0
            )
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            FileHandle.standardOutput.write(try encoder.encode(output))
            FileHandle.standardOutput.write(Data("\n".utf8))
        } catch {
            fputs("TE_CALENDAR_EVENTKIT_ERROR: \(error)\n", stderr)
            exit(70)
        }
    }
}
