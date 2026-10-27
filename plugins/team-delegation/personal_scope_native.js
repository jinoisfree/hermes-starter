// Read metadata only. Never create, move or edit an item from this resolver.
function run(argv) {
    const input = JSON.parse(argv[0]);
    if (input.domain === 'notes') {
        const app = Application('Notes');
        const folders = [];
        app.accounts().forEach(a => a.folders().forEach(f => folders.push({
            id: f.id(), name: f.name(), account: a.name(), account_id: a.id()
        })));
        let current = null, original = null;
        if (input.id) {
            // Enumeration succeeded, so a retired store can be classified without
            // using an AppleEvents error as evidence that an object was deleted.
            const store = value => String(value).match(/^x-coredata:\/\/([^/]+)\//);
            const wantedStore = store(input.id);
            const stores = folders.map(f => store(f.id)).filter(Boolean).map(m => m[1]);
            if (wantedStore && stores.length && !stores.includes(wantedStore[1])) {
                return JSON.stringify({folders, current, source_status: 'missing',
                    source_reason: 'notes_store_missing'});
            }
            try {
                const note = app.notes.byId(input.id);
                const parent = note.container();
                const found = folders.filter(f => f.id === parent.id());
                if (found.length !== 1) throw Error('scope_note_folder_unverified');
                current = found[0];
                original = {title: note.name(), created_at: note.creationDate().toISOString(),
                    folder_id: current.id, folder_name: current.name,
                    account_id: current.account_id, account_name: current.account};
            } catch (error) {
                // Generic transport/permission errors must never cause detachment.
                if (Number(error.errorNumber) === -1728) {
                    return JSON.stringify({folders, current: null, source_status: 'missing',
                        source_reason: 'note_not_found'});
                }
                throw error;
            }
        }
        return JSON.stringify({folders: folders, current: current, original: original});
    }

    if (input.domain === 'todo') {
        const app = Application('Reminders');
        const lists = app.lists().map(l => ({id: l.id(), name: l.name(), color: l.color()}));
        let current = null;
        if (input.id) {
            const reminder = app.reminders.byId(input.id);
            const found = lists.filter(l => l.id === reminder.container().id());
            if (found.length !== 1) throw Error('scope_reminder_list_unverified');
            current = found[0];
        }
        return JSON.stringify({lists: lists, current: current});
    }
    throw Error('scope_domain_invalid');
}
