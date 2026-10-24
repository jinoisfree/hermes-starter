import Contacts
import Foundation

let executorVersion = "contacts-direct-v1"
let null = NSNull()

struct ContactsFailure: Error {
    let code: String
    var fields: [String: Any] = [:]
}

func rejectNoteWrites(_ args: [String: Any]) throws {
    if args.keys.contains(where: { $0.lowercased().contains("note") }) {
        throw ContactsFailure(code: "CONTACTS_NOTE_WRITE_UNSUPPORTED")
    }
}

func digits(_ value: String) -> String { value.filter { $0.isNumber } }
func text(_ args: [String: Any], _ key: String) -> String {
    (args[key] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
}
func normalizedEmail(_ value: String) -> String {
    value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
}
func displayName(_ contact: CNContact) -> String {
    CNContactFormatter.string(from: contact, style: .fullName)
        ?? [contact.familyName, contact.givenName].filter { !$0.isEmpty }.joined(separator: " ")
}
func phoneLabel(_ label: String?) -> String {
    switch label {
    case CNLabelPhoneNumberMobile, CNLabelPhoneNumberiPhone: return "mobile"
    case CNLabelWork: return "work"
    case CNLabelPhoneNumberWorkFax: return "work_fax"
    case CNLabelPhoneNumberMain: return "main"
    default: return "other"
    }
}
func emailLabel(_ label: String?) -> String {
    switch label { case CNLabelWork: return "work"; case CNLabelHome: return "home"; default: return "other" }
}
func phoneValues(_ rows: [[String: String]]) -> [CNLabeledValue<CNPhoneNumber>] {
    let labels = ["mobile": CNLabelPhoneNumberMobile, "work": CNLabelWork,
                  "work_fax": CNLabelPhoneNumberWorkFax, "main": CNLabelPhoneNumberMain, "other": CNLabelOther]
    return rows.map { CNLabeledValue(label: labels[$0["label"] ?? "other"], value: CNPhoneNumber(stringValue: $0["value"]!)) }
}
func emailValues(_ rows: [[String: String]]) -> [CNLabeledValue<NSString>] {
    let labels = ["work": CNLabelWork, "home": CNLabelHome, "other": CNLabelOther]
    return rows.map { CNLabeledValue(label: labels[$0["label"] ?? "other"], value: $0["value"]! as NSString) }
}
func matchesQuery(_ contact: CNContact, _ query: String) -> Bool {
    if query.isEmpty { return true }
    let strings = [displayName(contact), contact.familyName, contact.givenName,
                   contact.organizationName] + contact.emailAddresses.map { $0.value as String }
    if strings.contains(where: { $0.localizedCaseInsensitiveContains(query) }) { return true }
    let number = digits(query)
    return !number.isEmpty && contact.phoneNumbers.contains { digits($0.value.stringValue).contains(number) }
}
func duplicates(_ contact: CNContact, phones: [[String: String]], emails: [[String: String]]) -> Bool {
    let mail = Set(emails.map { normalizedEmail($0["value"] ?? "") }.filter { !$0.isEmpty })
    if contact.emailAddresses.contains(where: { mail.contains(normalizedEmail($0.value as String)) }) { return true }
    return phones.contains { incoming in
        let number = digits(incoming["value"] ?? "")
        return !number.isEmpty && contact.phoneNumbers.contains {
            number == digits($0.value.stringValue) &&
                (incoming["label"] == "mobile" || phoneLabel($0.label) == "mobile")
        }
    }
}

final class Executor {
    let store = CNContactStore()
    var notesAvailable = true
    var before: Any = null
    var after: Any = null
    var mutationCalls = 0
    var applied = false
    var action = ""

    func envelope(_ fields: [String: Any]) -> [String: Any] {
        var value: [String: Any] = ["before": before, "after": after, "executorVersion": executorVersion,
                                    "action": action, "mutation_calls": mutationCalls, "applied": applied]
        value.merge(fields) { _, new in new }
        return value
    }
    func status() -> String {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .notDetermined: return "notDetermined"
        case .restricted: return "restricted"
        case .denied: return "denied"
        case .authorized: return "authorized"
        @unknown default: return "limited_or_unknown"
        }
    }
    func keys() -> [CNKeyDescriptor] {
        var result: [CNKeyDescriptor] = [CNContactIdentifierKey, CNContactFamilyNameKey, CNContactGivenNameKey,
            CNContactOrganizationNameKey, CNContactDepartmentNameKey, CNContactJobTitleKey,
            CNContactPhoneNumbersKey, CNContactEmailAddressesKey, CNContactPostalAddressesKey,
            CNContactImageDataAvailableKey, CNContactTypeKey].map { $0 as CNKeyDescriptor }
        result.append(CNContactFormatter.descriptorForRequiredKeys(for: .fullName))
        if notesAvailable { result.append(CNContactNoteKey as CNKeyDescriptor) }
        return result
    }
    func fetch(_ identifier: String? = nil) throws -> [CNContact] {
        func read() throws -> [CNContact] {
            let request = CNContactFetchRequest(keysToFetch: keys())
            if let identifier { request.predicate = CNContact.predicateForContacts(withIdentifiers: [identifier]) }
            var rows: [CNContact] = []
            try store.enumerateContacts(with: request) { contact, _ in rows.append(contact) }
            return rows
        }
        do { return try read() }
        catch let error as NSError where error.domain == CNErrorDomain && error.code == CNError.Code.unauthorizedKeys.rawValue {
            // macOS can protect note reads with an additional entitlement; snapshots expose availability.
            notesAvailable = false
            return try read()
        }
    }
    func snapshot(_ contact: CNContact) throws -> [String: Any] {
        let containers = try store.containers(matching: CNContainer.predicateForContainerOfContact(withIdentifier: contact.identifier))
        return ["identifier": contact.identifier, "display_name": displayName(contact),
                "family_name": contact.familyName, "given_name": contact.givenName,
                "organization": contact.organizationName, "department": contact.departmentName,
                "job_title": contact.jobTitle,
                "phones": contact.phoneNumbers.map { ["label": phoneLabel($0.label), "value": $0.value.stringValue] },
                "emails": contact.emailAddresses.map { ["label": emailLabel($0.label), "value": $0.value as String] },
                "addresses": contact.postalAddresses.map { row -> [String: String] in
                    let a = row.value
                    return ["label": row.label ?? "other", "street": a.street, "city": a.city,
                            "state": a.state, "postal_code": a.postalCode, "country": a.country]
                },
                "note": notesAvailable && contact.isKeyAvailable(CNContactNoteKey) ? contact.note as Any : null,
                "note_available": notesAvailable, "has_image": contact.imageDataAvailable,
                "container_names": containers.map { $0.name }]
    }
    func apply(_ args: [String: Any], to contact: CNMutableContact) throws {
        try rejectNoteWrites(args)
        if let v = args["family_name"] as? String { contact.familyName = v }
        if let v = args["given_name"] as? String { contact.givenName = v }
        if let v = args["organization"] as? String { contact.organizationName = v }
        if let v = args["department"] as? String { contact.departmentName = v }
        if let v = args["job_title"] as? String { contact.jobTitle = v }
        for key in ["phones", "replace_phones", "add_phones"] {
            if let rows = args[key] as? [[String: String]] {
                let values = phoneValues(rows)
                if key == "add_phones" {
                    contact.phoneNumbers += values.filter { new in !contact.phoneNumbers.contains { digits($0.value.stringValue) == digits(new.value.stringValue) } }
                } else { contact.phoneNumbers = values }
            }
        }
        for key in ["emails", "replace_emails", "add_emails"] {
            if let rows = args[key] as? [[String: String]] {
                let values = emailValues(rows)
                if key == "add_emails" {
                    contact.emailAddresses += values.filter { new in !contact.emailAddresses.contains { normalizedEmail($0.value as String) == normalizedEmail(new.value as String) } }
                } else { contact.emailAddresses = values }
            }
        }
        if let a = args["address"] as? [String: String] {
            let address = (contact.postalAddresses.first?.value.mutableCopy() as? CNMutablePostalAddress) ?? CNMutablePostalAddress()
            if let v = a["street"] { address.street = v }
            if let v = a["city"] { address.city = v }
            if let v = a["state"] { address.state = v }
            if let v = a["postal_code"] { address.postalCode = v }
            if let v = a["country"] { address.country = v }
            let old = contact.postalAddresses.first
            let value = old?.settingValue(address) ?? CNLabeledValue(label: CNLabelWork, value: address as CNPostalAddress)
            contact.postalAddresses = [value] + Array(contact.postalAddresses.dropFirst())
        }
        if let path = args["image_path"] as? String {
            let url = URL(fileURLWithPath: path)
            let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            guard size > 0 && size <= 5 * 1024 * 1024 else { throw ContactsFailure(code: "CONTACTS_IMAGE_TOO_LARGE_OR_EMPTY") }
            let data = try Data(contentsOf: url)
            guard data.count <= 5 * 1024 * 1024 else { throw ContactsFailure(code: "CONTACTS_IMAGE_TOO_LARGE_OR_EMPTY") }
            contact.imageData = data
        }
        guard !contact.familyName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ||
                !contact.givenName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw ContactsFailure(code: "CONTACTS_NAME_REQUIRED")
        }
    }
    func commit(_ request: CNSaveRequest, using execute: ((CNSaveRequest) throws -> Void)? = nil) throws {
        mutationCalls = 1
        do {
            if let execute { try execute(request) }
            else { try store.execute(request) }
        } catch {
            // The save transaction failed. A later readback error must keep applied=true instead.
            mutationCalls = 0
            applied = false
            after = null
            throw error
        }
        applied = true
    }
    func run(_ args: [String: Any]) async throws -> [String: Any] {
        action = text(args, "action")
        guard ["access", "search", "get", "create", "update"].contains(action) else {
            throw ContactsFailure(code: "CONTACTS_ACTION_REFUSED")
        }
        if action == "create" || action == "update" { try rejectNoteWrites(args) }
        if status() == "notDetermined" { _ = try? await store.requestAccess(for: .contacts) }
        guard status() == "authorized" else {
            throw ContactsFailure(code: "CONTACTS_PERMISSION_REQUIRED", fields: ["authorization_status": status()])
        }
        if action == "access" { return envelope(["ok": true, "authorization_status": status()]) }
        if action == "search" {
            let limit = min(50, max(1, args["limit"] as? Int ?? 20))
            let matches = try fetch().filter { matchesQuery($0, text(args, "query")) }
            let items = try matches.prefix(limit).map { try snapshot($0) }
            return envelope(["ok": true, "items": items, "result_count": items.count, "truncated": matches.count > limit])
        }
        if action == "get" || action == "update" {
            guard !text(args, "contact_id").isEmpty, let contact = try fetch(text(args, "contact_id")).first else {
                throw ContactsFailure(code: "CONTACTS_NOT_FOUND")
            }
            before = try snapshot(contact)
            if action == "get" { after = before; return envelope(["ok": true]) }
            guard !text(args, "expected_name").isEmpty && text(args, "expected_name") == displayName(contact) else {
                throw ContactsFailure(code: "CONTACTS_IDENTITY_MISMATCH")
            }
            let mutable = contact.mutableCopy() as! CNMutableContact
            try apply(args, to: mutable)
            let save = CNSaveRequest(); save.update(mutable)
            try commit(save)
            guard let refreshed = try fetch(mutable.identifier).first else { throw ContactsFailure(code: "CONTACTS_READBACK_FAILED") }
            after = try snapshot(refreshed)
            return envelope(["ok": true, "target_id": mutable.identifier])
        }
        let existing = try fetch()
        let phones = args["phones"] as? [[String: String]] ?? []
        let emails = args["emails"] as? [[String: String]] ?? []
        let matches = existing.filter { duplicates($0, phones: phones, emails: emails) }
        if !matches.isEmpty && (args["allow_duplicate"] as? Bool != true) {
            throw ContactsFailure(code: "CONTACTS_DUPLICATE", fields: ["matches": try matches.map { try snapshot($0) }])
        }
        let contact = CNMutableContact()
        try apply(args, to: contact)
        let save = CNSaveRequest(); save.add(contact, toContainerWithIdentifier: nil)
        try commit(save)
        guard let refreshed = try fetch(contact.identifier).first else { throw ContactsFailure(code: "CONTACTS_READBACK_FAILED") }
        after = try snapshot(refreshed)
        return envelope(["ok": true, "target_id": contact.identifier])
    }
}

@main struct ContactsDirect {
    static func main() async {
        let executor = Executor()
        let response: [String: Any]
        do {
            guard CommandLine.arguments.count == 2,
                  let data = CommandLine.arguments[1].data(using: .utf8),
                  let args = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw ContactsFailure(code: "CONTACTS_INVALID_ARGUMENTS")
            }
            response = try await executor.run(args)
        } catch let error as ContactsFailure {
            response = executor.envelope(["ok": false, "error": error.code].merging(error.fields) { _, new in new })
        } catch let error as NSError {
            let code = error.domain == CNErrorDomain && error.code == CNError.Code.authorizationDenied.rawValue
                ? "CONTACTS_PERMISSION_REQUIRED" : "CONTACTS_NATIVE_ERROR"
            response = executor.envelope(["ok": false, "error": code, "authorization_status": executor.status(),
                                           "native_error_domain": error.domain, "native_error_code": error.code])
        }
        if let data = try? JSONSerialization.data(withJSONObject: response, options: [.sortedKeys]),
           let string = String(data: data, encoding: .utf8) { print(string) }
    }
}
